"""Claude Code Hooks -- Shared Library.

No PEP 723 header -- imported by dispatchers, not executed directly.
Uses ONLY stdlib modules.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = HOOKS_DIR / "logs"
CONFIG_FILE = HOOKS_DIR / "config.env"
LEARNINGS_DB_PATH = HOOKS_DIR / "learnings.db"
SCHEMA_FILE = HOOKS_DIR / "schema.sql"
LEARNINGS_SCHEMA_FILE = HOOKS_DIR / "learnings_schema.sql"

PROFILE_LEVELS = {"minimal": 0, "standard": 1, "strict": 2}

DANGEROUS_PATH_CHARS = re.compile(r'[;`$|&()\']')

MAX_DB_RETRIES = 3
DB_RETRY_DELAY_S = 0.5
DB_BUSY_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Reads config.env into a dict. Cached after first load."""

    _cache: dict[str, str] | None = None

    @classmethod
    def load(cls) -> dict[str, str]:
        if cls._cache is not None:
            return cls._cache
        cfg: dict[str, str] = {}
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    # Strip inline comments (e.g., "10485760  # 10MB")
                    if "#" in value:
                        value = value.split("#")[0].strip()
                    cfg[key] = value
        cls._cache = cfg
        return cfg

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        return cls.load().get(key, default)


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------

class KillSwitch:
    """Checks .disabled and .disabled.<name> files."""

    @staticmethod
    def is_disabled(dispatcher_name: str) -> bool:
        if (HOOKS_DIR / ".disabled").exists():
            return True
        if (HOOKS_DIR / f".disabled.{dispatcher_name}").exists():
            return True
        return False


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """Per-dispatcher log file with rotation at 10MB."""

    def __init__(self, dispatcher_name: str) -> None:
        self.name = dispatcher_name
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = LOGS_DIR / f"{dispatcher_name}.log"
        try:
            self.max_bytes = int(Config.get("HOOKS_LOG_MAX_BYTES", "10485760"))
        except ValueError:
            self.max_bytes = 10485760
        self._start_time: float = 0.0

    def start(self) -> None:
        self._start_time = time.monotonic()
        self.log("INFO", "START")

    def log(self, level: str, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"[{ts}] [{level}] [{self.name}] {message}\n"
        self._rotate_if_needed()
        try:
            with open(self.log_file, "a") as f:
                f.write(entry)
        except OSError:
            pass  # Never let logging crash the hook

    def end(self, exit_code: int) -> None:
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        self.log("INFO", f"END exit={exit_code} duration={elapsed_ms}ms")

    def duration_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)

    def _rotate_if_needed(self) -> None:
        try:
            if self.log_file.exists() and self.log_file.stat().st_size > self.max_bytes:
                old = self.log_file.with_suffix(".log.old")
                self.log_file.rename(old)
        except OSError:
            pass

    def audit(self, db: "SessionDB | None", session_id: str,
              event_type: str, exit_code: int) -> None:
        """Write to hook_events table."""
        if db is None:
            return
        duration = self.duration_ms()
        try:
            db.exec(
                "INSERT INTO hook_events (session_id, event_type, dispatcher, "
                "payload_summary, exit_code, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, event_type, self.name, "", exit_code, duration),
            )
        except Exception:
            pass  # Never let audit crash the hook


# ---------------------------------------------------------------------------
# PayloadParser
# ---------------------------------------------------------------------------

class PayloadParser:
    """Reads stdin JSON once, exposes all fields as properties."""

    def __init__(self) -> None:
        raw = sys.stdin.read()
        try:
            self._data: dict[str, Any] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self._data = {}

    @property
    def payload(self) -> dict[str, Any]:
        return self._data

    @property
    def session_id(self) -> str:
        return str(self._data.get("session_id", ""))

    @property
    def tool_name(self) -> str:
        return str(self._data.get("tool_name", ""))

    @property
    def tool_input(self) -> dict[str, Any]:
        ti = self._data.get("tool_input", {})
        return ti if isinstance(ti, dict) else {}

    @property
    def file_path(self) -> str:
        fp = self.tool_input.get("file_path", "")
        if not fp:
            fp = self._data.get("file_path", "")
        return str(fp) if fp else ""

    @property
    def project_dir(self) -> str:
        return str(self._data.get("project_dir", ""))

    @property
    def db_path(self) -> str:
        pd = self.project_dir
        if pd:
            return str(Path(pd) / ".claude-session.db")
        return ""

    @property
    def agent_id(self) -> str:
        return str(self._data.get("agent_id", ""))

    @property
    def hook_event(self) -> str:
        return str(self._data.get("event", ""))

    @property
    def command(self) -> str:
        """Extract command text from Bash tool input."""
        return str(self.tool_input.get("command", ""))

    def get(self, *keys: str, default: Any = "") -> Any:
        """Walk into nested keys: get('tool_input', 'file_path')"""
        obj: Any = self._data
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
            else:
                return default
            if obj is None:
                return default
        return obj

    def validate_path(self, path: str, name: str) -> None:
        """Reject paths with shell metacharacters. Raises SystemExit(2)."""
        if DANGEROUS_PATH_CHARS.search(path):
            print(f"ERROR: suspicious characters in {name}: {path}",
                  file=sys.stderr)
            sys.exit(2)

    def validate_all_paths(self) -> None:
        """Validate file_path and project_dir if present."""
        if self.file_path:
            self.validate_path(self.file_path, "file_path")
        if self.project_dir:
            self.validate_path(self.project_dir, "project_dir")


# ---------------------------------------------------------------------------
# SessionDB
# ---------------------------------------------------------------------------

class SessionDB:
    """SQLite wrapper with WAL + busy_timeout + 3-retry on SQLITE_BUSY."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if not db_path:
            raise ValueError("db_path is empty")
        self._conn = sqlite3.connect(db_path, timeout=DB_BUSY_TIMEOUT_MS / 1000)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """Create all tables from schema.sql."""
        if SCHEMA_FILE.exists():
            schema_sql = SCHEMA_FILE.read_text()
            self._conn.executescript(schema_sql)

    def exec(self, sql: str, params: tuple = ()) -> None:
        """Write with 3-retry on OperationalError (SQLITE_BUSY)."""
        for attempt in range(MAX_DB_RETRIES):
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt < MAX_DB_RETRIES - 1:
                        time.sleep(DB_RETRY_DELAY_S)
                        continue
                raise

    def exec_many(self, statements: list[tuple[str, tuple]]) -> None:
        """Execute multiple statements in a single transaction."""
        for attempt in range(MAX_DB_RETRIES):
            try:
                cursor = self._conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                for sql, params in statements:
                    cursor.execute(sql, params)
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                self._conn.rollback()
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt < MAX_DB_RETRIES - 1:
                        time.sleep(DB_RETRY_DELAY_S)
                        continue
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Read, returns list of dicts."""
        cursor = self._conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def query_scalar(self, sql: str, params: tuple = ()) -> Any:
        """Return a single scalar value or None."""
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        if row:
            return row[0]
        return None

    def cleanup_zombies(self, current_session_id: str) -> int:
        """Mark stale agents from previous sessions. Returns count."""
        self.exec(
            "UPDATE agent_registry SET status = 'aborted_stale' "
            "WHERE status = 'running' AND session_id != ?",
            (current_session_id,),
        )
        count = self.query_scalar(
            "SELECT COUNT(*) FROM agent_registry WHERE status = 'aborted_stale'"
        )
        return int(count or 0)

    def prune_failed_paths(self, ttl_days: int = 7) -> None:
        """Mark old failed paths as irrelevant."""
        self.exec(
            "UPDATE failed_paths SET still_relevant = 0 "
            "WHERE still_relevant = 1 AND timestamp < datetime('now', ?)",
            (f"-{ttl_days} days",),
        )

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class Notifier:
    """Desktop (osascript), mobile (ntfy with TLS+auth), escalation."""

    @staticmethod
    def desktop(title: str, message: str) -> None:
        """macOS Notification Center via osascript. Non-blocking."""
        msg_escaped = message.replace('"', '\\"').replace("'", "\u2019")
        title_escaped = title.replace('"', '\\"')
        try:
            subprocess.Popen(
                [
                    "osascript", "-e",
                    f'display notification "{msg_escaped}" with title "{title_escaped}"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    @staticmethod
    def mobile(title: str, message: str) -> None:
        """Push via ntfy.sh with TLS + bearer auth. Non-blocking."""
        topic = Config.get("NTFY_TOPIC")
        if not topic:
            return
        url = Config.get("NTFY_URL", "https://ntfy.sh")
        token = Config.get("NTFY_TOKEN")

        # Strip sensitive details for mobile
        safe_message = message[:200]

        cmd = [
            "curl", "-s",
            "-H", f"Title: {title}",
            "-H", "Priority: high",
        ]
        if token:
            cmd += ["-H", f"Authorization: Bearer {token}"]
        cmd += ["-d", safe_message, f"{url}/{topic}"]

        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    @classmethod
    def escalation(cls, title: str, message: str) -> None:
        """Desktop + mobile always, regardless of walkaway."""
        cls.desktop(title, message)
        cls.mobile(title, message)

    @staticmethod
    def tts(message: str, severity: str = "info") -> None:
        """TTS cascade: macOS say -> OpenAI TTS -> ElevenLabs."""
        enabled = Config.get("HOOK_TTS_ENABLED", "false").lower()
        if enabled != "true":
            return
        min_severity = Config.get("HOOK_TTS_MIN_SEVERITY", "block")
        severity_levels = {"info": 0, "warn": 1, "block": 2, "escalation": 3}
        if severity_levels.get(severity, 0) < severity_levels.get(min_severity, 2):
            return

        provider = Config.get("HOOK_TTS_PROVIDER", "say")
        safe = message[:300]

        if provider == "say":
            try:
                rate = "175" if severity in ("block", "escalation") else "200"
                subprocess.Popen(
                    ["say", "-r", rate, safe],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass

    @staticmethod
    def is_walkaway() -> bool:
        return Path.home().joinpath(".claude", ".walkaway").exists()


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------

class ProfileManager:
    """Reads HOOK_PROFILE env var, falls back to git branch inference."""

    @staticmethod
    def get_profile() -> str:
        explicit = os.environ.get("HOOK_PROFILE", "").lower()
        if explicit in PROFILE_LEVELS:
            return explicit
        # Infer from git branch
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=5,
            )
            branch = result.stdout.strip()
        except Exception:
            return "standard"

        if branch in ("main", "master") or branch.startswith(("release/", "production/")):
            return "strict"
        if branch.startswith(("experimental/", "spike/")):
            return "minimal"
        return "standard"

    @staticmethod
    def should_execute(profile_tag: str) -> bool:
        """Check if the current profile level >= the tag's required level."""
        current = ProfileManager.get_profile()
        current_level = PROFILE_LEVELS.get(current, 1)
        required_level = PROFILE_LEVELS.get(profile_tag, 1)
        return current_level >= required_level


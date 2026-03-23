# /// script
# requires-python = ">=3.10"
# ///

"""session_start dispatcher -- DB init, zombie cleanup, project detection,
prune failed paths, restore context + learnings, git status, write HEALTH."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Config, HOOKS_DIR, LEARNINGS_DB_PATH, LEARNINGS_SCHEMA_FILE,
)

DISPATCHER_NAME = "session_start"
PROFILE_TAG = "minimal"


def _detect_language(project_dir: str) -> tuple[str, str]:
    """Detect project language and framework from marker files.
    Returns (language, framework)."""
    pd = Path(project_dir)
    language = ""
    framework = ""

    if (pd / "pyproject.toml").exists() or (pd / "setup.py").exists() or (pd / "setup.cfg").exists():
        language = "python"
        # Check framework in pyproject.toml
        pyproject = pd / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text().lower()
                if "django" in content:
                    framework = "django"
                elif "fastapi" in content:
                    framework = "fastapi"
                elif "flask" in content:
                    framework = "flask"
            except OSError:
                pass
    elif (pd / "Cargo.toml").exists():
        language = "rust"
    elif (pd / "go.mod").exists():
        language = "go"
    elif (pd / "package.json").exists() and (pd / "tsconfig.json").exists():
        language = "typescript"
    elif (pd / "package.json").exists():
        language = "javascript"
    elif (pd / "pom.xml").exists() or (pd / "build.gradle").exists():
        language = "java"

    return language, framework


def _check_tools(language: str) -> tuple[list[str], list[str]]:
    """Check tool availability per language. Returns (available, missing)."""
    # Universal tools
    tools_to_check = ["git", "gitleaks"]

    if language == "python":
        tools_to_check += ["ruff", "mypy", "pytest", "python3", "uv"]
    elif language in ("typescript", "javascript"):
        tools_to_check += ["biome", "node", "npm", "tsc"]
    elif language == "rust":
        tools_to_check += ["cargo", "rustfmt", "clippy-driver"]
    elif language == "go":
        tools_to_check += ["go", "gofmt"]

    available: list[str] = []
    missing: list[str] = []
    for tool in tools_to_check:
        if shutil.which(tool):
            available.append(tool)
        else:
            missing.append(tool)

    return available, missing


def _git_cmd(project_dir: str, args: list[str]) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", project_dir] + args,
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception:
        return ""


def main() -> int:
    # 1. Kill switch
    if KillSwitch.is_disabled(DISPATCHER_NAME):
        return 0

    # 2. Profile check
    if not ProfileManager.should_execute(PROFILE_TAG):
        return 0

    # 3. Parse payload
    p = PayloadParser()
    p.validate_all_paths()
    logger = Logger(DISPATCHER_NAME)
    logger.start()

    session_id = p.session_id
    project_dir = p.project_dir

    db = None
    exit_code = 0
    try:
        # 4. Initialize or open project-scoped SQLite DB
        if not project_dir:
            logger.log("WARN", "No project_dir in payload")
            return 0

        db_path = p.db_path
        db_exists = Path(db_path).exists()
        db = SessionDB(db_path)
        if not db_exists:
            db.init_schema()
            logger.log("INFO", f"Created new DB at {db_path}")

        # 5. Record session start metric
        db.exec(
            "INSERT INTO session_metrics (session_id, event_type, key, value) "
            "VALUES (?, 'session_start', 'start_time', datetime('now'))",
            (session_id,),
        )

        # 6. Clean up zombie agents
        zombie_count = db.cleanup_zombies(session_id)
        if zombie_count > 0:
            logger.log("INFO", f"Cleaned up {zombie_count} zombie agents")

        # 7. Prune old failed paths
        ttl_days = int(Config.get("HOOKS_FAILED_PATHS_TTL_DAYS", "7"))
        db.prune_failed_paths(ttl_days)

        # 8. Detect project type
        language, framework = _detect_language(project_dir)
        tools_available, tools_missing = _check_tools(language)

        # 9. Write detected state to project_state table
        state_rows = [
            ("INSERT OR REPLACE INTO project_state (key, value, updated_at) "
             "VALUES ('language', ?, datetime('now'))", (language,)),
            ("INSERT OR REPLACE INTO project_state (key, value, updated_at) "
             "VALUES ('framework', ?, datetime('now'))", (framework,)),
            ("INSERT OR REPLACE INTO project_state (key, value, updated_at) "
             "VALUES ('tools_available', ?, datetime('now'))", (",".join(tools_available),)),
            ("INSERT OR REPLACE INTO project_state (key, value, updated_at) "
             "VALUES ('tools_missing', ?, datetime('now'))", (",".join(tools_missing),)),
            ("INSERT OR REPLACE INTO project_state (key, value, updated_at) "
             "VALUES ('session_id', ?, datetime('now'))", (session_id,)),
        ]
        db.exec_many(state_rows)

        # --- Build output ---
        out_lines: list[str] = []

        lang_display = language
        if framework:
            lang_display = f"{language} ({framework})"

        out_lines.append("=== SESSION START ===")
        out_lines.append(f"Project: {project_dir}")
        out_lines.append(f"Language: {lang_display}")
        out_lines.append(f"Tools: {','.join(tools_available)}")
        out_lines.append("")

        # 10. Restore most recent context snapshot
        snapshot_row = db.query(
            "SELECT content FROM context_snapshots "
            "ORDER BY id DESC LIMIT 1",
        )
        if snapshot_row:
            try:
                snap = json.loads(snapshot_row[0]["content"])
                out_lines.append("--- Previous Session Context ---")
                if snap.get("high_level_goals"):
                    out_lines.append(f"[Goals] {snap['high_level_goals']}")
                if snap.get("active_tasks"):
                    tasks_text = "; ".join(
                        f"{t.get('type', '?')}:{t.get('status', '?')} - {t.get('task', '?')}"
                        for t in snap["active_tasks"][:10]
                    )
                    out_lines.append(f"[Active Tasks] {tasks_text}")
                if snap.get("key_files_modified"):
                    out_lines.append(f"[Key Files] {', '.join(snap['key_files_modified'][:20])}")
                if snap.get("failed_paths"):
                    for fp in snap["failed_paths"][:5]:
                        out_lines.append(
                            f"[Failed Paths from Prior Session] {fp.get('file', '?')}: "
                            f"{fp.get('approach', '?')} -- {fp.get('reason', '?')}"
                        )
                out_lines.append("--- End Previous Context ---")
                out_lines.append("")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # 11. Restore cross-session learnings from learnings.db
        if LEARNINGS_DB_PATH.exists():
            try:
                import sqlite3
                lconn = sqlite3.connect(str(LEARNINGS_DB_PATH), timeout=2)
                lconn.row_factory = sqlite3.Row
                lcursor = lconn.execute(
                    "SELECT pattern, frequency, last_seen FROM learnings "
                    "WHERE project_path = ? OR project_path = '*' "
                    "ORDER BY frequency DESC LIMIT 10",
                    (project_dir,),
                )
                learnings = [dict(row) for row in lcursor.fetchall()]
                lconn.close()

                if learnings:
                    out_lines.append("--- Learned Patterns ---")
                    for lr in learnings:
                        out_lines.append(f"  [{lr['frequency']}x] {lr['pattern']}")
                    out_lines.append("--- End Learned Patterns ---")
                    out_lines.append("")
            except Exception as e:
                logger.log("WARN", f"Could not read learnings.db: {e}")

        # 12. Output current git state
        git_diff_stat = _git_cmd(project_dir, ["diff", "--stat"])
        git_status = _git_cmd(project_dir, ["status", "--short"])

        if git_diff_stat or git_status:
            out_lines.append("--- Current Git State ---")
            if git_diff_stat:
                out_lines.append("git diff --stat:")
                out_lines.append(git_diff_stat.rstrip())
            if git_status:
                out_lines.append("git status --short:")
                out_lines.append(git_status.rstrip())
            out_lines.append("--- End Git State ---")
            out_lines.append("")

        # 13. Restore failed paths (max 20)
        max_inject = int(Config.get("HOOKS_FAILED_PATHS_MAX_INJECT", "20"))
        failed_rows = db.query(
            "SELECT file_path, approach, reason_failed FROM failed_paths "
            "WHERE still_relevant = 1 ORDER BY id DESC LIMIT ?",
            (max_inject,),
        )
        if failed_rows:
            out_lines.append("--- Failed Paths (still relevant) ---")
            out_lines.append("These approaches were already tried and failed. Do NOT repeat them.")
            for row in failed_rows:
                out_lines.append(f"  File: {row['file_path']}")
                out_lines.append(f"  Approach: {row['approach']}")
                out_lines.append(f"  Reason: {row['reason_failed']}")
            out_lines.append("--- End Failed Paths ---")

        # Print output
        print("\n".join(out_lines))

        # 14. Write HEALTH file
        status = "HEALTHY"
        if tools_missing:
            status = "DEGRADED"
        health_data = {
            "session_id": session_id,
            "project_dir": project_dir,
            "language": language,
            "framework": framework,
            "tools_available": tools_available,
            "tools_missing": tools_missing,
            "db_path": db_path,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
        }
        health_path = HOOKS_DIR / "HEALTH"
        try:
            health_path.write_text(json.dumps(health_data, indent=2) + "\n")
        except OSError as e:
            logger.log("WARN", f"Could not write HEALTH file: {e}")

    except Exception as e:
        logger.log("ERROR", str(e))
        exit_code = 1
    finally:
        logger.end(exit_code)
        logger.audit(db, session_id, DISPATCHER_NAME, exit_code)
        if db:
            db.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

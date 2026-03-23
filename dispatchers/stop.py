# /// script
# requires-python = ">=3.10"
# ///
"""Stop dispatcher -- fires when Claude finishes a response turn.

Three independent stages: format/lint, type check, desktop notification.
A crash in one does NOT prevent the others from running.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier,
)

DISPATCHER_NAME = "stop"
PROFILE_TAG = "standard"


# ---------------------------------------------------------------------------
# Stage 1: Format/lint (10s timeout per tool)
# ---------------------------------------------------------------------------

def stage_format(db: SessionDB, session_id: str, project_dir: str,
                 logger: Logger) -> None:
    last_formatted = db.query_scalar(
        "SELECT value FROM project_state WHERE key = 'last_formatted_at'"
    ) or "1970-01-01T00:00:00"

    rows = db.query(
        "SELECT DISTINCT file_path FROM file_claims "
        "WHERE session_id = ? AND timestamp > ? ORDER BY timestamp",
        (session_id, last_formatted),
    )
    if not rows:
        return

    for row in rows:
        fp = row["file_path"]
        if not Path(fp).exists():
            continue
        ext = Path(fp).suffix.lstrip(".")

        try:
            if ext == "py" and shutil.which("ruff"):
                subprocess.run(["ruff", "check", "--fix", fp],
                               capture_output=True, timeout=10)
                subprocess.run(["ruff", "format", fp],
                               capture_output=True, timeout=10)
            elif ext in ("ts", "js", "jsx", "tsx") and shutil.which("biome"):
                subprocess.run(["biome", "check", "--fix", fp],
                               capture_output=True, timeout=10)
            elif ext == "rs" and shutil.which("rustfmt"):
                subprocess.run(["rustfmt", fp],
                               capture_output=True, timeout=10)
            elif ext == "go" and shutil.which("gofmt"):
                subprocess.run(["gofmt", "-w", fp],
                               capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            logger.log("WARN", f"Format timeout for {fp}")
        except Exception as e:
            logger.log("WARN", f"Format error for {fp}: {e}")


# ---------------------------------------------------------------------------
# Stage 2: Type checking (15s timeout)
# ---------------------------------------------------------------------------

def stage_typecheck(db: SessionDB, session_id: str, project_dir: str,
                    logger: Logger) -> int:
    rows = db.query(
        "SELECT DISTINCT file_path FROM file_claims WHERE session_id = ?",
        (session_id,),
    )
    has_py = any(
        r["file_path"].endswith(".py")
        for r in rows if Path(r["file_path"]).exists()
    )
    has_ts = any(
        r["file_path"].endswith((".ts", ".tsx"))
        for r in rows if Path(r["file_path"]).exists()
    )

    errors: list[str] = []

    if has_py and shutil.which("mypy"):
        py_files = [
            r["file_path"] for r in rows
            if r["file_path"].endswith(".py") and Path(r["file_path"]).exists()
        ]
        for fp in py_files:
            try:
                result = subprocess.run(
                    ["mypy", fp], capture_output=True, text=True, timeout=15,
                )
                if "error:" in result.stdout:
                    errors.append(result.stdout)
            except subprocess.TimeoutExpired:
                logger.log("WARN", f"mypy timeout for {fp}")
            except Exception as e:
                logger.log("WARN", f"mypy error for {fp}: {e}")

    if has_ts and shutil.which("tsc"):
        tsconfig = Path(project_dir) / "tsconfig.json"
        if tsconfig.exists():
            try:
                result = subprocess.run(
                    ["tsc", "--noEmit"], capture_output=True, text=True,
                    timeout=15, cwd=project_dir,
                )
                if result.stdout.strip():
                    errors.append(result.stdout)
            except subprocess.TimeoutExpired:
                logger.log("WARN", "tsc timeout")
            except Exception as e:
                logger.log("WARN", f"tsc error: {e}")

    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 2  # Signal errors via asyncRewake
    return 0


# ---------------------------------------------------------------------------
# Stage 3: Notification (always fires)
# ---------------------------------------------------------------------------

def stage_notify() -> None:
    Notifier.desktop("Claude Code", "Turn complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    # 4. Open DB
    db = None
    if p.db_path and Path(p.db_path).exists():
        try:
            db = SessionDB(p.db_path)
        except Exception:
            pass

    session_id = p.session_id
    project_dir = p.project_dir
    exit_code = 0

    try:
        if db is None:
            # Without DB we can only notify
            try:
                stage_notify()
            except Exception:
                pass
            logger.end(0)
            return 0

        # Stage 1: Format/lint (independent)
        format_exit = 0
        try:
            stage_format(db, session_id, project_dir, logger)
        except Exception as e:
            logger.log("WARN", f"Format stage failed: {e}")
            format_exit = 1

        # Stage 2: Type checking (independent)
        typecheck_exit = 0
        try:
            typecheck_exit = stage_typecheck(db, session_id, project_dir, logger)
        except Exception as e:
            logger.log("WARN", f"Typecheck stage failed: {e}")

        # Update last_formatted_at
        try:
            db.exec(
                "INSERT OR REPLACE INTO project_state (key, value, updated_at) "
                "VALUES ('last_formatted_at', datetime('now'), datetime('now'))"
            )
        except Exception as e:
            logger.log("WARN", f"Failed to update last_formatted_at: {e}")

        # Stage 3: Notification (always fires, independent)
        try:
            stage_notify()
        except Exception:
            pass

        # Return typecheck exit code (for asyncRewake)
        exit_code = typecheck_exit

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

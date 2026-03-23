# /// script
# requires-python = ">=3.10"
# ///
"""SubagentStop dispatcher -- update agent_registry. Desktop notify if walkaway.

NO COMMITS. Commits happen only at task_completed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier,
)

DISPATCHER_NAME = "subagent_stop"
PROFILE_TAG = "standard"


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
    exit_code = 0

    try:
        if db is None:
            logger.log("WARN", "No database available")
            logger.end(0)
            return 0

        agent_id = p.agent_id or p.get("tool_input", "agent_id") or ""

        # 1. Extract completion info. Normalize status.
        status = p.get("status") or p.get("tool_input", "status") or "completed"
        error = p.get("error") or p.get("tool_input", "error") or ""

        if status in ("completed", "success", "done"):
            status = "completed"
        elif status in ("failed", "error", "crashed"):
            status = "failed"
        else:
            status = "completed"
        if error and error != "null":
            status = "failed"

        logger.log("INFO", f"Agent {agent_id} stopped status={status}")

        # 2. Get files this agent modified
        files_json = db.query_scalar(
            "SELECT json_group_array(file_path) FROM file_claims "
            "WHERE agent_id = ? AND session_id = ?",
            (agent_id, session_id),
        ) or "[]"

        # 3. Update agent_registry
        db.exec(
            "UPDATE agent_registry SET status = ?, end_time = datetime('now'), "
            "files_changed = ? "
            "WHERE id = ?",
            (status, files_json, agent_id),
        )

        # 4. Record in session_metrics
        db.exec(
            "INSERT INTO session_metrics (session_id, event_type, key, value) "
            "VALUES (?, 'subagent_stop', ?, ?)",
            (session_id, agent_id, status),
        )

        # 5. Desktop notification if walkaway
        if Notifier.is_walkaway():
            Notifier.desktop("Claude Code", f"Agent {agent_id} finished ({status})")

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

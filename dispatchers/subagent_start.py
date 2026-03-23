# /// script
# requires-python = ">=3.10"
# ///
"""SubagentStart dispatcher -- insert agent into agent_registry."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
)

DISPATCHER_NAME = "subagent_start"
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

        # Extract agent info
        agent_id = (
            p.agent_id
            or p.get("tool_input", "agent_id")
            or f"agent-{int(time.time())}"
        )
        agent_type = (
            p.get("agent_type")
            or p.get("tool_input", "type")
            or p.get("tool_input", "subagent_type")
            or "unknown"
        )
        task_desc = (
            p.get("task_description")
            or p.get("tool_input", "prompt")
            or p.get("tool_input", "task")
            or ""
        )[:1024]

        logger.log("INFO", f"Registering agent {agent_id} type={agent_type}")

        # Insert into agent_registry
        db.exec(
            "INSERT OR REPLACE INTO agent_registry "
            "(id, session_id, agent_type, task_description, spawn_time, status) "
            "VALUES (?, ?, ?, ?, datetime('now'), 'running')",
            (agent_id, session_id, agent_type, task_desc),
        )

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

# /// script
# requires-python = ">=3.10"
# ///
"""Elicitation dispatcher -- desktop + mobile always.

Swarm is hard-blocked when waiting for user input, so both notification
channels fire regardless of walkaway state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier,
)

DISPATCHER_NAME = "elicitation"
PROFILE_TAG = "minimal"


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

    # 4. Open DB (for audit and metrics)
    db = None
    if p.db_path and Path(p.db_path).exists():
        try:
            db = SessionDB(p.db_path)
        except Exception:
            pass

    session_id = p.session_id
    exit_code = 0

    try:
        # 1. Extract details
        tool_name = (
            p.get("tool_name")
            or p.get("tool_input", "tool_name")
            or ""
        )
        question = (
            p.get("question")
            or p.get("tool_input", "question")
            or p.get("message")
            or p.get("tool_input", "message")
            or ""
        )
        details = f"[{tool_name}] {question}".strip() if tool_name else question
        if not details:
            details = "Claude needs text input (check Claude Code)"
        details = details[:200]

        logger.log("INFO", f"Elicitation: {details[:80]}")

        # 2. ALWAYS send desktop + mobile (swarm is hard-blocked)
        Notifier.desktop("Claude Code -- INPUT NEEDED", f"INPUT NEEDED: {details}")
        Notifier.mobile("Claude Code -- INPUT NEEDED", f"INPUT NEEDED: {details}")

        # 3. Record in DB
        if db:
            db.exec(
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'elicitation_blocked', 'details', ?)",
                (session_id, details),
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

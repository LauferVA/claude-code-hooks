# /// script
# requires-python = ">=3.10"
# ///
"""Notification dispatcher -- desktop only. Lightweight."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier,
)

DISPATCHER_NAME = "notification"
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

    # 4. Open DB (for audit only)
    db = None
    if p.db_path and Path(p.db_path).exists():
        try:
            db = SessionDB(p.db_path)
        except Exception:
            pass

    session_id = p.session_id
    exit_code = 0

    try:
        # Extract message from payload
        message = (
            p.get("message")
            or p.get("tool_input", "message")
            or p.get("notification")
            or p.get("tool_input", "notification")
            or "Status update"
        )
        message = str(message)[:200]

        logger.log("INFO", f"Notification: {message[:80]}")

        # Desktop notification ONLY. No mobile. Lightweight.
        Notifier.desktop("Claude Code", message)

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

# /// script
# requires-python = ">=3.10"
# ///

"""post_compact dispatcher -- Read pre-compact snapshot from SQLite.
Write marker file at ${PROJECT_DIR}/.claude-compact-pending.
Do NOT output to stdout (confirmed bug, GitHub #15174)."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
)

DISPATCHER_NAME = "post_compact"
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

    session_id = p.session_id
    project_dir = p.project_dir

    db = None
    exit_code = 0
    try:
        if not project_dir:
            logger.log("WARN", "No project_dir in payload")
            return 0

        # 4. Read the compact_summary from payload
        compact_summary = p.get("compact_summary", default="")

        # 5. Open DB and read the pre-compact snapshot
        if not p.db_path or not Path(p.db_path).exists():
            logger.log("WARN", "No session DB found")
            return 0

        db = SessionDB(p.db_path)

        snapshot_rows = db.query(
            "SELECT id, content FROM context_snapshots "
            "WHERE session_id = ? AND snapshot_type = 'pre_compact' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )

        snapshot_content = {}
        if snapshot_rows:
            try:
                snapshot_content = json.loads(snapshot_rows[0]["content"])
            except (json.JSONDecodeError, KeyError):
                logger.log("WARN", "Could not parse pre-compact snapshot JSON")
        else:
            logger.log("INFO", "No pre-compact snapshot found in DB")

        # 6. Build JSON blob for marker file
        marker_data = {
            "pre_compact_snapshot": snapshot_content,
            "compact_summary": compact_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
        }

        # 7. Write marker file ATOMICALLY
        marker_path = Path(project_dir) / ".claude-compact-pending"
        tmp_path = marker_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(marker_data))
            tmp_path.rename(marker_path)  # atomic on same filesystem
            logger.log("INFO", f"Wrote compact-pending marker at {marker_path}")
        except OSError as e:
            logger.log("ERROR", f"Could not write marker file: {e}")
            exit_code = 1

        # Do NOT output to stdout -- PostCompact stdout does not inject
        # into context (confirmed bug, GitHub #15174).

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

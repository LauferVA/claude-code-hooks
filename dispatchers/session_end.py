# /// script
# requires-python = ">=3.10"
# ///

"""session_end dispatcher -- Batch metrics, 5-layer JSON snapshot,
fork background learning extractor, output summary.
Must complete within 10s timeout constraint."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Config, LEARNINGS_DB_PATH, LEARNINGS_SCHEMA_FILE,
)

DISPATCHER_NAME = "session_end"
PROFILE_TAG = "minimal"

# Inline learning extractor code -- forked as background process.
# Runs after session_end exits to avoid blocking the 10s timeout.
LEARNING_EXTRACTOR_CODE = '''
import json
import os
import sqlite3
import sys
from pathlib import Path

session_db_path = os.environ.get("SESSION_DB", "")
project_dir = os.environ.get("PROJECT_DIR", "")
session_id = os.environ.get("SESSION_ID", "")
learnings_db_path = os.environ.get("LEARNINGS_DB", "")
learnings_schema_path = os.environ.get("LEARNINGS_SCHEMA", "")

if not all([session_db_path, project_dir, session_id, learnings_db_path]):
    sys.exit(0)

try:
    # Open session DB read-only
    sconn = sqlite3.connect(f"file:{session_db_path}?mode=ro", uri=True, timeout=5)
    sconn.row_factory = sqlite3.Row

    # Query error patterns with frequency >= 2
    error_patterns = []
    try:
        cursor = sconn.execute(
            "SELECT error_category, file_path, COUNT(*) as freq "
            "FROM error_log WHERE session_id = ? "
            "GROUP BY error_category, file_path HAVING COUNT(*) >= 2",
            (session_id,),
        )
        error_patterns = [dict(row) for row in cursor.fetchall()]
    except Exception:
        pass

    # Query failed paths (session-scoped)
    failed_paths = []
    try:
        cursor = sconn.execute(
            "SELECT file_path, approach, reason_failed FROM failed_paths "
            "WHERE session_id = ? AND still_relevant = 1 ORDER BY id DESC LIMIT 20",
            (session_id,),
        )
        failed_paths = [dict(row) for row in cursor.fetchall()]
    except Exception:
        pass

    sconn.close()

    # Open/create learnings DB
    lconn = sqlite3.connect(learnings_db_path, timeout=5)
    lconn.execute("PRAGMA journal_mode = WAL")
    lconn.execute("PRAGMA busy_timeout = 5000")

    # Apply learnings schema
    if learnings_schema_path and Path(learnings_schema_path).exists():
        lconn.executescript(Path(learnings_schema_path).read_text())

    # Store error patterns as learnings
    for ep in error_patterns:
        pattern = f"[{ep['error_category']}] {ep['file_path']}: repeated {ep['freq']}x"
        lconn.execute(
            "INSERT INTO learnings (project_path, pattern, frequency, last_seen) "
            "VALUES (?, ?, 1, datetime('now')) "
            "ON CONFLICT(project_path, pattern) DO UPDATE SET "
            "  frequency = frequency + 1, "
            "  last_seen = datetime('now')",
            (project_dir, pattern),
        )

    # Store failed paths as learnings
    for fp in failed_paths:
        pattern = f"Failed: {fp['file_path']} approach={fp['approach']} reason={fp['reason_failed']}"
        lconn.execute(
            "INSERT INTO learnings (project_path, pattern, frequency, last_seen) "
            "VALUES (?, ?, 1, datetime('now')) "
            "ON CONFLICT(project_path, pattern) DO UPDATE SET "
            "  frequency = frequency + 1, "
            "  last_seen = datetime('now')",
            (project_dir, pattern),
        )

    # Insert session summary
    # Re-read the context snapshot for the summary
    try:
        sconn2 = sqlite3.connect(f"file:{session_db_path}?mode=ro", uri=True, timeout=5)
        cursor = sconn2.execute(
            "SELECT content FROM context_snapshots "
            "WHERE session_id = ? AND snapshot_type = 'session_end' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
        summary_json = row[0] if row else "{}"
        sconn2.close()
    except Exception:
        summary_json = "{}"

    lconn.execute(
        "INSERT INTO session_summaries "
        "(project_path, session_id, summary_json, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (project_dir, session_id, summary_json),
    )

    lconn.commit()
    lconn.close()

except Exception:
    pass
'''


def _git_diff_stat(project_dir: str) -> str:
    """Get git diff --stat output."""
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "diff", "--stat"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
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
        # 4. Bail if no DB exists
        if not p.db_path or not Path(p.db_path).exists():
            logger.log("WARN", "No session DB found, skipping session_end")
            print("=== SESSION END ===")
            print("No session database found.")
            print("=== END ===")
            return 0

        db = SessionDB(p.db_path)

        # 5. Compute duration from start_time
        start_time_row = db.query(
            "SELECT value FROM session_metrics "
            "WHERE session_id = ? AND key = 'start_time' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        duration_seconds = 0
        if start_time_row:
            try:
                duration_val = db.query_scalar(
                    "SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER)",
                    (start_time_row[0]["value"],),
                )
                duration_seconds = int(duration_val or 0)
            except Exception:
                pass

        # 6. Count metrics
        agent_count = db.query_scalar(
            "SELECT COUNT(*) FROM agent_registry WHERE session_id = ?",
            (session_id,),
        ) or 0

        error_count = db.query_scalar(
            "SELECT COUNT(*) FROM error_log WHERE session_id = ?",
            (session_id,),
        ) or 0

        file_count = db.query_scalar(
            "SELECT COUNT(DISTINCT file_path) FROM file_claims WHERE session_id = ?",
            (session_id,),
        ) or 0

        hook_count = db.query_scalar(
            "SELECT COUNT(*) FROM hook_events WHERE session_id = ?",
            (session_id,),
        ) or 0

        # 7. Batch ALL metrics into a single transaction
        metrics_statements: list[tuple[str, tuple]] = [
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'end_time', datetime('now'))",
                (session_id,),
            ),
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'duration_seconds', ?)",
                (session_id, str(duration_seconds)),
            ),
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'agent_count', ?)",
                (session_id, str(agent_count)),
            ),
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'error_count', ?)",
                (session_id, str(error_count)),
            ),
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'file_count', ?)",
                (session_id, str(file_count)),
            ),
            (
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'session_end', 'hook_count', ?)",
                (session_id, str(hook_count)),
            ),
        ]
        db.exec_many(metrics_statements)

        # 8. Build 5-layer JSON snapshot
        # Goals
        goals_rows = db.query(
            "SELECT value FROM session_metrics "
            "WHERE session_id = ? AND key = 'goal' "
            "ORDER BY id DESC LIMIT 5",
            (session_id,),
        )
        goals_text = "; ".join(r["value"] for r in goals_rows) if goals_rows else ""

        # Active tasks
        tasks_rows = db.query(
            "SELECT id, agent_type, status, task_description "
            "FROM agent_registry WHERE session_id = ? "
            "ORDER BY spawn_time DESC LIMIT 20",
            (session_id,),
        )
        active_tasks = [
            {
                "id": r["id"],
                "type": r["agent_type"] or "",
                "status": r["status"] or "",
                "task": r["task_description"] or "",
            }
            for r in tasks_rows
        ]

        # Files modified
        files_rows = db.query(
            "SELECT DISTINCT file_path FROM file_claims "
            "WHERE session_id = ? "
            "ORDER BY timestamp DESC LIMIT 50",
            (session_id,),
        )
        key_files = [r["file_path"] for r in files_rows]

        # Failed paths
        failed_rows = db.query(
            "SELECT file_path, approach, reason_failed FROM failed_paths "
            "WHERE still_relevant = 1 ORDER BY id DESC LIMIT 20",
        )
        failed_paths = [
            {
                "file": r["file_path"] or "",
                "approach": r["approach"] or "",
                "reason": r["reason_failed"] or "",
            }
            for r in failed_rows
        ]

        # Git diff stat
        git_diff_stat = _git_diff_stat(project_dir)

        snapshot = {
            "high_level_goals": goals_text,
            "active_tasks": active_tasks,
            "key_files_modified": key_files,
            "failed_paths": failed_paths,
            "git_diff_stat": git_diff_stat,
            "session_id": session_id,
            "duration_seconds": duration_seconds,
            "agent_count": int(agent_count),
            "error_count": int(error_count),
        }

        # 9. Store as context_snapshot
        db.exec(
            "INSERT INTO context_snapshots (session_id, snapshot_type, content) "
            "VALUES (?, 'session_end', ?)",
            (session_id, json.dumps(snapshot)),
        )

        # 10. Fork background learning extractor
        try:
            subprocess.Popen(
                [sys.executable, "-c", LEARNING_EXTRACTOR_CODE],
                env={
                    **os.environ,
                    "SESSION_DB": p.db_path,
                    "PROJECT_DIR": project_dir,
                    "SESSION_ID": session_id,
                    "LEARNINGS_DB": str(LEARNINGS_DB_PATH),
                    "LEARNINGS_SCHEMA": str(LEARNINGS_SCHEMA_FILE),
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # nohup equivalent
            )
            logger.log("INFO", "Forked learning extractor process")
        except Exception as e:
            logger.log("WARN", f"Could not fork learning extractor: {e}")

        # 11. Output brief summary to stdout
        duration_min = duration_seconds // 60
        duration_sec = duration_seconds % 60

        out_lines: list[str] = []
        out_lines.append("=== SESSION END ===")
        out_lines.append(f"Session: {session_id}")
        out_lines.append(f"Duration: {duration_min}m {duration_sec}s")
        out_lines.append(f"Agents spawned: {agent_count}")
        out_lines.append(f"Files touched: {file_count}")
        out_lines.append(f"Errors logged: {error_count}")
        out_lines.append(f"Hook events: {hook_count}")
        out_lines.append("")

        if key_files:
            out_lines.append(f"Files modified ({len(key_files)}):")
            for fp in key_files[:20]:
                out_lines.append(f"  {fp}")
            if len(key_files) > 20:
                out_lines.append(f"  ... and {len(key_files) - 20} more")
        out_lines.append("")
        out_lines.append("Context snapshot saved. Next session will restore this state.")
        out_lines.append("=== END ===")

        print("\n".join(out_lines))

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

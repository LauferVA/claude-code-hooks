# /// script
# requires-python = ">=3.10"
# ///
"""Context backup dispatcher -- called when token threshold crossed.

Reads SQLite data (git state, agents, file claims, failed paths, errors).
Writes markdown to .claude/backups/. Keeps max 5 backups.
"""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
)

DISPATCHER_NAME = "context_backup"
PROFILE_TAG = "strict"


def _git_diff_stat(project_dir: str) -> str:
    """Get git diff --stat output, 5s timeout."""
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "diff", "--stat"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.stdout else "(no changes)"
    except Exception:
        return "(git diff unavailable)"


def _build_markdown(
    session_id: str,
    project_dir: str,
    context_percent: int,
    db: SessionDB,
) -> str:
    """Build the full markdown backup document."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []

    lines.append("# Context Backup")
    lines.append(f"Timestamp: {ts}")
    lines.append(f"Session: {session_id}")
    lines.append(f"Context Usage: {context_percent}%")
    lines.append("")

    # Git State
    lines.append("## Git State")
    lines.append("```")
    lines.append(_git_diff_stat(project_dir))
    lines.append("```")
    lines.append("")

    # Active Agents
    lines.append("## Active Agents")
    try:
        agents = db.query(
            "SELECT id, agent_type, task_description, status, spawn_time "
            "FROM agent_registry WHERE session_id = ? ORDER BY spawn_time",
            (session_id,),
        )
        if agents:
            for a in agents:
                status = a.get("status", "unknown")
                agent_type = a.get("agent_type", "unknown")
                task = (a.get("task_description") or "")[:100]
                lines.append(f"- **{a['id']}** ({agent_type}) [{status}]: {task}")
        else:
            lines.append("(none)")
    except Exception:
        lines.append("(query failed)")
    lines.append("")

    # File Claims
    lines.append("## File Claims")
    try:
        claims = db.query(
            "SELECT DISTINCT file_path, operation FROM file_claims "
            "WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        if claims:
            for c in claims:
                op = c.get("operation", "unknown")
                lines.append(f"- `{c['file_path']}` ({op})")
        else:
            lines.append("(none)")
    except Exception:
        lines.append("(query failed)")
    lines.append("")

    # Failed Paths
    lines.append("## Failed Paths")
    try:
        failed = db.query(
            "SELECT file_path, approach, reason_failed FROM failed_paths "
            "WHERE still_relevant = 1 ORDER BY id DESC LIMIT 20",
        )
        if failed:
            for f in failed:
                fp = f.get("file_path", "")
                approach = (f.get("approach") or "")[:80]
                reason = (f.get("reason_failed") or "")[:80]
                lines.append(f"- `{fp}`: {approach} -- {reason}")
        else:
            lines.append("(none)")
    except Exception:
        lines.append("(query failed)")
    lines.append("")

    # Recent Errors
    lines.append("## Recent Errors")
    try:
        errors = db.query(
            "SELECT error_category, error_message, file_path, timestamp "
            "FROM error_log WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 10",
            (session_id,),
        )
        if errors:
            for e in errors:
                cat = e.get("error_category", "unknown")
                msg = (e.get("error_message") or "")[:120]
                fp = e.get("file_path") or ""
                lines.append(f"- [{cat}] {fp}: {msg}")
        else:
            lines.append("(none)")
    except Exception:
        lines.append("(query failed)")
    lines.append("")

    return "\n".join(lines)


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
        if not project_dir:
            logger.log("WARN", "No project_dir -- cannot create backup")
            logger.end(0)
            logger.audit(db, session_id, DISPATCHER_NAME, 0)
            if db:
                db.close()
            return 0

        if db is None:
            logger.log("WARN", "No database available -- cannot create backup")
            logger.end(0)
            return 0

        context_percent = int(p.get("context_percent", default=0) or 0)

        logger.log("INFO",
                    f"Context backup at {context_percent}% for {session_id}")

        # Build markdown document
        markdown = _build_markdown(session_id, project_dir, context_percent, db)

        # Write to backup directory
        backup_dir = Path(project_dir) / ".claude" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"context-backup-{ts}.md"
        backup_path.write_text(markdown)

        logger.log("INFO", f"Backup written to {backup_path}")

        # Keep max 5 backups (delete oldest)
        backups = sorted(backup_dir.glob("context-backup-*.md"))
        while len(backups) > 5:
            try:
                backups[0].unlink()
                logger.log("INFO", f"Pruned old backup: {backups[0].name}")
            except OSError as e:
                logger.log("WARN", f"Failed to prune backup: {e}")
            backups = backups[1:]

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

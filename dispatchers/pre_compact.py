# /// script
# requires-python = ">=3.10"
# ///

"""pre_compact dispatcher -- HIGHEST VALUE HOOK. Extract hard data
(git diff, agents, claims, failed paths, errors, code structure).
16KB cap. Store snapshot. Output everything to stdout."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Config, PROFILE_LEVELS,
)

DISPATCHER_NAME = "pre_compact"
PROFILE_TAG = "standard"
MAX_OUTPUT_BYTES = 16 * 1024  # 16KB hard cap


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


def _truncate_sections(
    sections: dict[str, str],
    priority_order: list[str],
    max_bytes: int,
) -> str:
    """Build output from sections dict. If total exceeds max_bytes,
    truncate sections in reverse priority order (lowest priority first).
    Priority order: last items get truncated first."""
    # Build full output first
    full = "\n\n".join(f"{sections[k]}" for k in priority_order if sections.get(k))
    if len(full.encode("utf-8")) <= max_bytes:
        return full

    # Need to truncate. Remove sections from the end of priority_order
    # until we fit. Truncation order per spec: oldest failed paths first,
    # then errors, then code structure. Always keep git state and agents.
    truncation_order = list(reversed(priority_order))
    included = dict(sections)

    for key in truncation_order:
        if key in ("header", "git_state", "agents", "tools", "footer"):
            continue  # Always keep these
        del included[key]
        test_output = "\n\n".join(
            f"{included[k]}" for k in priority_order if included.get(k)
        )
        if len(test_output.encode("utf-8")) <= max_bytes:
            return test_output

    # Last resort: just truncate the whole thing
    result = "\n\n".join(f"{included[k]}" for k in priority_order if included.get(k))
    return result[:max_bytes]


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
        # --- Section 1: Git state (always available, even without DB) ---
        git_diff_stat = _git_cmd(project_dir, ["diff", "--stat"]).rstrip()
        git_status = _git_cmd(project_dir, ["status", "--short"]).rstrip()
        git_branch = _git_cmd(project_dir, ["branch", "--show-current"]).strip()

        # Try to open DB
        if p.db_path and Path(p.db_path).exists():
            try:
                db = SessionDB(p.db_path)
            except Exception as e:
                logger.log("WARN", f"Could not open DB: {e}")

        # Read language from project_state if DB exists
        language = ""
        tools_line = ""
        if db:
            try:
                language = db.query_scalar(
                    "SELECT value FROM project_state WHERE key = 'language'"
                ) or ""
            except Exception:
                pass
            try:
                tools_line = db.query_scalar(
                    "SELECT value FROM project_state WHERE key = 'tools_available'"
                ) or ""
            except Exception:
                pass

        # Build sections dict
        sections: dict[str, str] = {}

        # Header
        header_lines = [
            "=== PRE-COMPACTION CONTEXT RESCUE ===",
            f"Session: {session_id}",
            f"Project: {project_dir}",
        ]
        if language:
            header_lines.append(f"Language: {language}")
        if git_branch:
            header_lines.append(f"Branch: {git_branch}")
        sections["header"] = "\n".join(header_lines)

        # Section 1 - Git state
        git_lines: list[str] = ["--- [1] Git Diff Stat ---"]
        if git_diff_stat:
            git_lines.append(git_diff_stat)
        else:
            git_lines.append("(no changes)")
        git_lines.append("")
        git_lines.append("--- [1b] Git Status ---")
        if git_status:
            git_lines.append(git_status)
        else:
            git_lines.append("(clean)")
        sections["git_state"] = "\n".join(git_lines)

        # Section 2 - Active agents
        agents_lines: list[str] = ["--- [2] Active Agents ---"]
        if db:
            agent_rows = db.query(
                "SELECT id, agent_type, task_description, spawn_time "
                "FROM agent_registry "
                "WHERE status = 'running' AND session_id = ? "
                "ORDER BY spawn_time DESC",
                (session_id,),
            )
            if agent_rows:
                for r in agent_rows:
                    agents_lines.append(
                        f"  [{r['agent_type']}] {r['id']} -- "
                        f"{r['task_description'] or '(no description)'} "
                        f"(since {r['spawn_time']})"
                    )
            else:
                agents_lines.append("  (none)")
        else:
            agents_lines.append("  (no DB)")
        sections["agents"] = "\n".join(agents_lines)

        # Section 3 - File claims
        claims_lines: list[str] = ["--- [3] Recent File Claims ---"]
        if db:
            claims_rows = db.query(
                "SELECT file_path, operation, agent_id, timestamp "
                "FROM file_claims WHERE session_id = ? "
                "ORDER BY timestamp DESC LIMIT 30",
                (session_id,),
            )
            if claims_rows:
                for r in claims_rows:
                    claims_lines.append(
                        f"  {r['operation'] or 'touch'} {r['file_path']} "
                        f"(by {r['agent_id'] or 'main'}, {r['timestamp']})"
                    )
            else:
                claims_lines.append("  (none)")
        else:
            claims_lines.append("  (no DB)")
        sections["claims"] = "\n".join(claims_lines)

        # Section 4 - Failed paths
        failed_lines: list[str] = []
        if db:
            failed_rows = db.query(
                "SELECT file_path, approach, reason_failed, timestamp "
                "FROM failed_paths WHERE still_relevant = 1 "
                "ORDER BY id DESC LIMIT 20",
            )
            total_failed = db.query_scalar(
                "SELECT COUNT(*) FROM failed_paths WHERE still_relevant = 1"
            ) or 0
            failed_lines.append(
                f"--- [4] Failed Paths ({total_failed} total, showing max 20) ---"
            )
            if failed_rows:
                failed_lines.append(
                    "DO NOT repeat these approaches -- they already failed."
                )
                for r in failed_rows:
                    failed_lines.append(f"  File: {r['file_path']}")
                    failed_lines.append(f"    Tried: {r['approach']}")
                    failed_lines.append(f"    Failed because: {r['reason_failed']}")
            else:
                failed_lines.append("  (none)")
        else:
            failed_lines.append("--- [4] Failed Paths ---")
            failed_lines.append("  (no DB)")
        sections["failed_paths"] = "\n".join(failed_lines)

        # Section 5 - Recent errors
        errors_lines: list[str] = ["--- [5] Recent Errors (last 10) ---"]
        if db:
            error_rows = db.query(
                "SELECT error_category, file_path, error_message, resolution, timestamp "
                "FROM error_log WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 10",
                (session_id,),
            )
            if error_rows:
                for r in error_rows:
                    errors_lines.append(
                        f"  [{r['error_category']}] {r['file_path']}"
                    )
                    errors_lines.append(f"    Error: {r['error_message']}")
                    if r.get("resolution"):
                        errors_lines.append(f"    Resolution: {r['resolution']}")
            else:
                errors_lines.append("  (none)")
        else:
            errors_lines.append("  (no DB)")
        sections["errors"] = "\n".join(errors_lines)

        # Section 6 - Code structure (strict profile only)
        current_profile = ProfileManager.get_profile()
        if PROFILE_LEVELS.get(current_profile, 1) >= PROFILE_LEVELS["strict"] and db:
            code_lines: list[str] = ["--- [6] Code Structure (modified files) ---"]
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
                from code_summary import summarize_file

                modified_files = db.query(
                    "SELECT DISTINCT file_path FROM file_claims "
                    "WHERE session_id = ? ORDER BY timestamp DESC LIMIT 10",
                    (session_id,),
                )
                for row in modified_files:
                    fp = row["file_path"]
                    if Path(fp).exists() and Path(fp).suffix in (
                        ".py", ".ts", ".js", ".rs", ".go"
                    ):
                        summary = summarize_file(fp)
                        if summary:
                            code_lines.append(f"  {fp}:")
                            code_lines.append(textwrap.indent(summary, "    "))
            except ImportError:
                code_lines.append("  (code_summary module not available)")
            except Exception as e:
                code_lines.append(f"  (error: {e})")
            sections["code_structure"] = "\n".join(code_lines)

        # Tools line
        if tools_line:
            sections["tools"] = f"--- Tools Available: {tools_line} ---"

        # Footer
        sections["footer"] = "=== END PRE-COMPACTION CONTEXT RESCUE ==="

        # Build output with priority-based truncation
        # Priority order: header and git_state are highest priority (always kept),
        # code_structure is lowest (truncated first)
        priority_order = [
            "header",
            "git_state",
            "agents",
            "claims",
            "failed_paths",
            "errors",
            "code_structure",
            "tools",
            "footer",
        ]

        output = _truncate_sections(sections, priority_order, MAX_OUTPUT_BYTES)

        # Store snapshot in SQLite
        if db:
            snapshot_json = json.dumps({
                "git_diff_stat": git_diff_stat,
                "git_status": git_status,
                "git_branch": git_branch,
                "agents": sections.get("agents", ""),
                "claims": sections.get("claims", ""),
                "failed_paths": sections.get("failed_paths", ""),
                "errors": sections.get("errors", ""),
                "code_structure": sections.get("code_structure", ""),
                "tools_available": tools_line,
                "language": language,
                "session_id": session_id,
            })
            db.exec(
                "INSERT INTO context_snapshots (session_id, snapshot_type, content) "
                "VALUES (?, 'pre_compact', ?)",
                (session_id, snapshot_json),
            )

        # Output everything to stdout
        print(output)

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

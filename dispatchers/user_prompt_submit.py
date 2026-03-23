# /// script
# requires-python = ">=3.10"
# ///

"""user_prompt_submit dispatcher -- Check for compact-pending marker file.
If not found: exit 0 (zero overhead). If found: read, delete, output
structured context (8KB cap)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, Logger, KillSwitch, ProfileManager,
)

DISPATCHER_NAME = "user_prompt_submit"
PROFILE_TAG = "standard"
MAX_OUTPUT_BYTES = 8 * 1024  # 8KB cap


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

    project_dir = p.project_dir
    if not project_dir:
        return 0

    # 4. Check for marker file -- fast path for normal prompts
    marker_path = Path(project_dir) / ".claude-compact-pending"
    if not marker_path.exists():
        return 0  # Zero overhead on normal turns

    # Only start logging/timing if we actually have work to do
    logger = Logger(DISPATCHER_NAME)
    logger.start()

    session_id = p.session_id
    exit_code = 0
    try:
        # 5. Read marker file
        try:
            marker_text = marker_path.read_text()
            marker_data = json.loads(marker_text)
        except (json.JSONDecodeError, OSError) as e:
            # Corrupt marker -- log, delete, exit 0. Never block a user prompt.
            logger.log("ERROR", f"Corrupt marker file: {e}")
            try:
                marker_path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0

        # 6. Delete marker ATOMICALLY
        try:
            tmp = marker_path.with_suffix(".deleting")
            marker_path.rename(tmp)
            tmp.unlink()
        except OSError as e:
            logger.log("WARN", f"Could not delete marker atomically: {e}")
            # Try direct unlink as fallback
            try:
                marker_path.unlink(missing_ok=True)
            except OSError:
                pass

        # 7. Extract sections from pre_compact_snapshot
        snapshot = marker_data.get("pre_compact_snapshot", {})
        compact_summary = marker_data.get("compact_summary", "")

        # 8. Build output capped at 8KB
        out_lines: list[str] = []
        out_lines.append("=== POST-COMPACTION CONTEXT RECOVERY ===")
        out_lines.append("")
        out_lines.append("The context was just compacted. Here is the preserved state:")
        out_lines.append("")

        # Git State
        git_diff_stat = snapshot.get("git_diff_stat", "")
        git_status = snapshot.get("git_status", "")
        if git_diff_stat or git_status:
            out_lines.append("--- Git State ---")
            if git_diff_stat:
                out_lines.append(git_diff_stat)
            if git_status:
                out_lines.append(git_status)
            out_lines.append("")

        # Active Agents
        agents = snapshot.get("agents", "")
        if agents:
            out_lines.append("--- Active Agents ---")
            out_lines.append(agents)
            out_lines.append("")

        # Failed Paths
        failed_paths = snapshot.get("failed_paths", "")
        if failed_paths:
            out_lines.append("--- Failed Paths ---")
            out_lines.append("DO NOT repeat these approaches.")
            out_lines.append(failed_paths)
            out_lines.append("")

        # Recent Errors
        errors = snapshot.get("errors", "")
        if errors:
            out_lines.append("--- Recent Errors ---")
            out_lines.append(errors)
            out_lines.append("")

        # Compact Summary
        if compact_summary:
            out_lines.append("--- Compact Summary ---")
            out_lines.append(compact_summary)
            out_lines.append("")

        out_lines.append("=== END RECOVERY ===")

        output = "\n".join(out_lines)

        # Enforce 8KB cap
        output_bytes = output.encode("utf-8")
        if len(output_bytes) > MAX_OUTPUT_BYTES:
            # Truncate to fit, preserving the footer
            footer = "\n=== END RECOVERY ==="
            max_content = MAX_OUTPUT_BYTES - len(footer.encode("utf-8")) - 20
            output = output_bytes[:max_content].decode("utf-8", errors="ignore")
            output += "\n... (truncated)" + footer

        print(output)
        logger.log("INFO", f"Emitted {len(output)} bytes of recovery context")

    except Exception as e:
        logger.log("ERROR", str(e))
        exit_code = 1
    finally:
        logger.end(exit_code)
        # No DB audit for user_prompt_submit -- we don't open the DB
        # to keep the zero-overhead fast path truly zero-overhead.

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

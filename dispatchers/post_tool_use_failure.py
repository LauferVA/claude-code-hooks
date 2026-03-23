# /// script
# requires-python = ">=3.10"
# ///

"""post_tool_use_failure -- Fires when a tool call FAILS.

(a) Extract error info
(b) Categorize error (lint, type, test, build, runtime)
(c) Determine file path
(d) Count previous errors for this file in session
(e) Insert new error into error_log
(f) Escalation ladder (1-2: retry, 3: research, 4: user escalation, 5+: block)
(g) Write to failed_paths at attempt >= 3
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier, Config, ReentrancyGuard,
)

DISPATCHER_NAME = "post_tool_use_failure"
PROFILE_TAG = "standard"


def categorize_error(msg: str) -> str:
    """Classify error message into a category."""
    msg_lower = msg.lower()
    if any(kw in msg_lower for kw in ('lint', 'eslint', 'pylint', 'flake8',
           'clippy', 'rubocop', 'stylelint')):
        return "lint"
    if any(kw in msg_lower for kw in ('type error', 'typecheck', 'tsc',
           'mypy', 'pyright', 'cannot find name', 'is not assignable')):
        return "type"
    if any(kw in msg_lower for kw in ('test', 'assert', 'expect', 'FAIL',
           'pytest', 'jest', 'mocha', 'cargo test')):
        return "test"
    if any(kw in msg_lower for kw in ('build', 'compile', 'compilation',
           'make error', 'cargo build', 'webpack', 'esbuild')):
        return "build"
    return "runtime"


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

    # 4. Open DB (if needed)
    db = None
    if p.db_path and Path(p.db_path).exists():
        try:
            db = SessionDB(p.db_path)
        except Exception:
            pass

    exit_code = 0
    try:
        session_id = p.session_id
        agent_id = p.agent_id
        tool_name = p.tool_name

        # ----------------------------------------------------------------
        # (a) Extract error info
        # ----------------------------------------------------------------
        error_message = (p.get("error") or p.get("error_message") or
                         p.get("message") or "Unknown error")
        error_message = str(error_message)[:2048]

        # ----------------------------------------------------------------
        # (b) Determine file path (try multiple locations)
        # ----------------------------------------------------------------
        error_file = p.file_path
        if not error_file:
            error_file = p.get("tool_input", "file_path")
        if not error_file:
            # Extract from command text
            cmd = p.command
            if cmd:
                m = re.search(r'/[^\s]+\.[a-zA-Z]+', cmd)
                if m:
                    error_file = m.group(0)
        error_file = str(error_file) if error_file else ""

        logger.log("INFO", f"Tool failure: tool={tool_name} file={error_file} "
                   f"error={error_message[:200]}")

        # ----------------------------------------------------------------
        # (c) Categorize error
        # ----------------------------------------------------------------
        error_category = categorize_error(error_message)
        logger.log("INFO", f"Error category: {error_category}")

        # ----------------------------------------------------------------
        # (d) Count previous errors and (e) insert new error
        # ----------------------------------------------------------------
        attempt_number = 1

        if db and error_file:
            try:
                prev_count = db.query_scalar(
                    "SELECT COUNT(*) FROM error_log "
                    "WHERE file_path = ? AND session_id = ?",
                    (error_file, session_id),
                )
                attempt_number = int(prev_count or 0) + 1
            except Exception as e:
                logger.log("WARN", f"Error count query failed: {e}")

            try:
                db.exec(
                    "INSERT INTO error_log "
                    "(session_id, agent_id, file_path, error_category, error_message, "
                    "attempt_number, resolution) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (session_id, agent_id, error_file, error_category,
                     error_message, attempt_number),
                )
                logger.log("INFO", f"Recorded error: attempt={attempt_number} "
                           f"file={error_file} category={error_category}")
            except Exception as e:
                logger.log("WARN", f"Error log insert failed: {e}")
        elif not error_file:
            logger.log("WARN", "No file_path could be determined for failed tool call")
        elif not db:
            logger.log("WARN", "No session DB available for error tracking")

        # ----------------------------------------------------------------
        # (f) Escalation ladder
        # ----------------------------------------------------------------
        if error_file:
            short_error = error_message[:300]

            if attempt_number >= 5:
                # Hard block
                logger.log("ERROR", f"BLOCKED: 5+ attempts on {error_file}")
                print(f"BLOCKED: Do not attempt changes to {error_file} again. "
                      "Move to other tasks or wait for user guidance.",
                      file=sys.stderr)
                if db:
                    try:
                        db.exec(
                            "UPDATE error_log SET resolution = 'abandoned' "
                            "WHERE file_path = ? AND session_id = ? AND attempt_number = ?",
                            (error_file, session_id, attempt_number),
                        )
                    except Exception:
                        pass
                exit_code = 2

            elif attempt_number == 4:
                # User escalation + mobile notification
                logger.log("ERROR", f"ESCALATE: 4 attempts on {error_file}")
                print(f"ESCALATE TO USER: 4 failed attempts on {error_file}. "
                      "Human expertise needed.", file=sys.stderr)
                Notifier.escalation("Claude Code: Escalation",
                                    f"4 failed attempts on {Path(error_file).name}")
                if db:
                    try:
                        db.exec(
                            "UPDATE error_log SET resolution = 'escalated_user' "
                            "WHERE file_path = ? AND session_id = ? AND attempt_number = ?",
                            (error_file, session_id, attempt_number),
                        )
                    except Exception:
                        pass

            elif attempt_number == 3:
                # Research trigger
                logger.log("WARN", f"RESEARCH REQUIRED: 3 attempts on {error_file}")
                print(f"RESEARCH REQUIRED: You have failed {error_file} 3 times. "
                      "Stop writing code. Read the relevant documentation or source "
                      "files. Verify your assumptions about what this code needs to "
                      "do before attempting another fix.", file=sys.stderr)
                if db:
                    try:
                        db.exec(
                            "UPDATE error_log SET resolution = 'escalated_research' "
                            "WHERE file_path = ? AND session_id = ? AND attempt_number = ?",
                            (error_file, session_id, attempt_number),
                        )
                    except Exception:
                        pass

            else:
                # Attempts 1-2: simple retry guidance
                logger.log("INFO", f"Attempt {attempt_number} on {error_file}")
                print(f"Error on {error_file}: {short_error}. Fix and retry.",
                      file=sys.stderr)
        else:
            # No file path -- still emit the error for visibility
            short_error = error_message[:300]
            logger.log("INFO", f"Tool failure without identifiable file: {tool_name}")
            print(f"Error in {tool_name}: {short_error}. Fix and retry.",
                  file=sys.stderr)

        # ----------------------------------------------------------------
        # (g) Write to failed_paths at attempt >= 3
        # ----------------------------------------------------------------
        if attempt_number >= 3 and error_file and db:
            try:
                existing = db.query_scalar(
                    "SELECT COUNT(*) FROM failed_paths "
                    "WHERE file_path = ? AND session_id = ? AND still_relevant = 1",
                    (error_file, session_id),
                )

                if not existing or int(existing) == 0:
                    db.exec(
                        "INSERT INTO failed_paths "
                        "(session_id, file_path, approach, reason_failed, context, still_relevant) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (session_id, error_file, error_category, error_message[:1024],
                         f"attempt={attempt_number} tool={tool_name}"),
                    )
                    logger.log("INFO", f"Added {error_file} to failed_paths "
                               f"(attempt {attempt_number})")
                else:
                    db.exec(
                        "UPDATE failed_paths SET reason_failed = ?, context = ?, "
                        "timestamp = datetime('now') "
                        "WHERE file_path = ? AND session_id = ? AND still_relevant = 1",
                        (error_message[:1024],
                         f"attempt={attempt_number} tool={tool_name}",
                         error_file, session_id),
                    )
                    logger.log("INFO", f"Updated failed_paths for {error_file} "
                               f"(attempt {attempt_number})")
            except Exception as e:
                logger.log("WARN", f"Failed paths update failed: {e}")

    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        raise
    except Exception as e:
        logger.log("ERROR", str(e))
        exit_code = 1
    finally:
        # 6. Audit + cleanup
        logger.end(exit_code)
        logger.audit(db, p.session_id, DISPATCHER_NAME, exit_code)
        if db:
            db.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

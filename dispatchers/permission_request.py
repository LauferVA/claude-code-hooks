# /// script
# requires-python = ">=3.10"
# ///

"""permission_request -- Fires when Claude is BLOCKED waiting for approval.

(a) Auto-approve read-only tools via hookSpecificOutput JSON
(b) Conservative Bash auto-approve (safe prefixes, no pipes/redirects)
(c) If not auto-approved: desktop + mobile notification ALWAYS
(d) Record in DB
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier, Config,
)

DISPATCHER_NAME = "permission_request"
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
        tool_name = p.tool_name

        # ----------------------------------------------------------------
        # (1) Check auto-approve config
        # ----------------------------------------------------------------
        auto_approve = Config.get("HOOKS_AUTO_APPROVE_READONLY", "true").lower() == "true"

        # ----------------------------------------------------------------
        # (2) Auto-approve read-only tools via hookSpecificOutput
        # ----------------------------------------------------------------
        READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}

        if auto_approve and tool_name in READ_ONLY_TOOLS:
            output = json.dumps({
                "decision": {"behavior": "allow"},
                "hookSpecificOutput": {
                    "autoApproved": True,
                    "reason": f"Read-only tool {tool_name} auto-approved by hooks",
                },
            })
            print(output)  # stdout -> hookSpecificOutput
            logger.log("INFO", f"Auto-approved read-only tool: {tool_name}")
            return 0

        # ----------------------------------------------------------------
        # (3) Conservative Bash auto-approve
        # ----------------------------------------------------------------
        SAFE_BASH_PREFIXES = [
            "ls", "pwd", "echo", "cat", "head", "tail", "wc",
            "git status", "git diff", "git log", "git branch",
            "git show", "git rev-parse",
            "python --version", "python3 --version",
            "node --version", "npm --version",
            "cargo --version", "go version",
            "which ", "type ",
        ]
        BASH_DANGEROUS_CHARS = re.compile(r'[|;&`$><]')

        if auto_approve and tool_name == "Bash":
            cmd = p.command
            if cmd and not BASH_DANGEROUS_CHARS.search(cmd):
                if any(cmd.strip().startswith(prefix) for prefix in SAFE_BASH_PREFIXES):
                    output = json.dumps({
                        "decision": {"behavior": "allow"},
                        "hookSpecificOutput": {
                            "autoApproved": True,
                            "reason": f"Safe Bash command auto-approved: {cmd[:60]}",
                        },
                    })
                    print(output)
                    logger.log("INFO", f"Auto-approved safe Bash: {cmd[:60]}")
                    return 0

        # ----------------------------------------------------------------
        # (4) Not auto-approved: send notifications ALWAYS
        # ----------------------------------------------------------------
        details = []
        if tool_name:
            details.append(f"tool={tool_name}")
        desc = p.get("description") or p.get("message") or ""
        if desc:
            details.append(str(desc)[:150])
        details_str = ", ".join(details) or "approval needed"

        logger.log("WARN", f"Permission blocked: {details_str}")

        Notifier.desktop("Claude Code -- BLOCKED", f"Permission needed: {details_str}")
        Notifier.mobile("Claude Code -- BLOCKED", f"Permission needed: {details_str}")

        # ----------------------------------------------------------------
        # (5) Record in DB
        # ----------------------------------------------------------------
        if db:
            try:
                db.exec(
                    "INSERT INTO session_metrics (session_id, event_type, key, value) "
                    "VALUES (?, 'permission_blocked', 'details', ?)",
                    (session_id, details_str[:500]),
                )
            except Exception as e:
                logger.log("WARN", f"DB record failed: {e}")

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

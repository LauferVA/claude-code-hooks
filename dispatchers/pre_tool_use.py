# /// script
# requires-python = ">=3.10"
# ///

"""pre_tool_use -- Fires BEFORE every tool call.

(a) Protected file guard (lockfiles, .git/*) -- profile: minimal
(b) Branch guard (block commits to main/master) -- profile: minimal
(c) Dangerous command blocker -- profile: minimal
(d) Command redirect suggestions -- profile: strict
"""

import re
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier, Config,
)

DISPATCHER_NAME = "pre_tool_use"
PROFILE_TAG = "minimal"  # Guards are minimal; redirects are strict


def main() -> int:
    # 1. Kill switch
    if KillSwitch.is_disabled(DISPATCHER_NAME):
        return 0

    # 2. Profile check (minimal -- almost always runs)
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
        tool_name = p.tool_name
        file_path = p.file_path
        command = p.command
        project_dir = p.project_dir

        # ----------------------------------------------------------------
        # (a) Protected file guard (profile: minimal)
        # ----------------------------------------------------------------
        if tool_name in ("Edit", "Write") and file_path:
            PROTECTED_PATTERNS = [
                "*lock.json",
                "*/package-lock.json",
                "*/poetry.lock",
                "*/Cargo.lock",
                "*/yarn.lock",
                "*/.git/*",
                "*/uv.lock",
                "*/pnpm-lock.yaml",
                "*/Gemfile.lock",
                "*/composer.lock",
            ]

            for pattern in PROTECTED_PATTERNS:
                if fnmatch(file_path, pattern):
                    logger.log("WARN", f"Blocked {tool_name} on protected file: {file_path}")
                    print("Do not manually edit lockfiles or git internals. "
                          "Run the appropriate package manager command.", file=sys.stderr)
                    exit_code = 2
                    return exit_code

        # ----------------------------------------------------------------
        # (b) Branch guard (profile: minimal)
        # ----------------------------------------------------------------
        if tool_name == "Bash" and "git commit" in command:
            try:
                result = subprocess.run(
                    ["git", "-C", project_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                branch = result.stdout.strip()
                if branch in ("main", "master"):
                    logger.log("WARN", f"Blocked git commit on protected branch: {branch}")
                    print("Create a feature branch before committing to main/master.",
                          file=sys.stderr)
                    exit_code = 2
                    return exit_code
            except Exception as e:
                logger.log("WARN", f"Branch check failed: {e}")

        # ----------------------------------------------------------------
        # (c) Dangerous command blocker (profile: minimal)
        # ----------------------------------------------------------------
        if tool_name == "Bash" and command:
            DANGEROUS_PATTERNS = [
                # rm -rf / or ~
                (r'rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(/\s*$|~)',
                 "Blocked: 'rm -rf /' or 'rm -rf ~' would destroy the filesystem."),
                # Fork bomb
                (r':\(\)\s*\{\s*:\|:\&\s*\}\s*;\s*:',
                 "Blocked: Fork bomb detected."),
                # dd to block device
                (r'dd\s+.*if=.*of=/dev/[a-z]',
                 "Blocked: dd writing to a block device could destroy disk data."),
                # Piped curl/wget to shell
                (r'(curl|wget)\s.*\|\s*(bash|sh|zsh)',
                 "Blocked: Piping remote scripts directly into a shell is unsafe."),
                # SQL destructive
                (r'(?i)(DROP\s+(DATABASE|TABLE)|TRUNCATE\s+)',
                 "Blocked: Destructive SQL operation detected."),
            ]

            # DELETE without WHERE
            DELETE_PATTERN = re.compile(r'(?i)DELETE\s+FROM\s+')
            DELETE_WHERE = re.compile(r'(?i)DELETE\s+FROM\s+\S+\s+WHERE\s')

            for pattern, reason in DANGEROUS_PATTERNS:
                if re.search(pattern, command):
                    logger.log("WARN", f"Dangerous command blocked: {reason}")
                    print(reason, file=sys.stderr)
                    exit_code = 2
                    return exit_code

            if DELETE_PATTERN.search(command) and not DELETE_WHERE.search(command):
                reason = "Blocked: DELETE FROM without a WHERE clause would delete all rows."
                logger.log("WARN", f"Dangerous command blocked: {reason}")
                print(reason, file=sys.stderr)
                exit_code = 2
                return exit_code

        # ----------------------------------------------------------------
        # (d) Command redirect suggestions (profile: strict)
        # ----------------------------------------------------------------
        if tool_name == "Bash" and command and ProfileManager.should_execute("strict"):
            if command.startswith("grep ") and shutil.which("rg"):
                print("Suggestion: 'rg' (ripgrep) is available and faster than 'grep'.",
                      file=sys.stderr)
            if command.startswith("find ") and shutil.which("fd"):
                print("Suggestion: 'fd' is available and faster than 'find'.",
                      file=sys.stderr)

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

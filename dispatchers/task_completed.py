# /// script
# requires-python = ">=3.10"
# ///
"""TaskCompleted dispatcher -- COMMIT BOUNDARY.

Detect coding vs non-coding. Test gate (pytest/vitest/cargo test/go test,
60s timeout, exit 2 on failure). Secret gate (gitleaks). Commit: stage
ONLY from file_claims, conventional commit format. Notification.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier,
)

DISPATCHER_NAME = "task_completed"
PROFILE_TAG = "minimal"  # Secret gate is minimal; full logic is standard

SOURCE_EXTS = {
    ".py", ".ts", ".js", ".jsx", ".tsx", ".rs", ".go",
    ".java", ".c", ".cpp", ".rb", ".ex", ".exs",
}

TEST_RUNNERS: dict[str, tuple[str, list[str]]] = {
    "python": ("pytest", ["pytest", "--tb=short", "-q"]),
    "typescript": ("vitest", ["npx", "vitest", "run"]),
    "javascript": ("npm", ["npm", "test"]),
    "rust": ("cargo", ["cargo", "test"]),
    "go": ("go", ["go", "test", "./..."]),
}


def _derive_commit_prefix(desc: str) -> str:
    d = desc.lower()
    if any(kw in d for kw in ("fix", "bug", "patch", "hotfix", "repair")):
        return "fix"
    if any(kw in d for kw in ("refactor", "restructur", "reorganiz", "clean")):
        return "refactor"
    if any(kw in d for kw in ("test", "spec", "coverage")):
        return "test"
    if any(kw in d for kw in ("doc", "readme", "comment")):
        return "docs"
    if any(kw in d for kw in ("perf", "optimi", "speed", "fast")):
        return "perf"
    if any(kw in d for kw in ("ci", "deploy", "pipeline", "workflow")):
        return "ci"
    if any(kw in d for kw in ("chore", "maint", "upkeep", "bump", "updat")):
        return "chore"
    return "feat"


def main() -> int:
    # 1. Kill switch
    if KillSwitch.is_disabled(DISPATCHER_NAME):
        return 0

    # 2. Profile check -- minimal is always allowed, but we check anyway
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
        # Extract task context
        task_id = (
            p.get("task_id")
            or p.get("tool_input", "task_id")
            or f"task-{int(time.time())}"
        )
        task_desc = (
            p.get("task_description")
            or p.get("tool_input", "description")
            or p.get("tool_input", "prompt")
            or "task"
        )

        logger.log("INFO", f"Task completed: {task_id} -- {task_desc[:80]}")

        # Get all files claimed in this session
        all_files: list[str] = []
        if db:
            rows = db.query(
                "SELECT DISTINCT file_path FROM file_claims WHERE session_id = ?",
                (session_id,),
            )
            all_files = [r["file_path"] for r in rows]

        if not all_files:
            logger.log("INFO", "No files claimed -- skipping all gates")
            # Still notify and record
            Notifier.desktop("Claude Code", f"Task completed: {task_desc[:80]}")
            if Notifier.is_walkaway():
                Notifier.mobile("Claude Code", f"Task completed: {task_desc[:80]}")
            if db:
                db.exec(
                    "INSERT INTO session_metrics (session_id, event_type, key, value) "
                    "VALUES (?, 'task_completed', ?, datetime('now'))",
                    (session_id, task_id),
                )
            logger.end(0)
            logger.audit(db, session_id, DISPATCHER_NAME, 0)
            if db:
                db.close()
            return 0

        # Determine if coding task
        is_coding = any(Path(f).suffix in SOURCE_EXTS for f in all_files)
        is_standard = ProfileManager.should_execute("standard")

        # ── TEST GATE (coding tasks only, profile: standard) ──
        if is_coding and is_standard and db and project_dir:
            lang = db.query_scalar(
                "SELECT value FROM project_state WHERE key = 'language'"
            )
            runner_info = TEST_RUNNERS.get(lang or "", (None, None))
            runner_name, test_cmd = runner_info

            if test_cmd and shutil.which(test_cmd[0]):
                try:
                    result = subprocess.run(
                        test_cmd, capture_output=True, text=True,
                        timeout=60, cwd=project_dir,
                    )
                    if result.returncode != 0:
                        print("TEST GATE FAILED -- task completion rejected.",
                              file=sys.stderr)
                        print(f"Runner: {runner_name}", file=sys.stderr)
                        print(f"Exit code: {result.returncode}", file=sys.stderr)
                        print(result.stdout, file=sys.stderr)
                        print(result.stderr, file=sys.stderr)

                        db.exec(
                            "INSERT INTO error_log (session_id, task_id, "
                            "error_category, error_message, resolution) "
                            "VALUES (?, ?, 'test', ?, 'escalated_user')",
                            (session_id, task_id,
                             (result.stdout + result.stderr)[:2048]),
                        )

                        Notifier.desktop("Claude Code",
                                         "TESTS FAILED -- task rejected")
                        if Notifier.is_walkaway():
                            Notifier.mobile("Claude Code",
                                            "Tests failed -- blocked")

                        exit_code = 2
                        return exit_code
                except subprocess.TimeoutExpired:
                    logger.log("WARN", "Test runner timed out (60s)")
                except Exception as e:
                    logger.log("WARN", f"Test gate error: {e}")

        # ── SECRET GATE (profile: minimal -- always runs) ──
        if project_dir and shutil.which("gitleaks"):
            try:
                result = subprocess.run(
                    ["gitleaks", "detect", "--no-git", "--source", project_dir],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 1:  # leaks found
                    print("SECRET GATE FAILED -- task completion rejected.",
                          file=sys.stderr)
                    print(result.stdout, file=sys.stderr)

                    if db:
                        db.exec(
                            "INSERT INTO error_log (session_id, task_id, "
                            "error_category, error_message, resolution) "
                            "VALUES (?, ?, 'security', "
                            "'gitleaks detected secrets', 'escalated_user')",
                            (session_id, task_id),
                        )

                    Notifier.desktop("Claude Code",
                                     "SECRETS DETECTED -- task rejected")
                    Notifier.mobile("Claude Code",
                                    "Secrets detected -- blocked")

                    exit_code = 2
                    return exit_code
            except subprocess.TimeoutExpired:
                logger.log("WARN", "gitleaks timed out (30s)")
            except Exception as e:
                logger.log("WARN", f"Secret gate error: {e}")

        # ── COMMIT (profile: standard) ──
        if is_standard and project_dir and Path(project_dir, ".git").is_dir():
            # Stage only claimed files that exist
            files_to_stage = [f for f in all_files if Path(f).exists()]
            if files_to_stage:
                try:
                    subprocess.run(
                        ["git", "-C", project_dir, "add", "--"] + files_to_stage,
                        capture_output=True, timeout=10,
                    )

                    # Check if anything is staged
                    staged = subprocess.run(
                        ["git", "-C", project_dir, "diff", "--cached",
                         "--name-only"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if staged.stdout.strip():
                        # Derive conventional commit prefix
                        prefix = _derive_commit_prefix(task_desc)
                        subject = task_desc[:68].replace("\n", " ")
                        commit_msg = f"{prefix}: {subject}"

                        subprocess.run(
                            ["git", "-C", project_dir, "commit",
                             "-m", commit_msg],
                            capture_output=True, text=True, timeout=10,
                        )

                        commit_hash = subprocess.run(
                            ["git", "-C", project_dir, "rev-parse",
                             "--short", "HEAD"],
                            capture_output=True, text=True, timeout=5,
                        ).stdout.strip()

                        logger.log("INFO",
                                   f"Committed {commit_hash}: {commit_msg}")

                        if db:
                            db.exec(
                                "INSERT INTO session_metrics "
                                "(session_id, event_type, key, value) "
                                "VALUES (?, 'commit', ?, ?)",
                                (session_id, task_id, commit_hash),
                            )
                except subprocess.TimeoutExpired:
                    logger.log("WARN", "Git commit timed out")
                except Exception as e:
                    logger.log("WARN", f"Commit error: {e}")

        # ── Notification ──
        Notifier.desktop("Claude Code", f"Task completed: {task_desc[:80]}")
        if Notifier.is_walkaway():
            Notifier.mobile("Claude Code", f"Task completed: {task_desc[:80]}")

        # ── Record in session_metrics ──
        if db:
            db.exec(
                "INSERT INTO session_metrics (session_id, event_type, key, value) "
                "VALUES (?, 'task_completed', ?, datetime('now'))",
                (session_id, task_id),
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

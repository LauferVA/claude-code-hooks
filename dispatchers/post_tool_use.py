# /// script
# requires-python = ">=3.10"
# ///

"""post_tool_use -- Fires AFTER every successful Edit/Write tool call.

(a) Tool filter (Edit/Write only)
(b) Path exclusions (binary, generated, vendored)
(c) Gitleaks secret scan (warn only)
(d) No-mock-code blocker (Python: ast; JS/TS/Rust/Go: pattern)
(e) File claim tracking
(f) Gitignore check for new files (Write only)
"""

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from hooks_common import (
    PayloadParser, SessionDB, Logger, KillSwitch, ProfileManager,
    Notifier, Config,
)

DISPATCHER_NAME = "post_tool_use"
PROFILE_TAG = "standard"


# ---------------------------------------------------------------------------
# Mock-code detection helpers
# ---------------------------------------------------------------------------

def _check_python_mock(file_path: str) -> bool:
    """Use ast.parse to detect placeholder-only function bodies.
    Excludes abstract classes (ABC import or @abstractmethod)."""
    try:
        source = Path(file_path).read_text()
    except OSError:
        return False

    # Exclude abstract base class files
    if re.search(r'(from\s+abc\s+import|@abstractmethod)', source):
        return False

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            # Strip leading docstring
            if (body and isinstance(body[0], ast.Expr) and
                    isinstance(getattr(body[0], 'value', None), ast.Constant) and
                    isinstance(body[0].value.value, str)):
                body = body[1:]
            if len(body) == 1:
                stmt = body[0]
                # pass as sole body
                if isinstance(stmt, ast.Pass):
                    return True
                # raise NotImplementedError() as sole body
                if isinstance(stmt, ast.Raise) and stmt.exc is not None:
                    if isinstance(stmt.exc, ast.Call):
                        func = stmt.exc.func
                        name = getattr(func, 'id', '') or getattr(func, 'attr', '')
                        if name == 'NotImplementedError':
                            return True
    return False


def _check_js_ts_mock(file_path: str) -> bool:
    """Heuristic: find single-statement function bodies that are stubs."""
    MOCK_PATTERNS = [
        r'throw new Error\(.*(not implemented|Not implemented)',
        r'//\s*TODO:\s*implement\s*$',
        r'/\*\s*stub\s*\*/',
        r'/\*\s*actual logic here\s*\*/',
    ]
    try:
        lines = Path(file_path).read_text().splitlines()
    except OSError:
        return False

    for i, line in enumerate(lines):
        stripped = line.strip()
        for pattern in MOCK_PATTERNS:
            if re.search(pattern, stripped):
                # Check if this is the sole body of a function
                # Previous line should end with {, next line should be }
                if i > 0 and i < len(lines) - 1:
                    prev = lines[i - 1].strip()
                    nxt = lines[i + 1].strip()
                    if prev.endswith("{") and nxt.startswith("}"):
                        return True
    return False


def _check_compiled_mock(file_path: str) -> bool:
    """Heuristic for Rust, Go, Java."""
    COMPILED_MOCK_PATTERNS = [
        r'todo!\(\)',
        r'unimplemented!\(\)',
        r'panic!\("not implemented"\)',
        r'throw new (Unsupported|Runtime).*Exception\("not implemented"\)',
    ]
    try:
        lines = Path(file_path).read_text().splitlines()
    except OSError:
        return False

    for i, line in enumerate(lines):
        stripped = line.strip()
        for pattern in COMPILED_MOCK_PATTERNS:
            if re.search(pattern, stripped):
                if i > 0 and i < len(lines) - 1:
                    prev = lines[i - 1].strip()
                    nxt = lines[i + 1].strip()
                    if prev.endswith("{") and nxt.startswith("}"):
                        return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
        tool_name = p.tool_name
        file_path = p.file_path
        session_id = p.session_id
        agent_id = p.agent_id

        # ----------------------------------------------------------------
        # (a) Tool filter -- only process Edit and Write
        # ----------------------------------------------------------------
        if tool_name not in ("Edit", "Write"):
            return 0

        # ----------------------------------------------------------------
        # (b) Path exclusions
        # ----------------------------------------------------------------
        EXCLUDED_EXTENSIONS = {
            ".csv", ".parquet", ".db", ".sqlite", ".png",
            ".jpg", ".jpeg", ".pdf", ".zip", ".whl", ".tar",
            ".gz", ".ico", ".svg", ".woff", ".woff2", ".ttf",
            ".eot", ".mp3", ".mp4", ".wav", ".webm",
        }
        EXCLUDED_DIRS = {
            "node_modules", "__pycache__", ".git", "dist", "build",
            "target", ".next", ".nuxt", "coverage", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", "venv", ".venv",
        }

        if file_path:
            fp = Path(file_path)
            if fp.suffix in EXCLUDED_EXTENSIONS:
                logger.log("INFO", f"Skipping excluded extension: {file_path}")
                return 0
            for part in fp.parts:
                if part in EXCLUDED_DIRS:
                    logger.log("INFO", f"Skipping excluded directory: {file_path}")
                    return 0

        # ----------------------------------------------------------------
        # (c) Gitleaks secret scan (warn only)
        # ----------------------------------------------------------------
        if file_path and Path(file_path).exists() and shutil.which("gitleaks"):
            try:
                result = subprocess.run(
                    ["gitleaks", "detect", "--no-git", "--source", file_path],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 1:  # leaks found
                    print(f"WARNING: gitleaks detected potential secrets in {file_path}. "
                          "Review before committing.", file=sys.stderr)
                    logger.log("WARN", f"Potential secrets in {file_path}")
            except Exception as e:
                logger.log("WARN", f"Gitleaks scan failed: {e}")

        # ----------------------------------------------------------------
        # (d) No-mock-code blocker
        # ----------------------------------------------------------------
        SOURCE_EXTENSIONS = {".py", ".ts", ".js", ".jsx", ".tsx", ".rs", ".go", ".java"}

        if file_path:
            fp = Path(file_path)
            if fp.suffix in SOURCE_EXTENSIONS and fp.exists():
                has_mock = False

                if fp.suffix == ".py":
                    has_mock = _check_python_mock(file_path)
                elif fp.suffix in (".ts", ".js", ".jsx", ".tsx"):
                    has_mock = _check_js_ts_mock(file_path)
                elif fp.suffix in (".rs", ".go", ".java"):
                    has_mock = _check_compiled_mock(file_path)

                if has_mock:
                    logger.log("WARN", f"Mock/placeholder code detected in {file_path}")
                    print("Do not use mock code or placeholders. "
                          "Implement the full required logic.", file=sys.stderr)
                    exit_code = 2
                    return exit_code

        # ----------------------------------------------------------------
        # (e) File claim tracking
        # ----------------------------------------------------------------
        if db and file_path:
            try:
                db.exec(
                    "INSERT INTO file_claims (session_id, agent_id, file_path, operation) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, agent_id, file_path, tool_name),
                )
                logger.log("INFO", f"Recorded file claim: {tool_name} on {file_path}")
            except Exception as e:
                logger.log("WARN", f"File claim tracking failed: {e}")

        # ----------------------------------------------------------------
        # (f) Gitignore check for new files (Write only)
        # ----------------------------------------------------------------
        if tool_name == "Write" and file_path and Path(file_path).exists():
            file_dir = str(Path(file_path).parent)
            try:
                # Check if inside a git repo
                result = subprocess.run(
                    ["git", "-C", file_dir, "rev-parse", "--git-dir"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    # Check if file is NOT gitignored
                    check = subprocess.run(
                        ["git", "-C", file_dir, "check-ignore", "-q", file_path],
                        capture_output=True, timeout=5,
                    )
                    if check.returncode != 0:  # Not ignored
                        basename = Path(file_path).name
                        warn_patterns = [".env", ".env.", ".log", ".tmp"]
                        if any(basename.startswith(p) or basename.endswith(p.lstrip("."))
                               for p in warn_patterns):
                            print(f"WARNING: {file_path} is not in .gitignore "
                                  "but looks like a file that should be ignored.",
                                  file=sys.stderr)
                            logger.log("WARN", f"File {file_path} is not gitignored "
                                       "but looks like it should be")
            except Exception as e:
                logger.log("WARN", f"Gitignore check failed: {e}")

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

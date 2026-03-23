"""Claude Code Hooks -- AST Layer 1-2 Code Summary Extraction.

Provides summarize_file() which routes to language-specific summarizers.
Uses ONLY stdlib modules: ast, re, pathlib.

This file has NO PEP 723 header -- it is imported by dispatchers, not
executed directly.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Python Summarizer (AST-based)
# ---------------------------------------------------------------------------

def summarize_python(file_path: str) -> str:
    """Uses ast.parse to extract:
    - Import statements (from X import Y)
    - Class declarations with method lists (name, args, return annotation)
    - Top-level function declarations with args and return type
    - Top-level constants (ALL_CAPS assignments)

    Returns formatted text like:
        imports: os, sys, pathlib.Path
        class MyClass:
          def method_a(self, x: int) -> str
          def method_b(self) -> None
        def top_level_func(path: str, count: int = 0) -> bool
        const: MAX_RETRIES = 3
    """
    try:
        source = Path(file_path).read_text()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return f"(could not parse {file_path})"

    lines: list[str] = []

    # Imports
    imports: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}")
    if imports:
        lines.append(f"imports: {', '.join(imports[:20])}")

    # Classes and functions
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig = _format_func_sig(item)
                    methods.append(f"  {sig}")
            lines.append(f"class {node.name}:")
            lines.extend(methods[:15])  # Cap at 15 methods per class
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig = _format_func_sig(node)
            lines.append(sig)
        elif isinstance(node, ast.Assign):
            # Top-level constants
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    lines.append(f"const: {target.id}")

    return "\n".join(lines)


def _format_func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a function signature: 'def name(arg: type, ...) -> return'"""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_parts: list[str] = []
    for arg in node.args.args:
        name = arg.arg
        if name == "self" or name == "cls":
            args_parts.append(name)
            continue
        ann = ""
        if arg.annotation:
            ann = f": {ast.unparse(arg.annotation)}"
        args_parts.append(f"{name}{ann}")
    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"
    return f"{prefix} {node.name}({', '.join(args_parts)}){ret}"


# ---------------------------------------------------------------------------
# TypeScript/JavaScript Summarizer (regex-based)
# ---------------------------------------------------------------------------

def summarize_typescript(file_path: str) -> str:
    """Grep for export/class/interface/type/function/const declarations.
    No AST -- pure regex on source text."""
    try:
        source = Path(file_path).read_text()
    except OSError:
        return f"(could not read {file_path})"

    patterns = [
        (r'^\s*export\s+(default\s+)?(class|interface|type|enum|function|const|let|var)\s+(\w+)', 'export'),
        (r'^\s*(class|interface|type|enum)\s+(\w+)', 'decl'),
        (r'^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)', 'func'),
        (r'^\s*(?:export\s+)?const\s+(\w+)\s*[:=]', 'const'),
    ]

    seen: set[str] = set()
    lines: list[str] = []
    for line in source.splitlines():
        for pattern, kind in patterns:
            m = re.match(pattern, line)
            if m:
                text = line.strip()[:120]
                if text not in seen:
                    seen.add(text)
                    lines.append(text)
                break
    return "\n".join(lines[:40])  # Cap at 40 declarations


# ---------------------------------------------------------------------------
# Rust Summarizer (regex-based)
# ---------------------------------------------------------------------------

def summarize_rust(file_path: str) -> str:
    """Grep for pub fn/struct/enum/impl/trait/mod/use declarations."""
    try:
        source = Path(file_path).read_text()
    except OSError:
        return f"(could not read {file_path})"

    patterns = [
        r'^\s*(?:pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+\w+',
        r'^\s*(?:pub(?:\(crate\))?\s+)?struct\s+\w+',
        r'^\s*(?:pub(?:\(crate\))?\s+)?enum\s+\w+',
        r'^\s*impl(?:<[^>]+>)?\s+\w+',
        r'^\s*(?:pub(?:\(crate\))?\s+)?trait\s+\w+',
        r'^\s*(?:pub(?:\(crate\))?\s+)?mod\s+\w+',
        r'^\s*use\s+',
    ]

    lines: list[str] = []
    for line in source.splitlines():
        for pattern in patterns:
            if re.match(pattern, line):
                lines.append(line.strip()[:120])
                break
    return "\n".join(lines[:40])


# ---------------------------------------------------------------------------
# Go Summarizer (regex-based)
# ---------------------------------------------------------------------------

def summarize_go(file_path: str) -> str:
    """Grep for func/type/interface declarations."""
    try:
        source = Path(file_path).read_text()
    except OSError:
        return f"(could not read {file_path})"

    patterns = [
        r'^\s*func\s+(?:\([^)]+\)\s+)?\w+',
        r'^\s*type\s+\w+\s+(struct|interface)',
        r'^\s*type\s+\w+\s+\w+',
    ]

    lines: list[str] = []
    for line in source.splitlines():
        for pattern in patterns:
            if re.match(pattern, line):
                lines.append(line.strip()[:120])
                break
    return "\n".join(lines[:40])


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

SUMMARIZERS = {
    ".py": summarize_python,
    ".ts": summarize_typescript,
    ".tsx": summarize_typescript,
    ".js": summarize_typescript,
    ".jsx": summarize_typescript,
    ".rs": summarize_rust,
    ".go": summarize_go,
}


def summarize_file(file_path: str) -> str:
    """Route to language-specific summarizer. Returns text summary."""
    ext = Path(file_path).suffix
    summarizer = SUMMARIZERS.get(ext)
    if summarizer:
        return summarizer(file_path)
    return f"(no summarizer for {ext})"

#!/bin/bash
# Claude Code Hooks -- Bootstrap Setup
# Run once on a new machine: bash ~/.claude/hooks/setup.sh

set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== Claude Code Hooks Setup (UV Python) ==="

# ----------------------------------------------------------------
# 1. Check and install dependencies
# ----------------------------------------------------------------
echo ""
echo "Checking dependencies..."

DEPS_MISSING=0
check_dep() {
    if command -v "$1" >/dev/null 2>&1; then
        echo "  [ok] $1 ($(command -v "$1"))"
    else
        echo "  [MISSING] $1 -- $2"
        DEPS_MISSING=1
    fi
}

check_dep uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
check_dep gitleaks "brew install gitleaks"
check_dep git "xcode-select --install"

if [ $DEPS_MISSING -eq 1 ]; then
    echo ""
    read -p "Install missing dependencies? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if ! command -v uv >/dev/null 2>&1; then
            echo "Installing uv..."
            curl -LsSf https://astral.sh/uv/install.sh | sh
            export PATH="$HOME/.local/bin:$PATH"
        fi
        if ! command -v gitleaks >/dev/null 2>&1; then
            echo "Installing gitleaks..."
            brew install gitleaks
        fi
    fi
fi

# ----------------------------------------------------------------
# 2. Configure global gitignore
# ----------------------------------------------------------------
echo ""
echo "Configuring gitignore..."

GITIGNORE_GLOBAL="$HOME/.gitignore_global"
if [ ! -f "$GITIGNORE_GLOBAL" ]; then
    touch "$GITIGNORE_GLOBAL"
    git config --global core.excludesfile "$GITIGNORE_GLOBAL"
fi

for pattern in ".claude-session.db" ".claude-session.db-wal" \
               ".claude-session.db-shm" ".claude-compact-pending"; do
    if ! grep -qxF "$pattern" "$GITIGNORE_GLOBAL" 2>/dev/null; then
        echo "$pattern" >> "$GITIGNORE_GLOBAL"
        echo "  Added $pattern to $GITIGNORE_GLOBAL"
    fi
done

# ----------------------------------------------------------------
# 3. Generate config.env if not present
# ----------------------------------------------------------------
echo ""
if [ ! -f "$HOOKS_DIR/config.env" ]; then
    RANDOM_TOPIC="claude-$(openssl rand -hex 6)"
    cp "$HOOKS_DIR/config.env.template" "$HOOKS_DIR/config.env"
    sed -i '' "s/claude-CHANGEME/$RANDOM_TOPIC/" "$HOOKS_DIR/config.env"
    echo "Generated config.env with ntfy topic: $RANDOM_TOPIC"
    echo "  Install the ntfy app on iOS/iPad and subscribe to: $RANDOM_TOPIC"
else
    echo "config.env already exists, skipping."
fi

# ----------------------------------------------------------------
# 4. Create directories
# ----------------------------------------------------------------
echo ""
echo "Creating directories..."
mkdir -p "$HOOKS_DIR/logs"
mkdir -p "$HOOKS_DIR/.spec"

# ----------------------------------------------------------------
# 5. Add shell aliases if not present
# ----------------------------------------------------------------
echo ""
ZSHRC="$HOME/.zshrc"
if [ -f "$ZSHRC" ]; then
    ALIASES_ADDED=0
    add_alias() {
        local name="$1" cmd="$2"
        if ! grep -q "alias $name=" "$ZSHRC" 2>/dev/null; then
            echo "alias $name='$cmd'" >> "$ZSHRC"
            echo "  Added alias: $name"
            ALIASES_ADDED=1
        fi
    }
    add_alias "claude-afk" "touch ~/.claude/.walkaway && echo 'Walk-away mode ON'"
    add_alias "claude-back" "rm -f ~/.claude/.walkaway && echo 'Walk-away mode OFF'"
    add_alias "claude-hooks-off" "touch ~/.claude/hooks/.disabled && echo 'All hooks disabled'"
    add_alias "claude-hooks-on" "rm -f ~/.claude/hooks/.disabled && echo 'All hooks enabled'"
    add_alias "claude-hooks-debug" "tail -f ~/.claude/hooks/logs/*.log"
    add_alias "claude-hooks-health" "cat ~/.claude/hooks/HEALTH 2>/dev/null || echo 'No health check yet'"

    # SessionEnd timeout env var
    if ! grep -q "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS" "$ZSHRC" 2>/dev/null; then
        echo "" >> "$ZSHRC"
        echo "# Claude Code Hooks: increase SessionEnd timeout to 10s" >> "$ZSHRC"
        echo "export CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=10000" >> "$ZSHRC"
        echo "  Added CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=10000 to .zshrc"
        ALIASES_ADDED=1
    fi

    if [ $ALIASES_ADDED -eq 1 ]; then
        echo "  Run: source ~/.zshrc"
    fi
fi

# ----------------------------------------------------------------
# 6. Verify everything
# ----------------------------------------------------------------
echo ""
echo "=== Verification ==="
ERRORS=0

verify() {
    if $1 >/dev/null 2>&1; then
        echo "  [ok] $2"
    else
        echo "  [FAIL] $2"
        ERRORS=1
    fi
}

verify "command -v uv" "uv installed"
verify "command -v gitleaks" "gitleaks installed"
verify "command -v git" "git installed"
verify "command -v python3" "python3 available"
verify "test -f $HOOKS_DIR/config.env" "config.env exists"
verify "test -d $HOOKS_DIR/logs" "logs directory exists"
verify "test -f $HOOKS_DIR/lib/hooks_common.py" "hooks_common.py exists"
verify "test -f $HOOKS_DIR/schema.sql" "schema.sql exists"

# Quick smoke test: import hooks_common
if python3 -c "import sys; sys.path.insert(0, '$HOOKS_DIR/lib'); import hooks_common" 2>/dev/null; then
    echo "  [ok] hooks_common.py imports successfully"
else
    echo "  [FAIL] hooks_common.py import failed"
    ERRORS=1
fi

if [ $ERRORS -eq 0 ]; then
    echo ""
    echo "Setup complete. All checks passed."
    echo "OK -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$HOOKS_DIR/HEALTH"
else
    echo ""
    echo "Setup completed with errors. Fix the issues above and re-run."
    echo "ERRORS -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$HOOKS_DIR/HEALTH"
fi

> **Archived 2026-08-31.** This repository is superseded: the hook system is now
> maintained as part of a consolidated Claude Code baseline configuration
> (publication pending — a link will be added here when it is public). This
> snapshot predates a 2026-08-31 revision of the hook set and no longer
> reflects the maintained system.

# Claude Code Hooks

An event-driven hook system for Claude Code. Provides some safety guardrails, code quality enforcement, context preservation, error recovery, and multi-agent coordination. Hooks fire automatically on every Claude Code session — no per-project configuration needed.

## Quick Start

```bash
# Run setup (installs dependencies, generates config)
bash ~/.claude/hooks/setup.sh

# Activate shell aliases
source ~/.zshrc

# That's it. Hooks are now live in every Claude Code session.
```

## Dependencies

| Tool | Purpose | Install |
|------|---------|---------|
| **Python 3.11+** | Runtime for all dispatchers | Pre-installed on macOS |
| **uv** | Python package manager (used for UV single-file scripts) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **gitleaks** | Secret scanning in source files | `brew install gitleaks` |
| **git** | Version control integration | `xcode-select --install` |
| **ntfy** (iOS/Android app) | Mobile push notifications | App Store / Play Store |

`setup.sh` handles installation of `uv` and `gitleaks` automatically.

**Not required** (eliminated by using Python stdlib):
- ~~jq~~ → Python `json` module
- ~~sqlite3 CLI~~ → Python `sqlite3` module

## Commands

### Shell Aliases (added to ~/.zshrc by setup.sh)

| Command | What it does |
|---------|-------------|
| `claude-afk` | Enable walk-away mode — mobile push for hard blocks |
| `claude-back` | Disable walk-away mode — desktop notifications only |
| `claude-hooks-off` | Disable ALL hooks globally (emergency kill switch) |
| `claude-hooks-on` | Re-enable all hooks |
| `claude-hooks-debug` | Tail all hook log files (live view) |
| `claude-hooks-health` | Show system health status |

### Per-Hook Disable

```bash
# Disable a single hook (e.g., stop formatting)
touch ~/.claude/hooks/.disabled.stop

# Re-enable it
rm ~/.claude/hooks/.disabled.stop
```

## What the Hooks Do

### Safety (PreToolUse)
- **Block dangerous commands**: `rm -rf /`, fork bombs, `dd`, `curl|bash`
- **Block destructive SQL**: `DROP TABLE`, `TRUNCATE`, `DELETE` without `WHERE`
- **Protect lockfiles**: Reject manual edits to `package-lock.json`, `poetry.lock`, `Cargo.lock`, `.git/*`
- **Branch guard**: Block commits directly to `main` or `master`
- **Command suggestions**: Recommend `rg` over `grep`, `fd` over `find`

### Code Quality (PostToolUse + Stop)
- **Secret scanning**: Gitleaks scans every written file for API keys, tokens, passwords
- **No-mock-code enforcement**: Rejects placeholder function bodies (`pass`-only, `TODO: implement`-only). Excludes legitimate abstract classes.
- **Auto-format on turn end**: Routes files to the correct formatter — Ruff (Python), Biome (JS/TS), rustfmt (Rust), gofmt (Go)
- **Type checking**: mypy (Python), tsc (TypeScript) — runs asynchronously, doesn't block

### Error Recovery (PostToolUseFailure)
Per-file escalation ladder:
1. **Attempts 1-2**: "Fix and retry" message
2. **Attempt 3**: "RESEARCH REQUIRED — stop coding, read the docs"
3. **Attempt 4**: "ESCALATE TO USER" + forced mobile notification
4. **Attempt 5+**: Hard block — agent cannot modify the file until you intervene

### Context Preservation (PreCompact + PostCompact + UserPromptSubmit)
- **Before compaction**: Extracts hard data (git diff, active agents, file claims, failed paths, errors, code structure) to SQLite
- **After compaction**: Writes a marker file that UserPromptSubmit picks up on your next message, injecting pristine context into the fresh window
- **Failed paths**: Approaches that were tried and failed are preserved across compactions and sessions — agents never repeat dead ends

### Notifications (Notification + PermissionRequest + Elicitation)
Three-tier routing to prevent alert fatigue:
- **Status updates** (Notification) → Desktop only
- **Permission blocks** (PermissionRequest) → Desktop + mobile always
- **Input needed** (Elicitation) → Desktop + mobile always
- **Error escalation** (attempt 4+) → Forced mobile regardless of walk-away mode

### Permission Auto-Approval (PermissionRequest)
Read-only tools (Read, Glob, Grep, WebSearch) are auto-approved — no more clicking "allow" for safe operations. Configurable via `HOOKS_AUTO_APPROVE_READONLY` in `config.env`.

### Commits (TaskCompleted)
- Commits only at task completion — not per-edit, not per-agent
- **Test gate**: Runs the project's test suite; blocks completion if tests fail
- **Secret gate**: Gitleaks scan before staging; blocks if secrets found
- **LLM-as-judge**: Native prompt hook verifies the task was actually completed
- **Conventional commits**: `feat:`, `fix:`, `refactor:`, etc.
- Stages ONLY files from the agent's file claims — never `git add .`

### Multi-Agent Coordination (SubagentStart + SubagentStop)
- Agent registry in SQLite tracks all spawned agents
- File claim tracking — which agent modified which files
- Zombie cleanup on session start (handles Ctrl+C kills)

### Cross-Session Learning (SessionEnd → SessionStart)
- Background learning extractor runs after session end
- Stores error patterns and failed approaches in global `learnings.db`
- Next session injects relevant learnings for the current project

### Runtime Profiles
Three safety tiers, auto-detected from git branch or set via env var:

| Profile | When | What runs |
|---------|------|-----------|
| **strict** | `main`, `release/*` branches | All hooks including advisory warnings |
| **standard** | `feature/*` branches (default) | Safety + quality + automation |
| **minimal** | `experimental/*`, `spike/*` branches | Safety-critical hooks only |

Override: `export HOOK_PROFILE=strict`

## Architecture

### How Hooks Work

Claude Code pipes a JSON payload to each hook's stdin. The hook:
1. Parses the JSON (PayloadParser)
2. Checks kill switch and profile
3. Makes decisions
4. Outputs to stdout (injected into Claude's context) or stderr (shown as error)
5. Exits 0 (allow) or 2 (block)

### File Structure

```
~/.claude/hooks/
├── dispatchers/           # 16 Python scripts, one per event
│   ├── session_start.py
│   ├── session_end.py
│   ├── pre_compact.py
│   ├── post_compact.py
│   ├── user_prompt_submit.py
│   ├── pre_tool_use.py
│   ├── post_tool_use.py
│   ├── post_tool_use_failure.py
│   ├── stop.py
│   ├── subagent_start.py
│   ├── subagent_stop.py
│   ├── task_completed.py
│   ├── notification.py
│   ├── permission_request.py
│   ├── elicitation.py
│   └── context_backup.py
├── lib/
│   ├── hooks_common.py    # Shared library (PayloadParser, SessionDB, Notifier, Logger, etc.)
│   └── code_summary.py    # AST-based code structure extraction
├── docs/                  # Documentation and research
├── .spec/
│   └── HOOKS_SPEC.md      # Complete implementation spec (3,951 lines)
├── schema.sql             # Project-scoped SQLite schema (9 tables)
├── learnings_schema.sql   # Global learnings database schema
├── policies.json          # Machine-readable policy definitions
├── config.env.template    # Configuration template
├── config.env             # Your config (gitignored)
├── setup.sh               # Bootstrap script
├── logs/                  # Per-dispatcher logs (gitignored)
└── HEALTH                 # System health status
```

### Data Storage

| Database | Location | Scope | Purpose |
|----------|----------|-------|---------|
| Session DB | `${PROJECT_DIR}/.claude-session.db` | Per-project | Agent registry, file claims, errors, snapshots |
| Learnings DB | `~/.claude/hooks/learnings.db` | Global | Cross-session patterns and failed approaches |

Both use SQLite with WAL mode and 5-second busy timeout for multi-agent concurrency.

### Key Design Decisions

1. **Python over bash** — stdlib `json` and `sqlite3` eliminate external dependencies
2. **Project-scoped DB** — prevents state collision between concurrent sessions in different projects
3. **PostCompact bug workaround** — PostCompact stdout doesn't inject into context (confirmed bug). We use a marker file + UserPromptSubmit instead.
4. **Commits at task completion only** — SubagentStop doesn't commit because formatting hasn't run yet. TaskCompleted fires after Stop (which formats), so commits contain clean code.
5. **Failed paths preservation** — the only system in the ecosystem that explicitly tracks and re-injects rejected approaches to prevent dead-end repetition
6. **Three-tier notifications** — prevents alert fatigue by routing informational updates to desktop only, reserving mobile for hard blocks
7. **Error escalation with research trigger** — for domain-specific work (bioinformatics, genomics), repeated failures usually indicate misunderstanding, not code bugs. Attempt 3 forces the agent to stop coding and investigate.

## Configuration

All settings in `~/.claude/hooks/config.env`:

```bash
# Mobile notifications
NTFY_TOPIC="claude-xxxxxx"      # Generated by setup.sh
NTFY_TOKEN=""                    # Optional: for authenticated topics
NTFY_URL="https://ntfy.sh"      # Override for self-hosted

# Permission auto-approval
HOOKS_AUTO_APPROVE_READONLY=true

# Failed paths
HOOKS_FAILED_PATHS_TTL_DAYS=7   # Auto-prune after N days
HOOKS_FAILED_PATHS_MAX_INJECT=20 # Max injected into context

# Timeouts (seconds)
HOOKS_FORMAT_TIMEOUT=10
HOOKS_TYPECHECK_TIMEOUT=15
HOOKS_TEST_TIMEOUT=60

# Logging
HOOKS_LOG_MAX_BYTES=10485760    # 10MB per log file

# TTS (optional)
HOOK_TTS_ENABLED=false
```

## Troubleshooting

**Hooks not firing?**
- Check `~/.claude/settings.json` has the hooks section
- Run `claude-hooks-health` to see system status
- Check `~/.claude/hooks/logs/` for error messages

**Hook blocking something it shouldn't?**
- Disable it: `touch ~/.claude/hooks/.disabled.<dispatcher_name>`
- Check the log: `cat ~/.claude/hooks/logs/<dispatcher_name>.log`

**No mobile notifications?**
- Install ntfy app and subscribe to the topic in `config.env`
- Test: `curl -d "test" https://ntfy.sh/your-topic`
- Check `~/.claude/.walkaway` exists (created by `claude-afk`)

**DB errors?**
- The session DB is created automatically by session_start
- If corrupt: delete `${PROJECT_DIR}/.claude-session.db` and restart the session

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `HOOK_PROFILE` | Override safety profile | Auto-detected from git branch |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | SessionEnd timeout | 10000 (set by setup.sh) |

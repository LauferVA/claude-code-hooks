-- Claude Code Hooks -- Global Learnings Database
-- Stored at ~/.claude/hooks/learnings.db
-- Cross-project learning with frequency counting

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

-- Patterns learned from error resolution, successful approaches
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,     -- absolute path or '*' for universal
    pattern TEXT NOT NULL,          -- human-readable description of what was learned
    frequency INTEGER DEFAULT 1,   -- how many times this pattern was observed
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    category TEXT,                  -- error_pattern, approach, optimization, security
    UNIQUE(project_path, pattern)
);

-- Session summaries for cross-session recall
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary_json TEXT,              -- full 5-layer snapshot JSON
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_learnings_project ON learnings(project_path);
CREATE INDEX IF NOT EXISTS idx_learnings_frequency ON learnings(frequency DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_project ON session_summaries(project_path);

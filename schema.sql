-- Claude Code Hooks -- Session Database Schema
-- Created per-project at ${PROJECT_DIR}/.claude-session.db

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

-- Project-level cached state (language, tools, timestamps)
CREATE TABLE IF NOT EXISTS project_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Spawned agent tracking
CREATE TABLE IF NOT EXISTS agent_registry (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_type TEXT,
    task_description TEXT,
    spawn_time TEXT DEFAULT (datetime('now')),
    end_time TEXT,
    status TEXT DEFAULT 'running',  -- running, completed, failed, aborted_stale
    files_changed TEXT              -- JSON array of file paths
);

-- Which agent modified which files
CREATE TABLE IF NOT EXISTS file_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    file_path TEXT NOT NULL,
    operation TEXT,  -- Write, Edit
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Session-level metrics (duration, counts, milestones)
CREATE TABLE IF NOT EXISTS session_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_type TEXT,
    key TEXT,
    value TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Approaches tried and rejected (preserved across compactions)
CREATE TABLE IF NOT EXISTS failed_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    file_path TEXT,
    approach TEXT,
    reason_failed TEXT,
    context TEXT,
    still_relevant INTEGER DEFAULT 1
);

-- Error tracking with escalation state
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    task_id TEXT,
    agent_id TEXT,
    file_path TEXT,
    error_category TEXT,  -- lint, type, test, build, runtime
    error_message TEXT,
    attempt_number INTEGER DEFAULT 1,
    resolution TEXT        -- auto_fixed, escalated_research, escalated_user, abandoned
);

-- Context snapshots for continuity across compactions and sessions
CREATE TABLE IF NOT EXISTS context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT DEFAULT (datetime('now')),
    snapshot_type TEXT,  -- pre_compact, post_compact, session_end
    content TEXT          -- JSON blob
);

-- Audit trail of all hook executions
CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    event_type TEXT,       -- dispatcher name
    dispatcher TEXT,       -- dispatcher name (same as event_type for command hooks)
    payload_summary TEXT,  -- truncated payload for debugging
    exit_code INTEGER,
    duration_ms INTEGER
);

-- LLM-as-judge verdicts from prompt hooks (optional, populated by TaskCompleted prompt hook)
CREATE TABLE IF NOT EXISTS llm_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    hook_event TEXT,       -- e.g., 'TaskCompleted'
    task_id TEXT,
    verdict_json TEXT,     -- raw JSON response from LLM
    decision TEXT,         -- 'allow' or 'block'
    reason TEXT            -- explanation if blocked
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_agent_session ON agent_registry(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_status ON agent_registry(status);
CREATE INDEX IF NOT EXISTS idx_claims_session ON file_claims(session_id);
CREATE INDEX IF NOT EXISTS idx_claims_agent ON file_claims(agent_id);
CREATE INDEX IF NOT EXISTS idx_claims_timestamp ON file_claims(timestamp);
CREATE INDEX IF NOT EXISTS idx_errors_file ON error_log(file_path, session_id);
CREATE INDEX IF NOT EXISTS idx_errors_session ON error_log(session_id);
CREATE INDEX IF NOT EXISTS idx_failed_relevant ON failed_paths(still_relevant);
CREATE INDEX IF NOT EXISTS idx_failed_session ON failed_paths(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_type ON context_snapshots(snapshot_type);
CREATE INDEX IF NOT EXISTS idx_hook_events_session ON hook_events(session_id);
CREATE INDEX IF NOT EXISTS idx_hook_events_timestamp ON hook_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_verdicts_session ON llm_verdicts(session_id);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON session_metrics(session_id);

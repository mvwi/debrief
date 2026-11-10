"""SQLite schema shared by every stage. One file: debrief.db."""

import sqlite3
from pathlib import Path

from debrief.paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    agent            TEXT,      -- Claude Code | Codex | Pi | ...
    model            TEXT,
    display_name     TEXT,
    author           TEXT,
    started_at       TEXT,
    last_activity_at TEXT,
    checkpoint_count INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    agent_lines      INTEGER,   -- committed lines written by the agent
    human_lines      INTEGER,   -- committed lines added or changed by a person
    prompt           TEXT       -- opening prompt
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    commit_sha    TEXT,
    message       TEXT,
    additions     INTEGER,
    deletions     INTEGER,
    files_changed INTEGER,
    ts            TEXT
);

-- One row per message, in order. Tool results that agents log as "user"
-- messages are folded into tool_calls, not stored here.
CREATE TABLE IF NOT EXISTS messages (
    session_id    TEXT NOT NULL,
    idx           INTEGER NOT NULL,  -- 0-based position within the session
    role          TEXT NOT NULL,     -- user | assistant
    user_kind     TEXT,              -- human | injected (skill/command/hook text) ; NULL for assistant
    ts            TEXT,
    text          TEXT,              -- visible text; pasted blocks kept inline
    checkpoint_id TEXT,              -- the checkpoint this message falls before (NULL after the last)
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    session_id  TEXT NOT NULL,
    msg_idx     INTEGER NOT NULL,    -- messages.idx of the assistant message that made the call
    call_idx    INTEGER NOT NULL,
    name        TEXT,                -- Bash | Edit | Read | apply_patch | ...
    input       TEXT,                -- JSON
    output      TEXT,                -- truncated to ~4 KB
    is_error    INTEGER,             -- 1 when the call failed (non-zero exit, tool error)
    files       TEXT,                -- JSON array of repo-relative paths the call read or wrote
    writes      INTEGER,             -- 1 when the call changed files
    PRIMARY KEY (session_id, msg_idx, call_idx)
);

-- Labels are long-format so the classifier schema can change without migrations.
CREATE TABLE IF NOT EXISTS turn_labels (
    session_id TEXT NOT NULL,
    msg_idx    INTEGER NOT NULL,     -- the human user message being labeled
    field      TEXT NOT NULL,
    value      TEXT,
    confidence REAL,                 -- probability of the chosen value
    probs      TEXT,                 -- JSON {value: probability} when Jev returns a distribution
    model      TEXT,
    input_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (session_id, msg_idx, field)
);

CREATE TABLE IF NOT EXISTS session_labels (
    session_id TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      TEXT,
    confidence REAL,                 -- probability of the chosen value
    probs      TEXT,                 -- JSON {value: probability} when Jev returns a distribution
    model      TEXT,
    input_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (session_id, field)
);

-- One row per log file debrief.sources has looked at, loaded or not, so a re-run can skip
-- files that have not changed.
CREATE TABLE IF NOT EXISTS source_logs (
    path       TEXT PRIMARY KEY,
    session_id TEXT,
    agent      TEXT,
    size       INTEGER,
    mtime      INTEGER,              -- st_mtime_ns
    status     TEXT,                 -- loaded, or why the file was skipped
    cwd        TEXT,                 -- as the log records it, for the --project filter
    dropped    INTEGER               -- entries left out because another session owns them
);

-- Which sessions' logs hold each entry. A resumed or forked session's log starts with a
-- copy of the earlier one; the copies load only into the session that owns them.
CREATE TABLE IF NOT EXISTS source_entries (
    entry_key  TEXT NOT NULL,        -- the entry's uuid, or the format's nearest equivalent
    session_id TEXT NOT NULL,
    native     INTEGER NOT NULL,     -- 1 when the entry names this session as its own
    PRIMARY KEY (entry_key, session_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS source_entries_session ON source_entries (session_id);

CREATE TABLE IF NOT EXISTS gh_prs (
    pr_number  INTEGER PRIMARY KEY,
    title      TEXT,
    state      TEXT,
    merged     INTEGER,
    author     TEXT,
    head_sha   TEXT,
    created_at TEXT,
    url        TEXT
);

-- A PR is settled once merged or closed. Outcome, review and CI analyses should
-- use only settled PRs; turn-level analyses don't need to.
CREATE VIEW IF NOT EXISTS settled_prs AS
    SELECT * FROM gh_prs WHERE merged = 1 OR state = 'closed';

CREATE TABLE IF NOT EXISTS gh_commit_prs (
    commit_sha TEXT NOT NULL,
    pr_number  INTEGER NOT NULL,
    PRIMARY KEY (commit_sha, pr_number)
);

CREATE TABLE IF NOT EXISTS gh_checks (
    check_id     INTEGER PRIMARY KEY,
    commit_sha   TEXT NOT NULL,
    name         TEXT,
    conclusion   TEXT,               -- success | failure | skipped | ...
    app          TEXT,
    run_id       INTEGER,            -- Actions run, when there is one
    started_at   TEXT,
    completed_at TEXT,
    failure_log  TEXT                -- tail of the failed step's log, failures only
);

CREATE TABLE IF NOT EXISTS gh_review_comments (
    comment_id  INTEGER PRIMARY KEY,
    pr_number   INTEGER NOT NULL,
    author      TEXT,
    is_bot      INTEGER,
    path        TEXT,
    line        INTEGER,
    body        TEXT,
    in_reply_to INTEGER,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS event_labels (
    event_type TEXT NOT NULL,        -- review_comment | check_failure
    event_id   INTEGER NOT NULL,     -- comment_id or check_id
    field      TEXT NOT NULL,
    value      TEXT,
    confidence REAL,                 -- probability of the chosen value
    probs      TEXT,                 -- JSON {value: probability} when Jev returns a distribution
    model      TEXT,
    input_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (event_type, event_id, field)
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    kind          TEXT,              -- hotspot | knowledge_gap | found_not_filed | correction_pattern | prompt_practice | ci | review
    title         TEXT,
    detail        TEXT,
    suggested_fix TEXT,
    evidence      TEXT,              -- JSON array of {session_id, msg_idx} or {event_type, event_id}
    created_at    TEXT
);

-- Findings from one run merged by the lever that fixes them (recommend.py).
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,                -- the findings run these were built from
    priority    INTEGER,             -- 1 = do first
    lever       TEXT,                -- agent_config | test_harness | ci_infra | review_tooling | team_practice | backlog
    title       TEXT,
    problem     TEXT,
    change      TEXT,                -- what to do, markdown
    target      TEXT,                -- file or system the change lands in
    effort      TEXT,                -- S | M | L
    impact      TEXT,
    finding_ids TEXT,                -- JSON array of findings.finding_id
    created_at  TEXT
);

-- Whether each item an idea cites supports that idea (verify_evidence.py).
CREATE TABLE IF NOT EXISTS evidence_checks (
    rec_id     INTEGER,              -- recommendations.rec_id
    ref        TEXT,                 -- m:<session_id>:<msg_idx> or e:<event_type>:<event_id>, as in export_site.py
    supports   INTEGER,              -- 1 = supports the idea's claim
    reason     TEXT,
    model      TEXT,
    created_at TEXT,
    PRIMARY KEY (rec_id, ref)
);
"""


# Columns added after tables existed. CREATE TABLE IF NOT EXISTS skips existing
# tables, so connect() adds any that are missing.
ADDED_COLUMNS = [
    ("sessions", "cwd", "TEXT"),
    ("tool_calls", "exit_code", "INTEGER"),  # shell exit code when the format records one
    ("gh_prs", "merged_at", "TEXT"),
    ("gh_prs", "closed_at", "TEXT"),
    ("gh_review_comments", "kind", "TEXT"),          # inline | review | issue (general PR discussion)
    ("gh_review_comments", "review_state", "TEXT"),  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED; review rows only
    ("gh_review_comments", "is_resolved", "INTEGER"),  # thread resolved; inline rows only
    ("gh_review_comments", "is_outdated", "INTEGER"),  # thread's code has since changed; inline rows only
]


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, column, type_ in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
    conn.commit()
    return conn

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

-- PRs a local session opened or pushed to, read from its own gh and git output (github.py).
-- Entire sessions link through checkpoints instead.
CREATE TABLE IF NOT EXISTS session_prs (
    session_id TEXT NOT NULL,
    repo       TEXT NOT NULL,        -- owner/name
    pr_number  INTEGER NOT NULL,
    source     TEXT,                 -- url (printed by gh pr create) | branch (a branch the session pushed)
    PRIMARY KEY (session_id, repo, pr_number)
);

CREATE TABLE IF NOT EXISTS gh_prs (
    repo       TEXT NOT NULL,        -- owner/name
    pr_number  INTEGER NOT NULL,
    title      TEXT,
    state      TEXT,
    merged     INTEGER,
    author     TEXT,
    head_sha   TEXT,
    created_at TEXT,
    url        TEXT,
    merged_at  TEXT,
    closed_at  TEXT,
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS gh_commit_prs (
    commit_sha   TEXT NOT NULL,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    committed_at TEXT,               -- set for commits listed from a local session's PR
    PRIMARY KEY (commit_sha, repo, pr_number)
);

-- A PR is settled once merged or closed. Outcome, review and CI analyses should
-- use only settled PRs; turn-level analyses don't need to.
CREATE VIEW IF NOT EXISTS settled_prs AS
    SELECT * FROM gh_prs WHERE merged = 1 OR state = 'closed';

-- The PRs each session worked on.
CREATE VIEW IF NOT EXISTS session_pr_links AS
    SELECT session_id, repo, pr_number FROM session_prs
    UNION
    SELECT cp.session_id, g.repo, g.pr_number FROM checkpoints cp JOIN gh_commit_prs g ON g.commit_sha = cp.commit_sha;

-- The commits whose checks count for each session: its checkpoints, or the commits of its
-- PRs made while it ran. Two sessions on one PR each get only their own commits.
CREATE VIEW IF NOT EXISTS session_commits AS
    SELECT session_id, commit_sha FROM checkpoints WHERE commit_sha IS NOT NULL
    UNION
    SELECT sp.session_id, g.commit_sha FROM session_prs sp
    JOIN sessions s ON s.session_id = sp.session_id
    JOIN gh_commit_prs g ON g.repo = sp.repo AND g.pr_number = sp.pr_number
    WHERE datetime(g.committed_at) BETWEEN datetime(s.started_at, '-10 minutes')
                                       AND datetime(s.last_activity_at, '+10 minutes');

CREATE TABLE IF NOT EXISTS gh_checks (
    check_id     INTEGER PRIMARY KEY,
    commit_sha   TEXT NOT NULL,
    name         TEXT,
    conclusion   TEXT,               -- success | failure | skipped | ...
    app          TEXT,
    run_id       INTEGER,            -- Actions run, when there is one
    started_at   TEXT,
    completed_at TEXT,
    failure_log  TEXT,               -- tail of the failed step's log, failures only
    repo         TEXT
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
    created_at  TEXT,
    repo        TEXT
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
    -- project (added below): the folder the finding is about; NULL when it spans projects
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
    # What the headless rule needs, kept so a re-run can apply it to a log without reading it.
    ("source_logs", "launcher", "TEXT"),         # as logged: Claude entrypoints, Codex "originator|source"; NULL in rows from before this column
    ("source_logs", "human_turns", "INTEGER"),   # after copied entries are left out; NULL when the log was not parsed
    ("source_logs", "parser", "INTEGER"),        # normalize.PARSER_VERSION the log was last read with
    ("tool_calls", "exit_code", "INTEGER"),  # shell exit code when the format records one
    ("gh_review_comments", "kind", "TEXT"),          # inline | review | issue (general PR discussion)
    ("gh_review_comments", "review_state", "TEXT"),  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED; review rows only
    ("gh_review_comments", "is_resolved", "INTEGER"),  # thread resolved; inline rows only
    ("gh_review_comments", "is_outdated", "INTEGER"),  # thread's code has since changed; inline rows only
    # The project (see session_projects) a finding or idea is about; NULL when it spans projects, and in old runs.
    ("findings", "project", "TEXT"),
    ("recommendations", "project", "TEXT"),
]


def add_repo(conn: sqlite3.Connection) -> None:
    """Move a database from before the gh_* tables named their repo. It held one repo, read
    here from a PR's URL. SQLite cannot change a primary key, so gh_prs and gh_commit_prs are
    rebuilt; SCHEMA then creates the new tables and the rows are copied in. One transaction."""
    old = {t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")] for t in ("gh_prs", "gh_commit_prs")}
    if not old["gh_prs"] or "repo" in old["gh_prs"]:
        return
    url = conn.execute("SELECT url FROM gh_prs WHERE url LIKE 'https://github.com/%/%/pull/%'").fetchone()
    repo = "/".join(url[0].split("/")[3:5]) if url else ""
    create = [stmt for stmt in SCHEMA.split(";") if "CREATE TABLE IF NOT EXISTS gh_prs" in stmt
              or "CREATE TABLE IF NOT EXISTS gh_commit_prs" in stmt]
    conn.execute("BEGIN")
    with conn:
        conn.execute("DROP VIEW IF EXISTS settled_prs")  # reads gh_prs; SCHEMA makes it again
        for table in old:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        for stmt in create:
            conn.execute(stmt)
        for table, columns in old.items():
            conn.execute(f"INSERT INTO {table} (repo, {', '.join(columns)}) SELECT ?, * FROM {table}_old", (repo,))
            conn.execute(f"DROP TABLE {table}_old")
        for table in ("gh_checks", "gh_review_comments"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN repo TEXT")
            conn.execute(f"UPDATE {table} SET repo = ?", (repo,))


def repo_projects(conn: sqlite3.Connection) -> dict[str, str]:
    """Each GitHub repo's project: the folder most of the sessions linked to its PRs ran in."""
    rows = conn.execute("""
        SELECT l.repo, s.cwd FROM main.sessions s JOIN session_pr_links l USING (session_id)
        WHERE s.cwd IS NOT NULL GROUP BY l.repo, s.cwd ORDER BY COUNT(DISTINCT l.session_id), s.cwd DESC""")
    return {repo: cwd for repo, cwd in rows}  # the last row per repo has the most sessions


def session_projects(conn: sqlite3.Connection) -> dict[str, str | None]:
    """session_id -> project, for every stage. A project is the folder the session ran in.

    A session with a PR in a repo whose project is another folder belongs to that project,
    unless its own folder is some repo's project: a worktree that ingest did not resolve
    still lands in its repo this way.
    """
    repos = repo_projects(conn)
    out = dict(conn.execute("SELECT session_id, cwd FROM main.sessions"))
    homes = set(repos.values())
    for sid, repo in conn.execute("SELECT session_id, repo FROM session_pr_links ORDER BY repo DESC"):
        if out.get(sid) not in homes and repo in repos:
            out[sid] = repos[repo]
    return out


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    add_repo(conn)
    conn.executescript(SCHEMA)
    for table, column, type_ in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
    conn.commit()
    return conn

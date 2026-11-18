-- Times are UTC text, "2026-11-17T09:30:00Z", so they compare as strings.

CREATE TABLE users (
	id TEXT PRIMARY KEY,
	github_id INTEGER NOT NULL UNIQUE,
	login TEXT NOT NULL,
	created_at TEXT NOT NULL,
	rights_confirmed_at TEXT
);

CREATE TABLE tokens (
	hash TEXT PRIMARY KEY,
	user_id TEXT NOT NULL,
	expires_at TEXT NOT NULL
);
CREATE INDEX tokens_user ON tokens (user_id);

CREATE TABLE device_codes (
	hash TEXT PRIMARY KEY,
	github_code TEXT NOT NULL,
	interval INTEGER NOT NULL,
	next_poll_at TEXT NOT NULL,
	expires_at TEXT NOT NULL
);

CREATE TABLE uploads (
	id TEXT PRIMARY KEY,
	user_id TEXT NOT NULL,
	machine TEXT NOT NULL,
	tool TEXT NOT NULL,
	redaction INTEGER NOT NULL,
	unpicked INTEGER NOT NULL,
	projects TEXT NOT NULL,
	expires_at TEXT NOT NULL
);
CREATE INDEX uploads_user ON uploads (user_id, machine);

CREATE TABLE upload_objects (
	upload_id TEXT NOT NULL,
	user_id TEXT NOT NULL,
	path TEXT NOT NULL,
	sha256 TEXT NOT NULL,
	bytes INTEGER NOT NULL,
	project TEXT,
	received INTEGER NOT NULL,
	PRIMARY KEY (upload_id, path)
);
CREATE INDEX upload_objects_sha ON upload_objects (user_id, sha256);

CREATE TABLE snapshots (
	user_id TEXT NOT NULL,
	machine TEXT NOT NULL,
	tool TEXT NOT NULL,
	redaction INTEGER NOT NULL,
	unpicked INTEGER NOT NULL,
	committed_at TEXT NOT NULL,
	PRIMARY KEY (user_id, machine)
);

CREATE TABLE projects (
	user_id TEXT NOT NULL,
	machine TEXT NOT NULL,
	key TEXT NOT NULL,
	id TEXT NOT NULL,
	name TEXT NOT NULL,
	sessions INTEGER NOT NULL,
	turns INTEGER NOT NULL,
	analysed_at TEXT,
	PRIMARY KEY (user_id, machine, key)
);
CREATE INDEX projects_id ON projects (user_id, id);

CREATE TABLE objects (
	user_id TEXT NOT NULL,
	machine TEXT NOT NULL,
	path TEXT NOT NULL,
	sha256 TEXT NOT NULL,
	bytes INTEGER NOT NULL,
	project TEXT,
	PRIMARY KEY (user_id, machine, path)
);
CREATE INDEX objects_sha ON objects (user_id, sha256);

-- Always empty. A batch inserts 0 here when its precondition fails; the CHECK then fails and
-- D1 rolls the whole batch back.
CREATE TABLE guard (ok INTEGER NOT NULL CHECK (ok = 1));

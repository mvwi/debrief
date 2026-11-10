"""Export against a tiny synthetic database: local sessions only, then with GitHub data.

DEBRIEF_HOME is read when debrief.paths is first imported, so each case runs the export in
its own interpreter with the variable set.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SCHEMA_VIA_CONNECT = "import sys; from debrief import db; db.connect(sys.argv[1]).close()"


def export(home: Path, fill) -> dict:
    env = {**os.environ, "DEBRIEF_HOME": str(home)}
    env.pop("DEBRIEF_DB", None)
    db = home / "debrief.db"
    subprocess.run([sys.executable, "-c", SCHEMA_VIA_CONNECT, str(db)], check=True, env=env)
    conn = sqlite3.connect(db)
    with conn:
        fill(conn)
    conn.close()
    subprocess.run([sys.executable, "-m", "debrief.export_site"], check=True, env=env, capture_output=True)
    assert (home / "site" / "index.html").exists() and (home / "site" / "app.js").exists()
    return json.loads((home / "site" / "data" / "index.json").read_text())


def local_sessions(conn) -> None:
    """Two sessions in two folders, as a local import leaves them: no author, no checkpoints, no labels."""
    conn.executemany(
        "INSERT INTO sessions (session_id, agent, started_at, last_activity_at, cwd, prompt) VALUES (?, ?, ?, ?, ?, ?)",
        [("s1", "Claude Code", "2026-01-05T10:00:00Z", "2026-01-05T11:00:00Z", "/home/me/alpha", "fix the build"),
         ("s2", "Codex", None, None, None, None)])
    conn.executemany(
        "INSERT INTO messages (session_id, idx, role, user_kind, ts, text) VALUES (?, ?, ?, ?, ?, ?)",
        [("s1", 0, "user", "human", "2026-01-05T10:00:00Z", "fix the build"),
         ("s1", 1, "assistant", None, "2026-01-05T10:01:00Z", "done"),
         ("s1", 2, "user", "human", "2026-01-05T10:02:00Z", "no, that is wrong"),
         ("s2", 0, "user", "human", None, "hello")])


def test_empty_database(tmp_path):
    index = export(tmp_path, lambda conn: None)
    assert index["sessions"] == [] and index["recommendations"] == []
    assert index["meta"]["from"] is None and index["meta"]["to"] is None


def test_local_sessions_only(tmp_path):
    index = export(tmp_path, local_sessions)
    meta = index["meta"]
    assert meta["has"] == {"github": False, "people": False, "checkpoints": False, "projects": True, "labels": False}
    assert meta["repo"] is None and "window_sessions" not in meta
    assert (meta["sessions"], meta["human_turns"], meta["run_id"]) == (2, 3, None)
    by_id = {s["id"]: s for s in index["sessions"]}
    assert by_id["s1"]["project"] == "/home/me/alpha" and by_id["s2"]["project"] is None
    assert by_id["s1"]["checkpoints"] is None and by_id["s1"]["author"] is None and by_id["s1"]["prs"] == []
    assert index["recommendations"] == [] and index["findings"] == {} and index["prs"] == {}
    assert index["ci"] == {"runs_by_pr": {}, "failures": []} and index["reviews"] == []
    shard = json.loads((tmp_path / "site" / "data" / "sessions" / f"{by_id['s1']['shard']}.json").read_text())
    assert [m["i"] for m in shard["s1"]["messages"]] == [0, 1, 2] and shard["s1"]["checkpoints"] == []


def with_github(conn) -> None:
    local_sessions(conn)
    conn.execute("UPDATE sessions SET author = 'ann', checkpoint_count = 1 WHERE session_id = 's1'")
    conn.execute("UPDATE sessions SET author = 'bob' WHERE session_id = 's2'")
    conn.execute("INSERT INTO checkpoints (checkpoint_id, session_id, commit_sha, ts) "
                 "VALUES ('c1', 's1', 'abc1234', '2026-01-05T10:30:00Z')")
    conn.execute("INSERT INTO gh_prs (pr_number, title, state, merged, url) "
                 "VALUES (7, 'Fix build', 'closed', 1, 'https://github.com/acme/widgets/pull/7')")
    conn.execute("INSERT INTO gh_commit_prs VALUES ('abc1234', 7)")
    log = "x\nFAIL\tgithub.com/acme/widgets/pkg/build\t0.3s\n/home/runner/work/widgets/widgets/pkg/build/run.go:12: boom\n"
    conn.executemany(
        "INSERT INTO gh_checks (check_id, commit_sha, name, conclusion, app, completed_at, failure_log) "
        "VALUES (?, 'abc1234', ?, 'failure', ?, '2026-01-05T10:40:00Z', ?)",
        [(1, "test", "github-actions", log), (2, "gate", "entire", None)])
    conn.execute("INSERT INTO gh_review_comments (comment_id, pr_number, author, is_bot, path, body, kind) "
                 "VALUES (9, 7, 'bot', 1, 'pkg/build/run.go', 'nit', 'inline')")
    conn.execute("INSERT INTO turn_labels (session_id, msg_idx, field, value, confidence) "
                 "VALUES ('s1', 2, 'turn_kind', 'correction', 0.9)")
    conn.execute("INSERT INTO findings (run_id, kind, title, evidence) VALUES ('r1', 'ci', 'Build breaks', ?)",
                 (json.dumps([{"session_id": "s1", "msg_idx": 2}, {"event_type": "check_failure", "event_id": 1}]),))
    conn.execute("INSERT INTO recommendations (run_id, priority, lever, title, finding_ids) "
                 "VALUES ('r1', 1, 'made_up_lever', 'Pin the toolchain', '[1]')")


def test_with_github(tmp_path):
    index = export(tmp_path, with_github)
    meta = index["meta"]
    assert meta["has"] == {"github": True, "people": True, "checkpoints": True, "projects": True, "labels": True}
    assert meta["repo"] == "acme/widgets" and meta["run_id"] == "r1"
    s1 = next(s for s in index["sessions"] if s["id"] == "s1")
    assert s1["prs"] == [{"n": 7, "state": "merged"}] and s1["settled"] and s1["corrections"] == 1
    # A job named "test" counts; the approval gate does not. The Go module and checkout dir are not hard-coded.
    assert s1["ci_failures"] == 1
    assert [(f["name"], f["area"], f["files"]) for f in index["ci"]["failures"]] == [("test", "pkg/build", ["pkg/build/run.go"])]
    assert [r["id"] for r in index["reviews"]] == [9]
    (rec,) = index["recommendations"]
    assert rec["lever"] == "made_up_lever" and rec["sessions"] == ["s1"] and rec["people"] == 1
    assert {index["refs"][e]["type"] for e in rec["evidence"]} == {"msg", "check"}

"""`debrief push --dry-run` against a made-up database.

DEBRIEF_HOME is read when debrief.paths is first imported, so each case runs in its own interpreter.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from debrief.sources import CLAUDE_DIR, CODEX_DIR

TOKEN = "ghp_" + "A1b2C3d4E" * 4  # token-shaped; made up
ALPHA, BETA, GAMMA = "/home/me/alpha", "/home/me/beta", "/home/me/acme-client"


def fill(conn) -> None:
    """Three projects. alpha has a PR, labels, a finding and an idea; the token is planted in every
    kind of text that came from a transcript or from GitHub."""
    conn.executemany(
        "INSERT INTO sessions (session_id, agent, started_at, last_activity_at, cwd, prompt, remote) "
        "VALUES (?, 'Claude Code', '2026-01-05T10:00:00Z', '2026-01-05T11:00:00Z', ?, ?, ?)",
        [("a1", ALPHA, f"deploy with {TOKEN}", "octo/alpha"), ("b1", BETA, "hello beta", ""),
         ("g1", GAMMA, "gamma secret plan", ""), ("../evil", ALPHA, "odd id", "")])
    # Where each log was read from: the agents' own folders, as an ingest with no --import leaves it.
    conn.executemany("INSERT INTO source_logs (path, session_id, status) VALUES (?, ?, 'loaded')",
                     [(str(CLAUDE_DIR / "-home-me-alpha" / "a1.jsonl"), "a1"), (str(CODEX_DIR / "2026" / "b1.jsonl"), "b1"),
                      (str(CLAUDE_DIR / "-home-me-acme-client" / "g1.jsonl"), "g1"),
                      (str(CLAUDE_DIR / "-home-me-alpha" / "evil.jsonl"), "../evil")])
    conn.executemany(
        "INSERT INTO messages (session_id, idx, role, user_kind, ts, text) VALUES (?, ?, ?, ?, '2026-01-05T10:00:00Z', ?)",
        [("a1", 0, "user", "human", f"deploy with {TOKEN}"), ("a1", 1, "assistant", None, f"ran it\n{TOKEN}"),
         ("a1", 2, "user", "human", "no, that is wrong"), ("b1", 0, "user", "human", "hello beta"),
         ("g1", 0, "user", "human", "gamma secret plan"), ("../evil", 0, "user", "human", "odd id")])
    conn.execute("INSERT INTO tool_calls (session_id, msg_idx, call_idx, name, input, output, is_error, files, writes) "
                 "VALUES ('a1', 1, 0, 'Bash', ?, ?, 1, '[]', 0)",
                 (json.dumps({"command": f"curl -H 'Authorization: token {TOKEN}'"}), f"denied {TOKEN}"))
    conn.execute("INSERT INTO turn_labels (session_id, msg_idx, field, value, confidence) "
                 "VALUES ('a1', 2, 'turn_kind', 'correction', 0.93)")
    conn.execute("INSERT INTO turn_labels (session_id, msg_idx, field, value, confidence) "
                 "VALUES ('g1', 0, 'turn_kind', 'correction', 0.9)")
    conn.execute("INSERT INTO session_prs VALUES ('a1', 'octo/alpha', 7, 'url')")
    conn.execute("INSERT INTO gh_prs (repo, pr_number, title, state, merged, url) VALUES "
                 f"('octo/alpha', 7, 'add {TOKEN}', 'closed', 1, 'https://github.com/octo/alpha/pull/7')")
    conn.execute("INSERT INTO gh_review_comments (comment_id, pr_number, author, body, repo, kind) "
                 f"VALUES (5, 7, 'rev', 'leaked {TOKEN}', 'octo/alpha', 'inline')")
    evidence = json.dumps([{"session_id": "a1", "msg_idx": 2}, {"event_type": "review_comment", "event_id": 5}])
    conn.execute("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                 f"VALUES (1, 'r1', 'correction_pattern', 'Wrong deploys', 'used {TOKEN}', ?, ?)", (evidence, ALPHA))
    conn.execute("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                 "VALUES (2, 'r1', 'correction_pattern', 'Gamma thing', 'gamma detail', ?, ?)",
                 (json.dumps([{"session_id": "g1", "msg_idx": 0}]), GAMMA))
    conn.execute("INSERT INTO recommendations (rec_id, run_id, priority, lever, title, problem, change, finding_ids, project) "
                 f"VALUES (1, 'r1', 1, 'agent_config', 'Say how to deploy', 'p', 'c {TOKEN}', '[1]', ?)", (ALPHA,))
    # An idea about a picked project that also stood on the unpicked project's finding.
    conn.execute("INSERT INTO recommendations (rec_id, run_id, priority, lever, title, problem, change, finding_ids, project) "
                 "VALUES (5, 'r1', 5, 'agent_config', 'Lemur idea', 'p', 'c', '[1, 2]', ?)", (ALPHA,))


def push(home: Path, *args: str, picked=(ALPHA, BETA), change=None) -> subprocess.CompletedProcess:
    """Run `debrief push` in a home that holds the made-up database, after `change(conn)` if given."""
    env = {**os.environ, "DEBRIEF_HOME": str(home)}
    env.pop("DEBRIEF_DB", None)
    (home / "config.toml").write_text(f"[projects]\ninclude = {json.dumps(list(picked))}\n" if picked else "")
    db = home / "debrief.db"
    new = not db.exists()
    subprocess.run([sys.executable, "-c", "import sys; from debrief import db; db.connect(sys.argv[1]).close()", str(db)],
                   check=True, env=env)
    conn = sqlite3.connect(db)
    with conn:
        if new:
            fill(conn)
        if change:
            change(conn)
    conn.close()
    return subprocess.run([sys.executable, "-m", "debrief.cli", "push", *args], check=False, env=env,
                          capture_output=True, text=True)


def read(out: Path) -> tuple[dict, bytes]:
    """The manifest, and every byte in the bundle."""
    files = sorted(p for p in out.rglob("*") if p.is_file())
    return json.loads((out / "manifest.json").read_text()), b"".join(p.read_bytes() for p in files)


def test_push_without_dry_run_refuses(tmp_path):
    got = push(tmp_path)
    assert got.returncode == 1 and "no server" in got.stderr and got.stdout == ""
    assert not (tmp_path / "push").exists() and not (tmp_path / "machine_id").exists()


def test_nothing_picked_sends_a_count_and_nothing_else(tmp_path):
    got = push(tmp_path, "--dry-run", picked=())
    assert got.returncode == 0, got.stderr
    manifest, data = read(tmp_path / "push")  # the default folder
    assert manifest["projects"] == [] and manifest["unpicked"] == 3
    assert manifest["objects"] == {} and manifest["sent"] == []
    assert [p.name for p in (tmp_path / "push").rglob("*") if p.is_file()] == ["manifest.json"]
    for private in (b"alpha", b"beta", b"acme", b"gamma", b"hello", b"correction", b"/home/me"):
        assert private not in data
    assert "3 other projects on this machine are not picked" in got.stdout


def test_bundle_holds_picked_projects_only_and_no_planted_token(tmp_path):
    out = tmp_path / "out"
    got = push(tmp_path, "--dry-run", "--out", str(out))
    assert got.returncode == 0, got.stderr
    manifest, data = read(out)

    assert TOKEN.encode() not in data and b"A1b2C3d4E" not in data
    assert b"[redacted]" in data
    # The unpicked project: not its name, its session, its text, its label or its finding.
    for private in (b"acme", b"g1", b"gamma", b"Gamma", b"Lemur"):
        assert private not in data
    assert manifest["unpicked"] == 1
    # No folder path in the manifest; projects go by repo, or by a hash on this machine.
    assert "/home/me" not in json.dumps(manifest)
    keys = {p["name"]: p["key"] for p in manifest["projects"]}
    assert keys["alpha"] == "octo/alpha" and keys["beta"].startswith("local:")
    assert manifest["redaction"] == 0 and "Left out 0 findings and 1 ideas" in got.stdout
    assert manifest["format"] == 1 and manifest["machine"] == (tmp_path / "machine_id").read_text()

    # Every object is listed with the hash and size of the file that was written.
    assert sorted(manifest["objects"]) == sorted(manifest["sent"])
    assert {"sessions/a1.json", "sessions/b1.json", "findings.json", "github.json"} <= set(manifest["objects"])
    assert len(manifest["objects"]) == 5  # and the session whose id is not a file name, under a hash
    assert all(p.resolve().is_relative_to(out.resolve()) for p in out.rglob("*"))
    for path, o in manifest["objects"].items():
        assert (out / path).stat().st_size == o["bytes"]

    a1 = json.loads((out / "sessions/a1.json").read_text())
    assert a1["project"] == "octo/alpha" and a1["corrections"] == 1 and a1["prs"] == [{"n": "octo/alpha#7", "state": "merged"}]
    assert a1["messages"][2]["l"] == {"turn_kind": ["correction", 0.93]}
    assert a1["messages"][1]["c"][0]["err"] == "denied [redacted]"
    found = json.loads((out / "findings.json").read_text())
    assert list(found["findings"]) == ["1"] and found["findings"]["1"]["project"] == "octo/alpha"
    assert found["recommendations"][0]["findings"] == [1] and found["run_id"] == "r1"
    assert set(json.loads((out / "github.json").read_text())["prs"]) == {"octo/alpha#7"}

    for line in ("Dry run: nothing was sent", "2 picked", "1 other project on this machine is not picked",
                 "sessions    3, of which 3 sent", "turns       4 of yours, 5 messages", "repo names  octo/alpha",
                 "Made an id for this machine"):
        assert line in got.stdout, line
    assert int(got.stdout.split("redactions  ")[1].split()[0]) >= 7 and "home-folder paths shortened to ~" in got.stdout
    assert "email addresses replaced" in got.stdout and "Upload only what you have the right to share" in got.stdout
    assert str(len(data)) in got.stdout.replace(",", "")


def test_second_push_sends_only_what_changed_and_says_what_it_removes(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    assert push(tmp_path, "--dry-run", "--out", str(first)).returncode == 0
    before, _ = read(first)

    def one_more_turn(conn):
        conn.execute("INSERT INTO messages (session_id, idx, role, user_kind, text) VALUES ('b1', 1, 'user', 'human', 'more')")

    got = push(tmp_path, "--dry-run", "--out", str(second), "--prev", str(first), change=one_more_turn)
    assert got.returncode == 0, got.stderr
    after, _ = read(second)
    assert after["sent"] == ["sessions/b1.json"] and sorted(after["objects"]) == sorted(before["objects"])
    assert sorted(p.name for p in second.rglob("*.json")) == ["b1.json", "manifest.json"]
    assert after["objects"]["sessions/a1.json"] == before["objects"]["sessions/a1.json"]
    assert after["objects"]["sessions/b1.json"] != before["objects"]["sessions/b1.json"]
    assert "Made an id" not in got.stdout and after["machine"] == before["machine"]

    # Unpick beta: its session leaves the manifest, which is how the server learns to delete it.
    got = push(tmp_path, "--dry-run", "--out", str(tmp_path / "third"), "--prev", str(second), picked=(ALPHA,))
    third, _ = read(tmp_path / "third")
    assert "sessions/b1.json" not in third["objects"] and third["sent"] == [] and third["unpicked"] == 2
    assert "This push removes 1 session the server holds" in got.stdout


def test_out_folder_that_is_not_a_bundle_is_left_alone(tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    got = push(tmp_path, "--dry-run", "--out", str(out))
    assert got.returncode != 0 and "not an earlier bundle" in got.stderr
    assert [p.name for p in out.iterdir()] == ["notes.txt"]


def shared_findings(conn) -> None:
    """Findings about all projects together, and ideas that lean on them."""
    both = json.dumps([{"session_id": "a1", "msg_idx": 2}, {"session_id": "b1", "msg_idx": 0}])
    with_gamma = json.dumps([{"session_id": "a1", "msg_idx": 2}, {"session_id": "g1", "msg_idx": 0}])
    conn.executemany("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                     "VALUES (?, 'r1', 'prompt_practice', ?, ?, ?, NULL)",
                     [(3, "Zebra habit", "zebra-detail about the client", with_gamma), (4, "Heron habit", "heron-detail", both)])
    conn.executemany("INSERT INTO recommendations (rec_id, run_id, priority, lever, title, problem, change, finding_ids, project) "
                     "VALUES (?, 'r1', ?, 'team_practice', ?, 'p', 'c', ?, ?)",
                     [(2, 2, "Quokka idea", "[4]", GAMMA),   # about an unpicked project, citing a finding that is kept
                      (3, 3, "Wombat idea", "[3]", None),    # cites only a finding that is left out
                      (4, 4, "Otter idea", "[3, 4]", None),   # one of its two findings is left out
                      (6, 6, "Ibis idea", "[4]", None)])      # every finding it cites is kept


def test_findings_and_ideas_that_draw_on_an_unpicked_project_are_left_out(tmp_path):
    got = push(tmp_path, "--dry-run", change=shared_findings)
    assert got.returncode == 0, got.stderr
    _, data = read(tmp_path / "push")
    for private in (b"Zebra", b"zebra-detail", b"Quokka", b"Wombat", b"Otter", b"Lemur", b"acme", b"gamma", b"Gamma"):
        assert private not in data, private
    found = json.loads((tmp_path / "push" / "findings.json").read_text())
    assert sorted(found["findings"]) == ["1", "4"] and found["findings"]["4"]["project"] is None
    ideas = {r["title"]: r for r in found["recommendations"]}
    assert sorted(ideas) == ["Ibis idea", "Say how to deploy"]
    assert ideas["Ibis idea"]["findings"] == [4] and set(ideas["Ibis idea"]["evidence"]) == {"m:a1:2", "m:b1:0"}
    assert "m:g1:0" not in found["refs"]
    assert "Left out 1 findings and 4 ideas: they draw on projects you have not picked" in got.stdout

    # With beta unpicked too, the finding that drew on it goes, and the idea that stood on it.
    got = push(tmp_path, "--dry-run", picked=(ALPHA,))
    _, data = read(tmp_path / "push")
    assert b"Heron" not in data and b"heron-detail" not in data and b"Ibis" not in data and b"beta" not in data
    assert "Left out 2 findings and 5 ideas" in got.stdout


def test_a_finding_about_one_project_goes_when_its_evidence_reaches_outside(tmp_path):
    def reach(conn):
        conn.execute("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                     "VALUES (7, 'r1', 'hotspot', 'Puffin spot', 'puffin-detail', ?, ?)",
                     (json.dumps([{"session_id": "a1", "msg_idx": 2}, {"session_id": "g1", "msg_idx": 0}]), ALPHA))

    got = push(tmp_path, "--dry-run", change=reach)
    _, data = read(tmp_path / "push")
    assert b"Puffin" not in data and b"puffin-detail" not in data and b"Wrong deploys" in data
    assert "Left out 1 findings and 1 ideas" in got.stdout


def test_sessions_that_name_an_author_are_other_peoples_and_are_left_out(tmp_path):
    def teammate(conn):
        conn.execute("INSERT INTO sessions (session_id, agent, author, started_at, last_activity_at, cwd, prompt) "
                     "VALUES ('t1', 'Claude Code', 'bob', '2026-01-06T10:00:00Z', '2026-01-06T11:00:00Z', ?, 'narwhal plan')",
                     (ALPHA,))
        conn.execute("INSERT INTO messages (session_id, idx, role, user_kind, text) VALUES ('t1', 0, 'user', 'human', 'narwhal plan')")
        conn.execute("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                     "VALUES (8, 'r1', 'hotspot', 'Walrus spot', 'walrus-detail', ?, ?)",
                     (json.dumps([{"session_id": "a1", "msg_idx": 2}, {"session_id": "t1", "msg_idx": 0}]), ALPHA))
        conn.execute("INSERT INTO recommendations (rec_id, run_id, priority, lever, title, problem, change, finding_ids, project) "
                     "VALUES (9, 'r1', 9, 'agent_config', 'Marmot idea', 'p', 'c', '[1, 8]', ?)", (ALPHA,))

    got = push(tmp_path, "--dry-run", change=teammate)
    assert got.returncode == 0, got.stderr
    manifest, data = read(tmp_path / "push")
    for private in (b"narwhal", b"t1", b"bob", b"Walrus", b"walrus-detail", b"Marmot"):
        assert private not in data, private
    assert "sessions/t1.json" not in manifest["objects"]
    alpha = next(p for p in manifest["projects"] if p["name"] == "alpha")
    assert (alpha["sessions"], alpha["turns"]) == (2, 3)  # a1 and the odd-id session; not the teammate's
    assert "Left out 1 sessions: they are other people's" in got.stdout
    assert "Left out 1 findings and 2 ideas" in got.stdout


def test_a_log_imported_from_another_folder_is_left_out(tmp_path):
    def imported(conn):
        # No author, as every log file loads; one read from a folder given to --import, one from a
        # path that only looks as if it were under the Claude Code folder, one with no log on record.
        for sid, word, path in (("i1", "pangolin", "/home/me/Downloads/logs/i1.jsonl"),
                                ("i2", "axolotl", str(CLAUDE_DIR / ".." / ".." / "Downloads" / "i2.jsonl")),
                                ("i3", "okapi", None)):
            conn.execute("INSERT INTO sessions (session_id, agent, started_at, last_activity_at, cwd, prompt) "
                         "VALUES (?, 'Claude Code', '2026-01-07T10:00:00Z', '2026-01-07T11:00:00Z', ?, ?)", (sid, ALPHA, word))
            conn.execute("INSERT INTO messages (session_id, idx, role, user_kind, text) VALUES (?, 0, 'user', 'human', ?)",
                         (sid, f"{word} plan"))
            if path:
                conn.execute("INSERT INTO source_logs (path, session_id, status) VALUES (?, ?, 'loaded')", (path, sid))
        # A copy of a1's log sits in an imported folder too; the one that was loaded is the local one.
        conn.execute("INSERT INTO source_logs (path, session_id, status) VALUES ('/home/me/Downloads/logs/a1.jsonl', 'a1', "
                     "'duplicate file')")
        conn.execute("INSERT INTO findings (finding_id, run_id, kind, title, detail, evidence, project) "
                     "VALUES (10, 'r1', 'hotspot', 'Tapir spot', 'tapir-detail', ?, ?)",
                     (json.dumps([{"session_id": "a1", "msg_idx": 2}, {"session_id": "i1", "msg_idx": 0}]), ALPHA))

    got = push(tmp_path, "--dry-run", change=imported)
    assert got.returncode == 0, got.stderr
    manifest, data = read(tmp_path / "push")
    for private in (b"pangolin", b"axolotl", b"okapi", b"i1", b"i2", b"i3", b"Tapir", b"tapir-detail"):
        assert private not in data, private
    assert "sessions/a1.json" in manifest["objects"] and len(manifest["objects"]) == 5
    assert "Left out 3 sessions: they were imported from another folder." in got.stdout
    assert "they are other people's" not in got.stdout

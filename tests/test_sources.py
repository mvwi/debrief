"""Local-log ingest, on small made-up logs."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from debrief import db, normalize, scrape, sources


def claude_entries(session_id: str, turns: list[tuple[str, str]], cwd: str = "/work/app", entrypoint: str = "cli",
                   sidechain: bool = False) -> list[dict]:
    """A user entry and a one-block reply per (uuid, prompt) turn."""
    out = []
    for n, (uuid, prompt) in enumerate(turns):
        base = {"sessionId": session_id, "cwd": cwd, "entrypoint": entrypoint, "isSidechain": sidechain}
        out.append({**base, "type": "user", "uuid": uuid, "timestamp": f"2026-01-02T10:{n:02}:00.000Z",
                    "message": {"role": "user", "content": prompt}})
        out.append({**base, "type": "assistant", "uuid": uuid + "-reply", "timestamp": f"2026-01-02T10:{n:02}:30.000Z",
                    "message": {"id": f"msg-{uuid}", "model": "claude-test-1", "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": f"call-{uuid}", "name": "Read", "input": {"file_path": f"{cwd}/a.py"}}],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}}})
    return out


def write(path: Path, entries: list[dict], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + tail)
    return path


def codex_entries(session_id: str, prompt: str, source: str = "vscode") -> list[dict]:
    def item(kind: str, payload: dict, second: int) -> dict:
        return {"timestamp": f"2026-01-03T09:00:{second:02}.000Z", "type": kind, "payload": payload}
    return [
        item("session_meta", {"id": session_id, "cwd": "/work/api", "source": source}, 0),
        item("turn_context", {"model": "gpt-test"}, 1),
        item("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}, 2),
        item("response_item", {"type": "message", "role": "assistant",
                               "content": [{"type": "output_text", "text": "done"}]}, 3),
        item("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 70, "output_tokens": 9}}}, 4),
    ]


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "debrief.db")


def human_turns(conn) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT session_id, text FROM messages WHERE user_kind = 'human' ORDER BY session_id, idx")]


def run(conn, tmp_path, **kw):
    return sources.sync(conn, kw.pop("claude_dir", tmp_path / "claude"), kw.pop("codex_dir", tmp_path / "codex"), **kw)


def test_loads_claude_and_codex_with_session_rows(conn, tmp_path):
    write(tmp_path / "claude/proj/s1.jsonl", [
        *claude_entries("s1", [("u1", "fix the parser\nplease"), ("u2", "now add a test")]),
        {"type": "ai-title", "aiTitle": "Parser fix", "sessionId": "s1"}])
    write(tmp_path / "codex/2026/01/03/rollout-x-c1.jsonl", codex_entries("c1", "ship it"))
    assert run(conn, tmp_path)["loaded"] == 2

    s1 = dict(conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone())
    assert (s1["agent"], s1["model"], s1["display_name"], s1["cwd"]) == ("Claude Code", "claude-test-1", "Parser fix", "/work/app")
    assert (s1["started_at"], s1["last_activity_at"]) == ("2026-01-02T10:00:00.000Z", "2026-01-02T10:01:30.000Z")
    assert (s1["input_tokens"], s1["output_tokens"], s1["prompt"]) == (30, 6, "fix the parser\nplease")
    # The contract with the other tracks: local sessions have no checkpoint or attribution data.
    assert [s1[k] for k in ("author", "checkpoint_count", "agent_lines", "human_lines")] == [None] * 4
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT files FROM tool_calls WHERE session_id = 's1'")] == ['["a.py"]'] * 2

    c1 = dict(conn.execute("SELECT * FROM sessions WHERE session_id = 'c1'").fetchone())
    assert (c1["agent"], c1["model"], c1["display_name"], c1["cwd"]) == ("Codex", "gpt-test", "ship it", "/work/api")
    assert (c1["input_tokens"], c1["output_tokens"]) == (70, 9)


def test_second_run_reads_nothing_and_a_changed_log_replaces_its_rows(conn, tmp_path, monkeypatch):
    log = write(tmp_path / "claude/proj/s1.jsonl", claude_entries("s1", [("u1", "first")]))
    run(conn, tmp_path)
    monkeypatch.setattr(sources, "read_jsonl", lambda *a, **k: pytest.fail("read an unchanged log"))
    assert run(conn, tmp_path) == {"unchanged": 1}
    monkeypatch.undo()

    # Still running: one more turn, and a last line cut off mid-write.
    write(log, claude_entries("s1", [("u1", "first"), ("u2", "second")]), tail='{"type": "user", "mess')
    assert run(conn, tmp_path) == {"loaded": 1}
    assert human_turns(conn) == [("s1", "first"), ("s1", "second")]


def test_skips_subagent_empty_and_metadata_only_logs(conn, tmp_path):
    write(tmp_path / "claude/proj/s1/subagents/agent-a.jsonl", claude_entries("s1", [("x1", "sub task")], sidechain=True))
    write(tmp_path / "claude/proj/empty.jsonl", [])
    write(tmp_path / "claude/proj/meta.jsonl", [{"type": "summary", "summary": "x", "sessionId": "meta"}])
    write(tmp_path / "claude/proj/bad id!.jsonl", claude_entries("bad id!", [("b1", "hello")]))
    assert run(conn, tmp_path) == {"sub-agent log": 1, "no human turn": 2, "unsafe session ID": 1}
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert run(conn, tmp_path) == {"sub-agent log": 1, "no human turn": 2, "unsafe session ID": 1}  # still reported


def test_headless_runs_are_skipped_unless_asked_for(conn, tmp_path):
    write(tmp_path / "claude/tmp/h1.jsonl", claude_entries("h1", [("h1", "summarise these findings")], entrypoint="sdk-cli"))
    write(tmp_path / "codex/rollout-h2.jsonl", codex_entries("h2", "one-shot", source="exec"))
    write(tmp_path / "claude/proj/s1.jsonl", claude_entries("s1", [("u1", "real work")]))
    assert run(conn, tmp_path) == {"headless run": 2, "loaded": 1}
    assert human_turns(conn) == [("s1", "real work")]
    assert run(conn, tmp_path, include_headless=True) == {"loaded": 2, "unchanged": 1}


def test_sdk_sessions_load_only_with_two_human_turns(conn, tmp_path):
    job = write(tmp_path / "claude/proj/job.jsonl", claude_entries("job", [("j1", "blame this CI failure")], entrypoint="sdk-ts"))
    write(tmp_path / "claude/proj/chat.jsonl", claude_entries("chat", [("c1", "hello"), ("c2", "go on")], entrypoint="sdk-py"))
    # Resumed from a terminal: not every entrypoint is the SDK, so it is a person's session.
    write(tmp_path / "claude/proj/mixed.jsonl", [*claude_entries("mixed", [("m1", "one turn")], entrypoint="sdk-ts"),
                                               {"type": "mode", "sessionId": "mixed", "entrypoint": "cli"}])
    assert run(conn, tmp_path) == {"headless run": 1, "loaded": 2}
    assert [s for s, _ in human_turns(conn)] == ["chat", "chat", "mixed"]

    # The job was a conversation still on its first turn: the second turn brings it in.
    write(job, claude_entries("job", [("j1", "blame this CI failure"), ("j2", "and the fix?")], entrypoint="sdk-ts"))
    assert run(conn, tmp_path) == {"loaded": 1, "unchanged": 2}
    assert [s for s, _ in human_turns(conn)].count("job") == 2


def test_include_headless_loads_one_turn_sdk_sessions(conn, tmp_path):
    write(tmp_path / "claude/proj/job.jsonl", claude_entries("job", [("j1", "review this diff")], entrypoint="sdk-ts"))
    assert run(conn, tmp_path) == {"headless run": 1}
    assert run(conn, tmp_path, include_headless=True) == {"loaded": 1}


@pytest.mark.parametrize("order", [("orig", "resumed", "zfork"), ("zfork", "resumed", "orig"), ("resumed", "orig", "zfork")])
def test_copied_history_loads_once_whatever_the_order(conn, tmp_path, order):
    """A resumed log keeps the copied entries' session ID, so the original owns them. A forked one
    rewrites it, which leaves no trace of the original: there the lowest session ID owns them."""
    shared = [("u1", "first prompt"), ("u2", "second prompt")]
    logs = {
        "orig": claude_entries("orig", [*shared, ("u3", "only in the original")]),
        "resumed": [*claude_entries("orig", shared), *claude_entries("resumed", [("u4", "after the resume")])],
        "zfork": claude_entries("zfork", [*shared, ("u5", "after the fork")]),
    }
    for name in order:  # one log per run, so each arrives after the earlier ones are loaded
        write(tmp_path / f"claude/proj/{name}.jsonl", logs[name])
        run(conn, tmp_path)
    assert human_turns(conn) == [
        ("orig", "first prompt"), ("orig", "second prompt"), ("orig", "only in the original"),
        ("resumed", "after the resume"),
        ("zfork", "after the fork"),
    ]
    # Tool calls of the copied replies went with them.
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 5
    assert conn.execute("SELECT started_at FROM sessions WHERE session_id = 'zfork'").fetchone()[0].startswith("2026-01-02T10:02")


def test_a_copy_with_nothing_new_and_a_moved_session_do_not_double_up(conn, tmp_path):
    turns = [("u1", "first prompt")]
    write(tmp_path / "claude/old-place/s1.jsonl", claude_entries("s1", turns))
    write(tmp_path / "claude/new-place/s1.jsonl", claude_entries("s1", [*turns, ("u2", "later")]))  # same ID, moved
    write(tmp_path / "claude/proj/zcopy.jsonl", claude_entries("zcopy", turns))  # a fork nobody typed into
    counts = run(conn, tmp_path)
    assert (counts["loaded"], counts["duplicate file"], counts["no human turn"]) == (1, 1, 1)
    assert human_turns(conn) == [("s1", "first prompt"), ("s1", "later")]

    # A copy loaded before its original arrives is taken out again once the original owns its turns.
    write(tmp_path / "claude/proj/a-orig.jsonl", claude_entries("a-orig", [("u9", "typed once")]))
    write(tmp_path / "claude/proj/z-resumed.jsonl", claude_entries("a-orig", [("u9", "typed once")]))
    os.rename(tmp_path / "claude/proj/a-orig.jsonl", tmp_path / "hidden.jsonl")
    assert run(conn, tmp_path)["loaded"] == 1
    os.rename(tmp_path / "hidden.jsonl", tmp_path / "claude/proj/a-orig.jsonl")
    run(conn, tmp_path)
    assert human_turns(conn)[0] == ("a-orig", "typed once") and len(human_turns(conn)) == 3
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_project_and_since_filters(conn, tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "pkg").mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
    write(tmp_path / "claude/a/in-sub.jsonl", claude_entries("in-sub", [("u1", "a")], cwd=str(repo / "pkg")))
    write(tmp_path / "claude/a/in-wt.jsonl", claude_entries("in-wt", [("u2", "b")], cwd=str(worktree)))
    old = write(tmp_path / "claude/b/elsewhere.jsonl", claude_entries("elsewhere", [("u3", "c")], cwd="/gone/dir"))
    os.utime(old, (1_600_000_000, 1_600_000_000))  # 2020

    assert run(conn, tmp_path, project=str(repo), since="2021-01-01") == {"loaded": 2, "older than --since": 1}
    # A sub-directory and a worktree both resolve to the main repo root; file paths stay checkout-relative.
    assert {r[0] for r in conn.execute("SELECT cwd FROM sessions")} == {str(repo)}
    assert conn.execute("SELECT files FROM tool_calls WHERE session_id = 'in-sub'").fetchone()[0] == '["pkg/a.py"]'
    assert conn.execute("SELECT files FROM tool_calls WHERE session_id = 'in-wt'").fetchone()[0] == '["a.py"]'
    assert run(conn, tmp_path, project=str(repo)) == {"unchanged": 2, "outside --project": 1}
    counts = run(conn, tmp_path)  # filter lifted: the unchanged log now loads
    assert (counts["loaded"], conn.execute("SELECT cwd FROM sessions WHERE session_id = 'elsewhere'").fetchone()[0]) == (1, "/gone/dir")


def test_import_folder_detects_each_format(conn, tmp_path):
    write(tmp_path / "in/one.jsonl", claude_entries("one", [("u1", "claude prompt")]))
    write(tmp_path / "in/two.jsonl", codex_entries("two", "codex prompt"))
    write(tmp_path / "in/three.jsonl", [
        {"type": "session", "id": "three", "cwd": "/work/pi", "timestamp": "2026-01-04T08:00:00.000Z"},
        {"type": "message", "id": "aaaa1111", "timestamp": "2026-01-04T08:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "pi prompt"}]}},
        {"type": "message", "id": "aaaa2222", "timestamp": "2026-01-04T08:00:02.000Z",
         "message": {"role": "assistant", "model": "pi-test", "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input": 4, "cacheRead": 1, "output": 2}}}])
    write(tmp_path / "in/notes.jsonl", [{"hello": "world"}])
    assert run(conn, tmp_path, import_dirs=[tmp_path / "in"]) == {"loaded": 3, "unknown format": 1}
    assert [tuple(r) for r in conn.execute("SELECT session_id, agent, model, input_tokens FROM sessions ORDER BY agent")] == [
        ("one", "Claude Code", "claude-test-1", 15), ("two", "Codex", "gpt-test", 70), ("three", "Pi", "pi-test", 5)]


def test_unsafe_session_ids_never_reach_the_raw_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(normalize, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(scrape, "RAW_DIR", tmp_path / "raw")
    for bad in ("../../etc", "a/b", "..", ".", "", "x" * 65, "has space", None, 7):
        assert not normalize.safe_session_id(bad), bad
    assert normalize.safe_session_id("efd45cc6-c764-4e22-80eb-cdbc1f9173f9")

    monkeypatch.setattr(scrape, "resolve_repo", lambda repo: "repo-1")
    monkeypatch.setattr(scrape, "api", lambda path: pytest.fail(f"fetched {path}"))
    scrape.main(["--ids", "../../escape"])
    assert "skipping session with unsafe ID '../../escape'" in capsys.readouterr().out
    assert not (tmp_path / "escape").exists() and not (tmp_path / "raw").exists()
    with pytest.raises(scrape.ApiError):
        scrape.fetch_session("repo-1", "../../escape", None)
    assert normalize.load_session(None, tmp_path / "raw" / "bad id").startswith("skip")


def test_entire_folders_still_load(conn, tmp_path):
    folder = tmp_path / "raw" / "e1"
    write(folder / "raw.jsonl", claude_entries("e1", [("u1", "from entire")]))
    (folder / "listing.json").write_text(json.dumps({"agent": "Claude Code", "displayName": "Entire one", "checkpointCount": 1}))
    (folder / "transcript.json").write_text(json.dumps({"session": {}, "checkpoints": [
        {"checkpointId": "cp1", "timestamp": "2026-01-02T11:00:00Z", "commitSha": "abc"}]}))
    assert normalize.load_session(conn, folder).startswith("loaded e1")
    assert tuple(conn.execute("SELECT display_name, checkpoint_count FROM sessions").fetchone()) == ("Entire one", 1)
    assert conn.execute("SELECT checkpoint_id FROM messages WHERE idx = 0").fetchone()[0] == "cp1"

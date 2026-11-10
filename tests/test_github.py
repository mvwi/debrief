"""The GitHub loader: linking local sessions to PRs, fetching several repos, the old-database
migration, and skipping cleanly. `gh` is faked; no network."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from debrief import cli, db, export_site, github, sources


def test_valid_shas_drops_anything_but_hex_ids():
    good = ["abc1234", "0123456789abcdef0123456789abcdef01234567"]
    bad = [None, "", "abc123", "ABC1234", "abc1234/../../user", "abc1234?x=1", "main"]
    assert github.valid_shas([*good, *bad, good[0]]) == good


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A scratch database that github.main() opens instead of the user's."""
    connect = db.connect
    path = tmp_path / "debrief.db"
    monkeypatch.setattr(db, "connect", lambda: connect(path))
    return connect(path)


@pytest.mark.parametrize("repo", ["nope", "a/b/c", "a/b?x", "../x", "a/..", "a.b/c", "a/ b"])
def test_bad_repo_exits_with_one_line(conn, repo):
    with pytest.raises(SystemExit) as e:
        github.main(["--repo", repo])
    assert "owner/name" in str(e.value.code) and "\n" not in str(e.value.code)


def test_no_checkpoints_skips_without_calling_gh(conn, monkeypatch, capsys):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: pytest.fail("gh was called"))
    github.main(["--repo", "acme/widgets"])
    assert capsys.readouterr().out.strip() == "github: skipped, no checkpoint commits to look up"


def test_missing_gh_skips(conn, monkeypatch, capsys):
    conn.execute("INSERT INTO checkpoints (checkpoint_id, session_id, commit_sha) VALUES ('c1', 's1', 'abc1234')")
    conn.commit()
    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    github.main(["--repo", "acme/widgets"])
    assert capsys.readouterr().out.strip() == "github: skipped, the gh CLI is not installed"


# --- local sessions ---------------------------------------------------------------------

def shell_session(session_id: str, cwd: str, hour: int, calls: list[tuple[str, str]]) -> list[dict]:
    """A made-up Claude Code log: two prompts, and one Bash call per (command, output)."""
    base = {"sessionId": session_id, "cwd": cwd, "entrypoint": "cli"}
    out = [{**base, "type": "user", "uuid": f"{session_id}-u{n}", "timestamp": f"2026-03-02T{hour:02}:0{n}:00.000Z",
            "message": {"role": "user", "content": text}} for n, text in enumerate(["fix it", "push it"])]
    for n, (command, output) in enumerate(calls):
        ts = f"2026-03-02T{hour:02}:1{n}:00.000Z"
        out.append({**base, "type": "assistant", "uuid": f"{session_id}-a{n}", "timestamp": ts,
                    "message": {"id": f"{session_id}-m{n}", "model": "claude-test-1", "content": [
                        {"type": "tool_use", "id": f"{session_id}-c{n}", "name": "Bash", "input": {"command": command}}]}})
        out.append({**base, "type": "user", "uuid": f"{session_id}-r{n}", "timestamp": ts, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": f"{session_id}-c{n}", "content": output}]}})
    return out


def load_sessions(conn, tmp_path) -> None:
    """Three sessions. `one` opens widgets#7. `two` pushes to the same branch later, to a branch whose
    PR closed long ago, and to main. `three` pushes quietly from a checkout whose origin is acme/gears."""
    logs = {
        "one": (9, [("git push -u origin fix/parser", "To github.com:acme/widgets.git\n * [new branch]      fix/parser -> fix/parser"),
                    ("gh pr create --fill", "Creating pull request\nhttps://github.com/acme/widgets/pull/7\n"),
                    ("gh pr view 99", "https://github.com/acme/widgets/pull/99")]),      # read, not made
        "two": (11, [("git commit -qm x && git push", "To github.com:acme/widgets.git\n   abc1234..def5678  fix/parser -> fix/parser"),
                     ("git push -q origin old/idea", ""),
                     ("git push origin main", "To github.com:acme/widgets.git\n   1111111..2222222  main -> main")]),
        "three": (13, [("git push -q origin HEAD:feat/teeth", "")]),
    }
    for sid, (hour, calls) in logs.items():
        path = tmp_path / "claude" / "proj" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        cwd = "/work/gears" if sid == "three" else "/work/widgets"
        path.write_text("".join(json.dumps(e) + "\n" for e in shell_session(sid, cwd, hour, calls)))
    assert sources.sync(conn, tmp_path / "claude", tmp_path / "codex")["loaded"] == 3


def pr(number: int, sha: str, closed_at: str | None = None) -> dict:
    return {"number": number, "title": f"PR {number}", "state": "closed" if closed_at else "open", "merged": bool(closed_at),
            "user": {"login": "ann"}, "head": {"sha": sha}, "created_at": "2026-03-02T09:20:00Z", "closed_at": closed_at,
            "merged_at": closed_at, "html_url": f"https://github.com/acme/x/pull/{number}"}


def commit(sha: str, date: str) -> dict:
    return {"sha": sha, "commit": {"committer": {"date": date}}}


def check(check_id: int, conclusion: str) -> dict:
    return {"id": check_id, "name": "test", "conclusion": conclusion, "app": {"slug": "github-actions"},
            "details_url": "", "started_at": "2026-03-02T09:30:00Z", "completed_at": "2026-03-02T09:31:00Z",
            "output": {"summary": "1 test failed"}}


# What the fake GitHub holds, by `gh api` path. Lists are returned as one page.
GITHUB = {
    "repos/acme/widgets/pulls?state=all&per_page=100&head=acme:fix%2Fparser": [pr(7, "b" * 40)],
    "repos/acme/widgets/pulls?state=all&per_page=100&head=acme:old%2Fidea": [pr(3, "0" * 40, closed_at="2025-01-01T00:00:00Z")],
    "repos/acme/gears/pulls?state=all&per_page=100&head=acme:feat%2Fteeth": [pr(7, "c" * 40)],
    "repos/acme/widgets/pulls/7": pr(7, "b" * 40),
    "repos/acme/gears/pulls/7": pr(7, "c" * 40, closed_at="2026-03-02T14:00:00Z"),
    "repos/acme/widgets/pulls/7/commits": [commit("a" * 40, "2026-03-02T09:15:00Z"), commit("b" * 40, "2026-03-02T11:12:00Z")],
    "repos/acme/gears/pulls/7/commits": [commit("c" * 40, "2026-03-02T13:11:00Z")],
    f"repos/acme/widgets/commits/{'a' * 40}/check-runs?filter=all": {"check_runs": [check(1, "failure")]},
    f"repos/acme/widgets/commits/{'b' * 40}/check-runs?filter=all": {"check_runs": [check(2, "failure")]},
    f"repos/acme/gears/commits/{'c' * 40}/check-runs?filter=all": {"check_runs": [check(3, "success")]},
    "repos/acme/widgets/pulls/7/comments": [{"id": 50, "user": {"login": "bob"}, "body": "rename this", "path": "a.py", "line": 3}],
    "repos/acme/gears/pulls/7/reviews": [{"id": 60, "user": {"login": "bob"}, "body": "", "state": "APPROVED"}],
}


@pytest.fixture
def fake_gh(monkeypatch):
    """Stand in for `gh` and `git`. Returns the list of `gh api` paths asked for."""
    asked = []

    def run(cmd, **kwargs):
        out = ""
        if cmd[0] == "git":  # git -C <cwd> remote get-url origin
            out = "git@github.com:acme/gears.git\n" if cmd[2] == "/work/gears" else ""
        elif cmd[:2] == ["gh", "api"]:
            assert all(c.startswith("-") or " " not in c for c in cmd[2:3]), cmd
            asked.append(cmd[2])
            if cmd[2] == "graphql":
                body = [{"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}]
            else:
                body = GITHUB.get(cmd[2], [])
                body = [body] if "--slurp" in cmd else body
            out = json.dumps(body)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(github, "requests", 0)
    github.remote_repo.cache_clear()
    return asked


def test_find_links_reads_pr_urls_and_pushed_branches(conn, tmp_path, fake_gh):
    load_sessions(conn, tmp_path)
    prs, pushes = github.find_links(conn)
    assert prs == {("one", "acme/widgets", 7)}  # the URL from `gh pr create`, not the one from `gh pr view`
    assert pushes == {("acme/widgets", "fix/parser"): {"one", "two"}, ("acme/widgets", "old/idea"): {"two"},
                      ("acme/gears", "feat/teeth"): {"three"}}  # no main; gears comes from the checkout's origin
    assert fake_gh == []


def test_local_sessions_fetch_every_repo_and_only_new_data_on_a_rerun(conn, tmp_path, fake_gh, capsys):
    load_sessions(conn, tmp_path)
    github.main([])
    links = [tuple(r) for r in conn.execute("SELECT session_id, repo, pr_number, source FROM session_prs ORDER BY 1, 2")]
    # old/idea's PR closed a year before session two began, so it is not two's.
    assert links == [("one", "acme/widgets", 7, "url"), ("three", "acme/gears", 7, "branch"), ("two", "acme/widgets", 7, "branch")]
    assert [tuple(r) for r in conn.execute("SELECT repo, pr_number, merged FROM gh_prs ORDER BY 1")] == [
        ("acme/gears", 7, 1), ("acme/widgets", 7, 0)]
    assert [tuple(r) for r in conn.execute("SELECT check_id, repo FROM gh_checks ORDER BY 1")] == [
        (1, "acme/widgets"), (2, "acme/widgets"), (3, "acme/gears")]
    assert [tuple(r) for r in conn.execute("SELECT comment_id, repo, pr_number FROM gh_review_comments ORDER BY 1")] == [
        (50, "acme/widgets", 7), (github.REVIEW_ID_OFFSET + 60, "acme/gears", 7)]
    # Both sessions are on widgets#7, but each gets only the commit made while it ran.
    assert [tuple(r) for r in conn.execute("SELECT session_id, substr(commit_sha, 1, 1) FROM session_commits ORDER BY 1")] == [
        ("one", "a"), ("three", "c"), ("two", "b")]
    first = len(fake_gh)
    assert f"{first} gh requests" in capsys.readouterr().out

    del fake_gh[:]
    github.main([])
    # Settled gears#7 is skipped, and so is every branch: each had a PR when looked up, after its sessions
    # ended. Open widgets#7 is refreshed, with its head's checks.
    assert not any("gears" in path or "head=" in path for path in fake_gh)
    assert f"repos/acme/widgets/commits/{'b' * 40}/check-runs?filter=all" in fake_gh
    assert f"repos/acme/widgets/commits/{'a' * 40}/check-runs?filter=all" not in fake_gh
    assert len(fake_gh) < first

    # A later session pushes the branch again: it is looked up again, and the session is linked.
    path = tmp_path / "claude" / "proj" / "four.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in shell_session(
        "four", "/work/widgets", 15, [("git push origin fix/parser", "To github.com:acme/widgets.git\n   def5678..fff9999  fix/parser -> fix/parser")])).replace("2026-03-02", "2099-01-01"))
    sources.sync(conn, tmp_path / "claude", tmp_path / "codex")
    del fake_gh[:]
    github.main([])
    assert [path for path in fake_gh if "head=" in path] == ["repos/acme/widgets/pulls?state=all&per_page=100&head=acme:fix%2Fparser"]
    assert conn.execute("SELECT source FROM session_prs WHERE session_id = 'four'").fetchone()[0] == "branch"


def test_dry_run_and_empty_database_make_no_requests(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: pytest.fail("a subprocess was run"))
    github.main([])
    assert capsys.readouterr().out.strip() == "github: skipped, no session pushed a branch or opened a pull request"
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/work/widgets')")
    conn.execute("INSERT INTO tool_calls VALUES ('s1', 1, 0, 'Bash', ?, ?, 0, '[]', 0, 0)", (
        json.dumps({"command": "gh pr create --fill"}), "https://github.com/acme/widgets/pull/7"))
    conn.commit()
    github.main(["--dry-run"])
    assert capsys.readouterr().out.strip() == (
        "github: dry run, no requests made. 1 PRs named in the logs of 1 sessions, 0 pushed branches to look up, 1 repos")


def test_export_keys_prs_by_repo(conn, tmp_path, fake_gh, monkeypatch):
    load_sessions(conn, tmp_path)
    github.main([])
    monkeypatch.setattr(export_site, "connect", db.connect)  # the scratch database, as patched by `conn`
    monkeypatch.setattr(export_site, "SITE_OUT", tmp_path / "site")
    monkeypatch.setattr(export_site.config, "projects", lambda: [r[0] for r in conn.execute("SELECT DISTINCT cwd FROM sessions")])
    export_site.main([])
    index = json.loads((tmp_path / "site" / "data" / "index.json").read_text())
    assert index["meta"]["has"]["github"] and index["meta"]["repo"] is None  # two repos: no single name
    assert sorted(index["prs"]) == ["acme/gears#7", "acme/widgets#7"]
    by_id = {s["id"]: s for s in index["sessions"]}
    assert by_id["three"]["prs"] == [{"n": "acme/gears#7", "state": "merged"}]
    assert (by_id["one"]["ci_failures"], by_id["two"]["ci_failures"], by_id["three"]["ci_failures"]) == (1, 1, 0)
    assert [r["pr"] for r in index["reviews"]] == ["acme/gears#7"]  # widgets#7 is still open


# --- sync -------------------------------------------------------------------------------

@pytest.fixture
def sync_stages(monkeypatch):
    """Run `debrief sync --dry-run` with every stage but github stubbed out; returns the github argvs."""
    ran = []
    stage = cli.stage
    monkeypatch.setattr(cli, "pick_projects", lambda dirs: True)  # as if a project were picked
    monkeypatch.setattr(cli, "stage", lambda name, argv: ran.append(argv) if name == "github" else None)
    return ran, stage


def test_sync_runs_github_after_ingest_and_dry_run_asks_for_no_requests(sync_stages):
    cli.sync(["--dry-run"])
    assert sync_stages[0] == [["--dry-run"]]


@pytest.mark.parametrize(("argv", "enabled", "line"), [
    (["--dry-run", "--no-github"], True, "github: skipped, --no-github"),
    (["--dry-run"], False, "github: skipped, [github] enabled = false in config.toml"),
])
def test_sync_skips_github_with_one_line(sync_stages, monkeypatch, capsys, argv, enabled, line):
    monkeypatch.setattr("debrief.config.github_enabled", lambda: enabled)
    cli.sync(argv)
    assert sync_stages[0] == [] and capsys.readouterr().out.strip() == line


def test_sync_goes_on_when_a_gh_call_fails(monkeypatch, capsys):
    def stage(name, argv):
        if name == "github":
            raise RuntimeError("gh api x failed: HTTP 502\nmore")
    monkeypatch.setattr(cli, "stage", stage)
    monkeypatch.setattr(cli, "pick_projects", lambda dirs: True)
    cli.sync(["--dry-run"])
    assert capsys.readouterr().out.strip() == "github: skipped, gh api x failed: HTTP 502"


# --- migration --------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE gh_prs (pr_number INTEGER PRIMARY KEY, title TEXT, state TEXT, merged INTEGER, author TEXT,
                     head_sha TEXT, created_at TEXT, url TEXT);
CREATE VIEW settled_prs AS SELECT * FROM gh_prs WHERE merged = 1 OR state = 'closed';
CREATE TABLE gh_commit_prs (commit_sha TEXT NOT NULL, pr_number INTEGER NOT NULL, PRIMARY KEY (commit_sha, pr_number));
CREATE TABLE gh_checks (check_id INTEGER PRIMARY KEY, commit_sha TEXT NOT NULL, name TEXT, conclusion TEXT, app TEXT,
                        run_id INTEGER, started_at TEXT, completed_at TEXT, failure_log TEXT);
CREATE TABLE gh_review_comments (comment_id INTEGER PRIMARY KEY, pr_number INTEGER NOT NULL, author TEXT, is_bot INTEGER,
                                 path TEXT, line INTEGER, body TEXT, in_reply_to INTEGER, created_at TEXT);
CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, commit_sha TEXT, message TEXT,
                          additions INTEGER, deletions INTEGER, files_changed INTEGER, ts TEXT);
INSERT INTO gh_prs VALUES (7, 'Fix build', 'closed', 1, 'ann', 'abc1234', '2026-01-05', 'https://github.com/acme/widgets/pull/7'),
                          (8, 'Draft', 'open', 0, 'ann', 'def5678', '2026-01-06', 'https://github.com/acme/widgets/pull/8');
INSERT INTO gh_commit_prs VALUES ('abc1234', 7), ('def5678', 8);
INSERT INTO gh_checks (check_id, commit_sha, name, conclusion) VALUES (1, 'abc1234', 'test', 'failure');
INSERT INTO gh_review_comments (comment_id, pr_number, body) VALUES (9, 7, 'nit');
INSERT INTO checkpoints (checkpoint_id, session_id, commit_sha) VALUES ('c1', 's1', 'abc1234');
"""


def test_old_database_gains_the_repo_and_keeps_its_rows(tmp_path):
    path = Path(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.close()
    for _ in range(2):  # the second connect finds nothing left to do
        conn = db.connect(path)
        assert [tuple(r) for r in conn.execute("SELECT repo, pr_number, title, merged_at FROM gh_prs ORDER BY 2")] == [
            ("acme/widgets", 7, "Fix build", None), ("acme/widgets", 8, "Draft", None)]
        assert [tuple(r) for r in conn.execute("SELECT * FROM gh_commit_prs ORDER BY 1")] == [
            ("abc1234", "acme/widgets", 7, None), ("def5678", "acme/widgets", 8, None)]
        assert [tuple(r) for r in conn.execute("SELECT check_id, repo FROM gh_checks")] == [(1, "acme/widgets")]
        assert [tuple(r) for r in conn.execute("SELECT comment_id, repo FROM gh_review_comments")] == [(9, "acme/widgets")]
        assert [tuple(r) for r in conn.execute("SELECT repo, pr_number FROM settled_prs")] == [("acme/widgets", 7)]
        assert [tuple(r) for r in conn.execute("SELECT * FROM session_pr_links")] == [("s1", "acme/widgets", 7)]
        # The key now holds the repo: the same number in another repo is a second row.
        conn.execute("INSERT INTO gh_prs (repo, pr_number) VALUES ('acme/gears', 7)")
        conn.rollback()
        conn.close()

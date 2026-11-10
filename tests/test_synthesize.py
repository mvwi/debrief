"""Checks for synthesize on a tiny synthetic database: skips without GitHub data, PR rules, gate jobs, projects."""

import json
import sqlite3

import pytest

from debrief import recommend, synthesize
from debrief.db import connect, session_projects


def add_session(conn, sid: str, commit: str | None = None, cwd: str | None = None) -> None:
    """A session with one corrected turn, labeled; with a checkpoint when commit is given."""
    conn.execute("INSERT INTO sessions (session_id, agent, last_activity_at, prompt, cwd) "
                 "VALUES (?, 'Claude Code', ?, ?, ?)", (sid, "2026-01-05T10:00:00Z", "add a parser", cwd))
    for idx, (role, kind, text) in enumerate([
            ("user", "human", "add a parser"), ("assistant", None, "Done, wrote src/parser.py."),
            ("user", "human", "no, it must reject empty input")]):
        conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, NULL)",
                     (sid, idx, role, kind, f"2026-01-05T10:0{idx}:00Z", text))
    conn.execute("INSERT INTO tool_calls (session_id, msg_idx, call_idx, name, is_error, files, writes) "
                 "VALUES (?, 1, 0, 'Edit', 0, '[\"src/parser.py\"]', 1)", (sid,))
    for field, value in (("turn_kind", "correction"), ("correction_type", "wrong_behavior")):
        conn.execute("INSERT INTO turn_labels (session_id, msg_idx, field, value, confidence) VALUES (?, 2, ?, ?, 0.9)",
                     (sid, field, value))
    for field, value in (("prompt_style", "terse"), ("outcome", "shipped_or_pr_ready")):
        conn.execute("INSERT INTO session_labels (session_id, field, value, confidence) VALUES (?, ?, ?, 0.9)",
                     (sid, field, value))
    if commit:
        conn.execute("INSERT INTO checkpoints (checkpoint_id, session_id, commit_sha) VALUES (?, ?, ?)",
                     (f"cp-{sid}", sid, commit))


def add_pr(conn, number: int, commit: str, merged: bool) -> None:
    conn.execute("INSERT INTO gh_prs (repo, pr_number, state, merged) VALUES ('acme/widgets', ?, ?, ?)",
                 (number, "closed" if merged else "open", int(merged)))
    conn.execute("INSERT INTO gh_commit_prs (commit_sha, repo, pr_number) VALUES (?, 'acme/widgets', ?)", (commit, number))


def add_check(conn, check_id: int, name: str, conclusion: str, log: str | None = None, app: str = "github-actions"):
    conn.execute("INSERT INTO gh_checks (check_id, commit_sha, name, conclusion, app, completed_at, failure_log) "
                 "VALUES (?, 'c1', ?, ?, ?, '2026-01-05T11:00:00Z', ?)", (check_id, name, conclusion, app, log))


PICKED = ["/w/local", "/w/big"]  # /w/small and sessions with no folder are not picked


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesize, "BUNDLE_DIR", tmp_path / "bundles")
    monkeypatch.setattr(synthesize, "INDEX_PATH", tmp_path / "bundles" / "index.json")
    monkeypatch.setattr(synthesize, "projects", lambda: PICKED)
    monkeypatch.setattr(recommend, "projects", lambda: PICKED)
    conn = connect(tmp_path / "debrief.db")
    add_session(conn, "local", cwd="/w/local")
    conn.commit()
    return conn


def test_local_database_skips_github_kinds(conn, tmp_path, capsys):
    synthesize.main(["--dry-run", "--db", str(tmp_path / "debrief.db")])
    out = capsys.readouterr().out
    assert "ci                   skipped: no CI checks in the database" in out
    assert "review               skipped: no PR review comments in the database" in out
    assert "found_not_filed" in out and "skipped: no evidence, nothing to send" in out
    written = {p.stem for p in (tmp_path / "bundles").glob("*/*/*.json")}
    assert "correction_pattern" in written and not written & {"ci", "review"}


def test_only_skipped_kind_exits_cleanly(conn, tmp_path, capsys):
    synthesize.main(["--dry-run", "--kind", "ci", "--db", str(tmp_path / "debrief.db")])
    assert "skipped" in capsys.readouterr().out


def test_missing_data(conn):
    assert synthesize.missing_data(conn, "ci") and synthesize.missing_data(conn, "review")
    assert synthesize.missing_data(conn, "hotspot") is None
    add_check(conn, 1, "lint", "failure", "boom")
    assert synthesize.missing_data(conn, "ci") is None


def test_local_bundles_have_evidence_and_no_ci_section(conn):
    synthesize.mark_relayed(conn)
    hotspot = synthesize.hotspot(conn, 0.7, 40, "")
    assert hotspot["files"][0]["path"] == "src/parser.py" and "ci_failures_by_check" not in hotspot
    assert synthesize.correction_pattern(conn, 0.7, 40, "")["totals"]["items"] == 1


def test_prompt_practice_keeps_sessions_without_pr(conn):
    add_session(conn, "merged", commit="c1")
    add_pr(conn, 1, "c1", merged=True)
    add_session(conn, "open", commit="c2")
    add_pr(conn, 2, "c2", merged=False)
    synthesize.mark_relayed(conn)
    bundle = synthesize.prompt_practice(conn, 0.7, 40, "")
    assert {s["session_id"]: s["pr"] for s in bundle["per_session"]} == {"local": "none", "merged": "merged_or_closed"}
    assert bundle["totals"]["excluded_unsettled"] == 1 and bundle["totals"]["sessions_without_pr"] == 1


def test_ci_keeps_a_real_test_job_and_drops_gate_jobs(conn):
    add_pr(conn, 1, "c1", merged=True)
    add_check(conn, 1, "test", "failure", "--- FAIL: TestParser (0.01s)")
    add_check(conn, 2, "all-green", "failure", '[ "failure" = "success" ]\n##[error]Process completed with exit code 1.')
    add_check(conn, 3, "all-green", "success")
    add_check(conn, 4, "Entire Gates", "failure", "not approved", app="entire")
    bundle = synthesize.ci(conn, 0.7, 40, "")
    assert bundle["totals"]["failures"] == 1 and bundle["totals"]["checks"] == 1
    assert bundle["by_failure_class"]["unlabeled"]["checks"] == {"test": 1}


# --- projects ---------------------------------------------------------------------------

def add_repo_data(conn, repo: str, sid: str, n: int) -> None:
    """A merged PR the session opened, with one failed check and one review comment."""
    conn.execute("INSERT INTO session_prs VALUES (?, ?, ?, 'url')", (sid, repo, n))
    conn.execute("INSERT INTO gh_prs (repo, pr_number, state, merged) VALUES (?, ?, 'closed', 1)", (repo, n))
    conn.execute("INSERT INTO gh_commit_prs VALUES (?, ?, ?, '2026-01-05T10:00:00Z')", (f"c{n}", repo, n))
    conn.execute("INSERT INTO gh_checks (check_id, commit_sha, name, conclusion, app, completed_at, failure_log, repo) "
                 "VALUES (?, ?, 'test', 'failure', 'github-actions', '2026-01-05T11:00:00Z', 'boom', ?)", (n, f"c{n}", repo))
    conn.execute("INSERT INTO gh_review_comments (comment_id, repo, pr_number, author, is_bot, body, kind) "
                 "VALUES (?, ?, ?, 'bot', 1, 'nit', 'inline')", (n, repo, n))


def two_projects(conn, only: str | None = None) -> None:
    """Two sessions in /w/big with a PR, one in /w/small with a PR; or just the project `only`."""
    for sid, cwd, repo, n in (("b1", "/w/big", "acme/big", 1), ("b2", "/w/big", None, 0), ("s1", "/w/small", "acme/small", 2)):
        if only in (None, cwd):
            add_session(conn, sid, cwd=cwd)
            if repo:
                add_repo_data(conn, repo, sid, n)
    for field in ("found_not_filed", "agent_could_have_found_it"):
        conn.execute("INSERT OR IGNORE INTO turn_labels (session_id, msg_idx, field, value, confidence) "
                     "SELECT session_id, 2, ?, 'true', 0.9 FROM sessions", (field,))
    conn.commit()


def test_one_pass_per_picked_project_then_one_across_them(conn):
    two_projects(conn)  # /w/big has 4 human turns, the fixture's /w/local 2
    add_session(conn, "nowhere")  # no folder: cannot be picked
    big, local, cross = synthesize.passes(conn, [*PICKED, "/w/no-sessions"])
    assert (big["project"], big["sessions"], big["repos"]) == ("/w/big", {"b1", "b2"}, {"acme/big"})
    assert (local["project"], local["sessions"], local["repos"]) == ("/w/local", {"local"}, set())
    assert "prompt_practice" not in big["kinds"] + local["kinds"]
    # The pass about the person reads picked projects only: not /w/small, not the session with no folder.
    assert (cross["project"], cross["kinds"]) == (None, ["prompt_practice"])
    assert (cross["sessions"], cross["repos"]) == ({"b1", "b2", "local"}, {"acme/big"})
    assert synthesize.passes(conn, []) == [] and synthesize.passes(conn, ["/w/no-sessions"]) == []


def test_nothing_picked_makes_no_run(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(synthesize, "projects", list)
    synthesize.main(["--dry-run", "--db", str(tmp_path / "debrief.db")])
    assert "no picked project has sessions" in capsys.readouterr().out
    assert not (tmp_path / "bundles").exists()


def test_dry_run_builds_bundles_per_picked_project(conn, tmp_path, capsys):
    two_projects(conn)
    synthesize.main(["--dry-run", "--db", str(tmp_path / "debrief.db")])
    out = capsys.readouterr().out
    assert "== big: 2 sessions, 4 human turns" in out and "== local: 1 sessions" in out and "small" not in out
    written = sorted(str(p.relative_to(p.parents[1])) for p in (tmp_path / "bundles").glob("*/*/*.json"))
    assert "00-big/review.json" in written and "01-local/hotspot.json" in written
    assert "02-all-projects/prompt_practice.json" in written and "01-local/review.json" not in written
    bundle = json.loads(next((tmp_path / "bundles").glob("*/02-all-projects/prompt_practice.json")).read_text())
    assert {x["session_id"] for x in bundle["bundle"]["per_session"]} == {"b1", "b2", "local"}


def test_scoped_rollup_equals_rollup_on_that_project_alone(conn, tmp_path):
    """The TEMP views must give what a database holding only that project's rows gives."""
    two_projects(conn)
    alone = connect(tmp_path / "alone.db")
    two_projects(alone, only="/w/big")
    for c in (conn, alone):
        synthesize.mark_relayed(c)
    synthesize.scope(conn, {"b1", "b2"}, {"acme/big"})
    for kind, rollup in synthesize.ROLLUPS.items():
        scoped = rollup(conn, 0.7, 40, "")
        assert scoped == rollup(alone, 0.7, 40, ""), kind
        assert synthesize.bundle_refs(scoped), kind  # the comparison is not of two empty bundles
    synthesize.scope(conn)
    assert synthesize.ci(conn, 0.7, 40, "")["totals"]["failures"] == 2  # the limit is lifted


def test_session_with_a_pr_joins_that_repos_project(conn):
    two_projects(conn)
    add_session(conn, "wt", cwd="/w/worktrees/robin")  # a worktree ingest did not resolve
    conn.execute("INSERT INTO session_prs VALUES ('wt', 'acme/big', 1, 'branch')")
    add_session(conn, "wt2", cwd="/w/worktrees/kudu")  # no PR: nothing says which repo it is
    conn.execute("INSERT INTO session_prs VALUES ('s1', 'acme/big', 1, 'branch')")  # its own folder is a repo's project
    projects = session_projects(conn)
    assert projects["wt"] == "/w/big" and projects["wt2"] == "/w/worktrees/kudu"
    assert projects["s1"] == "/w/small" and projects["local"] == "/w/local"


def test_ref_project(conn):
    two_projects(conn)
    projects, repos = session_projects(conn), {"acme/big": "/w/big", "acme/small": "/w/small"}
    assert synthesize.ref_project(conn, projects, repos, {"session_id": "s1", "msg_idx": 2}) == "/w/small"
    assert synthesize.ref_project(conn, projects, repos, {"event_type": "review_comment", "event_id": 2}) == "/w/small"
    assert synthesize.ref_project(conn, projects, repos, {"event_type": "check_failure", "event_id": 1}) == "/w/big"
    assert synthesize.ref_project(conn, projects, repos, {"event_type": "check_failure", "event_id": 99}) is None


def test_findings_keep_their_project_and_cache_per_pass(conn):
    ids = synthesize.save_findings(conn, "r1", "hotspot", [
        {"title": "a", "evidence": [], "project": "/w/big"}, {"title": "b", "evidence": []}], "now")
    assert [r["project"] for r in conn.execute("SELECT project FROM findings ORDER BY finding_id")] == ["/w/big", None]
    index = {"h": {"run_id": "r1", "kind": "hotspot", "pass": "/w/big", "findings": 2, "ids": ids},
             "old": {"run_id": "r1", "kind": "hotspot", "findings": 2}}  # written before passes existed
    assert synthesize.cached_run(conn, index, "h", "hotspot", "/w/big")["ids"] == ids
    assert synthesize.cached_run(conn, index, "h", "hotspot", "/w/small") is None
    assert synthesize.cached_run(conn, index, "old", "hotspot", "/w/big") is None
    assert synthesize.cached_run(conn, index, "old", "hotspot", synthesize.CROSS)["ids"] == ids


def test_recommend_calls_once_per_project_and_once_across(conn):
    two_projects(conn)
    synthesize.save_findings(conn, "r1", "hotspot", [
        {"title": "big", "evidence": [], "project": "/w/big"}, {"title": "local", "evidence": [], "project": "/w/local"},
        {"title": "habit", "evidence": []}, {"title": "unpicked", "evidence": [], "project": "/w/small"}], "now")
    groups = recommend.groups(recommend.load_findings(conn, "r1"), PICKED)
    assert {p: [f["title"] for f in fs] for p, fs in groups.items()} == {
        "/w/big": ["big"], "/w/local": ["local"], None: ["habit"]}
    own = json.loads(recommend.prompt(conn, "/w/big", groups["/w/big"]))
    assert own["project"] == "big" and own["team"]["sessions"] == 2 and "/w/" not in json.dumps(own)
    across = json.loads(recommend.prompt(conn, None, groups[None]))
    assert "project" not in across and across["team"]["sessions"] == 3  # picked projects only, not /w/small
    recs = [r for p, fs in groups.items()
            for r in recommend.check([{"lever": "backlog", "title": "x", "finding_ids": [fs[0]["id"]]}], fs)]
    assert [r["project"] for r in recs] == ["/w/big", "/w/local", None]
    recommend.save(conn, "r1", recs)
    assert [tuple(r) for r in conn.execute("SELECT priority, project FROM recommendations")] == [
        (1, "/w/big"), (2, "/w/local"), (3, None)]


def test_old_database_gains_project_columns_and_keeps_its_rows(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE findings (finding_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, "
                "title TEXT, detail TEXT, suggested_fix TEXT, evidence TEXT, created_at TEXT)")
    old.execute("CREATE TABLE recommendations (rec_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, priority INTEGER, "
                "lever TEXT, title TEXT, problem TEXT, change TEXT, target TEXT, effort TEXT, impact TEXT, "
                "finding_ids TEXT, created_at TEXT)")
    old.execute("INSERT INTO findings (run_id, kind, title) VALUES ('r0', 'ci', 'kept')")
    old.execute("INSERT INTO recommendations (run_id, title) VALUES ('r0', 'kept too')")
    old.commit()
    old.close()
    conn = connect(tmp_path / "old.db")
    assert tuple(conn.execute("SELECT title, project FROM findings").fetchone()) == ("kept", None)
    assert tuple(conn.execute("SELECT title, project FROM recommendations").fetchone()) == ("kept too", None)
    assert recommend.load_findings(conn, "r0")[0]["project"] is None

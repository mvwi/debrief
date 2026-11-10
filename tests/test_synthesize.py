"""Checks for synthesize on a tiny synthetic database: skips without GitHub data, PR rules, gate jobs."""

import pytest

from debrief import synthesize
from debrief.db import connect


def add_session(conn, sid: str, commit: str | None = None) -> None:
    """A session with one corrected turn, labeled; with a checkpoint when commit is given."""
    conn.execute("INSERT INTO sessions (session_id, agent, last_activity_at, prompt) VALUES (?, 'Claude Code', ?, ?)",
                 (sid, "2026-01-05T10:00:00Z", "add a parser"))
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
    conn.execute("INSERT INTO gh_prs (pr_number, state, merged) VALUES (?, ?, ?)",
                 (number, "closed" if merged else "open", int(merged)))
    conn.execute("INSERT INTO gh_commit_prs VALUES (?, ?)", (commit, number))


def add_check(conn, check_id: int, name: str, conclusion: str, log: str | None = None, app: str = "github-actions"):
    conn.execute("INSERT INTO gh_checks (check_id, commit_sha, name, conclusion, app, completed_at, failure_log) "
                 "VALUES (?, 'c1', ?, ?, ?, '2026-01-05T11:00:00Z', ?)", (check_id, name, conclusion, app, log))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(synthesize, "BUNDLE_DIR", tmp_path / "bundles")
    monkeypatch.setattr(synthesize, "INDEX_PATH", tmp_path / "bundles" / "index.json")
    conn = connect(tmp_path / "debrief.db")
    add_session(conn, "local")
    conn.commit()
    return conn


def test_local_database_skips_github_kinds(conn, tmp_path, capsys):
    synthesize.main(["--dry-run", "--db", str(tmp_path / "debrief.db")])
    out = capsys.readouterr().out
    assert "ci                   skipped: no CI checks in the database" in out
    assert "review               skipped: no PR review comments in the database" in out
    assert "found_not_filed" in out and "skipped: no evidence, nothing to send" in out
    written = {p.stem for p in (tmp_path / "bundles").glob("*/*.json")}
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

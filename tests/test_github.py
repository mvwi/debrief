"""The GitHub loader's guards: what goes into `gh api` paths, and skipping cleanly. No network."""

import pytest

from debrief import db, github


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

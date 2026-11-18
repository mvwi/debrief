"""`debrief login`, `logout` and a real `push`, against a made-up server that follows docs/server-api.md."""

import hashlib
import json
import os
import stat
import subprocess
import sys

import httpx
import pytest
from test_push import ALPHA, BETA, TOKEN, fill

from debrief import account, config, db, push
from debrief.paths import SERVER_ENV

URL = "https://debrief.example"
SECRET = "dbf_" + "t0ken" * 6  # made up
RIGHTS = "X-Debrief-Rights-Confirmed"


def refuse(status: int, code: str, headers=None, **extra) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={"error": {"code": code, "message": f"about {code}", **extra}})


class Server:
    """Sign-in and upload as the contract has them. `script` holds answers to give before the real
    one, by "METHOD path"; an exception there is raised, as a dropped connection would be."""

    def __init__(self):
        self.calls, self.script, self.pending = [], {}, 1
        self.files, self.uploads, self.snapshot, self.confirmed, self.made_uploads = {}, {}, None, False, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and request.url.host == "debrief.example"
        what = f"{request.method} {request.url.path}"
        self.calls.append((what, request))
        if self.script.get(what):
            answer = self.script[what].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if what == "POST /v1/auth/device":
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"device_code": "dev1", "user_code": "WDJB-MJHT", "interval": 5,
                                             "verification_uri": "https://github.com/login/device", "expires_in": 900})
        if what == "POST /v1/auth/token":
            assert json.loads(request.content) == {"device_code": "dev1"}
            self.pending -= 1
            if self.pending >= 0:
                return httpx.Response(202, json={"status": "pending", "interval": 7})
            return httpx.Response(200, json={"token": SECRET, "expires_at": "2027-02-15T09:30:00Z",
                                             "user": {"id": "u_1", "github_id": 583231, "login": "octocat"}})
        if request.headers.get("Authorization") != f"Bearer {SECRET}":
            return refuse(401, "invalid_token")
        if what == "DELETE /v1/auth/token":
            return httpx.Response(204)
        if what == "POST /v1/uploads":
            if not (self.confirmed or request.headers.get(RIGHTS) == "1"):
                return refuse(403, "confirmation_required")
            self.confirmed = True
            manifest = json.loads(request.content)
            if manifest["objects"] and not manifest["projects"]:
                return refuse(422, "invalid_manifest")
            assert all(len(p["analysed_at"] or "") <= 40 for p in manifest["projects"])
            self.made_uploads += 1
            up = f"up_{self.made_uploads}"
            self.uploads = {up: manifest}  # a new upload cancels the one before
            return httpx.Response(201, json={"upload_id": up, "expires_at": "2026-11-17T10:30:00Z", "missing": [
                path for path, o in manifest["objects"].items() if o["sha256"] not in self.files]})
        _, _, _, up, kind, *path = request.url.path.split("/")
        if up not in self.uploads:
            return refuse(404, "not_found")
        objects = self.uploads[up]["objects"]
        if kind == "files":
            o = objects["/".join(path)]
            assert int(request.headers["Content-Length"]) == o["bytes"] == len(request.content)
            assert hashlib.sha256(request.content).hexdigest() == o["sha256"]
            self.files[o["sha256"]] = request.content
            return httpx.Response(204)
        assert (kind, path, request.content) == ("commit", [], b"")
        missing = [path for path, o in objects.items() if o["sha256"] not in self.files]
        if missing:
            return refuse(409, "incomplete", missing=missing)
        self.snapshot = self.uploads.pop(up)
        size = sum(o["bytes"] for o in objects.values())
        return httpx.Response(200, json={"machine": self.snapshot["machine"], "committed_at": "2026-11-17T09:41:07Z",
                                         "objects": len(objects), "bytes": size, "removed": 0,
                                         "projects": self.snapshot["projects"],
                                         "usage": {"bytes": size, "quota_bytes": 500_000_000}})

    def made(self, prefix: str) -> list[str]:
        return [what for what, _ in self.calls if what.startswith(prefix)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch DEBRIEF_HOME with the server's address set, a fake server, and no real waiting."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(account, "HOME", tmp_path)
    monkeypatch.setattr(account, "CREDENTIALS", tmp_path / "credentials")
    monkeypatch.setattr(push, "MACHINE_PATH", tmp_path / "machine_id")
    monkeypatch.setattr(push, "CONFIRMED_PATH", tmp_path / "share_confirmed")
    monkeypatch.setattr(push, "DEFAULT_OUT", tmp_path / "push")
    monkeypatch.delenv(SERVER_ENV, raising=False)
    config.save({"server": {"url": URL}, "projects": {"include": [ALPHA, BETA]}})
    server, naps = Server(), []
    monkeypatch.setattr(account, "TRANSPORT", httpx.MockTransport(server))
    monkeypatch.setattr(account.time, "sleep", naps.append)
    server.naps, server.home = naps, tmp_path
    return server


@pytest.fixture
def signed_in(home, monkeypatch):
    """Signed in, with the made-up database of test_push.py."""
    account.save({"server": URL, "token": SECRET, "login": "octocat", "expires_at": "2027-02-15T09:30:00Z"})
    conn = db.connect(home.home / "debrief.db")
    with conn:
        fill(conn)
    monkeypatch.setattr(push, "connect", lambda: conn)
    return home


def test_login_shows_the_code_polls_at_the_servers_pace_and_keeps_the_token_private(home, capsys):
    account.login([])
    out = capsys.readouterr()
    assert "https://github.com/login/device" in out.out and "WDJB-MJHT" in out.out and "octocat" in out.out
    assert SECRET not in out.out + out.err and "dev1" not in out.out + out.err
    assert home.naps == [5, 7]  # the interval from the start, then the one the pending answer gave
    assert stat.S_IMODE(account.CREDENTIALS.stat().st_mode) == 0o600
    assert account.credentials() == {"server": URL, "token": SECRET, "login": "octocat", "expires_at": "2027-02-15T09:30:00Z"}


def test_login_tightens_a_credentials_file_that_was_readable_by_others(home):
    account.CREDENTIALS.write_text("{}")
    account.CREDENTIALS.chmod(0o644)
    account.login([])
    assert stat.S_IMODE(account.CREDENTIALS.stat().st_mode) == 0o600 and account.credentials()["token"] == SECRET


@pytest.mark.parametrize("route, answer, says", [
    ("POST /v1/auth/device", refuse(502, "github_unavailable"), "GitHub did not answer"),
    ("POST /v1/auth/device", httpx.ConnectError("refused"), "cannot reach https://debrief.example"),
    ("POST /v1/auth/device", httpx.Response(200, json={"user_code": "X"}), "does not understand"),
    ("POST /v1/auth/token", refuse(400, "invalid_device_code"), "no longer knows this sign-in"),
    ("POST /v1/auth/token", refuse(403, "access_denied"), "refused the sign-in at GitHub"),
    ("POST /v1/auth/token", refuse(410, "expired_device_code"), "code ran out"),
    ("POST /v1/auth/token", refuse(500, "internal"), "the server refused (500 internal: about internal)"),
    ("POST /v1/auth/token", httpx.Response(502, text="<html>bad gateway"), "the server refused (502 : no reason given)"),
])
def test_login_failures_say_why_and_save_nothing(home, route, answer, says):
    home.script[route] = [answer]
    with pytest.raises(SystemExit) as e:
        account.login([])
    assert says in str(e.value) and str(e.value).startswith("debrief: ") and not account.CREDENTIALS.exists()


def test_login_waits_out_a_rate_limit_and_a_github_outage(home):
    home.script["POST /v1/auth/device"] = [refuse(429, "rate_limited", headers={"Retry-After": "42"})]
    home.script["POST /v1/auth/token"] = [refuse(502, "github_unavailable"), refuse(429, "rate_limited")]
    account.login([])
    # 42 as the header says; 5 before poll 1 (GitHub is down); 5 before poll 2, which is told to
    # wait with no header (5) and is then pending; 7 as that answer says, and the sign-in is done.
    assert home.naps == [42, 5, 5, 5, 7] and account.credentials()["token"] == SECRET


def test_login_gives_up_when_the_server_stays_rate_limited(home):
    home.script["POST /v1/auth/device"] = [refuse(429, "rate_limited", headers={"Retry-After": "1"})] * account.TRIES
    with pytest.raises(SystemExit, match="taking no more requests"):
        account.login([])
    assert len(home.made("POST /v1/auth/device")) == account.TRIES


def test_login_stops_polling_when_the_code_has_run_out(home):
    home.pending = 10_000
    with pytest.raises(SystemExit, match="code ran out"):
        account.login([])
    assert sum(home.naps) >= 900 and len(home.naps) < 200 and not account.CREDENTIALS.exists()


def test_without_a_server_login_and_push_say_how_to_set_one(home, monkeypatch, capsys):
    config.save({"projects": {"include": [ALPHA]}})
    for command in (account.login, push.main):
        with pytest.raises(SystemExit) as e:
            command([])
        assert "no server is set" in str(e.value) and "[server]" in str(e.value) and SERVER_ENV in str(e.value)
    assert home.calls == [] and not push.MACHINE_PATH.exists()
    monkeypatch.setenv(SERVER_ENV, "http://debrief.example")  # not HTTPS, and not this machine
    with pytest.raises(SystemExit, match="DEBRIEF_SERVER must be an https://"):
        account.login([])
    assert home.calls == []


def test_server_address_comes_from_the_variable_before_the_file(home, monkeypatch):
    assert config.server() == URL and config.load()["server"] == {"url": URL}
    monkeypatch.setenv(SERVER_ENV, "http://localhost:8787/")
    assert config.server() == "http://localhost:8787"
    for bad in ("http://debrief.example", 'https://x"\nurl = "y', "ftp://x", 5):
        with pytest.raises(ValueError, match="server"):
            config.save({"server": {"url": bad}})


def test_logout_revokes_the_token_and_removes_the_file(signed_in, capsys):
    account.logout([])
    (what, request), = signed_in.calls
    assert what == "DELETE /v1/auth/token" and request.headers["Authorization"] == f"Bearer {SECRET}"
    assert not account.CREDENTIALS.exists() and "revoked" in capsys.readouterr().out
    account.logout([])
    assert "Not signed in" in capsys.readouterr().out and len(signed_in.calls) == 1


@pytest.mark.parametrize("answer, says", [
    (httpx.ConnectError("refused"), "The server was not told (refused): the token still works there"),
    (refuse(500, "internal"), "The server was not told (500 internal"),
    (refuse(401, "invalid_token"), "already dropped"),
])
def test_logout_removes_the_file_even_when_the_server_call_fails(signed_in, capsys, answer, says):
    signed_in.script["DELETE /v1/auth/token"] = [answer]
    account.logout([])
    out = capsys.readouterr().out
    assert not account.CREDENTIALS.exists() and says in out and SECRET not in out


def test_logout_asks_the_server_that_gave_the_token_even_with_none_set(signed_in, monkeypatch):
    config.save({})
    account.logout([])
    assert signed_in.made("DELETE") and not account.CREDENTIALS.exists()


def test_push_needs_a_sign_in_to_this_server(home, monkeypatch):
    with pytest.raises(SystemExit, match="not signed in to https://debrief.example. Run `debrief login`"):
        push.main(["--yes"])
    # A token from another server is not sent to this one.
    account.save({"server": "https://other.example", "token": SECRET})
    with pytest.raises(SystemExit, match="not signed in"):
        push.main(["--yes"])
    assert home.calls == [] and not push.MACHINE_PATH.exists()


def test_push_sends_byte_for_byte_what_a_dry_run_writes(signed_in, capsys):
    out = signed_in.home / "dry"
    push.main(["--dry-run", "--out", str(out)])
    assert signed_in.calls == []
    push.main(["--yes"])
    written = {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*.json")}
    manifest = json.loads(written.pop("manifest.json"))
    puts = {what.removeprefix("PUT /v1/uploads/up_1/files/"): request.content for what, request in signed_in.calls
            if request.method == "PUT"}
    assert puts == written and len(puts) == 5
    assert signed_in.snapshot == manifest
    # Nothing else left the machine: one create, the five objects, one commit, each with the token.
    assert [what.split("/")[-1] for what, _ in signed_in.calls if "files" not in what] == ["uploads", "commit"]
    assert len(signed_in.calls) == 7 and all(r.headers["Authorization"] == f"Bearer {SECRET}" for _, r in signed_in.calls)
    sent = b"".join(request.content for _, request in signed_in.calls)
    assert TOKEN.encode() not in sent and b"acme" not in sent and b"/home/me" not in sent

    # The manifest says which project each session belongs to, by the key the session object holds.
    for path, o in manifest["objects"].items():
        assert ("project" in o) == path.startswith("sessions/")
        if "project" in o:
            assert o["project"] == json.loads(written[path])["project"] in {p["key"] for p in manifest["projects"]}

    said = capsys.readouterr().out
    assert "About to push to https://debrief.example as octocat" in said and SECRET not in said
    assert "Pushed: sent 5 objects; the server now holds 5 (" in said and "alpha  (octo/alpha; 2 sessions, 3 turns)" in said
    assert f"{sum(len(b) for b in written.values()):,} of 500,000,000 bytes used." in said

    # A second push sends the manifest again and no object: the server holds them all.
    push.main([])
    assert len(signed_in.made("PUT")) == 5 and len(signed_in.made("POST /v1/uploads/up_2/commit")) == 1
    assert "Pushed: sent 0 objects" in capsys.readouterr().out


def test_first_push_asks_once_for_the_right_to_share(signed_in, monkeypatch, capsys):
    answers = iter(["no", "", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    for _ in range(2):
        with pytest.raises(SystemExit, match="nothing was sent"):
            push.main([])
    assert signed_in.calls == [] and not push.CONFIRMED_PATH.exists()
    assert "Upload only what you have the right to share" in capsys.readouterr().out

    push.main([])
    create = signed_in.calls[0][1]
    assert create.headers[RIGHTS] == "1" and push.CONFIRMED_PATH.exists() and signed_in.snapshot

    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked twice"))
    capsys.readouterr()
    push.main([])
    assert "right to share" not in capsys.readouterr().out
    assert all(r.headers[RIGHTS] == "1" for what, r in signed_in.calls if what == "POST /v1/uploads")


def test_a_server_that_forgot_the_confirmation_is_told_again_without_asking(signed_in, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked twice"))
    push.main(["--yes"])
    signed_in.confirmed = False  # the account was deleted and made again: the server asks for the header once more
    push.main([])
    assert len(signed_in.made("POST /v1/uploads/up_2/commit")) == 1


def test_push_with_nothing_picked_sends_no_projects_and_no_objects(signed_in):
    push.main(["--yes"])
    assert signed_in.snapshot["objects"]
    # A picked folder no session ran in is no project either; the findings and PR rows go with the sessions.
    for picked in ([], ["/home/me/empty"]):
        config.save({"server": {"url": URL}, "projects": {"include": picked}})
        push.main([])
        assert (signed_in.snapshot["projects"], signed_in.snapshot["objects"]) == ([], {})
    assert len(signed_in.made("PUT")) == 5 and len(signed_in.made("POST /v1/uploads/")) == 3  # commits


def test_push_with_no_one_to_ask_sends_nothing_unless_told_yes(signed_in, monkeypatch):
    def nobody(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", nobody)
    with pytest.raises(SystemExit, match="pass --yes"):
        push.main([])
    assert signed_in.calls == []
    push.main(["--yes"])
    assert signed_in.snapshot and push.CONFIRMED_PATH.exists()


def test_push_resumes_after_a_failed_put_and_sends_only_what_is_still_missing(signed_in, capsys):
    second = "PUT /v1/uploads/up_1/files/sessions/b1.json"
    # sessions/a1.json arrives, then the connection drops.
    signed_in.script[second] = [httpx.ReadError("connection lost")]
    push.main(["--yes"])
    order = [what for what, _ in signed_in.calls]
    assert order.index(second) == 2 and order[3] == "POST /v1/uploads"
    arrived = order[1].rsplit("/files/", 1)[1]
    again = [what.rsplit("/files/", 1)[1] for what in order[4:-1]]
    assert order[-1] == "POST /v1/uploads/up_2/commit" and arrived not in again and len(again) == 4
    assert len(signed_in.snapshot["objects"]) == 5 == len(signed_in.files)
    assert "starting the upload again" in capsys.readouterr().out


def test_push_starts_again_when_the_answer_to_a_commit_is_lost(signed_in):
    real = Server.__call__

    def commit_lands_but_the_answer_is_lost(self, request):
        answer = real(self, request)
        if request.url.path == "/v1/uploads/up_1/commit":
            raise httpx.ReadTimeout("no answer")
        return answer

    signed_in.__class__ = type("Lossy", (Server,), {"__call__": commit_lands_but_the_answer_is_lost})
    push.main(["--yes"])
    # The second create finds nothing missing, and the second commit changes nothing.
    assert [what for what, _ in signed_in.calls][-2:] == ["POST /v1/uploads", "POST /v1/uploads/up_2/commit"]
    assert len(signed_in.made("PUT")) == 5 and len(signed_in.snapshot["objects"]) == 5


def test_push_sends_what_an_incomplete_commit_names_and_commits_again(signed_in):
    signed_in.script["POST /v1/uploads/up_1/commit"] = [refuse(409, "incomplete", missing=["findings.json"])]
    push.main(["--yes"])
    order = [what for what, _ in signed_in.calls]
    assert order[-3:] == ["POST /v1/uploads/up_1/commit", "PUT /v1/uploads/up_1/files/findings.json",
                          "POST /v1/uploads/up_1/commit"]
    assert len(signed_in.made("POST /v1/uploads")) == 3 and signed_in.snapshot  # one create, two commits


@pytest.mark.parametrize("route, answers, says", [
    ("POST /v1/uploads", [refuse(401, "invalid_token")], "Run `debrief login`"),
    ("POST /v1/uploads", [refuse(413, "quota_exceeded", quota_bytes=500, used_bytes=400, needed_bytes=200)],
     "needs 200 bytes and your other machines hold 400; the server keeps at most 500"),
    ("POST /v1/uploads/up_1/commit", [refuse(413, "quota_exceeded", quota_bytes=500, used_bytes=400, needed_bytes=200)],
     "the server keeps at most 500"),
    ("POST /v1/uploads", [refuse(422, "too_many_machines", machines=["m"])], "the server refused (422 too_many_machines"),
    ("POST /v1/uploads", [refuse(403, "confirmation_required")], "the server refused (403 confirmation_required"),
    ("POST /v1/uploads", [refuse(429, "rate_limited", headers={"Retry-After": "3"})] * 5, "taking no more requests"),
    ("POST /v1/uploads", [refuse(500, "internal")] * 3, "the server refused (500 internal"),
    ("POST /v1/uploads", [httpx.ConnectError("refused")] * 3, "cannot reach https://debrief.example (refused)"),
    ("POST /v1/uploads", [httpx.Response(201, json={"upload_id": "up_9", "missing": ["sessions/../../etc/passwd"]})],
     "does not understand"),
    ("PUT /v1/uploads/up_1/files/findings.json", [refuse(422, "hash_mismatch")], "the server refused (422 hash_mismatch"),
])
def test_push_failures_say_why_and_commit_nothing(signed_in, route, answers, says):
    signed_in.script[route] = answers
    with pytest.raises(SystemExit) as e:
        push.main(["--yes"])
    assert says in str(e.value) and str(e.value).startswith("debrief: ") and signed_in.snapshot is None
    assert SECRET not in str(e.value)


def test_push_waits_as_long_as_a_rate_limit_says_then_goes_on(signed_in):
    signed_in.script["POST /v1/uploads"] = [refuse(429, "rate_limited", headers={"Retry-After": "17"})]
    push.main(["--yes"])
    assert signed_in.naps == [17] and signed_in.snapshot


def test_out_and_prev_are_for_dry_runs(signed_in):
    with pytest.raises(SystemExit) as e:
        push.main(["--out", str(signed_in.home / "x")])
    assert e.value.code == 2 and signed_in.calls == []


def test_the_command_line_knows_login_and_logout(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != SERVER_ENV} | {"DEBRIEF_HOME": str(tmp_path)}
    run = [sys.executable, "-m", "debrief.cli"]
    got = subprocess.run([*run, "login"], check=False, env=env, capture_output=True, text=True)
    assert got.returncode == 1 and "no server is set" in got.stderr and got.stdout == ""
    got = subprocess.run([*run, "logout"], check=False, env=env, capture_output=True, text=True)
    assert got.returncode == 0 and "Not signed in" in got.stdout
    assert list(tmp_path.iterdir()) == []

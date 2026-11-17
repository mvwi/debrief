"""The report server answers only requests addressed to this machine."""

import functools
import http.client
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from debrief import cli, config
from debrief.cli import ReportHandler
from debrief.paths import SITE_SRC, TOKEN_HEADER
from debrief.runner import Runner, estimate_of, progress


def get(port: int, host: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/index.html", headers={"Host": host})
    status = conn.getresponse().status
    conn.close()
    return status


def test_host_header(tmp_path):
    (tmp_path / "index.html").write_text("report")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(ReportHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    try:
        assert get(port, f"127.0.0.1:{port}") == 200
        assert get(port, f"localhost:{port}") == 200
        assert get(port, f"evil.example:{port}") == 403   # a rebound hostname
        assert get(port, "127.0.0.1") == 403               # wrong port
    finally:
        server.shutdown()


FAKE_KEY = "sk-or-v1-" + "0" * 28 + "a1b2"
TOKEN = {TOKEN_HEADER: ReportHandler.token}
JSON = {"Content-Type": "application/json"} | TOKEN
GOOD = {"stages": {"label": {"llm": "openrouter", "model": "typesafe/jev-1.13"}}, "github": False}


@pytest.fixture
def api(tmp_path, monkeypatch):
    """call(method, path, body, headers) -> (status, parsed JSON, raw text), against a scratch home."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "find_dotenv", lambda: str(tmp_path / "no-such.env"))
    for name in {*config.ENV.values(), config.KEY}:
        monkeypatch.delenv(name, raising=False)
    jobs = Runner(home=tmp_path, hide=(ReportHandler.token,))
    monkeypatch.setattr(ReportHandler, "jobs", jobs)
    (tmp_path / "site").mkdir()  # as in `debrief open`: the site is a folder inside the home, beside .env
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(ReportHandler, directory=str(tmp_path / "site")))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()

    def call(method, path, body=None, headers=JSON):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        data = body if isinstance(body, bytes | None) else json.dumps(body).encode()
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        text = r.read().decode()
        conn.close()
        return r.status, json.loads(text) if r.getheader("Content-Type") == "application/json" else None, text

    call.port, call.jobs, call.home = server.server_port, jobs, tmp_path
    yield call
    jobs.cancel()
    server.shutdown()


def test_settings_save_and_read_back(api):
    status, got, _ = api("GET", "/api/settings")
    assert status == 200 and got["stages"]["label"]["llm"] == "claude-code" and got["github"] is True
    assert list(got["stages"]["recommend"]["defaults"]) == ["claude-code"]
    status, got, _ = api("POST", "/api/settings", GOOD)
    assert status == 200 and got["stages"]["label"]["model"] == "typesafe/jev-1.13" and got["github"] is False
    assert config.choose("label") == ("openrouter", "typesafe/jev-1.13")
    assert not config.github_enabled()


def test_settings_keep_or_set_picked_projects(api):
    config.pick("/home/me/alpha")
    status, got, _ = api("POST", "/api/settings", GOOD)  # the Settings page sends no projects
    assert status == 200 and got["projects"] == ["/home/me/alpha"] == config.projects()
    status, got, _ = api("POST", "/api/settings", GOOD | {"projects": ["/home/me/beta"]})
    assert status == 200 and got["projects"] == ["/home/me/beta"] == config.projects()
    assert api("POST", "/api/settings", GOOD | {"projects": ["not/absolute"]})[0] == 400
    assert config.projects() == ["/home/me/beta"]


def test_sync_stops_before_any_model_call_until_a_project_is_picked(api, tmp_path, monkeypatch, capsys):
    from debrief import cli, db
    conn = db.connect(tmp_path / "debrief.db")
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/home/me/alpha')")
    conn.execute("INSERT INTO messages (session_id, idx, role, user_kind, text) VALUES ('s1', 0, 'user', 'human', 'hi')")
    conn.commit()
    monkeypatch.setattr(db, "connect", lambda: conn)
    ran = []
    monkeypatch.setattr(cli, "stage", lambda name, argv: ran.append(name))
    cli.sync(["--no-github"])
    out = capsys.readouterr().out
    assert ran == ["ingest"] and "No project is picked" in out and "1        1  /home/me/alpha" in out
    cli.sync(["--no-github", "--project", "/home/me/nowhere"])
    assert ran == ["ingest"] * 2 and "no sessions ran in /home/me/nowhere" in capsys.readouterr().out
    assert config.projects() == []
    cli.sync(["--no-github", "--project", "/home/me/alpha"])
    assert ran[2:] == ["ingest", "label", "synthesize", "recommend", "export"] and config.projects() == ["/home/me/alpha"]
    cli.sync(["--no-github"])  # the pick is kept
    assert ran[7:] == ["ingest", "label", "synthesize", "recommend", "export"]


def test_key_goes_in_and_never_comes_out(api):
    status, got, text = api("POST", "/api/key", {"key": FAKE_KEY})
    assert status == 200 and got["key"] == {"set": True, "last4": "a1b2", "source": "file"}
    assert FAKE_KEY in config.ENV_PATH.read_text()
    for _, _, text in (api("GET", "/api/settings"), api("POST", "/api/settings", GOOD), api("GET", "/.env"),
                       api("GET", "/../.env"), api("GET", "/%2e%2e/.env")):
        assert FAKE_KEY not in text and "0000" not in text
    status, got, _ = api("DELETE", "/api/key")
    assert status == 200 and got["key"]["set"] is False
    assert FAKE_KEY not in config.ENV_PATH.read_text()


@pytest.mark.parametrize(("method", "path", "body", "headers", "want"), [
    ("GET", "/api/settings", None, {"Host": "evil.example"}, 403),
    ("POST", "/api/settings", GOOD, JSON | {"Host": "evil.example"}, 403),
    ("POST", "/api/key", {"key": FAKE_KEY}, JSON | {"Host": "evil.example"}, 403),
    ("DELETE", "/api/key", None, JSON | {"Host": "evil.example"}, 403),
    ("POST", "/api/settings", GOOD, JSON | {"Origin": "http://evil.example"}, 403),
    ("POST", "/api/settings", GOOD, {"Content-Type": "application/json"}, 403),  # no token
    ("POST", "/api/key", {"key": FAKE_KEY}, JSON | {TOKEN_HEADER: "wrong"}, 403),
    ("DELETE", "/api/key", None, JSON | {TOKEN_HEADER: "caf\u00e9"}, 403),
    ("POST", "/api/key", {"key": FAKE_KEY}, JSON | {"Origin": "null"}, 403),
    ("DELETE", "/api/key", None, JSON | {"Origin": "http://evil.example"}, 403),
    ("POST", "/api/settings", GOOD, JSON | {"Content-Type": "text/plain"}, 415),
    ("POST", "/api/key", {"key": FAKE_KEY}, TOKEN, 415),
    ("DELETE", "/api/key", None, TOKEN, 415),
    ("POST", "/api/settings", GOOD | {"pad": "x" * 140000}, JSON, 413),
    ("POST", "/api/settings", b"{not json", JSON, 400),
    ("POST", "/api/settings", [1], JSON, 400),
    ("POST", "/api/settings", {"stages": GOOD["stages"]}, JSON, 400),                             # no github
    ("POST", "/api/settings", {"stages": {"lable": {}}, "github": True}, JSON, 400),
    ("POST", "/api/settings", {"stages": {"verify": {"llm": "openrouter"}}, "github": True}, JSON, 400),
    ("POST", "/api/settings", {"stages": {"label": {"model": 'a"\nb'}}, "github": True}, JSON, 400),
    ("POST", "/api/key", {"key": "short"}, JSON, 400),
    ("POST", "/api/key", {"key": FAKE_KEY + "\nEVIL=1"}, JSON, 400),
    ("POST", "/api/other", {}, JSON, 404),
    ("PUT", "/api/settings", GOOD, JSON, 501),
])
def test_refused_requests_write_nothing(api, method, path, body, headers, want):
    config.save({"label": {"model": "kept"}})
    config.save_key("k" * 20 + "kept")
    before = config.CONFIG_PATH.read_text(), config.ENV_PATH.read_text()
    assert api(method, path, body, headers)[0] == want
    assert (config.CONFIG_PATH.read_text(), config.ENV_PATH.read_text()) == before


def test_own_origin_is_accepted(api):
    for host in ("127.0.0.1", "localhost"):
        assert api("POST", "/api/settings", GOOD, JSON | {"Origin": f"http://{host}:{api.port}"})[0] == 200
    assert api("POST", "/api/settings", GOOD, JSON | {"Origin": "http://127.0.0.1:1"})[0] == 403


# --- the run API. The child is a fake: one line of Python, so `ps` shows it whole. ---

def fake(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", f"import subprocess, sys, time; {code}"]


# Prints what a sync prints, starts a child of its own as `claude -p` would be, then hangs.
HANGS = fake("print('== ingest'); print('== label --llm=claude-code'); print('  40/215 done, 0 failed'); "
             "print('kid', subprocess.Popen(['sleep', '60']).pid); time.sleep(60)")
DRY = fake("print('== label'); print('would label 74 items in 10 claude -p calls, 4 at a time: about 3 min'); "
           "sys.exit('--dry-run' not in sys.argv)")


def until(check, seconds: float = 5.0):
    end = time.monotonic() + seconds
    while not (got := check()) and time.monotonic() < end:
        time.sleep(0.02)
    return got


def alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(out) and not out.startswith("Z")


def test_progress_reads_stage_and_counts():
    lines = ["", "== ingest --project=/x", "11 headless runs skipped, 1 loaded", "== label --llm=claude-code",
             "classifying 74 items with claude-haiku-5-5 in 10 calls, 4 at a time", "ok   934c0f77 #107 how about 3/4 done",
             "  8/74 done, 0 failed", "=== turns: 3 items", "\x00 garbage �", "  16/74 done, 1 failed"]
    assert progress(lines) == {"stage": "label", "done": 16, "total": 74}
    assert progress(["== synthesize"], progress(lines)) == {"stage": "synthesize", "done": None, "total": None}
    assert progress(["== synthesize", "== myrepo: 89 sessions, 871 human turns", "== all projects: 95 sessions"])["stage"] == "synthesize"
    assert progress(["what is this"], {"stage": "label", "done": 1, "total": 2}) == {"stage": "label", "done": 1, "total": 2}
    assert progress([]) == {"stage": None, "done": None, "total": None}


def test_estimate_reads_the_dry_run_line():
    claude = "would label 74 items in 10 claude -p calls, 4 at a time: about 3 min"
    assert estimate_of(["== label", claude]) == {"items": 74, "calls": 10, "minutes": 3}
    assert estimate_of(["would label 74 items in 74 Jev calls, 8 at a time"]) == {"items": 74, "calls": 74, "minutes": None}
    assert estimate_of(["nothing here"]) == {"items": None, "calls": None, "minutes": None}


def test_run_start_conflict_cancel(api):
    api.jobs.cmd = HANGS
    assert api("GET", "/api/run")[1]["state"] == "idle"
    status, got, _ = api("POST", "/api/run", {"argv": ["--llm=openrouter"]})  # the body is ignored
    assert status == 200 and got["state"] == "running" and got["started"]
    got = until(lambda: (g := api("GET", "/api/run")[1])["done"] and len(g["log"]) == 4 and g)
    assert (got["stage"], got["done"], got["total"]) == ("label", 40, 215)
    child, kid = int((api.home / "run.pid").read_text()), int(got["log"][-1].split()[1])
    assert api("POST", "/api/run")[0] == 409
    assert api("POST", "/api/estimate")[0] == 409
    status, got, _ = api("DELETE", "/api/run")
    assert status == 200 and got["state"] == "cancelled" and got["ended"] and got["exit_code"] == -signal.SIGTERM
    assert not alive(child) and until(lambda: not alive(kid))  # the child's own child went with it
    assert not (api.home / "run.pid").exists()
    assert api("DELETE", "/api/run")[1]["state"] == "cancelled"  # nothing to stop: no error


@pytest.mark.parametrize(("code", "state"), [(0, "done"), (3, "failed")])
def test_run_finishes(api, code, state):
    api.jobs.cmd = fake(f"print('== export'); sys.exit({code})")
    assert api("POST", "/api/run")[0] == 200
    got = until(lambda: (g := api("GET", "/api/run")[1])["state"] != "running" and g)
    assert (got["state"], got["exit_code"], got["stage"], got["log"]) == (state, code, "export", ["== export"])
    assert (api.home / "run.log").read_text() == "== export\n"
    api.jobs.cmd = HANGS
    assert api("POST", "/api/run")[1]["state"] == "running"  # a finished run does not block the next


def test_log_never_holds_the_key_or_the_token(api):
    config.save_key(FAKE_KEY)
    api.jobs.cmd = fake(f"print('Authorization: Bearer {FAKE_KEY} t={ReportHandler.token}')")
    api("POST", "/api/run")
    until(lambda: api("GET", "/api/run")[1]["state"] == "done")
    api.jobs.export = api.jobs.cmd
    for text in ((api.home / "run.log").read_text(), api("GET", "/api/run")[2], api("POST", "/api/estimate")[2]):
        assert "Bearer [redacted] t=[redacted]" in text and FAKE_KEY not in text and ReportHandler.token not in text


def test_estimate_runs_the_dry_run_then_export(api):
    api.jobs.cmd, api.jobs.export = DRY, fake("print('exported')")
    status, got, _ = api("POST", "/api/estimate")
    assert status == 200 and (got["items"], got["calls"], got["minutes"], got["exit_code"]) == (74, 10, 3, 0)
    assert got["lines"][-1] == "exported"
    assert api("GET", "/api/run")[1]["state"] == "idle"
    api.jobs.cmd = fake("sys.exit('boom')")
    status, got, _ = api("POST", "/api/estimate")
    assert status == 500 and got["lines"] == ["boom"]  # and export did not run
    assert api("POST", "/api/run")[0] == 200  # a failed estimate does not block a run


@pytest.mark.parametrize(("method", "path", "headers", "want"), [
    (method, path, headers, want)
    for method, path in (("POST", "/api/run"), ("DELETE", "/api/run"), ("POST", "/api/estimate"))
    for headers, want in ((JSON | {"Host": "evil.example"}, 403), ({"Content-Type": "application/json"}, 403),
                          (JSON | {TOKEN_HEADER: "wrong"}, 403), (JSON | {"Origin": "http://evil.example"}, 403),
                          (TOKEN, 415), (JSON | {"Content-Length": "140000"}, 413))
] + [("GET", "/api/run", {"Host": "evil.example"}, 403), ("PUT", "/api/run", JSON, 501),
     ("GET", "/api/estimate", {}, 404)])
def test_refused_run_requests_start_and_stop_nothing(api, method, path, headers, want):
    ran = api.home / "ran"
    api.jobs.cmd = api.jobs.export = fake(f"open({str(ran)!r}, 'w')")
    if method == "DELETE":
        api.jobs.cmd = HANGS
        assert api("POST", "/api/run")[0] == 200
    assert api(method, path, None, headers)[0] == want
    time.sleep(0.2)
    assert not ran.exists()
    assert api("GET", "/api/run")[1]["state"] == ("running" if method == "DELETE" else "idle")


def test_bad_body_starts_nothing(api):
    api.jobs.cmd = HANGS
    assert api("POST", "/api/run", b"{not json")[0] == 400
    assert api("POST", "/api/run", [1])[0] == 400
    assert api("GET", "/api/run")[1]["state"] == "idle"


def test_orphan_run_is_stopped(tmp_path):
    """A run whose server was killed is stopped by the next server, and by the next start."""
    jobs = Runner(home=tmp_path)
    jobs.cmd = HANGS
    orphan = subprocess.Popen(HANGS, start_new_session=True, stdout=subprocess.PIPE, text=True)
    kid = int([orphan.stdout.readline() for _ in range(4)][-1].split()[1])
    (tmp_path / "run.pid").write_text(str(orphan.pid))
    jobs.stop_orphan()
    assert orphan.wait(5) == -signal.SIGTERM and until(lambda: not alive(kid))
    assert not (tmp_path / "run.pid").exists()

    orphan = subprocess.Popen(HANGS, start_new_session=True, stdout=subprocess.DEVNULL)
    (tmp_path / "run.pid").write_text(str(orphan.pid))
    assert jobs.start() and orphan.wait(5) == -signal.SIGTERM
    jobs.cancel()

    other = subprocess.Popen(["sleep", "60"], start_new_session=True)  # a reused pid: some other program
    (tmp_path / "run.pid").write_text(str(other.pid))
    jobs.stop_orphan()
    assert other.poll() is None and not (tmp_path / "run.pid").exists()
    other.kill()
    (tmp_path / "run.pid").write_text("not a pid")
    jobs.stop_orphan()


def served(home, code: str) -> subprocess.Popen:
    """`debrief open` in a process of its own, with a fake sync; its first line is read by the caller."""
    script = f"from debrief import cli; cli.ReportHandler.jobs.cmd = {HANGS!r}; {code}; cli.serve(['--no-browser', '--port', '0'])"
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        env=os.environ | {"DEBRIEF_HOME": str(home)}, stdout=subprocess.PIPE, text=True)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_stopping_the_server_stops_the_run(tmp_path, sig):
    (tmp_path / "site" / "data").mkdir(parents=True)
    (tmp_path / "site" / "data" / "index.json").write_text("{}")
    server = served(tmp_path, "print(cli.ReportHandler.token, flush=True)")
    try:
        token, url = server.stdout.readline().strip(), server.stdout.readline().split()[3]
        port = int(url.split(":")[2].split("/")[0])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/run", headers={"Content-Type": "application/json", TOKEN_HEADER: token})
        assert conn.getresponse().status == 200
        child = int(until(lambda: (tmp_path / "run.pid").exists() and (tmp_path / "run.pid").read_text()))
        assert alive(child)
        server.send_signal(sig)
        assert server.wait(10) == 0
        assert not alive(child) and not (tmp_path / "run.pid").exists()
    finally:
        server.kill()


def test_open_with_no_report_ingests_and_exports_only(tmp_path, monkeypatch):
    stages = []
    monkeypatch.setattr(cli, "SITE_OUT", tmp_path / "site")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check: stages.append(cmd[-1]))
    monkeypatch.setattr(ReportHandler, "jobs", Runner(home=tmp_path))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(http.server.ThreadingHTTPServer, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    cli.serve(["--no-browser", "--port", "0"])
    assert stages == ["ingest", "export"]  # as children, so the server holds no database lock

    stages.clear()
    (tmp_path / "site" / "data").mkdir(parents=True)
    (tmp_path / "site" / "data" / "index.json").write_text("{}")
    cli.serve(["--no-browser", "--port", "0"])
    assert stages == []  # a home with a report starts at once


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_page_forgets_an_estimate_once_a_check_has_ended():
    js = (SITE_SRC / "app.js").read_text()
    fn = re.search(r"^function estimateAfter\(.*?^}$", js, re.MULTILINE | re.DOTALL)[0]
    checks = """
      const assert = require("node:assert"), est = { items: 12, after: "t1" }, gone = { outdated: true };
      assert.equal(estimateAfter(null, { state: "done", ended: "t2" }), null);
      assert.equal(estimateAfter(est, { state: "done", ended: "t1" }), est);     // the check it was made after
      assert.equal(estimateAfter(est, { state: "running", ended: null }), est);  // a check that is still going
      for (const state of ["done", "failed", "cancelled"]) assert.deepEqual(estimateAfter(est, { state, ended: "t2" }), gone);
      const fresh = { items: 3, after: null };
      assert.equal(estimateAfter(fresh, { state: "idle", ended: null }), fresh);  // no check yet
      assert.equal(estimateAfter(fresh, { error: "" }), fresh);                   // the server did not answer
    """
    subprocess.run(["node", "-e", fn + checks], check=True)

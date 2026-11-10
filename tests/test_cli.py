"""The report server answers only requests addressed to this machine."""

import functools
import http.client
import http.server
import json
import threading

import pytest

from debrief import config
from debrief.cli import ReportHandler


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
TOKEN = {"X-Debrief-Token": ReportHandler.token}
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

    call.port = server.server_port
    yield call
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
    ("POST", "/api/key", {"key": FAKE_KEY}, JSON | {"X-Debrief-Token": "wrong"}, 403),
    ("DELETE", "/api/key", None, JSON | {"X-Debrief-Token": "caf\u00e9"}, 403),
    ("POST", "/api/key", {"key": FAKE_KEY}, JSON | {"Origin": "null"}, 403),
    ("DELETE", "/api/key", None, JSON | {"Origin": "http://evil.example"}, 403),
    ("POST", "/api/settings", GOOD, JSON | {"Content-Type": "text/plain"}, 415),
    ("POST", "/api/key", {"key": FAKE_KEY}, TOKEN, 415),
    ("DELETE", "/api/key", None, TOKEN, 415),
    ("POST", "/api/settings", GOOD | {"pad": "x" * 5000}, JSON, 413),
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

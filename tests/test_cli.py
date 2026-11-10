"""The report server answers only requests addressed to this machine."""

import functools
import http.client
import http.server
import threading

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

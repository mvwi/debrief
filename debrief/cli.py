"""debrief: review your coding-agent sessions and see what to change.

    debrief sync                 ingest local logs, label turns, find patterns, build the report
    debrief sync --project DIR   pick a project to analyse (kept in config.toml), then sync
    debrief sync --dry-run       ingest, then say what labeling would cost; no model calls
    debrief open                 serve the report on localhost and open it in the browser;
                                 the page can also start a sync, show its progress and stop it
    debrief <stage> [args]       run one stage; `debrief <stage> --help` lists its flags

Everything is kept under DEBRIEF_HOME (default ~/.local/share/debrief).
"""

import argparse
import contextlib
import functools
import hmac
import http.server
import importlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import webbrowser

from debrief import config
from debrief.paths import HOME, SITE_OUT
from debrief.runner import Runner

# Stage name -> module. Each module has main(argv).
STAGES = {
    "ingest": "sources",
    "label": "classify",
    "synthesize": "synthesize",
    "recommend": "recommend",
    "verify": "verify_evidence",
    "export": "export_site",
    "entire": "scrape",
    "entire-load": "normalize",
    "github": "github",
    "prune": "prune",
    "config": "config",
}


def stage(name: str, argv: list[str]) -> None:
    print(f"\n== {name} {' '.join(argv)}".rstrip(), flush=True)
    importlib.import_module(f"debrief.{STAGES[name]}").main(argv)


def sync(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="debrief sync", description="Ingest local logs and rebuild the report.")
    p.add_argument("--project", metavar="DIR", action="append", default=[],
                   help="Add this project to the ones sync analyses (repeatable; kept in config.toml)")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="Only logs and evidence from this date on")
    p.add_argument("--import", dest="import_dirs", action="append", default=[], metavar="DIR",
                   help="Also load a folder of JSONL logs (repeatable)")
    p.add_argument("--include-headless", action="store_true", help="Also load scripted runs (claude -p, SDK jobs)")
    p.add_argument("--llm", choices=["claude-code", "openrouter"],
                   help="Use this for labeling and synthesis, whatever config.toml says. "
                        "claude-code: `claude -p` on your Claude subscription; openrouter: needs OPENROUTER_API_KEY")
    p.add_argument("--no-github", action="store_true", help="Do not ask GitHub about your sessions' pull requests")
    p.add_argument("--dry-run", action="store_true",
                   help="Ingest, then print what labeling would cost; no model calls, no GitHub requests")
    args = p.parse_args(argv)

    # Ingest reads every log: it is local, and it is what tells you which projects there are.
    ingest = [f"--since={args.since}"] if args.since else []
    ingest += [f"--import={d}" for d in args.import_dirs] + ["--include-headless"] * args.include_headless
    since = [f"--since={args.since}"] if args.since else []
    llm = [f"--llm={args.llm}"] if args.llm else []

    stage("ingest", ingest)
    if not pick_projects(args.project):
        return
    off = "--no-github" if args.no_github else None if config.github_enabled() else "[github] enabled = false in config.toml"
    if off:
        print(f"\ngithub: skipped, {off}")
    else:
        try:
            stage("github", ["--dry-run"] * args.dry_run)
        except RuntimeError as e:  # a failed gh call; the report builds without GitHub data
            print(f"github: skipped, {str(e).splitlines()[0]}")
    if args.dry_run:
        # The label dry run also prints sample prompts; keep only its count and estimate.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stage("label", [*llm, "--dry-run"])
        lines = out.getvalue().splitlines()
        print("\n".join(line for line in lines if line.startswith(("== ", "=== ", "would label"))))
        return
    stage("label", llm)
    stage("synthesize", [*llm, *since])
    stage("recommend", [])
    stage("export", since)
    print("\nDone. Run `debrief open` to read the report.")


def pick_projects(dirs: list[str]) -> bool:
    """Add dirs to the picked projects. False, after printing the projects, when none is picked
    or a dir is not a project: sync then stops before any model call."""
    from debrief.db import connect
    from debrief.export_site import projects_section

    rows = projects_section(connect(), set(config.projects()))
    known = {r["path"] for r in rows}
    wanted = [os.path.abspath(os.path.expanduser(d)) for d in dirs]
    unknown = [d for d in wanted if d not in known]
    if not unknown:
        for d in wanted:
            config.pick(d)
        if any(p in known for p in config.projects()):
            return True
    print(f"\n{'no sessions ran in ' + ', '.join(unknown) if unknown else 'No project is picked, so nothing was analysed'}. "
          f"Projects found:\n")
    print(f"  {'sessions':>8} {'messages':>8}  project")
    for r in rows:
        print(f"  {r['sessions']:>8} {r['turns']:>8}  {r['path']}{'  (picked)' if r['picked'] else ''}")
    print("\nPick one with `debrief sync --project DIR` (repeat the flag for more). "
          "Picks are kept in config.toml under [projects].")
    return False


class ReportHandler(http.server.SimpleHTTPRequestHandler):
    """Static files, the settings API and the run API, for requests addressed to this machine only.

    A web page can point its own hostname at 127.0.0.1 (DNS rebinding) and reach this server
    through the browser; checking Host stops that, since the page cannot fake it.

    The writes are config.toml, the key in DEBRIEF_HOME/.env, and starting or stopping a sync
    (which takes nothing from the request: what to analyse comes from config). A write must be JSON, which
    a page on another site cannot send without a preflight this server never answers, and must
    come from this origin when the browser names one. It must also carry the token made for this
    run, which only the link `debrief open` prints holds: another user of this machine can reach
    the port but does not have the link.
    """

    MAX_BODY = 128 * 1024  # room for 200 picked project paths
    token = secrets.token_urlsafe(16)
    jobs = Runner(hide=(token,))

    def local(self) -> bool:
        ok = self.headers.get("Host") in self.hosts()
        if not ok:
            self.send_error(403, "debrief serves only 127.0.0.1 and localhost")
        return ok

    def hosts(self) -> tuple[str, str]:
        port = self.server.server_port
        return f"127.0.0.1:{port}", f"localhost:{port}"

    def send_head(self):
        return super().send_head() if self.local() else None

    def do_GET(self):
        if self.path not in ("/api/settings", "/api/run"):
            super().do_GET()
        elif self.local():
            self.reply_settings() if self.path == "/api/settings" else self.reply(200, self.jobs.status())

    def do_POST(self):
        if not self.local():
            return
        self.close_connection = True  # a refused body is never read, so the connection cannot be reused
        length = self.headers.get("Content-Length", "0")
        if not hmac.compare_digest(self.headers.get("X-Debrief-Token", "").encode(), self.token.encode()):
            return self.reply(403, {"error": "to change settings, open the link `debrief open` printed"})
        if self.headers.get("Origin") not in (None, *(f"http://{h}" for h in self.hosts())):
            return self.reply(403, {"error": "wrong origin"})
        if self.headers.get_content_type() != "application/json":
            return self.reply(415, {"error": "send application/json"})
        if not length.isdecimal() or int(length) > self.MAX_BODY:
            return self.reply(413, {"error": f"body must be at most {self.MAX_BODY} bytes"})
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
            if not isinstance(body, dict):
                raise TypeError("expected a JSON object")
            match self.command, self.path:
                case "POST", "/api/settings":
                    if not isinstance(body.get("stages"), dict) or not isinstance(body.get("github"), bool):
                        raise TypeError("expected {stages: {...}, github: true or false}")
                    # The page that does not send the picked projects keeps them.
                    config.save({**body["stages"], "github": {"enabled": body["github"]},
                                 "projects": {"include": body.get("projects", config.projects())}})
                case "POST", "/api/key":
                    config.save_key(body.get("key"))
                case "DELETE", "/api/key":
                    config.remove_key()
                case "POST", "/api/run":
                    if not self.jobs.start():
                        return self.reply(409, {"error": "a run or an estimate is already going"})
                    return self.reply(200, self.jobs.status())
                case "DELETE", "/api/run":
                    self.jobs.cancel()
                    return self.reply(200, self.jobs.status())
                case "POST", "/api/estimate":
                    # POST, not GET: it ingests, so it writes the database and must carry the token.
                    got = self.jobs.estimate()
                    if got is None:
                        return self.reply(409, {"error": "a run or an estimate is already going"})
                    return self.reply(500 if got["exit_code"] else 200, got)
                case _:
                    return self.reply(404, {"error": "no such endpoint"})
        except (ValueError, TypeError) as e:  # bad JSON is a ValueError
            return self.reply(400, {"error": str(e)})
        self.reply_settings()

    do_DELETE = do_POST

    def reply_settings(self) -> None:
        try:
            self.reply(200, config.settings())
        except SystemExit as e:  # config.toml was broken by hand
            self.reply(500, {"error": str(e)})

    def reply(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def serve(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="debrief open", description="Serve the report on localhost.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Serve without opening a browser tab")
    args = p.parse_args(argv)
    handler = functools.partial(ReportHandler, directory=str(SITE_OUT))
    try:
        # Loopback only: the report holds transcripts.
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as e:
        sys.exit(f"debrief: cannot listen on port {args.port} ({e.strerror}). Try --port.")
    jobs = ReportHandler.jobs
    jobs.stop_orphan()
    if not (SITE_OUT / "data" / "index.json").exists():
        print("No report yet: reading your local logs so the page has something to show (no model calls) ...", flush=True)
        # In child processes, as every later run is: a stage run here would keep its database
        # connection open for as long as the server lives, and its lock would fail those runs.
        for name in ("ingest", "export"):
            subprocess.run([sys.executable, "-m", "debrief.cli", name], check=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill` stops a run too, as Ctrl-C does
    url = f"http://127.0.0.1:{server.server_port}/"
    url += f"?t={ReportHandler.token}"  # the page keeps the token for this tab and drops it from the address
    print(f"Serving {SITE_OUT} at {url} (Ctrl-C to stop)", flush=True)
    # The browser is started with its address as a command-line argument, which other users can
    # see in the process list. So it is given a file only this user can read, which sends it on.
    door = HOME / "open.html"
    try:
        if not args.no_browser:
            door.unlink(missing_ok=True)
            with os.fdopen(os.open(door, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write(f'<!doctype html><meta http-equiv="refresh" content="0; url={url}">')
            webbrowser.open(door.as_uri())
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        door.unlink(missing_ok=True)
        jobs.cancel()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    commands = {"sync": sync, "open": serve}
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__ + f"\nStages: {', '.join(STAGES)}\nData: {HOME}")
        return
    name, rest = argv[0], argv[1:]
    if name in commands:
        commands[name](rest)
    elif name in STAGES:
        importlib.import_module(f"debrief.{STAGES[name]}").main(rest)
    else:
        sys.exit(f"debrief: unknown command {name!r}. Try `debrief --help`.")


if __name__ == "__main__":
    main()

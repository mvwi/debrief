"""debrief: review your coding-agent sessions and see what to change.

    debrief sync                 ingest local logs, label turns, find patterns, build the report
    debrief sync --dry-run       ingest, then say what labeling would cost; no model calls
    debrief open                 serve the report on localhost and open it in the browser
    debrief <stage> [args]       run one stage; `debrief <stage> --help` lists its flags

Everything is kept under DEBRIEF_HOME (default ~/.local/share/debrief).
"""

import argparse
import functools
import http.server
import importlib
import sys
import webbrowser

from debrief.paths import HOME, SITE_OUT

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
}


def stage(name: str, argv: list[str]) -> None:
    print(f"\n== {name} {' '.join(argv)}".rstrip(), flush=True)
    importlib.import_module(f"debrief.{STAGES[name]}").main(argv)


def sync(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="debrief sync", description="Ingest local logs and rebuild the report.")
    p.add_argument("--project", metavar="DIR", help="Only sessions run in this directory or below it")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="Only logs and evidence from this date on")
    p.add_argument("--import", dest="import_dirs", action="append", default=[], metavar="DIR",
                   help="Also load a folder of JSONL logs (repeatable)")
    p.add_argument("--include-headless", action="store_true", help="Also load scripted runs (claude -p, SDK jobs)")
    p.add_argument("--llm", choices=["claude-code", "openrouter"], default="claude-code",
                   help="claude-code: `claude -p` on your Claude subscription (default); openrouter: needs OPENROUTER_API_KEY")
    p.add_argument("--dry-run", action="store_true", help="Ingest, then print what labeling would cost; no model calls")
    args = p.parse_args(argv)

    ingest = [f"--{k}={v}" for k, v in (("project", args.project), ("since", args.since)) if v]
    ingest += [f"--import={d}" for d in args.import_dirs] + ["--include-headless"] * args.include_headless
    since = [f"--since={args.since}"] if args.since else []

    stage("ingest", ingest)
    if args.dry_run:
        stage("label", [f"--llm={args.llm}", "--dry-run"])
        return
    stage("label", [f"--llm={args.llm}"])
    stage("synthesize", [f"--llm={args.llm}", *since])
    stage("recommend", [])
    stage("export", since)
    print("\nDone. Run `debrief open` to read the report.")


def serve(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="debrief open", description="Serve the report on localhost.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Serve without opening a browser tab")
    args = p.parse_args(argv)
    if not (SITE_OUT / "data" / "index.json").exists():
        sys.exit(f"debrief: no report in {SITE_OUT}. Run `debrief sync` first.")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_OUT))
    try:
        # Loopback only: the report holds transcripts.
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as e:
        sys.exit(f"debrief: cannot listen on port {args.port} ({e.strerror}). Try --port.")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Serving {SITE_OUT} at {url} (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


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

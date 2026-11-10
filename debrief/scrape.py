"""Fetch Entire sessions for a repo into data/raw/.

Needs a logged-in `entire` CLI (`entire login`); requests go through `entire api`.

    uv run python -m debrief.scrape --since 2026-08-21 --until 2026-09-18 --limit 100 --dry-run
    uv run python -m debrief.scrape --since 2026-08-21 --until 2026-09-18 --limit 100
    uv run python -m debrief.scrape --ids efd45cc6-c764-4e22-80eb-cdbc1f9173f9 ...

Sessions are chosen from the window, not just the most recent: every Codex/Pi session
up to --max-other, then Claude Code sessions spread evenly across the window's dates.
A session is fetched again only when its last activity has changed.
"""

import argparse
import gzip
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

from debrief.normalize import safe_session_id
from debrief.paths import RAW_DIR

SUPPORTED_AGENTS = {"Claude Code", "Codex", "Pi"}  # the formats normalize.py parses
DELAY = 1.0  # seconds between requests
RETRIES = 3

requests_made = 0


class ApiError(Exception):
    pass


def api(path: str) -> str:
    """GET path through `entire api`, one request at a time, retrying 429 and 5xx."""
    global requests_made
    for attempt in range(RETRIES + 1):
        time.sleep(DELAY)
        requests_made += 1
        out = subprocess.run(
            ["entire", "api", "--to", "cell", f"/api/v1{path}"],
            capture_output=True,
            check=False,
            text=True,
        )
        if out.returncode == 0:
            return out.stdout
        message = (out.stderr.strip() or out.stdout[:300]).strip()
        if attempt < RETRIES and re.search(r"\b(429|5\d\d)\b|rate limit", message, re.IGNORECASE):
            wait = 10 * 2**attempt
            print(f"  retrying in {wait}s: {message[:120]}")
            time.sleep(wait)
            continue
        raise ApiError(f"entire api {path} failed: {message}")
    raise AssertionError("unreachable")


def api_json(path: str) -> dict:
    return json.loads(api(path))


def to_rfc3339(date: str) -> str:
    return f"{date}T00:00:00Z" if len(date) == 10 else date


def resolve_repo(repo_path: str) -> str:
    return api_json(f"/git/repos/resolve?repoPath={repo_path}")["repoId"]


def list_sessions(repo_id: str, since: str | None, until: str | None, cap: int = 2000) -> list[dict]:
    """Every session in the window, most recent first, 200 per request."""
    window = "".join(f"&{k}={to_rfc3339(v)}" for k, v in (("since", since), ("until", until)) if v)
    sessions, cursor = [], ""
    while len(sessions) < cap:
        page = api_json(f"/repos/{repo_id}/sessions?limit=200&scope=all{window}" + (f"&cursor={cursor}" if cursor else ""))
        sessions += page["sessions"]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return sessions


def spread(items: list[dict], n: int) -> list[dict]:
    """n items evenly spaced through items sorted by last activity."""
    items = sorted(items, key=lambda s: s["lastActivityAt"])
    if n <= 0:
        return []
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def select(sessions: list[dict], limit: int, max_other: int) -> list[dict]:
    usable = [s for s in sessions if s["agent"] in SUPPORTED_AGENTS and (s.get("stepCount") or 0) >= 2]
    others = spread([s for s in usable if s["agent"] != "Claude Code"], min(max_other, limit))
    claude = spread([s for s in usable if s["agent"] == "Claude Code"], limit - len(others))
    return others + claude


def describe(chosen: list[dict], sessions: list[dict]) -> None:
    def week(s: dict) -> str:
        return datetime.fromisoformat(s["lastActivityAt"]).strftime("%G-W%V")

    print(f"{len(sessions)} sessions in window; chose {len(chosen)}")
    print("in window by agent:", dict(Counter(s["agent"] for s in sessions)))
    print("chosen by agent:   ", dict(Counter(s["agent"] for s in chosen)))
    print("chosen by model:   ", dict(Counter(s.get("model") for s in chosen)))
    print("chosen by week:    ", dict(sorted(Counter(week(s) for s in chosen).items())))
    steps = sorted(s.get("stepCount") or 0 for s in chosen)
    if steps:
        print(f"steps per session: min {steps[0]}, median {steps[len(steps) // 2]}, max {steps[-1]}")
    new = sum(1 for s in chosen if needs_fetch(s["sessionId"], s))
    print(f"to fetch: {new} sessions, about {2 * new} requests")


def needs_fetch(session_id: str, listing: dict | None) -> bool:
    saved = RAW_DIR / session_id / "listing.json"
    if not (RAW_DIR / session_id / "transcript.json").exists():
        return True
    if listing is None or not saved.exists():
        return False
    return json.loads(saved.read_text()).get("lastActivityAt") != listing.get("lastActivityAt")


def fetch_session(repo_id: str, session_id: str, listing: dict | None) -> None:
    if not safe_session_id(session_id):  # main() filters these out; this guards other callers
        raise ApiError(f"unsafe session ID {session_id!r}")
    out = RAW_DIR / session_id
    out.mkdir(parents=True, exist_ok=True)
    base = f"/repos/{repo_id}/sessions/{session_id}"
    # Structured transcript: messages, tool inputs and checkpoints, but no tool output.
    transcript = api_json(f"{base}/transcript")
    # The agent's native log, which keeps tool output. Format varies by agent. `entire api`
    # refuses bodies over 32 MB (and ignores Range), so for those logs keep only the structured
    # transcript; normalize.py falls back to it when raw.jsonl.gz is missing.
    try:
        raw = api(f"{base}/transcript/raw")
    except ApiError as e:
        if "exceeds" not in str(e):
            raise
        raw = None
        print(f"  {session_id}: raw log over the size limit; saving the structured transcript only")
    (out / "transcript.json").write_text(json.dumps(transcript, indent=2))
    if raw is None:
        (out / "raw.jsonl.gz").unlink(missing_ok=True)  # an older copy would hide the newer transcript
    else:
        (out / "raw.jsonl.gz").write_bytes(gzip.compress(raw.encode()))
    (out / "raw.jsonl").unlink(missing_ok=True)
    if listing:  # written last: its presence with a matching lastActivityAt marks the fetch complete
        (out / "listing.json").write_text(json.dumps(listing, indent=2))
    session = transcript["sessions"][0]
    print(f"saved {session_id}  {session['agent']:<12} {len(session['messages']):>4} messages  "
          f"{len(transcript['checkpoints'])} checkpoints  {transcript['session']['displayName']}")


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up in log files as it happens
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default="gh/entireio/cli", help="Entire repo path, e.g. gh/owner/name")
    parser.add_argument("--ids", nargs="*", help="Fetch these session IDs instead of choosing from a window")
    parser.add_argument("--since", help="Only sessions last active on or after this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only sessions last active before this date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=100, help="How many sessions to choose from the window")
    parser.add_argument("--max-other", type=int, default=30, help="Cap on Codex/Pi sessions in the choice")
    parser.add_argument("--dry-run", action="store_true", help="Show the choice without fetching sessions")
    args = parser.parse_args(argv)

    repo_id = resolve_repo(args.repo)
    if args.ids:
        chosen = [{"sessionId": i} for i in args.ids]
        listings: dict[str, dict] = {}
    else:
        sessions = list_sessions(repo_id, args.since, args.until)
        chosen = select(sessions, args.limit, args.max_other)
        listings = {s["sessionId"]: s for s in chosen}
    # The ID comes from the API or the command line and names a folder under RAW_DIR.
    for s in chosen:
        if not safe_session_id(s.get("sessionId")):
            print(f"warning: skipping session with unsafe ID {s.get('sessionId')!r}")
    chosen = [s for s in chosen if safe_session_id(s.get("sessionId"))]
    if not args.ids:
        describe(chosen, sessions)
    if args.dry_run:
        for s in chosen:
            print(f"  {s.get('lastActivityAt', '')[:10]}  {s.get('agent', ''):<12} {s.get('stepCount', ''):>4}  {s.get('displayName', s['sessionId'])}")
        print(f"requests made: {requests_made}")
        return

    failed = []
    for s in chosen:
        session_id = s["sessionId"]
        if not needs_fetch(session_id, listings.get(session_id)):
            continue
        try:
            fetch_session(repo_id, session_id, listings.get(session_id))
        except ApiError as e:
            failed.append(session_id)
            print(f"failed {session_id}: {e}")
    print(f"requests made: {requests_made}; failed: {len(failed)} {failed or ''}")


if __name__ == "__main__":
    main()

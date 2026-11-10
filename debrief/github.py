"""Load GitHub PRs, checks and review comments for the commits in data/raw/.

Reads checkpoint commit SHAs from each transcript.json, looks them up with the
logged-in `gh` CLI, and writes gh_prs, gh_commit_prs, gh_checks and
gh_review_comments in debrief.db.

Re-runs skip what cannot change: commits already looked up, settled PRs,
checks that have all concluded, and failure logs already stored. Heads of
unsettled PRs always get their checks re-listed, since a job can be re-run.

Check runs are listed with filter=all, so every attempt of a re-run job is a
separate gh_checks row; the default returns only the latest attempt.

    uv run python -m debrief.github
    uv run python -m debrief.github --refresh       # ignore the skips and fetch everything
    uv run python -m debrief.github --checks-since 2026-08-11
        # only re-list checks on stored commits of sessions active since then,
        # to pick up attempts that the old latest-only listing dropped
"""

import argparse
import json
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from debrief import db
from debrief.paths import RAW_DIR

LOG_LINES = 150
LOG_BYTES = 12_000
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUN_ID = re.compile(r"/actions/runs/(\d+)")
# Inline comments, reviews and issue comments draw ids from separate sequences
# that can overlap, so review and issue rows get an offset added to their id.
REVIEW_ID_OFFSET = 10**15
ISSUE_ID_OFFSET = 2 * 10**15
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated comments(first: 100) { nodes { databaseId } } }
      }
    }
  }
}
"""
SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS gh_commits_seen (
    commit_sha   TEXT PRIMARY KEY,   -- checkpoint commit whose PRs and checks we have fetched
    looked_up_at TEXT
);
"""
RETRIES = 5
requests = 0
requests_lock = threading.Lock()


def gh(path: str, paginate: bool = False, fields: list[str] | None = None) -> str | None:
    """Return the body of a request, or None on 404/410. `fields` are extra gh flags."""
    # Job logs carry colour codes, which gh refuses to print without this flag.
    cmd = ["gh", "api", path, "--allow-escape-sequences", *(fields or [])] + (["--paginate", "--slurp"] if paginate else [])
    global requests
    for attempt in range(RETRIES):
        with requests_lock:
            requests += 1
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
        limited = "rate limit" in out.stderr or "HTTP 429" in out.stderr
        if out.returncode == 0 or not limited or attempt == RETRIES - 1:
            break
        wait = 10 * 2**attempt
        print(f"rate limited on {path}; retrying in {wait}s")
        time.sleep(wait)
    if out.returncode != 0:
        if "HTTP 404" in out.stderr or "HTTP 410" in out.stderr:
            return None
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()}")
    return out.stdout


def gh_list(path: str, key: str | None = None) -> list[dict]:
    """GET every page of a list endpoint. `key` names the list inside object pages."""
    body = gh(path, paginate=True)
    if body is None:
        return []
    pages = json.loads(body)
    return [item for page in pages for item in (page[key] if key else page)]


def load_shas() -> list[str]:
    shas = []
    for path in sorted(RAW_DIR.glob("*/transcript.json")):
        for cp in json.loads(path.read_text()).get("checkpoints", []):
            if cp.get("commitSha") and cp["commitSha"] not in shas:
                shas.append(cp["commitSha"])
    return shas


def is_bot(user: dict | None) -> int:
    user = user or {}
    login = user.get("login") or ""
    return int(user.get("type") == "Bot" or login.endswith("[bot]") or login == "Copilot")


def trim_log(log: str) -> str:
    """Keep the failing step's lines, capped to the last LOG_LINES / LOG_BYTES."""
    lines = [TIMESTAMP.sub("", ANSI.sub("", line)) for line in log.splitlines()]
    err = next((i for i, line in enumerate(lines) if "##[error]" in line), None)
    if err is not None:
        start = max((i for i in range(err) if lines[i].startswith("##[group]Run ")), default=0)
        # Keep any further error lines that follow straight after the first.
        end = err
        while end + 1 < len(lines) and "##[error]" in lines[end + 1]:
            end += 1
        lines = lines[start : end + 1]
    tail = "\n".join(lines[-LOG_LINES:])
    # Parallel test output can push Go's failure report far above the tail.
    fails = go_failures(lines[:-LOG_LINES])
    if fails:
        fails = fails[: LOG_BYTES // 2]
        return f"{fails}\n...\n{tail[-(LOG_BYTES - len(fails) - 5):]}"
    return tail[-LOG_BYTES:]


def go_failures(lines: list[str]) -> str:
    """Each `--- FAIL` line and the indented detail under it."""
    keep = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("--- FAIL"):
            keep.add(i)
            j = i + 1
            while j < len(lines) and lines[j].startswith(("    ", "\t")):
                keep.add(j)
                j += 1
    return "\n".join(lines[i] for i in sorted(keep))


def fetch_checks(repo: str, sha: str) -> list[dict]:
    rows = []
    for run in gh_list(f"repos/{repo}/commits/{sha}/check-runs?filter=all", key="check_runs"):
        match = RUN_ID.search(run.get("details_url") or "")
        rows.append({
            "check_id": run["id"],
            "commit_sha": sha,
            "name": run["name"],
            "conclusion": run["conclusion"],
            "app": (run.get("app") or {}).get("slug"),
            "run_id": int(match.group(1)) if match else None,
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
            "output": run.get("output") or {},
            "failure_log": None,
        })
    return rows


def fetch_failure_log(repo: str, check: dict) -> str | None:
    """Actions jobs: tail of the job log. Other apps: the check's own output text."""
    if check["run_id"]:
        log = gh(f"repos/{repo}/actions/jobs/{check['check_id']}/logs")
        return trim_log(log) if log else None
    out = check["output"]
    text = "\n\n".join(p for p in (out.get("title"), out.get("summary"), out.get("text")) if p)
    return text[-LOG_BYTES:] or None


def fetch_threads(repo: str, pr: int) -> dict[int, tuple[int, int]]:
    """Map inline comment id -> (is_resolved, is_outdated) of its thread."""
    owner, name = repo.split("/")
    fields = ["-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={pr}"]
    pages = json.loads(gh("graphql", paginate=True, fields=fields))
    flags = {}
    for page in pages:
        for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
            for comment in thread["comments"]["nodes"]:
                flags[comment["databaseId"]] = (int(thread["isResolved"]), int(thread["isOutdated"]))
    return flags


def comment_row(pr: int, kind: str, comment_id: int, item: dict, **extra) -> dict:
    row = {
        "comment_id": comment_id,
        "pr_number": pr,
        "kind": kind,
        "author": (item.get("user") or {}).get("login"),
        "is_bot": is_bot(item.get("user")),
        "path": None,
        "line": None,
        "body": item.get("body") or "",
        "in_reply_to": None,
        "created_at": item.get("created_at") or item.get("submitted_at"),
        "review_state": None,
        "is_resolved": None,
        "is_outdated": None,
    }
    return row | extra


def fetch_comments(repo: str, pr: int) -> list[dict]:
    threads = fetch_threads(repo, pr)
    inline = [
        comment_row(
            pr, "inline", c["id"], c,
            path=c.get("path"),
            line=c.get("line") or c.get("original_line"),
            in_reply_to=c.get("in_reply_to_id"),
            is_resolved=threads.get(c["id"], (None, None))[0],
            is_outdated=threads.get(c["id"], (None, None))[1],
        )
        for c in gh_list(f"repos/{repo}/pulls/{pr}/comments")
    ]
    # Empty-bodied reviews still matter when they approve or block.
    reviews = [
        comment_row(pr, "review", REVIEW_ID_OFFSET + r["id"], r, review_state=r.get("state"))
        for r in gh_list(f"repos/{repo}/pulls/{pr}/reviews")
        if (r.get("body") or "").strip() or r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
    ]
    issue = [
        comment_row(pr, "issue", ISSUE_ID_OFFSET + c["id"], c)
        for c in gh_list(f"repos/{repo}/issues/{pr}/comments")
    ]
    return inline + reviews + issue


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], columns: list[str]) -> None:
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    conn.executemany(sql, [[row[c] for c in columns] for row in rows])


def fetch_pr(repo: str, number: int) -> dict:
    d = json.loads(gh(f"repos/{repo}/pulls/{number}"))
    return {
        "pr_number": d["number"],
        "title": d["title"],
        "state": d["state"],
        "merged": int(bool(d.get("merged"))),
        "author": (d.get("user") or {}).get("login"),
        "head_sha": d["head"]["sha"],
        "created_at": d["created_at"],
        "merged_at": d.get("merged_at"),
        "closed_at": d.get("closed_at"),
        "url": d["html_url"],
    }


def stored(conn: sqlite3.Connection, sql: str, params: dict | None = None) -> set:
    return {row[0] for row in conn.execute(sql, params or {})}


CHECK_COLUMNS = ["check_id", "commit_sha", "name", "conclusion", "app", "run_id", "started_at", "completed_at",
                 "failure_log"]


def fetch_checks_with_logs(pool: ThreadPoolExecutor, repo: str, shas: list[str], logs: dict,
                           known: set) -> tuple[list[dict], list[dict]]:
    """Checks on `shas`, reusing stored logs. Fetch logs only for failures whose check_id is not in `known`."""
    checks = [c for rows in pool.map(lambda s: fetch_checks(repo, s), shas) for c in rows]
    for check in checks:
        check["failure_log"] = logs.get(check["check_id"])
    failed = [c for c in checks if c["conclusion"] in ("failure", "timed_out") and c["check_id"] not in known]
    for check, log in zip(failed, pool.map(lambda c: fetch_failure_log(repo, c), failed)):
        check["failure_log"] = log
    return checks, failed


def refetch_checks(conn: sqlite3.Connection, repo: str, since: str, workers: int) -> None:
    """Re-list checks on stored commits tied to sessions active since `since`: their checkpoint
    commits and the heads of their PRs. Commits with no checks stored had none, so they are skipped."""
    shas = sorted(stored(conn, """
        SELECT DISTINCT k.commit_sha FROM gh_checks k WHERE k.commit_sha IN (
            SELECT cp.commit_sha FROM checkpoints cp JOIN sessions s ON s.session_id = cp.session_id
            WHERE s.last_activity_at >= :since
            UNION
            SELECT p.head_sha FROM gh_prs p JOIN gh_commit_prs g ON g.pr_number = p.pr_number
            JOIN checkpoints cp ON cp.commit_sha = g.commit_sha JOIN sessions s ON s.session_id = cp.session_id
            WHERE s.last_activity_at >= :since)""", {"since": since}))
    known = stored(conn, "SELECT check_id FROM gh_checks")
    logs = dict(conn.execute("SELECT check_id, failure_log FROM gh_checks WHERE failure_log IS NOT NULL"))
    print(f"re-listing checks on {len(shas)} commits of sessions active since {since}")
    with ThreadPoolExecutor(workers) as pool:
        checks, failed = fetch_checks_with_logs(pool, repo, shas, logs, known)
    with conn:
        upsert(conn, "gh_checks", checks, CHECK_COLUMNS)
    new = [c for c in checks if c["check_id"] not in known]
    print(f"{len(checks)} checks listed, {len(new)} new; {len(failed)} failure logs fetched, "
          f"{sum(c['failure_log'] is not None for c in failed)} found")
    print(f"{requests} gh requests")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default="entireio/cli", help="GitHub owner/name")
    parser.add_argument("--workers", type=int, default=8, help="Parallel gh calls")
    parser.add_argument("--refresh", action="store_true", help="Fetch everything, ignoring what is stored")
    parser.add_argument("--checks-since", metavar="YYYY-MM-DD",
                        help="Only re-list checks on stored commits of sessions active since this date")
    args = parser.parse_args()
    repo = args.repo

    conn = db.connect()
    conn.executescript(SEEN_SCHEMA)
    if args.checks_since:
        refetch_checks(conn, repo, args.checks_since, args.workers)
        conn.close()
        return
    shas = load_shas()
    if args.refresh:
        seen = settled = concluded = pending = set()
        logs = {}
    else:
        seen = stored(conn, "SELECT commit_sha FROM gh_commit_prs UNION SELECT commit_sha FROM gh_commits_seen")
        settled = stored(conn, "SELECT pr_number FROM settled_prs")
        # Commits with checks stored and none of them pending.
        concluded = stored(conn, "SELECT commit_sha FROM gh_checks GROUP BY commit_sha "
                                 "HAVING COUNT(*) = COUNT(conclusion)")
        # Commits with a check still pending, unless every PR holding them has settled:
        # Entire Gates can stay pending forever on old commits.
        pending = stored(conn, "SELECT commit_sha FROM gh_checks WHERE conclusion IS NULL") - stored(
            conn, "SELECT commit_sha FROM gh_commit_prs GROUP BY commit_sha "
                  "HAVING MIN(pr_number IN (SELECT pr_number FROM settled_prs)) = 1")
        logs = dict(conn.execute("SELECT check_id, failure_log FROM gh_checks WHERE failure_log IS NOT NULL"))
    new_shas = [s for s in shas if s not in seen]
    print(f"{len(shas)} checkpoint commits, {len(new_shas)} not looked up before")

    with ThreadPoolExecutor(args.workers) as pool:
        commit_prs = dict(zip(new_shas, pool.map(lambda s: gh_list(f"repos/{repo}/commits/{s}/pulls"), new_shas)))
        links = [{"commit_sha": s, "pr_number": p["number"]} for s, found in commit_prs.items() for p in found]
        print(f"{sum(not found for found in commit_prs.values())} new commits in no PR")

        known = stored(conn, "SELECT pr_number FROM gh_commit_prs")
        pr_numbers = sorted((known | {link["pr_number"] for link in links}) - settled)
        prs = list(pool.map(lambda n: fetch_pr(repo, n), pr_numbers))
        print(f"{len(pr_numbers)} PRs refreshed ({len(settled)} settled PRs skipped)")

        # Checks for new commits, commits with pending checks, and heads of unsettled PRs,
        # where a job may have been re-run. A seen commit with no checks stored has none
        # on GitHub, so it is skipped too.
        heads = {p["head_sha"] for p in prs if not (p["merged"] or p["state"] == "closed")}
        candidates = dict.fromkeys(shas + [p["head_sha"] for p in prs])
        check_shas = [s for s in candidates if s in pending or s in heads or (s not in seen and s not in concluded)]
        checks, failed = fetch_checks_with_logs(pool, repo, check_shas, logs, set(logs))

        comments = [c for rows in pool.map(lambda n: fetch_comments(repo, n), pr_numbers) for c in rows]

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        upsert(conn, "gh_prs", prs, ["pr_number", "title", "state", "merged", "author", "head_sha", "created_at",
                                     "merged_at", "closed_at", "url"])
        upsert(conn, "gh_commit_prs", links, ["commit_sha", "pr_number"])
        upsert(conn, "gh_commits_seen", [{"commit_sha": s, "looked_up_at": now} for s in new_shas],
               ["commit_sha", "looked_up_at"])
        upsert(conn, "gh_checks", checks, CHECK_COLUMNS)
        upsert(conn, "gh_review_comments", comments, ["comment_id", "pr_number", "author", "is_bot", "path",
                                                      "line", "body", "in_reply_to", "created_at", "kind",
                                                      "review_state", "is_resolved", "is_outdated"])
    conn.close()

    for p in prs:
        print(f"  #{p['pr_number']} {p['state']}{' merged' if p['merged'] else ''}  {p['title']}")
    print(f"{len(checks)} checks on {len(check_shas)} commits; {len(failed)} failure logs fetched, "
          f"{sum(c['failure_log'] is not None for c in failed)} found")
    bots = sum(c["is_bot"] for c in comments)
    print(f"{len(comments)} review comments on refreshed PRs: {bots} bot, {len(comments) - bots} human")
    print(f"{requests} gh requests")


if __name__ == "__main__":
    main()

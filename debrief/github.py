"""Load GitHub PRs, checks and review comments for the PRs your sessions produced.

Optional. Uses the logged-in `gh` CLI, read-only, and writes session_prs, gh_prs,
gh_commit_prs, gh_checks and gh_review_comments in debrief.db. Without a logged-in `gh`,
or with nothing to look up, it prints one line and does nothing.

With no --repo it links local sessions to PRs from what their own logs show: the PR URL
`gh pr create` printed, and the branches `git push` sent, looked up as PR heads. A PR closed
before the session started is ignored. Then it fetches every repo those PRs are in.
With --repo it looks up the checkpoint commits of sessions imported from Entire in that repo.

Re-runs skip what cannot change: branches and commits already looked up, settled PRs,
checks that have all concluded, and failure logs already stored. Heads of
unsettled PRs always get their checks re-listed, since a job can be re-run.

Check runs are listed with filter=all, so every attempt of a re-run job is a
separate gh_checks row; the default returns only the latest attempt.

    uv run python -m debrief.github                       # local sessions, every repo they pushed to
    uv run python -m debrief.github --dry-run             # say what the logs link to; no requests
    uv run python -m debrief.github --repo owner/name     # Entire checkpoints in one repo
    uv run python -m debrief.github --refresh             # ignore the skips and fetch everything
    uv run python -m debrief.github --repo owner/name --checks-since 2026-08-11
        # only re-list checks on stored commits of sessions active since then,
        # to pick up attempts that the old latest-only listing dropped
"""

import argparse
import functools
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import quote

from debrief import config, db
from debrief.normalize import SHELL_TOOLS, parse_ts

LOG_LINES = 150
LOG_BYTES = 12_000
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUN_ID = re.compile(r"/actions/runs/(\d+)")
# Both go into `gh api` paths, so anything else is refused.
SHA = re.compile(r"[0-9a-f]{7,40}")
REPO = re.compile(r"[A-Za-z0-9-]+/(?!\.\.?$)[\w.-]+")  # the name may hold dots but not be "." or ".."
# What a session's shell calls show of its PRs: the URL `gh pr create` prints, the repo and
# branch in `git push` output ("To github.com:o/n.git", "abc..def  local -> branch"), and the
# branch named in the push command, since `git push -q` prints nothing.
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
GITHUB_REMOTE = r"(?:(?:[\w.-]+@)?github\.com:|(?:https|ssh)://(?:[\w.-]+@)?github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?\s*$"
PUSH_TO = re.compile("^To " + GITHUB_REMOTE, re.MULTILINE)
PUSH_REF = re.compile(r"^[ +*!=-]*(?:[0-9a-f]{7,40}\.\.\.?[0-9a-f]{7,40}|\[new branch\])\s+\S+\s+->\s+(\S+)", re.MULTILINE)
PUSH_CMD = re.compile(r"\bgit push\b(?:\s+-\S+)*\s+origin\s+(?:[\w./-]+:)?(\w[\w./-]*)")
# A PR whose head is one of these is a release or sync PR, not the session's work.
# shortcut: a fixed list, not each repo's default branch; ask GitHub if a repo's trunk has another name.
TRUNKS = {"main", "master", "develop", "trunk", "HEAD"}
RECHECK = timedelta(days=2)  # how long after a session a pushed branch may still get its PR
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
CREATE TABLE IF NOT EXISTS gh_branches_seen (
    repo         TEXT NOT NULL,
    branch       TEXT NOT NULL,      -- pushed by a local session and looked up as a PR head
    looked_up_at TEXT,
    prs          INTEGER,            -- how many PRs had it as head
    PRIMARY KEY (repo, branch)
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


def valid_shas(shas) -> list[str]:
    """`shas` in order, without repeats or anything that isn't a hex commit id."""
    return [s for s in dict.fromkeys(shas) if s and SHA.fullmatch(s)]


def load_shas(conn: sqlite3.Connection) -> list[str]:
    return valid_shas(r[0] for r in conn.execute("SELECT commit_sha FROM checkpoints ORDER BY session_id, ts"))


def gh_problem() -> str | None:
    """Why `gh` can't be used, or None when it is installed and logged in."""
    if not shutil.which("gh"):
        return "the gh CLI is not installed"
    if subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
        return "gh is not logged in (run `gh auth login`)"
    return None


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


@functools.cache
def remote_repo(cwd: str) -> str | None:
    """owner/name of the checkout's GitHub origin, read from disk."""
    out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    m = re.search(GITHUB_REMOTE, out.stdout)
    return m.group(1) if m else None


def find_links(conn: sqlite3.Connection) -> tuple[set, dict]:
    """What the logs say, with no requests: {(session_id, repo, pr_number)} from `gh pr create`
    output, and {(repo, branch): {session_id}} for the branches sessions pushed."""
    calls: dict[str, list] = {}
    marks = ", ".join("?" * (len(SHELL_TOOLS) + 1))
    for r in conn.execute(
            "SELECT t.session_id, s.cwd, t.input, t.output FROM tool_calls t JOIN sessions s USING (session_id) "
            f"WHERE lower(t.name) IN ({marks}) AND (t.input LIKE '%git push%' OR t.input LIKE '%gh pr create%')",
            [*SHELL_TOOLS, "exec"]):
        calls.setdefault(r["session_id"], []).append(r)
    prs, pushes = set(), {}
    for sid, rows in calls.items():
        created = {(repo, int(n)) for r in rows if "gh pr create" in r["input"] for repo, n in PR_URL.findall(r["output"] or "")}
        prs |= {(sid, repo, n) for repo, n in created if REPO.fullmatch(repo)}
        # The session's repo when a push does not print it: the one its other calls name, else the checkout's origin.
        named = {repo for repo, _ in created} | {repo for r in rows for repo in PUSH_TO.findall(r["output"] or "")}
        for r in rows:
            if "git push" not in r["input"]:
                continue
            out = r["output"] or ""
            repo = next(iter(PUSH_TO.findall(out)), None) or (next(iter(named)) if len(named) == 1 else remote_repo(r["cwd"] or ""))
            for branch in {b.removeprefix("refs/heads/") for b in PUSH_REF.findall(out) + PUSH_CMD.findall(r["input"])} - TRUNKS:
                if repo and REPO.fullmatch(repo):
                    pushes.setdefault((repo, branch), set()).add(sid)
    return prs, pushes


def link_sessions(conn: sqlite3.Connection, pool: ThreadPoolExecutor, prs: set, pushes: dict, refresh: bool) -> None:
    """Write session_prs: the PRs the logs name, plus the PRs whose head is a branch a session pushed."""
    spans = {r[0]: (r[1] or "", r[2] or "") for r in conn.execute("SELECT session_id, started_at, last_activity_at FROM sessions")}
    seen = {} if refresh else {(r["repo"], r["branch"]): r for r in conn.execute("SELECT * FROM gh_branches_seen")}

    def settled(key) -> bool:
        """Looked up since the last session that pushed it ended, and a PR was found or RECHECK has passed."""
        row, last = seen.get(key), parse_ts(max(spans[s][1] for s in pushes[key]))
        if not row or not last:
            return False
        waited = parse_ts(row["looked_up_at"]) - last
        return waited > RECHECK or (bool(row["prs"]) and waited > timedelta(0))

    todo = [key for key in sorted(pushes) if not settled(key)]
    found = list(pool.map(lambda key: json.loads(gh(
        f"repos/{key[0]}/pulls?state=all&per_page=100&head={key[0].split('/')[0]}:{quote(key[1], safe='')}") or "[]"), todo))
    rows = [(sid, repo, n, "url") for sid, repo, n in sorted(prs)]
    for key, heads in zip(todo, found):
        # A PR closed before the session began reused the branch name; it is not this session's.
        rows += [(sid, key[0], p["number"], "branch") for p in heads for sid in sorted(pushes[key])
                 if not p.get("closed_at") or p["closed_at"][:19] >= spans[sid][0][:19]]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        conn.executemany("INSERT OR IGNORE INTO session_prs VALUES (?, ?, ?, ?)", rows)
        conn.executemany("INSERT OR REPLACE INTO gh_branches_seen VALUES (?, ?, ?, ?)",
                         [(*key, now, len(heads)) for key, heads in zip(todo, found)])
    linked = conn.execute("SELECT COUNT(DISTINCT session_id), COUNT(DISTINCT repo || '#' || pr_number), "
                          "COUNT(DISTINCT repo) FROM session_prs").fetchone()
    print(f"{len(todo)} pushed branches looked up ({len(pushes) - len(todo)} skipped); "
          f"{linked[0]} sessions linked to {linked[1]} PRs in {linked[2]} repos")


def fetch_checks(repo: str, sha: str) -> list[dict]:
    rows = []
    for run in gh_list(f"repos/{repo}/commits/{sha}/check-runs?filter=all", key="check_runs"):
        match = RUN_ID.search(run.get("details_url") or "")
        rows.append({
            "check_id": run["id"],
            "repo": repo,
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


def comment_row(repo: str, pr: int, kind: str, comment_id: int, item: dict, **extra) -> dict:
    row = {
        "comment_id": comment_id,
        "repo": repo,
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
            repo, pr, "inline", c["id"], c,
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
        comment_row(repo, pr, "review", REVIEW_ID_OFFSET + r["id"], r, review_state=r.get("state"))
        for r in gh_list(f"repos/{repo}/pulls/{pr}/reviews")
        if (r.get("body") or "").strip() or r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
    ]
    issue = [
        comment_row(repo, pr, "issue", ISSUE_ID_OFFSET + c["id"], c)
        for c in gh_list(f"repos/{repo}/issues/{pr}/comments")
    ]
    return inline + reviews + issue


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], columns: list[str]) -> None:
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    conn.executemany(sql, [[row[c] for c in columns] for row in rows])


def fetch_pr(repo: str, number: int) -> dict | None:
    """One PR, or None when GitHub has none by that number that this login can see."""
    body = gh(f"repos/{repo}/pulls/{number}")
    if body is None:
        return None
    d = json.loads(body)
    return {
        "repo": repo,
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


def stored(conn: sqlite3.Connection, sql: str, params: dict | tuple = ()) -> set:
    return {row[0] for row in conn.execute(sql, params)}


CHECK_COLUMNS = ["check_id", "repo", "commit_sha", "name", "conclusion", "app", "run_id", "started_at", "completed_at",
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
    shas = valid_shas(sorted(stored(conn, """
        SELECT DISTINCT k.commit_sha FROM gh_checks k WHERE k.commit_sha IN (
            SELECT cp.commit_sha FROM checkpoints cp JOIN sessions s ON s.session_id = cp.session_id
            WHERE s.last_activity_at >= :since
            UNION
            SELECT p.head_sha FROM gh_prs p JOIN gh_commit_prs g ON g.repo = p.repo AND g.pr_number = p.pr_number
            JOIN checkpoints cp ON cp.commit_sha = g.commit_sha JOIN sessions s ON s.session_id = cp.session_id
            WHERE s.last_activity_at >= :since)""", {"since": since})))
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


def fetch_repo(conn: sqlite3.Connection, pool: ThreadPoolExecutor, repo: str, shas: list[str], refresh: bool) -> None:
    """Fetch one repo: the PRs holding checkpoint commits `shas` and the PRs local sessions link to."""
    local = stored(conn, "SELECT pr_number FROM session_prs WHERE repo = ?", (repo,))
    if refresh:
        seen = settled = concluded = pending = set()
        logs = {}
    else:
        seen = stored(conn, "SELECT commit_sha FROM gh_commit_prs UNION SELECT commit_sha FROM gh_commits_seen")
        settled = stored(conn, "SELECT pr_number FROM settled_prs WHERE repo = ?", (repo,))
        # Commits with checks stored and none of them pending.
        concluded = stored(conn, "SELECT commit_sha FROM gh_checks GROUP BY commit_sha "
                                 "HAVING COUNT(*) = COUNT(conclusion)")
        # Commits with a check still pending, unless every PR holding them has settled:
        # Entire Gates can stay pending forever on old commits.
        pending = stored(conn, "SELECT commit_sha FROM gh_checks WHERE conclusion IS NULL") - stored(
            conn, "SELECT commit_sha FROM gh_commit_prs g GROUP BY commit_sha HAVING MIN(EXISTS ("
                  "SELECT 1 FROM settled_prs sp WHERE sp.repo = g.repo AND sp.pr_number = g.pr_number)) = 1")
        logs = dict(conn.execute("SELECT check_id, failure_log FROM gh_checks WHERE failure_log IS NOT NULL"))
    new_shas = [s for s in shas if s not in seen]
    print(f"{repo}: {len(shas)} checkpoint commits, {len(new_shas)} not looked up before; {len(local)} PRs from local sessions")

    commit_prs = dict(zip(new_shas, pool.map(lambda s: gh_list(f"repos/{repo}/commits/{s}/pulls"), new_shas)))
    links = [{"commit_sha": s, "pr_number": p["number"], "committed_at": None} for s, found in commit_prs.items() for p in found]
    if new_shas:
        print(f"{sum(not found for found in commit_prs.values())} new commits in no PR")

    known = stored(conn, "SELECT pr_number FROM gh_commit_prs WHERE repo = ?", (repo,))
    pr_numbers = sorted((known | {link["pr_number"] for link in links} | local) - settled)
    prs = [p for p in pool.map(lambda n: fetch_pr(repo, n), pr_numbers) if p]
    print(f"{len(prs)} PRs refreshed ({len(settled)} settled PRs skipped)")
    if len(prs) < len(pr_numbers):
        print(f"{len(pr_numbers) - len(prs)} PRs not found: gone, or this gh login cannot read {repo}")
    pr_numbers = [p["pr_number"] for p in prs]
    # A local session names its PR, not its commits: list them, with dates, so each session's
    # checks can be told from another's on the same PR (db.session_commits).
    listed = [n for n in pr_numbers if n in local]
    for n, commits in zip(listed, pool.map(lambda n: gh_list(f"repos/{repo}/pulls/{n}/commits"), listed)):
        links += [{"commit_sha": c["sha"], "pr_number": n, "committed_at": c["commit"]["committer"]["date"]}
                  for c in commits]

    # Checks for new commits, commits with pending checks, and heads of unsettled PRs,
    # where a job may have been re-run. A seen commit with no checks stored has none
    # on GitHub, so it is skipped too.
    heads = {p["head_sha"] for p in prs if not (p["merged"] or p["state"] == "closed")}
    candidates = valid_shas(shas + [p["head_sha"] for p in prs] + [link["commit_sha"] for link in links])
    check_shas = [s for s in candidates if s in pending or s in heads or (s not in seen and s not in concluded)]
    checks, failed = fetch_checks_with_logs(pool, repo, check_shas, logs, set(logs))

    comments = [c for rows in pool.map(lambda n: fetch_comments(repo, n), pr_numbers) for c in rows]

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with conn:
        upsert(conn, "gh_prs", prs, ["repo", "pr_number", "title", "state", "merged", "author", "head_sha",
                                     "created_at", "merged_at", "closed_at", "url"])
        upsert(conn, "gh_commit_prs", [link | {"repo": repo} for link in links],
               ["commit_sha", "repo", "pr_number", "committed_at"])
        upsert(conn, "gh_commits_seen", [{"commit_sha": s, "looked_up_at": now} for s in new_shas],
               ["commit_sha", "looked_up_at"])
        upsert(conn, "gh_checks", checks, CHECK_COLUMNS)
        upsert(conn, "gh_review_comments", comments, ["comment_id", "repo", "pr_number", "author", "is_bot", "path",
                                                      "line", "body", "in_reply_to", "created_at", "kind",
                                                      "review_state", "is_resolved", "is_outdated"])

    for p in prs:
        print(f"  #{p['pr_number']} {p['state']}{' merged' if p['merged'] else ''}  {p['title']}")
    print(f"{len(checks)} checks on {len(check_shas)} commits; {len(failed)} failure logs fetched, "
          f"{sum(c['failure_log'] is not None for c in failed)} found")
    bots = sum(c["is_bot"] for c in comments)
    print(f"{len(comments)} review comments on refreshed PRs: {bots} bot, {len(comments) - bots} human")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="GitHub owner/name the Entire checkpoint commits belong to "
                                       "(default: the repos local sessions pushed to)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel gh calls")
    parser.add_argument("--refresh", action="store_true", help="Fetch everything, ignoring what is stored")
    parser.add_argument("--dry-run", action="store_true", help="Say what local sessions link to; make no requests")
    parser.add_argument("--checks-since", metavar="YYYY-MM-DD",
                        help="With --repo: only re-list checks on stored commits of sessions active since this date")
    args = parser.parse_args(argv)
    repo = args.repo
    if repo and not REPO.fullmatch(repo):
        sys.exit(f"github: --repo must be owner/name, got {repo!r}")
    if args.checks_since and not repo:
        sys.exit("github: --checks-since needs --repo")

    conn = db.connect()
    conn.executescript(SEEN_SCHEMA)
    shas = load_shas(conn) if repo else []
    prs, pushes = (set(), {}) if repo else find_links(conn)
    # Only the repos of picked projects: those a session run in a picked folder opened a PR in or
    # pushed to. Other sessions' links to those repos are kept, so a worktree session still links.
    picked = {r[0] for r in conn.execute(
        f"SELECT session_id FROM sessions WHERE cwd IN ({', '.join('?' * len(config.projects()))})", config.projects())}
    repos = {p[1] for p in prs if p[0] in picked} | {k[0] for k, sids in pushes.items() if sids & picked}
    prs, pushes = {p for p in prs if p[1] in repos}, {k: v for k, v in pushes.items() if k[0] in repos}
    if args.dry_run:
        print(f"github: dry run, no requests made. {len({p[1:] for p in prs})} PRs named in the logs of "
              f"{len({p[0] for p in prs})} sessions, {len(pushes)} pushed branches to look up, "
              f"{len({p[1] for p in prs} | {k[0] for k in pushes})} repos")
        conn.close()
        return
    # Nothing to do is not an error: GitHub data is optional and the site hides what is missing.
    if repo:
        skip = None if shas else "no checkpoint commits to look up"
    else:
        skip = None if prs or pushes else "no session of a picked project pushed a branch or opened a pull request"
    skip = skip or gh_problem()
    if skip:
        print(f"github: skipped, {skip}")
        conn.close()
        return
    if args.checks_since:
        refetch_checks(conn, repo, args.checks_since, args.workers)
        conn.close()
        return
    with ThreadPoolExecutor(args.workers) as pool:
        if not repo:
            link_sessions(conn, pool, prs, pushes, args.refresh)
        for name in [repo] if repo else sorted(stored(conn, "SELECT repo FROM session_prs") & repos):
            fetch_repo(conn, pool, name, shas, args.refresh)
    conn.close()
    print(f"{requests} gh requests")


if __name__ == "__main__":
    main()

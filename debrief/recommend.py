"""Merge one findings run into recommendations, grouped by the lever that fixes them.

Findings come per pipeline kind (hotspot, ci, ...), so one problem can show up under
several kinds. One Claude call reads every finding in a run and returns recommendations:
each names the lever (where the fix lives), the change to make, its effort, and the
findings it covers. Every finding lands in exactly one recommendation, except that a finding
listing several bugs may back one backlog recommendation per bug; any the model leaves out become their own.

    uv run python -m debrief.recommend                      # latest findings run
    uv run python -m debrief.recommend --run 20260925T182434Z --dry-run
"""

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, datetime

from debrief.config import choose, load_env
from debrief.db import connect
from debrief.synthesize import ClaudeCodeError, ask_claude_code, parse_json

LEVERS = {
    "agent_config": "Agent instructions: CLAUDE.md, AGENTS.md, skills, slash commands",
    "test_harness": "Tests and the local dev loop: test isolation, fixtures, task runner and scripts",
    "ci_infra": "CI workflows, runners, caching, tool pins, log output",
    "review_tooling": "Automated code review and its config: review bots, their rules and noise",
    "team_practice": "How the people prompt and run sessions: habits, templates, docs for humans",
    "backlog": "A product or code bug an agent flagged in a session and didn't file during it: check it, file it",
}

# Where a finding goes when the model leaves it out.
KIND_LEVER = {"found_not_filed": "backlog", "ci": "ci_infra", "review": "review_tooling",
              "prompt_practice": "team_practice"}

SYSTEM = f"""You turn findings about how a developer or a team works with AI coding agents into a short list
of ideas for them. "team" in the input gives the number of people; it may be one, so don't write as if there
were several. Take the repo, language and tools from the findings; assume none. Ideas are leads to
check, not verdicts: the labels behind them are from a classifier and some will be false positives. Suggest;
don't tell them what they should do.

Each finding came from labeled agent sessions and, when the data has them, CI checks or PR review
comments, and cites evidence.
Several findings often describe one problem from different angles (a hotspot file, a correction
pattern and a knowledge gap can all be the same missing doc). Merge those.

Levers (where the fix lives):
{chr(10).join(f"- {k}: {v}" for k, v in LEVERS.items())}

Counts:
- Each finding's "evidence" gives refs, sessions and people: the cited items, the sessions behind them
  and the people who ran those sessions. Use them, and "team", to judge how much weight a finding
  carries. Don't quote them; the dashboard counts reach itself.
- Counts inside finding text often describe a whole category or an unnamed subset, not one problem.

Rules:
- Assign every finding id to exactly one recommendation. The one exception: if a finding lists several
  unrelated bugs, split it into one backlog recommendation per bug, each citing that finding.
- Backlog: exactly one recommendation per distinct bug, unless another recommendation's change already
  fixes it. Never group bugs, even small, related or single-session ones, and even if one session flagged
  them together; each backlog title names one bug, never two joined by "and".
  Say the bug was flagged in a session and not filed during it; we can't see the issue tracker, so
  don't say nobody filed it.
- "change" is the suggested change. For agent_config, give the exact text to add and the file.
  For others, name the file, workflow, setting or command. Markdown is fine; keep it under 120 words.
- "problem" says what goes wrong, in two or three plain sentences. It may use counts only about this
  specific problem, each with its unit named (messages, sessions, CI runs, review comments). Never quote
  category-wide totals, never a count out of an unnamed subset ("31 of 80 failures"), and never add counts
  of different units together. Never give a share of an unnamed or unsized set ("most of the sampled
  ones", "most of these passed", "most log tails", "many of them"): name the count and its unit ("3 of 5
  CI runs"), or say it without a share ("some passed on re-run"). If unsure, describe it
  without numbers.
- Never state a rate, ratio, percentage or per-session average ("1.8 corrections per session", "twice as
  often", "40% of runs"), even if a finding gives one: the dashboard computes rates itself, and not always
  the same way. Use only raw counts of this problem's own items, with the unit named ("corrected in 6
  messages across 4 sessions").
- "impact" is one neutral line saying what the problem is and where it shows up, with no numbers at all
  (e.g. "People paste review bot findings into the session by hand, across several sessions").
  Don't promise an outcome ("removes", "saves").
- effort: S (under an hour), M (a day or less), L (more).
- Plain, short sentences. No hype. Do not invent facts that are not in the findings.

Reply with JSON only:
{{"recommendations": [{{"lever": "<lever>", "title": "<imperative, under 70 chars>",
  "problem": "...", "change": "...", "target": "<file or system the change lands in>",
  "effort": "S|M|L", "impact": "...", "finding_ids": [<ids>]}}]}}"""


def latest_run(conn) -> str:
    return conn.execute("SELECT MAX(run_id) FROM findings").fetchone()[0]


def evidence_stats(conn, evidence: list[dict]) -> dict:
    """Sessions and people behind a finding's evidence, events mapped to sessions via their commits or PR."""
    sessions = {e["session_id"] for e in evidence if "session_id" in e}
    for e in evidence:
        if e.get("event_type") == "check_failure":
            sessions |= {r[0] for r in conn.execute(
                "SELECT DISTINCT cp.session_id FROM gh_checks k JOIN session_commits cp ON cp.commit_sha = k.commit_sha "
                "WHERE k.check_id = ?", (e["event_id"],))}
        elif e.get("event_type") == "review_comment":
            sessions |= {r[0] for r in conn.execute(
                "SELECT DISTINCT l.session_id FROM gh_review_comments c "
                "JOIN session_pr_links l ON l.repo = c.repo AND l.pr_number = c.pr_number "
                "WHERE c.comment_id = ?", (e["event_id"],))}
    people = {r[0] for s in sessions for r in conn.execute(
        "SELECT author FROM sessions WHERE session_id = ? AND author IS NOT NULL", (s,))}
    return {"refs": len(evidence), "sessions": len(sessions), "people": len(people)}


def load_findings(conn, run_id: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY finding_id", (run_id,)):
        evidence = json.loads(r["evidence"] or "[]")
        out.append({"id": r["finding_id"], "kind": r["kind"], "title": r["title"], "detail": r["detail"],
                    "suggested_fix": r["suggested_fix"], "evidence": evidence_stats(conn, evidence)})
    return out


def team_context(conn) -> dict:
    sessions, people = conn.execute("SELECT COUNT(*), COUNT(DISTINCT author) FROM sessions").fetchone()
    human = conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'user' AND user_kind = 'human'").fetchone()[0]
    return {"sessions": sessions, "people": people, "human_turns": human}


# A backlog idea that reads like a list of bugs rather than one.
BUNDLED = re.compile(r"\b(bugs|several|multiple|various)\b", re.IGNORECASE)
# A share of a set the reader can't size.
SHARE = re.compile(r"\b(most|many) (of|\w+s\b)", re.IGNORECASE)
# A rate or ratio: a decimal, a percentage, "per session", "3x", "twice as".
RATE = re.compile(r"\d\.\d|\d\s*%|\bper (session|run|message|turn|pr)\b|\b\d+x\b|\b(twice|thrice) as\b",
                  re.IGNORECASE)


def check(recs: list[dict], findings: list[dict]) -> list[dict]:
    """Keep known finding ids, each in one recommendation (or several backlog ones); add one per orphan."""
    by_id = {f["id"]: f for f in findings}
    owner: dict[int, str] = {}  # finding id -> lever of the first recommendation that claimed it
    kept = []
    for r in recs:
        backlog = r.get("lever") == "backlog"
        ids = [i for i in r.get("finding_ids") or []
               if i in by_id and (i not in owner or (backlog and owner[i] == "backlog"))]
        for i in ids:
            owner.setdefault(i, r.get("lever"))
        if not ids:
            print(f"dropped recommendation with no known findings: {r.get('title')!r}")
            continue
        if r.get("lever") not in LEVERS:
            r["lever"] = KIND_LEVER.get(by_id[ids[0]]["kind"], "agent_config")
        kept.append({**r, "finding_ids": ids})
    numbered = [r for r in kept if re.search(r"\b\d", r.get("impact") or "")]
    for r in numbered:
        print(f"warning: impact has a number: {r.get('title')!r}: {r['impact']!r}")
    if numbered:
        print(f"{len(numbered)} of {len(kept)} impact lines have numbers; the prompt asks for none")
    for r in kept:
        if r["lever"] == "backlog" and BUNDLED.search(f"{r.get('title')} {r.get('problem')}"):
            print(f"warning: backlog idea may bundle several bugs: {r.get('title')!r}")
        if m := SHARE.search(f"{r.get('title')} {r.get('impact')} {r.get('problem')}"):
            print(f"warning: share of an unsized set ({m.group(0)!r}): {r.get('title')!r}")
        if m := RATE.search(f"{r.get('title')} {r.get('impact')} {r.get('problem')}"):
            print(f"warning: rate or ratio ({m.group(0)!r}): {r.get('title')!r}")
    for f in findings:
        if f["id"] not in owner:
            print(f"finding {f['id']} left out by the model; kept as its own recommendation")
            kept.append({"lever": KIND_LEVER.get(f["kind"], "agent_config"), "title": f["title"],
                         "problem": f["detail"], "change": f["suggested_fix"], "target": "", "effort": "M",
                         "impact": "", "finding_ids": [f["id"]]})
    # The UI sorts by reach; priority just keeps the model's order.
    for i, r in enumerate(kept, 1):
        r["priority"] = i
    return kept


def save(conn, run_id: str, recs: list[dict]) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute("DELETE FROM recommendations WHERE run_id = ?", (run_id,))
    conn.executemany(
        "INSERT INTO recommendations (run_id, priority, lever, title, problem, change, target, effort, impact, "
        "finding_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, r["priority"], r["lever"], r.get("title"), r.get("problem"), r.get("change"), r.get("target"),
          r.get("effort"), r.get("impact"), json.dumps(r["finding_ids"]), now) for r in recs])
    conn.commit()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", help="Findings run_id (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="Print the prompt; no model call")
    args = parser.parse_args(argv)
    load_env()

    conn = connect()
    run_id = args.run or latest_run(conn)
    if not run_id:
        sys.exit("no findings yet; run synthesize.py")
    findings = load_findings(conn, run_id)
    user = json.dumps({"team": team_context(conn), "findings": findings}, ensure_ascii=False)
    print(f"run {run_id}: {len(findings)} findings, ~{(len(SYSTEM) + len(user)) // 4} input tokens")
    if args.dry_run:
        print(user[:4000])
        return
    if not shutil.which("claude"):
        sys.exit("claude (Claude Code) is not on PATH")

    _, model = choose("recommend")
    try:
        raw, usage = ask_claude_code(user, model, system=SYSTEM,
                                     parse=lambda t: parse_json(t).get("recommendations", []))
    except ClaudeCodeError as e:
        sys.exit(f"failed: {e}")
    recs = check(raw, findings)
    save(conn, run_id, recs)
    print(f"{len(recs)} recommendations saved for run {run_id} ({usage['in']} in / {usage['out']} out tokens, "
          f"~${usage['cost']:.4f} API-equivalent)")
    for r in recs:
        print(f"{r['priority']:>2}. [{r['lever']}/{r.get('effort')}] {r['title']}  <- {r['finding_ids']}")


if __name__ == "__main__":
    main()

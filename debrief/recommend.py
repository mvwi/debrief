"""Merge one findings run into recommendations, grouped by the lever that fixes them.

Findings come per project and pipeline kind (hotspot, ci, ...), so one problem can show up
under several kinds. Each project gets one Claude call over its findings, and the findings
about the person's habits across projects get one more. A call returns recommendations:
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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from debrief.config import choose, load_env, projects
from debrief.db import connect, session_projects
from debrief.synthesize import ClaudeCodeError, ask_claude_code, parse_json, short_name

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

Projects: "project" on the input says every finding is about that one repo. Without it, the findings
are about the person's habits across their repos.

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
- "target" starts with the project's name and a colon when the input names a project
  ("canopy: CLAUDE.md", "canopy: .github/workflows/ci.yml"); no prefix when it does not.
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
                    "suggested_fix": r["suggested_fix"], "evidence": evidence_stats(conn, evidence),
                    "project": r["project"]})
    return out


def team_context(conn, picked: set[str]) -> dict:
    """Sessions, people and human turns in these projects."""
    ids = [s for s, p in session_projects(conn).items() if p in picked]
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS team (session_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM team")
    conn.executemany("INSERT INTO team VALUES (?)", [(s,) for s in ids])
    people = conn.execute("SELECT COUNT(DISTINCT author) FROM sessions JOIN team USING (session_id)").fetchone()[0]
    human = conn.execute("SELECT COUNT(*) FROM messages JOIN team USING (session_id) "
                         "WHERE role = 'user' AND user_kind = 'human'").fetchone()[0]
    return {"sessions": len(ids), "people": people, "human_turns": human}


def groups(findings: list[dict], picked: list[str]) -> dict[str | None, list[dict]]:
    """Findings per model call: one per picked project, and one (None) for those about all of them.
    Findings of a project no longer picked are left out."""
    out: dict[str | None, list[dict]] = {}
    for f in findings:
        if f["project"] in (None, *picked):
            out.setdefault(f["project"], []).append(f)
    return out


def prompt(conn, project: str | None, findings: list[dict]) -> str:
    """The user message for one call. A project goes by folder name; the full path stays here."""
    body = {"team": team_context(conn, {project} if project else set(projects()))}
    if project:
        body["project"] = short_name(project)
    return json.dumps({**body, "findings": [{k: v for k, v in f.items() if k != "project"} for f in findings]},
                      ensure_ascii=False)


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
    for r in kept:
        r["project"] = by_id[r["finding_ids"][0]]["project"]  # one call holds one project's findings
    return kept


def save(conn, run_id: str, recs: list[dict]) -> None:
    now = datetime.now(UTC).isoformat()
    # The UI sorts by reach; priority just keeps the order the ideas came in.
    for i, r in enumerate(recs, 1):
        r["priority"] = i
    conn.execute("DELETE FROM recommendations WHERE run_id = ?", (run_id,))
    conn.executemany(
        "INSERT INTO recommendations (run_id, priority, lever, title, problem, change, target, effort, impact, "
        "finding_ids, project, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, r["priority"], r["lever"], r.get("title"), r.get("problem"), r.get("change"), r.get("target"),
          r.get("effort"), r.get("impact"), json.dumps(r["finding_ids"]), r.get("project"), now) for r in recs])
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
    prompts = {p: (fs, prompt(conn, p, fs)) for p, fs in groups(load_findings(conn, run_id), projects()).items()}
    print(f"run {run_id}: {len(prompts)} model calls")
    for p, (fs, user) in prompts.items():
        print(f"  {short_name(p) if p else 'across projects'}: {len(fs)} findings, "
              f"~{(len(SYSTEM) + len(user)) // 4} input tokens")
    if args.dry_run:
        print(next(iter(prompts.values()), ("", ""))[1][:4000])
        return
    if not shutil.which("claude"):
        sys.exit("claude (Claude Code) is not on PATH")

    _, model = choose("recommend")
    with ThreadPoolExecutor(max_workers=2) as pool:  # 2, as in synthesize: the calls share subscription limits
        futures = {p: pool.submit(ask_claude_code, user, model, system=SYSTEM,
                                  parse=lambda t: parse_json(t).get("recommendations", []))
                   for p, (_, user) in prompts.items()}
    recs, tokens_in, tokens_out, cost, failed = [], 0, 0, 0.0, 0
    for p, fut in futures.items():
        try:
            raw, usage = fut.result()
        except ClaudeCodeError as e:
            # Keep the run whole: a group the model failed on still gets one idea per finding.
            print(f"{short_name(p) if p else 'across projects'}: failed: {e}")
            raw, usage, failed = [], {"in": 0, "out": 0, "cost": 0}, failed + 1
            if failed == len(futures):
                sys.exit("failed: no call succeeded; recommendations left as they were")
        recs += check(raw, prompts[p][0])
        tokens_in, tokens_out, cost = tokens_in + (usage["in"] or 0), tokens_out + (usage["out"] or 0), cost + usage["cost"]
    save(conn, run_id, recs)
    print(f"{len(recs)} recommendations saved for run {run_id} ({tokens_in} in / {tokens_out} out tokens, "
          f"~${cost:.4f} API-equivalent)")
    for r in recs:
        where = f" {short_name(r['project'])}" if r["project"] else ""
        print(f"{r['priority']:>2}. [{r['lever']}/{r.get('effort')}{where}] {r['title']}  <- {r['finding_ids']}")


if __name__ == "__main__":
    main()

"""Check each idea's cited evidence against the idea itself.

An idea (a row in recommendations) cites the evidence of every finding it covers, so it can
inherit items that are about something else: a finding that lists eight bugs, split into eight
backlog ideas, gives each idea all of its messages. One model call per idea reads the idea's
title and problem plus each cited item, resolved as export_site.py shows it, and says which
items directly support this idea. Results land in evidence_checks, one row per (idea, item);
export_site.py then counts reach from supported items only.

Ideas that already have check rows are skipped unless --force.

    uv run python -m debrief.verify_evidence                    # latest recommendations run
    uv run python -m debrief.verify_evidence --run 20260925T182434Z --force
    uv run python -m debrief.verify_evidence --dry-run          # print the first prompt; no model call
"""

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from dotenv import load_dotenv

from debrief.db import connect
from debrief.export_site import labels_by_turn, ref_id, resolve
from debrief.synthesize import ClaudeCodeError, ask_claude_code, parse_json

MODEL = "claude-sonnet-5"

SYSTEM = """You check evidence for one idea from a report on how a developer or a team works with AI coding
agents. The idea names a specific problem. It cites items: human messages in agent sessions (with the agent
text just before them), failed CI runs (with a log tail) and PR review comments.

For each item, decide whether it directly supports this idea's specific claim: the item shows the problem
the idea describes happening, or names the exact bug. An item about a different bug, a different file or
area, or a different kind of problem does not support it, even if it is related or came from the same
session. Judge each item on its own content, not on the idea's wording.

Reply with JSON only:
{"supports": ["<item id>", ...], "reasons": {"<item id>": "<under 15 words: why it does or doesn't>"}}
Give a reason for every item."""


def item_view(ref: dict) -> dict:
    """The parts of a resolved ref the model reads."""
    if ref["type"] == "msg":
        return {"type": "human message", "agent_before": ref["agent_before"], "text": ref["text"]}
    if ref["type"] == "check":
        return {"type": "failed CI run", "check": ref["name"], "failure_class": ref["failure_class"],
                "log_tail": ref["log"]}
    if ref["type"] == "review":
        return {"type": "review comment", "path": ref["path"], "body": ref["body"], "held_up": ref["held_up"]}
    return {"type": "missing"}


def idea_refs(conn, labels, rec, findings: dict[int, list[dict]]) -> dict[str, dict]:
    """ref id -> resolved ref, over the idea's findings, in the order export_site.py lists them."""
    out: dict[str, dict] = {}
    for fid in json.loads(rec["finding_ids"] or "[]"):
        for ref in findings.get(fid, []):
            if ref_id(ref) not in out:
                out[ref_id(ref)] = resolve(conn, labels, ref)
    return out


def prompt(rec, refs: dict[str, dict]) -> tuple[str, dict[str, str]]:
    """The user prompt, and short item ids (i1, i2, ...) -> ref ids."""
    short = {f"i{n}": r for n, r in enumerate(refs, 1)}
    body = {"idea": {"title": rec["title"], "problem": rec["problem"]},
            "items": [{"id": s, **item_view(refs[r])} for s, r in short.items()]}
    return json.dumps(body, ensure_ascii=False), short


def parse(text: str) -> list[dict]:
    reply = parse_json(text)
    return [reply] if isinstance(reply, dict) and isinstance(reply.get("supports"), list) else []


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", help="Recommendations run_id (default: latest)")
    parser.add_argument("--force", action="store_true", help="Check ideas that already have check rows")
    parser.add_argument("--parallel", type=int, default=3, help="Concurrent model calls (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="Print the first prompt; no model call")
    args = parser.parse_args(argv)
    load_dotenv()

    conn = connect()
    run_id = args.run or conn.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0]
    if not run_id:
        sys.exit("no recommendations yet; run recommend.py")
    findings = {r["finding_id"]: json.loads(r["evidence"] or "[]")
                for r in conn.execute("SELECT finding_id, evidence FROM findings WHERE run_id = ?", (run_id,))}
    recs = conn.execute("SELECT * FROM recommendations WHERE run_id = ? ORDER BY priority", (run_id,)).fetchall()
    checked = {r[0] for r in conn.execute("SELECT DISTINCT rec_id FROM evidence_checks")}
    todo = [r for r in recs if args.force or r["rec_id"] not in checked]
    print(f"run {run_id}: {len(recs)} ideas, {len(recs) - len(todo)} already checked, {len(todo)} to check")
    if not todo:
        return

    labels = labels_by_turn(conn)
    jobs = {}
    for rec in todo:
        refs = idea_refs(conn, labels, rec, findings)
        if refs:
            jobs[rec["rec_id"]] = (rec, refs, *prompt(rec, refs))
    if not jobs:
        print("no idea cites any evidence; nothing to check")
        return
    if args.dry_run:
        _, _, user, _ = next(iter(jobs.values()))
        print(f"~{sum(len(j[2]) for j in jobs.values()) // 4} input tokens over {len(jobs)} calls\n")
        print(user[:6000])
        return
    if not shutil.which("claude"):
        sys.exit("claude (Claude Code) is not on PATH")

    model = os.environ.get("VERIFY_MODEL") or MODEL
    now = datetime.now(UTC).isoformat()
    cost, kept, total = 0.0, 0, 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(ask_claude_code, user, model, system=SYSTEM, parse=parse): rec_id
                   for rec_id, (_, _, user, _) in jobs.items()}
        for fut in as_completed(futures):
            rec, refs, _, short = jobs[futures[fut]]
            try:
                replies, usage = fut.result()
            except ClaudeCodeError as e:
                print(f"#{rec['rec_id']} failed: {e}")
                continue
            cost += usage["cost"]
            if not replies:
                print(f"#{rec['rec_id']} unreadable reply; no rows saved")
                continue
            reply = replies[0]
            unknown = [s for s in reply["supports"] if s not in short]
            if unknown:
                print(f"#{rec['rec_id']} ignored unknown item ids: {unknown}")
            yes = {short[s] for s in reply["supports"] if s in short}
            reasons = reply.get("reasons") if isinstance(reply.get("reasons"), dict) else {}
            conn.execute("DELETE FROM evidence_checks WHERE rec_id = ?", (rec["rec_id"],))
            conn.executemany(
                "INSERT INTO evidence_checks (rec_id, ref, supports, reason, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(rec["rec_id"], r, int(r in yes), str(reasons.get(s) or ""), model, now) for s, r in short.items()])
            conn.commit()
            kept += len(yes)
            total += len(refs)
            print(f"#{rec['rec_id']} {len(yes)}/{len(refs)} supported  {rec['title']}  (${usage['cost']:.4f})")
    print(f"\n{kept} of {total} cited items support their idea; {total - kept} rejected. "
          f"~${cost:.4f} API-equivalent ({model} via claude -p)")


if __name__ == "__main__":
    main()

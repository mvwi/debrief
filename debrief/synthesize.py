"""Turn labels into findings: SQL rollups per finding kind, then one Claude call per kind.

Rollups become evidence bundles (bundles/<run_id>/<kind>.json); Claude reads each
bundle and returns findings, which land in the findings table under a new run_id.
Each bundle holds at most --max-evidence items; its counts cover all evidence.
--since sets where evidence starts: messages by ts, CI failures by completed_at, review
comments by created_at, sessions by last activity. The default is all of it.

A kind with no evidence is skipped with a note and never sent to the model: ci and review
need GitHub data, so a database of local sessions skips them.

Claude runs through headless Claude Code (`claude -p`, billed to the Claude subscription) by
default, or through OpenRouter with --llm openrouter.

A bundle's hash (model + prompt + bundle) is saved in its file and in bundles/index.json.
If the latest run for a kind came from the same hash, its findings are copied into the new
run instead of calling the model (--force calls it anyway).

    uv run python -m debrief.synthesize --dry-run
    uv run python -m debrief.synthesize --kind hotspot --min-confidence 0.8
    uv run python -m debrief.synthesize --llm openrouter
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
from dotenv import load_dotenv

from debrief.config import choose
from debrief.db import connect
from debrief.paths import BUNDLE_DIR
from debrief.relay import is_relayed

INDEX_PATH = BUNDLE_DIR / "index.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
CLAUDE_CODE_TIMEOUT = 900  # seconds per call
CLAUDE_CODE_RETRIES = (30, 120)  # backoff before each retry, for rate and usage limits
MAX_TOKENS = 8000
KINDS = ["hotspot", "knowledge_gap", "found_not_filed", "correction_pattern", "prompt_practice", "ci", "review"]
QUOTE_LEN = 400
PER_SESSION = 5  # items from one session (or PR, or commit) before others get a turn
HOTSPOT_FILES = 25

# turn_labels field -> column name in the turns CTE; each also gets <name>_p, its confidence.
TURN_FIELDS = {
    "turn_kind": "turn_kind", "correction_type": "correction_type", "resource_pointed_to": "resource_pointed_to",
    "agent_could_have_found_it": "could_have_found", "human_caught_defect": "caught_defect",
    "design_reversal": "reversal", "found_not_filed": "found_not_filed",
}

# A relayed turn (pasted bot output; see relay.py) is not the person correcting the agent:
# its correction becomes turn_kind 'relayed', its reversal and caught defect drop, and it
# counts as something the agent could have found.
RELAYED = {
    "turn_kind": "CASE WHEN r.msg_idx IS NOT NULL AND l.turn_kind = 'correction' THEN 'relayed' ELSE l.turn_kind END",
    "reversal": "CASE WHEN r.msg_idx IS NULL THEN l.reversal END",
    "caught_defect": "CASE WHEN r.msg_idx IS NULL THEN l.caught_defect END",
    "could_have_found": "CASE WHEN r.msg_idx IS NULL THEN l.could_have_found ELSE 'true' END",
    "could_have_found_p": "CASE WHEN r.msg_idx IS NULL THEN l.could_have_found_p "
                          "ELSE COALESCE(l.could_have_found_p, :c) END",
}

# One row per labeled human turn, with only the labels that clear :c. Needs mark_relayed().
TURNS = "labeled AS (\n    SELECT session_id, msg_idx,\n" + ",\n".join(
    f"        MAX(CASE WHEN field = '{f}' AND confidence >= :c THEN value END) AS {a},\n"
    f"        MAX(CASE WHEN field = '{f}' AND confidence >= :c THEN confidence END) AS {a}_p"
    for f, a in TURN_FIELDS.items()) + "\n    FROM turn_labels GROUP BY session_id, msg_idx\n),\n" \
    "turns AS (\n    SELECT l.session_id, l.msg_idx, r.msg_idx IS NOT NULL AS relayed,\n" + ",\n".join(
    f"        {RELAYED.get(c, 'l.' + c)} AS {c}" for a in TURN_FIELDS.values() for c in (a, f"{a}_p")) + \
    "\n    FROM labeled l LEFT JOIN relayed r USING (session_id, msg_idx)\n)"

# For each labeled message, the idx of the human message before it (-1 if none).
# The agent activity a label refers to is the messages in (prev_idx, msg_idx).
WINDOWS = """
windows AS (
    SELECT t.session_id, t.msg_idx,
        COALESCE((SELECT MAX(p.idx) FROM messages p
                  WHERE p.session_id = t.session_id AND p.idx < t.msg_idx
                    AND p.role = 'user' AND p.user_kind = 'human'), -1) AS prev_idx
    FROM (SELECT DISTINCT session_id, msg_idx FROM turn_labels) t
)"""


def mark_relayed(conn: sqlite3.Connection) -> None:
    """Fill the temp table TURNS reads with human turns that paste bot output."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS relayed (session_id TEXT, msg_idx INTEGER, "
                 "PRIMARY KEY (session_id, msg_idx))")
    conn.execute("DELETE FROM relayed")
    human = conn.execute("SELECT session_id, idx, text FROM messages WHERE role = 'user' AND user_kind = 'human'")
    conn.executemany("INSERT INTO relayed VALUES (?, ?)", [(s, i) for s, i, t in human.fetchall() if is_relayed(t)])


def excerpt(text: str | None, n: int = QUOTE_LEN, tail: bool = False) -> str:
    """Collapse whitespace and cut to n chars, keeping the start (or the end)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    return "…" + text[-n:] if tail else text[:n] + "…"


def rows(conn: sqlite3.Connection, sql: str, params: dict) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def agent_text(conn: sqlite3.Connection, session_id: str, prev_idx: int, msg_idx: int, n: int = QUOTE_LEN) -> str:
    """Tail of the assistant text in (prev_idx, msg_idx]."""
    texts = [r[0] for r in conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND idx > ? AND idx <= ? AND role = 'assistant' "
        "AND text != '' ORDER BY idx", (session_id, prev_idx, msg_idx))]
    return excerpt("\n".join(t for t in texts if t), n, tail=True)


def turn_totals(conn: sqlite3.Connection, matched: list[dict], shown: int, since: str) -> dict:
    """Counts over all matching turns, against every human and labeled turn since `since`."""
    human, human_sessions = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT session_id) FROM messages WHERE role = 'user' AND user_kind = 'human' "
        "AND COALESCE(ts, '') >= :since", {"since": since}).fetchone()
    labeled, labeled_sessions = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT session_id) FROM messages m WHERE role = 'user' AND user_kind = 'human' "
        "AND COALESCE(ts, '') >= :since "
        "AND EXISTS (SELECT 1 FROM turn_labels l WHERE l.session_id = m.session_id AND l.msg_idx = m.idx)",
        {"since": since}).fetchone()
    return {"items": len(matched), "shown": shown, "sessions": len({t["session_id"] for t in matched}),
            "human_turns": human, "human_sessions": human_sessions,
            "labeled_turns": labeled, "labeled_sessions": labeled_sessions}


def pick(groups: list[list[dict]], n: int, key: Callable[[dict], object], per_key: int = PER_SESSION) -> list[dict]:
    """Up to n items, one per group in turn, taking each group's items in order.

    No key (session, PR, commit) gets more than per_key items until nothing else is left.
    An item listed in several groups is taken once.
    """
    queues = [list(g) for g in groups if g]
    chosen: list[dict] = []
    taken: set = set()
    per: Counter = Counter()
    for limit in (per_key, math.inf):
        progress = True
        while progress and len(chosen) < n:
            progress = False
            for q in queues:
                if len(chosen) >= n:
                    break
                q[:] = [x for x in q if ref_key(x) not in taken]
                i = next((i for i, x in enumerate(q) if per[key(x)] < limit), None)
                if i is None:
                    continue
                item = q.pop(i)
                chosen.append(item)
                taken.add(ref_key(item))
                per[key(item)] += 1
                progress = True
    return chosen


def by_session(x: dict) -> str:
    return x["session_id"]


def ranked(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: -(x["_p"] or 0))


def clean(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# A PR is settled once merged or closed (db.settled_prs). CI and review rollups keep only
# work whose PR is settled. The outcome rollup drops a session only while its PR is open:
# a session with no linked PR (local logs, or work never pushed) stays in.
SESSION_PR = """EXISTS (
    SELECT 1 FROM checkpoints cp
    JOIN gh_commit_prs g ON g.commit_sha = cp.commit_sha
    WHERE cp.session_id = s.session_id)"""
SETTLED_SESSION = """EXISTS (
    SELECT 1 FROM checkpoints cp
    JOIN gh_commit_prs g ON g.commit_sha = cp.commit_sha
    JOIN settled_prs sp ON sp.pr_number = g.pr_number
    WHERE cp.session_id = s.session_id)"""
SETTLED_CHECK = """EXISTS (
    SELECT 1 FROM gh_commit_prs g JOIN settled_prs sp ON sp.pr_number = g.pr_number
    WHERE g.commit_sha = k.commit_sha)"""

# Checks that say nothing about the code. The Entire GitHub App's check is an approval gate
# (only that app has the slug 'entire'). A gate job fails because another job did: its log
# compares job results, as in `[ "failure" = "success" ]`. Any check name with such a
# failure is left out, passes included, so the totals don't count it either.
# shortcut: knows only this shell form of gate job; add forms as they turn up.
REAL_CHECK = """(COALESCE(k.app, '') != 'entire' AND COALESCE(k.name, '') NOT IN (
    SELECT COALESCE(name, '') FROM gh_checks WHERE conclusion = 'failure' AND (
        failure_log LIKE '%"failure" = "success"%' OR failure_log LIKE '%"cancelled" = "success"%'
        OR failure_log LIKE '%"skipped" = "success"%')))"""

# Real CI failures. flaky = 1 when the same check later passed on the same commit.
# Needs :since, the window start.
CI_FAILS = f"""
ci_fails AS (
    SELECT k.*, EXISTS (
        SELECT 1 FROM gh_checks s
        WHERE s.commit_sha = k.commit_sha AND s.name = k.name AND s.conclusion = 'success'
          AND s.completed_at > k.completed_at) AS flaky,
        {SETTLED_CHECK} AS settled
    FROM gh_checks k
    WHERE k.conclusion = 'failure' AND {REAL_CHECK}
      AND COALESCE(k.completed_at, '') >= :since
)"""


def event_label_sql(event_type: str, field: str, column: str = "value") -> str:
    return (f"(SELECT {column} FROM event_labels WHERE event_type = '{event_type}' AND event_id = {{id}} "
            f"AND field = '{field}' AND confidence >= :c)")


# --- rollups ---------------------------------------------------------------

def hotspot(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Files the agent wrote just before corrections, reversals and caught defects; CI failures by check."""
    flagged = rows(conn, f"""
        WITH {TURNS}, {WINDOWS}
        SELECT tu.*, w.prev_idx, m.text,
            (SELECT json_group_array(DISTINCT f.value)
             FROM tool_calls tc JOIN json_each(tc.files) f
             WHERE tc.session_id = w.session_id AND tc.msg_idx > w.prev_idx AND tc.msg_idx < w.msg_idx
               AND tc.writes = 1 AND f.value NOT LIKE '/%') AS files
        FROM turns tu
        JOIN windows w USING (session_id, msg_idx)
        JOIN messages m ON m.session_id = tu.session_id AND m.idx = tu.msg_idx
        WHERE (tu.turn_kind = 'correction' OR tu.reversal = 'true' OR tu.caught_defect = 'true')
          AND COALESCE(m.ts, '') >= :since
        ORDER BY tu.session_id, tu.msg_idx""", {"c": c, "since": since})
    errors = dict(conn.execute("""
        SELECT f.value, COUNT(*) FROM tool_calls tc JOIN json_each(tc.files) f
        JOIN messages m ON m.session_id = tc.session_id AND m.idx = tc.msg_idx
        WHERE tc.is_error = 1 AND COALESCE(m.ts, '') >= :since GROUP BY f.value""", {"since": since}))
    files: dict[str, dict] = {}
    turns = []
    for t in flagged:
        paths = json.loads(t["files"])
        if not paths:
            continue
        # Rank a turn by its strongest flag.
        p = max((t[f"{a}_p"] or 0) for a, hit in (("turn_kind", "correction"), ("reversal", "true"),
                                                   ("caught_defect", "true")) if t[a] == hit)
        turn = {"session_id": t["session_id"], "msg_idx": t["msg_idx"],
                "labels": {k: t[k] for k in ("turn_kind", "correction_type", "reversal", "caught_defect") if t[k]},
                "files_written_before": paths, "human": excerpt(t["text"]), "_p": p, "_prev": t["prev_idx"]}
        turns.append(turn)
        for path in paths:
            f = files.setdefault(path, {"path": path, "sessions": set(), "turns": 0, "corrections": 0, "reversals": 0,
                                        "caught_defects": 0, "tool_errors": errors.get(path, 0), "_turns": []})
            f["sessions"].add(t["session_id"])
            f["turns"] += 1
            f["corrections"] += t["turn_kind"] == "correction"
            f["reversals"] += t["reversal"] == "true"
            f["caught_defects"] += t["caught_defect"] == "true"
            f["_turns"].append(turn)
    for f in files.values():
        f["sessions"] = len(f["sessions"])
    top = sorted(files.values(), key=lambda f: (-f["sessions"], -f["turns"], -f["tool_errors"]))[:HOTSPOT_FILES]

    checks = rows(conn, f"""
        WITH {CI_FAILS}
        SELECT name, {event_label_sql('check_failure', 'failure_class').format(id='k.check_id')} AS failure_class,
            COUNT(*) AS failures, SUM(flaky) AS flaky, GROUP_CONCAT(check_id) AS check_ids
        FROM ci_fails k WHERE settled
        GROUP BY name, failure_class ORDER BY failures DESC LIMIT 25""", {"c": c, "since": since})
    # One example per check for the top checks; the ci kind has the detail.
    for i, ch in enumerate(checks):
        first = int(ch.pop("check_ids").split(",")[0])
        if i < n // 5:
            ch["example"] = {"event_type": "check_failure", "event_id": first}

    # Each top file's turns in turn; the last group holds turns on other files.
    groups = [ranked(f["_turns"]) for f in top] + [ranked(turns)]
    shown = pick(groups, n - sum("example" in ch for ch in checks), by_session)
    shown_keys = {ref_key(t) for t in shown}
    for f in top:
        f["shown_turns"] = [{"session_id": t["session_id"], "msg_idx": t["msg_idx"]}
                            for t in f.pop("_turns") if ref_key(t) in shown_keys]
    for t in shown:
        t["agent_before"] = agent_text(conn, t["session_id"], t["_prev"], t["msg_idx"] - 1, 250)
    totals = {**turn_totals(conn, turns, len(shown), since),
              "flagged_turns_without_files": len(flagged) - len(turns), "files": len(files)}
    bundle = {"totals": totals, "files": top, "flagged_turns": [clean(t) for t in shown]}
    if checks:  # absent without GitHub data
        bundle["ci_failures_by_check"] = checks
    return bundle


def knowledge_gap(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Turns where the human pointed the agent at a resource or supplied something it could have found.

    Counts split the session's opening message (context given up front) from later turns
    (the agent needed steering), per resource type.
    """
    turns = rows(conn, f"""
        WITH {TURNS}, {WINDOWS}
        SELECT tu.*, w.prev_idx, w.prev_idx < 0 AS opening, m.text
        FROM turns tu
        JOIN windows w USING (session_id, msg_idx)
        JOIN messages m ON m.session_id = tu.session_id AND m.idx = tu.msg_idx
        WHERE (tu.could_have_found = 'true' OR tu.resource_pointed_to NOT IN ('none', ''))
          AND COALESCE(m.ts, '') >= :since
        ORDER BY tu.session_id, tu.msg_idx""", {"c": c, "since": since})
    groups: dict[str, dict] = {}
    for t in turns:
        t["_p"] = max(t["could_have_found_p"] if t["could_have_found"] == "true" else 0,
                      t["resource_pointed_to_p"] if t["resource_pointed_to"] not in (None, "none", "") else 0)
        g = groups.setdefault(t["resource_pointed_to"] or "unlabeled", {
            part: {"turns": 0, "sessions": set(), "agent_could_have_found_it": 0, "_turns": []}
            for part in ("mid_session", "opening")})
        part = g["opening" if t["opening"] else "mid_session"]
        part["turns"] += 1
        part["sessions"].add(t["session_id"])
        part["agent_could_have_found_it"] += t["could_have_found"] == "true"
        part["_turns"].append(t)
    groups = dict(sorted(groups.items(), key=lambda kv: (-kv[1]["mid_session"]["turns"], -kv[1]["opening"]["turns"])))
    # Opening and mid-session turns queue apart, so examples show both.
    parts = [g[part] for part in ("mid_session", "opening") for g in groups.values()]
    shown = {ref_key(t) for t in pick([ranked(p["_turns"]) for p in parts], n, by_session)}
    for g in groups.values():
        examples = []
        for p in (g["mid_session"], g["opening"]):
            p["sessions"] = len(p["sessions"])
            examples += [{
                "session_id": t["session_id"], "msg_idx": t["msg_idx"], "opening": bool(t["opening"]),
                "agent_could_have_found_it": t["could_have_found"], "turn_kind": t["turn_kind"],
                "human": excerpt(t["text"]),
                "agent_before": agent_text(conn, t["session_id"], t["prev_idx"], t["msg_idx"] - 1, 250),
            } for t in p.pop("_turns") if ref_key(t) in shown]
        g["examples"] = examples
    split = {}
    for part, opening in (("mid_session", False), ("opening", True)):
        these = [t for t in turns if bool(t["opening"]) == opening]
        split[part] = {"turns": len(these), "sessions": len({t["session_id"] for t in these})}
    return {"totals": {**turn_totals(conn, turns, len(shown), since), **split}, "by_resource_type": groups}


def found_not_filed(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Every place the agent noticed a problem outside its task and left it unfiled in the session."""
    hits = rows(conn, f"""
        WITH {WINDOWS}
        SELECT l.session_id, l.msg_idx, l.confidence, w.prev_idx, m.role, m.text
        FROM turn_labels l
        JOIN windows w USING (session_id, msg_idx)
        JOIN messages m ON m.session_id = l.session_id AND m.idx = l.msg_idx
        WHERE l.field = 'found_not_filed' AND l.value = 'true' AND l.confidence >= :c AND COALESCE(m.ts, '') >= :since
        ORDER BY l.session_id, l.msg_idx""", {"c": c, "since": since})
    # Repeats of one deferred problem are reworded, so the model merges them, not us.
    for h in hits:
        h["_p"] = h["confidence"]
    shown = sorted(pick([ranked(hits)], n, by_session), key=lambda h: (h["session_id"], h["msg_idx"]))
    grouped: dict[str, list] = {}
    for h in shown:
        # On a human turn the flag covers the agent messages before it; on the
        # per-session row (an assistant message) it covers that message and those before it.
        end = h["msg_idx"] if h["role"] == "assistant" else h["msg_idx"] - 1
        item = {"session_id": h["session_id"], "msg_idx": h["msg_idx"], "confidence": round(h["confidence"], 2),
                "agent_text": agent_text(conn, h["session_id"], h["prev_idx"], end, 800)}
        if h["role"] == "user":
            item["human_reply"] = excerpt(h["text"], 200)
        grouped.setdefault(h["session_id"], []).append(item)
    totals = {"items": len(hits), "shown": len(shown), "sessions": len({h["session_id"] for h in hits}),
              "shown_sessions": len(grouped)}
    return {"totals": totals, "sessions": [{"session_id": k, "hits": v} for k, v in grouped.items()]}


def correction_pattern(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Correction types, how often each occurs, and example turns."""
    turns = rows(conn, f"""
        WITH {TURNS}, {WINDOWS}
        SELECT tu.*, w.prev_idx, m.text
        FROM turns tu
        JOIN windows w USING (session_id, msg_idx)
        JOIN messages m ON m.session_id = tu.session_id AND m.idx = tu.msg_idx
        WHERE tu.turn_kind = 'correction' AND COALESCE(m.ts, '') >= :since
        ORDER BY tu.session_id, tu.msg_idx""", {"c": c, "since": since})
    groups: dict[str, dict] = {}
    for t in turns:
        t["_p"] = t["correction_type_p"] or t["turn_kind_p"]
        g = groups.setdefault(t["correction_type"] or "unlabeled", {"count": 0, "sessions": set(), "_turns": []})
        g["count"] += 1
        g["sessions"].add(t["session_id"])
        g["_turns"].append(t)
    groups = dict(sorted(groups.items(), key=lambda kv: -kv[1]["count"]))
    shown = {ref_key(t) for t in pick([ranked(g["_turns"]) for g in groups.values()], n, by_session)}
    for g in groups.values():
        g["sessions"] = len(g["sessions"])
        g["examples"] = [{
            "session_id": t["session_id"], "msg_idx": t["msg_idx"],
            "reversal": t["reversal"], "caught_defect": t["caught_defect"],
            "human": excerpt(t["text"]),
            "agent_before": agent_text(conn, t["session_id"], t["prev_idx"], t["msg_idx"] - 1, 300),
        } for t in g.pop("_turns") if ref_key(t) in shown]
    return {"totals": turn_totals(conn, turns, len(shown), since), "by_correction_type": groups}


def prompt_practice(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Session-level prompt labels against outcome and per-session correction/reversal rates."""
    sessions = rows(conn, f"""
        WITH {TURNS},
        per_session AS (
            SELECT session_id,
                SUM(turn_kind = 'correction') AS corrections,
                SUM(reversal = 'true') AS reversals,
                SUM(caught_defect = 'true') AS caught_defects
            FROM turns GROUP BY session_id
        ),
        first_human AS (
            SELECT session_id, MIN(idx) AS msg_idx, COUNT(*) AS human_turns
            FROM messages WHERE role = 'user' AND user_kind = 'human' GROUP BY session_id
        )
        SELECT s.session_id, fh.msg_idx, fh.human_turns, s.agent, s.prompt, AVG(sl.confidence) AS _p,
            MAX(CASE WHEN sl.field = 'task_type' THEN sl.value END) AS task_type,
            MAX(CASE WHEN sl.field = 'prompt_style' THEN sl.value END) AS prompt_style,
            MAX(CASE WHEN sl.field = 'prompt_specificity' THEN sl.value END) AS prompt_specificity,
            MAX(CASE WHEN sl.field = 'planned_first' THEN sl.value END) AS planned_first,
            MAX(CASE WHEN sl.field = 'tests_added' THEN sl.value END) AS tests_added,
            MAX(CASE WHEN sl.field = 'outcome' THEN sl.value END) AS outcome,
            COALESCE(p.corrections, 0) AS corrections, COALESCE(p.reversals, 0) AS reversals,
            COALESCE(p.caught_defects, 0) AS caught_defects,
            {SESSION_PR} AS has_pr, {SETTLED_SESSION} AS settled
        FROM sessions s
        JOIN session_labels sl ON sl.session_id = s.session_id AND sl.confidence >= :c
        JOIN first_human fh ON fh.session_id = s.session_id
        LEFT JOIN per_session p ON p.session_id = s.session_id
        WHERE COALESCE(s.last_activity_at, '') >= :since
        GROUP BY s.session_id ORDER BY s.session_id""", {"c": c, "since": since})
    unsettled = sum(s["has_pr"] and not s["settled"] for s in sessions)
    sessions = [s for s in sessions if s["settled"] or not s["has_pr"]]
    by_label: dict[str, dict] = {}
    for field in ("prompt_style", "prompt_specificity", "task_type", "planned_first", "tests_added"):
        for s in sessions:
            if not s[field]:
                continue
            g = by_label.setdefault(field, {}).setdefault(
                s[field], {"sessions": 0, "outcomes": {}, "human_turns": 0, "corrections": 0, "reversals": 0})
            g["sessions"] += 1
            g["outcomes"][s["outcome"] or "unlabeled"] = g["outcomes"].get(s["outcome"] or "unlabeled", 0) + 1
            for k in ("human_turns", "corrections", "reversals"):
                g[k] += s[k]
    for groups in by_label.values():
        for g in groups.values():
            g["corrections_per_session"] = round(g["corrections"] / g["sessions"], 2)
            g["reversals_per_session"] = round(g["reversals"] / g["sessions"], 2)
    # Show sessions from every outcome in turn, most confidently labeled first.
    outcomes: dict[str, list] = {}
    for s in ranked(sessions):
        outcomes.setdefault(s["outcome"] or "unlabeled", []).append(s)
    shown = sorted(pick(list(outcomes.values()), n, by_session), key=lambda s: s["session_id"])
    per_session = [
        {"session_id": s["session_id"], "msg_idx": s["msg_idx"], "agent": s["agent"],
         **{k: s[k] for k in ("task_type", "prompt_style", "prompt_specificity", "planned_first", "tests_added",
                              "outcome", "human_turns", "corrections", "reversals", "caught_defects")},
         "pr": "merged_or_closed" if s["has_pr"] else "none", "opening_prompt": excerpt(s["prompt"], 300)}
        for s in shown]
    totals = {"items": len(sessions), "shown": len(shown), "sessions": len(sessions), "excluded_unsettled": unsettled,
              "sessions_without_pr": sum(not s["has_pr"] for s in sessions)}
    return {"totals": totals, "by_label": by_label, "per_session": per_session}


def ci(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Failed CI checks by failure_class, with log snippets, flake flags, and their PRs and sessions."""
    fails = rows(conn, f"""
        WITH {CI_FAILS}
        SELECT k.check_id, k.name, k.commit_sha, k.failure_log, k.flaky, k.settled,
            {event_label_sql('check_failure', 'failure_class').format(id='k.check_id')} AS failure_class,
            {event_label_sql('check_failure', 'failure_class', 'confidence').format(id='k.check_id')} AS _p,
            (SELECT GROUP_CONCAT(pr_number) FROM gh_commit_prs cp WHERE cp.commit_sha = k.commit_sha) AS prs,
            (SELECT GROUP_CONCAT(DISTINCT session_id) FROM checkpoints cp WHERE cp.commit_sha = k.commit_sha) AS sessions
        FROM ci_fails k
        ORDER BY k.check_id""", {"c": c, "since": since})
    unsettled = sum(not f["settled"] for f in fails)
    fails = [f for f in fails if f["settled"]]
    total = conn.execute(f"SELECT COUNT(*) FROM gh_checks k WHERE conclusion IS NOT NULL "
                         f"AND {REAL_CHECK} AND COALESCE(completed_at, '') >= :since "
                         f"AND {SETTLED_CHECK}", {"since": since}).fetchone()[0]
    groups: dict[str, dict] = {}
    for f in fails:
        f["event_type"], f["event_id"] = "check_failure", f["check_id"]
        g = groups.setdefault(f["failure_class"] or "unlabeled", {"count": 0, "flaky": 0, "checks": {}, "_fails": []})
        g["count"] += 1
        g["flaky"] += f["flaky"]
        g["checks"][f["name"]] = g["checks"].get(f["name"], 0) + 1
        g["_fails"].append(f)
    groups = dict(sorted(groups.items(), key=lambda kv: -kv[1]["count"]))
    shown = {ref_key(f) for f in pick([ranked(g["_fails"]) for g in groups.values()], n, lambda f: f["commit_sha"])}
    for g in groups.values():
        g["examples"] = [{
            "event_type": "check_failure", "event_id": f["check_id"], "check": f["name"],
            "flaky": bool(f["flaky"]), "prs": f["prs"], "sessions": f["sessions"],
            "log": excerpt(f["failure_log"], 500, tail=True),
        } for f in g.pop("_fails") if ref_key(f) in shown]
    totals = {"items": len(fails), "shown": len(shown), "checks": total, "failures": len(fails),
              "excluded_unsettled": unsettled, "flaky": sum(f["flaky"] for f in fails)}
    return {"totals": totals, "by_failure_class": groups}


def review(conn: sqlite3.Connection, c: float, n: int, since: str) -> dict:
    """Review comments on settled PRs by whether they held up, bot vs human, with thread state."""
    comments = rows(conn, f"""
        SELECT rc.comment_id, rc.pr_number, rc.author, rc.is_bot, rc.path, rc.body,
            rc.kind, rc.review_state, rc.is_resolved, rc.is_outdated,
            {event_label_sql('review_comment', 'held_up').format(id='rc.comment_id')} AS held_up,
            {event_label_sql('review_comment', 'held_up', 'confidence').format(id='rc.comment_id')} AS _p,
            {event_label_sql('review_comment', 'comment_type').format(id='rc.comment_id')} AS comment_type,
            rc.pr_number IN (SELECT pr_number FROM settled_prs) AS settled
        FROM gh_review_comments rc
        WHERE rc.in_reply_to IS NULL AND COALESCE(rc.created_at, '') >= :since
        ORDER BY rc.comment_id""", {"c": c, "since": since})
    unsettled = sum(not r["settled"] for r in comments)
    comments = [r for r in comments if r["settled"]]
    counts: dict[str, dict] = {}
    groups: dict[str, list] = {}
    for r in comments:
        who = "bot" if r["is_bot"] else "human"
        held, ctype, kind = r["held_up"] or "unlabeled", r["comment_type"] or "unlabeled", r["kind"] or "unknown"
        if kind == "inline":
            # GitHub thread state decides fixed vs open; the label only says valid, wrong or unclear.
            validity = {"valid_and_fixed": "valid", "valid_open": "valid"}.get(held, held)
            status = "resolved" if r["is_resolved"] else "outdated" if r["is_outdated"] else "open"
            outcome = f"{validity}/{status}"
        else:
            outcome = held
        w = counts.setdefault(who, {"comment_type": {}})
        w.setdefault(kind, {})[outcome] = w.setdefault(kind, {}).get(outcome, 0) + 1
        w["comment_type"][ctype] = w["comment_type"].get(ctype, 0) + 1
        groups.setdefault(f"{who}/{kind}/{outcome}", []).append(
            {"event_type": "review_comment", "event_id": r["comment_id"], "pr": r["pr_number"],
             "author": r["author"], "review_state": r["review_state"],
             "is_resolved": r["is_resolved"], "is_outdated": r["is_outdated"], "path": r["path"],
             "comment_type": ctype, "body": excerpt(r["body"], 300), "_p": r["_p"]})
    groups = dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))
    shown = {ref_key(x) for x in pick([ranked(g) for g in groups.values()], n, lambda x: x["pr"])}
    examples = {k: [clean(x) for x in g if ref_key(x) in shown] for k, g in groups.items()}
    totals = {"items": len(comments), "shown": len(shown), "comments": len(comments),
              "prs": len({r["pr_number"] for r in comments}), "excluded_unsettled": unsettled}
    return {"totals": totals, "counts": counts, "examples": {k: v for k, v in examples.items() if v}}


ROLLUPS = {
    "hotspot": hotspot, "knowledge_gap": knowledge_gap, "found_not_filed": found_not_filed,
    "correction_pattern": correction_pattern, "prompt_practice": prompt_practice, "ci": ci, "review": review,
}


# --- evidence refs -------------------------------------------------------------

def ref_key(d: dict) -> tuple | None:
    """A hashable key for a session or event ref, or None if d isn't one."""
    try:
        if "session_id" in d and "msg_idx" in d:
            return ("msg", str(d["session_id"]), int(d["msg_idx"]))
        if "event_type" in d and "event_id" in d:
            return ("event", str(d["event_type"]), int(d["event_id"]))
    except (TypeError, ValueError):
        pass
    return None


def ref_of(key: tuple) -> dict:
    kind, a, b = key
    return {"session_id": a, "msg_idx": b} if kind == "msg" else {"event_type": a, "event_id": b}


def bundle_refs(obj) -> set[tuple]:
    """Every ref anywhere in a bundle."""
    if isinstance(obj, list):
        return set().union(*(bundle_refs(x) for x in obj)) if obj else set()
    if isinstance(obj, dict):
        found = {k} if (k := ref_key(obj)) else set()
        return found.union(*(bundle_refs(v) for v in obj.values()))
    return set()


# --- LLM pass ----------------------------------------------------------------------

SYSTEM = """You analyze how a developer or a team works with AI coding agents. It may be one person; the
bundle's refs and counts show how many sessions there are.
You get an evidence bundle: SQL rollups over labeled agent session transcripts and, when the data has them,
CI checks and PR review comments. Take the repo, language and tools from the evidence; assume none.
Labels come from a classifier and can be wrong; quotes are excerpts.
The bundle shows a capped sample of items. "totals" and the per-group counts cover all evidence, not just the sample:
use them when you state counts.

Write findings for the people who ran these sessions. Rules:
- Each finding must be specific and actionable: a concrete line to add to the agent's instruction file
  (CLAUDE.md, AGENTS.md or whichever the sessions use), a test to add,
  a script or doc to write, an issue to file, a check to change. Name files, commands and checks when the evidence does.
- Cite evidence only by copying refs that appear in the bundle: {"session_id": ..., "msg_idx": ...} or
  {"event_type": ..., "event_id": ...}. Never invent or alter a ref.
- Base each finding on a pattern across several items where possible; say how many items support it.
- If the evidence is too thin to support a finding, return fewer findings or none. Do not pad. Do not invent.
- Plain, short sentences.

Reply with JSON only:
{"findings": [{"kind": "<kind>", "title": "<one line>", "detail": "<what the evidence shows, with counts>",
  "suggested_fix": "<the concrete change>", "evidence": [<refs>]}]}"""

KIND_HINTS = {
    "hotspot": "Files the agent had just written when a human corrected it, reversed a design or caught a defect; "
               "plus CI failures by check when the data has any. Look for files or areas that need better docs, tests or guardrails.",
    "knowledge_gap": "Turns where the human pointed the agent at something (spec, script, doc, prior PR, code) "
                     "or the agent could have found the answer itself. Counts split each resource type into "
                     "opening (the session's first human message) and mid_session (later turns); examples carry "
                     "the same flag. A pointer in the opening message is context given up front, a good habit, "
                     "and not a gap in itself. Mid-session pointers and could-have-found turns are the gaps. "
                     "Report opening and mid-session counts separately and never add them together. Look for "
                     "knowledge to put where agents will find it.",
    "found_not_filed": "Places where the agent noticed a bug or problem outside its task and deferred it without filing "
                       "it during the session. Hits are grouped by session. The agent often repeats the same deferred problem on "
                       "later turns: merge repeats into one finding that cites all their refs, and count each "
                       "problem once. Some hits are false positives (e.g. a test-result summary): skip any "
                       "item whose agent_text does not explicitly defer or leave open a specific problem. Each real "
                       "one is a candidate issue: write the issue title and a one-line body in suggested_fix.",
    "correction_pattern": "What kinds of corrections humans make, with examples. Look for repeat mistakes that a "
                          "rule, check or test would prevent.",
    "prompt_practice": "Prompt style, specificity and task type against outcome and correction rates. Sessions "
                       "whose PR is still open are left out. Each session's pr is merged_or_closed or none; "
                       "totals.sessions_without_pr counts the none ones. A session with no PR has no merge to "
                       "confirm its outcome, which is then only the classifier's reading of the transcript: say "
                       "so when a finding rests on those sessions. Small samples: "
                       "report differences only when counts support them, and state the counts.",
    "ci": "Failed CI checks on merged or closed PRs, by failure class with log tails; flaky = the same check later passed on the same "
          "commit (a re-run fixed it). Look for flaky tests to fix, and for failures an agent could catch before pushing "
          "(a local task or script, a pre-push hook, a line in the agent's instruction file).",
    "review": "PR review comments on merged or closed PRs, bot vs human, split by kind (inline thread, review "
              "body, issue comment). Inline outcomes read <validity>/<thread state>: validity (valid | wrong | "
              "unclear) comes from a classifier, thread state (resolved | outdated | open) from GitHub. Other "
              "kinds carry the classifier's held_up label only. Look for review noise to cut and for "
              "comment types that point at gaps agents keep leaving.",
}


def user_prompt(kind: str, bundle: dict) -> str:
    return f"Finding kind: {kind}\n{KIND_HINTS[kind]}\n\nEvidence bundle:\n{json.dumps(bundle, ensure_ascii=False)}"


def canonical_model(model: str) -> str:
    """One name per model across transports, in OpenRouter form: claude-opus-5-5 -> anthropic/claude-opus-5.5."""
    if model.startswith("claude-"):
        return "anthropic/" + re.sub(r"-(\d{1,2})-(\d{1,2})$", r"-\1.\2", model)
    return model


def prompt_hash(model: str, user: str) -> str:
    # The hash covers the model and prompts, not the transport: Claude Code and OpenRouter
    # send the same prompts to the same model, so a bundle cached from one is reused by the
    # other. A different model (or model version) misses the cache.
    return hashlib.sha256(json.dumps([canonical_model(model), SYSTEM, user], ensure_ascii=False).encode()).hexdigest()


def input_price(model: str) -> float | None:
    """USD per input token from OpenRouter's model list, or None if it can't be fetched."""
    try:
        resp = httpx.get(MODELS_URL, timeout=15)
        resp.raise_for_status()
        model_info = next(m for m in resp.json()["data"] if m["id"] == model)
        return float(model_info["pricing"]["prompt"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration):
        return None


def parse_json(text: str) -> dict:
    """Parse a model reply that should be JSON, tolerating code fences and surrounding prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1]) if start >= 0 < end else {}


def findings_of(text: str) -> list[dict]:
    findings = parse_json(text).get("findings", [])
    return findings if isinstance(findings, list) else []


def ask_openrouter(user: str, model: str, key: str, system: str = SYSTEM) -> tuple[list[dict], dict]:
    """One chat completion for one bundle prompt; returns (findings, usage)."""
    resp = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "max_tokens": MAX_TOKENS, "response_format": {"type": "json_object"},
              "usage": {"include": True},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=600,
    )
    resp.raise_for_status()
    body = resp.json()
    usage = body.get("usage") or {}
    return findings_of(body["choices"][0]["message"]["content"] or ""), {
        "in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens"), "cost": usage.get("cost") or 0}


class ClaudeCodeError(RuntimeError):
    pass


def claude_code_once(user: str, model: str, cwd: str, system: str = SYSTEM) -> dict:
    """Run `claude -p` as a plain model call and return its JSON result envelope."""
    cmd = [
        "claude", "-p", "--model", model, "--output-format", "json", "--max-turns", "1",
        "--system-prompt", system,
        "--tools", "",                 # no tools: one model turn, nothing else
        "--setting-sources", "",       # skip user/project settings, hooks and CLAUDE.md
        "--strict-mcp-config",         # no MCP servers
        "--disable-slash-commands",    # no skills
        "--no-session-persistence",    # keep these out of `claude --resume`
    ]
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=cwd,
                              timeout=CLAUDE_CODE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as e:
        raise ClaudeCodeError(f"timed out after {CLAUDE_CODE_TIMEOUT}s") from e
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        env = None
    if proc.returncode or not isinstance(env, dict) or env.get("is_error"):
        detail = (env or {}).get("result") or proc.stderr.strip() or proc.stdout.strip()
        # An API error still reports subtype "success", so show the HTTP status instead.
        status = (env or {}).get("api_error_status")
        why = f"HTTP {status}" if status else (env or {}).get("subtype", "no JSON envelope")
        raise ClaudeCodeError(f"exit {proc.returncode}, {why}: {excerpt(detail, 300)}")
    return env


def ask_claude_code(user: str, model: str, system: str = SYSTEM,
                    parse: Callable[[str], list[dict]] = findings_of) -> tuple[list[dict], dict]:
    """One headless Claude Code call, retried on failure; returns (parsed items, usage)."""
    # Run from an empty directory so nothing from this project reaches the model.
    with tempfile.TemporaryDirectory(prefix="synth-") as cwd:
        for wait in (*CLAUDE_CODE_RETRIES, None):
            try:
                env = claude_code_once(user, model, cwd, system)
                break
            except ClaudeCodeError as e:
                if wait is None:
                    raise
                print(f"  claude -p failed ({e}); retrying in {wait}s")
                time.sleep(wait)
    usage = env.get("usage") or {}
    tokens_in = sum(usage.get(k) or 0 for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return parse(env.get("result") or ""), {
        "in": tokens_in, "out": usage.get("output_tokens"), "cost": env.get("total_cost_usd") or 0,
        "seconds": round((env.get("duration_ms") or 0) / 1000)}


def check_evidence(kind: str, findings: list[dict], allowed: set[tuple]) -> list[dict]:
    """Keep only refs present in the bundle; drop findings left with none."""
    kept = []
    for f in findings:
        refs = [ref_key(r) for r in f.get("evidence") or [] if isinstance(r, dict)]
        good = list(dict.fromkeys(r for r in refs if r in allowed))
        if len(good) < len(refs):
            print(f"  {kind}: dropped {len(refs) - len(good)} unknown ref(s) from {f.get('title')!r}")
        if not good:
            print(f"  {kind}: dropped finding with no valid evidence: {f.get('title')!r}")
            continue
        kept.append({**f, "kind": kind, "evidence": [ref_of(r) for r in good]})
    return kept


# --- cache -----------------------------------------------------------------------------

def load_index() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def cached_run(conn: sqlite3.Connection, index: dict, digest: str, kind: str) -> str | None:
    """The run whose findings came from this exact prompt, if it is still the latest run for kind."""
    entry = index.get(digest)
    if not entry or entry["kind"] != kind:
        return None
    count = conn.execute("SELECT COUNT(*) FROM findings WHERE run_id = ? AND kind = ?",
                         (entry["run_id"], kind)).fetchone()[0]
    latest = conn.execute("SELECT MAX(run_id) FROM findings WHERE kind = ?", (kind,)).fetchone()[0]
    if count != entry["findings"] or (latest and latest > entry["run_id"]):
        return None
    return entry["run_id"]


def save_findings(conn: sqlite3.Connection, run_id: str, kind: str, findings: list[dict], now: str) -> None:
    conn.executemany(
        "INSERT INTO findings (run_id, kind, title, detail, suggested_fix, evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(run_id, kind, f.get("title"), f.get("detail"), f.get("suggested_fix"),
          json.dumps(f["evidence"]), now) for f in findings])
    conn.commit()


# --- main ------------------------------------------------------------------------------

# Kinds built only from GitHub data: the table each needs, and what to call it in the skip note.
NEEDS = {"ci": ("gh_checks", "CI checks"), "review": ("gh_review_comments", "PR review comments")}


def missing_data(conn: sqlite3.Connection, kind: str) -> str | None:
    """Why kind can't be built from this database, or None if it can."""
    table, what = NEEDS.get(kind, (None, None))
    if table and not conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
        return f"no {what} in the database (needs GitHub data)"
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=KINDS, help="Only this finding kind")
    parser.add_argument("--min-confidence", type=float, default=0.7, help="Ignore labels below this confidence")
    parser.add_argument("--max-evidence", type=int, default=40, help="Most evidence items per bundle")
    parser.add_argument("--since", default="", help="Ignore evidence before this ISO date (default: keep all)")
    parser.add_argument("--llm", choices=["claude-code", "openrouter"],
                        help="claude-code: `claude -p` on the Claude subscription; openrouter: OpenRouter API. "
                             "Default: [synthesize] in config.toml, else claude-code")
    parser.add_argument("--parallel", type=int,
                        help="Concurrent model calls (default 2 for claude-code, which shares subscription "
                             "limits; one per kind for openrouter)")
    parser.add_argument("--dry-run", action="store_true", help="Build bundles and print them; no model call")
    parser.add_argument("--force", action="store_true", help="Call the model even when a bundle is cached")
    parser.add_argument("--db", type=Path, help="Database path (default debrief.db)")
    args = parser.parse_args(argv)
    load_dotenv()

    conn = connect(args.db) if args.db else connect()
    mark_relayed(conn)
    args.llm, model = choose("synthesize", args.llm)
    subscription = args.llm == "claude-code"
    price = None if subscription else input_price(model)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = BUNDLE_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    index = load_index()

    bundles, prompts, digests, cached = {}, {}, {}, {}
    todo_tokens = 0
    print(f"{'kind':<20} {'items':>11} {'refs':>5} {'~tokens':>8} {'~cost':>8}")
    for kind in [args.kind] if args.kind else KINDS:
        if why := missing_data(conn, kind):
            print(f"{kind:<20} skipped: {why}")
            continue
        bundle = bundles[kind] = ROLLUPS[kind](conn, args.min_confidence, args.max_evidence, args.since)
        user = prompts[kind] = user_prompt(kind, bundle)
        digest = digests[kind] = prompt_hash(model, user)
        (out / f"{kind}.json").write_text(json.dumps(
            {"kind": kind, "model": model, "hash": digest, "bundle": bundle}, indent=2, ensure_ascii=False))
        tokens = (len(SYSTEM) + len(user)) // 4
        cost = "sub" if subscription else f"${tokens * price:.3f}" if price is not None else "?"
        note = ""
        if not bundle_refs(bundle):
            note = "  skipped: no evidence, nothing to send"
        elif not args.force and (hit := cached_run(conn, index, digest, kind)):
            cached[kind] = hit
            note = f"  cached from {hit}"
        else:
            todo_tokens += tokens
        items = f"{bundle['totals']['shown']}/{bundle['totals']['items']}"
        print(f"{kind:<20} {items:>11} {len(bundle_refs(bundle)):>5} {tokens:>8} {cost:>8}{note}")
    if subscription:
        print(f"~{todo_tokens} input tokens to send to {model} via claude -p (subscription)")
    elif price is None:
        print(f"~{todo_tokens} input tokens to send ({model} price unavailable)")
    else:
        print(f"~{todo_tokens} input tokens to send, ~${todo_tokens * price:.2f} at {model} input price "
              f"(output extra, at most {MAX_TOKENS} tokens per kind)")
    print(f"bundles in {out}")

    if args.dry_run:
        if not bundles:
            return
        kind = args.kind or max(bundles, key=lambda k: len(bundle_refs(bundles[k])))
        print(f"\n--- {kind} ---\n{json.dumps(bundles[kind], indent=2, ensure_ascii=False)[:6000]}")
        return

    if subscription:
        if not shutil.which("claude"):
            sys.exit("claude (Claude Code) is not on PATH")
        ask = partial(ask_claude_code, model=model)
    else:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            sys.exit("OPENROUTER_API_KEY is not set")
        ask = partial(ask_openrouter, model=model, key=key)
    todo = {k: p for k, p in prompts.items() if bundle_refs(bundles[k]) and k not in cached}
    now = datetime.now(UTC).isoformat()
    total, cost = 0, 0.0

    for kind, source in cached.items():
        conn.execute(
            "INSERT INTO findings (run_id, kind, title, detail, suggested_fix, evidence, created_at) "
            "SELECT ?, kind, title, detail, suggested_fix, evidence, ? FROM findings "
            "WHERE run_id = ? AND kind = ? ORDER BY finding_id", (run_id, now, source, kind))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM findings WHERE run_id = ? AND kind = ?", (run_id, kind)).fetchone()[0]
        index[digests[kind]] = {"run_id": run_id, "kind": kind, "findings": n}
        total += n
        print(f"{kind}: {n} findings copied from {source}")

    workers = args.parallel or (2 if subscription else len(todo)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {k: pool.submit(ask, p) for k, p in todo.items()}
    for kind, fut in futures.items():
        try:
            raw, usage = fut.result()
            findings = check_evidence(kind, raw, bundle_refs(bundles[kind]))
        except (httpx.HTTPError, ClaudeCodeError, json.JSONDecodeError, KeyError) as e:
            print(f"{kind}: failed: {e}")
            continue
        save_findings(conn, run_id, kind, findings, now)
        index[digests[kind]] = {"run_id": run_id, "kind": kind, "findings": len(findings)}
        total += len(findings)
        cost += usage["cost"]
        took = f", {usage['seconds']}s" if "seconds" in usage else ""
        print(f"{kind}: {len(findings)} findings, {usage['in']} in / {usage['out']} out tokens, "
              f"${usage['cost']:.4f}{took}")
        for f in findings:
            print(f"[{kind}] {f.get('title')}  ({len(f['evidence'])} refs)\n    fix: {f.get('suggested_fix')}")
    INDEX_PATH.write_text(json.dumps(index, indent=2))
    # Claude Code reports what the call would cost at API prices; the subscription pays instead.
    billed = f"~${cost:.4f} API-equivalent, billed to subscription" if subscription else f"${cost:.4f}"
    print(f"\n{total} findings saved under run_id {run_id} ({model} via {args.llm}), cost {billed}")


if __name__ == "__main__":
    main()

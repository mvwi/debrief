"""Label turns, sessions and GitHub events with Jev (TypeSafe's decision model) via OpenRouter.

Reads messages, tool_calls, checkpoints and gh_* tables from debrief.db and writes
turn_labels, session_labels and event_labels. Needs OPENROUTER_API_KEY in .env.

    uv run python -m debrief.classify --dry-run
    uv run python -m debrief.classify --session efd45cc6-c764-4e22-80eb-cdbc1f9173f9
    uv run python -m debrief.classify --only events --force

API: POST https://openrouter.ai/api/alpha/decisions with {model, state, questions};
the reply holds {answers: {field: {type, ...}}, model, usage}. Shapes come from the
@openrouter/sdk models (DecisionsRequest, DecisionsResponse).

Storage per answer type:
  noul   value "true"/"false" (p > 0.5), confidence max(p, 1-p), probs {"true": p}
  choice value the chosen key, confidence its probability, probs the full distribution
  score  value the most likely level's label, confidence its probability, probs
         {label: p}; an extra row "<field>_score" holds the probability-weighted
         level (0 = first label) as the value, for averaging.
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import httpx
from dotenv import load_dotenv

from debrief.db import connect

URL = "https://openrouter.ai/api/alpha/decisions"
STATE_CAP = 24_000        # chars; Jev's context is 32K tokens
HUMAN_MIN = 6_000         # never trim the human message below this
PROMPT_CAP = 8_000        # opening prompt in session state
OLD_TEXT_CAP = 400        # assistant texts older than the last two
TAIL_TEXT_CAP = 1_500     # last assistant texts in session state
WORKERS = 8


def noul(instructions: str, true: str, false: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


def choice(instructions: str, **criteria: str) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, *levels: str) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# Hand-edited after jev-align round 2 (2026-09-25): train F1 0.63 -> 0.78 on 60 agent-labeled
# turns (compare_definitions.py). Test failures blamed on the environment count as true: they
# point at infra or dev-setup fixes.
FOUND_NOT_FILED = noul(
    "In the agent activity shown, does the agent name a concrete problem that lies outside what "
    "the human asked it to do, and explicitly leave it unaddressed?",
    "The agent flags a bug, flaw, missing safeguard or failing test that it calls pre-existing, "
    "unrelated to this change, or out of scope, and leaves it for later, another PR, or the human. "
    "E.g. 'that's pre-existing, so I'll leave it for its own PR'; 'this test also fails on main, not "
    "caused by this change'; 'the sandbox blocks keychain access so that test fails here; unrelated'.",
    "Anything else, including: findings from a review, audit or cleanup pass the human asked for "
    "(/simplify, /code-review), even ones the agent chooses to skip; the agent analysing or planning "
    "the requested work; differences or tradeoffs the user asked for; progress or status notes; the "
    "human, not the agent, deferring something.",
)

TURN_QUESTIONS = {
    "turn_kind": choice(
        "What is the human message doing?",
        new_request="Asks for new work",
        answer_to_agent_question="Answers a question the agent asked",
        correction="Tells the agent something it did or said is wrong",
        approval_or_go_ahead="Approves or tells the agent to proceed",
        question_to_agent="Asks the agent a question",
        status_check="Asks how things stand or what happened",
        other="None of the above",
    ),
    "correction_type": choice(
        "If the human corrects the agent, what kind of correction is it?",
        none="Not a correction",
        wrong_assumption="Agent assumed something false",
        missed_requirement="Agent left out something asked for",
        style_or_convention="Code style, naming or project convention",
        scope="Agent did too much or too little",
        factual_error="Agent stated a wrong fact",
        process="Agent skipped a step, e.g. tests, or didn't ask first",
    ),
    "resource_pointed_to": choice(
        "What resource does the human point the agent to?",
        none="No resource",
        api_spec_or_schema="API spec, schema, swagger, live API response",
        script_or_command="A script or command to run",
        doc_or_readme="Docs or README",
        prior_pr_or_issue="An earlier PR or issue",
        code_location="A file, function or place in the code",
        external_url="An outside URL",
        other="Something else",
    ),
    # Hand-edited after jev-align round 2 (2026-09-25): train F1 0.64 -> 0.84 on 60 agent-labeled
    # turns; held-out false positives 5 -> 0 of 11.
    "agent_could_have_found_it": noul(
        "Is the human surfacing information the agent had access to but did not find or check itself?",
        "The human states, rather than asks, something the agent could already reach with its own "
        "tools: code behavior, file contents or locations, test or command results, merge conflicts, "
        "git, PR, CI or trail state (a PR is merged, a finding is open), review findings about the "
        "branch, or that a spec, doc or endpoint exists. A hunch stated as fact counts, and so does a "
        "rhetorical question that points at such a source.",
        "Anything else, including: real questions, even ones naming files or lines; instructions, "
        "decisions, preferences or approvals; a new task or a session-opening brief, even one full of "
        "facts; clarifying what the human meant; content from outside the agent's reach (support "
        "threads, emails, a coworker's machine, a login only the human could complete).",
    ),
    # Hand-edited after agent labeling (2026-09-25): train F1 0.48 -> 0.85 on 60 agent-labeled turns;
    # held-out 0.45 -> 0.83 on 20. Relayed findings count as false: agent_could_have_found_it covers them.
    # The examples quote training items, so confirm on fresh labels.
    "human_caught_defect": noul(
        "Did the person discover, by running, testing or reading it, that the agent's own change is broken "
        "or gives wrong output?",
        "The person ran, tested or read what the agent built or changed and says, in their own words, that "
        "it is broken or wrong, even tersely or as a question. E.g. 'still same error' after the agent's "
        "fix; output from running the agent's script that shows an error or unwanted line; 'why is it "
        "duplicated?' above the command's output; 'this seems worse' about output the agent just changed; "
        "'you dropped all the no-error assertions'; 'remove the package-lock, I think the merge added that?'.",
        "Anything else, including: findings relayed from bots, trails, CI, Copilot, Codex or other "
        "reviewers, with or without a header ('[P2] …', 'Entire Trail review comment', 'address the trail "
        "findings', 'ci is failing'), and numbered or formal write-ups that cite file:line, describe impact "
        "and suggest a fix, which read as pasted reviews; merge or rebase conflicts; new requests or "
        "tickets; problems in code or services the agent did not change, or behavior that predates its "
        "change (a server routing logins, an old hint); errors from the person's own tools or setup ('that "
        "is the error, fix it' after their own lint run); complaints about wording, length or clarity; "
        "design objections, asks for a different approach, or deciding how a case should behave when the "
        "agent did what was asked ('wrong, it should fail too'); process corrections ('don't commit to "
        "main'); questions and hypotheticals; harness text such as 'Stop hook feedback:'.",
    ),
    # Hand-edited after jev-align round 2 (2026-09-25): train F1 0.40 -> 0.75 on 60 agent-labeled
    # turns. The examples quote training items, so confirm on fresh labels.
    "design_reversal": noul(
        "Does the newest human message reject how the agent built something it already implemented, "
        "asking for a different approach so that work gets redone?",
        "The agent already changed code or acted on a design, and the human wants that approach "
        "replaced, removed or built another way, even when asked as a question or alongside new "
        "requests. E.g. 'make --json 1-1 with the API response' after the agent built custom output "
        "types; 'why is the change so large, the fix is just to store tokens.json'; 'don't use python "
        "for the matrix, simpler way?'; 'stop, revert what you just did'; 'I deleted that method, "
        "check the code and fix the test'.",
        "Anything else, including: fixing a specific field, text or bug in work that is otherwise kept "
        "('we don't need the $schema field', 'no, we are keeping checkpointCount'); stopping or "
        "narrowing scope; the human changing their own earlier request; criticism of a plan the agent "
        "only proposed; a bare 'yes' to the agent's own offer to redo; review findings to repair; "
        "questions; new requirements; clarifying which scenario to test.",
    ),
    "found_not_filed": FOUND_NOT_FILED,
}

TAIL_QUESTIONS = {"found_not_filed": FOUND_NOT_FILED}

# The opening message has no agent work before it to correct, reverse or defer.
FIRST_TURN_QUESTIONS = {k: TURN_QUESTIONS[k] for k in
                        ("turn_kind", "resource_pointed_to", "agent_could_have_found_it")}

SESSION_QUESTIONS = {
    "task_type": choice(
        "What kind of task was this session?",
        feature="New feature",
        bugfix="Fix a bug",
        refactor="Restructure code without changing behavior",
        investigation="Find out how something works or why it fails",
        code_review="Review code or a PR",
        docs="Write or update docs",
        tests="Write or fix tests",
        ops_or_ci="Build, deploy, CI or tooling",
        other="Something else",
    ),
    "prompt_style": choice(
        "How did the human open the session?",
        pasted_spec="Pasted a spec or long requirements",
        links_issue_or_pr="Pointed to an issue or PR",
        one_line_ask="One short request",
        asks_for_plan_first="Asked for a plan before code",
        detailed_instructions="Several paragraphs of step-by-step instructions",
        question="Asked a question",
    ),
    "prompt_specificity": score(
        "How specific was the opening prompt?", "vague", "some detail", "precise spec"
    ),
    "planned_first": noul(
        "Did the agent propose or discuss a plan before editing code?",
        "Plan came before edits",
        "Went straight to edits",
    ),
    "tests_added": noul(
        "Did the session add or change tests?",
        "Tests added or changed",
        "No test changes",
    ),
    "outcome": choice(
        "How did the session end?",
        shipped_or_pr_ready="Work done, committed or PR ready",
        partial="Some work done, more left",
        abandoned="Dropped without a result",
        investigation_only="Answered a question, no code meant to ship",
        unclear="Can't tell",
    ),
}

REVIEW_QUESTIONS = {
    "comment_type": choice(
        "What is this review comment about?",
        bug="A bug or wrong behavior",
        style="Code style or naming",
        test_gap="Missing or weak tests",
        docs="Docs or comments",
        design="Design or structure",
        security="Security",
        nit="Minor nit",
        question="A question to the author",
    ),
    # Hand-edited after agent labeling (2026-09-25): train macro-F1 0.68 -> 0.97 on 60 agent-labeled
    # comments; held-out 0.72 -> 0.77 on 20 (accuracy 0.75 -> 0.95; its one miss is the lone outdated
    # item). Resolved beats outdated: the old wording sent resolved, outdated threads to outdated.
    # Declined with a reason counts as wrong; a comment with no finding counts as unclear.
    "held_up": choice(
        "Judging by the thread, did this review comment hold up?",
        valid_and_fixed="GitHub shows the thread resolved and no reply disputes it, whether or not it is also "
                        "outdated; or a reply says the problem was fixed, done or addressed, or the author did "
                        "what the comment asked.",
        valid_open="The comment names a concrete problem, nobody disputes it, and nothing says it was fixed: the "
                   "thread is unresolved and current, or it is a review body with findings and no replies.",
        outdated="Thread unresolved AND outdated with no reply, or a reply says the code it targeted was since "
                 "removed or rewritten so it no longer applies. A resolved thread is never outdated.",
        wrong="A reply disputes or declines the comment with a reason: not applicable, false positive, won't "
              "fix, kept on purpose, already handled elsewhere. Counts even when the thread is resolved.",
        unclear="The comment holds no concrete finding to judge: a PR overview or summary, 'found no new "
                "issues', a stale-review placeholder, a pointer to findings posted elsewhere, an approval or "
                "'tested', a status note, or a bare re-trigger such as '@cursor review' or 'bugbot run'.",
    ),
}

CHECK_QUESTIONS = {
    "failure_class": choice(
        "Why did this CI check fail?",
        lint_or_format="Lint or format error",
        test_regression="A test fails because of a code change",
        flaky_test="A test fails at random",
        build_error="Compile or build error",
        infra_or_env="Runner, network, setup or credentials",
        timeout="Ran out of time",
        other="Something else",
    ),
}

KEYS = {"turn_labels": ("session_id", "msg_idx"), "session_labels": ("session_id",),
        "event_labels": ("event_type", "event_id")}


# --- state building ---------------------------------------------------------

def clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 15] + " …[truncated]"


def clip_head(text: str, n: int) -> str:
    """Keep the end of text; for trimming the oldest part of a log."""
    return text if len(text) <= n else "[earlier activity trimmed]… " + text[-(n - 30):]


def one_line(text: str | None, n: int) -> str:
    return clip(" ".join((text or "").split()), n)


def tool_summary(call) -> str:
    try:
        args = json.loads(call["input"] or "{}")
    except json.JSONDecodeError:
        args = call["input"]
    if isinstance(args, dict):
        keys = ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "prompt")
        picked = [str(args[k]) for k in keys if args.get(k)]
        summary = picked[0] if picked else ", ".join(json.loads(call["files"] or "[]")) or json.dumps(args)
    else:
        summary = str(args)
    summary = re.sub(r"^cd \S+\s*(&&|;)\s*", "", summary.strip())  # drop the usual `cd <worktree> &&`
    line = f"- {call['name']}: {one_line(summary, 150)}"
    if call["is_error"]:
        out = " ".join((call["output"] or "").split())
        line += f"  ERROR: {out[-200:]}"
    return line


def activity_blocks(msgs: list, calls: dict) -> list[str]:
    """One block per message between two human turns; last two assistant texts kept whole."""
    assistant_idxs = [m["idx"] for m in msgs if m["role"] == "assistant" and (m["text"] or "").strip()]
    keep_whole = set(assistant_idxs[-2:])
    blocks = []
    for m in msgs:
        if m["role"] == "user":
            tag = "Earlier human message" if m["user_kind"] == "human" else "[injected context]"
            blocks.append(f"{tag}: {one_line(m['text'], 300)}")
            continue
        parts = []
        if (m["text"] or "").strip():
            text = m["text"].strip() if m["idx"] in keep_whole else clip(m["text"], OLD_TEXT_CAP)
            parts.append(f"\nAgent: {text}")
        parts += [tool_summary(c) for c in calls.get(m["idx"], [])]
        if parts:
            blocks.append("\n".join(parts))
    return blocks


def turn_state(blocks: list[str], human: str | None, first: bool) -> str:
    """Agent activity then the human message, capped at STATE_CAP, oldest activity trimmed first."""
    head_a = "## Agent activity since the previous human message\n"
    head_h = "\n\n## Human message\n"
    activity = "\n".join(blocks).strip()
    if not activity:
        activity = "(none: first message of the session)" if first else "(none)"
    room = STATE_CAP - len(head_a) - len(head_h)
    human = clip(human, max(HUMAN_MIN, room - len(activity)))
    while len(blocks) > 1 and len(activity) > room - len(human):
        blocks = blocks[1:]
        activity = "[earlier activity trimmed]\n" + "\n".join(blocks)
    activity = clip_head(activity, room - len(human))
    return head_a + activity + head_h + human


def tail_state(blocks: list[str]) -> str:
    return clip_head("## Agent activity after the last human message\n" + "\n".join(blocks).strip(), STATE_CAP)


def session_state(conn, sess, msgs: list, calls: dict) -> str:
    humans = [m for m in msgs if m["role"] == "user" and m["user_kind"] == "human"]
    prompt = humans[0]["text"] if humans else sess["prompt"]
    commits = conn.execute(
        "SELECT message FROM checkpoints WHERE session_id = ? ORDER BY ts", (sess["session_id"],)
    ).fetchall()
    all_calls = [c for cs in calls.values() for c in cs]
    written = {f for c in all_calls if c["writes"] for f in json.loads(c["files"] or "[]")}
    tail = [m["text"] for m in msgs if m["role"] == "assistant" and (m["text"] or "").strip()][-2:]
    lines = [
        f"## Session: {sess['display_name'] or '(no name)'}",
        f"\n## Opening human prompt\n{clip(prompt, PROMPT_CAP)}",
        "\n## Checkpoint commits",
        *[f"- {one_line((c['message'] or '').splitlines()[0] if c['message'] else '', 200)}" for c in commits],
        *(["(none)"] if not commits else []),
        (f"\n## Counts\nhuman messages: {len(humans)}, tool calls: {len(all_calls)}, "
        f"tool errors: {sum(1 for c in all_calls if c['is_error'])}, files written: {len(written)}"),
        "\n## Last agent messages",
        *[clip(t, TAIL_TEXT_CAP) for t in tail],
    ]
    return clip("\n".join(lines), STATE_CAP)


# --- work items -------------------------------------------------------------

def item(table: str, key: tuple, state: str, questions: dict, label: str) -> dict:
    return {"table": table, "key": key, "state": state, "questions": questions, "label": label}


def session_items(conn, session_filter: str | None) -> tuple[list, list, list]:
    """Build (turn, tail, session) items for every session with messages."""
    where, params = ("WHERE session_id = ?", (session_filter,)) if session_filter else ("", ())
    turns, tails, sessions = [], [], []
    for sess in conn.execute(f"SELECT * FROM sessions {where} ORDER BY started_at", params).fetchall():
        sid = sess["session_id"]
        msgs = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY idx", (sid,)).fetchall()
        if not msgs:
            continue
        calls: dict[int, list] = {}
        for c in conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY msg_idx, call_idx", (sid,)
        ):
            calls.setdefault(c["msg_idx"], []).append(c)

        since, first = [], True
        for m in msgs:
            if m["role"] == "user" and m["user_kind"] == "human":
                state = turn_state(activity_blocks(since, calls), m["text"], first)
                questions = FIRST_TURN_QUESTIONS if first else TURN_QUESTIONS
                turns.append(item("turn_labels", (sid, m["idx"]), state, questions,
                                  f"{sid[:8]} #{m['idx']} {one_line(m['text'], 60)}"))
                # Back-to-back human messages share the agent activity before the first one.
                if any(x["role"] == "assistant" for x in since):
                    since = []
                since.append(m)
                first = False
            else:
                since.append(m)
        last_assistant = [m["idx"] for m in since if m["role"] == "assistant"]
        blocks = activity_blocks([m for m in since if m["user_kind"] != "human"], calls)
        if last_assistant and blocks:
            tails.append(item("turn_labels", (sid, last_assistant[-1]), tail_state(blocks),
                              TAIL_QUESTIONS, f"{sid[:8]} #{last_assistant[-1]} (after last human)"))
        sessions.append(item("session_labels", (sid,), session_state(conn, sess, msgs, calls),
                             SESSION_QUESTIONS, f"{sid[:8]} {sess['display_name'] or ''}"))
    return turns, tails, sessions


def who(r) -> str:
    return f"{r['author']}{' (bot)' if r['is_bot'] else ''}"


def where(c) -> str:
    """Review-body comments have no path."""
    return f"{c['path']}:{c['line']}" if c["path"] else "the PR as a whole (review body)"


def thread_status(c) -> str:
    if c["kind"] == "review":
        return f"Review verdict: {c['review_state'] or 'unknown'}\n"
    flags = [("resolved" if c["is_resolved"] else "unresolved") if c["is_resolved"] is not None else None,
             ("outdated" if c["is_outdated"] else "current") if c["is_outdated"] is not None else None]
    return f"Thread: {', '.join(f for f in flags if f) or 'status unknown'}\n"


def event_items(conn) -> tuple[list, list]:
    reviews = []
    roots = conn.execute(  # skip review bodies with no text: plain approvals
        "SELECT * FROM gh_review_comments WHERE in_reply_to IS NULL "
        "AND NOT (kind = 'review' AND trim(coalesce(body, '')) = '') ORDER BY comment_id"
    ).fetchall()
    for c in roots:
        replies = conn.execute(
            "SELECT * FROM gh_review_comments WHERE in_reply_to = ? ORDER BY created_at", (c["comment_id"],)
        ).fetchall()
        state = "\n".join([
            f"## Review comment on {where(c)} (PR #{c['pr_number']}) by {who(c)}",
            thread_status(c),
            clip(c["body"], 8_000),
            "\n## Replies",
            *[f"- {who(r)}: {clip(r['body'], 2_000)}" for r in replies],
            *(["(none)"] if not replies else []),
        ])
        reviews.append(item("event_labels", ("review_comment", c["comment_id"]), clip(state, STATE_CAP),
                            REVIEW_QUESTIONS, f"comment {c['comment_id']} {one_line(c['body'], 50)}"))
    checks = []
    for c in conn.execute(
        # Skip the Entire approval gate and the 'test' rollup job, which only echoes other jobs.
        "SELECT * FROM gh_checks WHERE conclusion IN ('failure', 'timed_out') AND failure_log IS NOT NULL "
        "AND coalesce(app, '') != 'entire' AND name != 'test' ORDER BY check_id"
    ):
        head = f"## CI check: {c['name']} ({c['conclusion']})\n\n## Failure log (tail)\n"
        state = head + (c["failure_log"] or "")[-(STATE_CAP - len(head)):]
        checks.append(item("event_labels", ("check_failure", c["check_id"]), state,
                           CHECK_QUESTIONS, f"check {c['check_id']} {c['name']}"))
    return reviews, checks


# --- Jev --------------------------------------------------------------------

def input_hash(model: str, it: dict) -> str:
    payload = json.dumps([model, it["questions"], it["state"]], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def is_cached(conn, it: dict) -> bool:
    cols = KEYS[it["table"]]
    where = " AND ".join(f"{c} = ?" for c in cols)
    row = conn.execute(f"SELECT input_hash FROM {it['table']} WHERE {where} LIMIT 1", it["key"]).fetchone()
    return row is not None and row["input_hash"] == it["hash"]


def decide(client: httpx.Client, model: str, it: dict) -> dict:
    """POST one decision; retry 429/5xx and network errors with backoff."""
    body = {"model": model, "state": it["state"], "questions": it["questions"]}
    for attempt in range(6):
        try:
            r = client.post(URL, json=body)
        except httpx.TransportError:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("retry-after") or 2 ** attempt))
            continue
        if r.is_error:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"HTTP {r.status_code} after retries: {r.text[:300]}")


def to_rows(answers: dict, questions: dict) -> list[tuple]:
    """(field, value, confidence, probs_json) per answer; see module docstring."""
    rows = []
    for field, a in answers.items():
        kind = a.get("type")
        if kind == "noul":
            p = a["noul"]
            rows.append((field, str(p > 0.5).lower(), max(p, 1 - p), json.dumps({"true": p})))
        elif kind == "choice":
            probs = a.get("probabilities") or {}
            rows.append((field, a["choice"], probs.get(a["choice"], a.get("confidence")), json.dumps(probs)))
        elif kind == "score":
            levels = questions[field]["criteria"]
            legend = a.get("legend") or {str(i): lvl for i, lvl in enumerate(levels)}
            probs = {str(legend.get(k, k)): p for k, p in (a.get("probabilities") or {}).items()}
            if probs:
                best = max(probs, key=probs.get)
                rows.append((field, best, probs[best], json.dumps(probs)))
            else:
                rows.append((field, levels[round(a["score"])], a.get("confidence"), None))
            rows.append((f"{field}_score", str(round(a["score"], 3)), a.get("confidence"), None))
    return rows


def save(conn, model: str, it: dict, answers: dict) -> None:
    table, cols = it["table"], KEYS[it["table"]]
    where = " AND ".join(f"{c} = ?" for c in cols)
    rows = to_rows(answers, it["questions"])
    conn.execute(f"DELETE FROM {table} WHERE {where}", it["key"])  # drop fields from older question sets
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}, field, value, confidence, probs, model, input_hash, created_at) "
        f"VALUES ({', '.join('?' * (len(cols) + 7))})",
        [(*it["key"], f, v, c, p, model, it["hash"], now) for f, v, c, p in rows],
    )
    conn.commit()


# --- CLI --------------------------------------------------------------------

def dry_run(kinds: dict[str, list]) -> None:
    for kind, items in kinds.items():
        sizes = [len(it["state"]) for it in items]
        print(f"=== {kind}: {len(items)} items", end="")
        if sizes:
            print(f"  state chars min {min(sizes)} median {int(statistics.median(sizes))} max {max(sizes)}")
        else:
            print()
            continue
        print(json.dumps(items[0]["questions"], indent=2))
        for it in items[:2]:
            print(f"\n--- {it['label']}  ({len(it['state'])} chars)\n{it['state']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["turns", "sessions", "events"])
    parser.add_argument("--session", help="Only this session ID (turns and sessions)")
    parser.add_argument("--dry-run", action="store_true", help="Print states and questions; call nothing")
    parser.add_argument("--force", action="store_true", help="Ignore cached labels")
    parser.add_argument("--limit", type=int, help="At most N items of each kind")
    args = parser.parse_args()

    load_dotenv()
    model = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    conn = connect()

    kinds: dict[str, list] = {}
    if args.only in (None, "turns", "sessions"):
        turns, tails, sessions = session_items(conn, args.session)
        if args.only != "sessions":
            kinds |= {"turns": turns, "tails": tails}
        if args.only != "turns":
            kinds["sessions"] = sessions
    if args.only in (None, "events") and not args.session:
        kinds["review_comments"], kinds["check_failures"] = event_items(conn)

    for kind, items in kinds.items():
        for it in items:
            it["hash"] = input_hash(model, it)
        todo = [it for it in items if args.force or not is_cached(conn, it)]
        kinds[kind] = todo[: args.limit] if args.limit else todo
        if len(todo) < len(items):
            print(f"{kind}: {len(items) - len(todo)} cached, skipped")

    if args.dry_run:
        dry_run(kinds)
        return
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is empty in .env; use --dry-run or set it.")

    items = [it for its in kinds.values() for it in its]
    print(f"classifying {len(items)} items with {model}")
    cost, failed = 0.0, 0
    headers = {"Authorization": f"Bearer {key}", "X-OpenRouter-Title": "entire-insights"}
    with httpx.Client(headers=headers, timeout=120) as client, ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(decide, client, model, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                resp = fut.result()
            except Exception as e:  # noqa: BLE001 - report and keep going
                failed += 1
                print(f"FAIL {it['label']}: {e}")
                continue
            save(conn, model, it, resp["answers"])
            cost += (resp.get("usage") or {}).get("cost") or 0
            print(f"ok   {it['label']}")
    print(f"done: {len(items) - failed} ok, {failed} failed, cost ${cost:.4f}")


if __name__ == "__main__":
    main()

"""Build the debrief site: copy the static files to SITE_OUT and write its data beside them.

data/index.json holds everything the overview pages need (recommendations, findings, resolved
evidence, per-session stats, flagged turns, CI and review rows, skill uses). Session
transcripts load on demand from shard files, data/sessions/sNN.json, each a JSON object
of {session_id: transcript}; each session in index.json names its shard.

Every table but sessions and messages may be empty. meta.has tells the site which optional
data exists (GitHub, people, checkpoints, projects, labels) so it can hide the rest.

    uv run python -m debrief.export_site
    uv run python -m debrief.export_site --run 20260925T182434Z --since 2026-08-11
"""

import argparse
import json
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime

from debrief import skills
from debrief.db import connect
from debrief.paths import SITE_OUT, SITE_SRC
from debrief.recommend import LEVERS
from debrief.relay import is_relayed

MIN_CONF = 0.7          # same default as synthesize.py
PR_KEY = "p.repo || '#' || p.pr_number"  # how the export names a PR: owner/name#12; p is the PR row
SUBTYPE_CONF = 0.5      # correction_type on a turn already labeled a correction
MSG_CAP = 8_000         # chars of message text in a session file
TOOL_IN_CAP = 300
TOOL_OUT_CAP = 800      # output kept for failed calls only
SHARD_BYTES = 1_500_000  # target size of a transcript shard file

# Friction signals per human turn: code -> (field, value).
SIGNALS = {
    "correction": ("turn_kind", "correction"),
    "reversal": ("design_reversal", "true"),
    "defect": ("human_caught_defect", "true"),
    "could_find": ("agent_could_have_found_it", "true"),
    "not_filed": ("found_not_filed", "true"),
}
CORRECTING = ("correction", "reversal", "defect")  # together, the site's "Agent corrected"

# Secret-shaped strings, redacted from every string before it is serialized. Redacting the
# serialized JSON instead misses a secret that follows an escape: in "\nghp_..." the "n" of
# the escape is a word character, so \b never matches.
SECRETS = [
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"), "[redacted]"),
    (re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}"), "[redacted]"),  # needs a digit
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"), "[redacted]"),  # Stripe
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted]"),
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "[redacted]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}"), "[redacted]"),  # Google API key
    (re.compile(r"\b(?:npm|hf)_[A-Za-z0-9]{30,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL), "[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{24,}=*"), r"\1[redacted]"),
    (re.compile(r"(https?://)[^/\s:@\"\\]+:[^/\s@\"\\]+@"), r"\1[redacted]@"),
]


def redact(text: str) -> str:
    """Redact secrets in one plain (not JSON-encoded) string."""
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return text


# A run of non-space text that holds U+FFFD and is at least this long without it is blanked whole.
# Shorter than any token pattern above can match, so no secret touching a mark survives.
DAMAGED_MIN = 16
DAMAGED_RUN = re.compile(r"\S*\ufffd\S*")


# In damaged text a private key's header may not survive well enough to match, so any text
# that spells PRIVATE KEY once marks and spaces are ignored is cut from its first dashes on.
DAMAGED_KEY = re.compile(r"P\W*R\W*I\W*V\W*A\W*T\W*E\W*K\W*E\W*Y")
DAMAGED_KEY_END = re.compile(r"E\W*N\W*D\b[^\n]{0,60}?-{3,}")


def redact_damaged(text: str) -> str:
    """Redact a string holding U+FFFD, which marks bytes the source could not decode, then drop it.

    A mark can sit inside a secret, or be the only break before or after one, in any mix, so
    no single way of reading the marks finds every secret. Three steps, each on what the
    last one left:
    1. blank what the patterns find with the marks removed, and with the marks as spaces
       (this is what catches a secret of several words, such as a private key or a bearer
       token, whose first word a mark touches);
    2. blank any long run of text that still touches a mark, secret or not;
    3. the normal pass.
    """
    key = DAMAGED_KEY.search(text)
    if key:
        start = max(text.rfind("---", 0, key.start()), 0)
        start = len(text[:start].rstrip("-")) if start else 0
        ends = [m.end() for m in DAMAGED_KEY_END.finditer(text, key.end())]
        text = text[:start] + "[redacted]" + (text[ends[-1]:].lstrip("-") if ends else "")
    keep = [i for i, c in enumerate(text) if c != "\ufffd"]
    joined = "".join(text[i] for i in keep)
    spaced = text.replace("\ufffd", " ")
    spans = []
    for pattern, _ in SECRETS:
        spans += [(keep[m.start()], keep[m.end() - 1] + 1) for m in pattern.finditer(joined) if m.end() > m.start()]
        spans += [m.span() for m in pattern.finditer(spaced)]
    out, at = [], 0
    for start, end in sorted(spans):
        if start >= at:
            out += [text[at:start], "[redacted]"]
        at = max(at, end)
    text = "".join([*out, text[at:]])

    def blank(m: re.Match) -> str:
        rest = m.group().replace("\ufffd", "")
        return "[redacted]" if len(rest) >= DAMAGED_MIN else rest
    return redact(DAMAGED_RUN.sub(blank, text))


def redact_tree(obj):
    """Redact every string, keys included, in a JSON-like structure, and drop U+FFFD (the artifact host rejects it)."""
    if isinstance(obj, str):
        return redact_damaged(obj) if "\ufffd" in obj else redact(obj)
    if isinstance(obj, (list, tuple)):
        return [redact_tree(v) for v in obj]
    if isinstance(obj, dict):
        return {redact_tree(k): redact_tree(v) for k, v in obj.items()}
    return obj


def excerpt(text, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def tail(text, n: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def labels_by_turn(conn) -> dict[tuple, dict]:
    """Labels per labeled human turn, plus a "relayed" pseudo-label on pasted bot output."""
    out: dict[tuple, dict] = defaultdict(dict)
    for r in conn.execute("SELECT session_id, msg_idx, field, value, confidence FROM turn_labels"):
        out[(r[0], r[1])][r[2]] = (r[3], r[4] or 0)
    for sid, idx, text in conn.execute("SELECT session_id, idx, text FROM messages "
                                       "WHERE role = 'user' AND user_kind = 'human'"):
        if (sid, idx) in out and is_relayed(text):
            out[(sid, idx)]["relayed"] = ("true", 1.0)
    return out


def signals_of(labels: dict) -> list[str]:
    """A relayed bot finding isn't the person correcting the agent; the agent could have fetched it."""
    relayed = "relayed" in labels
    return [code for code, (field, value) in SIGNALS.items()
            if (relayed and code == "could_find")
            or (not (relayed and code in CORRECTING)
                and labels.get(field, (None, 0))[0] == value and labels[field][1] >= MIN_CONF)]


def correction_type(labels: dict) -> str:
    value, conf = labels.get("correction_type", (None, 0))
    return value if value not in (None, "none") and conf >= SUBTYPE_CONF else "unclassified"


# --- evidence -------------------------------------------------------------------------

def ref_id(ref: dict) -> str:
    if "session_id" in ref:
        return f"m:{ref['session_id']}:{ref['msg_idx']}"
    return f"e:{ref['event_type']}:{ref['event_id']}"


def resolve_msg(conn, labels, session_id: str, idx: int) -> dict:
    m = conn.execute("SELECT role, text FROM messages WHERE session_id = ? AND idx = ?", (session_id, idx)).fetchone()
    prev = conn.execute(
        "SELECT MAX(idx) FROM messages WHERE session_id = ? AND idx < ? AND role = 'user' AND user_kind = 'human'",
        (session_id, idx)).fetchone()[0]
    end = idx if m and m[0] == "assistant" else idx - 1
    agent = [r[0] for r in conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND idx > ? AND idx <= ? AND role = 'assistant' "
        "AND text != '' ORDER BY idx", (session_id, -1 if prev is None else prev, end))]
    lab = labels.get((session_id, idx), {})
    return {"type": "msg", "session_id": session_id, "msg_idx": idx, "role": m[0] if m else None,
            "text": excerpt(m[1] if m else "", 500), "agent_before": tail(" ".join(" ".join(agent).split()), 400),
            "signals": signals_of(lab), "correction_type": correction_type(lab) if "correction" in signals_of(lab)
            else None}


def event_label(conn, event_type: str, event_id: int, field: str) -> str | None:
    r = conn.execute("SELECT value FROM event_labels WHERE event_type = ? AND event_id = ? AND field = ?",
                     (event_type, event_id, field)).fetchone()
    return r[0] if r else None


def resolve_check(conn, check_id: int) -> dict:
    c = conn.execute("SELECT * FROM gh_checks WHERE check_id = ?", (check_id,)).fetchone()
    if not c:
        return {"type": "missing"}
    prs = [r[0] for r in conn.execute(f"SELECT {PR_KEY} FROM gh_commit_prs p WHERE commit_sha = ?", (c["commit_sha"],))]
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM session_commits WHERE commit_sha = ?", (c["commit_sha"],))]
    return {"type": "check", "check_id": check_id, "name": c["name"], "conclusion": c["conclusion"],
            "failure_class": event_label(conn, "check_failure", check_id, "failure_class"),
            "commit": (c["commit_sha"] or "")[:10], "prs": prs, "sessions": sessions,
            "log": tail(c["failure_log"], 1200), "at": c["completed_at"]}


def resolve_review(conn, comment_id: int) -> dict:
    c = conn.execute("SELECT * FROM gh_review_comments WHERE comment_id = ?", (comment_id,)).fetchone()
    if not c:
        return {"type": "missing"}
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM session_pr_links WHERE repo = ? AND pr_number = ?",
        (c["repo"], c["pr_number"]))]
    return {"type": "review", "comment_id": comment_id, "sessions": sessions, "author": c["author"],
            "is_bot": bool(c["is_bot"]), "kind": c["kind"], "path": c["path"], "line": c["line"], "pr": f"{c['repo']}#{c['pr_number']}",
            "body": excerpt(c["body"], 600), "resolved": c["is_resolved"], "outdated": c["is_outdated"],
            "comment_type": event_label(conn, "review_comment", comment_id, "comment_type"),
            "held_up": event_label(conn, "review_comment", comment_id, "held_up")}


def resolve(conn, labels, ref: dict) -> dict:
    if "session_id" in ref:
        return resolve_msg(conn, labels, ref["session_id"], int(ref["msg_idx"]))
    if ref["event_type"] == "check_failure":
        return resolve_check(conn, int(ref["event_id"]))
    return resolve_review(conn, int(ref["event_id"]))


# --- sections -------------------------------------------------------------------------

def findings_and_recs(conn, labels, run_id: str) -> tuple[dict, list, dict]:
    findings, refs = {}, {}
    for r in conn.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY finding_id", (run_id,)):
        evidence = json.loads(r["evidence"] or "[]")
        for ref in evidence:
            refs.setdefault(ref_id(ref), resolve(conn, labels, ref))
        findings[r["finding_id"]] = {"id": r["finding_id"], "kind": r["kind"], "title": r["title"],
                                     "detail": r["detail"], "fix": r["suggested_fix"],
                                     "evidence": [ref_id(e) for e in evidence]}
    # verify_evidence.py's verdicts: rec_id -> ref ids that support the idea. No rows: not checked.
    supported: dict[int, set] = {}
    for rec_id, ref, ok in conn.execute("SELECT rec_id, ref, supports FROM evidence_checks"):
        hits = supported.setdefault(rec_id, set())  # an idea whose refs all failed is still checked
        if ok:
            hits.add(ref)
    recs = []
    for r in conn.execute("SELECT * FROM recommendations WHERE run_id = ? ORDER BY priority", (run_id,)):
        ids = [i for i in json.loads(r["finding_ids"] or "[]") if i in findings]
        cited = list(dict.fromkeys(e for i in ids for e in findings[i]["evidence"]))
        checked = r["rec_id"] in supported
        evidence = [e for e in cited if e in supported[r["rec_id"]]] if checked else cited
        # Reach (sessions, people) comes from `evidence` in main(), so it counts supported refs only.
        recs.append({"id": r["rec_id"], "priority": r["priority"], "lever": r["lever"], "title": r["title"],
                     "problem": r["problem"], "change": r["change"], "target": r["target"], "effort": r["effort"],
                     "impact": r["impact"], "findings": ids, "evidence": evidence, "evidence_all": len(cited),
                     "checked": checked, "weak": checked and not evidence})
    return findings, recs, refs


def evidence_sessions(ref: dict) -> list[str]:
    if ref["type"] == "msg":
        return [ref["session_id"]]
    return ref.get("sessions", [])


def sessions_section(conn, labels, since: str) -> tuple[list, list]:
    """Per-session stats, and every human turn with a friction signal, for sessions last active since `since`."""
    slabels: dict[str, dict] = defaultdict(dict)
    for r in conn.execute("SELECT session_id, field, value, confidence FROM session_labels"):
        slabels[r[0]][r[1]] = r[2] if (r[3] or 0) >= 0.5 or r[1].endswith("_score") else None
    prs: dict[str, list] = defaultdict(list)
    for r in conn.execute(f"""
        SELECT l.session_id, {PR_KEY}, CASE WHEN p.merged = 1 THEN 'merged' ELSE p.state END
        FROM session_pr_links l JOIN gh_prs p ON p.repo = l.repo AND p.pr_number = l.pr_number
        ORDER BY p.repo, p.pr_number"""):
        prs[r[0]].append({"n": r[1], "state": r[2]})
    tools = {r[0]: (r[1], r[2] or 0) for r in conn.execute(
        "SELECT session_id, COUNT(*), SUM(is_error) FROM tool_calls GROUP BY session_id")}
    # app 'entire' is Entire's approval gate, which fails until a person approves: not a CI failure.
    ci_fail = dict(conn.execute("""
        SELECT cp.session_id, COUNT(DISTINCT k.check_id) FROM gh_checks k
        JOIN session_commits cp ON cp.commit_sha = k.commit_sha
        WHERE k.conclusion = 'failure' AND COALESCE(k.app, '') != 'entire'
        GROUP BY cp.session_id"""))
    turns_by_session: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT session_id, idx, text, ts FROM messages "
                          "WHERE role = 'user' AND user_kind = 'human' ORDER BY session_id, idx"):
        turns_by_session[r[0]].append(r)
    # Entire's started_at is the first checkpoint, often long after the first message.
    first_ts = dict(conn.execute("SELECT session_id, MIN(ts) FROM messages WHERE ts IS NOT NULL GROUP BY session_id"))

    sessions, flagged = [], []
    for s in conn.execute("SELECT * FROM sessions WHERE COALESCE(last_activity_at, started_at, '') >= ? "
                          "ORDER BY started_at", (since,)):
        sid = s["session_id"]
        started = first_ts.get(sid) or s["started_at"] or s["last_activity_at"]
        kinds: Counter = Counter()
        friction = []
        for t in turns_by_session.get(sid, []):
            lab = labels.get((sid, t[1]), {})
            kinds[lab.get("turn_kind", ("unlabeled",))[0] or "unlabeled"] += 1
            sig = [x for x in signals_of(lab) if x != "not_filed"]
            if sig:
                friction.append([t[1], sig])
                flagged.append({"s": sid, "i": t[1], "sig": sig, "ctype": correction_type(lab)
                                if "correction" in sig else None, "text": excerpt(t[2], 260), "ts": t[3]})
        not_filed = [idx for (ss, idx), lab in labels.items() if ss == sid and "not_filed" in signals_of(lab)]
        sl = slabels.get(sid, {})
        n_tools, n_err = tools.get(sid, (0, 0))
        count = Counter(sig for _, sigs in friction for sig in sigs)
        pr_list = prs.get(sid, [])
        sessions.append({
            "id": sid, "name": s["display_name"], "agent": s["agent"], "model": s["model"],
            "author": s["author"] or None, "started": started, "last": s["last_activity_at"] or started,
            # The folder the session ran in; the site shows its last part and filters on the whole path.
            "project": s["cwd"] or None,  # db.connect adds the column to old databases
            "human_turns": len(turns_by_session.get(sid, [])), "turn_kinds": dict(kinds),
            "corrections": count["correction"], "reversals": count["reversal"], "defects": count["defect"],
            "could_find": count["could_find"], "not_filed": len(not_filed), "friction": friction,
            "relayed": sum("relayed" in labels.get((sid, t[1]), {}) for t in turns_by_session.get(sid, [])),
            "tool_calls": n_tools, "tool_errors": n_err, "checkpoints": s["checkpoint_count"],
            "outcome": sl.get("outcome"), "task_type": sl.get("task_type"), "prompt_style": sl.get("prompt_style"),
            "specificity": sl.get("prompt_specificity"), "planned_first": sl.get("planned_first"),
            "tests_added": sl.get("tests_added"), "prs": pr_list,
            "settled": any(p["state"] in ("merged", "closed") for p in pr_list),
            "ci_failures": ci_fail.get(sid, 0), "prompt": excerpt(s["prompt"], 400),
        })
    return sessions, flagged


def hotspot_files(conn, labels, ids: set[str]) -> list[dict]:
    """Repo files the agent wrote in the stretch before a correction, reversal or caught defect, in sessions `ids`."""
    # shortcut: paths are relative to each session's repo, so src/ in two repos counts as one place.
    # sessions.cwd can't split them: worktrees of one repo have different cwds. Split once sessions name their repo.
    files: dict[str, dict] = {}
    rows = conn.execute("""
        SELECT m.session_id, m.idx,
            COALESCE((SELECT MAX(p.idx) FROM messages p WHERE p.session_id = m.session_id AND p.idx < m.idx
                      AND p.role = 'user' AND p.user_kind = 'human'), -1) AS prev
        FROM messages m WHERE m.role = 'user' AND m.user_kind = 'human'""").fetchall()
    for sid, idx, prev in rows:
        sig = [x for x in signals_of(labels.get((sid, idx), {})) if x in CORRECTING]
        if not sig or sid not in ids:
            continue
        paths = {r[0] for r in conn.execute("""
            SELECT DISTINCT f.value FROM tool_calls tc JOIN json_each(tc.files) f
            WHERE tc.session_id = ? AND tc.msg_idx > ? AND tc.msg_idx < ? AND tc.writes = 1
              AND f.value NOT LIKE '/%'""", (sid, prev, idx))}
        for path in paths:
            f = files.setdefault(path, {"path": path, "sessions": set(), "turns": []})
            f["sessions"].add(sid)
            f["turns"].append({"s": sid, "i": idx, "sig": sig})
    return sorted(({**f, "sessions": sorted(f["sessions"])} for f in files.values()),
                  key=lambda f: (-len(f["sessions"]), -len(f["turns"])))


# Where a failed CI run broke, read from the tail of its log.
PKG_RE = re.compile(r"^FAIL[ \t]+([\w.-]+\.[a-z]+/\S+)", re.MULTILINE)  # go test: FAIL<tab>host/owner/repo/pkg<tab>1.2s
MODULE_RE = re.compile(r"^[\w.-]+\.[a-z]+/[^/]+/[^/]+")  # a Go module path: host/owner/repo
GOTESTSUM_RE = re.compile(r"^=== FAIL: (\S+)(?: (Test\S+))?", re.MULTILINE)  # gotestsum: === FAIL: pkg TestX
TEST_RE = re.compile(r"^\s*--- FAIL: (Test\S+)", re.MULTILINE)
RERUN_RE = re.compile(r"^\s*mise run test:\S+ .*?(Test\w+)\s*$", re.MULTILINE)  # e2e report's rerun hints
FILE_RE = re.compile(r"(?<![\w./\\@-])((?:[A-Za-z]:)?[\w./\\-]*?[\w-]+\.(?:go|sh|ps1|ya?ml)):\d+")
# GitHub's checkout dirs, Linux and Windows: <work>/<repo>/<repo>/
RUNNER_RE = re.compile(r"^(?:/?home/runner/work/([^/]+)/\1/|[A-Za-z]:/a/([^/]+)/\2/)")
MAX_FILES = 5


def failure_places(log: str | None) -> dict:
    """Failing packages, tests and files named in a CI log, plus the best single area."""
    log = log or ""
    pkgs, tests, files = [], [], []

    def add(xs: list, x: str) -> None:
        if x and x not in xs:
            xs.append(x)

    def pkg(p: str) -> str:
        p = MODULE_RE.sub("", p).strip("/")
        return p or "(root)"

    for m in PKG_RE.finditer(log):
        add(pkgs, pkg(m.group(1)))
    for m in GOTESTSUM_RE.finditer(log):
        add(pkgs, pkg(m.group(1)))
        if m.group(2):
            add(tests, m.group(2).split("/")[0])
    for rx in (TEST_RE, RERUN_RE):
        for m in rx.finditer(log):
            add(tests, m.group(1).split("/")[0])
    # Skip the first line: the log is a tail, so it may start mid-word.
    for m in FILE_RE.finditer(log.partition("\n")[2]):
        path = RUNNER_RE.sub("", m.group(1).replace("\\", "/"))
        if ":" in path or path.startswith("/") or "/go/" in path or "mise/installs" in path:
            continue  # outside the repo: Go toolchain, module cache
        if path != "testing.go" and not path.startswith("_"):  # Go's test runner and generated main
            add(files, path)
    # A bare name adds nothing once the same file shows up with its path.
    full = {f.rsplit("/", 1)[-1] for f in files if "/" in f}
    files = [f for f in files if "/" in f or f not in full]
    area = pkgs[0] if pkgs else next((f.rsplit("/", 1)[0] for f in files if "/" in f), None)
    return {"pkgs": pkgs, "tests": tests, "files": files[:MAX_FILES], "area": area}


def ci_section(conn, since: str) -> dict:
    """Failed runs on settled PRs that finished inside the window."""
    rows = conn.execute(f"""
        SELECT k.check_id, k.name, k.failure_log, k.completed_at,
            (SELECT value FROM event_labels l WHERE l.event_type = 'check_failure' AND l.event_id = k.check_id
               AND l.field = 'failure_class') AS class,
            EXISTS (SELECT 1 FROM gh_checks s WHERE s.commit_sha = k.commit_sha AND s.name = k.name
                    AND s.conclusion = 'success' AND s.completed_at > k.completed_at) AS flaky,
            (SELECT {PR_KEY} FROM gh_commit_prs g JOIN settled_prs p ON p.repo = g.repo AND p.pr_number = g.pr_number
             WHERE g.commit_sha = k.commit_sha ORDER BY g.pr_number LIMIT 1) AS pr
        FROM gh_checks k
        WHERE k.conclusion = 'failure' AND COALESCE(k.app, '') != 'entire'
          AND k.completed_at >= ?""", (since,)).fetchall()
    # Completed check runs per settled PR, so the site can scope failure rates to a set of sessions.
    runs = dict(conn.execute(f"""
        SELECT {PR_KEY}, COUNT(DISTINCT k.check_id) FROM gh_checks k
        JOIN gh_commit_prs g ON g.commit_sha = k.commit_sha
        JOIN settled_prs p ON p.repo = g.repo AND p.pr_number = g.pr_number
        WHERE k.conclusion IS NOT NULL AND COALESCE(k.app, '') != 'entire'
          AND k.completed_at >= ?
        GROUP BY 1""", (since,)))
    failures = [{"id": r[0], "name": r[1], "at": r[3], "class": r[4], "flaky": bool(r[5]), "pr": r[6],
                 **failure_places(r[2])} for r in rows if r[6] is not None]
    return {"runs_by_pr": runs, "failures": failures}


def review_section(conn) -> list[dict]:
    rows = conn.execute(f"""
        SELECT c.comment_id, c.author, c.is_bot, c.kind, {PR_KEY}, c.is_resolved, c.is_outdated, c.path,
            (SELECT value FROM event_labels l WHERE l.event_type = 'review_comment' AND l.event_id = c.comment_id
               AND l.field = 'comment_type') AS type,
            (SELECT value FROM event_labels l WHERE l.event_type = 'review_comment' AND l.event_id = c.comment_id
               AND l.field = 'held_up') AS held
        FROM gh_review_comments c JOIN settled_prs p ON p.repo = c.repo AND p.pr_number = c.pr_number
        WHERE c.in_reply_to IS NULL""").fetchall()
    return [{"id": r[0], "author": r[1], "bot": bool(r[2]), "kind": r[3], "pr": r[4], "resolved": r[5],
             "outdated": r[6], "path": r[7] or None, "type": r[8], "held": r[9]} for r in rows]


def prs_section(conn) -> dict:
    return {r[0]: {"title": r[1], "state": "merged" if r[3] else r[2], "url": r[4]}
            for r in conn.execute(f"SELECT {PR_KEY}, title, state, merged, url FROM gh_prs p")}


def session_file(conn, labels, sid: str) -> dict:
    calls: dict[int, list] = defaultdict(list)
    for c in conn.execute("SELECT msg_idx, name, input, output, is_error, files, writes FROM tool_calls "
                          "WHERE session_id = ? ORDER BY msg_idx, call_idx", (sid,)):
        try:
            args = json.loads(c["input"] or "{}")
        except json.JSONDecodeError:
            args = c["input"]
        if isinstance(args, dict):
            keys = ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "skill")
            summary = next((str(args[k]) for k in keys if args.get(k)), None) or json.dumps(args)
        else:
            summary = str(args)
        call = {"n": c["name"], "in": excerpt(summary, TOOL_IN_CAP)}
        if c["is_error"]:
            call["err"] = tail(c["output"], TOOL_OUT_CAP)
        if c["writes"]:
            call["w"] = json.loads(c["files"] or "[]")
        calls[c["msg_idx"]].append(call)
    msgs = []
    for m in conn.execute("SELECT idx, role, user_kind, ts, text FROM messages WHERE session_id = ? ORDER BY idx",
                          (sid,)):
        text = m["text"] or ""
        item = {"i": m["idx"], "r": m["role"], "ts": m["ts"],
                "t": text if len(text) <= MSG_CAP else text[:MSG_CAP] + "\n…[truncated]"}
        if m["role"] == "user":
            item["k"] = m["user_kind"]
        if m["idx"] in calls:
            item["c"] = calls[m["idx"]]
        lab = labels.get((sid, m["idx"]))
        if lab:
            item["sig"] = signals_of(lab)
            item["l"] = {f: [v, round(c, 2)] for f, (v, c) in lab.items() if v not in ("none", "false", None)}
        msgs.append(item)
    checkpoints = [{"sha": (r[0] or "")[:10], "msg": excerpt((r[1] or "").split("\n")[0], 120), "ts": r[2]}
                   for r in conn.execute("SELECT commit_sha, message, ts FROM checkpoints WHERE session_id = ? "
                                         "ORDER BY ts", (sid,))]
    return {"id": sid, "messages": msgs, "checkpoints": checkpoints}


def pack_shards(blobs: dict[str, str]) -> list[list[str]]:
    """Group session ids into shards of about SHARD_BYTES, largest first; a session bigger than
    that gets a shard to itself."""
    shards: list[list[str]] = []
    sizes: list[int] = []
    for sid in sorted(blobs, key=lambda k: -len(blobs[k].encode())):
        n = len(blobs[sid].encode())
        slot = next((i for i, used in enumerate(sizes) if used + n <= SHARD_BYTES), None)
        if slot is None:
            shards.append([])
            sizes.append(0)
            slot = len(shards) - 1
        shards[slot].append(sid)
        sizes[slot] += n
    return shards


def skills_section(conn, ids: set[str]) -> list[dict]:
    return [{"s": u["session_id"], "i": u["msg_idx"], "skill": u["skill"], "by": u["invoked_by"], "ts": u["ts"]}
            for u in skills.uses(conn) if u["session_id"] in ids]


def volume(conn, ids: set[str]) -> dict:
    """How much raw material the analysis read for sessions `ids`, for the methodology page."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS window_sessions (session_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM window_sessions")
    conn.executemany("INSERT INTO window_sessions VALUES (?)", [(i,) for i in ids])
    # PRs, checks and review comments reach the window through the sessions' PRs.
    prs = ("SELECT DISTINCT repo, pr_number FROM session_pr_links "
           "WHERE session_id IN (SELECT session_id FROM window_sessions)")
    checks = (f"SELECT k.* FROM gh_checks k JOIN gh_commit_prs g ON g.commit_sha = k.commit_sha "
              f"WHERE (g.repo, g.pr_number) IN ({prs})")
    in_window = "session_id IN (SELECT session_id FROM window_sessions)"

    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "messages": one(f"SELECT COUNT(*) FROM messages WHERE {in_window}"),
        "tool_calls": one(f"SELECT COUNT(*) FROM tool_calls WHERE {in_window}"),
        "prs": one(f"SELECT COUNT(*) FROM ({prs})"),
        "checks": one(f"SELECT COUNT(DISTINCT check_id) FROM ({checks})"),
        "check_failures": one(f"SELECT COUNT(DISTINCT check_id) FROM ({checks}) WHERE conclusion = 'failure'"),
        "review_comments": one(f"SELECT COUNT(*) FROM gh_review_comments WHERE (repo, pr_number) IN ({prs})"),
        "labels": one(f"SELECT (SELECT COUNT(*) FROM turn_labels WHERE {in_window}) "
                      f"+ (SELECT COUNT(*) FROM session_labels WHERE {in_window})"),
    }


def repo_of(prs: dict) -> str | None:
    """owner/name when every PR is in one repo; None with several, or without GitHub data."""
    repos = {key.rsplit("#", 1)[0] for key in prs}
    return repos.pop() if len(repos) == 1 else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", help="Findings run_id (default: latest with recommendations)")
    parser.add_argument("--since", default="", metavar="YYYY-MM-DD",
                        help="Only sessions active since this date (default: all)")
    args = parser.parse_args(argv)

    conn: sqlite3.Connection = connect()
    run_id = args.run or conn.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0] \
        or conn.execute("SELECT MAX(run_id) FROM findings").fetchone()[0]
    labels = labels_by_turn(conn)
    findings, recs, refs = findings_and_recs(conn, labels, run_id)
    sessions, flagged = sessions_section(conn, labels, args.since)
    authors = {s["id"]: s["author"] for s in sessions}
    projects = {s["id"]: s["project"] for s in sessions}
    prs = prs_section(conn)
    for rec in recs:
        touched = {sid for e in rec["evidence"] for sid in evidence_sessions(refs[e])}
        rec["sessions"] = sorted(touched)
        rec["people"] = len({authors.get(s) for s in touched} - {None})
    started = [s["started"] for s in sessions if s["started"]]
    vol = volume(conn, set(authors))
    index = {
        "meta": {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "run_id": run_id,
                 "from": args.since or min(started, default=None), "to": max(started, default=None),
                 "sessions": len(sessions), "repo": repo_of(prs), "min_conf": MIN_CONF,
                 "human_turns": sum(s["human_turns"] for s in sessions), **vol,
                 # Optional data. The site hides pages, columns and tiles for whatever is False.
                 "has": {"github": bool(prs),
                         "people": len(set(authors.values()) - {None}) > 1,  # one person: nothing to compare
                         "checkpoints": any(s["checkpoints"] for s in sessions),
                         "projects": any(projects.values()),
                         "labels": vol["labels"] > 0}},
        "levers": LEVERS, "recommendations": recs, "findings": findings, "refs": refs,
        "sessions": sessions, "flagged": flagged, "files": hotspot_files(conn, labels, set(authors))[:60],
        "ci": ci_section(conn, args.since), "reviews": review_section(conn), "prs": prs,
        "skills": skills_section(conn, set(authors)),
    }
    SITE_OUT.mkdir(parents=True, exist_ok=True)
    for static in SITE_SRC.iterdir():
        if static.is_file():
            shutil.copy(static, SITE_OUT / static.name)
    out = SITE_OUT / "data"
    shard_dir = out / "sessions"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for stale in shard_dir.glob("*.json"):
        stale.unlink()
    blobs = {s["id"]: json.dumps(redact_tree(session_file(conn, labels, s["id"])), ensure_ascii=False,
                                separators=(",", ":"))
             for s in sessions}
    shards = pack_shards(blobs)
    shard_of = {}
    for n, ids in enumerate(shards):
        name = f"s{n:02d}"
        (shard_dir / f"{name}.json").write_text("{" + ",".join(f"{json.dumps(sid)}:{blobs[sid]}" for sid in ids) + "}")
        shard_of.update(dict.fromkeys(ids, name))
    for s in sessions:
        s["shard"] = shard_of[s["id"]]
    (out / "index.json").write_text(json.dumps(redact_tree(index), ensure_ascii=False, separators=(",", ":")))
    size = sum(p.stat().st_size for p in out.rglob("*.json"))
    print(f"run {run_id or '(none yet)'}: {len(recs)} recommendations, {len(findings)} findings, {len(sessions)} sessions "
          f"in {len(shards)} shards; {size / 1e6:.1f} MB in {out}")


if __name__ == "__main__":
    main()

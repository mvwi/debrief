"""Load sessions straight from agent logs on disk: no Entire account needed.

Reads Claude Code logs (~/.claude/projects), Codex logs (~/.codex/sessions) and any folder
of .jsonl logs given with --import (Claude Code, Codex or Pi; the format is detected per
file). A re-run reads only files whose size or modified time changed, and replaces those
sessions' rows.

    uv run python -m debrief.sources
    uv run python -m debrief.sources --project ~/Dev/myrepo --since 2026-10-01
    uv run python -m debrief.sources --import ~/Downloads/logs

Left out on purpose, and counted in the summary:
  sub-agent logs   Claude Code keeps each sub-agent's turns in its own file. No person
                   typed them, and the parent session already records the call and its result.
  headless runs    `claude -p` and `codex exec`: debrief's own model calls are such runs,
                   and must not come back in as sessions. Also sessions a program started
                   (through the Claude SDK, or a Codex wrapper app) with fewer than
                   two human turns.
                   --include-headless loads them all.
  no human turn    empty files, metadata-only files, and copies with nothing new in them.
  copied entries   a resumed or forked session's log starts with a copy of the earlier
                   log. Each entry loads into one session only, so no turn counts twice.
A session still running loads as far as it has got; the next run replaces it.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from functools import cache
from pathlib import Path

from debrief import db
from debrief.normalize import PARSER_VERSION, PARSERS, read_jsonl, safe_session_id, write_log

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
# How the agent was started, as its log records it. `claude -p` logs entrypoint "sdk-cli";
# `codex exec` logs source "exec". Other "sdk-*" entrypoints are programs driving Claude Code:
# mostly one-prompt jobs, but also wrapper apps a person talks through. Those count as
# headless only below SDK_MIN_TURNS human turns. Codex is judged the same way by
# session_meta.originator: its own front ends name themselves "codex...", anything else
# is a wrapper. (`source` is no help: wrapper apps log "vscode", as the IDE extension does.)
HEADLESS_CLAUDE = {"sdk-cli"}
SDK_MIN_TURNS = 2
HEADLESS_CODEX = {"exec"}
GITDIR = re.compile(r"gitdir: (.+?)/\.git/worktrees/")
GITHUB_URL = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
# The main repo root an earlier load recorded for a folder: git's own answer, kept for when the folder is gone.
REMEMBERED = """SELECT s.cwd FROM source_logs l JOIN sessions s USING (session_id)
    WHERE l.status = 'loaded' AND l.cwd = ? AND s.cwd != l.cwd LIMIT 1"""

# The owner of an entry is the session the entry itself names (native), else the lowest
# session ID: a rule that gives the same answer whatever order the logs load in.
# BEATEN is true for a row `a` of source_entries when another session owns its entry.
BEATEN = """EXISTS (
    SELECT 1 FROM source_entries b WHERE b.entry_key = a.entry_key AND b.session_id != a.session_id
    AND (b.native > a.native OR (b.native = a.native AND b.session_id < a.session_id)))"""
FOREIGN = f"SELECT a.entry_key FROM source_entries a WHERE a.session_id = ? AND {BEATEN}"
# Loaded sessions whose count of such entries is no longer the count they were loaded with.
# shortcut: compares counts, not sets; exact while logs only grow. Compare sets if agents start rewriting logs.
STALE = f"""SELECT path, agent FROM source_logs l WHERE status = 'loaded' AND dropped !=
    (SELECT COUNT(*) FROM source_entries a WHERE a.session_id = l.session_id AND {BEATEN})"""


def sniff(entries: list[dict]) -> str | None:
    """Which agent wrote these entries, or None."""
    for e in entries[:50]:
        if e.get("type") == "session_meta":
            return "Codex"
        if e.get("type") == "session" and "cwd" in e:
            return "Pi"
        if "sessionId" in e or "parentUuid" in e:
            return "Claude Code"
    return None


def entry_key(agent: str, e: dict) -> str | None:
    """An ID that a copy of this entry in another log shares; None for entries no parser reads."""
    if agent == "Claude Code":
        return e.get("uuid") if e.get("type") in ("user", "assistant", "attachment", "system") else None
    if agent == "Pi":  # Pi IDs are 8 characters, too short to be unique across sessions
        return f"{e['id']}:{e.get('timestamp')}" if e.get("id") and e.get("type") != "session" else None
    if e.get("type") == "response_item":  # Codex entries carry no ID
        return hashlib.sha1(json.dumps([e.get("timestamp"), e.get("payload")], sort_keys=True).encode()).hexdigest()
    return None


def most_common(values: list) -> str | None:
    return Counter(values).most_common(1)[0][0] if values else None


def facts(agent: str, entries: list[dict], stem: str) -> dict:
    """What the log says about its session: id, cwd, launcher (as logged; see is_headless),
    model, title, tokens in and out, and remote (the GitHub owner/name of its checkout; only
    Codex logs record one)."""
    f = {"id": stem, "cwd": "", "launcher": "", "model": None, "title": None, "in": None, "out": None, "remote": ""}
    if agent == "Claude Code":
        f["cwd"] = next((e["cwd"] for e in entries if e.get("cwd")), "")
        f["launcher"] = ",".join(sorted({str(e["entrypoint"]) for e in entries if e.get("entrypoint")}))
        for kind, field in (("ai-title", "aiTitle"), ("custom-title", "customTitle")):  # a custom title wins
            f["title"] = next((e[field] for e in reversed(entries) if e.get("type") == kind and e.get(field)), f["title"])
        replies = [e["message"] for e in entries if e.get("type") == "assistant" and not e.get("isSidechain")
                   and isinstance(e.get("message"), dict) and e["message"].get("model") != "<synthetic>"]
        f["model"] = most_common([m["model"] for m in replies if m.get("model")])
        # One entry per content block, each repeating the reply's usage: keep the last per reply.
        usage = {m.get("id") or i: m["usage"] for i, m in enumerate(replies) if isinstance(m.get("usage"), dict)}
        if usage:
            f["in"] = sum(u.get(k) or 0 for u in usage.values()
                          for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            f["out"] = sum(u.get("output_tokens") or 0 for u in usage.values())
    elif agent == "Codex":
        payloads = [(e.get("type"), e["payload"]) for e in entries if isinstance(e.get("payload"), dict)]
        meta = next((p for t, p in payloads if t == "session_meta"), {})
        f["id"], f["cwd"] = meta.get("id") or stem, meta.get("cwd") or ""
        f["launcher"] = f"{meta.get('originator') or ''}|{meta.get('source') or ''}"
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        url = GITHUB_URL.search(str(git.get("repository_url") or ""))
        f["remote"] = url.group(1) if url else ""
        f["model"] = most_common([p["model"] for t, p in payloads if t == "turn_context" and p.get("model")])
        totals = [p["info"]["total_token_usage"] for t, p in payloads if p.get("type") == "token_count"
                  and isinstance(p.get("info"), dict) and isinstance(p["info"].get("total_token_usage"), dict)]
        if totals:  # a running total; input_tokens already counts cached input
            f["in"], f["out"] = totals[-1].get("input_tokens"), totals[-1].get("output_tokens")
    elif agent == "Pi":
        head = next((e for e in entries if e.get("type") == "session"), {})
        f["id"], f["cwd"] = head.get("id") or stem, head.get("cwd") or ""
        replies = [e["message"] for e in entries if e.get("type") == "message"
                   and isinstance(e.get("message"), dict) and e["message"].get("role") == "assistant"]
        f["model"] = most_common([m["model"] for m in replies if m.get("model")])
        usage = [m["usage"] for m in replies if isinstance(m.get("usage"), dict)]
        if usage:
            f["in"] = sum(u.get(k) or 0 for u in usage for k in ("input", "cacheRead", "cacheWrite"))
            f["out"] = sum(u.get("output") or 0 for u in usage)
    return f


@cache
def repo_roots(cwd: str) -> tuple[str, str]:
    """(checkout root, main repo root) for a directory, read from disk; (cwd, cwd) when it is
    not in a git checkout or no longer exists. The two differ for a git worktree, whose .git
    is a file pointing into the main repo. Sessions of one project share the main repo root."""
    try:
        for d in (Path(cwd), *Path(cwd).parents) if cwd else ():
            git = d / ".git"
            if git.is_dir():
                return str(d), str(d)
            if git.is_file():
                m = GITDIR.match(git.read_text())
                return str(d), str((d / m.group(1)).resolve()) if m else str(d)
    except OSError:
        pass
    return cwd, cwd


def is_headless(agent: str | None, launcher: str, human_turns: int | None, include_headless: bool) -> bool:
    """The headless rule. It reads only what source_logs stores (the launcher as the log gives
    it, and the turn count), so a change to the rule or to --include-headless can be applied
    to a log that has not changed without reading it. human_turns None means not parsed yet."""
    always = wrapped = False
    if agent == "Claude Code":  # launcher: the log's entrypoints, comma-separated
        entrypoints = set(filter(None, launcher.split(",")))
        always = bool(entrypoints) and entrypoints <= HEADLESS_CLAUDE
        wrapped = bool(entrypoints) and all(e.startswith("sdk-") for e in entrypoints)
    elif agent == "Codex":  # launcher: "originator|source"
        originator, _, source = launcher.partition("|")
        always = source in HEADLESS_CODEX
        wrapped = bool(originator) and not originator.lower().startswith("codex")
    if include_headless:
        return False
    return always or (wrapped and human_turns is not None and human_turns < SDK_MIN_TURNS)


def unload(conn, session_id: str) -> None:
    """Remove a session's rows. A log that is not loaded must not own entries either. Its labels
    go too: they point at message positions, which a later reload would number afresh."""
    with conn:
        for table in ("source_entries", "sessions", "messages", "tool_calls", "turn_labels", "session_labels"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))


def in_project(project: str | None, *dirs: str) -> bool:
    if not project:
        return True
    root = str(Path(project).expanduser().resolve())
    return any(d == root or d.startswith(root + "/") for d in dirs)


def load_log(conn, path: Path, agent: str | None = None, project: str | None = None,
             include_headless: bool = False) -> str:
    """Load one log file and record it in source_logs. Returns its status: "loaded", or why not."""
    stat = path.stat()
    entries = [e for e in read_jsonl(path, tolerant=True) if isinstance(e, dict)]
    agent = agent or sniff(entries)
    f = facts(agent, entries, path.stem) if agent else {"id": None, "cwd": "", "launcher": ""}
    session_id, (checkout, repo) = f["id"], repo_roots(f["cwd"])
    if repo and repo == f["cwd"] and not Path(repo, ".git").exists():
        # Disk no longer says which repo this folder belongs to (a removed worktree). An earlier
        # load of a session run there, or in a folder above it, may have: use its answer.
        known = (conn.execute(REMEMBERED, (str(d),)).fetchone() for d in (Path(repo), *Path(repo).parents))
        repo = next((r[0] for r in known if r), repo)

    def done(status: str, dropped: int = 0, human_turns: int | None = None) -> str:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO source_logs (path, session_id, agent, size, mtime, status, cwd, dropped,"
                " launcher, human_turns, parser) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(path), session_id, agent, stat.st_size, stat.st_mtime_ns, status, f["cwd"], dropped,
                 f["launcher"], human_turns, PARSER_VERSION))
        return status

    turns = [e for e in entries if e.get("type") in ("user", "assistant")]
    if agent is None:
        return done("unknown format" if entries else "no human turn")
    if agent == "Claude Code" and turns and all(e.get("isSidechain") for e in turns):
        return done("sub-agent log")
    if not safe_session_id(session_id):
        return done("unsafe session ID")
    if is_headless(agent, f["launcher"], None, include_headless):
        return done("headless run")  # known from the launcher alone, so not parsed
    if not in_project(project, f["cwd"], repo):
        return done("outside --project")
    # The same session ID in two files (a session moved between project folders): the bigger file wins.
    others = conn.execute("SELECT path, size FROM source_logs WHERE session_id = ? AND path != ? AND status = 'loaded'",
                          (session_id, str(path))).fetchall()
    if any((o["size"], str(path)) > (stat.st_size, o["path"]) for o in others if Path(o["path"]).exists()):
        return done("duplicate file")

    keys = [entry_key(agent, e) for e in entries]
    with conn:
        conn.execute("DELETE FROM source_entries WHERE session_id = ?", (session_id,))
        conn.executemany("INSERT OR REPLACE INTO source_entries VALUES (?,?,?)",
                         {(k, session_id, int(e.get("sessionId", session_id) == session_id))
                          for k, e in zip(keys, entries) if k})
    foreign = {r[0] for r in conn.execute(FOREIGN, (session_id,))}
    kept = [e for k, e in zip(keys, entries) if k not in foreign]
    log = PARSERS[agent](kept)
    prompts = [m["text"] for m in log.messages if m["user_kind"] == "human"]
    # Judged on every read, so a one-turn wrapped session that gains a second turn loads on the next run.
    if not prompts or is_headless(agent, f["launcher"], len(prompts), include_headless):
        unload(conn, session_id)  # it may hold rows from an earlier load
        return done("headless run" if prompts else "no human turn", human_turns=len(prompts))
    prompt = prompts[0]
    f = facts(agent, kept, path.stem) | {k: f[k] for k in ("cwd", "launcher", "remote")} | {"id": session_id}
    stamps = [e["timestamp"] for e in kept if isinstance(e.get("timestamp"), str)]
    lines = prompt.strip().splitlines()
    write_log(conn, (
        session_id, agent, f["model"], f["title"] or (lines[0][:80] if lines else None), None,
        min(stamps, default=None), max(stamps, default=None), None, f["in"], f["out"], None, None, prompt, repo,
    ), log, root=checkout)
    with conn:
        conn.execute("UPDATE sessions SET remote = ? WHERE session_id = ?", (f["remote"], session_id))
        conn.execute("UPDATE source_logs SET status = 'duplicate file' WHERE session_id = ? AND path != ?",
                     (session_id, str(path)))
    return done("loaded", len(foreign), len(prompts))


def find_logs(claude_dir: Path | None, codex_dir: Path | None, import_dirs: list[Path]) -> list[tuple[Path, str | None]]:
    """(path, agent) for every log file; agent is None where load_log must detect it."""
    found = []
    for root, agent in ((claude_dir, "Claude Code"), (codex_dir, "Codex"), *((d, None) for d in import_dirs)):
        if root:
            found += [(p, agent) for p in sorted(Path(root).expanduser().glob("**/*.jsonl")) if p.is_file()]
    return found


def sync(conn, claude_dir: Path | None = CLAUDE_DIR, codex_dir: Path | None = CODEX_DIR,
         import_dirs: list[Path] = (), project: str | None = None, since: str | None = None,
         include_headless: bool = False) -> Counter:
    """Load every new or changed log. Returns a count of files per status."""
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    known = {r["path"]: r for r in conn.execute("SELECT * FROM source_logs")}
    # Codex sessions loaded before sessions.remote existed: read their logs once more to fill it.
    no_remote = {r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE agent = 'Codex' AND remote IS NULL")}
    counts: Counter = Counter()
    changed = False
    for path, agent in find_logs(claude_dir, codex_dir, list(import_dirs)):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                counts["older than --since"] += 1
                continue
            old = known.get(str(path))
            # A row from before the launcher column existed says too little, and one read by an
            # older parser may hold rows the parser would no longer produce: read those logs once more.
            if old and old["launcher"] is not None and old["parser"] == PARSER_VERSION and (
                    old["size"], old["mtime"]) == (stat.st_size, stat.st_mtime_ns) and not (
                    old["status"] == "loaded" and old["session_id"] in no_remote):
                # Unchanged, so not read; but the rule and flags apply afresh to what the row records.
                headless = is_headless(old["agent"], old["launcher"], old["human_turns"], include_headless)
                if old["status"] == "loaded" and headless:
                    unload(conn, old["session_id"])
                    with conn:
                        conn.execute("UPDATE source_logs SET status = 'headless run' WHERE path = ?", (str(path),))
                    counts["headless run"] += 1
                    changed = True  # sessions holding copies of its entries now own them
                    continue
                # Read it again only if what kept it out no longer does.
                retry = (old["status"] == "headless run" and not headless) or (
                    old["status"] == "outside --project" and in_project(project, old["cwd"], repo_roots(old["cwd"])[1]))
                if not retry:
                    counts["unchanged" if old["status"] == "loaded" else old["status"]] += 1
                    continue
            status = load_log(conn, path, agent, project, include_headless)
            changed = changed or status == "loaded"
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:  # one bad log shouldn't stop the rest
            status = "failed"
            print(f"failed {path}: {type(e).__name__}: {e}")
        counts[status] += 1
    # A newly loaded log can take over entries that a session loaded earlier holds a copy of.
    for _ in range(3 if changed else 0):
        stale = [r for r in conn.execute(STALE).fetchall() if Path(r["path"]).exists()]
        if not stale:
            break
        for r in stale:
            load_log(conn, Path(r["path"]), r["agent"], include_headless=include_headless)
        counts["reloaded without copied entries"] += len(stale)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude-dir", type=Path, default=CLAUDE_DIR, help="Claude Code logs (default: %(default)s)")
    parser.add_argument("--codex-dir", type=Path, default=CODEX_DIR, help="Codex logs (default: %(default)s)")
    parser.add_argument("--import", dest="import_dirs", type=Path, action="append", default=[], metavar="DIR",
                        help="Also load .jsonl logs from this folder; repeat for more")
    parser.add_argument("--project", metavar="DIR", help="Only sessions run in this directory or below it")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="Only logs modified on or after this date")
    parser.add_argument("--include-headless", action="store_true", help="Also load `claude -p`, `codex exec` and one-turn wrapper-app runs")
    args = parser.parse_args(argv)

    counts = sync(db.connect(), args.claude_dir, args.codex_dir, args.import_dirs, args.project, args.since,
                  args.include_headless)
    skipped = {"sub-agent log": "sub-agent logs skipped", "headless run": "headless runs skipped"}
    print(", ".join(f"{n} {skipped.get(status, status)}" for status, n in sorted(counts.items())) or "no logs found")


if __name__ == "__main__":
    main()

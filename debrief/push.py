"""Build what an upload to a team server would hold. There is no server yet, so only --dry-run works.

    debrief push --dry-run              write the bundle under DEBRIEF_HOME/push and say what is in it
    debrief push --dry-run --out DIR    write it to DIR
    debrief push --dry-run --prev DIR   send only what changed since the bundle in DIR

Only picked projects (`[projects] include` in config.toml) contribute anything. The rest are a
count. docs/upload-format.md describes the files.
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any

from debrief import config
from debrief.db import connect, repo_projects, session_projects
from debrief.export_site import (
    ci_section,
    findings_and_recs,
    labels_by_turn,
    projects_section,
    prs_section,
    review_section,
    session_file,
    sessions_section,
)
from debrief.paths import HOME, NAME
from debrief.sources import CLAUDE_DIR, CODEX_DIR
from debrief.synthesize import ref_project

FORMAT = 1
REDACTION = 0  # shortcut: becomes debrief.redact.VERSION with the swap described at scrub()
MACHINE_PATH = HOME / "machine_id"
DEFAULT_OUT = HOME / "push"
SAFE_NAME = re.compile(r"[A-Za-z0-9_-][\w.-]*", re.ASCII)  # a session id that can be a file name as it is
NO_SERVER = (f"{NAME}: there is no server to push to yet. "
             f"`{NAME} push --dry-run` builds what would be sent and sends nothing.")


def scrub(obj, *, home_paths: bool = False, emails: bool = False) -> tuple[Any, dict[str, int]]:
    """(scrubbed copy, {"secrets": n, "home_paths": n, "emails": n}). The one door to redaction."""
    # shortcut: stands in for debrief.redact.scrub, which has this signature; replace this function
    # with `from debrief.redact import scrub` when that module lands. Until then home paths and
    # email addresses are left as they are, and secrets are counted by the marker, so text that already said "[redacted]" hides a hit.
    from debrief.export_site import redact_tree
    out = redact_tree(obj)
    marks = [json.dumps(x, ensure_ascii=False).count("[redacted]") for x in (out, obj)]
    return out, {"secrets": max(marks[0] - marks[1], 0), "home_paths": 0, "emails": 0}


def machine_id() -> tuple[str, bool]:
    """This machine's id, a random UUID made on first use, and whether this call made it."""
    if MACHINE_PATH.exists():
        return MACHINE_PATH.read_text().strip(), False
    HOME.mkdir(parents=True, exist_ok=True)
    MACHINE_PATH.write_text(new := str(uuid.uuid4()))
    return new, True


def object_path(session_id: str) -> str:
    """Where a session's object goes. An id that is not a plain file name (logs can be imported,
    so it may hold a slash) is named by its hash."""
    name = session_id if SAFE_NAME.fullmatch(session_id) else hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return f"sessions/{name}.json"


def build(conn, picked: set[str], machine: str, have: dict[str, str] | None = None) -> tuple[dict, dict[str, bytes], dict]:
    """(manifest, {path: bytes} for the objects to send, stats for the summary).

    `have` is {path: sha256} of what the server already holds; an object with the same hash is
    listed in the manifest but not sent.
    """
    have = have or {}
    labels = labels_by_turn(conn)
    run_id = conn.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0] \
        or conn.execute("SELECT MAX(run_id) FROM findings").fetchone()[0]
    rows = projects_section(conn, picked)
    repo_of = {}
    for repo, path in sorted(repo_projects(conn).items(), reverse=True):
        repo_of[path] = repo  # a folder tied to several repos takes the first by name
    key = {r["path"]: repo_of.get(r["path"]) or "local:" + hashlib.sha256(f"{machine}:{r['path']}".encode()).hexdigest()[:12]
           for r in rows if r["picked"]}

    raw: dict[str, object] = {}
    every, _ = sessions_section(conn, labels, "", picked)
    # A session with an author came from Entire and may be a teammate's; a server would file it
    # under whoever pushed. Local logs name no author.
    mine = [s for s in every if not s["author"]]
    # Nor is a log read from anywhere but the agents' own log folders on this machine: `--import`
    # takes any folder, and a teammate's log names no author. The loaded row is the file the
    # session's rows were read from.
    local = {sid for sid, path in conn.execute("SELECT session_id, path FROM source_logs WHERE status = 'loaded'")
             if ".." not in Path(path).parts and any(Path(path).is_relative_to(root) for root in (CLAUDE_DIR, CODEX_DIR))}
    sessions = [s for s in mine if s["id"] in local]
    for s in sessions:
        raw[object_path(s["id"])] = {**s, "project": key[s["project"]], **session_file(conn, labels, s["id"])}
    ids = {s["id"] for s in sessions}

    findings, recs, refs = findings_and_recs(conn, labels, run_id, picked)
    found, ideas = len(findings), len(recs)
    findings, recs, refs = only_picked(conn, findings, recs, refs, key, ids)
    left_out = {"findings": found - len(findings), "ideas": ideas - len(recs), "authored": len(every) - len(mine), "imported": len(mine) - len(sessions)}
    if findings or recs:
        for item in (*findings.values(), *recs):
            item["project"] = item["project"] and key[item["project"]]
        for ref in refs.values():
            if "sessions" in ref:  # a CI or review event names every session on its PR; keep the bundled ones
                ref["sessions"] = [sid for sid in ref["sessions"] if sid in ids]
        raw["findings.json"] = {"run_id": run_id, "findings": findings, "recommendations": recs, "refs": refs}

    linked = {p["n"] for s in sessions for p in s["prs"]}
    prs = {k: v for k, v in prs_section(conn).items() if k in linked}
    if prs:
        ci = ci_section(conn, "")
        raw["github.json"] = {
            "prs": prs,
            "ci": {"runs_by_pr": {k: v for k, v in ci["runs_by_pr"].items() if k in linked},
                   "failures": [f for f in ci["failures"] if f["pr"] in linked]},
            "reviews": [r for r in review_section(conn) if r["pr"] in linked]}

    counts, objects, send = Counter(), {}, {}
    for path, obj in raw.items():
        clean, n = scrub(obj, home_paths=True, emails=True)
        counts.update(n)
        body = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        objects[path] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        if have.get(path) != objects[path]["sha256"]:
            send[path] = body
    count, turns = Counter(s["project"] for s in sessions), Counter()
    for s in sessions:
        turns[s["project"]] += s["human_turns"]
    projects, n = scrub([{"key": key[r["path"]], "name": r["name"], "sessions": count[r["path"]], "turns": turns[r["path"]],
                          "analysed_at": r["analysed_at"]} for r in rows if r["picked"]], home_paths=True, emails=True)
    counts.update(n)
    manifest = {"format": FORMAT, "tool": f"{NAME} {version('debrief-cli')}", "machine": machine,
                "redaction": REDACTION,
                "projects": projects, "unpicked": sum(not r["picked"] for r in rows),
                "objects": objects, "sent": sorted(send)}
    stats = {
        "sessions": len(sessions), "sessions_sent": sum(p.startswith("sessions/") for p in send),
        "turns": sum(s["human_turns"] for s in sessions),
        "messages": sum(len(raw[object_path(s["id"])]["messages"]) for s in sessions),
        "secrets": counts["secrets"], "home_paths": counts["home_paths"], "emails": counts["emails"],
        "repos": sorted({k for k in key.values() if not k.startswith("local:")} | {k.rsplit("#", 1)[0] for k in prs}),
        "removed": sorted(set(have) - set(objects)), "left_out": left_out,
    }
    return manifest, send, stats


def only_picked(conn, findings: dict, recs: list, refs: dict, key: dict[str, str], ids: set[str]) -> tuple[dict, list, dict]:
    """Cut the report's findings and ideas down to what an upload may hold. The report's own rule
    keeps a finding when some of its evidence is in a picked project, and an idea when some of
    its findings are kept; a model wrote each text from all of them, so that is too loose to send.

    Kept: a finding whose stored evidence is all in the bundle (turns of sessions `ids`, events
    of a picked project's repo), and an idea whose stored findings are all kept. Either must be
    about a project with a key, or about all of them.
    """
    project, repos = session_projects(conn), repo_projects(conn)

    def in_bundle(ref: dict) -> bool:
        return ref["session_id"] in ids if "session_id" in ref else ref_project(conn, project, repos, ref) in key

    def stored(table: str, column: str, id_column: str, row_id: int) -> list:
        return json.loads(conn.execute(f"SELECT {column} FROM {table} WHERE {id_column} = ?", (row_id,)).fetchone()[0] or "[]")

    findings = {i: f for i, f in findings.items() if f["project"] in (None, *key)
                and all(in_bundle(e) for e in stored("findings", "evidence", "finding_id", i))}
    recs = [r for r in recs if r["project"] in (None, *key)
            and all(i in findings for i in stored("recommendations", "finding_ids", "rec_id", r["id"]))]
    cited = {e for f in findings.values() for e in f["evidence"]}
    return findings, recs, {i: ref for i, ref in refs.items() if i in cited}


def write(out: Path, manifest: dict, send: dict[str, bytes]) -> int:
    """Write the bundle to `out`, replacing an earlier bundle there. Returns the bytes written."""
    if out.exists() and any(out.iterdir()) and not (out / "manifest.json").exists():
        sys.exit(f"{NAME}: {out} is not empty and is not an earlier bundle; name another folder")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)  # it holds transcripts
    (out / "sessions").mkdir(exist_ok=True)
    for old in (*out.glob("sessions/*.json"), out / "findings.json", out / "github.json"):
        old.unlink(missing_ok=True)
    files = {**send, "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=1).encode()}
    for path, body in files.items():
        (out / path).write_bytes(body)
    return sum(len(b) for b in files.values())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog=f"{NAME} push", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Build the bundle and write it to a folder; send nothing")
    p.add_argument("--out", metavar="DIR", type=Path, default=DEFAULT_OUT, help=f"Where to write it (default {DEFAULT_OUT})")
    p.add_argument("--prev", metavar="DIR", type=Path,
                   help="An earlier bundle, taken as what the server holds: only objects that changed since are written")
    args = p.parse_args(argv)
    if not args.dry_run:
        sys.exit(NO_SERVER)
    have = None
    if args.prev:
        try:
            have = {path: o["sha256"] for path, o in json.loads((args.prev / "manifest.json").read_text())["objects"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            sys.exit(f"{NAME}: {args.prev} holds no bundle manifest ({e})")
    machine, made = machine_id()
    manifest, send, stats = build(connect(), set(config.projects()), machine, have)
    size = write(args.out, manifest, send)

    others = manifest["unpicked"]
    print(f"Dry run: nothing was sent. The bundle is in {args.out} ({len(send) + 1} files, {size:,} bytes).")
    print(f"  projects    {len(manifest['projects'])} picked")
    for r in manifest["projects"]:
        print(f"                {r['name']}  ({r['key']}; {r['sessions']} sessions, {r['turns']} turns)")
    print(f"              {others} other project{'' if others == 1 else 's'} on this machine "
          f"{'is' if others == 1 else 'are'} not picked and nothing about them is sent")
    print(f"  sessions    {stats['sessions']}, of which {stats['sessions_sent']} sent"
          f"{' (the rest have not changed)' if have is not None else ''}")
    print(f"  turns       {stats['turns']} of yours, {stats['messages']} messages in all")
    print(f"  redactions  {stats['secrets']} secrets and {stats['emails']} email addresses replaced, "
          f"{stats['home_paths']} home-folder paths shortened to ~")
    print(f"  repo names  {', '.join(stats['repos']) or 'none'}")
    if stats["removed"]:
        gone = sum(path.startswith("sessions/") for path in stats["removed"])
        print(f"  This push removes {gone} session{'' if gone == 1 else 's'} the server holds "
              f"({len(stats['removed'])} object{'' if len(stats['removed']) == 1 else 's'} no longer in a picked project).")
    out = stats["left_out"]
    if out["authored"]:
        print(f"  Left out {out['authored']} sessions: they are other people's (they name an author).")
    if out["imported"]:
        print(f"  Left out {out['imported']} sessions: they were imported from another folder.")
    if out["findings"] or out["ideas"]:
        print(f"  Left out {out['findings']} findings and {out['ideas']} ideas: "
              "they draw on projects you have not picked or on sessions that are not sent.")
    if made:
        print(f"  Made an id for this machine, a random number kept in {MACHINE_PATH}.")
    print("Redaction is pattern-based: read the bundle before you trust it.")
    print("Upload only what you have the right to share: transcripts hold your employer's or clients' code "
          "and other people's names.")


if __name__ == "__main__":
    main()

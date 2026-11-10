"""Drop sessions last active before a date, and the GitHub rows only they link to.

Removes their data/raw/<id>/ folders and their rows in sessions, checkpoints, messages,
tool_calls, turn_labels and session_labels. Then drops GitHub rows no remaining session
reaches: commit links and seen-markers for commits that are no longer checkpoints, PRs no
remaining commit is in, checks on commits that are neither a kept checkpoint nor a kept PR's
head, comments on dropped PRs, and event labels of dropped checks and comments. Raw folders
with no session row go too when their listing.json is older than the date, or when empty.
findings, recommendations and evidence_checks stay as history. Ends with VACUUM.

    uv run python -m debrief.prune --before 2026-09-14 --dry-run
    uv run python -m debrief.prune --before 2026-09-14
"""

import argparse
import json
import shutil
from pathlib import Path

from debrief import db
from debrief.paths import RAW_DIR

SESSION_TABLES = ["sessions", "checkpoints", "messages", "tool_calls", "turn_labels", "session_labels"]
GH_TABLES = ["gh_commit_prs", "gh_commits_seen", "gh_prs", "gh_checks", "gh_review_comments", "event_labels"]

# Each runs after the session rows are gone, in this order, since later ones read earlier results.
KEPT_COMMITS = "SELECT commit_sha FROM checkpoints WHERE commit_sha IS NOT NULL"
GH_DELETES = [
    f"DELETE FROM gh_commit_prs WHERE commit_sha NOT IN ({KEPT_COMMITS})",
    f"DELETE FROM gh_commits_seen WHERE commit_sha NOT IN ({KEPT_COMMITS})",
    "DELETE FROM gh_prs WHERE pr_number NOT IN (SELECT pr_number FROM gh_commit_prs)",
    # github.py also lists checks on PR heads, which need not be checkpoint commits.
    (f"DELETE FROM gh_checks WHERE commit_sha NOT IN ({KEPT_COMMITS}) "
     "AND commit_sha NOT IN (SELECT head_sha FROM gh_prs WHERE head_sha IS NOT NULL)"),
    "DELETE FROM gh_review_comments WHERE pr_number NOT IN (SELECT pr_number FROM gh_prs)",
    ("DELETE FROM event_labels WHERE event_type = 'review_comment' "
     "AND event_id NOT IN (SELECT comment_id FROM gh_review_comments)"),
    "DELETE FROM event_labels WHERE event_type = 'check_failure' AND event_id NOT IN (SELECT check_id FROM gh_checks)",
]


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SESSION_TABLES + GH_TABLES}


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def stale_folders(before: str, dropped: set[str], known: set[str]) -> list[Path]:
    """Raw folders to remove: dropped sessions, plus session-less folders that are old or empty."""
    out = []
    for folder in sorted(p for p in RAW_DIR.iterdir() if p.is_dir()):
        listing = folder / "listing.json"
        orphan = folder.name not in known and (
            not any(folder.iterdir())
            or (listing.exists() and json.loads(listing.read_text()).get("lastActivityAt", "") < before))
        if folder.name in dropped or orphan:
            out.append(folder)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True, metavar="YYYY-MM-DD", help="Drop sessions last active before this")
    parser.add_argument("--dry-run", action="store_true", help="Report what would go; change nothing")
    args = parser.parse_args()
    assert len(args.before) == 10 and args.before[4] == args.before[7] == "-", "--before must be YYYY-MM-DD"

    conn = db.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS gh_commits_seen (commit_sha TEXT PRIMARY KEY, looked_up_at TEXT)")
    known = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
    # ISO timestamps compare as strings; a session with no date is kept.
    dropped = {r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE last_activity_at < ?", (args.before,))}
    folders = stale_folders(args.before, dropped, known)
    before_counts, raw_before, db_before = counts(conn), dir_bytes(RAW_DIR), db.DB_PATH.stat().st_size
    print(f"{len(dropped)} of {len(known)} sessions last active before {args.before}; "
          f"{len(folders)} raw folders to remove ({sum(f.name not in known for f in folders)} with no session row)")

    conn.execute("BEGIN")
    marks = ",".join("?" * len(dropped))
    for t in SESSION_TABLES:
        conn.execute(f"DELETE FROM {t} WHERE session_id IN ({marks})", list(dropped))
    for sql in GH_DELETES:
        conn.execute(sql)
    after_counts = counts(conn)
    for t in before_counts:
        print(f"  {t:<20} {before_counts[t]:>9} -> {after_counts[t]:>9}")
    # Self-check: no kept session lost rows, and every kept link still resolves.
    assert after_counts["sessions"] == len(known) - len(dropped)
    assert not conn.execute("SELECT 1 FROM sessions WHERE last_activity_at < ?", (args.before,)).fetchone()
    lost = conn.execute(f"SELECT COUNT(DISTINCT commit_sha) FROM ({KEPT_COMMITS}) WHERE commit_sha NOT IN "
                        "(SELECT commit_sha FROM gh_commits_seen UNION SELECT commit_sha FROM gh_commit_prs)").fetchone()[0]
    assert not conn.execute("SELECT 1 FROM gh_commit_prs WHERE pr_number NOT IN (SELECT pr_number FROM gh_prs)"
                            ).fetchone(), "a kept commit links to a PR that is gone"
    print(f"  {lost} kept checkpoint commits were never looked up (github.py fetches those as before)")

    if args.dry_run:
        conn.rollback()
        print(f"dry run: nothing changed. data/raw {raw_before / 1e6:.0f} MB, debrief.db {db_before / 1e6:.0f} MB")
        return
    conn.commit()
    for folder in folders:
        shutil.rmtree(folder)
    conn.execute("VACUUM")
    conn.close()
    print(f"data/raw {raw_before / 1e6:.0f} -> {dir_bytes(RAW_DIR) / 1e6:.0f} MB, "
          f"debrief.db {db_before / 1e6:.0f} -> {db.DB_PATH.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()

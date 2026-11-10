"""Measure Claude Code labels against the stored Jev labels and a set of reference labels.

`label` runs one Claude model over a fixed sample and writes its answers to a JSON file; it
never writes to the database, so the Jev labels there stay as they are. `report` reads those
files and prints agreement per field. Reference labels are jev-align test sets: one
<field>.jsonl per question with rows {story_id, inputs: {state}, label, evaluation_split}.

    uv run python -m debrief.compare_llm label --model claude-haiku-5-5 --testset DIR --out haiku.json
    uv run python -m debrief.compare_llm report --testset DIR haiku.json sonnet.json \\
        --disagreements out.txt --pick claude-haiku-5-5

The sample: every reference row, plus whole sessions from the database (in a fixed order,
by hash of the session ID) up to --db-turns turn items and --sessions session items.
"""

import argparse
import hashlib
import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from debrief import classify
from debrief.db import connect

FIRST_MARKER = "(none: first message of the session)"
HUMAN_MARKER = "## Human message"
CUT = 0.7  # synthesize's default --min-confidence


def read_testset(directory: Path) -> dict[str, list[dict]]:
    return {f.stem: [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
            for f in sorted(directory.glob("*.jsonl"))}


def questions_for(field: str, state: str) -> dict:
    """The question set classify.py sends with this state."""
    if field in classify.REVIEW_QUESTIONS:
        return classify.REVIEW_QUESTIONS
    if HUMAN_MARKER not in state:
        return classify.TAIL_QUESTIONS
    return classify.FIRST_TURN_QUESTIONS if FIRST_MARKER in state else classify.TURN_QUESTIONS


def sample(conn, testset: dict[str, list[dict]], db_turns: int, n_sessions: int) -> dict[str, dict[str, dict]]:
    """{group: {item id: item}} for the groups turns, sessions and events."""
    turns, events = {}, {}
    for field, rows in testset.items():
        group = events if field in classify.REVIEW_QUESTIONS else turns
        for r in rows:
            state = r["inputs"]["state"]
            group.setdefault(r["story_id"], classify.item("", (), state, questions_for(field, state), r["story_id"]))
    order = sorted((r["session_id"] for r in conn.execute("SELECT session_id FROM sessions")),
                   key=lambda sid: hashlib.sha256(sid.encode()).hexdigest())
    sessions, added = {}, 0
    for sid in order:
        t, tails, s = classify.session_items(conn, sid)
        if s and len(sessions) < n_sessions:
            sessions[sid] = s[0]
        new = {":".join(map(str, it["key"])): it for it in t + tails}
        if added + len(new) <= db_turns:
            added += len(new)
            turns |= {k: v for k, v in new.items() if k not in turns}
        if len(sessions) >= n_sessions and added >= db_turns - 5:
            break
    return {"turns": turns, "sessions": sessions, "events": events}


def label(model: str, groups: dict[str, dict[str, dict]], workers: int) -> dict:
    out = {"model": model, "groups": {}, "items": {}}
    for group, items in groups.items():
        ids = list(items)
        batches = classify.batches([{**items[i], "id": i} for i in ids])
        started = time.monotonic()
        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(lambda b: classify.label_batch(b, model), batches))
        done = 0
        for batch, (answers, _) in zip(batches, results, strict=True):
            for i, a in answers.items():
                rows = classify.to_rows(a, batch[i]["questions"])
                out["items"][f"{group}:{batch[i]['id']}"] = {f: [v, c] for f, v, c, _ in rows}
                done += 1
        out["groups"][group] = {"items": len(ids), "labeled": done, "calls": sum(n for _, n in results),
                                "first_calls": len(batches), "seconds": round(time.monotonic() - started, 1)}
        print(group, out["groups"][group])
    return out


def jev_labels(conn) -> dict[str, dict]:
    """Stored labels in the same {group:id: {field: [value, confidence]}} shape."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT session_id, msg_idx, field, value, confidence FROM turn_labels"):
        out.setdefault(f"turns:{r['session_id']}:{r['msg_idx']}", {})[r["field"]] = [r["value"], r["confidence"]]
    for r in conn.execute("SELECT session_id, field, value, confidence FROM session_labels"):
        out.setdefault(f"sessions:{r['session_id']}", {})[r["field"]] = [r["value"], r["confidence"]]
    for r in conn.execute("SELECT event_type, event_id, field, value, confidence FROM event_labels"):
        out.setdefault(f"events:{r['event_type']}:{r['event_id']}", {})[r["field"]] = [r["value"], r["confidence"]]
    return out


def agreement(pairs: list[tuple[str, str]]) -> dict:
    """pairs are (reference, prediction). Precision and recall are for the value "true"."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    agree = sum(a == b for a, b in pairs) / n
    values = {v for pair in pairs for v in pair}
    chance = sum((sum(a == v for a, _ in pairs) / n) * (sum(b == v for _, b in pairs) / n) for v in values)
    out = {"n": n, "agree": agree, "kappa": (agree - chance) / (1 - chance) if chance < 1 else 1.0}
    if values <= {"true", "false"}:
        tp = sum(a == b == "true" for a, b in pairs)
        ref, pred = sum(a == "true" for a, _ in pairs), sum(b == "true" for _, b in pairs)
        out |= {"ref_true": ref, "pred_true": pred,
                "precision": tp / pred if pred else None, "recall": tp / ref if ref else None}
    return out


def fmt(m: dict) -> str:
    if not m["n"]:
        return "n=0"
    line = f"n={m['n']:<4} agree {m['agree']:.2f}  kappa {m['kappa']:5.2f}"
    if "ref_true" in m:
        p, r = (f"{x:.2f}" if x is not None else " n/a" for x in (m["precision"], m["recall"]))
        line += f"  true: ref {m['ref_true']:<3} pred {m['pred_true']:<3} precision {p} recall {r}"
    return line


def pairs_for(field: str, ref: dict, pred: dict, ids=None) -> list[tuple[str, str]]:
    return [(ref[i][field][0], pred[i][field][0]) for i in (ids if ids is not None else ref)
            if i in ref and i in pred and field in ref[i] and field in pred[i]]


def report(conn, testset: dict[str, list[dict]], runs: list[dict]) -> None:
    labelers = {"jev": jev_labels(conn)} | {r["model"]: r["items"] for r in runs}
    models = [r["model"] for r in runs]

    print("== Agreement with the reference labels (reference is the first of each pair) ==")
    for field, rows in testset.items():
        group = "events" if field in classify.REVIEW_QUESTIONS else "turns"
        for split in ["all", *sorted({r["evaluation_split"] for r in rows})]:
            ref = {f"{group}:{r['story_id']}": {field: [str(r["label"]).lower() if isinstance(r["label"], bool)
                                                        else r["label"], None]}
                   for r in rows if split in ("all", r["evaluation_split"])}
            with_jev = [i for i in ref if field in labelers["jev"].get(i, {})]
            print(f"\n{field} [{split}] reference rows {len(ref)}, of which Jev has a stored label for {len(with_jev)}")
            print(f"  {'jev':<18} {fmt(agreement(pairs_for(field, ref, labelers['jev'])))}")
            for m in models:
                print(f"  {m:<18} {fmt(agreement(pairs_for(field, ref, labelers[m])))}")
                print(f"  {m + ' *':<18} {fmt(agreement(pairs_for(field, ref, labelers[m], with_jev)))}")
    print("\n  * = on the rows Jev also labeled")

    fields = [f for qs in (classify.TURN_QUESTIONS, classify.SESSION_QUESTIONS, classify.REVIEW_QUESTIONS)
              for f in qs]
    print("\n== Claude against Jev (Jev is the reference), every sampled item with a stored Jev label ==")
    for field in fields:
        print(f"\n{field}")
        for m in models:
            print(f"  {m:<18} {fmt(agreement(pairs_for(field, labelers['jev'], labelers[m], labelers[m])))}")
        for a, b in itertools.pairwise(models):
            print(f"  {a} vs {b}: {fmt(agreement(pairs_for(field, labelers[a], labelers[b])))}")

    print(f"\n== Share of labels under confidence {CUT} (same items for Jev and the model) ==")
    for m in models:
        print(f"\n{m}")
        total = [0, 0, 0]
        for field in fields:
            both = [i for i in labelers[m] if field in labelers[m][i] and field in labelers["jev"].get(i, {})]
            if not both:
                continue
            low_m = sum(labelers[m][i][field][1] < CUT for i in both)
            low_j = sum((labelers["jev"][i][field][1] or 0) < CUT for i in both)
            total = [total[0] + len(both), total[1] + low_m, total[2] + low_j]
            print(f"  {field:<26} n={len(both):<4} model {low_m / len(both):.2f}  jev {low_j / len(both):.2f}")
        if total[0]:
            print(f"  {'all fields':<26} n={total[0]:<4} model {total[1] / total[0]:.2f}  jev {total[2] / total[0]:.2f}")
        print("  agreement with Jev by the model's own confidence:")
        for lo, hi in ((0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)):
            hits = [labelers[m][i][f][0] == labelers["jev"][i][f][0] for i in labelers[m] for f in fields
                    if f in labelers[m][i] and f in labelers["jev"].get(i, {}) and lo <= labelers[m][i][f][1] < hi]
            if hits:
                print(f"    {lo:.1f}-{min(hi, 1):.1f}  n={len(hits):<5} agree {sum(hits) / len(hits):.2f}")

    print("\n== Time ==")
    for r in runs:
        for group, g in r["groups"].items():
            rate = g["labeled"] / g["seconds"] * 60 if g["seconds"] else 0
            print(f"  {r['model']:<18} {group:<9} {g['labeled']}/{g['items']} items, {g['calls']} calls "
                  f"({g['first_calls']} batches), {g['seconds']:.0f}s, {rate:.0f} items/min")


def write_disagreements(conn, groups: dict, run: dict, path: Path, per_field: int) -> None:
    """Up to per_field items per field where the model and Jev differ, in a fixed arbitrary order."""
    jev, mine = jev_labels(conn), run["items"]
    states = {f"{g}:{i}": it["state"] for g, items in groups.items() for i, it in items.items()}
    out = [f"Disagreements between {run['model']} and Jev. Private: holds transcript text.\n"]
    fields = [f for qs in (classify.TURN_QUESTIONS, classify.SESSION_QUESTIONS, classify.REVIEW_QUESTIONS) for f in qs]
    for field in fields:
        differ = sorted((i for i in mine if field in mine[i] and field in jev.get(i, {})
                         and mine[i][field][0] != jev[i][field][0]),
                        key=lambda i: hashlib.sha256(i.encode()).hexdigest())
        out.append(f"\n{'#' * 100}\n# {field}: {len(differ)} disagreements, showing {min(len(differ), per_field)}\n")
        for i in differ[:per_field]:
            group, _, key = i.partition(":")
            head, _, human = states[i].rpartition(HUMAN_MARKER)
            body = (f"…{head[-2500:]}\n{HUMAN_MARKER}{classify.clip(human, 2500)}" if head
                    else classify.clip(states[i], 5000))
            out.append(f"\n{'-' * 100}\n{field} | {group} {key}\n"
                       f"  {run['model']}: {mine[i][field][0]} ({mine[i][field][1]:.2f})\n"
                       f"  jev: {jev[i][field][0]} ({jev[i][field][1]:.2f})\n\n{body}\n")
    path.write_text("\n".join(out))
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("label", "report"):
        p = sub.add_parser(name)
        p.add_argument("--testset", type=Path, required=True, help="Directory of <field>.jsonl reference labels")
        p.add_argument("--db-turns", type=int, default=160, help="Turn items to add from whole sessions")
        p.add_argument("--sessions", type=int, default=40, help="Session items to label")
    sub.choices["label"].add_argument("--model", required=True)
    sub.choices["label"].add_argument("--out", type=Path, required=True)
    sub.choices["label"].add_argument("--parallel", type=int, default=classify.CLAUDE_WORKERS)
    sub.choices["label"].add_argument("--dry-run", action="store_true", help="Print the sample size and stop")
    sub.choices["report"].add_argument("runs", type=Path, nargs="+", help="Files written by label")
    sub.choices["report"].add_argument("--disagreements", type=Path, help="Write disagreements with Jev here")
    sub.choices["report"].add_argument("--pick", help="Model whose disagreements to write (default: first run)")
    sub.choices["report"].add_argument("--per-field", type=int, default=15)
    args = parser.parse_args(argv)

    conn = connect()
    testset = read_testset(args.testset)
    groups = sample(conn, testset, args.db_turns, args.sessions)
    if args.command == "label":
        for group, items in groups.items():
            print(f"{group}: {len(items)} items, {len(classify.batches(list(items.values())))} calls")
        if not args.dry_run:
            args.out.write_text(json.dumps(label(args.model, groups, args.parallel)))
        return
    runs = [json.loads(p.read_text()) for p in args.runs]
    report(conn, testset, runs)
    if args.disagreements:
        run = next((r for r in runs if r["model"] == args.pick), runs[0])
        write_disagreements(conn, groups, run, args.disagreements, args.per_field)


if __name__ == "__main__":
    main()

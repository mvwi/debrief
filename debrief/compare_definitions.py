"""Score candidate definitions of one Jev question against labeled examples.

Binary (noul) questions report F1, precision and recall; multiclass (choice) questions such
as held_up report accuracy, macro-F1 and per-class F1.

Labels come from a jev-align run (labels.jsonl). Each candidate replaces the question in
the same multi-question call classify.py makes, and is scored `--repeats` times because
Jev's answers vary a little between calls.

    uv run python -m debrief.compare_definitions found_not_filed .jev-align/runs/<run> candidates.py
    uv run python -m debrief.compare_definitions found_not_filed data/jevalign/testset/found_not_filed.jsonl \
        data/jevalign/candidates/baseline.py --baseline
    uv run python -m debrief.compare_definitions held_up data/jevalign/testset/held_up.jsonl \
        data/jevalign/candidates/held_up.py --baseline

candidates.py defines CANDIDATES = {"name": noul(...) or choice(...), ...}; "current" is added for you.
"""

import argparse
import json
import os
import runpy
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

from debrief import classify

TAIL_MARKER = "## Human message"  # turn states end with the human message; tail states don't


def load_labels(path: Path) -> list[dict]:
    """A jev-align run directory (its labels.jsonl) or a labels file in the same format."""
    file = path / "labels.jsonl" if path.is_dir() else path
    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]


def questions_for(field: str, definition: dict, state: str) -> dict:
    """The question set classify.py would send for this state, with one question swapped."""
    if field in classify.REVIEW_QUESTIONS:
        return {**classify.REVIEW_QUESTIONS, field: definition}
    if field == "found_not_filed" and TAIL_MARKER not in state:
        return {field: definition}
    return {**classify.TURN_QUESTIONS, field: definition}


def answer(client: httpx.Client, model: str, field: str, definition: dict, state: str) -> float | str:
    """P(true) for a noul question, the chosen key for a choice question."""
    answers = classify.decide(client, model, {"state": state, "questions": questions_for(field, definition, state)})
    a = answers["answers"][field]
    return a["noul"] if a["type"] == "noul" else a["choice"]


def metrics(labels: list[bool], probs: list[float]) -> dict:
    tp = sum(y and p > 0.5 for y, p in zip(labels, probs, strict=True))
    fp = sum(not y and p > 0.5 for y, p in zip(labels, probs, strict=True))
    fn = sum(y and p <= 0.5 for y, p in zip(labels, probs, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def choice_metrics(labels: list[str], preds: list[str], classes: list[str]) -> dict:
    """Accuracy, per-class F1 and macro-F1 over the classes that occur in labels or predictions."""
    pairs = list(zip(labels, preds, strict=True))
    f1 = {}
    for c in classes:
        tp = sum(y == c and p == c for y, p in pairs)
        fp = sum(y != c and p == c for y, p in pairs)
        fn = sum(y == c and p != c for y, p in pairs)
        if tp + fp + fn:
            f1[c] = 2 * tp / (2 * tp + fp + fn)
    return {"accuracy": sum(y == p for y, p in pairs) / len(pairs), "macro_f1": statistics.mean(f1.values()),
            "f1": f1, "wrong": [(y, p) for y, p in pairs if y != p]}


def mean_sd(values: list[float]) -> str:
    return f"{statistics.mean(values):.2f}±{statistics.pstdev(values):.2f}"


def report_noul(rows: list[dict], idx: list[int], candidates: dict, by: dict, repeats: int) -> None:
    labels = [rows[i]["label"] is True for i in idx]
    print(f"{len(idx)} items, {sum(labels)} true")
    print(f"{'candidate':<14} {'F1':>11} {'precision':>11} {'recall':>11}   TP/FP/FN per repeat")
    for name in candidates:
        runs = [metrics(labels, [by[name][rep][i] for i in idx]) for rep in range(repeats)]
        cells = {k: mean_sd([r[k] for r in runs]) for k in ("f1", "precision", "recall")}
        counts = " ".join(f"{r['tp']}/{r['fp']}/{r['fn']}" for r in runs)
        print(f"{name:<14} {cells['f1']:>11} {cells['precision']:>11} {cells['recall']:>11}   {counts}")


def report_choice(rows: list[dict], idx: list[int], candidates: dict, by: dict, repeats: int) -> None:
    labels = [rows[i]["label"] for i in idx]
    classes = list(dict.fromkeys(k for d in candidates.values() for k in d["criteria"]))
    print(f"{len(idx)} items: " + ", ".join(f"{c} {labels.count(c)}" for c in classes))
    print(f"{'candidate':<14} {'accuracy':>11} {'macro-F1':>11}   " + " ".join(f"{c[:11]:>11}" for c in classes))
    for name in candidates:
        runs = [choice_metrics(labels, [by[name][rep][i] for i in idx], classes) for rep in range(repeats)]
        per_class = " ".join(f"{statistics.mean(r['f1'].get(c, 0.0) for r in runs):>11.2f}" for c in classes)
        print(f"{name:<14} {mean_sd([r['accuracy'] for r in runs]):>11} "
              f"{mean_sd([r['macro_f1'] for r in runs]):>11}   {per_class}")
        confusions = Counter(pair for r in runs for pair in r["wrong"])
        print(f"{'':<14} misses (label->pred, over all repeats): "
              + ", ".join(f"{y}->{p} {n}" for (y, p), n in confusions.most_common()))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("field")
    parser.add_argument("labels", type=Path, help="jev-align run directory or labels .jsonl")
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline", action="store_true", help="also score the baseline definition")
    args = parser.parse_args(argv)

    current = {**classify.TURN_QUESTIONS, **classify.REVIEW_QUESTIONS,
               "found_not_filed": classify.FOUND_NOT_FILED}[args.field]
    candidates = {"current": current, **runpy.run_path(str(args.candidates)).get("CANDIDATES", {})}
    if args.baseline:
        baseline = runpy.run_path(str(Path(args.candidates).parent / "baseline.py"))["BASELINE"]
        candidates = {"baseline": baseline[args.field], **candidates}
    rows = load_labels(args.labels)
    load_dotenv()
    model = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
    key = os.environ["OPENROUTER_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}

    jobs = [(name, rep, i) for name in candidates for rep in range(args.repeats) for i in range(len(rows))]
    with httpx.Client(headers=headers, timeout=120) as client, ThreadPoolExecutor(classify.WORKERS) as pool:
        results = list(pool.map(
            lambda j: answer(client, model, args.field, candidates[j[0]], rows[j[2]]["inputs"]["state"]), jobs))
    by = {}
    for (name, rep, i), result in zip(jobs, results, strict=True):
        by.setdefault(name, {}).setdefault(rep, {})[i] = result

    report = report_choice if current["type"] == "choice" else report_noul
    for split in sorted({r["evaluation_split"] for r in rows}, key=["train", "holdout", "test"].index):
        print(f"\n{split}: ", end="")
        report(rows, [i for i, r in enumerate(rows) if r["evaluation_split"] == split], candidates, by, args.repeats)


if __name__ == "__main__":
    main()

"""Checks for the Claude Code path of classify: batching, reply parsing, stored rows. No model calls."""

import json

import pytest

from debrief import classify
from debrief.synthesize import ClaudeCodeError

Q = {
    "flag": classify.noul("Is it?", "yes", "no"),
    "kind": classify.choice("Which?", a="A", b="B"),
    "detail": classify.score("How much?", "low", "mid", "high"),
}
GOOD = {"flag": 0.9, "kind": ["b", 0.8], "detail": ["high", 0.6]}


def item(n: int, questions: dict = Q, size: int = 10) -> dict:
    return classify.item("turn_labels", ("s", n), f"state {n} " + "x" * size, questions, f"item {n}")


def test_batches_split_by_question_set_count_and_size(monkeypatch):
    monkeypatch.setattr(classify, "BATCH_ITEMS", 3)
    monkeypatch.setattr(classify, "BATCH_CHARS", 100)
    other = {"flag": Q["flag"]}
    items = [item(0, other), *[item(n) for n in range(1, 5)], item(5, size=80), item(6, size=80)]
    got = [[it["key"][1] for it in b] for b in classify.batches(items)]
    assert sorted(got) == [[0], [1, 2, 3], [4], [5], [6]]  # 18 + 88 chars is over the cap
    assert all(len({json.dumps(it["questions"]) for it in b}) == 1 for b in classify.batches(items))


def test_prompt_numbers_items_and_lists_options():
    prompt = classify.batch_prompt([item(1), item(2)])
    assert "===== ITEM 2 =====\nstate 2" in prompt and "- b: B" in prompt and "- mid" in prompt


def test_parse_reply_tolerates_fences_and_truncation():
    full = "```json\n" + json.dumps({"1": GOOD, "2": GOOD}) + "\n```"
    assert classify.parse_reply(full) == {1: GOOD, 2: GOOD}
    cut = json.dumps({"1": GOOD, "2": GOOD})[:-20]
    assert classify.parse_reply(cut) == {1: GOOD}
    assert classify.parse_reply("sorry, no") == {}


def test_rows_match_the_stored_shape():
    rows = {f: (v, c, p) for f, v, c, p in classify.to_rows(classify.to_answers(GOOD, Q), Q)}
    assert rows["flag"] == ("true", 0.9, '{"true": 0.9}')
    assert rows["kind"] == ("b", 0.8, "{}")
    assert rows["detail"] == ("high", 0.6, None)
    assert rows["detail_score"] == ("2", 0.6, None)
    low = classify.to_rows(classify.to_answers({**GOOD, "flag": 0.2}, Q), Q)[0]
    assert low[1:3] == ("false", 0.8)


@pytest.mark.parametrize("bad", [
    {"kind": ["b", 0.8], "detail": ["high", 0.6]},  # a question left out
    {**GOOD, "flag": 1.5},
    {**GOOD, "flag": True},
    {**GOOD, "kind": ["zzz", 0.8]},
    {**GOOD, "detail": "high"},
    "not an object",
])
def test_bad_answers_are_rejected(bad):
    with pytest.raises((KeyError, TypeError, ValueError)):
        classify.to_answers(bad, Q)


def test_label_batch_retries_only_the_bad_items():
    prompts = []

    def ask(user, model, system, parse):
        prompts.append(user)
        n = user.count("===== ITEM ")
        if len(prompts) == 1:  # first reply: item 2 missing, item 4 invalid
            return parse(json.dumps({"1": GOOD, "3": GOOD, "4": {"flag": "maybe"}})), {}
        if "state 4 " in user:  # item 4 never comes back
            return parse("{}"), {}
        return parse(json.dumps({str(i): GOOD for i in range(1, n + 1)})), {}

    answers, calls = classify.label_batch([item(n) for n in range(1, 5)], "m", ask)
    assert sorted(answers) == [0, 1, 2] and calls == 3
    assert prompts[1].count("===== ITEM ") == 1 and "state 2 " in prompts[1]


def test_label_batch_survives_a_failed_call():
    def ask(*_):
        raise ClaudeCodeError("boom")

    assert classify.label_batch([item(1), item(2)], "m", ask) == ({}, 1)


def test_run_claude_saves_rows_and_skips_failures(tmp_path, monkeypatch):
    conn = classify.connect(tmp_path / "t.db")
    items = [item(1), item(2)]
    for it in items:
        it["hash"] = classify.input_hash("m", it)
    monkeypatch.setattr(classify, "label_batch", lambda batch, model: ({0: classify.to_answers(GOOD, Q)}, 1))
    classify.run_claude(conn, "m", items, 1)
    rows = conn.execute("SELECT msg_idx, field, value, model FROM turn_labels ORDER BY field").fetchall()
    assert [tuple(r) for r in rows] == [(1, "detail", "high", "m"), (1, "detail_score", "2", "m"),
                                        (1, "flag", "true", "m"), (1, "kind", "b", "m")]
    assert classify.is_cached(conn, items[0]) and not classify.is_cached(conn, items[1])

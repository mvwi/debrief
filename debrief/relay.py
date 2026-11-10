"""Spot human messages that mostly relay a bot's output: review findings, trail comments, CI logs.

A person who pastes an Entire Trail comment or a Codex review item isn't correcting the agent
themselves; the agent could have fetched the finding. is_relayed(text) is True when a paste
starts the message, or follows a short lead-in, or carries a signature only a bot writes.
A person's own words that mention a finding ("there is a finding on the trail") don't count.

    >>> is_relayed("Entire Trail review comment\\nID: 019ff135\\nSeverity: medium")
    True
    >>> is_relayed("I've got this issue:\\n  - [P2] Pick a placement — cmd/x.go:141\\n    mirrorReadCluster ...")
    True
    >>> is_relayed("tests are failing and you dismissed a trail finding")
    False
"""

import re

# Lines a bot's finding starts with, after any bullet or list number.
BULLET = r"^[ \t]*(?:\d+\.|[-•*▎])?[ \t]*"
START = re.compile("|".join([
    BULLET + r"Entire Trail review comment\b",
    BULLET + r"#{1,4}[ \t]*(?:critical|high|medium|low)[ \t]+(?:severity[ \t]+)?finding\b",
    BULLET + r"\[P[0-3]\]",                                  # Codex review: [P2] Title — file.go:12
    BULLET + r"P[0-3][ \t]*(?:—|–|:|[ \t]-[ \t]|[ \t]+\S+\.\w+:\d)",  # P2 — Title, P1 file.go:480
    r"^[ \t]*Review comment:[ \t]*$",                        # Codex review header
    r"^[ \t]*\**Location:?\**:?[ \t]*`?[\w./-]+\.\w+:\d+",   # **Location:** `file.go:163`
]), re.IGNORECASE | re.MULTILINE)

# Text only a bot writes; relayed wherever it appears.
SIGNATURE = re.compile(r"^Suggested changeset \d|^This issue also appears (?:on|in) line", re.MULTILINE)

# A CI log with a one-line lead-in: "The tests is failing in CI:\nok  github.com/..."
CI_LEAD = re.compile(r"\A[^\n]{0,80}\bCI\b[^\n]{0,40}:[ \t]*\n", re.IGNORECASE)
CI_LOG = re.compile(r"^(?:ok|FAIL|---|===)[ \t]", re.MULTILINE)

PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")
LEAD_MAX = 300      # chars of the person's own words before the paste
LEAD_SHARE = 0.35   # ... and at most this share of the message


def is_relayed(text: str | None) -> bool:
    """True when the message is mostly pasted bot, review or CI output."""
    text = PASTE_TAG.sub("", text or "").strip()
    if not text:
        return False
    if SIGNATURE.search(text):
        return True
    if CI_LEAD.match(text) and len(CI_LOG.findall(text)) >= 3:
        return True
    m = START.search(text)
    return bool(m) and m.start() <= LEAD_MAX and m.start() <= LEAD_SHARE * len(text)


if __name__ == "__main__":
    relayed = [
        ("Entire Trail review comment\nID: 019ff135-c403\nURL: https://entire.io/gh/entireio/cli/trails/1008\n"
        "Severity: medium\nContext: cmd/entire/cli/api/trails.go:15"),
        "### Medium finding\n\n**Location:** `cmd/entire/cli/agent/hook_command.go:163`\n\nThe Windows wrappers ...",
        (" - [P2] Empty protection output misclassifies absent providers — cmd/entire/cli/repo_protection.go:135\n"
        "   repo.Provider.Or(\"\") != \"github\" is treated as ..."),
        " 1. P2 — Suggested fix is not runnable\n    cmd/entire/cli/doctor.go:1390 prints: ...",
        "\n - P1 cmd/entire/cli/checkpoint/refs_store.go:480 — remote lookup still fetches only the canonical ref.",
        "I've got this issue:\n  - [P2] Pick a placement compatible with the active credentials — repo_grant.go:141",
        ("let first fix this:\n• The new placement default can be derived from a different identity.\n\n"
        "  Review comment:\n\n  - [P2] Derive the default jurisdiction from ENTIRE_TOKEN — repo_mirror.go:88\n"
        "    The default is picked before the token is read, so a caller who set ENTIRE_TOKEN for another\n"
        "    jurisdiction gets a mirror in the wrong region, and the control-plane request then fails."),
        ("These git subprocesses inherit the environment ... Consider scrubbing repo-override env vars.\n\n"
        "This issue also appears on line 34 of the same file.\n\nSuggested changeset 1 (1)\ncmd/x_test.go"),
        ("The tests is failing in CI:\nok      github.com/entireio/cli/cmd/entire/cli/settings    1.890s\n"
        "--- FAIL: TestX (0.01s)\nFAIL    github.com/entireio/cli/cmd/entire/cli/strategy    19.997s\n"),
    ]
    own_words = [
        "tests are failing and you dismissed a trail finding",
        "now high severity finding on trail",
        "can you look at the copilot comments, check the trail for findings again",
        "there is a high finding by copilot, is it legit? or bullshit ?",
        "checkpoint.go:830 means an anchorless import is now visible in repo history - i think this is fine?",
        "The radio button selection is moving with the cursor but should not.",
        "Here it is as one self-contained handoff.\n\n" + "context " * 200 + "\n### High finding\n...",
    ]
    for s in relayed:
        assert is_relayed(s), s
    for s in own_words:
        assert not is_relayed(s), s
    print(f"ok: {len(relayed)} relayed, {len(own_words)} own words")

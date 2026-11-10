"""Checks for relay.is_relayed: each bot pattern with a paste that must hit and own words that must not."""

import doctest

import pytest

from debrief import relay
from debrief.relay import is_relayed

ACTIONS_LINES = ("2026-01-05T10:00:01.1234567Z ##[group]Run make test\n"
                 "2026-01-05T10:00:02.1234567Z running 3 tests\n"
                 "2026-01-05T10:00:03.1234567Z ##[error]Process completed with exit code 1.\n")

# pattern -> (a paste that is relayed, a person's own words on the same subject)
CASES = {
    "trail header": ("Entire Trail review comment\nID: 019f-c403\nSeverity: medium\nContext: cmd/app/api.go:15",
                     "tests are failing and you dismissed a trail finding"),
    "severity heading": ("### Medium finding\n\n**Location:** `cmd/app/hook.go:163`\n\nThe Windows wrappers ...",
                         "now high severity finding on trail"),
    "codex [P2]": (" - [P2] Empty output misclassifies absent providers — cmd/app/protect.go:135\n   treated as ...",
                   "is the P2 thing from codex legit or not?"),
    "codex P2 dash": (" 1. P2 — Suggested fix is not runnable\n    cmd/app/doctor.go:1390 prints: ...",
                      "p2 can wait, do the parser first"),
    "codex P1 path": ("\n - P1 cmd/app/refs_store.go:480 — remote lookup still fetches only the canonical ref.",
                      "refs_store.go:480 means an anchorless import is now visible in history - i think this is fine?"),
    "short lead-in": ("I've got this issue:\n  - [P2] Pick a placement compatible with the credentials — grant.go:141",
                      "Here it is as one self-contained handoff.\n\n" + "context " * 200 + "\n### High finding\n..."),
    "codex review header": (("let first fix this:\n• The default can come from another identity.\n\n"
                            "  Review comment:\n\n  - [P2] Derive the default region from the token — mirror.go:88\n"
                            "    The default is picked before the token is read, so a caller who set a token for\n"
                            "    another region gets a mirror in the wrong one, and the next request fails."),
                            "can you leave a review comment: on the PR saying we will fix it later"),
    "github suggested changeset": (("These subprocesses inherit the environment ... Consider scrubbing env vars.\n\n"
                                   "This issue also appears on line 34 of the same file.\n\n"
                                   "Suggested changeset 1 (1)\ncmd/x_test.go"),
                                   "there is a high finding by copilot, is it legit? or bullshit ?"),
    "copilot summary": (("## Pull request overview\n\nCopilot reviewed 3 out of 4 changed files in this pull request "
                        "and generated 2 comments."),
                        "can you look at the copilot comments, copilot reviewed the PR again"),
    "coderabbit header": ("_⚠️ Potential issue_ | _🟠 Major_\n\n**Guard against an empty list.**\n\n`items[0]` raises ...",
                          "coderabbit flagged a potential issue in the parser, take a look"),
    "coderabbit marker": ("Guard against an empty list.\n<!-- This is an auto-generated comment by CodeRabbit -->",
                          "ignore the auto-generated comments, coderabbit is noisy today"),
    "coderabbit agent prompt": (("fix these\n\nIn `@src/parser.py` around lines 40-52, guard against an empty list "
                                "before indexing, and add a test for it."),
                                "in src/parser.py around line 40 the guard is wrong, the list can be empty"),
    "coderabbit prompt title": ("🤖 Prompt for AI Agents\nVerify each finding against the current code.",
                                "write me a prompt for AI agents that explains the release steps"),
    "bugbot severity": ("### Retry loop never backs off\n\n**Medium Severity**\n\nThe loop sleeps zero seconds ...",
                        "I'd call this medium severity, the retry loop never backs off"),
    "bugbot markup": (("Retry loop never backs off\n<!-- DESCRIPTION START -->\nThe loop sleeps zero seconds.\n"
                      "<!-- DESCRIPTION END -->\n<!-- BUGBOT_BUG_ID: 1f2e -->"),
                      "bugbot left a comment on the retry loop, description says it never backs off"),
    "sentry suspect issues": (("## Suspect Issues\nThis pull request was deployed and Sentry observed the following "
                              "issues:\n\n- ‼️ **TypeError: x is undefined** `render(app/views/home)`"),
                              "sentry observed the same TypeError again after the deploy, can you check"),
    "sentry bug prediction": (("**Bug:** The handler reads `user.id` before the null check.\n\n"
                              "Severity: CRITICAL | Confidence: 0.95"),
                              "severity is critical here and my confidence is high: the handler reads user.id early"),
    "actions log": (ACTIONS_LINES, "the run failed with ##[error] somewhere near the end, go look at the log"),
    "ci lead, go": (("The tests is failing in CI:\nok      example.com/app/settings    1.890s\n"
                    "--- FAIL: TestX (0.01s)\nFAIL    example.com/app/strategy    19.997s\n"),
                    "tests are failing in CI: can you look"),
    "ci lead, pytest": (("CI is red:\nFAILED tests/test_parser.py::test_empty - IndexError: list index out of range\n"
                        "FAILED tests/test_parser.py::test_one - AssertionError\n"
                        "ERROR tests/test_cli.py::test_help - ImportError\n"),
                        "FAILED tests/test_parser.py::test_empty is the one I care about, the other two are old"),
    "ci lead, cargo": (("the pipeline says:\ntest parser::empty ... FAILED\ntest parser::one ... ok\n"
                       "error: test failed, to rerun pass `--lib`\n"),
                       "the pipeline says no, but I think the parser test is just flaky"),
    "ci lead, jest": (("GitHub Actions output:\nFAIL src/parser.test.ts\n  ✕ handles empty input (3 ms)\n"
                      "  ✓ handles one item (1 ms)\n"),
                      "The radio button selection is moving with the cursor but should not."),
}


@pytest.mark.parametrize("name", CASES)
def test_pattern(name):
    paste, own_words = CASES[name]
    assert is_relayed(paste), paste
    assert not is_relayed(own_words), own_words


def test_edges():
    assert not is_relayed(None)
    assert not is_relayed("")
    assert is_relayed("<pasted_content id='1'>" + CASES["codex [P2]"][0] + "</pasted_content>")
    # Log-shaped lines without a lead-in that names CI are a person's own test run.
    assert not is_relayed("I ran it locally:\n" + CASES["ci lead, pytest"][0].split("\n", 1)[1])
    # Two Actions lines are a quote, three are a log.
    assert not is_relayed("look at this\n" + "".join(ACTIONS_LINES.splitlines(keepends=True)[:2]))


def test_doctests():
    assert doctest.testmod(relay).failed == 0

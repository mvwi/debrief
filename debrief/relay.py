"""Spot human messages that mostly relay a bot's output: review findings, trail comments, CI logs.

A person who pastes a review bot's comment or a CI log isn't correcting the agent themselves;
the agent could have fetched it. is_relayed(text) is True when a paste starts the message, or
follows a short lead-in, or carries a signature only a bot writes. A person's own words that
mention a finding ("there is a finding on the trail") don't count. A miss costs little and a
false hit hides a real correction, so every pattern here must be one people don't type.

    >>> is_relayed("Entire Trail review comment\\nID: 019ff135\\nSeverity: medium")
    True
    >>> is_relayed("I've got this issue:\\n  - [P2] Pick a placement — cmd/x.go:141\\n    mirrorReadCluster ...")
    True
    >>> is_relayed("tests are failing and you dismissed a trail finding")
    False
    >>> is_relayed("Copilot reviewed 3 out of 4 changed files in this pull request and generated 2 comments.")
    True
    >>> is_relayed("copilot reviewed this and says the retry loop is wrong, is it?")
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
    r"^[ \t]*In `@[\w./-]+` around lines? \d+",              # CodeRabbit "Prompt for AI Agents"
]), re.IGNORECASE | re.MULTILINE)

# Text only a bot writes; relayed wherever it appears.
SIGNATURE = re.compile("|".join([  # noqa: FLY002 (one pattern per line, each with its source)
    r"^Suggested changeset \d",                               # GitHub UI, copied with a Copilot comment
    r"^This issue also appears (?:on|in) line",
    r"^Copilot reviewed \d+ out of \d+ changed files",        # Copilot review summary
    r"auto-generated (?:comment|reply) by CodeRabbit",
    r"^_?⚠️ Potential issue_?(?:[ \t]*\||[ \t]*$)",           # CodeRabbit comment header
    r"^(?:<summary>)?🤖 Prompt for AI Agents",
    r"<!-- (?:BUGBOT_BUG_ID|DESCRIPTION START)\b",            # Cursor Bugbot comment markup
    r"^\*\*(?:Low|Medium|High|Critical) Severity\*\*[ \t]*$",  # Bugbot severity line
    r"\bSentry observed the following issues:",              # Sentry "Suspect Issues" PR comment
    r"\bSeverity: (?:CRITICAL|HIGH|MEDIUM|LOW) \| Confidence: \d",  # Sentry bug prediction footer
]), re.MULTILINE)

# Raw GitHub Actions log lines: a 7-digit timestamp prefix or a ##[error] marker. Three of
# them make a pasted log, with or without a lead-in.
ACTIONS_LOG = re.compile(r"^(?:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z |##\[(?:error|group|endgroup|warning)\])",
                         re.MULTILINE)

# A CI log with a one-line lead-in: "The tests is failing in CI:\nok  github.com/..."
# The lead-in must name CI and three lines must look like test-runner output: both, because
# either alone is something people write.
CI_LEAD = re.compile(r"\A[^\n]{0,80}\b(?:CI|pipeline|GitHub Actions)\b[^\n]{0,40}:[ \t]*\n", re.IGNORECASE)
CI_LOG = re.compile("|".join([  # noqa: FLY002 (one pattern per line, each with its source)
    r"^(?:ok|FAIL|PASS|---|===)[ \t]",                        # go test, jest
    r"^(?:FAILED|ERROR|PASSED) \S+(?:::|\.py)",               # pytest summary lines
    r"^E {3,}\S",                                             # pytest assertion detail
    r"^test \S+ \.\.\. (?:ok|FAILED|ignored)",                # cargo test
    r"^error(?:\[E\d+\])?: ",                                 # rustc, many compilers
    r"^npm (?:ERR!|error) ",
    r"^[ \t]*[✓✕✗×●] ",                                       # jest, vitest, mocha
]), re.MULTILINE)

PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")
LEAD_MAX = 300      # chars of the person's own words before the paste
LEAD_SHARE = 0.35   # ... and at most this share of the message


def is_relayed(text: str | None) -> bool:
    """True when the message is mostly pasted bot, review or CI output."""
    text = PASTE_TAG.sub("", text or "").strip()
    if not text:
        return False
    if SIGNATURE.search(text) or len(ACTIONS_LOG.findall(text)) >= 3:
        return True
    if CI_LEAD.match(text) and len(CI_LOG.findall(text)) >= 3:
        return True
    m = START.search(text)
    return bool(m) and m.start() <= LEAD_MAX and m.start() <= LEAD_SHARE * len(text)

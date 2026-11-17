"""Replace secret-shaped strings with [redacted] before text leaves the database for the report or an upload.

    redact(text)      one string
    redact_tree(obj)  nested JSON-like data
    scrub(obj, home_paths=False, emails=False)  either, plus counts; for callers that upload

Three kinds of rule, in the order they run: exact values this machine already knows (debrief's
saved OpenRouter key and secret-named environment variables), known token shapes (SECRETS),
and values given to a secret-sounding name (NAMED). It is pattern matching: a secret with
no known shape and no telling name beside it gets through.
"""

import os
import re
from functools import cache
from pathlib import Path

from debrief import config

MARK = "[redacted]"
EMAIL_MARK = "[email]"
VERSION = 2  # bump when a change to the patterns or scrub rules changes what comes out; uploads record it

# Redacting serialized JSON instead of each string misses a secret that follows an escape:
# in "\nghp_..." the "n" of the escape is a word character, so \b never matches.
SECRETS = [
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"), MARK),
    (re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{20,}"), MARK),  # needs a digit
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,}"), MARK),  # Stripe
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MARK),
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"), MARK),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), MARK),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{35}"), MARK),  # Google API key
    (re.compile(r"\b(?:npm|hf)_[A-Za-z0-9]{30,}"), MARK),
    (re.compile(r"\bre_[A-Za-z0-9]{8}_[A-Za-z0-9]{20,}"), MARK),  # Resend
    # Other vendors' prefixes. The digit keeps snake_case names (r8_some_long_function_name) out.
    (re.compile(r"\b(?:gsk|r8|phx|sbp|shpat|figd|ntn|dop_v1|lin_api|whsec|sntrys|sb_secret)_"
                r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}"), MARK),
    (re.compile(r"\b(?:ya29\.|GOCSPX-|SG\.|pypi-AgE|xai-|pplx-|tvly-)(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9._-]{20,}"), MARK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), MARK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL), MARK),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{24,}=*"), rf"\1{MARK}"),
    # user:password@ in any URL: https, postgres, mongodb+srv, redis, amqp ...
    (re.compile(r"\b([a-z][a-z0-9+.-]{1,15}://)[^/\s:@\"\\]*:[^/\s@\"\\]+@"), rf"\1{MARK}@"),
    (re.compile(r"(?i)\b((?:set-)?cookie[\"']?:\s*[\"']?)[^\s=;\"']+=[^\n\"']{8,}"), rf"\1{MARK}"),
]

# A value given to a secret-sounding name: API_KEY=..., "client_secret": "...", token: ..., --token ...
# The name stays, the value goes. The value must be 12+ characters with a digit and a letter,
# which leaves out code (`token = options.authToken`) and prose. Also left alone: UUIDs (ids,
# not secrets), paths, the AWS4-HMAC-SHA256 scheme name and PostHog's public phc_ project keys.
# "auth" does not match "author".
NAME = r"(?:api[_-]?key|access[_-]?key|private[_-]?key|secret|token|passw(?:or)?d|passwd|pwd|credential|auth(?!or(?!iz)))"
VALUE = (r"(?![0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|[.~]{0,2}/|AWS4-|phc_)"
         r"(?=[A-Za-z0-9+/_.~=-]*\d)(?=[0-9+/_.~=-]*[A-Za-z])[A-Za-z0-9+/_.~=-]{12,}")
NAMED = [
    (re.compile(rf"(?i)(\b[\w.-]*{NAME}[\w-]*\\?[\"']?[ \t]*[:=][ \t]*(?:(?:basic|bearer|token)[ \t]+)?\\?[\"']?){VALUE}"),
     rf"\1{MARK}"),
    (re.compile(rf"(?i)(--[\w-]*{NAME}[\w-]*[ \t]+\\?[\"']?){VALUE}"), rf"\1{MARK}"),  # a command-line flag: --token abc
]

HOME = re.compile(r"(?<![\w.~-])(?:/(?:Users|home)/|[A-Za-z]:\\{1,2}Users\\{1,2})[^/\\\s\"'`:;,<>|()\[\]{}]+")

# Claude Code names a project's log folder after its path, every other character a dash: -Users-name-Dev-x.
# Only for a home under /Users or /home: the dashed form of / or /root would match ordinary text.
HOME_DASHED = re.sub(r"[^A-Za-z0-9]", "-", str(Path.home())) if Path.home().parent.name in ("Users", "home") else None

# An email address. Left alone: git@host, user@host:path (ssh, scp, git remotes), user@host inside a
# URL, pkg@1.2.3 and @scope/pkg (the domain must start with a letter and end in a letter-only label).
# An address right after a colon (mailto:a@b.com, "email":a@b.com) is one; scheme://user:pass@host is
# safe only because the secret rules blank it first.
# A colon after it marks user@host:path only when text follows at once ("sent to a@b.com: done" is prose).
EMAIL = re.compile(r"(?<![\w.+/@-])(?!git@)[A-Za-z0-9][\w.+-]*@[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b(?![@\w]|:\S|\.[A-Za-z0-9-])")

KNOWN_NAME = re.compile(r"(?i)KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL")
KNOWN_MIN = 12  # a shorter value could blank ordinary words


@cache
def known_secrets() -> tuple[str, ...]:
    """Exact secret values this machine holds: debrief's saved key and secret-named environment variables."""
    values = {v for k, v in os.environ.items() if KNOWN_NAME.search(k)}
    values.add(config.dotenv_values(config.ENV_PATH).get(config.KEY))
    keep = (v for v in values if v and len(v) >= KNOWN_MIN and not v.startswith(("/", "~")))  # not a path to a key file
    return tuple(sorted(keep, key=len, reverse=True))


def clean(text: str) -> tuple[str, int]:
    """Redact one string that holds no U+FFFD; return it and the number of replacements."""
    count = 0
    for secret in known_secrets():
        count += text.count(secret)
        text = text.replace(secret, MARK)
    for pattern, repl in [*SECRETS, *NAMED]:
        text, n = pattern.subn(repl, text)
        count += n
    return text, count


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
        text = text[:start] + MARK + (text[ends[-1]:].lstrip("-") if ends else "")
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
            out += [text[at:start], MARK]
        at = max(at, end)
    text = "".join([*out, text[at:]])

    def blank(m: re.Match) -> str:
        rest = m.group().replace("\ufffd", "")
        return MARK if len(rest) >= DAMAGED_MIN else rest
    return clean(DAMAGED_RUN.sub(blank, text))[0]


def scrub(obj, *, home_paths: bool = False, emails: bool = False):
    """Return (scrubbed copy, {"secrets": n, "home_paths": n, "emails": n}) for a string or JSON-like data.

    Every string is redacted, dict keys included, and U+FFFD is dropped (the artifact host
    rejects it). With home_paths, /Users/<name>, /home/<name> and C:\\Users\\<name> become ~,
    and so does this user's home folder in Claude's dashed form (-Users-<name>). With emails,
    each email address becomes [email]; GitHub logins, @mentions and owner/name stay.
    """
    counts = {"secrets": 0, "home_paths": 0, "emails": 0}

    def walk(o):
        if isinstance(o, str):
            if "\ufffd" in o:
                # Counted by marks gained: the damaged pass merges overlapping finds.
                text = redact_damaged(o)
                counts["secrets"] += max(text.count(MARK) - o.count(MARK), 0)
            else:
                text, n = clean(o)
                counts["secrets"] += n
            if home_paths:
                text, n = HOME.subn("~", text)
                counts["home_paths"] += n
                if HOME_DASHED:
                    counts["home_paths"] += text.count(HOME_DASHED)
                    text = text.replace(HOME_DASHED, "~")
            if emails:
                text, n = EMAIL.subn(EMAIL_MARK, text)
                counts["emails"] += n
            return text
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {walk(k): walk(v) for k, v in o.items()}
        return o

    return walk(obj), counts


def redact(text: str) -> str:
    """Redact secrets in one plain (not JSON-encoded) string."""
    return scrub(text)[0]


def redact_tree(obj):
    """Redact every string, keys included, in a JSON-like structure, and drop U+FFFD."""
    return scrub(obj)[0]

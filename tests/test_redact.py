"""Checks for debrief.redact."""

import json

from debrief.redact import redact, redact_tree

GH = "ghp_" + "a" * 36
CASES = {
    GH: True,
    "sk-ant-api03-" + "Ab1" * 8: True,
    "AKIA" + "A1B2C3D4E5F6G7H8": True,
    "Authorization: Bearer " + "x1" * 15: True,
    "https://user:pass1@github.com/x": True,
    "glpat-" + "aB3_" * 6: True,
    "sk_live_" + "a1B2" * 6: True,
    "sk-learn-model-selection-guide": False,  # a kebab-case name, not a key
    "task-notification": False,
    "AKIAXXXX…": False,
}


def test_redact():
    for text, secret in CASES.items():
        assert ("[redacted]" in redact(text)) == secret, text


def test_secret_after_an_escape():
    # Serialized, a newline before the token reads as "\\nghp_": the old JSON-level pass missed it.
    for lead in ("\n", "\t", "export TOKEN=\n", "line one\r\n"):
        out = json.dumps(redact_tree({"t": [lead + GH, {"deep": lead + GH}]}))
        assert GH not in out and out.count("[redacted]") == 2, lead


def test_private_key():
    pem = '-----BEGIN RSA PRIVATE KEY-----\nMII"x\\y\n-----END RSA PRIVATE KEY-----'
    assert redact(f"key:\n{pem}\nafter") == "key:\n[redacted]\nafter"
    assert "MII" not in redact("-----BEGIN PRIVATE KEY-----\nMIIabc")  # cut off before END


def test_tree_keeps_other_types():
    assert redact_tree({"n": 1, "x": None, "ok": True, "s": "plain"}) == {"n": 1, "x": None, "ok": True, "s": "plain"}


def test_replacement_char_cannot_hide_a_secret():
    split = GH[:10] + "\ufffd" + GH[10:]
    out = json.dumps(redact_tree({split: split}), ensure_ascii=False)
    assert GH not in out and "\ufffd" not in out and out.count("[redacted]") == 2


def test_replacement_char_as_the_only_word_break():
    out = redact_tree("x\ufffd" + GH)
    assert GH not in out and "[redacted]" in out


def test_secret_split_after_its_minimum_length():
    # The head alone is long enough to match, so a head-only redaction would leave the tail.
    tail = "Zz9" * 14
    for token in ("sk-proj-" + "a1B" * 9 + "\ufffd" + tail, "Bearer " + "x1" * 14 + "\ufffd" + tail):
        out = redact_tree("key " + token + " end")
        assert tail not in out and "[redacted]" in out and out.endswith(" end"), out


def test_damaged_text_without_secrets_only_loses_the_mark():
    assert redact_tree("caf\ufffd au lait, task-notification") == "caf au lait, task-notification"


def test_one_mark_breaks_and_another_splits():
    # Neither "all marks removed" nor "all marks as breaks" matches this one.
    out = redact_tree("x\ufffd" + GH[:12] + "\ufffd" + GH[12:] + " end")
    assert GH[12:] not in out and GH[:12] not in out and out.endswith(" end"), out


def test_only_marks():
    assert redact_tree("\ufffd\ufffd") == ""


def test_mark_as_the_trailing_break():
    aws = "AKIA" + "A1B2C3D4E5F6G7H8"
    for text in (aws + "\ufffdx", "AK\ufffdIA" + aws[4:] + "\ufffdx", "x\ufffd" + aws + "\ufffdy"):
        assert aws[4:] not in redact_tree(text), text


def test_damaged_private_key():
    pem = "-----BEGIN\ufffd RSA PRIVATE KEY-----\nMII" + "a" * 30 + "\ufffd" + "b" * 30 + "\n-----END RSA PRIVATE KEY-----"
    assert "MII" not in redact_tree(pem) and "bbbb" not in redact_tree(pem)


def test_damaged_key_header_with_a_clean_body():
    body = "MII" + "Ab1" * 20
    for header in ("-----BEGIN\ufffdRSA\ufffdPRIVATE\ufffdKEY-----",   # marks where the spaces were
                   "-----BEGIN\ufffdRSA PRI\ufffdVATE KEY-----",        # one mark a space, one extra
                   "-----BEGIN RSA PRIVATE KEY-----\ufffd"):
        out = redact_tree(f"before\n{header}\n{body}\n{body}\n-----END RSA PRIVATE KEY-----\nafter")
        assert body not in out and out.startswith("before\n") and out.endswith("\nafter"), (header, out)
    cut = redact_tree(f"-----BEGIN\ufffdPRIVATE\ufffdKEY-----\n{body}")  # no END line
    assert body not in cut


def test_mark_touching_the_word_before_a_bearer_token():
    token = "x1" * 15
    assert token not in redact_tree("x\ufffdbearer " + token)


def test_old_import_path():
    from debrief import export_site
    from debrief import redact as module
    assert export_site.redact is module.redact and export_site.redact_tree is module.redact_tree
    assert export_site.redact_damaged is module.redact_damaged and export_site.SECRETS is module.SECRETS


def test_redact_alone_handles_damaged_text():
    assert GH[10:] not in redact(GH[:10] + "\ufffd" + GH[10:])


# Made-up values in the shapes the measurement on real logs found, or that the old patterns could not match.
VALUE = "k9Xq" * 6
NEW_SHAPES = [
    "set -Ux RESEND_API_KEY re_" + "a1B2c3D4" + "_" + VALUE,
    "gsk_" + VALUE, "phx_" + VALUE, "lin_api_" + VALUE, "whsec_" + VALUE, "shpat_" + VALUE,
    "ya29." + VALUE, "GOCSPX-" + VALUE, "xai-" + VALUE,
    "DATABASE_URL=postgresql://app:" + "hunter2pw@db.internal:5432/app",
    "mongodb+srv://app:" + "hunter2pw@cluster0.example.net/x",
    "redis://default:" + "hunter2pw@cache:6379", "redis://:" + "hunter2pw@cache:6379",
    "Cookie: session=" + VALUE + "; theme=dark",
    'curl -H "Set-Cookie: sid=' + VALUE + '"',
]


def test_new_shapes():
    for text in NEW_SHAPES:
        out = redact(text)
        assert "[redacted]" in out and VALUE not in out and "hunter2pw" not in out, text


NAMED_SECRETS = [
    "API_KEY={}", "export CLIENT_SECRET='{}'", '"client_secret": "{}"', '{{\\"apiKey\\": \\"{}\\"}}',
    "aws_secret_access_key = {}", "with the team token: {}", "http://127.0.0.1:4740/#token={}",
    '-H "x-task-lint-token: {}"', "Authorization: Basic {}", "authorization: bearer {}", "POSTGRES_PASSWORD={}",
    "install.sh --token '{}' 2>&1", "login --api-key {}",
]
NOT_SECRETS = [
    "const token = options.refreshedAuthToken",              # code: no digit
    '"session_id":"01a1b2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b"',   # an id
    "auth_token: 01a1b2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b",      # a UUID is an id even under a secret name
    "githubCommitAuthorEmail: '12345678+someone@users'",     # author is not auth
    '"tokenRef":"/Users/someone/Library/tokens/a1b2c3"',      # a path
    '"Authorization": "AWS4-HMAC-SHA256 Credential=x"',
    "api_token: phc_" + VALUE,                                # PostHog project keys are public
    "max_tokens: 4096", "tokens: 380", "password: string",
    "commit 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39",        # a git SHA
    "id 01a1b2c3-d4e5-4f60-8a9b-0c1d2e3f4a5b",
    '"integrity": "sha512-' + VALUE + '=="',                  # a lock file hash
]


def test_value_given_to_a_secret_name():
    for form in NAMED_SECRETS:
        out = redact(form.format(VALUE))
        assert VALUE not in out and "[redacted]" in out, form
        assert out.startswith(form.split("{")[0][:6]), out   # the name stays
    assert redact("password=Zq8vR2kLp9XmN4") == "password=[redacted]"  # 12+ characters is enough


def test_not_secrets_are_left_alone():
    for text in NOT_SECRETS:
        assert redact(text) == text


def test_known_values(monkeypatch, tmp_path):
    from debrief import config
    from debrief import redact as module
    env = tmp_path / ".env"
    env.write_text(f"{config.KEY}=plainsavedkeyvalue\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setenv("ACME_DEPLOY_TOKEN", "correct horse battery")   # no known shape, no name beside it below
    monkeypatch.setenv("SHORT_SECRET", "abc")                           # too short: would blank ordinary words
    monkeypatch.setenv("SSH_KEY_PATH", "/home/someone/.ssh/id_ed25519")  # a path, not a secret
    monkeypatch.setenv("EDITOR_NAME", "an ordinary long value")         # not a secret name
    module.known_secrets.cache_clear()
    try:
        text = "ran with correct horse battery and plainsavedkeyvalue; abc /home/someone/.ssh/id_ed25519 an ordinary long value"
        assert redact(text) == "ran with [redacted] and [redacted]; abc /home/someone/.ssh/id_ed25519 an ordinary long value"
    finally:
        monkeypatch.undo()
        module.known_secrets.cache_clear()


def test_scrub_counts_and_home_paths(monkeypatch):
    from debrief import redact as module
    from debrief.redact import scrub
    monkeypatch.setattr(module, "HOME_DASHED", "-Users-someone")
    data = {"t": ["cd /Users/someone/Dev/x && cat /home/runner/work/a.txt", GH + " and " + GH],
            "w": ("C:\\Users\\Some.One\\repo", "projects/-Users-someone-Dev-x/a.jsonl"),
            "url": "https://example.com/home/page and src/Users/list.ts", "n": 3}
    out, counts = scrub(data, home_paths=True)
    assert out == {"t": ["cd ~/Dev/x && cat ~/work/a.txt", "[redacted] and [redacted]"],
                   "w": ["~\\repo", "projects/~-Dev-x/a.jsonl"],
                   "url": "https://example.com/home/page and src/Users/list.ts", "n": 3}
    assert counts == {"secrets": 2, "home_paths": 4, "emails": 0}
    assert data["t"][1] == GH + " and " + GH   # the input is not changed
    kept, counts = scrub(data)
    assert kept["t"][0] == data["t"][0] and counts == {"secrets": 2, "home_paths": 0, "emails": 0}
    assert scrub("x\ufffd" + GH)[1]["secrets"] == 1


NOT_EMAILS = [
    "git remote add origin git@github.com:owner/name.git", "ssh -T git@github.com", "scp deploy@build.example.net:/srv/app .",
    "https://deploy@build.example.net/x", "npm i @scope/pkg@1.2.3 react@19.3.1 next@16.4.4-canary.rc", "pnpm@12.15.0",
    "@app.route('/x')", "@dataclass", "thanks @octocat, see owner/name#12", "icons/logo@2x.png", "python@3.14",
    "charm.land/lipgloss/v2@v2.0.6", "psql postgresql://postgres@127.0.0.1:5432/db", "WEB @type:session -@session.initial_view.name:/x",
]


def test_emails():
    from debrief.redact import scrub
    text = 'to: ["jane.doe+work@example.co.uk"], cc <bob@example.com>; author 12345+octocat@users.noreply.github.com'
    out, counts = scrub(text, emails=True)
    assert out == 'to: ["[email]"], cc <[email]>; author [email]' and counts["emails"] == 3
    assert scrub(text)[0] == text and redact(text) == text   # off by default, and never in the local report
    for kept in NOT_EMAILS:
        assert scrub(kept, emails=True) == (kept, {"secrets": 0, "home_paths": 0, "emails": 0}), kept


def test_version():
    from debrief.redact import VERSION
    assert VERSION == 1

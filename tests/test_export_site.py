"""Checks for export_site.redact and redact_tree."""

import json

from debrief.export_site import redact, redact_tree

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
    assert redact_tree(pem) == "[redacted]"

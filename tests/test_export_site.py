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

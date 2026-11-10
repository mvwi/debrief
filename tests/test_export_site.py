"""Checks for export_site.redact. Run: uv run pytest"""

import json

from debrief.export_site import redact

CASES = {
    "ghp_" + "a" * 36: True,
    "sk-ant-api03-" + "Ab1" * 8: True,
    "AKIA" + "A1B2C3D4E5F6G7H8": True,
    "Authorization: Bearer " + "x1" * 15: True,
    "https://user:pass1@github.com/x": True,
    "sk-learn-model-selection-guide": False,  # a kebab-case name, not a key
    "task-notification": False,
    "AKIAXXXX…": False,
}


def test_redact():
    for text, secret in CASES.items():
        out = redact(json.dumps({"t": text}))
        json.loads(out)  # a redaction must leave valid JSON
        assert ("[redacted]" in out) == secret, (text, out)

    pem = redact(json.dumps({"t": '-----BEGIN RSA PRIVATE KEY-----\nMII"x\n-----END RSA PRIVATE KEY-----'}))
    assert "[redacted]" in pem and json.loads(pem)

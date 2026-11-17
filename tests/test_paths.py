"""The names existing users' data lives under. A rename must move that data or keep these."""

import os
import subprocess
import sys
from pathlib import Path

from debrief import paths


def show(**env):
    """HOME and DB_PATH as a fresh process sees them: paths reads the environment at import."""
    base = {k: v for k, v in os.environ.items() if k not in ("DEBRIEF_HOME", "DEBRIEF_DB")}
    code = "from debrief import paths; print(paths.HOME); print(paths.DB_PATH)"
    out = subprocess.run([sys.executable, "-c", code], env=base | env, check=True, capture_output=True, text=True)
    return out.stdout.split()


def test_stored_names_are_unchanged():
    home = str(Path.home() / ".local" / "share" / "debrief")
    assert show() == [home, f"{home}/debrief.db"]
    assert show(DEBRIEF_HOME="/x/y") == ["/x/y", "/x/y/debrief.db"]
    assert show(DEBRIEF_DB="/z/d.db") == [home, "/z/d.db"]
    assert paths.TOKEN_HEADER == "X-Debrief-Token"


def test_page_and_server_share_the_name():
    js = (paths.SITE_SRC / "app.js").read_text()
    assert f'const APP = "{paths.NAME}";' in js

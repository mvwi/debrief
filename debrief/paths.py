"""Where debrief keeps its data. Set DEBRIEF_HOME to move it; nothing is written inside the package."""

import os
from pathlib import Path

# The one name the program matches on or stores under: data folder, env var prefix, database
# file, token header. Changing it strands existing users' data: see tests/test_paths.py.
NAME = "debrief"
TOKEN_HEADER = f"X-{NAME.title()}-Token"

HOME = Path(os.environ.get(f"{NAME.upper()}_HOME") or Path.home() / ".local" / "share" / NAME)
DB_PATH = Path(os.environ.get(f"{NAME.upper()}_DB") or HOME / f"{NAME}.db")
RAW_DIR = HOME / "raw"          # one folder per session, as fetched or imported
BUNDLE_DIR = HOME / "bundles"   # evidence bundles sent to the model
SITE_OUT = HOME / "site"        # the built site: static files plus data/
ARTIFACT_OUT = HOME / "artifact"
SITE_SRC = Path(__file__).parent / "site"

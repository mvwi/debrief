"""Where debrief keeps its data. Set DEBRIEF_HOME to move it; nothing is written inside the package."""

import os
from pathlib import Path

HOME = Path(os.environ.get("DEBRIEF_HOME") or Path.home() / ".local" / "share" / "debrief")
DB_PATH = Path(os.environ.get("DEBRIEF_DB") or HOME / "debrief.db")
RAW_DIR = HOME / "raw"          # one folder per session, as fetched or imported
BUNDLE_DIR = HOME / "bundles"   # evidence bundles sent to the model
SITE_OUT = HOME / "site"        # the built site: static files plus data/
ARTIFACT_OUT = HOME / "artifact"
SITE_SRC = Path(__file__).parent / "site"

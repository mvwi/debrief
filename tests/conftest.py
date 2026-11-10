"""Keep every test away from the user's real data folder."""

import os
import tempfile

# Set before any debrief module is imported: debrief.paths reads it at import time.
os.environ["DEBRIEF_HOME"] = tempfile.mkdtemp(prefix="debrief-test-")
os.environ.pop("DEBRIEF_DB", None)

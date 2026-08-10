"""Fix the environment before anything imports the application.

Both `BUSINESS` and `CRM_DB` are read at import time — the first by business.py,
the second by crm.py — so setting them inside a test module is already too late
if pytest imported another module first. And `os.environ.setdefault` is not
protection: a developer with `BUSINESS=clinic` exported in their shell (the
documented way to run the project) had nine tests fail for no visible reason.

The database goes to a temporary directory for the same reason the simulator
does it: these tests delete rows, and the real databases hold the demo data the
dashboard is shown with.
"""

import os
import tempfile
from pathlib import Path

os.environ["BUSINESS"] = "hotel"
os.environ.pop("BUSINESS_CONFIG", None)
os.environ["CRM_DB"] = str(Path(tempfile.mkdtemp(prefix="receptionist-tests-")) / "test.db")

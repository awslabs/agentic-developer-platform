from pathlib import Path
import sys

root = Path(__file__).resolve().parents[4]
for directory in [root / "modules/tools", root / "modules/tools/validation", root / "modules/agent-factory/agent-worker-image"]:
    sys.path.insert(0, str(directory))

from test_store import jobs  # noqa: E402,F401

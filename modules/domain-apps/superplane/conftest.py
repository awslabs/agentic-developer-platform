"""Test imports and an explicit marker for credentialed/live boundary checks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "superplane_live: explicit real-environment acceptance; exclude from offline CI",
    )

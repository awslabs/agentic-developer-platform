"""Record selected pytest case IDs before execution; report skips honestly."""

import json
import os
from pathlib import Path
import sys

import pytest

# Some reusable suites run from a module directory with only .github/scripts
# on PYTHONPATH. Resolve the shared registry from this checkout explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytest_plugins = ["tests.regression.pytest_plugin"]


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    target = Path(os.environ["REGRESSION_INVENTORY"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([item.nodeid for item in session.items]) + "\n")
    for item in session.items:
        item.user_properties.append(("adp_case_id", item.nodeid))

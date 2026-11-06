"""Record selected pytest case IDs before execution; report skips honestly."""

import json
import importlib.util
import os
from pathlib import Path
import sys

import pytest

# Module suites also own a top-level `tests` package. Load the shared registry
# under its own name so it cannot replace their tests/conftest.py package.
_PACKAGE = "_adp_regression_registry"
_root = Path(__file__).resolve().parents[2] / "tests" / "regression"
if _PACKAGE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _PACKAGE, _root / "__init__.py", submodule_search_locations=[str(_root)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_PACKAGE] = _module
    _spec.loader.exec_module(_module)
pytest_plugins = [f"{_PACKAGE}.pytest_plugin"]


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    target = Path(os.environ["REGRESSION_INVENTORY"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([item.nodeid for item in session.items]) + "\n")
    for item in session.items:
        item.user_properties.append(("adp_case_id", item.nodeid))

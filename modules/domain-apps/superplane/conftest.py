"""Test imports and an explicit marker for credentialed/live boundary checks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The transferred Superplane components (U22, #5326) live under `src/` and carry their
# own tests, which came with them. Those tests import the component's own dependency set
# (`sqlalchemy`, `httpx`, `aiosqlite`, ... — declared in src/superplane-api/pyproject.toml)
# and run from the component directory, because that is the layout upstream wrote them for
# and the transfer preserved every file byte-for-byte.
#
# So they are collected by their OWN lane, not by the module-wide glob. Without this, the
# module-wide run in superplane-domain-ci.yml aborts during COLLECTION with
# `ModuleNotFoundError: No module named 'sqlalchemy'` and reports zero results for the
# other 1772 tests — one uninstalled dependency in transferred code taking down every
# unrelated suite in the module.
#
# This is a collection boundary, not an exemption. `src/` is gated by:
#   * "Run transferred Superplane API tests" in superplane-domain-ci.yml, which installs
#     src/superplane-api[dev] and runs pytest there;
#   * the Go lanes for the controller and platform monitor;
#   * CI path filters that watch src/** (so a change there triggers those lanes).
# src/TRANSFER-MANIFEST.md records the exact commands.
# The trusted executor uses a separate venv with harness-jobs and real PostgreSQL
# in the controller-execution-tests job. Its required JUnit gate refuses skips.
collect_ignore = ["src", "executor/tests"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "superplane_live: explicit real-environment acceptance; exclude from offline CI",
    )

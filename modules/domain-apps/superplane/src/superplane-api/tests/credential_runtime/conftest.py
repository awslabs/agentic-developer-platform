"""Reuse real lifecycle journals in the lane that installs the Kubernetes SDK.

The module-wide domain lane installs Gateway dependencies only. API CI installs
the maintained API/bootstrap/lifecycle/executor packages and the actual SDK.
These tests retain the production verifier and fake provider transports; no SDK
stub or skip substitutes for the dependency. The app-owned transfer constraints
pin this lane to SDK31, matching the executor worker extra (>=31,<32). These are
source integration tests; they do not replace built-image or live acceptance.
"""

import sys
import importlib
from pathlib import Path

import pytest
from kubernetes import client  # noqa: F401 - required dependency, never importorskip

# Only shared test support lives outside the installed package. Production
# packages are installed by the API lane; this mirrors its account recovery suite.
MODULE_TEST_ROOT = Path(__file__).resolve().parents[4]
if str(MODULE_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_TEST_ROOT))

from workspace_provisioning.tests.test_bootstrap_runtime_postgres import (  # noqa: E402,F401
    bootstrap_harness,
)
from workspace_provisioning.tests.test_member_credentials import fixture  # noqa: E402,F401


@pytest.fixture(autouse=True)
def require_real_credential_journal(monkeypatch):
    # No module-level skip mark is applied in this directory. A missing/broken
    # disposable PostgreSQL must fail fixture setup, including outside CI.
    monkeypatch.setenv("WORKSPACE_PROVISIONING_REQUIRE_POSTGRES", "1")
    # The reused migration renderer also has lazy optional-dependency skips.
    # Resolve its actual dependencies here so absence fails before those paths.
    for dependency in ("pgserver", "asyncpg", "sqlalchemy", "alembic"):
        importlib.import_module(dependency)

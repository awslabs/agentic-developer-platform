"""
Shared fixtures for the infra-consistency E2E tests.

These tests use boto3 + kubernetes client directly (no browser).
They skip cleanly if E2E_CHAT_ENABLED != 1 or if kubectl / AWS creds
are unavailable.
"""

from __future__ import annotations

import os
import shutil

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-skip all tests in this package unless E2E_CHAT_ENABLED=1."""
    enabled = os.environ.get("E2E_CHAT_ENABLED", "").lower() in ("1", "true", "yes")
    skip_marker = pytest.mark.skip(
        reason="Infra consistency tests require E2E_CHAT_ENABLED=1 and AWS/kubectl"
    )
    for item in items:
        if not enabled:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def kubectl_available():
    """Skip the entire session if kubectl is not on PATH."""
    if not shutil.which("kubectl"):
        pytest.skip("kubectl not found on PATH — skipping infra tests")
    return True


@pytest.fixture(scope="session")
def aws_region():
    return os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture(scope="session")
def environment():
    return os.environ.get("ENVIRONMENT", "dev")


@pytest.fixture(scope="session")
def name_prefix(environment):
    return f"adp-{environment}"

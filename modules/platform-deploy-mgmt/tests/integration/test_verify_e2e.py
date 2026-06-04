"""Integration test: end-to-end phase verification against a real account.

Gated behind INTEGRATION_TEST_ACCOUNT env var. Skipped in CI unless explicitly
enabled (requires real AWS credentials).

Usage:
    INTEGRATION_TEST_ACCOUNT=443458828159 pytest tests/integration/ -v
"""

from __future__ import annotations

import os

import boto3
import pytest

from platform_deploy_mgmt.checks.boto_helpers import Context
from platform_deploy_mgmt.checks.runner import run_phase

INTEGRATION_ACCOUNT = os.environ.get("INTEGRATION_TEST_ACCOUNT")

pytestmark = pytest.mark.skipif(
    not INTEGRATION_ACCOUNT,
    reason="INTEGRATION_TEST_ACCOUNT not set — skipping live integration tests",
)


@pytest.fixture
def live_ctx() -> Context:
    """Build a Context using real AWS credentials from environment."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    # Use current credentials for both sessions in integration test
    session = boto3.Session(region_name=region)
    return Context(
        customer_account_id=INTEGRATION_ACCOUNT,
        region=region,
        environment="dev",
        customer_session=session,
        platform_session=session,
        deploy_id=f"integration-test-{INTEGRATION_ACCOUNT}",
        mode="deploy",
        workflow_run_id="integration-test",
        workflow_run_url="",
    )


class TestPhase1E2E:
    """Run Phase 1 checks against a real AWS account."""

    def test_phase_1_completes(self, live_ctx):
        """Phase 1 verification runs to completion (may pass or fail)."""
        exit_code = run_phase(1, live_ctx)
        # We just assert it runs without crashing — actual pass/fail
        # depends on the target account's state
        assert exit_code in (0, 1)

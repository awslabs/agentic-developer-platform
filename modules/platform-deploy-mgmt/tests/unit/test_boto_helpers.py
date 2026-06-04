"""Unit tests for boto_helpers.py — verify session separation contract."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from platform_deploy_mgmt.checks.boto_helpers import Context


class TestContextProperties:
    """Verify Context derived properties."""

    def test_state_bucket_name(self):
        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=MagicMock(),
            platform_session=MagicMock(),
        )
        assert ctx.state_bucket_name == "adp-terraform-state-123456789012"

    def test_evidence_bucket_name(self):
        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=MagicMock(),
            platform_session=MagicMock(),
        )
        assert ctx.evidence_bucket_name == "adp-platform-deploy-evidence"

    def test_deployments_table_name(self):
        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=MagicMock(),
            platform_session=MagicMock(),
        )
        assert ctx.deployments_table_name == "adp-platform-deployments"


class TestSessionSeparation:
    """Verify that customer_session is never used for write operations."""

    def test_customer_session_is_distinct_from_platform_session(self):
        """The two sessions must be distinct objects."""
        customer = MagicMock(name="customer_session")
        platform = MagicMock(name="platform_session")
        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=customer,
            platform_session=platform,
        )
        assert ctx.customer_session is not ctx.platform_session

    def test_build_context_from_env_creates_separate_sessions(self):
        """build_context_from_env must create two distinct sessions."""
        env = {
            "CUSTOMER_ACCOUNT_ID": "111222333444",
            "AWS_REGION": "us-east-1",
            "ENVIRONMENT": "dev",
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_SESSION_TOKEN": "token123",
            "GITHUB_RUN_ID": "99999",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "aws-e/adp",
        }
        with patch.dict("os.environ", env, clear=False):
            from platform_deploy_mgmt.checks.boto_helpers import build_context_from_env

            ctx = build_context_from_env()
            assert ctx.customer_account_id == "111222333444"
            assert ctx.customer_session is not ctx.platform_session

    def test_runner_uses_platform_session_for_s3_upload(self):
        """Evidence upload must use platform_session, not customer_session."""
        from platform_deploy_mgmt.checks.runner import _upload_evidence
        from platform_deploy_mgmt.checks.shape import (
            CheckSummary,
            PhaseEvidence,
            RunMetadata,
            RunTarget,
        )

        customer = MagicMock(name="customer_session")
        platform = MagicMock(name="platform_session")
        mock_s3 = MagicMock()
        platform.client.return_value = mock_s3

        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=customer,
            platform_session=platform,
            mode="deploy",
        )

        evidence = PhaseEvidence(
            phase=1,
            phase_name="Test",
            target=RunTarget("123456789012", "us-east-1", "dev"),
            run=RunMetadata(),
            result="pass",
            summary=CheckSummary(1, 0, 0, 1),
            checks=[],
        )

        _upload_evidence(ctx, 1, evidence)

        # platform_session.client("s3") was called, not customer_session
        platform.client.assert_called_with("s3")
        customer.client.assert_not_called()

    def test_runner_uses_platform_session_for_ddb_update(self):
        """DDB update must use platform_session, not customer_session."""
        from platform_deploy_mgmt.checks.runner import _update_deployment_status
        from platform_deploy_mgmt.checks.shape import (
            CheckSummary,
            PhaseEvidence,
            RunMetadata,
            RunTarget,
        )

        customer = MagicMock(name="customer_session")
        platform = MagicMock(name="platform_session")
        mock_table = MagicMock()
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        platform.resource.return_value = mock_resource

        ctx = Context(
            customer_account_id="123456789012",
            region="us-east-1",
            environment="dev",
            customer_session=customer,
            platform_session=platform,
            deploy_id="test-1",
            mode="deploy",
        )

        evidence = PhaseEvidence(
            phase=1,
            phase_name="Test",
            target=RunTarget("123456789012", "us-east-1", "dev"),
            run=RunMetadata(finished_at="2024-01-01T00:00:00Z"),
            result="pass",
            summary=CheckSummary(1, 0, 0, 1),
            checks=[],
        )

        _update_deployment_status(ctx, 1, evidence)

        # platform_session.resource("dynamodb") was called, not customer_session
        platform.resource.assert_called_with("dynamodb")
        customer.resource.assert_not_called()

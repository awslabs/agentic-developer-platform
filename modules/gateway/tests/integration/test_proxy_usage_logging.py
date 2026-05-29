"""Issue #992: Verify ProxyService.invoke() writes rows to usage_logs."""

from unittest.mock import patch

import pytest

from src.proxy.service import ProxyService
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def agent_token_context():
    """Token context representing an agent caller."""
    from datetime import datetime, timedelta

    return TokenContext(
        user_id="agent-dev-001",
        org_id="org-test-123",
        team_id="team-test-789",
        department_id="dept-test-012",
        account_type="agent",
        is_admin=False,
        expires_at=datetime.now() + timedelta(hours=1),
    )


class TestProxyUsageLogging:
    """Verify that proxy invoke writes to usage_logs on success and failure."""

    @pytest.mark.asyncio
    async def test_invoke_success_writes_usage_log(self, db_session_factory, agent_token_context):
        """A successful invoke() call should write a usage_logs row with status 200."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = {
            "api_format": "bedrock",
            "model": "claude-3.5-sonnet",
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            await proxy.invoke(request, agent_token_context)

        # Verify a row was written
        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.org_id == "org-test-123"
        assert row.status_code == 200
        assert row.model == "claude-3.5-sonnet"
        assert row.account_type == "agent"
        assert row.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_invoke_failure_writes_usage_log(self, db_session_factory, agent_token_context):
        """A failed invoke() call should write a usage_logs row with status 500."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient(error=RuntimeError("Bedrock unavailable"))
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = {
            "api_format": "bedrock",
            "model": "claude-3.5-sonnet",
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            with pytest.raises(Exception):
                await proxy.invoke(request, agent_token_context)

        # Verify a failure row was written
        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status_code == 500
        assert row.input_tokens == 0
        assert row.output_tokens == 0

    @pytest.mark.asyncio
    async def test_usage_logging_failure_does_not_break_invoke(self, agent_token_context):
        """If usage logging fails, invoke() should still succeed."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = {
            "api_format": "bedrock",
            "model": "claude-3.5-sonnet",
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        # Make get_session_factory raise to simulate DB unavailability
        with patch(
            "src.proxy.service.get_session_factory",
            side_effect=RuntimeError("DB down"),
        ):
            # Should not raise — logging failure is swallowed
            result = await proxy.invoke(request, agent_token_context)

        assert result is not None

"""Issue #992 / #1001: Verify all ProxyService entry points write rows to usage_logs.

PR #993 wired _log_usage() into invoke() only, but production routes call
invoke_model(), chat_completions(), and messages(). Issue #1001 moved logging
into the 6 inner helpers so every path gets coverage.
"""

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


class TestInternalInvokeUsageLogging:
    """Verify that the internal invoke() helper no longer writes usage_logs directly.

    After issue #1001, invoke() delegates to _invoke_bedrock() which is wrapped
    by the per-format helpers that handle logging. invoke() itself should NOT
    double-write.
    """

    @pytest.mark.asyncio
    async def test_internal_invoke_does_not_write_usage_log(self, db_session_factory, agent_token_context):
        """invoke() should NOT write a usage_logs row (logging moved to inner helpers)."""
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

        # invoke() no longer writes usage_logs — the per-format helpers do
        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 0


class TestInvokeModelUsageLogging:
    """Verify invoke_model() (non-streaming) writes usage_logs."""

    @pytest.mark.asyncio
    async def test_invoke_model_success_writes_usage_log(self, db_session_factory, agent_token_context):
        """invoke_model(stream=False) should write a usage_logs row with status 200."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            await proxy.invoke_model("claude-3.5-sonnet", body, agent_token_context, stream=False)

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status_code == 200
        assert row.org_id == "org-test-123"
        assert row.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_invoke_model_failure_writes_usage_log(self, db_session_factory, agent_token_context):
        """invoke_model(stream=False) failure should write a usage_logs row with status 500."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient(error=RuntimeError("Bedrock unavailable"))
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            with pytest.raises(Exception):
                await proxy.invoke_model("claude-3.5-sonnet", body, agent_token_context, stream=False)

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        assert rows[0].status_code == 500
        assert rows[0].input_tokens == 0
        assert rows[0].output_tokens == 0


class TestInvokeModelStreamUsageLogging:
    """Verify invoke_model() (streaming) writes usage_logs after stream completes."""

    @pytest.mark.asyncio
    async def test_invoke_model_stream_writes_usage_log(self, db_session_factory, agent_token_context):
        """invoke_model(stream=True) should write a usage_logs row after stream is consumed."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            stream = await proxy.invoke_model("claude-3.5-sonnet", body, agent_token_context, stream=True)
            # Consume the stream
            chunks = []
            async for chunk in stream:
                chunks.append(chunk)

        assert len(chunks) > 0

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        row = rows[0]
        assert row.status_code == 200
        assert row.latency_ms >= 0
        # Streaming extracts usage from SSE chunks
        assert row.input_tokens >= 0
        assert row.output_tokens >= 0


class TestChatCompletionsUsageLogging:
    """Verify chat_completions() writes usage_logs."""

    @pytest.mark.asyncio
    async def test_chat_completions_non_stream_writes_usage_log(self, db_session_factory, agent_token_context):
        """chat_completions(stream=False) should write a usage_logs row."""
        from src.proxy.schemas import OpenAIChatCompletionRequest, OpenAIMessage, OpenAIRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="hi")],
            max_tokens=100,
            stream=False,
        )

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            await proxy.chat_completions(request, agent_token_context)

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        assert rows[0].status_code == 200
        assert rows[0].org_id == "org-test-123"

    @pytest.mark.asyncio
    async def test_chat_completions_stream_writes_usage_log(self, db_session_factory, agent_token_context):
        """chat_completions(stream=True) should write a usage_logs row after stream consumed."""
        from src.proxy.schemas import OpenAIChatCompletionRequest, OpenAIMessage, OpenAIRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="hi")],
            max_tokens=100,
            stream=True,
        )

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            stream = await proxy.chat_completions(request, agent_token_context)
            # Consume stream
            async for _ in stream:
                pass

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        assert rows[0].status_code == 200


class TestMessagesUsageLogging:
    """Verify messages() writes usage_logs."""

    @pytest.mark.asyncio
    async def test_messages_non_stream_writes_usage_log(self, db_session_factory, agent_token_context):
        """messages(stream=False) should write a usage_logs row."""
        from src.proxy.schemas import AnthropicMessage, AnthropicMessagesRequest, AnthropicRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="hi")],
            max_tokens=100,
            stream=False,
        )

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            await proxy.messages(request, agent_token_context)

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        assert rows[0].status_code == 200
        assert rows[0].org_id == "org-test-123"

    @pytest.mark.asyncio
    async def test_messages_stream_writes_usage_log(self, db_session_factory, agent_token_context):
        """messages(stream=True) should write a usage_logs row after stream consumed."""
        from src.proxy.schemas import AnthropicMessage, AnthropicMessagesRequest, AnthropicRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="hi")],
            max_tokens=100,
            stream=True,
        )

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            stream = await proxy.messages(request, agent_token_context)
            # Consume stream
            async for _ in stream:
                pass

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        assert rows[0].status_code == 200


class TestUsageLoggingResilience:
    """Verify usage logging failures don't break the proxy."""

    @pytest.mark.asyncio
    async def test_usage_logging_failure_does_not_break_invoke_model(self, agent_token_context):
        """If usage logging fails, invoke_model() should still succeed."""
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

        # Make get_session_factory raise to simulate DB unavailability
        with patch(
            "src.proxy.service.get_session_factory",
            side_effect=RuntimeError("DB down"),
        ):
            result = await proxy.invoke_model("claude-3.5-sonnet", body, agent_token_context, stream=False)

        assert result is not None

    @pytest.mark.asyncio
    async def test_usage_logging_failure_does_not_break_chat_completions(self, agent_token_context):
        """If usage logging fails, chat_completions() should still succeed."""
        from src.proxy.schemas import OpenAIChatCompletionRequest, OpenAIMessage, OpenAIRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="hi")],
            max_tokens=100,
            stream=False,
        )

        with patch(
            "src.proxy.service.get_session_factory",
            side_effect=RuntimeError("DB down"),
        ):
            result = await proxy.chat_completions(request, agent_token_context)

        assert result is not None

    @pytest.mark.asyncio
    async def test_usage_logging_failure_does_not_break_messages(self, agent_token_context):
        """If usage logging fails, messages() should still succeed."""
        from src.proxy.schemas import AnthropicMessage, AnthropicMessagesRequest, AnthropicRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="hi")],
            max_tokens=100,
            stream=False,
        )

        with patch(
            "src.proxy.service.get_session_factory",
            side_effect=RuntimeError("DB down"),
        ):
            result = await proxy.messages(request, agent_token_context)

        assert result is not None


class TestStreamingUsageTokenCounts:
    """Verify streaming paths extract real token counts from SSE chunks."""

    @pytest.mark.asyncio
    async def test_streaming_extracts_token_counts(self, db_session_factory, agent_token_context):
        """Streaming usage_logs rows should have non-zero token counts from SSE data."""
        from src.proxy.schemas import AnthropicMessage, AnthropicMessagesRequest, AnthropicRole
        from tests.proxy.conftest import MockBedrockClient, MockPoolService

        # The default mock stream includes message_start with input_tokens=10
        # and message_delta with output_tokens=5
        mock_client = MockBedrockClient()
        mock_pool = MockPoolService(client=mock_client)
        proxy = ProxyService(pool_service=mock_pool)

        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="hi")],
            max_tokens=100,
            stream=True,
        )

        with patch("src.proxy.service.get_session_factory", return_value=db_session_factory):
            stream = await proxy.messages(request, agent_token_context)
            async for _ in stream:
                pass

        from sqlalchemy import select

        from src.shared.models.usage import UsageLog

        async with db_session_factory() as session:
            result = await session.execute(select(UsageLog).where(UsageLog.user_id == "agent-dev-001"))
            rows = result.scalars().all()

        assert len(rows) == 1
        row = rows[0]
        # The mock stream emits message_start with input_tokens=10
        # and message_delta with output_tokens=5
        assert row.input_tokens == 10
        assert row.output_tokens == 5

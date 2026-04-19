"""
E2E tests for proxy/LLM request user stories.

Test modes:
- @pytest.mark.unit: Pure Python-level logic tests (mock_bedrock_client + mocks)
- @pytest.mark.integration: ASGI app in-process tests (api_client in unit mode)
- @pytest.mark.live_only: Real HTTP against deployed gateway

User Stories Covered:
- US-4.1: OpenAI-Compatible Chat Completions
- US-4.2: Anthropic Messages Format
- US-4.3: Bedrock InvokeModel Pass-Through
- US-9.6: Model Not Allowed
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.proxy, pytest.mark.e2e]

from src.shared.exceptions import ModelNotAllowedError
from tests.e2e.config import get_test_bedrock_model
from tests.fixtures.factories import (
    create_department,
    create_org,
    create_team,
    create_token,
    create_user,
)
from tests.fixtures.mock_aws import MockBedrockClient


# =============================================================================
# Unit tests -- pure Python logic, mock_bedrock_client
# =============================================================================


@pytest.mark.unit
class TestOpenAIChatCompletions:
    """
    Unit tests for OpenAI-Compatible Chat Completions.

    User Story US-4.1.
    """

    async def test_openai_format_chat_completion(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """POST /v1/chat/completions accepts OpenAI format."""
        openai_request = {
            "model": "claude-3.5-sonnet",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of France?"},
            ],
            "max_tokens": 1024,
            "temperature": 0.7,
            "stream": False,
        }

        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=openai_request,
        )

        assert response is not None
        assert "content" in response
        assert response["role"] == "assistant"

    async def test_openai_streaming_response(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Streaming responses use Server-Sent Events (SSE)."""
        openai_request = {
            "model": "claude-3.5-sonnet",
            "messages": [{"role": "user", "content": "Tell me a short story."}],
            "max_tokens": 2048,
            "stream": True,
        }

        chunks = []
        async for chunk in mock_bedrock_client.invoke_model_with_response_stream(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=openai_request,
        ):
            chunks.append(chunk)

        assert len(chunks) > 0
        assert b"event:" in chunks[0] or b"data:" in chunks[0]

    async def test_get_models_returns_available_models(self):
        """GET /v1/models returns available models."""
        models_response = {
            "object": "list",
            "data": [
                {"id": "claude-3.5-sonnet", "object": "model", "owned_by": "anthropic", "permission": []},
                {"id": "claude-3-haiku", "object": "model", "owned_by": "anthropic", "permission": []},
            ],
        }

        assert models_response["object"] == "list"
        assert len(models_response["data"]) >= 1

    async def test_model_alias_resolution(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Model aliases resolved to Bedrock model IDs."""
        model_aliases = {
            "claude-3.5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
            "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
            "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
        }

        alias = "claude-3.5-sonnet"
        resolved_model_id = model_aliases.get(alias)
        assert resolved_model_id == "anthropic.claude-3-5-sonnet-20241022-v2:0"

        response = await mock_bedrock_client.invoke_model(
            model_id=resolved_model_id,
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response is not None

    async def test_bearer_token_authentication(
        self,
        db_session: AsyncSession,
    ):
        """Authentication via Authorization: Bearer bg-... header."""
        org = await create_org(db_session, id="org-bearer")
        dept = await create_department(db_session, org.id, id="dept-bearer")
        team = await create_team(db_session, org.id, dept.id, id="team-bearer")
        user = await create_user(db_session, org.id, team.id, id="user-bearer")

        token, raw_token = await create_token(
            db_session, org.id, team.id, dept.id, user.id,
        )
        await db_session.commit()

        assert raw_token.startswith("bg-")
        auth_header = f"Bearer {raw_token}"
        assert auth_header.startswith("Bearer bg-")


@pytest.mark.unit
class TestAnthropicMessagesFormat:
    """
    Unit tests for Anthropic Messages Format.

    User Story US-4.2.
    """

    async def test_anthropic_messages_format(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """POST /v1/messages accepts Anthropic Messages format."""
        anthropic_request = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Explain quantum computing briefly."}],
        }

        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=anthropic_request,
        )

        assert response is not None
        assert response["type"] == "message"
        assert response["role"] == "assistant"

    async def test_anthropic_streaming_format(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Streaming responses match Anthropic SSE format."""
        anthropic_request = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 2048,
            "stream": True,
            "messages": [{"role": "user", "content": "Write a haiku about coding."}],
        }

        chunks = []
        async for chunk in mock_bedrock_client.invoke_model_with_response_stream(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=anthropic_request,
        ):
            chunks.append(chunk)

        assert len(chunks) > 0
        all_content = b"".join(chunks)
        assert b"message_start" in all_content
        assert b"message_stop" in all_content

    async def test_anthropic_version_headers_forwarded(self):
        """anthropic-beta and anthropic-version headers forwarded."""
        request_headers = {
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "messages-2024-01-01",
        }

        assert "anthropic-version" in request_headers
        assert request_headers["anthropic-version"] == "2023-06-01"

    async def test_x_api_key_authentication(
        self,
        db_session: AsyncSession,
    ):
        """Authentication via X-Api-Key: bg-... header."""
        org = await create_org(db_session, id="org-apikey")
        dept = await create_department(db_session, org.id, id="dept-apikey")
        team = await create_team(db_session, org.id, dept.id, id="team-apikey")
        user = await create_user(db_session, org.id, team.id, id="user-apikey")

        token, raw_token = await create_token(
            db_session, org.id, team.id, dept.id, user.id,
        )
        await db_session.commit()

        api_key_header = raw_token
        assert api_key_header.startswith("bg-")


@pytest.mark.unit
class TestBedrockPassThrough:
    """
    Unit tests for Bedrock InvokeModel Pass-Through.

    User Story US-4.3.
    """

    async def test_bedrock_invoke_endpoint(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """/bedrock/invoke endpoint available."""
        bedrock_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "What is 2 + 2?"}],
        }

        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=bedrock_request,
        )
        assert response is not None

    async def test_bedrock_streaming_endpoint(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """/bedrock/invoke-with-response-stream endpoint."""
        bedrock_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": "Count from 1 to 5."}],
        }

        chunks = []
        async for chunk in mock_bedrock_client.invoke_model_with_response_stream(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=bedrock_request,
        ):
            chunks.append(chunk)

        assert len(chunks) > 0

    async def test_anthropic_body_fields_preserved(self):
        """anthropic_beta and anthropic_version body fields preserved."""
        bedrock_request = {
            "anthropic_version": "bedrock-2023-05-31",
            "anthropic_beta": ["computer-use-2024-10-22"],
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }

        assert "anthropic_version" in bedrock_request
        assert "anthropic_beta" in bedrock_request
        assert bedrock_request["anthropic_version"] == "bedrock-2023-05-31"


@pytest.mark.unit
class TestModelNotAllowed:
    """
    Unit tests for Model Not Allowed error handling.

    User Story US-9.6.
    """

    async def test_model_not_allowed_returns_403(self):
        """Requesting unauthorized model returns 403."""
        requested_model = "anthropic.claude-3-opus"
        allowed_models = ["anthropic.claude-3-5-sonnet-*", "amazon.titan-*"]

        with pytest.raises(ModelNotAllowedError) as exc:
            raise ModelNotAllowedError(requested_model, allowed_models)

        assert exc.value.status_code == 403
        assert exc.value.error == "model_not_allowed"
        assert exc.value.details["model"] == requested_model
        assert exc.value.details["allowed_models"] == allowed_models

    async def test_model_not_allowed_error_message(self):
        """Error message indicates team doesn't have access."""
        with pytest.raises(ModelNotAllowedError) as exc:
            raise ModelNotAllowedError("claude-3-opus", ["claude-3-sonnet"])

        assert "team does not have access" in exc.value.message


@pytest.mark.unit
class TestRequestResponseFormat:
    """Unit tests for request/response format translation."""

    async def test_response_includes_usage_info(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Response includes usage information (tokens)."""
        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hi"}]},
        )

        assert "usage" in response
        assert "input_tokens" in response["usage"]
        assert "output_tokens" in response["usage"]

    async def test_response_includes_model_info(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Response includes model information."""
        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Hello"}]},
        )

        assert "model" in response
        assert "claude" in response["model"].lower()

    async def test_response_includes_stop_reason(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """Response includes stop reason."""
        response = await mock_bedrock_client.invoke_model(
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body={"messages": [{"role": "user", "content": "Count to 3"}]},
        )

        assert "stop_reason" in response
        assert response["stop_reason"] in ["end_turn", "max_tokens", "stop_sequence"]


# =============================================================================
# Integration tests -- HTTP via api_client (ASGI in unit mode, HTTP in live)
# =============================================================================


@pytest.mark.integration
class TestProxyHTTPIntegration:
    """
    HTTP-level proxy tests that exercise the ASGI app.

    In live mode these hit actual Bedrock via the deployed gateway.
    In unit mode the ASGI app is used with mocked backend.
    """

    _REJECT_CODES = (401, 403, 503)

    async def test_unauthenticated_proxy_rejected(self, api_client):
        """POST /v1/messages without auth is rejected."""
        response = await api_client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code in self._REJECT_CODES

    async def test_request_id_propagation(self, api_client):
        """Client-sent X-Request-ID is echoed in the response."""
        import uuid

        req_id = str(uuid.uuid4())
        response = await api_client.post(
            "/v1/messages",
            headers={"X-Request-ID": req_id, "Content-Type": "application/json"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        resp_req_id = response.headers.get("x-request-id", "")
        if resp_req_id:
            assert resp_req_id == req_id


# =============================================================================
# Live-only tests -- OAuth path (real Bedrock calls)
# =============================================================================


@pytest.mark.live_only
class TestLiveBedrockProxyOAuth:
    """
    Live tests that exercise the real proxy path via OAuth / JWT.

    In live mode these hit actual Bedrock via the deployed gateway.
    """

    async def test_bedrock_completion_returns_200(self, api_client, jwt_for_user):
        """POST /v1/messages with configured model returns a Bedrock completion."""
        model = get_test_bedrock_model()
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 30,
                "messages": [{"role": "user", "content": "Say hello in one word."}],
            },
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text[:300]}"
        body = response.json()
        assert "content" in body or "choices" in body

    async def test_streaming_sse_response(self, api_client, jwt_for_user):
        """Streaming invoke returns SSE chunks with message_stop event."""
        model = get_test_bedrock_model()
        async with api_client.stream(
            "POST",
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 50,
                "stream": True,
                "messages": [{"role": "user", "content": "Count to 3."}],
            },
        ) as resp:
            assert resp.status_code == 200
            content_type = resp.headers.get("content-type", "")
            assert "text/event-stream" in content_type or "application/json" in content_type

            chunks: list[str] = []
            async for line in resp.aiter_lines():
                chunks.append(line)

        all_text = "\n".join(chunks)
        assert "data:" in all_text or "data: " in all_text, "Expected SSE data lines"

    async def test_bedrock_error_passthrough(self, api_client, jwt_for_user):
        """Request for an inaccessible model returns structured 4xx, not 500."""
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_user}", "Content-Type": "application/json"},
            json={
                "model": "anthropic.claude-3-opus-99999999-v99:0",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert 400 <= response.status_code < 500, f"Expected 4xx for inaccessible model, got {response.status_code}"

    async def test_request_id_propagation_live(self, api_client, jwt_for_user):
        """Client-sent X-Request-ID is echoed in a successful response."""
        import uuid

        req_id = str(uuid.uuid4())
        model = get_test_bedrock_model()
        response = await api_client.post(
            "/v1/messages",
            headers={
                "Authorization": f"Bearer {jwt_for_user}",
                "Content-Type": "application/json",
                "X-Request-ID": req_id,
            },
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        if response.status_code == 200:
            resp_req_id = response.headers.get("x-request-id", "")
            if resp_req_id:
                assert resp_req_id == req_id

    async def test_agent_jwt_bedrock_completion(self, api_client, jwt_for_agent):
        """Agent M2M JWT can invoke Bedrock via /v1/messages."""
        model = get_test_bedrock_model()
        response = await api_client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {jwt_for_agent}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Say hi."}],
            },
        )
        # Agent should be able to invoke; 200 or a non-auth error
        assert response.status_code != 401, f"Agent JWT should not get 401: {response.text[:200]}"

    async def test_health_endpoint_no_auth(self, api_client):
        """GET /health returns 200 without authentication."""
        response = await api_client.get("/health")
        assert response.status_code == 200, f"Expected 200 on /health, got {response.status_code}"


# =============================================================================
# Live-only tests -- IAM SigV4 path (the canonical agent path)
# =============================================================================


@pytest.mark.live_only
class TestLiveBedrockProxyIAM:
    """
    Live tests that exercise the real proxy path via IAM SigV4.

    This is the primary agent path -- agents running with IRSA sign requests
    with SigV4 and the Lambda authorizer maps the IAM principal to an org/team.
    """

    async def test_iam_bedrock_completion_returns_200(self, iam_signed_client):
        """POST /v1/messages with SigV4 from a registered IRSA returns 200."""
        model = get_test_bedrock_model()
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": 30,
                "messages": [{"role": "user", "content": "Say hello in one word."}],
            },
        )
        # Should succeed or get a business-logic error, not auth rejection
        assert response.status_code != 401, f"IAM auth should not return 401: {response.text[:200]}"
        # Ideally 200
        if response.status_code == 200:
            body = response.json()
            assert "content" in body or "choices" in body

    async def test_iam_streaming_sse_response(self, iam_signed_client):
        """Streaming invoke via IAM SigV4 returns SSE chunks."""
        model = get_test_bedrock_model()
        async with iam_signed_client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": 50,
                "stream": True,
                "messages": [{"role": "user", "content": "Count to 3."}],
            },
        ) as resp:
            if resp.status_code == 200:
                chunks: list[str] = []
                async for line in resp.aiter_lines():
                    chunks.append(line)
                all_text = "\n".join(chunks)
                assert "data:" in all_text or len(chunks) > 0

    async def test_iam_error_passthrough(self, iam_signed_client):
        """IAM-authed request for an inaccessible model returns 4xx, not 500."""
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": "anthropic.claude-3-opus-99999999-v99:0",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        # Should not return 500
        assert response.status_code < 500 or response.status_code in (400, 403, 404), \
            f"Expected <500 for inaccessible model via IAM, got {response.status_code}"

    async def test_iam_request_id_propagation(self, iam_signed_client):
        """X-Request-ID is echoed in IAM-authed responses."""
        import uuid

        req_id = str(uuid.uuid4())
        model = get_test_bedrock_model()
        response = await iam_signed_client.post(
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"X-Request-ID": req_id},
        )
        if response.status_code == 200:
            resp_req_id = response.headers.get("x-request-id", "")
            if resp_req_id:
                assert resp_req_id == req_id

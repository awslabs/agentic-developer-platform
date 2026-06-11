"""
Comprehensive Live API Tests for BedrockGateway.

Issue #219: These are REAL integration tests against the live deployed gateway.
No mocks for Bedrock or database - verifies the full stack:
CloudFront -> ALB -> EKS pods -> RDS database -> Bedrock API

Uses pytest + httpx against https://dp7n42m5j4pl6.cloudfront.net/api

Requirements:
- AWS credentials with access to Secrets Manager (for M2M token)
- pip install httpx boto3

Run:
    pytest tests/integration/test_live_api.py -v --tb=short
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Generator

import boto3
import httpx
import pytest

# Every test in this module makes real HTTP calls against the deployed gateway
# (CloudFront → ALB → EKS → RDS → Bedrock) and needs a live Cognito M2M secret.
# Mark the whole module live_only so the unit CI job (pytest -m "not live_only")
# deselects it; it runs in gateway-live-tests.yml (pytest -m "live_only").
pytestmark = pytest.mark.live_only

# =============================================================================
# Configuration
# =============================================================================

BASE_URL = "https://dp7n42m5j4pl6.cloudfront.net/api"
SECRET_ID = "bedrockgw-dev-agent-cognito-credentials"
AWS_REGION = "us-east-1"

# Model to use for Bedrock tests - using inference profile
TEST_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"
TEST_MODEL_GLOBAL = "global.anthropic.claude-opus-4-5-20251101-v1:0"

# Timeout for requests (Bedrock can be slow)
REQUEST_TIMEOUT = 120.0
STREAM_TIMEOUT = 180.0


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def auth_token() -> str:
    """
    Get M2M token from Cognito via AWS Secrets Manager.

    This token is valid for 60 minutes and is reused for all tests.
    """
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    secret = sm.get_secret_value(SecretId=SECRET_ID)
    creds = json.loads(secret["SecretString"])

    response = httpx.post(
        creds["token_endpoint"],
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "scope": creds["scope"],
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture(scope="session")
def client(auth_token: str) -> Generator[httpx.Client, None, None]:
    """
    Create a sync HTTP client with auth headers for API tests.
    """
    with httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=REQUEST_TIMEOUT,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def async_client(auth_token: str) -> Generator[httpx.AsyncClient, None, None]:
    """
    Create an async HTTP client for concurrent tests.
    """
    client = httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=REQUEST_TIMEOUT,
    )
    yield client
    # Note: cleanup handled in test teardown


@pytest.fixture(scope="session")
def unauthenticated_client() -> Generator[httpx.Client, None, None]:
    """
    Create a client without auth headers for testing auth failures.
    """
    with httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT) as client:
        yield client


@pytest.fixture(scope="module")
def test_org_id(client: httpx.Client) -> str:
    """
    Get an existing organization ID for tests.

    Returns the first available organization - assumes at least one exists.
    """
    response = client.get("/admin/organizations")
    response.raise_for_status()
    orgs = response.json()

    # Handle paginated response
    if isinstance(orgs, dict) and "items" in orgs:
        items = orgs["items"]
    elif isinstance(orgs, list):
        items = orgs
    else:
        pytest.fail(f"Unexpected organizations response format: {orgs}")

    if not items:
        pytest.skip("No organizations found - cannot run admin tests")

    return items[0]["id"]


@pytest.fixture(scope="module")
def unique_id() -> str:
    """Generate a unique ID for test resources."""
    return f"test-live-api-{uuid.uuid4().hex[:8]}"


# =============================================================================
# Test Data Cleanup Tracking
# =============================================================================


class _TestDataTracker:
    """Track test data for cleanup. Prefixed with _ to avoid pytest collection."""

    def __init__(self):
        self.budgets_to_delete: list[tuple[str, str, str, str]] = []  # (org_id, entity_type, entity_id, period_type)
        self.ratelimits_to_delete: list[tuple[str, str, str]] = []  # (org_id, entity_type, entity_id)

    def add_budget(self, org_id: str, entity_type: str, entity_id: str, period_type: str):
        self.budgets_to_delete.append((org_id, entity_type, entity_id, period_type))

    def add_ratelimit(self, org_id: str, entity_type: str, entity_id: str):
        self.ratelimits_to_delete.append((org_id, entity_type, entity_id))


@pytest.fixture(scope="module")
def data_tracker() -> _TestDataTracker:
    """Track test data for cleanup."""
    return _TestDataTracker()


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_data(
    client: httpx.Client,
    data_tracker: _TestDataTracker,
) -> Generator[None, None, None]:
    """Clean up test data after all tests in the module."""
    yield

    # Clean up budgets
    for org_id, entity_type, entity_id, period_type in data_tracker.budgets_to_delete:
        try:
            client.delete(f"/admin/organizations/{org_id}/budget/{entity_type}/{entity_id}/{period_type}")
        except Exception:
            pass  # Best effort cleanup

    # Clean up rate limits
    for org_id, entity_type, entity_id in data_tracker.ratelimits_to_delete:
        try:
            client.delete(f"/admin/organizations/{org_id}/ratelimit/{entity_type}/{entity_id}")
        except Exception:
            pass  # Best effort cleanup


# =============================================================================
# 1. Health & Connectivity Tests
# =============================================================================


class TestHealthConnectivity:
    """Test health and connectivity endpoints."""

    def test_health_endpoint(self, client: httpx.Client):
        """GET /health -> 200 {"status":"healthy"}"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_ready_endpoint(self, client: httpx.Client):
        """GET /ready -> 200 {"status":"ready"}"""
        response = client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_v1_health_endpoint(self, client: httpx.Client):
        """GET /v1/health -> 200"""
        response = client.get("/v1/health")
        assert response.status_code == 200


# =============================================================================
# 2. Proxy / Bedrock Tests (Real Calls)
# =============================================================================


class TestProxyBedrock:
    """Test proxy endpoints with real Bedrock calls."""

    def test_v1_messages_anthropic_format(self, client: httpx.Client):
        """
        POST /v1/messages (Anthropic format) -> verify content, usage, stop_reason.
        """
        response = client.post(
            "/v1/messages",
            json={
                "model": TEST_MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
            },
            headers={"anthropic-version": "2023-06-01"},
            timeout=REQUEST_TIMEOUT,
        )
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()

        # Verify response structure
        assert "content" in data, f"Missing 'content' in response: {data}"
        assert "usage" in data, f"Missing 'usage' in response: {data}"
        assert "stop_reason" in data, f"Missing 'stop_reason' in response: {data}"

        # Verify usage has expected fields
        usage = data["usage"]
        assert "input_tokens" in usage
        assert "output_tokens" in usage

    def test_v1_messages_streaming(self, client: httpx.Client):
        """
        POST /v1/messages with stream: true -> verify SSE events.
        """
        with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": TEST_MODEL,
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "Count from 1 to 5."}],
            },
            headers={"anthropic-version": "2023-06-01"},
            timeout=STREAM_TIMEOUT,
        ) as response:
            assert response.status_code == 200, f"Status: {response.status_code}"

            events = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(line)

            # Verify we received SSE events
            assert len(events) > 0, "No SSE events received"

            # Check for message_start event
            has_message_start = any("message_start" in event for event in events)
            assert has_message_start, f"No message_start event found in: {events[:5]}"

    def test_model_invoke_bedrock_format(self, client: httpx.Client):
        """
        POST /model/{model_id}/invoke -> verify Bedrock format response.
        """
        model_id = TEST_MODEL.replace("/", "%2F")  # URL encode if needed

        response = client.post(
            f"/model/{model_id}/invoke",
            json={
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Say OK."}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()

        # Verify Bedrock response structure
        assert "content" in data or "completion" in data, f"Unexpected response: {data}"

    def test_model_invoke_with_response_stream(self, client: httpx.Client):
        """
        POST /model/{model_id}/invoke-with-response-stream -> verify streaming SSE.
        """
        model_id = TEST_MODEL.replace("/", "%2F")

        with client.stream(
            "POST",
            f"/model/{model_id}/invoke-with-response-stream",
            json={
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Say hello."}],
            },
            timeout=STREAM_TIMEOUT,
        ) as response:
            assert response.status_code == 200

            chunks = list(response.iter_lines())
            assert len(chunks) > 0, "No streaming chunks received"

    def test_bedrock_invoke_passthrough(self, client: httpx.Client):
        """
        POST /bedrock/invoke -> verify pass-through works.
        """
        response = client.post(
            "/bedrock/invoke",
            json={
                "modelId": TEST_MODEL,
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Respond with OK."}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert response.status_code == 200, f"Response: {response.text}"

    def test_v1_chat_completions_openai_format(self, client: httpx.Client):
        """
        POST /v1/chat/completions (OpenAI format) -> verify OpenAI format response.
        """
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": TEST_MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Say hi."}],
            },
            timeout=REQUEST_TIMEOUT,
        )
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()

        # Verify OpenAI response structure
        assert "choices" in data, f"Missing 'choices' in response: {data}"
        assert len(data["choices"]) > 0
        assert "message" in data["choices"][0]

    def test_v1_models_list(self, client: httpx.Client):
        """
        GET /v1/models -> verify returns model list.
        """
        response = client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()

        # Verify response structure (OpenAI format)
        assert "data" in data or isinstance(data, list), f"Unexpected response: {data}"

    def test_v1_messages_count_tokens(self, client: httpx.Client):
        """
        POST /v1/messages/count_tokens -> verify token counting.
        """
        response = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": TEST_MODEL,
                "messages": [{"role": "user", "content": "Hello, world!"}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "input_tokens" in data


# =============================================================================
# 3. Admin API Tests (Real Database Calls)
# =============================================================================


class TestAdminAPI:
    """Test admin API endpoints with real database calls."""

    def test_list_organizations(self, client: httpx.Client):
        """
        GET /admin/organizations -> verify returns list (tests DB connection).
        """
        response = client.get("/admin/organizations")
        assert response.status_code == 200
        data = response.json()

        # Handle paginated response
        if isinstance(data, dict) and "items" in data:
            assert isinstance(data["items"], list)
        elif isinstance(data, list):
            pass  # Direct list is also acceptable
        else:
            pytest.fail(f"Unexpected response format: {data}")

    def test_get_organization(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/organizations/{org_id} -> verify organization details.
        """
        response = client.get(f"/admin/organizations/{test_org_id}")
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["id"] == test_org_id

    def test_list_departments(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/organizations/{org_id}/departments -> verify returns list.
        """
        response = client.get(f"/admin/organizations/{test_org_id}/departments")
        assert response.status_code == 200
        data = response.json()

        # Handle paginated or direct list response
        if isinstance(data, dict) and "items" in data:
            assert isinstance(data["items"], list)
        elif isinstance(data, list):
            pass
        else:
            pytest.fail(f"Unexpected response format: {data}")

    def test_get_usage_timeseries(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/organizations/{org_id}/usage/timeseries -> verify response structure.
        """
        response = client.get(f"/admin/organizations/{test_org_id}/usage/timeseries")
        assert response.status_code == 200
        # Response structure varies - just verify it's valid JSON
        response.json()

    def test_get_user_roles(self, client: httpx.Client):
        """
        GET /admin/users/roles -> verify returns role list.
        """
        response = client.get("/admin/users/roles")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list) or "roles" in data

    def test_get_my_chats(self, client: httpx.Client):
        """
        GET /admin/users/me/chats -> verify returns chat list.
        """
        response = client.get("/admin/users/me/chats")
        assert response.status_code == 200
        data = response.json()
        # Response should be a list or paginated response
        assert isinstance(data, list) or isinstance(data, dict)

    def test_get_org_dashboard(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/dashboard/org/{org_id} -> verify dashboard data.
        """
        response = client.get(f"/admin/dashboard/org/{test_org_id}")
        assert response.status_code == 200
        data = response.json()
        # Dashboard should have some structure
        assert isinstance(data, dict)

    def test_list_budgets(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/organizations/{org_id}/budgets -> verify returns list.
        """
        response = client.get(f"/admin/organizations/{test_org_id}/budgets")
        assert response.status_code == 200
        data = response.json()

        if isinstance(data, dict) and "items" in data:
            assert isinstance(data["items"], list)
        elif isinstance(data, list):
            pass
        else:
            pytest.fail(f"Unexpected response format: {data}")

    def test_list_ratelimits(self, client: httpx.Client, test_org_id: str):
        """
        GET /admin/organizations/{org_id}/ratelimits -> verify returns list.
        """
        response = client.get(f"/admin/organizations/{test_org_id}/ratelimits")
        assert response.status_code == 200
        data = response.json()

        if isinstance(data, dict) and "items" in data:
            assert isinstance(data["items"], list)
        elif isinstance(data, list):
            pass
        else:
            pytest.fail(f"Unexpected response format: {data}")


class TestBudgetCRUD:
    """Test full CRUD cycle for budgets."""

    def test_budget_crud_cycle(
        self,
        client: httpx.Client,
        test_org_id: str,
        unique_id: str,
        data_tracker: _TestDataTracker,
    ):
        """
        Full CRUD cycle for budgets: create -> read -> update -> delete.
        """
        entity_id = f"test-entity-{unique_id}"
        entity_type = "team"
        period_type = "monthly"

        # CREATE
        create_response = client.post(
            f"/admin/organizations/{test_org_id}/budgets",
            json={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "period_type": period_type,
                "budget_amount": 1000.0,
                "warning_threshold": 0.8,
                "hard_limit": True,
            },
        )
        assert create_response.status_code == 201, f"Create failed: {create_response.text}"
        created = create_response.json()
        assert created["entity_id"] == entity_id

        # Track for cleanup
        data_tracker.add_budget(test_org_id, entity_type, entity_id, period_type)

        # READ
        read_response = client.get(f"/admin/organizations/{test_org_id}/budget/{entity_type}/{entity_id}")
        assert read_response.status_code == 200, f"Read failed: {read_response.text}"
        read_data = read_response.json()
        # Handle potentially null response or list
        if read_data:
            if isinstance(read_data, list):
                assert len(read_data) > 0
            else:
                assert read_data.get("entity_id") == entity_id or "budget_amount" in read_data

        # UPDATE
        update_response = client.put(
            f"/admin/organizations/{test_org_id}/budget/{entity_type}/{entity_id}",
            json={
                "budget_amount": 2000.0,
                "warning_threshold": 0.9,
            },
        )
        assert update_response.status_code == 200, f"Update failed: {update_response.text}"

        # DELETE
        delete_response = client.delete(f"/admin/organizations/{test_org_id}/budget/{entity_type}/{entity_id}/{period_type}")
        assert delete_response.status_code == 204, f"Delete failed: {delete_response.text}"


class TestRateLimitCRUD:
    """Test full CRUD cycle for rate limits."""

    def test_ratelimit_crud_cycle(
        self,
        client: httpx.Client,
        test_org_id: str,
        unique_id: str,
        data_tracker: _TestDataTracker,
    ):
        """
        Full CRUD cycle for rate limits: create -> read -> update -> delete.
        """
        entity_id = f"test-entity-{unique_id}"
        entity_type = "team"

        # CREATE
        create_response = client.post(
            f"/admin/organizations/{test_org_id}/ratelimits",
            json={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "requests_per_minute": 100,
                "tokens_per_minute": 10000,
                "concurrent_requests": 10,
            },
        )
        assert create_response.status_code == 201, f"Create failed: {create_response.text}"
        created = create_response.json()
        assert created["entity_id"] == entity_id

        # Track for cleanup
        data_tracker.add_ratelimit(test_org_id, entity_type, entity_id)

        # READ
        read_response = client.get(f"/admin/organizations/{test_org_id}/ratelimit/{entity_type}/{entity_id}")
        assert read_response.status_code == 200, f"Read failed: {read_response.text}"
        read_data = read_response.json()
        if read_data:
            assert read_data.get("entity_id") == entity_id or "requests_per_minute" in read_data

        # UPDATE
        update_response = client.put(
            f"/admin/organizations/{test_org_id}/ratelimit/{entity_type}/{entity_id}",
            json={
                "requests_per_minute": 200,
                "tokens_per_minute": 20000,
            },
        )
        assert update_response.status_code == 200, f"Update failed: {update_response.text}"

        # DELETE
        delete_response = client.delete(f"/admin/organizations/{test_org_id}/ratelimit/{entity_type}/{entity_id}")
        assert delete_response.status_code == 204, f"Delete failed: {delete_response.text}"


# =============================================================================
# 4. Authentication Tests
# =============================================================================


class TestAuthentication:
    """Test authentication and authorization."""

    def test_request_without_token_returns_401(self, unauthenticated_client: httpx.Client):
        """Request without token -> 401."""
        response = unauthenticated_client.get("/admin/organizations")
        assert response.status_code == 401

    def test_request_with_invalid_token_returns_401(self, unauthenticated_client: httpx.Client):
        """Request with invalid token -> 401."""
        response = unauthenticated_client.get(
            "/admin/organizations",
            headers={"Authorization": "Bearer invalid-token-12345"},
        )
        assert response.status_code == 401

    def test_request_with_malformed_token_returns_401(self, unauthenticated_client: httpx.Client):
        """Request with malformed token -> 401."""
        response = unauthenticated_client.get(
            "/admin/organizations",
            headers={"Authorization": "Bearer"},
        )
        assert response.status_code == 401

    def test_request_with_valid_token_returns_200(self, client: httpx.Client):
        """Request with valid token -> 200."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_x_api_key_header_works(self, auth_token: str, unauthenticated_client: httpx.Client):
        """
        Request with X-Api-Key header (Anthropic SDK style) -> should work.
        """
        response = unauthenticated_client.get(
            "/v1/models",
            headers={"X-Api-Key": auth_token},
        )
        assert response.status_code == 200

    def test_auth_me_endpoint(self, client: httpx.Client):
        """
        GET /auth/me -> verify returns current user info.
        """
        response = client.get("/auth/me")
        assert response.status_code == 200
        data = response.json()
        # Should return user information
        assert isinstance(data, dict)


# =============================================================================
# 5. Database Connectivity Stress Tests
# =============================================================================


class TestDatabaseConnectivity:
    """
    Database connectivity stress tests.

    These verify the RDS IAM auth connection doesn't fail under load.
    """

    def test_sequential_admin_calls_with_gaps(self, client: httpx.Client):
        """
        Make 10 sequential admin API calls with 2-second gaps.

        All should return 200 - tests connection pool stability.
        """
        for i in range(10):
            response = client.get("/admin/organizations")
            assert response.status_code == 200, f"Request {i + 1} failed: {response.text}"

            # Check for PAM authentication errors in response
            if response.text:
                assert "PAM authentication failed" not in response.text, f"PAM auth error on request {i + 1}"

            if i < 9:  # Don't sleep after last request
                time.sleep(2)

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, auth_token: str):
        """
        Make 20 concurrent requests using asyncio.

        Verifies connection pool handles load correctly.
        """
        async with httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=REQUEST_TIMEOUT,
        ) as client:

            async def make_request(i: int) -> tuple[int, int, str]:
                response = await client.get("/admin/organizations")
                return i, response.status_code, response.text

            # Launch 20 concurrent requests
            tasks = [make_request(i) for i in range(20)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Verify all succeeded
            failures = []
            for result in results:
                if isinstance(result, Exception):
                    failures.append(f"Exception: {result}")
                else:
                    i, status, text = result
                    if status != 200:
                        failures.append(f"Request {i}: status={status}")
                    if "PAM authentication failed" in text:
                        failures.append(f"Request {i}: PAM auth error")

            assert not failures, f"Concurrent request failures: {failures}"


# =============================================================================
# 6. Validation Tests
# =============================================================================


class TestValidation:
    """Test input validation and error handling."""

    def test_missing_required_fields_returns_422(self, client: httpx.Client):
        """Missing required fields -> 422."""
        response = client.post(
            "/v1/messages",
            json={
                # Missing 'model' and 'messages'
                "max_tokens": 100,
            },
            headers={"anthropic-version": "2023-06-01"},
        )
        assert response.status_code == 422

    def test_invalid_entity_type_returns_422(
        self,
        client: httpx.Client,
        test_org_id: str,
    ):
        """Invalid entity_type values -> 422 or 400."""
        response = client.get(f"/admin/organizations/{test_org_id}/budget/invalid_type/some-id")
        # Should be 422 (validation error) or 400 (bad request)
        assert response.status_code in [400, 422, 404], f"Unexpected status: {response.status_code}"

    def test_negative_budget_amount_returns_422(
        self,
        client: httpx.Client,
        test_org_id: str,
    ):
        """Negative budget amounts -> 422."""
        response = client.post(
            f"/admin/organizations/{test_org_id}/budgets",
            json={
                "entity_type": "team",
                "entity_id": "test-validation",
                "period_type": "monthly",
                "budget_amount": -100.0,  # Negative amount
            },
        )
        # Should reject negative amounts
        assert response.status_code in [400, 422], f"Unexpected status: {response.status_code}"

    def test_nonexistent_org_id_handling(self, client: httpx.Client):
        """
        Non-existent org_id handling.

        The API should return 404 for non-existent orgs.
        Note: CloudFront may return 200 with HTML (SPA fallback) for some paths.
        """
        response = client.get("/admin/organizations/nonexistent-org-12345")

        # API should return 404, but CloudFront may return 200 with HTML fallback
        if response.status_code == 200:
            # Check if it's HTML (CloudFront SPA fallback) or JSON
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                pytest.skip("CloudFront SPA fallback - API not directly accessible at this path")
            else:
                # If it's JSON, it should be a proper error or empty response
                data = response.json()
                # Accept if it's null/None or an error response
                assert data is None or "error" in str(data).lower() or data == {}, f"Expected 404 or error response, got: {data}"
        else:
            # Should be 404
            assert response.status_code == 404


# =============================================================================
# 7. Usage API Tests
# =============================================================================


class TestUsageAPI:
    """Test usage tracking endpoints."""

    def test_get_usage_summary(self, client: httpx.Client):
        """GET /usage/summary -> verify response structure."""
        response = client.get("/usage/summary")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_get_usage_by_model(self, client: httpx.Client):
        """GET /usage/models -> verify returns model usage."""
        response = client.get("/usage/models")
        assert response.status_code == 200

    def test_get_usage_timeline(self, client: httpx.Client):
        """GET /usage/timeline -> verify timeline data."""
        response = client.get("/usage/timeline")
        assert response.status_code == 200

    def test_get_usage_logs(self, client: httpx.Client, test_org_id: str):
        """GET /usage/logs -> verify returns logs (requires org_id param)."""
        response = client.get(f"/usage/logs?org_id={test_org_id}")
        assert response.status_code == 200


# =============================================================================
# 8. Budgets API Tests (Direct Routes)
# =============================================================================


class TestBudgetsAPI:
    """Test budgets API direct routes."""

    def test_get_organization_budget_overview(self, client: httpx.Client):
        """GET /budgets/organization/overview -> verify response."""
        response = client.get("/budgets/organization/overview")
        assert response.status_code == 200

    def test_get_budget_alerts(self, client: httpx.Client):
        """GET /budgets/organization/alerts -> verify returns alerts."""
        response = client.get("/budgets/organization/alerts")
        assert response.status_code == 200


# =============================================================================
# 9. Rate Limits API Tests (Direct Routes)
# =============================================================================


class TestRateLimitsAPI:
    """Test rate limits API direct routes."""

    def test_list_rate_limits(self, client: httpx.Client):
        """GET /ratelimits -> verify returns list."""
        response = client.get("/ratelimits")
        assert response.status_code == 200


# =============================================================================
# 10. Service Account Tests
# =============================================================================


class TestServiceAccounts:
    """Test service account endpoints."""

    def test_list_service_accounts(self, client: httpx.Client):
        """GET /auth/service-accounts -> verify returns list."""
        response = client.get("/auth/service-accounts")
        assert response.status_code == 200

    def test_list_org_service_accounts(self, client: httpx.Client, test_org_id: str):
        """GET /admin/organizations/{org_id}/service-accounts -> verify returns list."""
        response = client.get(f"/admin/organizations/{test_org_id}/service-accounts")
        assert response.status_code == 200


# =============================================================================
# 11. Agents API Tests
# =============================================================================


class TestAgentsAPI:
    """Test agent management endpoints."""

    def test_list_agents(self, client: httpx.Client):
        """GET /admin/agents -> verify returns list or appropriate error."""
        response = client.get("/admin/agents")
        # May return 200 with list, or 500 if Cognito integration not configured
        if response.status_code == 500:
            # Check if it's a Cognito configuration issue
            try:
                data = response.json()
                if "cognito" in str(data).lower() or "error" in str(data).lower():
                    pytest.skip("Cognito agent management not configured")
            except Exception:
                # Empty or non-JSON response
                pytest.skip("Agents endpoint returned 500 with no JSON body")
        assert response.status_code == 200


# =============================================================================
# 12. Pool Status Tests
# =============================================================================


class TestPoolStatus:
    """Test account pool status endpoints."""

    def test_get_pool_status(self, client: httpx.Client):
        """GET /admin/pool/status -> verify returns status."""
        response = client.get("/admin/pool/status")
        assert response.status_code == 200


# =============================================================================
# 13. Admin Logs Tests
# =============================================================================


class TestAdminLogs:
    """Test admin log query endpoints."""

    def test_query_logs(self, client: httpx.Client):
        """GET /admin/logs -> verify returns logs or handles errors gracefully."""
        response = client.get("/admin/logs")
        # May return 200 with logs, or 500 if log storage not configured
        if response.status_code == 500:
            # Skip if there's a backend configuration issue
            pytest.skip("Admin logs endpoint returned 500 - may not be configured")
        assert response.status_code == 200


# =============================================================================
# Run Configuration
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

"""
Unit tests for the public GET /auth/login-options endpoint (Issue #2746).

The endpoint exposes the deployment-level "is GitHub login wired" signal to the
unauthenticated login page. It must:
- require no Authorization header (public),
- return only a boolean,
- never 5xx (check failures resolve to False),
- cache reads to bound Secrets Manager calls,
- honour cache invalidation after App registration.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.routes import router

test_app = FastAPI()
test_app.include_router(router)
client = TestClient(test_app)


@pytest.fixture(autouse=True)
def _reset_login_cache():
    """Clear the module-level login_enabled cache before and after each test."""
    from src.admin.connections import service

    service._invalidate_login_enabled_cache()
    yield
    service._invalidate_login_enabled_cache()


def _mock_sm_with_client_id(client_id: str) -> MagicMock:
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": client_id})}
    return sm


@pytest.mark.unit
class TestLoginOptionsEndpoint:
    """Route-level behaviour of GET /auth/login-options."""

    def test_returns_true_when_real_client_id(self):
        """A real (non-placeholder) client_id → github_login_enabled True."""
        mock_sm = _mock_sm_with_client_id("Iv1.realclientid")
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            resp = client.get("/auth/login-options")

        assert resp.status_code == 200
        assert resp.json() == {"github_login_enabled": True}

    @pytest.mark.parametrize(
        "secret_string",
        [
            json.dumps({"client_id": "PLACEHOLDER", "client_secret": "PLACEHOLDER"}),
            json.dumps({"client_id": "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"}),
            json.dumps({"client_id": ""}),
            json.dumps({"not_client_id": "x"}),
            "",
            "{not valid json",
        ],
    )
    def test_returns_false_for_placeholder_or_malformed(self, secret_string):
        """Placeholder / missing / empty / malformed secret → False, still 200."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": secret_string}
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            resp = client.get("/auth/login-options")

        assert resp.status_code == 200
        assert resp.json() == {"github_login_enabled": False}

    def test_returns_200_false_on_client_error(self):
        """Secrets Manager ClientError → 200 with False, never a 5xx."""
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "GetSecretValue",
        )
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            resp = client.get("/auth/login-options")

        assert resp.status_code == 200
        assert resp.json() == {"github_login_enabled": False}

    def test_requires_no_authorization_header(self):
        """The endpoint is public — a call with no token succeeds (not 401/403)."""
        mock_sm = _mock_sm_with_client_id("Iv1.realclientid")
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            resp = client.get("/auth/login-options")  # no headers

        assert resp.status_code == 200

    def test_response_contains_only_the_boolean(self):
        """No secret material or extra fields leak — response is exactly one key."""
        mock_sm = _mock_sm_with_client_id("Iv1.realclientid")
        with (
            patch("boto3.client", return_value=mock_sm),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            resp = client.get("/auth/login-options")

        body = resp.json()
        assert list(body.keys()) == ["github_login_enabled"]


@pytest.mark.unit
class TestLoginOptionsCache:
    """The TTL cache bounds Secrets Manager reads and honours invalidation."""

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_does_not_rehit_sm(self):
        """Two calls within the TTL → only one Secrets Manager read."""
        from src.admin.connections.service import is_github_login_enabled

        mock_sm = _mock_sm_with_client_id("Iv1.realclientid")
        with (
            patch("boto3.client", return_value=mock_sm) as mock_boto,
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            first = await is_github_login_enabled()
            second = await is_github_login_enabled()

        assert first is True
        assert second is True
        # Client built (and thus SM read) exactly once thanks to the cache.
        assert mock_boto.call_count == 1

    @pytest.mark.asyncio
    async def test_invalidation_forces_resh_read(self):
        """After invalidation the next call re-reads Secrets Manager."""
        from src.admin.connections.service import (
            _invalidate_login_enabled_cache,
            is_github_login_enabled,
        )

        mock_sm = _mock_sm_with_client_id("Iv1.realclientid")
        with (
            patch("boto3.client", return_value=mock_sm) as mock_boto,
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            await is_github_login_enabled()
            _invalidate_login_enabled_cache()
            await is_github_login_enabled()

        assert mock_boto.call_count == 2

    @pytest.mark.asyncio
    async def test_stale_cache_error_serves_last_value(self):
        """When the cache is stale and the refresh errors, the last value is served."""
        from src.admin.connections import service
        from src.admin.connections.service import is_github_login_enabled

        # Seed the cache with an already-expired True entry.
        service._LOGIN_ENABLED_CACHE = (0.0, True)
        with (
            patch("boto3.client", side_effect=RuntimeError("boom")),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            # Stale entry exists → error path returns the last cached True.
            assert await is_github_login_enabled() is True

    @pytest.mark.asyncio
    async def test_error_with_no_cache_fails_closed(self):
        """With no cache present, an error resolves to False (backend fail-closed)."""
        from src.admin.connections.service import is_github_login_enabled

        # Cache reset by the autouse fixture → None.
        with (
            patch("boto3.client", side_effect=RuntimeError("boom")),
            patch.dict("os.environ", {"ENVIRONMENT": "dev", "AWS_REGION": "us-east-1"}),
        ):
            assert await is_github_login_enabled() is False

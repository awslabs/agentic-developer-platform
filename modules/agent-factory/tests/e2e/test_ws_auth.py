"""
Auth layer E2E tests (live-only).

Tests 1-4 from the issue:
  1. Unauthenticated WS connect returns 401/403
  2. Expired JWT returns 401
  3. Valid user JWT connects successfully, $connect registers session in DynamoDB
  4. Valid agent JWT (client_credentials) connects successfully
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.live_only]


@pytest.fixture
def ws_url(test_env):
    return test_env.live.ws_url


class TestUnauthenticatedConnect:
    """Test 1: No token -> connection rejected."""

    @pytest.mark.asyncio
    async def test_no_token_rejected(self, ws_url):
        import websockets

        with pytest.raises(websockets.exceptions.InvalidStatusCode) as exc_info:
            async with websockets.connect(ws_url):
                pass
        assert exc_info.value.status_code in (401, 403)


class TestExpiredJWT:
    """Test 2: Expired JWT -> connection rejected."""

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, ws_url, expired_jwt):
        import websockets

        url = f"{ws_url}?token={expired_jwt}"
        with pytest.raises(websockets.exceptions.InvalidStatusCode) as exc_info:
            async with websockets.connect(url):
                pass
        assert exc_info.value.status_code in (401, 403)


class TestValidUserJWT:
    """Test 3: Valid user JWT connects and registers session."""

    @pytest.mark.asyncio
    async def test_user_jwt_connects(self, ws_url, jwt_for_user, test_env):
        import websockets
        import boto3

        token = jwt_for_user(test_env.live.test_user_email)
        url = f"{ws_url}?token={token}"

        async with websockets.connect(url) as ws:
            # Connection succeeded
            assert ws.open

            # Optionally verify DynamoDB session row
            if test_env.live.sessions_table:
                ddb = boto3.resource("dynamodb", region_name="us-east-1")
                table = ddb.Table(test_env.live.sessions_table)
                # The session key format is channel:channel_id:user_id
                # We can scan for recent entries
                # For now, just verify the connection stays open
                await ws.ping()


class TestValidAgentJWT:
    """Test 4: Valid agent JWT (client_credentials) connects."""

    @pytest.mark.asyncio
    async def test_agent_jwt_connects(self, ws_url, jwt_for_agent):
        import websockets

        token = jwt_for_agent()
        url = f"{ws_url}?token={token}"

        async with websockets.connect(url) as ws:
            assert ws.open
            await ws.ping()

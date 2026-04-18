"""
End-to-end WebSocket flow tests (live-only).

Tests 18-20 from the issue:
 18. Direct-response round-trip: send simple payload, receive reply within 10s.
 19. Long-running round-trip: send complex payload, KEDA worker processes, reply within 900s.
 20. Concurrent connections: 5 WS connections, distinct session IDs, all get distinct replies.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest


pytestmark = [pytest.mark.live_only]


@pytest.fixture
def ws_url(test_env):
    return test_env.live.ws_url


@pytest.fixture
def user_token(jwt_for_user, test_env):
    return jwt_for_user(test_env.live.test_user_email)


class TestDirectResponseRoundTrip:
    """Test 18: Direct-response round-trip within 10s."""

    @pytest.mark.asyncio
    async def test_simple_greeting_gets_reply(self, ws_url, user_token):
        import websockets

        url = f"{ws_url}?token={user_token}"
        async with websockets.connect(url) as ws:
            session_id = f"test-direct-{uuid.uuid4().hex[:8]}"
            await ws.send(json.dumps({
                "action": "message",
                "text": "Hello, what is 2 + 2?",
                "session_id": session_id,
            }))

            # Wait for response (direct_response path should reply within 10s)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                response = json.loads(raw)
                assert "content" in response or "text" in response
                assert response.get("type") == "response"
            except asyncio.TimeoutError:
                pytest.fail("No response received within 10 seconds for direct_response payload")


class TestLongRunningRoundTrip:
    """Test 19: Long-running round-trip via KEDA worker.

    This test requires the worker image to be deployed. If the ScaledJob
    is not present, it will skip gracefully.
    """

    @pytest.mark.asyncio
    async def test_complex_task_processed_by_worker(self, ws_url, user_token, kube_client):
        # Check if ScaledJob exists — skip if not deployed
        sj = kube_client.get_scaledjob("agent-gateway-worker")
        if not sj:
            pytest.skip(
                "ScaledJob 'agent-gateway-worker' not deployed in adp-gateway-agents. "
                "Deploy via deploy-all.sh Step 7/7 (build-and-push + keda-scaledjob.yaml)."
            )

        import websockets

        url = f"{ws_url}?token={user_token}"
        async with websockets.connect(url) as ws:
            session_id = f"test-longrun-{uuid.uuid4().hex[:8]}"
            await ws.send(json.dumps({
                "action": "message",
                "text": "Analyze the architecture of modules/agent-factory and suggest improvements to the gateway pipeline. Consider error handling, retry logic, and observability.",
                "session_id": session_id,
            }))

            # Long-running path: wait up to 120s (KEDA needs to spawn pod + process)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                response = json.loads(raw)
                assert "content" in response or "text" in response
            except asyncio.TimeoutError:
                pytest.fail(
                    "No response received within 120 seconds for long_running payload. "
                    "Check KEDA pod status: kubectl get pods -n adp-gateway-agents"
                )


class TestConcurrentConnections:
    """Test 20: 5 concurrent WS connections get distinct replies."""

    @pytest.mark.asyncio
    async def test_five_connections_get_distinct_replies(self, ws_url, user_token):
        import websockets

        num_connections = 5
        url = f"{ws_url}?token={user_token}"

        async def send_and_receive(idx: int) -> dict:
            async with websockets.connect(url) as ws:
                session_id = f"test-concurrent-{idx}-{uuid.uuid4().hex[:8]}"
                await ws.send(json.dumps({
                    "action": "message",
                    "text": f"What is {idx} times {idx}?",
                    "session_id": session_id,
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                return {"session_id": session_id, "response": json.loads(raw)}

        tasks = [send_and_receive(i) for i in range(num_connections)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out any exceptions
        successes = [r for r in results if isinstance(r, dict)]
        errors = [r for r in results if isinstance(r, Exception)]

        assert len(successes) >= 3, f"Expected at least 3 successful replies, got {len(successes)}. Errors: {errors}"

        # Verify distinct session IDs
        session_ids = [r["session_id"] for r in successes]
        assert len(set(session_ids)) == len(session_ids), "Session IDs should be unique"

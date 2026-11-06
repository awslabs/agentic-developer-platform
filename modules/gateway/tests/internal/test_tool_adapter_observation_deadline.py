"""Observation must not block unrelated requests while service signing stalls."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.credentials import Credentials
from fastapi import HTTPException

from src.internal import tool_adapter_observation as observation

RELEASE = {
    "contract_version": "1.0",
    "source_revision": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "group": "atlassian-foundation",
    "group_generation": 1,
    "backend_generation": 1,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["kms_sign", "credential_resolution"])
async def test_slow_signing_or_credentials_preserve_event_loop_and_refuse_expired_attempt(monkeypatch, phase):
    started = threading.Event()
    release = threading.Event()
    post = AsyncMock()

    class KMS:
        def sign(self, **kwargs):
            if phase == "kms_sign":
                started.set()
                release.wait(timeout=3)
            return {"Signature": b"fixture-signature"}

    def client(service, *, config):
        assert service == "kms"
        assert config.retries["total_max_attempts"] == 1
        assert 0 < config.connect_timeout <= 2
        assert 0 < config.read_timeout <= 5
        return KMS()

    def credentials():
        if phase == "credential_resolution":
            started.set()
            release.wait(timeout=3)
        return None

    monkeypatch.setenv("ADP_TOOL_ADAPTER_OBSERVE_URL", "https://adapter.example.test/internal/v1/tool-adapters/observe")
    monkeypatch.setenv(
        "ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
    )
    monkeypatch.setenv("ADP_TOOL_ADAPTER_CORE_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-core")
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(RELEASE))
    monkeypatch.setattr(observation.boto3, "client", client)
    monkeypatch.setattr(observation.boto3, "Session", lambda: SimpleNamespace(get_credentials=credentials))
    monkeypatch.setattr(observation.httpx.AsyncClient, "post", post)
    attempt = SimpleNamespace(
        state_digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(milliseconds=900),
        connection_snapshot={"config_revision": "b" * 64},
    )
    began = time.monotonic()
    task = asyncio.create_task(observation.observe_connection(attempt=attempt, user_id="alice", tenant_id="tenant"))
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        await asyncio.wait_for(asyncio.sleep(0.02), timeout=0.15)
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(task, timeout=2)
        assert refused.value.status_code == 503
        assert refused.value.detail == {"error": "adapter_unavailable"}
        assert time.monotonic() - began < 2
        post.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_adapter_reply_cannot_extend_original_observation_deadline(monkeypatch):
    class KMS:
        def sign(self, **kwargs):
            return {"Signature": b"fixture-signature"}

    async def delayed_reply(*args, **kwargs):
        await asyncio.sleep(3)
        raise AssertionError("expired operation must not receive an adapter reply")

    monkeypatch.setenv("ADP_TOOL_ADAPTER_OBSERVE_URL", "https://adapter.example.test/internal/v1/tool-adapters/observe")
    monkeypatch.setenv(
        "ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
    )
    monkeypatch.setenv("ADP_TOOL_ADAPTER_CORE_ROLE_ARN", "arn:aws:iam::123456789012:role/adp-core")
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(RELEASE))
    monkeypatch.setattr(observation.boto3, "client", lambda service, **kwargs: KMS())
    monkeypatch.setattr(
        observation.boto3,
        "Session",
        lambda: SimpleNamespace(get_credentials=lambda: Credentials("test", "test")),
    )
    monkeypatch.setattr(observation, "get_settings", lambda: SimpleNamespace(aws_region="us-east-1"))
    post = AsyncMock(side_effect=delayed_reply)
    monkeypatch.setattr(observation.httpx.AsyncClient, "post", post)
    attempt = SimpleNamespace(
        state_digest="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(milliseconds=900),
        connection_snapshot={"config_revision": "b" * 64},
    )
    began = time.monotonic()
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(observation.observe_connection(attempt=attempt, user_id="alice", tenant_id="tenant"), 2)
    assert refused.value.status_code == 503
    assert refused.value.detail == {"error": "adapter_unavailable"}
    assert time.monotonic() - began < 2
    post.assert_awaited_once()

"""
E2E-specific conftest — helpers for memory, artifact, and identity assertions.

Complements the parent conftest.py fixtures (ws_client_async, fresh_jwt, cleanup, etc.).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Callable

import boto3
import pytest


# ---------------------------------------------------------------------------
# Token introspection (decode without verification — fine for assertions)
# ---------------------------------------------------------------------------


def user_id_from_token(token: str) -> str:
    """Extract Cognito `sub` from an access token (no signature verification)."""
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("Malformed JWT — expected 3 parts")
    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    sub = payload.get("sub", "")
    if not sub:
        raise ValueError("Token has no 'sub' claim")
    return sub


@pytest.fixture
def get_user_id():
    """Fixture returning user_id_from_token helper."""
    return user_id_from_token


# ---------------------------------------------------------------------------
# Polling helper — exponential backoff
# ---------------------------------------------------------------------------


async def wait_for(
    predicate: Callable[[], Any],
    timeout: float = 30.0,
    interval: float = 2.0,
    max_interval: float = 10.0,
    description: str = "condition",
) -> Any:
    """Poll a predicate until it returns a truthy value or timeout is reached.

    Uses exponential backoff starting at `interval`.
    Raises TimeoutError if the predicate never returns truthy.
    """
    deadline = time.monotonic() + timeout
    current_interval = interval
    while True:
        result = predicate()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {description} after {timeout}s")
        await asyncio.sleep(min(current_interval, remaining))
        current_interval = min(current_interval * 1.5, max_interval)


@pytest.fixture
def poll():
    """Fixture returning the wait_for async polling helper."""
    return wait_for


# ---------------------------------------------------------------------------
# Memory cleanup helper — scan all rows for a scope and register for cleanup
# ---------------------------------------------------------------------------


def scan_memory_rows_for_cleanup(
    scope_type: str, scope_value: str, cleanup_tracker
) -> list[dict]:
    """Scan memory table for scope, register all rows for cleanup, return them."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.Table("adp-dev-agent-memory")
    pk = f"scope#{scope_type}#{scope_value}"
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": pk, ":prefix": "mem#"},
    )
    items = resp.get("Items", [])
    for item in items:
        cleanup_tracker.track_memory(item["PK"], item["SK"])
    return items


def scan_artifacts_for_cleanup(session_id: str, cleanup_tracker) -> list[dict]:
    """Scan artifact catalog for session, register all rows for cleanup, return them."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.Table("adp-dev-chat-artifacts")
    pk = f"session#{session_id}"
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": pk, ":prefix": "art#"},
    )
    items = resp.get("Items", [])
    for item in items:
        cleanup_tracker.track_artifact(item["PK"], item["SK"])
        # Also track S3 objects if s3Key is present
        s3_key = item.get("s3Key")
        if s3_key:
            cleanup_tracker.track_s3(os.environ.get("ARTIFACTS_BUCKET", "adp-dev-chat-artifacts"), s3_key)
    return items


# ---------------------------------------------------------------------------
# Session ID factory
# ---------------------------------------------------------------------------


@pytest.fixture
def make_session_id():
    """Return a factory that creates unique session IDs with a test prefix."""
    import uuid

    def _make(prefix: str = "e2e") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    return _make

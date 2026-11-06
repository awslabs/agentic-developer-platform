from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import jwt
import pytest
from fastapi import HTTPException

from src.auth import native_launch_revocation as sessions


@pytest.fixture
def offline_sessions(monkeypatch):
    store = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(
        sessions,
        "get_ratelimit_config",
        lambda: SimpleNamespace(
            backend_type="redis",
            redis_url="redis://offline.invalid",
        ),
    )
    monkeypatch.setattr(sessions, "_redis", lambda url: store)
    return store


@pytest.fixture
def fixed_clock(monkeypatch):
    instant = datetime.now(UTC).replace(microsecond=0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(sessions, "datetime", FixedDateTime)
    return instant


def access_token(expiry, identity="member"):
    return jwt.encode({"sub": identity, "exp": expiry}, "offline-test-key-not-for-production-0123", algorithm="HS256")


@pytest.mark.asyncio
async def test_logout_revokes_base_and_tenant_lease_without_revoking_new_token(offline_sessions, fixed_clock):
    original = access_token(fixed_clock + timedelta(minutes=30))
    refreshed = access_token(fixed_clock + timedelta(hours=1))
    assert await sessions.require_native_launch_session("Bearer " + original) is None
    await sessions.revoke_native_launch("Bearer adpctx1~signed-lease~" + original, fixed_clock + timedelta(minutes=5))
    with pytest.raises(HTTPException) as denied:
        await sessions.require_native_launch_session("Bearer " + original)
    assert denied.value.status_code == 401
    with pytest.raises(HTTPException):
        await sessions.require_native_launch_session("Bearer adpctx1~different-lease~" + original)
    assert await sessions.require_native_launch_session("Bearer " + refreshed) is None
    assert 300 < await offline_sessions.ttl(sessions._key("Bearer " + original)) <= 1800
    await offline_sessions.aclose()


@pytest.mark.asyncio
async def test_revocation_store_failure_fails_closed(monkeypatch, fixed_clock):
    original = access_token(fixed_clock + timedelta(minutes=5))
    store = SimpleNamespace(get=AsyncMock(side_effect=OSError("offline")), set=AsyncMock(side_effect=OSError("offline")))
    monkeypatch.setattr(
        sessions,
        "get_ratelimit_config",
        lambda: SimpleNamespace(
            backend_type="redis",
            redis_url="redis://offline.invalid",
        ),
    )
    monkeypatch.setattr(sessions, "_redis", lambda url: store)
    with pytest.raises(HTTPException) as denied:
        await sessions.require_native_launch_session("Bearer " + original)
    assert denied.value.status_code == 503
    with pytest.raises(HTTPException) as denied:
        await sessions.revoke_native_launch("Bearer " + original, fixed_clock + timedelta(minutes=5))
    assert denied.value.status_code == 503


@pytest.mark.asyncio
async def test_expired_session_is_not_persisted(offline_sessions, fixed_clock):
    original = access_token(fixed_clock + timedelta(minutes=5))
    with pytest.raises(HTTPException) as denied:
        await sessions.revoke_native_launch("Bearer " + original, fixed_clock - timedelta(seconds=1))
    assert denied.value.status_code == 401
    assert await offline_sessions.get(sessions._key("Bearer " + original)) is None
    await offline_sessions.aclose()

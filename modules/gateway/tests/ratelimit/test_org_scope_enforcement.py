"""Author limits through the admin API contract, then exercise the real limiter (#4952)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.admin.schemas import RateLimitCreateRequest
from src.admin.service import AdminService
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.models import EntityType
from src.ratelimit.service import RateLimitService
from src.shared.models.usage import RateLimitConfig as StoredLimit
from src.shared.schemas.auth import TokenContext


@pytest.mark.parametrize("entity_type,entity_id", [("org", "acme"), ("department", "engineering"), ("team", "platform"), ("user", "alice")])
async def test_authored_limit_throttles_its_member(db_session, entity_type, entity_id):
    """Reverting the enum to 'organization' makes the org case allow excess requests."""
    await AdminService(db_session).create_ratelimit("acme", RateLimitCreateRequest(entity_type=entity_type, entity_id=entity_id, rpm=2))
    limiter = RateLimitService(config=RateLimitConfig(backend_type="memory", default_rpm=10000, burst_multiplier=1, refill_buffer_seconds=60))

    @asynccontextmanager
    async def session():
        yield db_session

    context = TokenContext(
        org_id="acme",
        department_id="engineering",
        team_id="platform",
        user_id="alice",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with patch.object(limiter, "_get_session", session):
        assert (await limiter.check_rate_limit(context)).allowed
        assert (await limiter.consume_rate_limit(context)).allowed
        assert (await limiter.consume_rate_limit(context)).allowed
        result = await limiter.check_rate_limit(context)
        assert not result.allowed
        assert result.limit_type == "rpm"
        assert result.limit == 2
        # A matching-looking entity in another partition never shares this quota.
        other = context.model_copy(update={"org_id": "other", "attributed_org_id": "other"})
        assert (await limiter.check_rate_limit(other)).allowed


async def test_legacy_spelling_and_live_reload_use_the_same_key(db_session):
    """A rolling upgrade can read old rows, and a changed config takes effect on reload."""
    row = StoredLimit(org_id="acme", entity_type="organization", entity_id="acme", rpm=2)
    db_session.add(row)
    await db_session.commit()
    limiter = RateLimitService()

    @asynccontextmanager
    async def session():
        yield db_session

    with patch.object(limiter, "_get_session", session):
        await limiter._load_from_db()
        assert limiter._get_limits_for_entity(EntityType.ORGANIZATION, "acme", "acme")["rpm"] == 2
        row.entity_type = "org"
        row.rpm = 7
        await db_session.commit()
        limiter._last_db_load = 0
        await limiter._load_from_db()
        assert limiter._get_limits_for_entity(EntityType.ORGANIZATION, "acme", "acme")["rpm"] == 7
        assert len(limiter._rate_limits) == 1


def test_legacy_api_spelling_is_accepted_but_serializes_canonically():
    assert EntityType("organization") is EntityType.ORGANIZATION
    assert EntityType.ORGANIZATION.value == "org"


@pytest.mark.parametrize("legacy_first", [True, False])
async def test_duplicate_spellings_never_weaken_an_org_limit(db_session, legacy_first):
    rows = [
        StoredLimit(org_id="acme", entity_type="organization", entity_id="acme", rpm=2, tpm=0),
        StoredLimit(org_id="acme", entity_type="org", entity_id="acme", rpm=10, tpm=100),
    ]
    db_session.add_all(rows if legacy_first else list(reversed(rows)))
    await db_session.commit()
    limiter = RateLimitService()

    @asynccontextmanager
    async def session():
        yield db_session

    with patch.object(limiter, "_get_session", session):
        await limiter._load_from_db()
    limits = limiter._get_limits_for_entity(EntityType.ORGANIZATION, "acme", "acme")
    assert limits["rpm"] == 2
    assert limits["tpm"] == 100

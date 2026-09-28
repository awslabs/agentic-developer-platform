"""Tests for ElastiCache IAM authentication (Issue #4342).

These verify:
1. The auth token is a SigV4-presigned ``Action=connect`` request for the right user
2. Tokens are cached below expiry, and re-minted once stale (the refresh path —
   a one-time startup token would be a ~15-minute timed outage)
3. The credential provider returns ``(username, token)`` and re-mints per call,
   which is what makes refresh automatic across pool reconnects
4. The factory attaches the provider when IAM auth is on, and stays passwordless
   when it is off (local dev / docker-compose / tests)
5. Every gateway Redis call site goes through the factory rather than
   constructing an unauthenticated client of its own
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared import redis_client as rc


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Token cache is module-global; isolate every test from its neighbours."""
    rc.reset_token_cache()
    yield
    rc.reset_token_cache()


@contextmanager
def _spy_signer():
    """Count real presign calls, so 'was it refreshed?' is asserted on a fresh
    signing rather than on token strings (which are wall-clock derived)."""
    from botocore.signers import RequestSigner

    real = RequestSigner.generate_presigned_url
    with patch.object(RequestSigner, "generate_presigned_url", autospec=True, side_effect=real) as spy:
        yield spy


def _settings(**overrides):
    base = {
        "redis_iam_auth": True,
        "redis_username": "bedrockgw-dev-iam-user",
        "redis_cache_name": "bedrockgw-dev-redis-cluster",
        "aws_region": "us-east-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestGetElastiCacheAuthToken:
    """Token generation via botocore's SigV4 presigner."""

    def test_token_is_presigned_connect_request_for_the_user(self):
        token = rc.get_elasticache_auth_token(
            cache_name="bedrockgw-dev-redis-cluster",
            username="bedrockgw-dev-iam-user",
            region="us-east-1",
        )

        # Scheme must be stripped — ElastiCache rejects a token carrying https://.
        assert not token.startswith("https://")
        assert token.startswith("bedrockgw-dev-redis-cluster/?")
        assert "Action=connect" in token
        assert "User=bedrockgw-dev-iam-user" in token
        # Proves it is genuinely SigV4-signed for the elasticache service.
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in token
        assert "X-Amz-Signature=" in token
        assert "elasticache%2Faws4_request" in token

    def test_token_signed_for_requested_region(self):
        token = rc.get_elasticache_auth_token(
            cache_name="bedrockgw-prod-redis-cluster",
            username="bedrockgw-prod-iam-user",
            region="eu-west-1",
        )
        assert "eu-west-1%2Felasticache" in token

    def test_token_expiry_is_capped_at_fifteen_minutes(self):
        """ElastiCache rejects a longer-lived token."""
        assert rc._TOKEN_EXPIRY_SECONDS == 900
        token = rc.get_elasticache_auth_token("rg", "user", "us-east-1")
        assert "X-Amz-Expires=900" in token


class TestTokenCaching:
    """Caching below expiry, with a re-mint once stale."""

    def test_token_cached_within_ttl(self):
        """Within the TTL, no re-signing: connection churn must not re-sign per
        handshake."""
        with _spy_signer() as sign:
            with patch.object(rc.time, "monotonic", return_value=1000.0):
                first = rc.get_elasticache_auth_token("rg", "user", "us-east-1")
                second = rc.get_elasticache_auth_token("rg", "user", "us-east-1")

        assert sign.call_count == 1
        assert first == second

    def test_stale_token_is_refreshed_not_reused(self):
        """The core of the issue's 'token refresh is mandatory' requirement.

        Asserts a *fresh signing call*, not string inequality: SigV4 signatures
        are derived from wall-clock time, so two mints inside the same second are
        legitimately byte-identical. Comparing strings would make this test pass
        or fail on timing rather than on whether a refresh occurred.
        """
        with _spy_signer() as sign:
            with patch.object(rc.time, "monotonic", return_value=1000.0):
                rc.get_elasticache_auth_token("rg", "user", "us-east-1")
            assert sign.call_count == 1

            # Past the cache TTL — must re-sign rather than serve the stale token.
            with patch.object(rc.time, "monotonic", return_value=1000.0 + rc._TOKEN_CACHE_TTL + 1):
                refreshed = rc.get_elasticache_auth_token("rg", "user", "us-east-1")

            assert sign.call_count == 2, "expired token was served instead of re-minted"

        assert "X-Amz-Signature=" in refreshed

    def test_cache_ttl_leaves_margin_before_expiry(self):
        """A cached token must never be served close to its own expiry."""
        assert rc._TOKEN_CACHE_TTL < rc._TOKEN_EXPIRY_SECONDS

    def test_distinct_users_and_caches_do_not_share_a_token(self):
        with patch.object(rc.time, "monotonic", return_value=1000.0):
            a = rc.get_elasticache_auth_token("rg-a", "user-a", "us-east-1")
            b = rc.get_elasticache_auth_token("rg-b", "user-a", "us-east-1")
            c = rc.get_elasticache_auth_token("rg-a", "user-b", "us-east-1")

        assert a != b, "different replication groups must sign different tokens"
        assert a != c, "different users must sign different tokens"


class TestCredentialProvider:
    """The provider is what makes refresh automatic on every connection."""

    def test_returns_username_and_token(self):
        provider = rc.ElastiCacheIAMCredentialProvider(
            username="bedrockgw-dev-iam-user",
            cache_name="bedrockgw-dev-redis-cluster",
            region="us-east-1",
        )
        username, token = provider.get_credentials()

        assert username == "bedrockgw-dev-iam-user"
        assert "User=bedrockgw-dev-iam-user" in token

    def test_remints_when_token_goes_stale(self):
        """redis-py calls get_credentials() per connection; a stale token must
        not survive into a new connection."""
        provider = rc.ElastiCacheIAMCredentialProvider("user", "rg", "us-east-1")

        with _spy_signer() as sign:
            with patch.object(rc.time, "monotonic", return_value=500.0):
                provider.get_credentials()
            with patch.object(rc.time, "monotonic", return_value=500.0 + rc._TOKEN_CACHE_TTL + 1):
                provider.get_credentials()

        assert sign.call_count == 2


class TestCreateRedisClient:
    """The single factory all call sites use."""

    def test_attaches_credential_provider_when_iam_auth_on(self):
        with (
            patch.object(rc, "get_settings", return_value=_settings()),
            patch.object(rc.redis, "from_url") as mock_from_url,
        ):
            rc.create_redis_client("rediss://host:6379/0", decode_responses=True)

        _, kwargs = mock_from_url.call_args
        provider = kwargs["credential_provider"]
        assert isinstance(provider, rc.ElastiCacheIAMCredentialProvider)
        # Call-site kwargs must survive untouched.
        assert kwargs["decode_responses"] is True

    def test_provider_uses_configured_username_cache_and_region(self):
        settings = _settings(
            redis_username="custom-user",
            redis_cache_name="custom-rg",
            aws_region="ap-south-1",
        )
        with (
            patch.object(rc, "get_settings", return_value=settings),
            patch.object(rc.redis, "from_url") as mock_from_url,
        ):
            rc.create_redis_client("rediss://host:6379/0")

        provider = mock_from_url.call_args.kwargs["credential_provider"]
        username, token = provider.get_credentials()
        assert username == "custom-user"
        assert "custom-rg/?" in token
        assert "ap-south-1%2Felasticache" in token

    def test_no_credential_when_iam_auth_off(self):
        """Local dev / docker-compose / tests must stay passwordless."""
        with (
            patch.object(rc, "get_settings", return_value=_settings(redis_iam_auth=False)),
            patch.object(rc.redis, "from_url") as mock_from_url,
        ):
            rc.create_redis_client("redis://localhost:6379/0", encoding="utf-8")

        kwargs = mock_from_url.call_args.kwargs
        assert "credential_provider" not in kwargs
        assert kwargs["encoding"] == "utf-8"

    @pytest.mark.parametrize("missing", ["redis_username", "redis_cache_name"])
    def test_raises_named_error_when_iam_config_incomplete(self, missing):
        """The bug this issue fixes was an untraceable 'Authentication required';
        a misconfiguration must name itself instead."""
        with patch.object(rc, "get_settings", return_value=_settings(**{missing: ""})):
            with pytest.raises(RuntimeError, match="BG_REDIS_IAM_AUTH"):
                rc.create_redis_client("rediss://host:6379/0")


class TestAllCallSitesRouteThroughFactory:
    """Regression guard: the defect was per-call-site unauthenticated clients.

    Each test asserts the module calls the shared factory. If someone reintroduces
    a bare ``redis.from_url`` at one of these sites, its Redis access silently
    breaks again under IAM auth — exactly the failure #4342 fixes.
    """

    @pytest.mark.asyncio
    async def test_run_binding_cache(self):
        from src.budget.run_binding import RunBindingResolver

        resolver = RunBindingResolver(table_name="t", aws_region="us-east-1", redis_url="rediss://h:6379/0")
        with patch("src.budget.run_binding.create_redis_client", return_value=MagicMock()) as factory:
            await resolver._get_cache()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_reservation_store(self):
        from src.budget.reservations import ReservationStore

        store = ReservationStore(redis_url="rediss://h:6379/0", ttl_seconds=120)
        with patch("src.budget.reservations.create_redis_client", return_value=MagicMock()) as factory:
            await store._get_client()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_grace_window(self):
        from src.budget.grace_window import GraceWindow

        window = GraceWindow(grace_seconds=60, redis_url="rediss://h:6379/0")
        with patch("src.budget.grace_window.create_redis_client", return_value=MagicMock()) as factory:
            await window._get_client()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_ratelimit_backend(self):
        from src.ratelimit.backends.redis import RedisBackend

        backend = RedisBackend(redis_url="rediss://h:6379/0")
        with patch("src.ratelimit.backends.redis.create_redis_client", return_value=MagicMock()) as factory:
            await backend._get_client()
        factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_admin_health(self):
        from src.admin.health import HealthChecker

        client = AsyncMock()

        with (
            patch("src.shared.config.get_settings", return_value=_settings(redis_url="rediss://h:6379/0")),
            patch("src.shared.redis_client.create_redis_client", return_value=client) as factory,
        ):
            result = await HealthChecker()._check_redis()

        factory.assert_called_once()
        client.ping.assert_awaited_once()
        assert result is not None and result.status == "healthy"

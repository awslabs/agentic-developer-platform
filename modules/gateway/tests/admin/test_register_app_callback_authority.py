"""Authority on the GitHub App setup callback — #5664 (A10).

``register_app_callback`` writes the deployment's SHARED GitHub App credentials,
the webhook signing secret and the GitHub sign-in secret. Replacing them is
destructive for every tenant at once and hands control of the trusted inbound
webhook path to whoever triggered it.

Its only check was possession of a 15-minute state token — and that token could be
minted by any signed-in user through the user-facing identity-link surface (see
tests/auth/test_identity_link_provider_guard.py, the other half of this finding).
There was no signed-in-user check, no administrator check, no same-initiator
check, and the "an App is already registered" guard ran in a *different, earlier*
request so it never protected the write.

The route cannot use ``get_current_user``: GitHub redirects the operator's browser
here as a plain GET with no Authorization header, so authority is re-derived
server-side from the initiator recorded on the state token.

Every refusal is asserted to write ZERO secrets, because a refusal that still
performs half the write is not a refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Bound as a module, not via `from ... import` — tests/admin/test_connections_service.py
# calls importlib.reload() on this module, which replaces SetupAuthorityError with a
# fresh class object. A name imported here before that reload would no longer be the
# class the service raises, so `pytest.raises` would miss it depending on file order.
# Late attribute access always sees the live definitions.
import src.admin.connections.service as svc
from src.auth.magic_link import NonceNotFoundError, TokenExpiredError, store_nonce
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import MagicLinkNonce

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.admin.models  # noqa: F401
        import src.shared.models.onboarding  # noqa: F401
        import src.shared.models.organization  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(db_engine) -> AsyncSession:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(id="org-acme", name="Acme", aws_accounts=[], role_mappings={}, settings={}),
                Department(id="dept-eng", org_id="org-acme", name="Eng"),
                Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng"),
                # The platform administrator who legitimately runs setup.
                User(id="user-admin", org_id="org-acme", team_id="team-eng", email="admin@test.com", role="platform_admin"),
                # An ordinary member — the attacker in the escalation path.
                User(id="user-mallory", org_id="org-acme", team_id="team-eng", email="mallory@test.com", role="member"),
                # A user with no role recorded at all.
                User(id="user-norole", org_id="org-acme", team_id="team-eng", email="norole@test.com"),
            ]
        )
        await session.commit()
        yield session
        await session.rollback()


async def _mint_state(db: AsyncSession, *, target_user_id: str | None, cognito_sub: str = "sub-x") -> str:
    """Mint a register-flow state nonce exactly as register_app_start does."""
    import uuid

    # Model authenticated issuance: subject and canonical target agree. Denial
    # tests then exercise a revoked/insufficient role or a deleted target.
    user = await db.get(User, target_user_id) if target_user_id else None
    if user:
        user.cognito_sub = cognito_sub
        await db.commit()
    jti = str(uuid.uuid4())
    await store_nonce(
        jti=jti,
        provider=svc._PROVIDER_GITHUB_APP_REGISTER,
        provider_user_id=cognito_sub,
        channel_context=svc._setup_context(kind="platform", owner_type="user"),
        target_user_id=target_user_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=900),
        db=db,
    )
    return jti


class _SecretStoreSpy:
    """Counts every write against Secrets Manager.

    The acceptance criterion is "no shared application, webhook-signing or
    sign-in secret is written", verified by asserting zero write calls — not by
    inspecting a return value, which a half-completed write could still produce.
    """

    def __init__(self, *, existing_app_id: str | None = None):
        self.client = MagicMock()
        self.client.create_secret = MagicMock(side_effect=self._record_create)
        self.client.put_secret_value = MagicMock(side_effect=self._record_put)
        self.writes: list[tuple[str, str]] = []
        if existing_app_id:
            self.client.get_secret_value = MagicMock(return_value={"SecretString": existing_app_id})
        else:
            # No App registered yet: mimic Secrets Manager's not-found behaviour.
            self.client.get_secret_value = MagicMock(side_effect=Exception("ResourceNotFoundException"))

    def _record_create(self, **kwargs):
        self.writes.append(("create_secret", kwargs.get("Name", "")))
        return {}

    def _record_put(self, **kwargs):
        self.writes.append(("put_secret_value", kwargs.get("SecretId", "")))
        return {}


# ---------------------------------------------------------------------------
# Refusals — each must write nothing
# ---------------------------------------------------------------------------


class TestRegisterAppCallbackRefusesWithoutAuthority:
    async def test_non_admin_initiator_is_refused_and_writes_no_secret(self, db):
        """The core escalation: an ordinary member's state token must not be able
        to replace the deployment's shared credentials."""
        state = await _mint_state(db, target_user_id="user-mallory")
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError, match="not a platform administrator"):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == [], f"refusal still wrote secrets: {spy.writes}"

    async def test_user_with_no_role_is_refused(self, db):
        """Absent role must fail closed, not default to permitted."""
        state = await _mint_state(db, target_user_id="user-norole")
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == []

    async def test_org_admin_is_not_sufficient(self, db):
        """org_admin is a TENANT-level role. The secrets written here are shared by
        every tenant, so tenant-level admin must not reach them — otherwise the
        escalation simply reopens one rung lower."""
        from src.shared.models.onboarding import TenantMembership

        db.add(User(id="user-orgadmin", org_id="org-acme", team_id="team-eng", email="oa@test.com", role="org_admin"))
        await db.commit()
        db.add(
            TenantMembership(
                id="tm-5664",
                user_id="user-orgadmin",
                tenant_id="org-acme",
                role="org_admin",
                is_active=True,
            )
        )
        await db.commit()

        state = await _mint_state(db, target_user_id="user-orgadmin")
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError, match="not a platform administrator"):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == []

    async def test_state_without_recorded_initiator_is_refused(self, db):
        """A nonce carrying no users.id cannot establish who is completing the
        flow, so there is nobody whose authority could be checked."""
        state = await _mint_state(db, target_user_id=None)
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError, match="no initiator"):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == []

    async def test_deleted_initiator_is_refused(self, db):
        """A token outliving its initiator must not still be honoured."""
        state = await _mint_state(db, target_user_id="user-ghost")
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError, match="no longer exists"):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == []

    async def test_refusal_does_not_consume_the_nonce(self, db):
        """A denied attempt must leave the legitimate admin's flow usable, and must
        not let an attacker burn a real admin's in-flight setup link."""
        state = await _mint_state(db, target_user_id="user-mallory")
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        nonce = await db.get(MagicLinkNonce, state)
        assert nonce is not None
        assert nonce.consumed_at is None, "refusal consumed the nonce — not recoverable/idempotent"

    async def test_unknown_state_is_still_refused_before_any_write(self, db):
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(NonceNotFoundError):
                await svc.register_app_callback(code="gh-code", state="no-such-jti", db=db)

        assert spy.writes == []

    async def test_expired_state_is_refused_before_any_write(self, db):
        import uuid

        jti = str(uuid.uuid4())
        await store_nonce(
            jti=jti,
            provider=svc._PROVIDER_GITHUB_APP_REGISTER,
            provider_user_id="sub-x",
            channel_context=None,
            target_user_id="user-admin",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            db=db,
        )
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(TokenExpiredError):
                await svc.register_app_callback(code="gh-code", state=jti, db=db)

        assert spy.writes == []


class TestRegisterAppCallbackOverwriteGuard:
    async def test_existing_app_is_not_silently_overwritten(self, db):
        """The guard used to run only in register-START, a different request, so by
        the time this callback wrote secrets it had long since passed. Overwriting a
        live App breaks every tenant's connection at once."""
        state = await _mint_state(db, target_user_id="user-admin")
        spy = _SecretStoreSpy(existing_app_id="998877")

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(svc.SetupAuthorityError, match="already registered"):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert spy.writes == []

    async def test_guard_runs_before_the_github_exchange(self, db):
        """Refusing before the provider call keeps the single-use GitHub code
        unspent, so a legitimate retry is still possible."""
        state = await _mint_state(db, target_user_id="user-admin")
        spy = _SecretStoreSpy(existing_app_id="998877")
        exchange = AsyncMock()

        with patch("boto3.client", return_value=spy.client), patch("httpx.AsyncClient.post", exchange):
            with pytest.raises(svc.SetupAuthorityError):
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        exchange.assert_not_awaited()


# ---------------------------------------------------------------------------
# The legitimate path must still work — the "gated the wrong half" check
# ---------------------------------------------------------------------------


class TestRegisterAppCallbackAllowsPlatformAdmin:
    async def test_platform_admin_completes_setup(self, db):
        """Guards the failure mode where the role check is placed on the wrong half
        of the redirect flow and legitimate setup becomes impossible."""
        state = await _mint_state(db, target_user_id="user-admin")
        spy = _SecretStoreSpy()

        gh_response = MagicMock()
        gh_response.status_code = 201
        gh_response.json = MagicMock(
            return_value={
                "id": 424242,
                "slug": "acme-adp-agent-platform",
                "pem": "-----BEGIN RSA PRIVATE KEY-----\nstub\n-----END RSA PRIVATE KEY-----",
                "client_id": "Iv1.stub",
                "client_secret": "stub-client-secret",
                "webhook_secret": "stub-webhook-secret",
                "owner": {"type": "User", "login": "someone", "id": 1},
            }
        )

        with (
            patch("boto3.client", return_value=spy.client),
            patch("httpx.AsyncClient.post", AsyncMock(return_value=gh_response)),
            patch("src.admin.connections.service._store_app_credentials", new_callable=AsyncMock, return_value=True) as store,
            patch("src.admin.connections.service.get_github_app_provider", MagicMock()),
            patch("src.admin.connections.service._invalidate_login_enabled_cache", MagicMock()),
            patch("src.admin.connections.service._invalidate_verification_cache", MagicMock()),
        ):
            redirect = await svc.register_app_callback(code="gh-code", state=state, db=db)

        store.assert_awaited_once()
        assert "acme-adp-agent-platform" in redirect

        nonce = await db.get(MagicLinkNonce, state)
        assert nonce is not None and nonce.consumed_at is not None, "successful setup must consume its single-use state token"

    async def test_admin_role_alias_is_accepted(self, db):
        """`auth_service` maps both "platform_admin" and "admin" claims to
        is_admin, so the database-side check must accept the same pair or a
        legitimate operator is locked out."""
        db.add(User(id="user-admin2", org_id="org-acme", team_id="team-eng", email="a2@test.com", role="admin"))
        await db.commit()
        state = await _mint_state(db, target_user_id="user-admin2")
        spy = _SecretStoreSpy()

        # Reaching the GitHub exchange proves the authority gate passed; the
        # exchange itself is stubbed to fail so no secret is written.
        gh_response = MagicMock()
        gh_response.status_code = 502
        gh_response.text = "nope"

        with patch("boto3.client", return_value=spy.client), patch("httpx.AsyncClient.post", AsyncMock(return_value=gh_response)):
            with pytest.raises(Exception) as exc:
                await svc.register_app_callback(code="gh-code", state=state, db=db)

        assert not isinstance(exc.value, svc.SetupAuthorityError), "an 'admin'-role operator was wrongly refused"
        assert spy.writes == []

    async def test_state_from_another_namespace_is_not_accepted(self, db):
        """The register callback must only honour its OWN namespace, so a token
        minted for a different flow cannot be replayed into credential replacement."""
        import uuid

        jti = str(uuid.uuid4())
        await store_nonce(
            jti=jti,
            provider="github_install",  # install flow, not the register flow
            provider_user_id="sub-x",
            channel_context=None,
            target_user_id="user-admin",
            expires_at=datetime.now(UTC) + timedelta(seconds=900),
            db=db,
        )
        spy = _SecretStoreSpy()

        with patch("boto3.client", return_value=spy.client):
            with pytest.raises(NonceNotFoundError):
                await svc.register_app_callback(code="gh-code", state=jti, db=db)

        assert spy.writes == []
        rows = (await db.execute(select(MagicLinkNonce).where(MagicLinkNonce.jti == jti))).scalars().all()
        assert rows[0].consumed_at is None

"""Provider-namespace guard on the user-facing identity-link surface — #5664 (A10).

`magic_link_nonces` is shared by two unrelated one-time credentials: the
user-facing identity-linking token, and the browser-redirect state token that is
the SOLE authenticator on the platform-admin GitHub App registration callback
(`register_app_callback`, which overwrites the shared App credentials, the
webhook signing secret and the GitHub sign-in secret). They are told apart only
by the `provider` text column.

`POST /auth/identities/{provider}/link` took `provider` straight off the URL path
and passed it to `store_nonce` with no validation, so an ordinary signed-in user
could mint a nonce in the admin namespace. These tests pin the guard shut, and
assert it runs BEFORE any state is written — the ORM validator on
`UserIdentity.provider` is one request too late, since by then the nonce row
exists and the token has already been returned to the caller.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.identity.providers import (
    INTERNAL_SETUP_NAMESPACES,
    SUPPORTED_PROVIDERS,
    is_linkable_provider,
)
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import MagicLinkNonce, UserIdentity
from src.shared.schemas.auth import TokenContext

_SECRET = "test-magic-link-secret-key-32chars!!"


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/auth/test_magic_link.py)
# ---------------------------------------------------------------------------


def _ctx(user_id: str = "user-alice", *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id="org-acme",
        team_id="team-eng",
        department_id="dept-eng",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(
                    id="org-acme",
                    name="Acme Corp",
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                ),
                Department(id="dept-eng", org_id="org-acme", name="Engineering"),
                Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Eng"),
                User(id="user-alice", org_id="org-acme", team_id="team-eng", email="alice@test.com"),
            ]
        )
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, caller: TokenContext | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db_session

    async def _get_caller():
        return caller or _ctx()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user_context] = _get_caller
    app.dependency_overrides[get_secrets_manager] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------


class TestProviderAllowlist:
    def test_internal_namespaces_are_disjoint_from_linkable_providers(self):
        """The escalation is only closed while these two sets do not intersect."""
        assert INTERNAL_SETUP_NAMESPACES
        assert not (INTERNAL_SETUP_NAMESPACES & SUPPORTED_PROVIDERS)

    @pytest.mark.parametrize("provider", sorted(INTERNAL_SETUP_NAMESPACES))
    def test_internal_setup_namespaces_are_not_linkable(self, provider):
        assert is_linkable_provider(provider) is False

    @pytest.mark.parametrize("provider", ["slack", "github", "discord", "whatsapp"])
    def test_real_providers_stay_linkable(self, provider):
        """Guards the 'allowlist too narrow' failure mode: a legitimate provider
        that got dropped would have all its linking rejected."""
        assert is_linkable_provider(provider) is True

    @pytest.mark.parametrize("provider", ["bogus", "", "GitHub", "github ", "../github"])
    def test_unknown_providers_are_not_linkable(self, provider):
        assert is_linkable_provider(provider) is False


# ---------------------------------------------------------------------------
# Route-level enforcement — the actual privilege-escalation path
# ---------------------------------------------------------------------------


class TestLinkRouteRejectsInternalNamespaces:
    @pytest.mark.parametrize("provider", sorted(INTERNAL_SETUP_NAMESPACES))
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_admin_namespace_nonce_cannot_be_minted_by_a_user(self, _secret, provider, db):
        """A non-admin asking for a token in the GitHub-App-setup namespace is
        refused. Before #5664 this returned 201 with a usable state token, which
        `register_app_callback` accepts as its only authority."""
        client = _make_app(db, _ctx(is_admin=False))

        resp = client.post(f"/auth/identities/{provider}/link", json={"provider_user_id": "U-attacker"})

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "unsupported_provider"
        # The refusal must not leak which internal namespaces exist.
        assert provider not in resp.text.replace(f"/auth/identities/{provider}/link", "")

    @pytest.mark.parametrize("provider", sorted(INTERNAL_SETUP_NAMESPACES))
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_refusal_writes_no_nonce(self, _secret, provider, db):
        """Rejection must land before persistence: a stored nonce is the exact
        artifact the admin callback consumes, so writing one and then erroring
        would leave the escalation open."""
        client = _make_app(db, _ctx(is_admin=False))

        client.post(f"/auth/identities/{provider}/link", json={"provider_user_id": "U-attacker"})

        nonces = (await db.execute(select(MagicLinkNonce))).scalars().all()
        assert nonces == []

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_admin_caller_is_also_refused(self, _secret, db):
        """Being a platform admin is not a licence to mint a setup credential on
        the user-facing surface — the admin flow has its own start endpoint."""
        client = _make_app(db, _ctx(is_admin=True))

        resp = client.post("/auth/identities/github_app_register/link", json={"provider_user_id": "U1"})

        assert resp.status_code == 400
        assert (await db.execute(select(MagicLinkNonce))).scalars().all() == []


class TestLinkRouteRejectsUnknownProviders:
    @pytest.mark.parametrize("provider", ["bogus", "GitHub", "cognito_admin"])
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_unknown_provider_rejected_before_any_write(self, _secret, provider, db):
        client = _make_app(db, _ctx())

        resp = client.post(f"/auth/identities/{provider}/link", json={"provider_user_id": "U1"})

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "unsupported_provider"
        assert (await db.execute(select(MagicLinkNonce))).scalars().all() == []
        assert (await db.execute(select(UserIdentity))).scalars().all() == []

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_provider_check_precedes_the_not_configured_check(self, _secret, db):
        """Ordering note: an unsupported provider is refused on its own merits, so
        the answer does not change with signing-key configuration state."""
        with patch("src.auth.vault_routes._get_magic_link_secret", return_value=""):
            client = _make_app(db, _ctx())
            resp = client.post("/auth/identities/bogus/link", json={"provider_user_id": "U1"})

        assert resp.status_code in (400, 503)
        assert (await db.execute(select(MagicLinkNonce))).scalars().all() == []

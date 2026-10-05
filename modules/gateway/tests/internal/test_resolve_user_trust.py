"""Resolving an account is not proving control of it — #5664 (A10), repair 2.

`POST /internal/v1/resolve-user` auto-provisions a shadow identity when an inbound
event arrives from a channel an administrator mapped to a tenant. It used to write
that row as ``admin_manual``, a member of ``PROVEN_METHODS``, which made this
endpoint a proof-manufacturing route: the external account id arrives in the
REQUEST BODY and nobody verifies it, so naming someone else's account id was
enough to have a row that the approval gate, the workspace-access query and the
webhook authority gate all treated as an administrator's verified assertion.

Why the fix is a new label and not a narrower filter
----------------------------------------------------
The write and the trust filter were the same value in the same function, so simply
relabelling the row unproven would have made every SUBSEQUENT resolve miss the
filter — re-provisioning another shadow user and re-issuing another magic link on
every inbound message, forever. That coupling is why the previous slice left the
mislabel in place.

So the two questions are split instead:

    "WHICH platform user is this account?"  → IDENTIFYING_METHODS (routing)
    "Has anyone demonstrated CONTROL?"      → is_proven          (authority)

This module pins both halves, because each is a distinct way to get it wrong:
dropping the routing half is an availability bug (magic-link loop), and dropping
the authority half is the finding itself. It also pins the positive paths — a
genuine ``oauth`` link and a new ``admin_attested`` one must still resolve AND
still mint authority — since a fix that over-refuses would be a platform-wide
dispatch outage rather than a security improvement.

`test_proven_consumers.py` covers the downstream consumers of the label. This file
covers the endpoint that WRITES it, and the 409 ambiguity branch, which had no
test at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.routes import router
from src.shared.database import get_db
from src.shared.identity.verification import (
    CHANNEL_PLACEMENT,
    IDENTIFYING_METHODS,
    PROVEN_METHODS,
    UNPROVEN_METHODS,
    identifies,
    is_proven,
)
from src.shared.models.base import Base
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, UserIdentity

_VALID_CALLER = "test-service-principal"
_SECRET = "test-magic-link-secret-key-32chars!!"


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
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

        def _org(org_id: str, name: str) -> Organization:
            return Organization(
                id=org_id,
                name=name,
                aws_accounts=[],
                role_mappings={},
                settings={},
                github_installation_ids=[],
                cognito_client_ids=[],
            )

        session.add_all(
            [
                _org("org-a", "Org A"),
                _org("org-b", "Org B"),
                ChannelTenantMap(provider="slack", provider_scope_id="T-mapped", org_id="org-a"),
            ]
        )
        await session.commit()
        yield session


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db

    async def _verify(request: Request, x_caller_identity: str | None = Header(default=None)) -> None:
        if x_caller_identity != _VALID_CALLER:
            raise HTTPException(status_code=403, detail={"error": "forbidden"})
        request.state.token_context = SimpleNamespace(
            user_id="iam-agent:ingress",
            auth_source="iam",
            scope="internal",
            org_id="__platform__",
            credential_scopes=["internal:identity:resolve", "internal:identity:link", "internal:cross-tenant"],
        )

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[verify_internal_or_irsa] = _verify
    return TestClient(app, raise_server_exceptions=False)


def _resolve(client: TestClient, **body) -> object:
    return client.post("/internal/v1/resolve-user", json=body, headers={"X-Caller-Identity": _VALID_CALLER})


def _seed(db: AsyncSession, *rows) -> None:
    async def _go():
        db.add_all(rows)
        await db.commit()

    asyncio.get_event_loop().run_until_complete(_go())


def _identity(user_id: str, org_id: str, method: str, provider_user_id: str = "555") -> UserIdentity:
    return UserIdentity(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        provider="github",
        provider_user_id=provider_user_id,
        verification_method=method,
    )


def _user(user_id: str, org_id: str) -> User:
    return User(id=user_id, org_id=org_id, team_id="", email=f"{user_id}@test.example")


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------


class TestPolicyVocabulary:
    """The two sets must stay in the relationship the endpoint's split relies on."""

    def test_channel_placement_identifies_but_does_not_prove(self):
        """The whole repair in one assertion.

        If both halves were True this endpoint would still manufacture proof; if
        both were False every inbound message would re-provision a shadow user.
        """
        assert identifies(CHANNEL_PLACEMENT) is True
        assert is_proven(CHANNEL_PLACEMENT) is False

    @pytest.mark.parametrize("method", sorted(PROVEN_METHODS))
    def test_every_proven_method_still_identifies(self, method):
        """The superset direction: a genuinely proven row must keep routing.

        Asserted over the policy set rather than a literal list, so a method added
        to PROVEN_METHODS without adding it to IDENTIFYING_METHODS fails here
        instead of silently breaking resolution for that population.
        """
        assert identifies(method) is True
        assert is_proven(method) is True

    @pytest.mark.parametrize("method", ["self_asserted", "magic_link", "", None, "totally_new_scheme", "CHANNEL_PLACEMENT"])
    def test_unproven_and_unknown_values_neither_prove_nor_identify(self, method):
        """Fail-closed on both readers.

        Includes the wrong-case spelling: string membership is exact, and a caller
        that got the case wrong must not accidentally land in a trusted set.
        """
        assert is_proven(method) is False
        assert identifies(method) is False

    def test_a_placement_row_is_reclaimable_by_a_proven_owner(self):
        """The relabel also closes a lockout, via `vault_routes.py`'s holder rule.

        That path refuses a takeover when `is_proven(holder.verification_method)` —
        so while auto-provisioned rows were labelled `admin_manual` they were
        PERMANENTLY unclaimable: the account's real owner, even after an
        out-of-band-confirmed magic link, got 409 `held_by_proven_link` forever.
        Internal nonces always carry `DELIVERY_SHARED_CHANNEL` and
        `target_user_id=None`, so no in-channel flow could ever produce a strong
        enough claim to displace it. Unproven placement makes the row recoverable.
        """
        assert not is_proven(CHANNEL_PLACEMENT), "an unclaimable auto-provisioned row is a lockout, not a security property"

    def test_only_placement_and_legacy_manual_identify_without_proof(self):
        """Pins the size of the gap between the two sets.

        A future value added to IDENTIFYING_METHODS is a new way to resolve to a
        platform user without proving control, which is exactly the class of change
        that needs a deliberate decision rather than an incidental one.
        """
        assert IDENTIFYING_METHODS - PROVEN_METHODS == {CHANNEL_PLACEMENT, "admin_manual"}
        assert CHANNEL_PLACEMENT in UNPROVEN_METHODS


# ---------------------------------------------------------------------------
# The auto-provision write
# ---------------------------------------------------------------------------


@patch("src.internal.routes._get_magic_link_secret", return_value=_SECRET)
@patch("src.internal.routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
@patch("src.internal.routes.get_settings")
class TestAutoProvisionIsNotProof:
    @staticmethod
    def _settings(mock_settings):
        settings = MagicMock()
        settings.internal_api_key = _VALID_CALLER
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

    def test_provisioned_row_is_channel_placement_and_unverified(self, mock_settings, _url, _secret, client, db):
        """The row the endpoint writes, read back from the database.

        Asserting the persisted row and not just the response body: the response is
        derived, whereas this row is what every downstream consumer queries.
        """
        self._settings(mock_settings)

        resp = _resolve(client, provider="slack", provider_user_id="U-new", channel_context="T-mapped")

        assert resp.status_code == 201
        assert resp.json()["verification_method"] == CHANNEL_PLACEMENT

        async def _read():
            return (await db.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "U-new"))).scalar_one()

        row = asyncio.get_event_loop().run_until_complete(_read())
        assert row.verification_method == CHANNEL_PLACEMENT
        assert row.verified_at is None, "nothing was verified, so there is no verification timestamp"
        assert not is_proven(row.verification_method)

    def test_the_response_never_claims_a_proven_method(self, mock_settings, _url, _secret, client):
        """The caller decides authority from this field, so a 201 must not lie.

        The webhook Lambda reads `verification_method` off this response to decide
        whether to mint human dispatch authority. Reporting `admin_manual` here is
        how the body-supplied account id became an administrator's assertion.
        """
        self._settings(mock_settings)

        body = _resolve(client, provider="slack", provider_user_id="U-claims", channel_context="T-mapped").json()

        assert body["verification_method"] not in PROVEN_METHODS
        assert body["is_shadow"] is True

    def test_second_resolve_reuses_the_row_and_does_not_reissue_a_magic_link(self, mock_settings, _url, _secret, client, db):
        """The availability half — the coupling that blocked the previous slice.

        If the unproven row stopped satisfying the lookup, this second call would
        404 with a fresh magic link and provision a duplicate shadow user on every
        inbound message. Pinning 200 plus a stable user_id and a single identity row
        is what makes the relabel safe to ship.
        """
        self._settings(mock_settings)

        first = _resolve(client, provider="slack", provider_user_id="U-repeat", channel_context="T-mapped")
        second = _resolve(client, provider="slack", provider_user_id="U-repeat", channel_context="T-mapped")

        assert first.status_code == 201
        assert second.status_code == 200, "an unproven row must still answer the routing question"
        assert second.json()["user_id"] == first.json()["user_id"]
        assert "magic_link_url" not in second.json()
        assert second.json()["verification_method"] == CHANNEL_PLACEMENT

        async def _count():
            rows = (await db.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "U-repeat"))).scalars().all()
            users = (await db.execute(select(User).where(User.is_shadow.is_(True)))).scalars().all()
            return len(rows), len([u for u in users if u.email.endswith("U-repeat@shadow.adp")])

        identity_count, user_count = asyncio.get_event_loop().run_until_complete(_count())
        assert identity_count == 1, "the second resolve must reuse the row, not write another"
        assert user_count == 1, "the second resolve must not provision a duplicate shadow user"


# ---------------------------------------------------------------------------
# The lookup filter
# ---------------------------------------------------------------------------


@patch("src.internal.routes._get_magic_link_secret", return_value=_SECRET)
@patch("src.internal.routes._build_magic_link_url", side_effect=lambda t: f"https://gw.example.com/auth/link/magic?token={t}")
@patch("src.internal.routes.get_settings")
class TestLookupFilter:
    @staticmethod
    def _settings(mock_settings):
        settings = MagicMock()
        settings.internal_api_key = _VALID_CALLER
        settings.magic_link_secret = _SECRET
        settings.gateway_base_url = "https://gw.example.com"
        mock_settings.return_value = settings

    @pytest.mark.parametrize("method", sorted(PROVEN_METHODS))
    def test_genuinely_proven_rows_resolve_and_report_their_proof(self, mock_settings, _url, _secret, client, db, method):
        """The positive path the repair must preserve, across every proven method.

        A real OAuth sign-in and a real administrator link must resolve to their
        platform user AND carry their method through to the caller, or the webhook
        authority gate denies every legitimate dispatch.
        """
        self._settings(mock_settings)
        _seed(db, _user("u-proven", "org-a"), _identity("u-proven", "org-a", method))

        resp = _resolve(client, provider="github", provider_user_id="555")

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "u-proven"
        assert resp.json()["verification_method"] == method
        assert is_proven(resp.json()["verification_method"])

    @pytest.mark.parametrize("method", ["self_asserted", "magic_link"])
    def test_unproven_non_placement_rows_do_not_resolve(self, mock_settings, _url, _secret, client, db, method):
        """A self-asserted claim is not a routing target either.

        `channel_placement` earns routing because the PLATFORM created it as a
        stable attribution target for a mapped channel. A row the user asserted
        about themselves earns nothing, so it must not resolve — otherwise naming
        another person's account id still redirects their events.
        """
        self._settings(mock_settings)
        _seed(db, _user("u-claim", "org-a"), _identity("u-claim", "org-a", method))

        resp = _resolve(client, provider="github", provider_user_id="555")

        assert resp.status_code == 404
        assert "magic_link_url" in resp.json()["detail"]

    def test_legacy_manual_identity_routes_without_authority_or_relabel(self, mock_settings, _url, _secret, client, db):
        from datetime import UTC, datetime

        self._settings(mock_settings)
        user = _user("u-legacy", "org-a")
        user.is_shadow = False
        identity = _identity("u-legacy", "org-a", "admin_manual")
        identity.verified_at = datetime.now(UTC)
        _seed(db, user, identity)

        first = _resolve(client, provider="github", provider_user_id="555")
        second = _resolve(client, provider="github", provider_user_id="555")
        assert first.status_code == second.status_code == 200
        assert first.json()["user_id"] == second.json()["user_id"] == "u-legacy"
        assert first.json()["verification_method"] == "admin_manual"
        assert identifies(first.json()["verification_method"])
        assert not is_proven(first.json()["verification_method"])
        assert identity.verification_method == "admin_manual"

    def test_ambiguous_cross_tenant_identity_is_refused_not_guessed(self, mock_settings, _url, _secret, client, db):
        """The 409 branch, which previously had no test anywhere in the repo.

        The unique index is `(provider, provider_user_id, org_id)`, so one account
        legitimately holds one row per tenant. `scalar_one_or_none()` used to raise
        MultipleResultsFound here and surface as a 500. Picking a row instead would
        be guessing which tenant an inbound event belongs to.
        """
        self._settings(mock_settings)
        _seed(
            db,
            _user("u-a", "org-a"),
            _user("u-b", "org-b"),
            _identity("u-a", "org-a", "oauth"),
            _identity("u-b", "org-b", "oauth"),
        )

        resp = _resolve(client, provider="github", provider_user_id="555")

        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "ambiguous_identity"

    def test_org_id_disambiguates_the_ambiguous_case(self, mock_settings, _url, _secret, client, db):
        """The refusal is recoverable, which is what makes it a safe default.

        A caller that knows the tenant gets a precise answer, so the 409 is a
        request for disambiguation rather than a dead end.
        """
        self._settings(mock_settings)
        _seed(
            db,
            _user("u-a", "org-a"),
            _user("u-b", "org-b"),
            _identity("u-a", "org-a", "oauth"),
            _identity("u-b", "org-b", "oauth"),
        )

        resp = _resolve(client, provider="github", provider_user_id="555", org_id="org-b")

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "u-b"

    def test_a_placement_row_does_not_make_another_tenants_lookup_ambiguous(self, mock_settings, _url, _secret, client, db):
        """Widening the filter must not turn a clean lookup into a 409.

        `channel_placement` joining the identifying set means one more row can match
        a bare (provider, provider_user_id) query. That is the intended cost, and it
        must stay recoverable via org_id rather than becoming a hard failure for a
        tenant whose own link is proven.
        """
        self._settings(mock_settings)
        _seed(
            db,
            _user("u-proven", "org-a"),
            _user("u-placed", "org-b"),
            _identity("u-proven", "org-a", "oauth"),
            _identity("u-placed", "org-b", CHANNEL_PLACEMENT),
        )

        scoped = _resolve(client, provider="github", provider_user_id="555", org_id="org-a")

        assert scoped.status_code == 200
        assert scoped.json()["user_id"] == "u-proven"
        assert is_proven(scoped.json()["verification_method"])
        assert _resolve(client, provider="github", provider_user_id="555").status_code == 409

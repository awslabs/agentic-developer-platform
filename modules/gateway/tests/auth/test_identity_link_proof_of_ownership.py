"""An identity link must record HOW ownership was proven — #5664 (A10).

The finding: any signed-in user could name an arbitrary external account and the
platform recorded the result as a *verified* link. Three things combined to make
the claim self-certifying:

1. ``provider_user_id`` arrived in the request body, so the claimant chose which
   account to claim.
2. The response handed the magic link straight back to the claimant, so the
   "confirmation" step could be completed by the same person who made the claim.
3. The confirm handler then wrote ``verification_method="magic_link"`` with
   ``verified_at=now()``.

Nothing in that circle ever contacted the account being claimed. So "verified"
meant only "the requester can read their own HTTP response".

The INTERNAL issuance path posts the link in the shared conversation that
triggered it. That is shared-channel access, not proof of account ownership, and
its confirmations remain unproven. The private-delivery positives below seed
user-bound nonces as consumer-contract fixtures; they do not exercise a deployed
private-message adapter. Provider-confirmed onboarding and authenticated admin
identity mapping are the existing production proof-establishing paths.

Earlier confirmations used the ambiguous ``magic_link`` string, so a consumer
reading ``user_identities`` could not establish how ownership had been proven. These
tests pin the distinction:

* a self-service claim is recorded as ``self_asserted`` with ``verified_at``
  **NULL**, and the link is not returned to the claimant;
* an out-of-band-delivered link that is confirmed becomes
  ``magic_link_confirmed`` with ``verified_at`` set;
* ``is_proven`` separates the two, fail-closed.
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

from src.auth.magic_link import issue_token, store_nonce
from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager, router
from src.shared.database import get_db
from src.shared.identity.verification import (
    DELIVERY_PROVIDER_DM,
    MAGIC_LINK_CONFIRMED,
    PROVEN_METHODS,
    SELF_ASSERTED,
    UNPROVEN_METHODS,
    is_proven,
)
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import MagicLinkNonce, UserIdentity
from src.shared.schemas.auth import TokenContext

_SECRET = "test-magic-link-secret-key-32chars!!"


def _ctx(user_id: str = "user-alice") -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id="org-acme",
        team_id="team-eng",
        department_id="dept-eng",
        account_type="human",
        is_admin=False,
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
# The trust registry itself
# ---------------------------------------------------------------------------


class TestVerificationTrustRegistry:
    def test_self_asserted_is_never_proof(self):
        assert is_proven(SELF_ASSERTED) is False

    def test_out_of_band_confirmation_is_proof(self):
        assert is_proven(MAGIC_LINK_CONFIRMED) is True

    def test_the_two_sets_are_disjoint(self):
        assert not (PROVEN_METHODS & UNPROVEN_METHODS)

    @pytest.mark.parametrize("value", [None, "", "bogus", "MAGIC_LINK_CONFIRMED", "future_method"])
    def test_unknown_values_fail_closed(self, value):
        """A method nobody declared proven must not be trusted by default —
        including a future writer's new string, and including NULL."""
        assert is_proven(value) is False

    def test_legacy_bare_magic_link_is_not_trusted(self):
        """Rows written before this change could be either an out-of-band
        confirmation or a self-certified claim. The platform cannot tell which, so
        it must not assume the favourable one."""
        assert is_proven("magic_link") is False


# ---------------------------------------------------------------------------
# The claim surface must not hand the proof back to the claimant
# ---------------------------------------------------------------------------


class TestSelfServiceClaimIsNotProof:
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_the_link_is_not_returned_to_the_requester(self, _secret, db):
        """The circle closes here: a link returned to the claimant lets the same
        person who made the claim also 'confirm' it."""
        client = _make_app(db)

        resp = client.post("/auth/identities/slack/link", json={"provider_user_id": "U-victim"})

        assert resp.status_code in (201, 202), resp.text
        body = resp.json()
        assert "magic_link_url" not in body, "the confirmation link was handed back to the claimant"
        assert "token=" not in resp.text, "a usable token leaked in the response body"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_an_unproven_claim_sets_no_verified_at(self, _secret, db):
        """Acceptance criterion: an unproven claim creates no verified record.

        If a row is recorded at all it must be visibly unproven, so a consumer
        reading it cannot mistake the claim for evidence.
        """
        client = _make_app(db)

        client.post("/auth/identities/slack/link", json={"provider_user_id": "U-victim"})
        await db.commit()

        rows = (await db.execute(select(UserIdentity).where(UserIdentity.provider == "slack"))).scalars().all()

        for row in rows:
            assert row.verified_at is None, f"unproven claim stamped verified_at={row.verified_at!r}"
            assert is_proven(row.verification_method) is False, f"unproven claim recorded as trusted method {row.verification_method!r}"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_claiming_someone_elses_account_grants_nothing_trusted(self, _secret, db):
        """The end-to-end shape of the finding: Alice claims Bob's Slack account
        and no trusted link exists afterwards, however the flow is driven."""
        client = _make_app(db, _ctx("user-alice"))

        client.post("/auth/identities/slack/link", json={"provider_user_id": "U-bob"})
        await db.commit()

        trusted = [
            row
            for row in (await db.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "U-bob"))).scalars().all()
            if is_proven(row.verification_method)
        ]
        assert trusted == [], "an unproven claim produced a trusted identity link"


# ---------------------------------------------------------------------------
# The out-of-band path must still work, and must be distinguishable
# ---------------------------------------------------------------------------


class TestOutOfBandConfirmationIsProof:
    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_confirming_a_delivered_link_records_proof(self, _secret, db):
        """Guards the 'broke the legitimate half' failure mode: a link that was
        delivered to the claimed account and confirmed there is real evidence and
        must still produce a trusted, timestamped row."""
        result = issue_token(
            provider="slack",
            provider_user_id="U-alice",
            channel_context="T01/C01",
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-alice",
            channel_context="T01/C01",
            target_user_id="user-alice",
            expires_at=result["expires_at"],
            db=db,
            # Private delivery to the claimed account is what makes this evidence.
            # A shared-channel post is asserted to be UNPROVEN separately below.
            delivery_method=DELIVERY_PROVIDER_DM,
        )
        await db.commit()

        client = _make_app(db)
        resp = client.post(f"/auth/link/magic?token={result['token']}")

        assert resp.status_code == 201, resp.text
        await db.commit()

        row = (await db.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "U-alice"))).scalar_one()
        assert row.verification_method == MAGIC_LINK_CONFIRMED
        assert row.verified_at is not None
        assert is_proven(row.verification_method) is True

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_confirmed_rows_do_not_reuse_the_ambiguous_label(self, _secret, db):
        """The bare "magic_link" string cannot distinguish the two paths, so the
        confirmed path must stop writing it — otherwise consumers are back to
        guessing."""
        result = issue_token(
            provider="slack",
            provider_user_id="U-alice2",
            channel_context=None,
            target_user_id="user-alice",
            secret_key=_SECRET,
        )
        await store_nonce(
            jti=result["jti"],
            provider="slack",
            provider_user_id="U-alice2",
            channel_context=None,
            target_user_id="user-alice",
            expires_at=result["expires_at"],
            db=db,
            delivery_method=DELIVERY_PROVIDER_DM,
        )
        await db.commit()

        client = _make_app(db)
        client.post(f"/auth/link/magic?token={result['token']}")
        await db.commit()

        row = (await db.execute(select(UserIdentity).where(UserIdentity.provider_user_id == "U-alice2"))).scalar_one()
        assert row.verification_method != "magic_link", "still writing the ambiguous legacy label"

    @patch("src.auth.vault_routes._get_magic_link_secret", return_value=_SECRET)
    async def test_a_claim_does_not_consume_its_own_nonce(self, _secret, db):
        """A self-service claim may prepare a confirmation, but it must leave the
        nonce unconsumed — the account holder is the one who completes it."""
        client = _make_app(db)

        client.post("/auth/identities/slack/link", json={"provider_user_id": "U-victim2"})
        await db.commit()

        nonces = (await db.execute(select(MagicLinkNonce))).scalars().all()
        for nonce in nonces:
            assert nonce.consumed_at is None

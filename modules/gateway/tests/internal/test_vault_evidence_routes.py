"""Transport-level tests for the vault evidence/delivery routes — Issue #5528.

The authorization RULES are tested against the services directly in
``tests/auth/test_vault_evidence.py`` and ``tests/auth/test_vault_delivery.py``.
This module tests only what the transport adds, which is where the two bugs this
layer could plausibly introduce would live:

1. **Answering with the wrong status.** The domain consumer maps a 403 onto "denied"
   and a 503 onto "the vault is unavailable", and those send an operator in opposite
   directions. A refusal that surfaced as 503 would be reported as an outage.
2. **Identifying the caller from something the caller controls.** The internal plane
   authenticates the shared worker IRSA role; ``broker_identity`` states outright
   that *"workers share IRSA"*. So the recipient identity must come from the
   HMAC-verified run credential and the tenant from its ``tenant_id`` — never from
   the request body. A test asserting only "delivery works" would pass just as
   happily against a route that read ``body.recipient``, which is why the tests
   below drive the mismatches rather than the agreement.

Delivery is gated on a registry-held scope that no seed grants today, so the route
is inert in this deployment. That is asserted here as behaviour
(:class:`TestDeliveryIsInertWithoutTheRegistryScope`) rather than left as a claim in
a docstring — an "inert" endpoint nobody tested is just an untested endpoint.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.vault_delivery import DELIVERY_PERMISSION
from src.internal import vault_metadata_authority
from src.internal.auth_deps import VerifiedInternalIdentity, verify_internal_or_irsa
from src.internal.credential_routes import get_secrets_manager
from src.internal.vault_evidence_routes import DELIVERY_SCOPE, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import (
    CredentialValidationEvidence,
    CredentialWorkspaceDelegation,
    UserCredential,
)
from tests.agentauth.test_bootstrap_routes import kubernetes as workload_verifier  # noqa: F401
from tests.agentauth.test_bootstrap_routes import store as bootstrap_store  # noqa: F401
from tests.operation_delivery_support import grant_operation, operation_storage

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

ORG = "org-acme"
OTHER_ORG = "org-other"
WORKSPACE = "ws-1"
CRED = "cred-1"
FOREIGN_CRED = "cred-foreign"
FOREIGN_WORKSPACE = "ws-other"
ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:acme/openai-AbCdEf"
VERSION = "11111111-2222-3333-4444-555555555555"
SECRET_VALUE = "sk-live-do-not-log-this-0123456789"

# invocation_id#attempt — the shape RunCredential.principal produces.
PRINCIPAL = "inv-abc#1"
OTHER_PRINCIPAL = "inv-xyz#1"


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def engine():
    eng = _make_engine()
    await operation_storage(eng)
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for org_id in (ORG, OTHER_ORG):
            session.add(
                Organization(
                    id=org_id,
                    name=org_id,
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                )
            )
        session.add(Department(id="dept-eng", org_id=ORG, name="Engineering"))
        session.add(Team(id="team-eng", org_id=ORG, department_id="dept-eng", name="Eng"))
        session.add(User(id="user-alice", org_id=ORG, team_id="team-eng", email="alice@test.com"))
        session.add(Department(id="dept-other", org_id=OTHER_ORG, name="Other Dept"))
        session.add(Team(id="team-other", org_id=OTHER_ORG, department_id="dept-other", name="Other"))
        session.add(User(id="user-bob", org_id=OTHER_ORG, team_id="team-other", email="bob@test.com"))
        # A FULLY VALID credential belonging to the other tenant: delegated, live, and
        # resolvable. It exists so the cross-tenant test can be non-vacuous — with a
        # credential that only lived in ORG, `deliver_credential` would refuse on its
        # own tenant filter and the test would pass whether or not the route checked
        # the verified tenant at all.
        session.add(
            UserCredential(
                id=FOREIGN_CRED,
                org_id=OTHER_ORG,
                user_id="user-bob",
                service="openai",
                label="default",
                credential_type="api_key",
                secret_arn=ARN,
                strict=False,
            )
        )
        session.add(
            CredentialWorkspaceDelegation(
                id="deleg-foreign",
                credential_id=FOREIGN_CRED,
                workspace_id=FOREIGN_WORKSPACE,
                org_id=OTHER_ORG,
                delegated_by="user:user-bob",
            )
        )
        session.add(
            # `owner_scope` is a derived property, not a column — it reads "user"
            # because `user_id` is set. `strict=False` skips the constructor's
            # ownership validation, matching the service-level fixtures.
            UserCredential(
                id=CRED,
                org_id=ORG,
                user_id="user-alice",
                service="openai",
                label="default",
                credential_type="api_key",
                secret_arn=ARN,
                strict=False,
            )
        )
        session.add(
            CredentialWorkspaceDelegation(
                id="deleg-1",
                credential_id=CRED,
                workspace_id=WORKSPACE,
                org_id=ORG,
                delegated_by="user:user-alice",
            )
        )
        await grant_operation(session, org=ORG, workspace=WORKSPACE, holder=PRINCIPAL)
        await grant_operation(session, org=OTHER_ORG, workspace=FOREIGN_WORKSPACE, holder=PRINCIPAL, operation="foreign-op", credential=FOREIGN_CRED)
        session.add(
            CredentialValidationEvidence(
                credential_id=FOREIGN_CRED,
                org_id=OTHER_ORG,
                workspace_id=FOREIGN_WORKSPACE,
                validated_version_id=VERSION,
                provider_account_id="123456789012",
                credential_valid=True,
                permissions_sufficient=True,
                quota_available=True,
                observed_capacity=1,
                checked_at=datetime.now(UTC),
            )
        )
        session.add(
            CredentialValidationEvidence(
                credential_id=CRED,
                org_id=ORG,
                workspace_id=WORKSPACE,
                validated_version_id=VERSION,
                provider_account_id="123456789012",
                credential_valid=True,
                permissions_sufficient=True,
                quota_available=True,
                observed_capacity=1,
                checked_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return factory


@pytest.fixture
def sm():
    helper = MagicMock()
    helper.current_version_id.return_value = VERSION
    helper.get_secret.return_value = SECRET_VALUE
    helper.get_secret_at_version.return_value = (SECRET_VALUE, VERSION)
    return helper


@pytest.fixture
def client(session_factory, sm):
    """A TestClient with the vault router mounted and auth stubbed out.

    ``verify_internal_or_irsa`` is overridden rather than exercised: transport
    authentication is already covered by ``tests/internal/test_auth_deps.py``, and
    re-testing it here would say nothing about this module. What is NOT stubbed is
    the run-credential verification — that is this module's own addition, so each
    test below supplies it explicitly.
    """
    from src.internal.auth_deps import verify_internal_or_irsa

    app = FastAPI()
    app.include_router(router)

    async def _db_override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    app.dependency_overrides[verify_internal_or_irsa] = lambda: None

    with TestClient(app) as test_client:
        yield test_client


def _evidence_body(**overrides):
    body = {
        "org_id": ORG,
        "workspace_id": WORKSPACE,
        "credential_id": CRED,
        "principal": "user:user-alice",
    }
    body.update(overrides)
    return body


def _delivery_body(**overrides):
    body = {
        "operation_id": "op-1",
        "attempt_id": "att-1",
        "job_id": "job-1",
        "org_id": ORG,
        "workspace_id": WORKSPACE,
        "credential_id": CRED,
        "recipient": PRINCIPAL,
        "provider": "openai",
        "provider_account_id": "123456789012",
    }
    body.update(overrides)
    return body


def _verified(principal: str = PRINCIPAL, tenant_id: str = ORG):
    """Patch the run-credential verification to yield a given executor identity.

    Patched at ``_verified_executor`` rather than by forging a credential token
    because minting one requires ``AGENT_RUN_CREDENTIAL_KEY`` and a DynamoDB
    authority store. The verification logic itself is covered by the agentauth
    suites; what matters here is that the route consumes ITS answer and not the
    body's claim, which is exactly what patching the seam lets these tests prove.
    """

    async def _fake(_request):
        return principal, tenant_id

    return patch("src.internal.vault_evidence_routes._verified_executor", _fake)


def _granted(scopes: list[str]):
    """Patch the token context so the caller holds the given registry scopes."""
    context = MagicMock()
    context.credential_scopes = scopes
    return patch(
        "src.internal.vault_evidence_routes._granted_permissions",
        lambda _request: frozenset({DELIVERY_PERMISSION}) if DELIVERY_SCOPE in scopes else frozenset(),
    )


class TestEvidenceRoute:
    def test_returns_vault_facts_for_the_owner(self, metadata_client):
        client, _ = metadata_client
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(principal="service:metadata-reader"))
        assert response.status_code == 200
        body = response.json()
        assert body["credential_id"] == CRED
        assert body["owner_principal"] == "user:user-alice"
        assert body["owner_scope"] == "user"
        assert body["delegated_to_workspaces"] == [WORKSPACE]
        assert body["current_version_id"] == VERSION
        # No attestation was claimed, so none is asserted.
        assert body["attested_report_digest"] is None

    def test_response_carries_no_secret_value_or_arn(self, metadata_client):
        """The whole response body, as text — not just the fields we remembered.

        Asserting on ``body["value"] is None`` would only prove the field we thought
        of is absent. Scanning the serialised text catches a value that arrives in a
        field a future change adds.
        """
        client, _ = metadata_client
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(principal="service:metadata-reader"))
        assert response.status_code == 200
        assert SECRET_VALUE not in response.text
        assert ARN not in response.text
        assert "secret_arn" not in response.json()

    def test_unestablished_evidence_is_403_and_not_503(self, client):
        """A denial must not read as an outage. See the module docstring."""
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(credential_id="cred-nope"))
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "denied"

    def test_foreign_tenant_is_refused_identically_to_unknown(self, client):
        """Cross-tenant and not-found must be indistinguishable to the caller.

        Compares the two full responses. If a later change made the foreign-tenant
        path more specific, this fails — which is the point: the difference IS the
        enumeration oracle, so the test asserts on their equality rather than on
        each one's status alone.
        """
        unknown = client.post("/internal/v1/credential-evidence", json=_evidence_body(credential_id="cred-nope"))
        foreign = client.post("/internal/v1/credential-evidence", json=_evidence_body(org_id=OTHER_ORG))
        assert unknown.status_code == foreign.status_code == 403
        assert unknown.json() == foreign.json()

    def test_a_malformed_digest_claim_is_rejected_by_the_parser(self, client):
        """Not a hex sha256 — refused at the boundary, never reaching a comparison."""
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(report_digest="not-a-digest"))
        assert response.status_code == 422

    def test_a_forged_digest_claim_is_refused(self, client):
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(report_digest="a" * 64))
        assert response.status_code == 403


class TestEvidenceRouteAttestation:
    """The digest is recomputed from the Gateway's rows, never echoed back."""

    @pytest.fixture
    async def with_validation(self, session_factory):
        from sqlalchemy import delete

        from src.auth.vault_evidence import validation_digest

        readings = {
            "credential_valid": True,
            "permissions_sufficient": True,
            "quota_available": True,
            "observed_capacity": 40,
            "detail": "ok",
        }
        async with session_factory() as session:
            await session.execute(delete(CredentialValidationEvidence))
            session.add(
                CredentialValidationEvidence(
                    validated_version_id=VERSION,
                    provider_account_id="123456789012",
                    id="val-1",
                    credential_id=CRED,
                    workspace_id=WORKSPACE,
                    org_id=ORG,
                    checked_at=datetime.now(UTC) - timedelta(minutes=5),
                    **readings,
                )
            )
            await session.commit()
        return validation_digest(**readings)

    def test_a_matching_claim_is_confirmed(self, metadata_client, with_validation):
        client, _ = metadata_client
        digest = with_validation
        # Guards against a vacuous pass: if the fixture ever stopped returning a
        # resolved digest, the comparison below would be against a coroutine and the
        # test would fail for the wrong reason rather than silently prove nothing.
        assert isinstance(digest, str) and len(digest) == 64
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(report_digest=digest, principal="service:metadata-reader"))
        assert response.status_code == 200
        body = response.json()
        assert body["attested_report_digest"] == digest
        assert body["report_checked_at"] is not None

    def test_a_near_miss_claim_is_refused(self, client, with_validation):
        """One flipped character must not attest. Guards the compare, not the lookup."""
        digest = with_validation
        forged = ("b" if digest[0] != "b" else "c") + digest[1:]
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(report_digest=forged))
        assert response.status_code == 403


class TestDeliveryUsesTheVerifiedIdentityNotTheBody:
    """The two bugs this layer could introduce. See the module docstring."""

    def test_a_body_naming_a_foreign_tenant_is_refused(self, client, sm):
        """The route's own tenant check, isolated so it cannot pass by proxy.

        The request asks for OTHER_ORG's credential, in OTHER_ORG's workspace, with
        OTHER_ORG's delegation — a reference that is entirely VALID within that
        tenant, so ``deliver_credential`` would resolve and serve it. The only thing
        standing between this request and another tenant's secret is the route
        comparing the asserted ``org_id`` against the verified ``tenant_id``.

        Verified as non-tautological by mutation: replacing that comparison with
        ``if False`` makes this test fail. An earlier version of this test reused
        ORG's own credential id and survived the mutation, because the service's
        independent tenant filter refused it first — it was asserting defense in
        depth while appearing to assert the route.
        """
        with _verified(tenant_id=ORG), _granted([DELIVERY_SCOPE]):
            response = client.post(
                "/internal/v1/credential-delivery",
                json=_delivery_body(operation_id="foreign-op", org_id=OTHER_ORG, workspace_id=FOREIGN_WORKSPACE, credential_id=FOREIGN_CRED),
            )
        assert response.status_code == 403
        assert SECRET_VALUE not in response.text
        # Never reached the vault: refused before any secret fetch.
        sm.get_secret.assert_not_called()

    def test_the_same_foreign_reference_would_otherwise_have_been_served(self, client):
        """Proves the previous test's request is refused ONLY by the tenant check.

        Identical reference, but now the verified run credential belongs to
        OTHER_ORG — and it succeeds. Without this, the refusal above could be caused
        by anything (a typo in the fixture, an unrelated guard) and the test would
        still pass while proving nothing about tenant isolation.
        """
        with _verified(tenant_id=OTHER_ORG), _granted([DELIVERY_SCOPE]):
            response = client.post(
                "/internal/v1/credential-delivery",
                json=_delivery_body(operation_id="foreign-op", org_id=OTHER_ORG, workspace_id=FOREIGN_WORKSPACE, credential_id=FOREIGN_CRED),
            )
        assert response.status_code == 200

    def test_naming_another_executor_as_recipient_is_refused(self, client):
        """The verified principal is PRINCIPAL; the body asks for OTHER_PRINCIPAL.

        This is the check that makes the lease recipient-BOUND rather than
        recipient-labelled: without it any worker sharing the IRSA role could
        request a credential bound to a different run.
        """
        with _verified(principal=PRINCIPAL), _granted([DELIVERY_SCOPE]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body(recipient=OTHER_PRINCIPAL))
        assert response.status_code == 403
        assert SECRET_VALUE not in response.text

    def test_an_unverified_run_is_refused_before_any_vault_read(self, client, sm):
        """No secret fetch and no version read on the unauthenticated path.

        Asserting the mocks were never called is what distinguishes "refused" from
        "did the work, then refused" — the latter leaks existence through timing and
        would have already pulled the value into process memory.
        """
        from fastapi import HTTPException

        async def _refuse(_request):
            raise HTTPException(status_code=403, detail={"error": "denied"})

        with patch("src.internal.vault_evidence_routes._verified_executor", _refuse):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_an_unreachable_authority_store_is_503_not_403(self, client):
        """Unavailability and denial are different answers.

        The inverse of the evidence route's rule: a genuine outage must NOT be
        reported as a denial, or an operator will go looking for a misconfigured
        permission instead of a broken store.
        """
        from src.agentauth.store import AuthorityStoreError

        # Patches the real seam: `_verified_executor` is NOT stubbed here, so the
        # route's own try/except is what has to classify this error. Stubbing
        # `_verified_executor` would have tested the stub's choice of status instead.
        with (
            patch("src.agentauth.routes.get_agent_runtime", return_value=MagicMock()),
            patch("src.internal.vault_evidence_routes.run_in_threadpool", side_effect=AuthorityStoreError("down")),
        ):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "unavailable"
        assert SECRET_VALUE not in response.text


class TestDeliveryIsInertWithoutTheRegistryScope:
    """The activation gate, asserted as behaviour rather than claimed in prose."""

    def test_a_caller_without_the_scope_is_refused(self, client, sm):
        with _verified(), _granted([]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_an_unrelated_credential_scope_does_not_grant_delivery(self, client, sm):
        """Holding raw-read or materialize is not authority to use this path."""
        with _verified(), _granted(["credential:raw-read", "credential:materialize"]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_no_seed_grants_the_delivery_scope_today(self):
        """Pins the inert posture to the Terraform seeds, not to a comment.

        If a future change grants ``DELIVERY_SCOPE`` in a seed, this test fails and
        forces the activation to be a deliberate, reviewed decision — which is the
        deployment step this story explicitly does not authorize.
        """
        from pathlib import Path

        repo = Path(__file__).resolve().parents[4]
        seeds = list((repo / "modules" / "agent-factory" / "infra").glob("*.tf")) + list((repo / "modules" / "gateway" / "infra").rglob("*.tf"))
        assert seeds, "expected to find Terraform seed files to check"
        granting = [p for p in seeds if DELIVERY_SCOPE in p.read_text(encoding="utf-8", errors="ignore")]
        assert granting == [], f"{DELIVERY_SCOPE} is granted in {granting}; delivery is no longer inert"


class TestDeliverySuccessPath:
    """What a granted caller gets, once the scope exists."""

    def test_delivers_the_value_with_the_revocation_limitation(self, client):
        with _verified(), _granted([DELIVERY_SCOPE]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 200
        body = response.json()
        assert body["value"] == SECRET_VALUE
        assert body["credential_type"] == "api_key"
        assert body["provenance_id"]
        # On the SUCCESS path too: the operator who needs this is not reading a refusal.
        assert "does not revoke it" in body["revocation_limitation"]

    def test_the_audit_row_records_the_delivery_and_not_the_value(self, client, session_factory):
        from sqlalchemy import select

        from src.shared.models.audit import AuditLog

        with _verified(), _granted([DELIVERY_SCOPE]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 200

        async def _rows():
            async with session_factory() as session:
                result = await session.scalars(select(AuditLog).where(AuditLog.event_type == "vault_credential_delivered"))
                return list(result.all())

        rows = asyncio.run(_rows())
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == PRINCIPAL
        assert row.details["credential_id"] == CRED
        assert row.details["recipient"] == PRINCIPAL
        # The value must not be anywhere in the serialised audit detail.
        assert SECRET_VALUE not in str(row.details)
        assert ARN not in str(row.details)

    def test_a_refusal_is_audited_too(self, client, session_factory):
        """A denied delivery must leave a trace, or an attempt is invisible."""
        from sqlalchemy import select

        from src.shared.models.audit import AuditLog

        with _verified(), _granted([]):
            response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert response.status_code == 403

        async def _rows():
            async with session_factory() as session:
                result = await session.scalars(select(AuditLog).where(AuditLog.event_type == "vault_credential_delivery_denied"))
                return list(result.all())

        rows = asyncio.run(_rows())
        assert len(rows) == 1
        assert rows[0].details["authenticated_recipient"] == PRINCIPAL
        assert SECRET_VALUE not in str(rows[0].details)


class TestRevocationStateRoute:
    def test_reports_that_a_delegated_credential_admits_work(self, metadata_client):
        client, state = metadata_client
        state["scopes"].append("credential:revocation:read")
        response = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
            },
        )
        assert response.status_code == 200
        assert response.json()["admits_work"] is True

    def test_a_negative_answer_always_carries_the_limitation(self, metadata_client, session_factory):
        """The requirement is that the limit is SURFACED, not merely documented."""
        from sqlalchemy import update

        async def expire():
            async with session_factory() as session:
                await session.execute(
                    update(UserCredential).where(UserCredential.id == CRED).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await session.commit()

        asyncio.run(expire())
        client, state = metadata_client
        state["scopes"].append("credential:revocation:read")
        response = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["admits_work"] is False
        assert "revoked at the provider" in body["limitation"]

    def test_this_route_needs_no_recipient(self, metadata_client):
        """It returns no material, so requiring an executor identity would be noise."""
        client, state = metadata_client
        state["scopes"].append("credential:revocation:read")
        response = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
            },
        )
        assert response.status_code == 200


class TestPairedClientServerDelivery:
    """End-to-end client/server pairing for credential delivery.

    These tests cover the integration seam that the foreground review found missing:
    the client must supply executor identity headers that the route verifies, and the
    route must refuse adversarial substitutions that the earlier unit tests could not
    cover because they patched the seam rather than driving through it.

    Authentication is stubbed at ``_verified_executor`` — the same seam the other
    transport tests patch — because the live K8s TokenReview and DynamoDB authority
    store are AWS deployment prerequisites (#5535/#5538). What is NOT stubbed is:

    * The full HTTP request/response round-trip through the FastAPI router.
    * The ``_granted_permissions`` translation (registry scope → delivery permission).
    * The service-layer authorization (tenant, delegation, expiry, recipient match).
    * The audit path and the secret-redaction discipline.

    The delivery-scope test (TestDeliveryIsInertWithoutTheRegistryScope) remains the
    activation gate — it asserts no seed grants the scope today. These tests grant it
    locally so they exercise the live path without claiming the deployment is active.
    """

    @pytest.fixture
    def paired_client(self, session_factory, sm):
        """The same fixture as `client`, but pre-wired with the delivery scope."""
        from src.internal.auth_deps import verify_internal_or_irsa

        app = FastAPI()
        app.include_router(router)

        async def _db_override():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _db_override
        app.dependency_overrides[get_secrets_manager] = lambda: sm
        app.dependency_overrides[verify_internal_or_irsa] = lambda: None

        with TestClient(app) as test_client:
            yield test_client

    def _deliver(self, test_client, *, principal=PRINCIPAL, tenant_id=ORG, scope_granted=True, **body_overrides):
        """Drive a delivery request through the full paired seam.

        ``principal`` and ``tenant_id`` come from the stubbed executor verification.
        ``scope_granted`` controls whether DELIVERY_SCOPE appears in the token context.
        """
        body = {
            "operation_id": "op-1",
            "attempt_id": "att-1",
            "job_id": "job-1",
            "org_id": ORG,
            "workspace_id": WORKSPACE,
            "credential_id": CRED,
            "recipient": PRINCIPAL,
            "provider": "openai",
            "provider_account_id": "123456789012",
        }
        body.update(body_overrides)

        with (
            _verified(principal=principal, tenant_id=tenant_id),
            _granted([DELIVERY_SCOPE] if scope_granted else []),
        ):
            return test_client.post("/internal/v1/credential-delivery", json=body)

    def test_valid_executor_with_scope_receives_material(self, paired_client, sm):
        """The full paired path: authenticated executor, correct scope, valid fixture."""
        response = self._deliver(paired_client)
        assert response.status_code == 200
        body = response.json()
        assert body["value"] == SECRET_VALUE
        assert body["credential_id"] == CRED
        assert "does not revoke it" in body["revocation_limitation"]

    def test_wrong_recipient_is_refused_before_any_fetch(self, paired_client, sm):
        """Verified principal is PRINCIPAL; body names a different executor.

        This is what makes the lease recipient-BOUND: any worker sharing the fleet
        IRSA role would pass the shared-key check, but only the one whose run
        credential names the same principal as the body's recipient passes here.
        """
        response = self._deliver(paired_client, recipient="impostor#1")
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_cross_tenant_request_is_refused_uniformly(self, paired_client, sm):
        """Body names OTHER_ORG; verified run credential belongs to ORG.

        The route compares the asserted org_id against the verified tenant_id before
        any vault read. Refusing here is what makes cross-tenant delivery impossible
        even when the fleet IRSA role is shared.
        """
        response = self._deliver(
            paired_client,
            tenant_id=ORG,  # verified identity belongs to ORG
            org_id=OTHER_ORG,  # body claims OTHER_ORG
            workspace_id=FOREIGN_WORKSPACE,
            credential_id=FOREIGN_CRED,
        )
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_no_scope_means_route_is_inert_even_for_correct_executor(self, paired_client, sm):
        """The scope gate, exercised through the full pairing rather than mocked."""
        response = self._deliver(paired_client, scope_granted=False)
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_the_response_carries_no_secret_anywhere_on_a_refusal(self, paired_client):
        """Even in the response body of a 403: no value, no ARN, no hint."""
        response = self._deliver(paired_client, recipient="wrong-executor")
        assert response.status_code == 403
        assert SECRET_VALUE not in response.text
        assert ARN not in response.text

    def test_unverified_executor_is_403_not_503(self, paired_client):
        """A failed executor verification is a denial, not an outage."""
        from fastapi import HTTPException

        async def _refuse(_request):
            raise HTTPException(status_code=403, detail={"error": "denied"})

        with patch("src.internal.vault_evidence_routes._verified_executor", _refuse):
            response = paired_client.post(
                "/internal/v1/credential-delivery",
                json={
                    "operation_id": "op-1",
                    "attempt_id": "att-1",
                    "job_id": "job-1",
                    "org_id": ORG,
                    "workspace_id": WORKSPACE,
                    "credential_id": CRED,
                    "recipient": PRINCIPAL,
                    "provider": "openai",
                    "provider_account_id": "123456789012",
                },
            )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "denied"

    def test_expired_credential_is_refused_after_successful_auth(self, session_factory, sm, paired_client):
        """Expiry is checked after executor verification, not before."""
        from sqlalchemy import update

        async def expire():
            async with session_factory() as session:
                await session.execute(
                    update(UserCredential).where(UserCredential.id == CRED).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await session.commit()

        asyncio.run(expire())
        response = self._deliver(paired_client)
        assert response.status_code == 403
        sm.get_secret.assert_not_called()

    def test_revoked_delegation_is_refused(self, session_factory, sm, paired_client):
        """Withdrawing the workspace delegation denies subsequent delivery."""
        from sqlalchemy import update

        async def revoke():
            async with session_factory() as session:
                await session.execute(
                    update(CredentialWorkspaceDelegation)
                    .where(CredentialWorkspaceDelegation.credential_id == CRED)
                    .values(revoked_at=datetime.now(UTC))
                )
                await session.commit()

        asyncio.run(revoke())
        response = self._deliver(paired_client)
        assert response.status_code == 403
        sm.get_secret.assert_not_called()


def test_missing_executor_storage_returns_unavailable_without_secret_io(client, session_factory, sm):
    from sqlalchemy import text

    async def remove_storage():
        async with session_factory() as session:
            await session.execute(text("DROP TABLE harness_operation_leases"))
            await session.commit()

    asyncio.run(remove_storage())
    with _verified(), _granted([DELIVERY_SCOPE]):
        response = client.post("/internal/v1/credential-delivery", json=_delivery_body())
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "unavailable"
    sm.current_version_id.assert_not_called()
    sm.get_secret_at_version.assert_not_called()


@pytest.fixture
def metadata_client(session_factory, sm, monkeypatch):
    from src.internal import vault_evidence_routes

    registry_id = "metadata-reader"
    state = {
        "tenant": ORG,
        "scopes": ["credential:evidence:read", f"credential:evidence:workspace:{WORKSPACE}"],
        "available": True,
        "actual_executor": vault_evidence_routes._verified_executor,
    }
    current = SimpleNamespace(
        user_id=registry_id,
        agent_registry_id=registry_id,
        org_id=ORG,
        auth_source="iam",
        scope="internal",
        credential_scopes=state["scopes"],
    )
    registry = SimpleNamespace(
        get_current_agent=lambda _registry, _role: {
            "org_id": ORG,
            "scope": "internal",
            "credential_scopes": list(state.get("current_scopes", state["scopes"])),
            "domain_org_id": state.get("domain_org_id"),
        }
        if state["available"]
        else None
    )
    monkeypatch.setattr(vault_metadata_authority, "get_agent_registry_service", lambda: registry)
    state["registry"] = registry

    async def verified_executor(_request):
        return PRINCIPAL, ORG

    monkeypatch.setattr(vault_evidence_routes, "_verified_executor", verified_executor)

    app = FastAPI()
    app.include_router(router)

    async def db_override():
        async with session_factory() as session:
            yield session

    async def iam_override(request: Request):
        request.state.token_context = current
        request.state.internal_identity = VerifiedInternalIdentity(
            "service",
            registry_id,
            state["tenant"],
            "iam",
        )

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    app.dependency_overrides[verify_internal_or_irsa] = iam_override
    with TestClient(app, headers={"X-Caller-Identity": "arn:aws:iam::111122223333:role/metadata-reader"}) as test_client:
        yield test_client, state


def test_metadata_service_requires_verified_tenant_and_its_own_principal(metadata_client, sm):
    client, state = metadata_client
    own = _evidence_body(principal="service:metadata-reader")
    answer = client.post("/internal/v1/credential-evidence", json=own)
    assert answer.status_code == 200, answer.json()
    assert answer.json()["owner_principal"] == "user:user-alice"
    assert answer.json()["org_id"] == ORG

    denied = [
        {**own, "org_id": OTHER_ORG, "credential_id": FOREIGN_CRED, "workspace_id": FOREIGN_WORKSPACE},
        {**own, "workspace_id": FOREIGN_WORKSPACE},
        {**own, "principal": "user:user-alice"},
        {**own, "principal": "service:another-reader"},
    ]
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        responses = [client.post("/internal/v1/credential-evidence", json=body) for body in denied]
    assert [response.status_code for response in responses] == [403] * len(denied)
    expected = {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    assert all(response.json() == expected for response in responses)
    reader.assert_not_called()
    sm.get_secret.assert_not_called()

    state["tenant"] = None
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        missing = client.post("/internal/v1/credential-evidence", json=own)
    assert missing.json() == responses[0].json()
    reader.assert_not_called()


def test_metadata_registry_revocation_and_revocation_route_fail_before_lookup(metadata_client):
    client, state = metadata_client
    body = _evidence_body(principal="service:metadata-reader")
    state["scopes"].clear()
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        missing_capability = client.post("/internal/v1/credential-evidence", json=body)
    assert missing_capability.status_code == 403
    reader.assert_not_called()

    state["scopes"].append("credential:evidence:read")
    state["available"] = False
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        removed_registration = client.post("/internal/v1/credential-evidence", json=body)
    assert removed_registration.json() == missing_capability.json()
    reader.assert_not_called()

    state["available"] = True
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        denied = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
            },
        )
    assert denied.status_code == 403
    reader.assert_not_called()


def test_revocation_requires_current_run_operation_and_exact_credential(metadata_client):
    client, state = metadata_client
    state["scopes"].append("credential:revocation:read")
    own = {
        "org_id": ORG,
        "workspace_id": WORKSPACE,
        "credential_id": CRED,
        "operation_id": "op-1",
        "attempt_id": "att-1",
        "job_id": "job-1",
    }
    wrong = [
        {**own, "org_id": OTHER_ORG},
        {**own, "workspace_id": FOREIGN_WORKSPACE},
        {**own, "attempt_id": "att-other"},
        {**own, "credential_id": FOREIGN_CRED},
        {**own, "operation_id": "not-this-operation"},
    ]
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        responses = [client.post("/internal/v1/credential-revocation-state", json=body) for body in wrong]
    assert [response.status_code for response in responses] == [403] * len(wrong)
    expected = {"detail": {"error": "denied", "message": "credential delivery refused"}}
    assert all(response.json() == expected for response in responses)
    reader.assert_not_called()

    with _verified(principal=OTHER_PRINCIPAL), patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        wrong_holder = client.post("/internal/v1/credential-revocation-state", json=own)
    assert wrong_holder.json() == expected
    reader.assert_not_called()


def test_authorized_revocation_of_missing_credential_returns_false(metadata_client, session_factory):
    from sqlalchemy import delete

    async def remove_credential():
        async with session_factory() as session:
            await session.execute(delete(UserCredential).where(UserCredential.id == CRED))
            await session.commit()

    asyncio.run(remove_credential())
    client, state = metadata_client
    state["scopes"].append("credential:revocation:read")
    response = client.post(
        "/internal/v1/credential-revocation-state",
        json={
            "org_id": ORG,
            "workspace_id": WORKSPACE,
            "credential_id": CRED,
            "operation_id": "op-1",
            "attempt_id": "att-1",
            "job_id": "job-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["admits_work"] is False
    assert "revoked at the provider" in response.json()["limitation"]


def test_unavailable_operation_store_never_returns_revocation_boolean(metadata_client, session_factory):
    from sqlalchemy import text

    async def remove_operation_store():
        async with session_factory() as session:
            await session.execute(text("DROP TABLE harness_operations"))
            await session.commit()

    asyncio.run(remove_operation_store())
    client, state = metadata_client
    state["scopes"].append("credential:revocation:read")
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        response = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
            },
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "credential revocation authority unavailable"}
    reader.assert_not_called()


def test_unavailable_metadata_registry_returns_503_without_vault_read(metadata_client, monkeypatch):
    from botocore.exceptions import ClientError

    client, _ = metadata_client

    def unavailable(_registry, _role):
        raise ClientError({"Error": {"Code": "Unavailable", "Message": "internal"}}, "GetItem")

    monkeypatch.setattr(
        vault_metadata_authority,
        "get_agent_registry_service",
        lambda: SimpleNamespace(get_current_agent=unavailable),
    )
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(principal="service:metadata-reader"))
    assert response.status_code == 503
    assert response.json() == {"detail": "credential evidence authority unavailable"}
    reader.assert_not_called()


@pytest.fixture
async def signed_producer_client(session_factory, sm, monkeypatch):
    from sqlalchemy import update

    from src.auth import agent_registry
    from src.internal import domain_current_identity, domain_operation_store

    domain_id = "00000000-0000-4000-8000-000000000001"
    subject = "subject-alice"
    registry_id = "api-producer"
    role = "arn:aws:iam::111122223333:role/superplane-api-producer"
    row = {
        "agent_id": registry_id,
        "agent_name": registry_id,
        "role_arn": role,
        "org_id": ORG,
        "domain_org_id": domain_id,
        "team_id": "team-eng",
        "scope": "internal",
        "credential_scopes": ["domain:operation-producer"],
        "allowed_models": [],
    }
    state = {"grant": True, "registered": True, "current_scopes": ["domain:operation-producer"]}

    def current_agent(name, registered_role):
        if not state["registered"] or (name, registered_role) != (registry_id, role):
            return None
        return {**row, "domain_org_id": state.get("domain_id", domain_id), "credential_scopes": list(state["current_scopes"])}

    registry = SimpleNamespace(
        get_agent_by_role_arn=lambda name: row if state["registered"] and name == role else None,
        get_current_agent=current_agent,
    )
    monkeypatch.setattr(agent_registry, "get_agent_registry_service", lambda: registry)
    monkeypatch.setattr(vault_metadata_authority, "get_agent_registry_service", lambda: registry)
    binding = SimpleNamespace(
        domain="superplane",
        org_id=domain_id,
        adp_org_id=ORG,
        producer_registry_id=registry_id,
        worker_registry_id="worker",
    )
    state["binding"] = binding
    monkeypatch.setattr(domain_operation_store, "bindings", lambda: (state["binding"],))
    monkeypatch.setattr(domain_current_identity, "_cognito_identity", lambda _: (True, ORG))

    async def domain_grants(_binding):
        result = SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    permissions="workspace:renew_credential",
                    revoked_at=None,
                )
            ]
            if state["grant"]
            else []
        )
        session = SimpleNamespace(scalar=AsyncMock(return_value=1), execute=AsyncMock(return_value=result))
        state["session"] = session
        yield session

    monkeypatch.setattr(domain_operation_store, "domain_session", asynccontextmanager(domain_grants))
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == "user-alice").values(cognito_sub=subject))
        session.add(TenantMembership(id="member-alice", user_id="user-alice", tenant_id=ORG, role="member", is_active=True))
        await session.commit()

    settings = MagicMock()
    settings.trust_apigw_headers = True
    settings.apigw_provenance_secret = "test-edge-provenance"
    monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.middleware.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router)

    async def db_override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    headers = {
        "X-Adp-Edge-Provenance": "test-edge-provenance",
        "X-Caller-Identity": "arn:aws:sts::111122223333:assumed-role/superplane-api-producer/request",
    }
    with TestClient(app, headers=headers) as signed_client:
        yield signed_client, state, domain_id, subject


def test_signed_producer_uses_adp_tenant_for_vault_and_domain_id_for_response(signed_producer_client):
    client, state, domain_id, subject = signed_producer_client
    own = _evidence_body(org_id=domain_id, principal=subject)
    response = client.post("/internal/v1/credential-evidence", json=own)
    assert response.status_code == 200, response.json()
    assert response.json()["org_id"] == domain_id
    assert response.json()["owner_principal"] == "user:user-alice"
    assert state["session"].scalar.await_count == 1
    assert state["session"].execute.await_count == 1

    absent = client.post("/internal/v1/credential-evidence", json={**own, "credential_id": "absent"})
    assert absent.status_code == 403
    expected = {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    assert absent.json() == expected

    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        wrong_domain = client.post("/internal/v1/credential-evidence", json={**own, "org_id": OTHER_ORG})
        state["grant"] = False
        revoked = client.post("/internal/v1/credential-evidence", json=own)
    assert wrong_domain.json() == revoked.json() == expected
    reader.assert_not_called()

    state["grant"] = True
    state["current_scopes"].clear()
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        stale = client.post("/internal/v1/credential-evidence", json=own)
    assert stale.json() == expected
    reader.assert_not_called()


def test_signed_producer_rejects_missing_edge_provenance_before_vault_read(signed_producer_client):
    client, _, domain_id, subject = signed_producer_client
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        response = client.post(
            "/internal/v1/credential-evidence",
            json=_evidence_body(org_id=domain_id, principal=subject),
            headers={"X-Adp-Edge-Provenance": "wrong"},
        )
    assert response.status_code == 403
    reader.assert_not_called()


def test_producer_stale_identity_and_binding_refuse_before_vault_read(signed_producer_client, session_factory, monkeypatch):
    from sqlalchemy import update

    from src.internal import domain_current_identity

    client, state, domain_id, subject = signed_producer_client
    body = _evidence_body(org_id=domain_id, principal=subject)
    expected = {"detail": {"error": "denied", "message": "credential evidence could not be established"}}

    async def set_membership(revoked_at):
        async with session_factory() as session:
            await session.execute(update(TenantMembership).where(TenantMembership.id == "member-alice").values(revoked_at=revoked_at))
            await session.commit()

    asyncio.run(set_membership(datetime.now(UTC)))
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        assert client.post("/internal/v1/credential-evidence", json=body).json() == expected
    reader.assert_not_called()
    asyncio.run(set_membership(None))

    original = domain_current_identity.current_human_identity

    async def wrong_identity(*args, **kwargs):
        return {**await original(*args, **kwargs), "subject": "subject-bob"}

    monkeypatch.setattr(domain_current_identity, "current_human_identity", wrong_identity)
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        assert client.post("/internal/v1/credential-evidence", json=body).json() == expected
    reader.assert_not_called()

    async def wrong_membership(*args, **kwargs):
        return {**await original(*args, **kwargs), "membership_id": "foreign-membership"}

    monkeypatch.setattr(domain_current_identity, "current_human_identity", wrong_membership)
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        assert client.post("/internal/v1/credential-evidence", json=body).json() == expected
    reader.assert_not_called()
    monkeypatch.setattr(domain_current_identity, "current_human_identity", original)

    state["binding"] = SimpleNamespace(**{**vars(state["binding"]), "adp_org_id": OTHER_ORG})
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        assert client.post("/internal/v1/credential-evidence", json=body).json() == expected
    reader.assert_not_called()


def test_signed_superplane_adapter_reads_bound_producer_evidence(signed_producer_client, session_factory, monkeypatch):
    from pathlib import Path

    import botocore.auth
    import httpx
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    # add_auth replaces X-Amz-Date, including on the offline edge's verification
    # request. Both signatures must use one instant across the thread handoff.
    signed_at = datetime(2026, 10, 18, tzinfo=UTC)
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: signed_at)

    root = Path(__file__).resolve().parents[3]
    superplane = root / "domain-apps/superplane"
    for package in ("contracts", "auth", "workspace_bootstrap", "executor", "infra/account-factory", "src/superplane-api"):
        monkeypatch.syspath_prepend(str(superplane / package))
    from superplane_contracts.connections import CredentialReference

    from app.adapters.adp_vault_client import AdpVaultClient

    client, state, domain_id, subject = signed_producer_client
    credentials = Credentials("OFFLINEACCESS", "offline-signing-secret", "offline-session-token", method="assume-role-with-web-identity")
    endpoint = "https://abcdefghij.execute-api.us-east-1.amazonaws.com/dev"
    traces = []

    def edge(request):
        signed = AWSRequest(
            method="POST",
            url=str(request.url),
            data=request.content,
            headers={
                "Content-Type": request.headers["content-type"],
                "X-Amz-Date": request.headers["x-amz-date"],
                "X-Amz-Security-Token": request.headers["x-amz-security-token"],
            },
        )
        botocore.auth.SigV4Auth(credentials, "execute-api", "us-east-1").add_auth(signed)
        assert request.headers["authorization"] == signed.headers["Authorization"]
        traces.append(request.content)
        response = client.post("/internal/v1/credential-evidence", content=request.content, headers={"Content-Type": "application/json"})
        return httpx.Response(response.status_code, json=response.json())

    adapter = AdpVaultClient(
        base_url=endpoint,
        region="us-east-1",
        session=SimpleNamespace(get_credentials=lambda: credentials),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(edge)),
    )
    reference = CredentialReference(CRED, "openai", "default")

    async def read(org_id=domain_id, principal=subject):
        return await adapter.read(org_id=org_id, workspace_id=WORKSPACE, reference=reference, principal=principal, report_digest=None)

    own = asyncio.run(read())
    assert own is not None
    assert own.ownership.owner_principal == "user:user-alice"
    assert own.authenticated_subject == subject
    assert own.authenticated_owner_principal == "user:user-alice"
    assert len(traces) == 1
    assert asyncio.run(read(org_id=OTHER_ORG)) is None
    assert asyncio.run(read(principal="subject-bob")) is None
    state["grant"] = False
    assert asyncio.run(read()) is None
    assert len(traces) == 4

    async def revoke_credential_delegation():
        from sqlalchemy import update

        async with session_factory() as session:
            await session.execute(
                update(CredentialWorkspaceDelegation).where(CredentialWorkspaceDelegation.credential_id == CRED).values(revoked_at=datetime.now(UTC))
            )
            await session.commit()

    state["grant"] = True
    asyncio.run(revoke_credential_delegation())
    owner_only = asyncio.run(read())
    assert owner_only is not None
    assert owner_only.authenticated_subject == subject
    assert owner_only.authenticated_owner_principal == owner_only.ownership.owner_principal == "user:user-alice"
    assert owner_only.ownership.delegated_to_workspaces == frozenset()
    assert len(traces) == 5

    import sys

    from src.auth.vault_evidence import VaultCredentialEvidence

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/superplane_offline_test")
    monkeypatch.setenv("SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS", "true")
    before_modules = set(sys.modules)
    try:
        from app.routers import provider_connections as consumer

        monkeypatch.setattr(consumer, "get_credential_evidence_reader", lambda: adapter)
        request = SimpleNamespace(
            state=SimpleNamespace(
                caller=SimpleNamespace(principal=SimpleNamespace(subject=subject)),
                grant=SimpleNamespace(permissions=frozenset({"workspace:renew_credential"})),
            )
        )
        consumed = asyncio.run(consumer._vault_evidence(request, domain_id, WORKSPACE, reference))
        assert consumed.ownership.owner_principal == "user:user-alice"
        assert consumed.ownership.delegated_to_workspaces == frozenset()
        assert len(traces) == 6

        reserved_reference = CredentialReference("spda1:synthetic-provider", "openai", "default")
        reserved_reading = VaultCredentialEvidence(
            org_id=domain_id,
            workspace_id=WORKSPACE,
            credential_id=reserved_reference.credential_id,
            service="openai",
            label="default",
            owner_principal="domain_app:superplane/installation",
            owner_scope="domain_app",
            delegated_to_workspaces=frozenset({WORKSPACE}),
            current_version_id=VERSION,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        with patch("src.internal.domain_provider_authority.evidence", new_callable=AsyncMock, return_value=reserved_reading) as resolver:
            reserved = asyncio.run(consumer._vault_evidence(request, domain_id, WORKSPACE, reserved_reference))
        assert reserved.ownership.owner_principal == "domain_app:superplane/installation"
        assert reserved.authenticated_subject == subject
        assert resolver.await_args.kwargs["adp_org_id"] == ORG
        assert len(traces) == 7
    finally:
        for name in set(sys.modules) - before_modules:
            if name == "app" or name.startswith("app."):
                del sys.modules[name]

    state["binding"] = SimpleNamespace(**{**vars(state["binding"]), "org_id": ORG})
    state["domain_id"] = ORG
    legacy_equal_ids = asyncio.run(read(org_id=ORG))
    assert legacy_equal_ids is not None
    assert legacy_equal_ids.org_id == ORG
    assert legacy_equal_ids.authenticated_owner_principal == "user:user-alice"
    assert len(traces) == 8


def test_reserved_producer_evidence_keeps_domain_and_adp_tenants(signed_producer_client):
    from src.auth.vault_evidence import VaultCredentialEvidence

    client, _, domain_id, subject = signed_producer_client
    reference = "spda1:synthetic-provider"
    reading = VaultCredentialEvidence(
        org_id=domain_id,
        workspace_id=WORKSPACE,
        credential_id=reference,
        service="openai",
        label="default",
        owner_principal="domain_app:superplane/installation",
        owner_scope="domain_app",
        delegated_to_workspaces=frozenset({WORKSPACE}),
        current_version_id=VERSION,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    with patch("src.internal.domain_provider_authority.evidence", new_callable=AsyncMock, return_value=reading) as resolver:
        response = client.post(
            "/internal/v1/credential-evidence",
            json=_evidence_body(
                org_id=domain_id,
                principal=subject,
                credential_id=reference,
                service="openai",
                label="default",
            ),
        )
    assert response.status_code == 200, response.json()
    assert response.json()["org_id"] == domain_id
    assert response.json()["authenticated_subject"] == subject
    assert response.json()["authenticated_owner_principal"] == "user:user-alice"
    assert resolver.await_args.kwargs == {
        "org_id": domain_id,
        "workspace_id": WORKSPACE,
        "credential_id": reference,
        "service": "openai",
        "label": "default",
        "principal": subject,
        "report_digest": None,
        "adp_org_id": ORG,
    }
    with patch("src.internal.domain_provider_authority.evidence", new_callable=AsyncMock) as resolver:
        denied = client.post(
            "/internal/v1/credential-evidence",
            json=_evidence_body(
                org_id=domain_id,
                principal="subject-bob",
                credential_id=reference,
            ),
        )
    assert denied.status_code == 403
    resolver.assert_not_awaited()


def test_run_evidence_requires_live_bound_operation(metadata_client):
    client, state = metadata_client
    state["scopes"].append("credential:evidence:read")
    body = _evidence_body(
        principal=PRINCIPAL,
        operation_id="op-1",
        job_id="job-1",
        attempt_id="att-1",
        service="openai",
        label="default",
    )
    header = {"X-Adp-Run-Credential": "offline-run"}
    own = client.post("/internal/v1/credential-evidence", json=body, headers=header)
    assert own.status_code == 200, own.json()
    assert own.json()["org_id"] == ORG
    assert own.json()["owner_principal"] == "user:user-alice"

    denied = [
        {**body, "principal": "user:user-alice"},
        {**body, "operation_id": "other-op"},
        {**body, "job_id": "other-job"},
        {**body, "attempt_id": "other-attempt"},
        {**body, "workspace_id": FOREIGN_WORKSPACE},
        {**body, "credential_id": FOREIGN_CRED},
        {**body, "service": "github"},
        {**body, "label": "different"},
        {**body, "org_id": OTHER_ORG},
        {**body, "attempt_id": None},
    ]
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        responses = [client.post("/internal/v1/credential-evidence", json=claim, headers=header) for claim in denied]
        no_credential = client.post("/internal/v1/credential-evidence", json=body)
    expected = {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    assert all(response.status_code == 403 and response.json() == expected for response in [*responses, no_credential])
    reader.assert_not_called()


def test_explicit_cross_tenant_service_needs_both_scopes_and_stored_delegation(metadata_client, session_factory):
    client, state = metadata_client
    principal = "service:metadata-reader"
    body = _evidence_body(
        org_id=OTHER_ORG,
        workspace_id=FOREIGN_WORKSPACE,
        credential_id=FOREIGN_CRED,
        principal=principal,
        operation_id="cross-op",
        job_id="cross-job",
        attempt_id="cross-att",
        service="openai",
        label="default",
    )
    expected = {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    state["scopes"].append("internal:cross-tenant")
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        not_delegated = client.post("/internal/v1/credential-evidence", json=body)
    assert not_delegated.status_code == 403 and not_delegated.json() == expected
    reader.assert_not_called()

    async def store_delegation():
        async with session_factory() as session:
            await grant_operation(
                session,
                org=OTHER_ORG,
                workspace=FOREIGN_WORKSPACE,
                holder=principal,
                operation="cross-op",
                job="cross-job",
                attempt="cross-att",
                credential=FOREIGN_CRED,
            )
            await session.commit()

    asyncio.run(store_delegation())
    allowed = client.post("/internal/v1/credential-evidence", json=body)
    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["org_id"] == OTHER_ORG
    assert allowed.json()["owner_principal"] == "user:user-bob"

    denied = [
        {**body, "principal": "user:user-bob"},
        {**body, "principal": "service:another-reader"},
        {**body, "operation_id": "foreign-op"},
        {**body, "operation_id": None},
        {**body, "attempt_id": "att-1"},
        {**body, "workspace_id": WORKSPACE},
        {**body, "credential_id": CRED},
    ]
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        responses = [client.post("/internal/v1/credential-evidence", json=claim) for claim in denied]
    assert all(response.status_code == 403 and response.json() == expected for response in responses)
    reader.assert_not_called()

    state["scopes"].remove("credential:evidence:read")
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        lacking_read = client.post("/internal/v1/credential-evidence", json=body)
    assert lacking_read.json() == expected
    reader.assert_not_called()
    state["scopes"].append("credential:evidence:read")
    state["scopes"].remove("internal:cross-tenant")
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        lacking_cross = client.post("/internal/v1/credential-evidence", json=body)
    assert lacking_cross.json() == expected
    reader.assert_not_called()
    state["scopes"].append("internal:cross-tenant")
    state["current_scopes"] = [scope for scope in state["scopes"] if scope != "internal:cross-tenant"]
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        revoked_registry_grant = client.post("/internal/v1/credential-evidence", json=body)
    assert revoked_registry_grant.json() == expected
    reader.assert_not_called()
    del state["current_scopes"]

    state["scopes"].append("credential:revocation:read")
    revocation = {
        "org_id": OTHER_ORG,
        "workspace_id": FOREIGN_WORKSPACE,
        "credential_id": FOREIGN_CRED,
        "operation_id": "cross-op",
        "job_id": "cross-job",
        "attempt_id": "cross-att",
    }
    response = client.post("/internal/v1/credential-revocation-state", json=revocation)
    assert response.status_code == 200, response.json()
    assert response.json()["admits_work"] is True
    state["scopes"].remove("internal:cross-tenant")
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        no_cross_scope = client.post("/internal/v1/credential-revocation-state", json=revocation)
    assert no_cross_scope.status_code == 403
    reader.assert_not_called()
    state["scopes"].append("internal:cross-tenant")
    assert client.post("/internal/v1/credential-revocation-state", json=revocation).json()["admits_work"] is True
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        denied = client.post("/internal/v1/credential-revocation-state", json={**revocation, "attempt_id": "att-1"})
    assert denied.status_code == 403
    reader.assert_not_called()
    state["scopes"].remove("credential:revocation:read")
    with patch("src.internal.vault_evidence_routes.revocation_state") as reader:
        lacking_revocation = client.post("/internal/v1/credential-revocation-state", json=revocation)
    assert lacking_revocation.status_code == 403
    reader.assert_not_called()

    from sqlalchemy import text

    async def expire_lease():
        async with session_factory() as session:
            await session.execute(
                text("UPDATE harness_operation_leases SET closed_at=:closed WHERE operation_id='cross-op'"), {"closed": datetime.now(UTC).isoformat()}
            )
            await session.commit()

    state["scopes"].append("credential:revocation:read")
    asyncio.run(expire_lease())
    with (
        patch("src.internal.vault_evidence_routes.read_credential_evidence") as evidence_reader,
        patch("src.internal.vault_evidence_routes.revocation_state") as revocation_reader,
    ):
        closed = client.post("/internal/v1/credential-evidence", json=body)
        closed_revocation = client.post("/internal/v1/credential-revocation-state", json=revocation)
    assert closed.json() == expected
    assert closed_revocation.status_code == 403
    evidence_reader.assert_not_called()
    revocation_reader.assert_not_called()


def test_domain_executor_evidence_keeps_domain_operation_and_adp_vault_tenants(
    metadata_client,
    session_factory,
    bootstrap_store,  # noqa: F811
    workload_verifier,  # noqa: F811
    monkeypatch,
):
    import json

    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.grants import AUTHORITY_PAID_DOMAIN_OPERATION, AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
    from src.agentauth.routes import AgentRuntime
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, mint_credential
    from src.internal import domain_operation_runtime, domain_operation_store, vault_evidence_routes
    from tests.agentauth.test_bootstrap_routes import ENV

    client, state = metadata_client
    domain_id = "00000000-0000-4000-8000-000000000002"
    registry_id = "metadata-reader"
    binding = SimpleNamespace(
        domain="superplane",
        adp_org_id=ORG,
        org_id=domain_id,
        worker_registry_id=registry_id,
        repo="org/repo",
        current_identity_enforced=False,
    )
    state["domain_org_id"] = domain_id
    state["scopes"].extend(("domain:operation-executor", "credential:revocation:read"))
    monkeypatch.setattr(vault_evidence_routes, "_verified_executor", state["actual_executor"])
    monkeypatch.setattr(domain_operation_runtime, "bindings", lambda: (binding,))
    monkeypatch.setattr(domain_operation_runtime, "binding_for", lambda domain, org: binding if (domain, org) == ("superplane", domain_id) else None)
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: state["registry"])
    monkeypatch.setattr(domain_operation_runtime, "bootstrap_store", lambda: bootstrap_store)
    monkeypatch.setattr(
        domain_operation_runtime,
        "runtime_for",
        lambda _: AgentRuntime(
            store=bootstrap_store,
            workloads=workload_verifier[0],
            env=ENV,
        ),
    )

    async def paid_operation(_binding, _operation_id, **_kwargs):
        return {
            "operation_id": "domain-op",
            "job_id": "domain-job",
            "attempt_id": "domain-att",
            "org_id": domain_id,
            "workspace_id": WORKSPACE,
            "state": "pending",
            "cancel_requested_at": None,
            "cleanup_required": False,
            "reservation_state": "confirmed",
        }

    monkeypatch.setattr(domain_operation_runtime, "paid_operation", paid_operation)
    monkeypatch.setenv(CREDENTIAL_KEY_ENV, ENV[CREDENTIAL_KEY_ENV])

    now = datetime.now(UTC)
    reference = "paid-domain:offline-operation"
    bootstrap_store.client.put_item(
        TableName=bootstrap_store.table,
        Item={
            "pk": {"S": f"TENANT#{ORG}"},
            "sk": {"S": f"AUTHORITY#{reference}"},
            "status": {"S": "active"},
            "human_id": {"S": "user-alice"},
            "authority_kind": {"S": AUTHORITY_PAID_DOMAIN_OPERATION},
        },
    )
    grant = DelegatedGrant(
        grant_id="domain-run-grant",
        tenant_id=ORG,
        principal=PRINCIPAL,
        authority=AuthorityReference(AUTHORITY_PAID_DOMAIN_OPERATION, reference, "user-alice", ORG),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({binding.repo}),
        expires_at=now + timedelta(hours=1),
    )
    envelope = {
        "message_id": "inv-abc",
        "tenant_id": ORG,
        "persona": "developer",
        "source_ref": {"repo": binding.repo, "issue": 1},
        "arrived_at": now.isoformat(),
    }
    original = {
        "domain": "superplane",
        "operation_id": "domain-op",
        "job_id": "domain-job",
        "attempt_id": "domain-att",
        "org_id": domain_id,
        "domain_org_id": domain_id,
        "adp_org_id": ORG,
        "workspace_id": WORKSPACE,
        "mode": "execution",
    }
    bootstrap_store.provision_pending(
        envelope=envelope,
        grant=grant,
        now=now,
        execution_metadata={"domain_operation": {"S": json.dumps(original)}},
    )
    bootstrap_store.bind(
        invocation_id="inv-abc",
        digest=envelope_digest(envelope),
        pod=workload_verifier[0].verify("pod-token"),
        now=now,
    )
    headers = {
        "X-Adp-Run-Credential": mint_credential(
            invocation_id="inv-abc",
            attempt=1,
            tenant_id=ORG,
            persona="developer",
            now=now,
            env=ENV,
        ),
        "X-Adp-Workload-Token": "pod-token",
    }

    @asynccontextmanager
    async def operation_session(_binding):
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(domain_operation_store, "operation_session", operation_session)

    async def store_operation():
        async with session_factory() as session:
            await grant_operation(
                session,
                org=domain_id,
                workspace=WORKSPACE,
                holder=PRINCIPAL,
                operation="domain-op",
                job="domain-job",
                attempt="domain-att",
            )
            await session.commit()

    asyncio.run(store_operation())
    body = _evidence_body(
        org_id=domain_id,
        principal=PRINCIPAL,
        operation_id="domain-op",
        job_id="domain-job",
        attempt_id="domain-att",
        service="openai",
        label="default",
    )
    evidence = client.post("/internal/v1/credential-evidence", json=body, headers=headers)
    assert evidence.status_code == 200, evidence.json()
    assert evidence.json()["org_id"] == domain_id
    assert evidence.json()["owner_principal"] == "user:user-alice"

    state["domain_org_id"] = "another-domain"
    with patch("src.internal.vault_evidence_routes.read_credential_evidence") as reader:
        denied = client.post("/internal/v1/credential-evidence", json=body, headers=headers)
    assert denied.status_code == 403
    reader.assert_not_called()
    state["domain_org_id"] = domain_id

    revocation = {
        "org_id": domain_id,
        "workspace_id": WORKSPACE,
        "credential_id": CRED,
        "operation_id": "domain-op",
        "job_id": "domain-job",
        "attempt_id": "domain-att",
    }
    response = client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers)
    assert response.status_code == 200, response.json()
    assert response.json()["admits_work"] is True

    def change_grant(revoked):
        bootstrap_store.client.update_item(
            TableName=bootstrap_store.table,
            Key={"pk": {"S": f"TENANT#{ORG}"}, "sk": {"S": f"GRANT#{PRINCIPAL}"}},
            UpdateExpression="SET revoked = :revoked",
            ExpressionAttributeValues={":revoked": {"BOOL": revoked}},
        )

    change_grant(True)
    with (
        patch("src.internal.vault_evidence_routes.read_credential_evidence") as evidence_reader,
        patch("src.internal.vault_evidence_routes.revocation_state") as revocation_reader,
    ):
        revoked_evidence = client.post("/internal/v1/credential-evidence", json=body, headers=headers)
        revoked_state = client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers)
    assert revoked_evidence.status_code == revoked_state.status_code == 403
    assert revoked_evidence.json() == {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    assert revoked_state.json() == {"detail": {"error": "denied", "message": "credential delivery refused"}}
    evidence_reader.assert_not_called()
    revocation_reader.assert_not_called()

    change_grant(False)
    assert client.post("/internal/v1/credential-evidence", json=body, headers=headers).status_code == 200
    assert client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers).json()["admits_work"] is True

    def change_assignment(assignment):
        bootstrap_store.client.update_item(
            TableName=bootstrap_store.table,
            Key={"pk": {"S": f"TENANT#{ORG}"}, "sk": {"S": "EXEC#inv-abc"}},
            UpdateExpression="SET domain_operation = :assignment",
            ExpressionAttributeValues={":assignment": {"S": json.dumps(assignment)}},
        )

    change_assignment({**original, "adp_org_id": OTHER_ORG})
    with (
        patch("src.internal.vault_evidence_routes.read_credential_evidence") as evidence_reader,
        patch("src.internal.vault_evidence_routes.revocation_state") as revocation_reader,
    ):
        wrong_assignment = client.post("/internal/v1/credential-evidence", json=body, headers=headers)
        wrong_revocation = client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers)
    assert wrong_assignment.json() == revoked_evidence.json()
    assert wrong_revocation.json() == revoked_state.json()
    evidence_reader.assert_not_called()
    revocation_reader.assert_not_called()

    change_assignment(original)
    state["scopes"].remove("domain:operation-executor")
    with (
        patch("src.internal.vault_evidence_routes.read_credential_evidence") as evidence_reader,
        patch("src.internal.vault_evidence_routes.revocation_state") as revocation_reader,
    ):
        no_capability = client.post("/internal/v1/credential-evidence", json=body, headers=headers)
        no_revocation_capability = client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers)
    assert no_capability.json() == revoked_evidence.json()
    assert no_revocation_capability.json() == revoked_state.json()
    evidence_reader.assert_not_called()
    revocation_reader.assert_not_called()
    state["scopes"].append("domain:operation-executor")
    assert client.post("/internal/v1/credential-evidence", json=body, headers=headers).status_code == 200
    assert client.post("/internal/v1/credential-revocation-state", json=revocation, headers=headers).json()["admits_work"] is True


def test_real_run_credential_and_pod_authorize_mounted_metadata(
    session_factory,
    sm,
    bootstrap_store,  # noqa: F811
    workload_verifier,  # noqa: F811
    monkeypatch,
):
    from src.agentauth.bootstrap import envelope_digest
    from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
    from src.agentauth.routes import AgentRuntime
    from src.agentauth.run_credential import mint_credential
    from src.auth import agent_registry
    from tests.agentauth.test_bootstrap_routes import ENV

    now = datetime.now(UTC)
    bootstrap_store.client.put_item(
        TableName=bootstrap_store.table,
        Item={
            "pk": {"S": f"TENANT#{ORG}"},
            "sk": {"S": "AUTHORITY#approval"},
            "status": {"S": "active"},
            "human_id": {"S": "user-alice"},
            "authority_kind": {"S": "service_policy"},
        },
    )
    grant = DelegatedGrant(
        grant_id="real-run-grant",
        tenant_id=ORG,
        principal=PRINCIPAL,
        authority=AuthorityReference("service_policy", "approval", "user-alice", ORG),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        repo_scope=frozenset({"org/repo"}),
        expires_at=now + timedelta(hours=1),
    )
    envelope = {
        "message_id": "inv-abc",
        "tenant_id": ORG,
        "persona": "developer",
        "source_ref": {"repo": "org/repo", "issue": 1},
        "arrived_at": now.isoformat(),
    }
    bootstrap_store.provision_pending(envelope=envelope, grant=grant, now=now)
    pod = workload_verifier[0].verify("pod-token")
    bootstrap_store.bind(invocation_id="inv-abc", digest=envelope_digest(envelope), pod=pod, now=now)
    runtime = AgentRuntime(store=bootstrap_store, workloads=workload_verifier[0], env=ENV)
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)

    registry_id = "metadata-executor"
    role = "arn:aws:iam::111122223333:role/metadata-executor"
    row = {
        "agent_id": registry_id,
        "agent_name": registry_id,
        "role_arn": role,
        "org_id": ORG,
        "team_id": "team-eng",
        "scope": "internal",
        "credential_scopes": ["credential:evidence:read", "credential:revocation:read", DELIVERY_SCOPE],
        "allowed_models": [],
    }
    registry = SimpleNamespace(
        get_agent_by_role_arn=lambda name: row if name == role else None,
        get_current_agent=lambda name, registered_role: row if (name, registered_role) == (registry_id, role) else None,
    )
    monkeypatch.setattr(agent_registry, "get_agent_registry_service", lambda: registry)
    monkeypatch.setattr(vault_metadata_authority, "get_agent_registry_service", lambda: registry)
    settings = MagicMock()
    settings.trust_apigw_headers = True
    settings.apigw_provenance_secret = "test-edge-provenance"
    monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.middleware.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router)

    async def db_override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_secrets_manager] = lambda: sm
    headers = {
        "X-Adp-Edge-Provenance": "test-edge-provenance",
        "X-Caller-Identity": "arn:aws:sts::111122223333:assumed-role/metadata-executor/request",
        "X-Adp-Run-Credential": mint_credential(
            invocation_id="inv-abc",
            attempt=1,
            tenant_id=ORG,
            persona="developer",
            now=now,
            env=ENV,
        ),
        "X-Adp-Workload-Token": "pod-token",
    }
    with TestClient(app, headers=headers) as client:
        evidence = client.post(
            "/internal/v1/credential-evidence",
            json=_evidence_body(
                principal=PRINCIPAL,
                operation_id="op-1",
                job_id="job-1",
                attempt_id="att-1",
                service="openai",
                label="default",
            ),
        )
        assert evidence.status_code == 200, evidence.json()
        assert evidence.json()["org_id"] == ORG
        assert evidence.json()["owner_principal"] == "user:user-alice"
        revocation = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "org_id": ORG,
                "workspace_id": WORKSPACE,
                "credential_id": CRED,
                "operation_id": "op-1",
                "job_id": "job-1",
                "attempt_id": "att-1",
            },
        )
        assert revocation.status_code == 200, revocation.json()
        assert revocation.json()["admits_work"] is True
        preflight = client.post("/internal/v1/credential-delivery/preflight", json=_delivery_body())
        assert preflight.status_code == 200, preflight.json()
        assert preflight.json()["admits_work"] is True
        sm.get_secret_at_version.assert_not_called()
        delivery = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        assert delivery.status_code == 200, delivery.json()
        assert delivery.json()["value"] == SECRET_VALUE
        sm.get_secret_at_version.assert_called_once_with(ARN, VERSION)

        bootstrap_store.client.update_item(
            TableName=bootstrap_store.table,
            Key={"pk": {"S": f"TENANT#{ORG}"}, "sk": {"S": f"GRANT#{PRINCIPAL}"}},
            UpdateExpression="SET revoked = :revoked",
            ExpressionAttributeValues={":revoked": {"BOOL": True}},
        )
        with patch("src.internal.vault_evidence_routes.deliver_credential") as reader:
            refused_preflight = client.post("/internal/v1/credential-delivery/preflight", json=_delivery_body())
            refused_delivery = client.post("/internal/v1/credential-delivery", json=_delivery_body())
        expected = {"detail": {"error": "denied", "message": "credential delivery refused"}}
        assert refused_preflight.status_code == refused_delivery.status_code == 403
        assert refused_preflight.json() == refused_delivery.json() == expected
        reader.assert_not_called()
        sm.get_secret_at_version.assert_called_once_with(ARN, VERSION)
        bootstrap_store.client.update_item(
            TableName=bootstrap_store.table,
            Key={"pk": {"S": f"TENANT#{ORG}"}, "sk": {"S": f"GRANT#{PRINCIPAL}"}},
            UpdateExpression="SET revoked = :revoked",
            ExpressionAttributeValues={":revoked": {"BOOL": False}},
        )
        assert client.post("/internal/v1/credential-delivery/preflight", json=_delivery_body()).status_code == 200

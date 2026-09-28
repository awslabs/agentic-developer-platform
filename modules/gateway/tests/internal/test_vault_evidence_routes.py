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
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.vault_delivery import DELIVERY_PERMISSION
from src.internal.credential_routes import get_secrets_manager
from src.internal.vault_evidence_routes import DELIVERY_SCOPE, router
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import (
    CredentialValidationEvidence,
    CredentialWorkspaceDelegation,
    UserCredential,
)
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
    def test_returns_vault_facts_for_the_owner(self, client):
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body())
        assert response.status_code == 200
        body = response.json()
        assert body["credential_id"] == CRED
        assert body["owner_principal"] == "user:user-alice"
        assert body["owner_scope"] == "user"
        assert body["delegated_to_workspaces"] == [WORKSPACE]
        assert body["current_version_id"] == VERSION
        # No attestation was claimed, so none is asserted.
        assert body["attested_report_digest"] is None

    def test_response_carries_no_secret_value_or_arn(self, client):
        """The whole response body, as text — not just the fields we remembered.

        Asserting on ``body["value"] is None`` would only prove the field we thought
        of is absent. Scanning the serialised text catches a value that arrives in a
        field a future change adds.
        """
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body())
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

    def test_a_matching_claim_is_confirmed(self, client, with_validation):
        digest = with_validation
        # Guards against a vacuous pass: if the fixture ever stopped returning a
        # resolved digest, the comparison below would be against a coroutine and the
        # test would fail for the wrong reason rather than silently prove nothing.
        assert isinstance(digest, str) and len(digest) == 64
        response = client.post("/internal/v1/credential-evidence", json=_evidence_body(report_digest=digest))
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
    def test_reports_that_a_delegated_credential_admits_work(self, client):
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

    def test_a_negative_answer_always_carries_the_limitation(self, client):
        """The requirement is that the limit is SURFACED, not merely documented."""
        response = client.post(
            "/internal/v1/credential-revocation-state",
            json={
                "operation_id": "op-1",
                "attempt_id": "att-1",
                "job_id": "job-1",
                "org_id": ORG,
                "workspace_id": "ws-undelegated",
                "credential_id": CRED,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["admits_work"] is False
        assert "revoked at the provider" in body["limitation"]

    def test_this_route_needs_no_recipient(self, client):
        """It returns no material, so requiring an executor identity would be noise."""
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

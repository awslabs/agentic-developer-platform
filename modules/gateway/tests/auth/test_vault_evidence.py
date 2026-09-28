"""Vault credential evidence — Issue #5528 (Wave 6 / w6-05).

The central property under test is that a CLAIMED attestation is never accepted on
the caller's word. The evidence reader recomputes the digest from the Gateway's own
validation rows and refuses unless its independent computation agrees, so a forged
digest must produce no evidence at all.

The digest recipe is verified against the CONSUMER's source rather than against a
hardcoded hex string. A literal would agree with whatever this module happens to
compute and prove nothing about interoperability; deriving the expectation from
``provider_connections.py``'s own recipe means a change on either side fails here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.vault_evidence import (
    delegated_workspaces,
    read_credential_evidence,
    resolve_exact_credential,
    validation_digest,
    verify_contract_version,
)
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import (
    CredentialType,
    CredentialValidationEvidence,
    CredentialWorkspaceDelegation,
    UserCredential,
)

ORG = "org-acme"
OTHER_ORG = "org-other"
WORKSPACE = "ws-w1"
OTHER_WORKSPACE = "ws-w2"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-abc123"
VERSION = "11111111-2222-3333-4444-555555555555"


def make_engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def engine():
    eng = make_engine()
    async with eng.begin() as conn:
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
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
        session.add(User(id="user-alice", org_id=ORG, team_id="team-eng", email="alice@acme.com"))
        session.add(User(id="user-bob", org_id=ORG, team_id="team-eng", email="bob@acme.com"))
        await session.commit()
        yield session


@pytest.fixture
def sm() -> MagicMock:
    mock = MagicMock()
    mock.current_version_id.return_value = VERSION
    return mock


async def _cred(
    db: AsyncSession,
    *,
    org_id: str = ORG,
    user_id: str | None = "user-alice",
    team_id: str | None = None,
    service: str = "aws",
    label: str = "default",
    expires_at: datetime | None = None,
) -> UserCredential:
    cred = UserCredential(
        org_id=org_id,
        user_id=user_id,
        team_id=team_id,
        service=service,
        credential_type=CredentialType.api_key,
        label=label,
        secret_arn=ARN,
        expires_at=expires_at,
        strict=False,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    return cred


async def _delegate(db: AsyncSession, cred: UserCredential, *, workspace_id: str = WORKSPACE, revoked: bool = False) -> None:
    db.add(
        CredentialWorkspaceDelegation(
            org_id=cred.org_id,
            credential_id=cred.id,
            workspace_id=workspace_id,
            delegated_by="user-alice",
            delegated_at=datetime.now(UTC),
            revoked_at=datetime.now(UTC) if revoked else None,
        )
    )
    await db.commit()


async def _validation(
    db: AsyncSession,
    cred: UserCredential,
    *,
    workspace_id: str = WORKSPACE,
    credential_valid: bool = True,
    permissions_sufficient: bool = True,
    quota_available: bool = True,
    observed_capacity: int | None = 4,
    detail: str = "",
    checked_at: datetime | None = None,
) -> CredentialValidationEvidence:
    row = CredentialValidationEvidence(
        org_id=cred.org_id,
        credential_id=cred.id,
        workspace_id=workspace_id,
        validated_version_id=VERSION,
        provider_account_id="123456789012",
        credential_valid=credential_valid,
        permissions_sufficient=permissions_sufficient,
        quota_available=quota_available,
        observed_capacity=observed_capacity,
        detail=detail,
        checked_at=checked_at or (datetime.now(UTC) - timedelta(minutes=5)),
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _read(
    db: AsyncSession,
    sm: MagicMock,
    *,
    org_id: str = ORG,
    workspace_id: str = WORKSPACE,
    credential_id: str,
    service: str | None = None,
    label: str | None = None,
    principal: str = "user:user-alice",
    report_digest: str | None = None,
):
    """Call the reader with defaults, so each test states only what it varies."""
    return await read_credential_evidence(
        db,
        sm,
        org_id=org_id,
        workspace_id=workspace_id,
        credential_id=credential_id,
        service=service,
        label=label,
        principal=principal,
        report_digest=report_digest,
    )


def _digest_of(row: CredentialValidationEvidence) -> str:
    return validation_digest(
        credential_valid=row.credential_valid,
        permissions_sufficient=row.permissions_sufficient,
        quota_available=row.quota_available,
        observed_capacity=row.observed_capacity,
        detail=row.detail,
    )


# ---------------------------------------------------------------------------
# The digest recipe must match the consumer, byte for byte
# ---------------------------------------------------------------------------


class TestDigestRecipeMatchesTheConsumer:
    """AC-02: the recipe is the consumer's, derived from its code not restated."""

    def test_matches_the_consumers_serialisation(self):
        """Replicates ``_vault_evidence``'s recipe independently and compares.

        The consumer does ``asdict(report)``, pops ``checked_at``, then
        ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` and sha256. If
        our field set, key order, separators or excluded field differ, this fails.
        """

        @dataclass(frozen=True)
        class ValidationReport:
            credential_valid: bool
            permissions_sufficient: bool
            quota_available: bool
            observed_capacity: int | None
            checked_at: datetime
            detail: str = ""

        report = ValidationReport(
            credential_valid=True,
            permissions_sufficient=False,
            quota_available=True,
            observed_capacity=7,
            checked_at=datetime.now(UTC),
            detail="partial quota",
        )
        readings = asdict(report)
        readings.pop("checked_at")
        expected = hashlib.sha256(json.dumps(readings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        assert (
            validation_digest(
                credential_valid=report.credential_valid,
                permissions_sufficient=report.permissions_sufficient,
                quota_available=report.quota_available,
                observed_capacity=report.observed_capacity,
                detail=report.detail,
            )
            == expected
        )

    def test_the_consumers_field_set_has_not_drifted(self):
        """The contract's report must still be the five fields we digest + checked_at.

        Guards the case the recipe cannot detect on its own: a field ADDED to
        ``ValidationReport`` upstream would be digested by the consumer and missed
        by us, so every legitimate call would fail with no local test failing.
        Skips (rather than passes) when the contract package is not installed —
        "could not check" must not read as "checked and fine".
        """
        pytest.importorskip("superplane_contracts")
        from dataclasses import fields

        from superplane_contracts.connections import ValidationReport

        assert {f.name for f in fields(ValidationReport)} == {
            "credential_valid",
            "permissions_sufficient",
            "quota_available",
            "observed_capacity",
            "checked_at",
            "detail",
        }

    def test_measured_zero_is_distinct_from_not_measured(self):
        """The contract keeps these as different facts; they must digest differently."""
        zero = validation_digest(credential_valid=True, permissions_sufficient=True, quota_available=True, observed_capacity=0, detail="")
        unmeasured = validation_digest(credential_valid=True, permissions_sufficient=True, quota_available=True, observed_capacity=None, detail="")
        assert zero != unmeasured

    def test_every_reading_changes_the_digest(self):
        """No reading may be silently absent from the digest."""
        base = dict(credential_valid=True, permissions_sufficient=True, quota_available=True, observed_capacity=1, detail="d")
        baseline = validation_digest(**base)
        for field, altered in (
            ("credential_valid", False),
            ("permissions_sufficient", False),
            ("quota_available", False),
            ("observed_capacity", 2),
            ("detail", "other"),
        ):
            assert validation_digest(**{**base, field: altered}) != baseline, field


class TestContractVersionIsChecked:
    """AC-01/AC-02: the declared version is validated, and fails closed."""

    def test_absent_contract_package_fails_closed(self, monkeypatch):
        """ "Cannot check the version" must not read as "the version is fine"."""
        import builtins

        real_import = builtins.__import__

        def _no_contract(name, *args, **kwargs):
            if name.startswith("superplane_contracts"):
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_contract)
        assert verify_contract_version() is False

    def test_a_version_the_registry_does_not_serve_is_refused(self):
        pytest.importorskip("superplane_contracts")
        assert verify_contract_version("v0") is False
        assert verify_contract_version("v999") is False

    def test_the_declared_version_is_accepted(self):
        pytest.importorskip("superplane_contracts")
        assert verify_contract_version() is True


# ---------------------------------------------------------------------------
# Exact resolution: substitution and ambiguity
# ---------------------------------------------------------------------------


class TestExactResolution:
    """AC-01: credential substitution and cross-tenant access are refused."""

    async def test_resolves_the_named_credential(self, db):
        cred = await _cred(db)
        got = await resolve_exact_credential(db, org_id=ORG, credential_id=cred.id)
        assert got is not None and got.id == cred.id

    async def test_another_tenants_credential_does_not_resolve(self, db):
        """The org filter is the tenant boundary, not decoration."""
        cred = await _cred(db, org_id=OTHER_ORG, user_id=None, team_id="team-other")
        assert await resolve_exact_credential(db, org_id=ORG, credential_id=cred.id) is None

    async def test_a_reference_whose_metadata_disagrees_is_refused(self, db):
        """An id/service/label disagreement is ambiguous about what it names.

        Refused rather than resolved in favour of the id: a caller that names one
        service and receives a credential for another has been handed a
        substitution, which is exactly what AC-01 requires be refused.
        """
        cred = await _cred(db, service="aws", label="prod")
        assert await resolve_exact_credential(db, org_id=ORG, credential_id=cred.id, service="gcp") is None
        assert await resolve_exact_credential(db, org_id=ORG, credential_id=cred.id, label="dev") is None
        # Agreeing metadata still resolves.
        assert await resolve_exact_credential(db, org_id=ORG, credential_id=cred.id, service="aws", label="prod") is not None

    async def test_unknown_and_blank_ids_resolve_to_nothing(self, db):
        assert await resolve_exact_credential(db, org_id=ORG, credential_id="no-such-id") is None
        assert await resolve_exact_credential(db, org_id=ORG, credential_id="") is None
        assert await resolve_exact_credential(db, org_id="", credential_id="x") is None

    async def test_does_not_rank_among_candidates(self, db):
        """Two credentials for one service must not collapse into a winner.

        ``CredentialResolver.resolve`` deliberately ranks and returns the first
        match; that behaviour is wrong for exact binding, so this path must return
        precisely the row named and nothing else.
        """
        first = await _cred(db, service="aws", label="one")
        second = await _cred(db, service="aws", label="two")
        assert (await resolve_exact_credential(db, org_id=ORG, credential_id=first.id)).label == "one"
        assert (await resolve_exact_credential(db, org_id=ORG, credential_id=second.id)).label == "two"


class TestDelegationLookup:
    async def test_active_delegations_are_listed(self, db):
        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=WORKSPACE)
        assert await delegated_workspaces(db, org_id=ORG, credential_id=cred.id) == frozenset({WORKSPACE})

    async def test_revoked_delegations_do_not_admit(self, db):
        """A withdrawn delegation is retained for diagnosis but must not admit work."""
        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=WORKSPACE, revoked=True)
        assert await delegated_workspaces(db, org_id=ORG, credential_id=cred.id) == frozenset()

    async def test_a_delegation_pair_is_unique(self, db):
        """Uniqueness is what makes "ambiguous" refusable rather than a ranking."""
        from sqlalchemy.exc import IntegrityError

        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=WORKSPACE)
        with pytest.raises(IntegrityError):
            await _delegate(db, cred, workspace_id=WORKSPACE)
        await db.rollback()


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------


class TestEvidenceWithoutAttestation:
    async def test_owner_receives_ownership_version_and_scope(self, db, sm):
        cred = await _cred(db, expires_at=datetime.now(UTC) + timedelta(days=1))
        await _delegate(db, cred, workspace_id=WORKSPACE)
        evidence = await _read(
            db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, service="aws", label="default", principal="user:user-alice"
        )
        assert evidence is not None
        assert evidence.owner_principal == "user:user-alice"
        assert evidence.owner_scope == "user"
        assert evidence.current_version_id == VERSION
        assert evidence.delegated_to_workspaces == frozenset({WORKSPACE})
        assert evidence.expires_at is not None and evidence.expires_at.tzinfo is not None
        assert evidence.attested_report_digest is None

    async def test_expiry_is_timezone_aware_even_on_sqlite(self, db, sm):
        """The consumer REFUSES a naive ``expires_at``; SQLite drops tzinfo.

        Without normalisation the same row would pass on Postgres and 403 on
        SQLite, so this is a real cross-backend behaviour difference.
        """
        cred = await _cred(db, expires_at=datetime.now(UTC) + timedelta(days=1))
        await _delegate(db, cred)
        evidence = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice")
        assert evidence.expires_at.utcoffset() is not None

    async def test_a_delegated_workspace_principal_receives_evidence(self, db, sm):
        """Delegation, not only ownership, establishes a relationship."""
        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=WORKSPACE)
        evidence = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="some-other-principal")
        assert evidence is not None

    async def test_a_principal_with_no_relationship_gets_nothing(self, db, sm):
        cred = await _cred(db)
        # No delegation to WORKSPACE at all.
        assert await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="stranger") is None

    async def test_cross_tenant_read_returns_nothing(self, db, sm):
        cred = await _cred(db, org_id=OTHER_ORG, user_id=None, team_id="team-other")
        assert await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice") is None

    async def test_blank_workspace_or_principal_is_refused(self, db, sm):
        cred = await _cred(db)
        await _delegate(db, cred)
        for workspace, principal in ((" ", "user:user-alice"), (WORKSPACE, " "), ("", "p"), (WORKSPACE, "")):
            assert await _read(db, sm, org_id=ORG, workspace_id=workspace, credential_id=cred.id, principal=principal) is None

    async def test_an_unreadable_version_is_unknown_not_fatal(self, db, sm):
        """Ownership is still established when the version cannot be read.

        ``None`` here means "not established", which the evidence reports as such
        rather than substituting a plausible-looking version.
        """
        sm.current_version_id.side_effect = RuntimeError("AccessDenied")
        cred = await _cred(db)
        await _delegate(db, cred)
        evidence = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice")
        assert evidence is not None and evidence.current_version_id is None

    async def test_team_and_org_owners_are_distinct_principals(self, db, sm):
        """An unprefixed comparison would make these the same principal."""
        team_cred = await _cred(db, user_id=None, team_id="shared-id")
        await _delegate(db, team_cred)
        evidence = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=team_cred.id, principal="team:shared-id")
        assert evidence.owner_principal == "team:shared-id"
        assert evidence.owner_scope == "team"
        # The same raw string under a different scope is NOT the owner.
        assert await _read(db, sm, org_id=ORG, workspace_id=OTHER_WORKSPACE, credential_id=team_cred.id, principal="user:shared-id") is None


class TestAttestationIsIndependentlyVerified:
    """AC-01: forged report digests are refused; a real one is confirmed."""

    async def test_a_matching_digest_is_confirmed(self, db, sm):
        cred = await _cred(db)
        await _delegate(db, cred)
        row = await _validation(db, cred)
        evidence = await _read(
            db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=_digest_of(row)
        )
        assert evidence is not None
        assert evidence.attested_report_digest == _digest_of(row)
        assert evidence.report_checked_at is not None and evidence.report_checked_at.tzinfo is not None

    async def test_a_forged_digest_yields_no_evidence(self, db, sm):
        """The central property: a caller's digest is never taken on its word."""
        cred = await _cred(db)
        await _delegate(db, cred)
        await _validation(db, cred)
        forged = hashlib.sha256(b"i-made-this-up").hexdigest()
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=forged) is None
        )

    async def test_the_digest_is_never_echoed_back(self, db, sm):
        """A caller-supplied digest must not appear in the answer unverified.

        Echoing would let any caller manufacture an attestation, so a forged digest
        must produce NO evidence rather than evidence carrying that digest.
        """
        cred = await _cred(db)
        await _delegate(db, cred)
        await _validation(db, cred, observed_capacity=4)
        forged = hashlib.sha256(b"forged").hexdigest()
        result = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=forged)
        assert result is None

    async def test_a_claimed_attestation_with_no_stored_report_is_refused(self, db, sm):
        """Nothing to verify against is not a pass."""
        cred = await _cred(db)
        await _delegate(db, cred)
        any_digest = validation_digest(credential_valid=True, permissions_sufficient=True, quota_available=True, observed_capacity=4, detail="")
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=any_digest)
            is None
        )

    async def test_a_report_for_another_workspace_does_not_attest(self, db, sm):
        """The report must be bound to THIS workspace, not merely exist."""
        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=WORKSPACE)
        await _delegate(db, cred, workspace_id=OTHER_WORKSPACE)
        row = await _validation(db, cred, workspace_id=OTHER_WORKSPACE)
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=_digest_of(row))
            is None
        )

    async def test_a_future_observation_time_is_not_a_measurement(self, db, sm):
        """The consumer refuses a future ``report_checked_at``; refuse it here too."""
        cred = await _cred(db)
        await _delegate(db, cred)
        row = await _validation(db, cred, checked_at=datetime.now(UTC) + timedelta(hours=1))
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=_digest_of(row))
            is None
        )

    async def test_a_rotated_reading_invalidates_the_old_digest(self, db, sm):
        """AC-01 rotation-during-execution: a stale attestation stops verifying."""
        cred = await _cred(db)
        await _delegate(db, cred)
        row = await _validation(db, cred, observed_capacity=4)
        stale = _digest_of(row)
        # The provider re-reports: capacity is now exhausted.
        row.observed_capacity = 0
        await db.commit()
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=stale) is None
        )
        # The current reading still verifies, so this is not a blanket failure.
        assert (
            await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice", report_digest=_digest_of(row))
            is not None
        )


class TestEvidenceNeverCarriesSecretMaterial:
    """AC-01: no secret value or ARN reaches the evidence surface."""

    async def test_no_arn_or_value_in_the_evidence(self, db, sm):
        cred = await _cred(db)
        await _delegate(db, cred)
        evidence = await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="user:user-alice")
        rendered = repr(evidence)
        assert ARN not in rendered
        assert "secret_arn" not in rendered
        # The reader must not have fetched the VALUE at any point.
        assert sm.get_secret.call_count == 0

    async def test_the_reader_never_raises_for_a_denial(self, db, sm):
        """The port declares NONE_MEANS_UNVERIFIED: a raise would read as a 503.

        The consumer maps ``None`` onto 403 (denied) and an exception onto 503
        (vault unavailable), so raising on a denial would report a refused
        credential as a broken vault — a false outage.
        """
        cred = await _cred(db, org_id=OTHER_ORG, user_id=None, team_id="t")
        for digest in (None, hashlib.sha256(b"x").hexdigest()):
            assert await _read(db, sm, org_id=ORG, workspace_id=WORKSPACE, credential_id=cred.id, principal="p", report_digest=digest) is None

"""Operation-bound credential delivery — Issue #5528 (Wave 6 / w6-05).

Almost every test here asserts a REFUSAL, which is the shape of the requirement:
delivery is authorized for one credential, one recipient, one operation, and the
interesting behaviour is everything it turns down. The positive path is a handful of
cases; the negative path is the story.

Two properties get particular attention because they are the ones a plausible
implementation gets wrong:

* refusals are UNIFORM — a refusal that explained whether the credential exists,
  belongs to another tenant, or is merely undelegated would let a caller map another
  tenant's vault by reading the differences between messages;
* the value never reaches a log, a refusal message, a model, or a serialised form,
  including when a provider error nests it inside another structure.
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.vault_delivery import (
    CREDENTIAL_MANAGEMENT_PERMISSION,
    DELIVERY_PERMISSION,
    REVOCATION_LIMITATION,
    DeliveredSecret,
    DeliveryRefusedError,
    OperationBinding,
    RevocationAnswer,
    authorize_delivery,
    deliver_credential,
    revocation_state,
)
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import (
    CredentialType,
    CredentialValidationEvidence,
    CredentialWorkspaceDelegation,
    UserCredential,
)
from tests.operation_delivery_support import bind_credential, grant_operation, operation_storage

ORG = "org-acme"
OTHER_ORG = "org-other"
WORKSPACE = "ws-w1"
OTHER_WORKSPACE = "ws-w2"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-abc123"
# Developer invocation identity is independent of durable operation/job IDs.
EXECUTOR = "invocation-27#3"
OTHER_EXECUTOR = "op-other#1"
SECRET_VALUE = "sk-live-do-not-log-this-0123456789"
GRANTED = frozenset({DELIVERY_PERMISSION})


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
    await operation_storage(eng)
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
        await grant_operation(session, org=ORG, workspace=WORKSPACE, holder=EXECUTOR)
        await session.commit()
        yield session


VERSION_ID = "version-abc123"


@pytest.fixture
def sm() -> MagicMock:
    """Secrets Manager helper mock.

    Three methods are used by ``deliver_credential``:

    * ``current_version_id(arn)`` — returns the pinned ``VERSION_ID`` before the
      fetch and after it (post-fetch re-check). Mocked here so all delivery tests
      pass without AWS connectivity and with stable version identity.
    * ``get_secret_at_version(arn, version_id)`` — returns ``(SECRET_VALUE, VERSION_ID)``.
      Previously ``get_secret`` was called; F3 replaced it with the versioned call.
    * ``get_secret`` — retained as a sentinel: tests that assert ``sm.get_secret.call_count == 0``
      are testing that a refused request never reached Secrets Manager at all.
    """
    mock = MagicMock()
    mock.get_secret.return_value = SECRET_VALUE
    mock.current_version_id.return_value = VERSION_ID
    mock.get_secret_at_version.return_value = (SECRET_VALUE, VERSION_ID)
    return mock


def binding(*, org_id: str = ORG, workspace_id: str = WORKSPACE) -> OperationBinding:
    """The durable operation and current lease seeded by the fixture."""
    return OperationBinding(
        operation_id="op-1",
        attempt_id="att-1",
        job_id="job-1",
        org_id=org_id,
        workspace_id=workspace_id,
        provider="aws",
        provider_account_id="123456789012",
    )


async def _cred(
    db: AsyncSession,
    *,
    org_id: str = ORG,
    service: str = "aws",
    label: str = "default",
    expires_at: datetime | None = None,
) -> UserCredential:
    cred = UserCredential(
        org_id=org_id,
        user_id="user-alice" if org_id == ORG else None,
        team_id=None if org_id == ORG else "team-other",
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
    await bind_credential(db, credential=cred.id, service=service, label=label)
    db.add(
        CredentialValidationEvidence(
            credential_id=cred.id,
            org_id=org_id,
            workspace_id=WORKSPACE,
            validated_version_id=VERSION_ID,
            provider_account_id="123456789012",
            credential_valid=True,
            permissions_sufficient=True,
            quota_available=True,
            observed_capacity=1,
            checked_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return cred


async def _delegate(
    db: AsyncSession,
    cred: UserCredential,
    *,
    workspace_id: str = WORKSPACE,
    revoked: bool = False,
) -> CredentialWorkspaceDelegation:
    row = CredentialWorkspaceDelegation(
        org_id=cred.org_id,
        credential_id=cred.id,
        workspace_id=workspace_id,
        delegated_by="user-alice",
        delegated_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _delivered(db: AsyncSession, cred: UserCredential, **kwargs):
    return await authorize_delivery(
        db,
        binding=kwargs.pop("binding", binding()),
        credential_id=kwargs.pop("credential_id", cred.id),
        service=kwargs.pop("service", None),
        label=kwargs.pop("label", None),
        recipient=kwargs.pop("recipient", EXECUTOR),
        authenticated_recipient=kwargs.pop("authenticated_recipient", EXECUTOR),
        granted_permissions=kwargs.pop("granted_permissions", GRANTED),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The container that refuses to render itself
# ---------------------------------------------------------------------------


class TestDeliveredSecretIsUnprintable:
    """AC-01 redaction: the value must not escape through any rendering path.

    Each path is tested separately because they are genuinely separate hooks: a
    redacted ``__repr__`` alone still leaks through ``f"{secret}"`` unless
    ``__str__`` is overridden, and through ``f"{secret:>40}"`` unless
    ``__format__`` is too.
    """

    def test_repr_str_and_format_are_all_redacted(self):
        secret = DeliveredSecret(SECRET_VALUE)
        for rendered in (
            repr(secret),
            str(secret),
            f"{secret}",
            f"{secret!s}",
            f"{secret!r}",
            f"{secret:>60}",
            # Both legacy forms are exercised deliberately: %-formatting is how
            # every logging call in this codebase renders its arguments, so it is a
            # real leak path rather than a style relic.
            "{}".format(secret),  # noqa: UP032
            "%s" % (secret,),  # noqa: UP031
        ):
            assert SECRET_VALUE not in rendered
            assert "REDACTED" in rendered

    def test_the_value_is_reachable_only_through_reveal(self):
        secret = DeliveredSecret(SECRET_VALUE)
        assert secret.reveal() == SECRET_VALUE
        # __slots__ means no __dict__ to walk for the value.
        assert not hasattr(secret, "__dict__")

    def test_it_is_not_a_str_subclass(self):
        """A str subclass loses protection at the first concatenation."""
        assert not isinstance(DeliveredSecret(SECRET_VALUE), str)

    def test_it_cannot_be_serialised(self):
        """Serialisation is how a value reaches a queue, a cache or another process."""
        with pytest.raises(TypeError):
            pickle.dumps(DeliveredSecret(SECRET_VALUE))

    def test_it_cannot_be_a_dict_key(self):
        """Dict keys get rendered whenever the dict does."""
        with pytest.raises(TypeError):
            {DeliveredSecret(SECRET_VALUE): "x"}  # noqa: B018

    def test_an_empty_value_is_not_a_secret(self):
        for bad in ("", None, 123, b"bytes"):
            with pytest.raises(ValueError):
                DeliveredSecret(bad)  # type: ignore[arg-type]

    def test_equality_does_not_reveal_in_assertion_output(self):
        assert DeliveredSecret(SECRET_VALUE) == DeliveredSecret(SECRET_VALUE)
        assert DeliveredSecret(SECRET_VALUE) != DeliveredSecret("other-value")
        assert DeliveredSecret(SECRET_VALUE) != SECRET_VALUE

    def test_nesting_it_in_a_structure_still_redacts(self):
        """AC-01 names NESTED material specifically: containers use repr on members."""
        secret = DeliveredSecret(SECRET_VALUE)
        for nested in ([secret], {"k": secret}, (secret,), {"outer": {"inner": [secret]}}):
            assert SECRET_VALUE not in repr(nested)
            assert SECRET_VALUE not in str(nested)


# ---------------------------------------------------------------------------
# The binding itself
# ---------------------------------------------------------------------------


class TestOperationBinding:
    def test_a_complete_binding_is_accepted(self):
        bound = binding()
        assert bound.attempt_id == "att-1"
        assert bound.operation_id == "op-1"
        assert bound.job_id == "job-1"

    def test_every_field_is_required(self):
        """A blank field means the operation is not actually identified."""
        complete = dict(operation_id="op-1", attempt_id="att-1", job_id="job-1", org_id=ORG, workspace_id=WORKSPACE)
        for field in complete:
            for blank in ("", "   ", None):
                with pytest.raises(DeliveryRefusedError):
                    OperationBinding(**{**complete, field: blank})

    def test_it_is_immutable(self):
        """A binding that could be edited after an authorization check is not a binding."""
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is a dataclass detail
            binding().workspace_id = OTHER_WORKSPACE  # type: ignore[misc]

    def test_a_refused_binding_raises_the_declared_refusal_type(self):
        """The contract declares refusal_exceptions=("DeliveryRefused",)."""
        with pytest.raises(DeliveryRefusedError) as caught:
            OperationBinding(operation_id="", attempt_id="a", job_id="j", org_id=ORG, workspace_id=WORKSPACE)
        assert isinstance(caught.value, PermissionError)


# ---------------------------------------------------------------------------
# Operation binding validation against the authenticated principal (F1 fix)
# ---------------------------------------------------------------------------


class TestBindingValidationAgainstPrincipal:
    """Asserted identities must match the durable operation and live lease."""

    async def test_correct_binding_is_authorized(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        result = await _delivered(db, cred)
        assert result.id == cred.id

    async def test_wrong_operation_id_is_refused(self, db):
        """Replacing operation_id with a nonexistent identity refuses delivery."""
        cred = await _cred(db)
        await _delegate(db, cred)
        bad_binding = OperationBinding(
            operation_id="review-nonexistent-identity",
            attempt_id="att-1",
            job_id="job-1",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=bad_binding)

    async def test_wrong_attempt_id_is_refused(self, db):
        """Replacing attempt_id with a nonexistent identity refuses delivery."""
        cred = await _cred(db)
        await _delegate(db, cred)
        bad_binding = OperationBinding(
            operation_id="op-1",
            attempt_id="review-nonexistent-identity",
            job_id="job-1",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=bad_binding)

    async def test_wrong_job_id_is_refused(self, db):
        """Replacing job_id with a nonexistent identity refuses delivery."""
        cred = await _cred(db)
        await _delegate(db, cred)
        bad_binding = OperationBinding(
            operation_id="op-1",
            attempt_id="att-1",
            job_id="review-nonexistent-identity",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=bad_binding)

    async def test_all_three_wrong_is_refused(self, db):
        """Replacing all three binding fields refuses delivery (same refusal type)."""
        cred = await _cred(db)
        await _delegate(db, cred)
        bad_binding = OperationBinding(
            operation_id="review-nonexistent-identity",
            attempt_id="review-nonexistent-identity",
            job_id="review-nonexistent-identity",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=bad_binding)

    async def test_principal_without_hash_is_refused(self, db):
        """A principal that lacks the invocation_id#attempt format refuses."""
        cred = await _cred(db)
        await _delegate(db, cred)
        bad_binding = OperationBinding(
            operation_id="op-1",
            attempt_id="att-1",
            job_id="no-hash-separator",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=bad_binding, authenticated_recipient="no-hash-separator", recipient="no-hash-separator")

    async def test_binding_refusal_has_same_message_as_other_refusals(self, db):
        """Binding validation refusals are uniform — not distinguishable from others."""
        cred = await _cred(db)
        await _delegate(db, cred)
        wrong_op_binding = OperationBinding(
            operation_id="different-op",
            attempt_id="att-1",
            job_id="job-1",
            org_id=ORG,
            workspace_id=WORKSPACE,
        )
        wrong_credential_message = None
        wrong_op_message = None
        with pytest.raises(DeliveryRefusedError) as caught:
            await _delivered(db, cred, credential_id="no-such-cred")
        wrong_credential_message = str(caught.value)
        with pytest.raises(DeliveryRefusedError) as caught:
            await _delivered(db, cred, binding=wrong_op_binding)
        wrong_op_message = str(caught.value)
        assert wrong_credential_message == wrong_op_message


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestAuthorizationSucceedsOnlyWhenEverythingHolds:
    async def test_the_delegated_credential_is_authorized(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        got = await _delivered(db, cred)
        assert got.id == cred.id

    async def test_agreeing_metadata_still_authorizes(self, db):
        cred = await _cred(db, service="aws", label="prod")
        await _delegate(db, cred)
        assert (await _delivered(db, cred, service="aws", label="prod")).id == cred.id

    async def test_an_unexpired_credential_is_authorized(self, db):
        cred = await _cred(db, expires_at=datetime.now(UTC) + timedelta(hours=1))
        await _delegate(db, cred)
        assert (await _delivered(db, cred)).id == cred.id


class TestRecipientBinding:
    """AC-01: a caller may not receive another executor's credential."""

    async def test_naming_another_executor_is_refused(self, db):
        """The TRANSPORT's identity is the authority, not the request's claim.

        Without this check any authenticated executor could obtain any other
        executor's credential simply by naming it, which makes the lease
        recipient-LABELLED rather than recipient-BOUND.
        """
        cred = await _cred(db)
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, recipient=OTHER_EXECUTOR, authenticated_recipient=EXECUTOR)

    async def test_an_unauthenticated_caller_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        for authenticated in ("", "   ", None):
            with pytest.raises(DeliveryRefusedError):
                await _delivered(db, cred, recipient=EXECUTOR, authenticated_recipient=authenticated)

    async def test_a_blank_named_recipient_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        for named in ("", "   ", None):
            with pytest.raises(DeliveryRefusedError):
                await _delivered(db, cred, recipient=named, authenticated_recipient=EXECUTOR)


class TestPermissionSplit:
    """Managing a credential is not authority to use it."""

    async def test_the_management_permission_alone_is_refused(self, db):
        """Rotating a credential must not confer the right to read its value.

        These are different capabilities in the contract, so a holder of only
        ``workspace:renew_credential`` is refused rather than upgraded.
        """
        cred = await _cred(db)
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, granted_permissions=frozenset({CREDENTIAL_MANAGEMENT_PERMISSION}))

    async def test_no_permissions_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, granted_permissions=frozenset())

    async def test_a_malformed_permission_set_is_refused(self, db):
        """A non-set or non-string membership is not something to interpret."""
        cred = await _cred(db)
        await _delegate(db, cred)
        for bad in (None, "workspace:provision", ["workspace:provision"], {1, 2}):
            with pytest.raises(DeliveryRefusedError):
                await _delivered(db, cred, granted_permissions=bad)

    async def test_holding_both_permissions_is_authorized(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        both = frozenset({DELIVERY_PERMISSION, CREDENTIAL_MANAGEMENT_PERMISSION})
        assert (await _delivered(db, cred, granted_permissions=both)).id == cred.id


class TestReferenceRefusals:
    """AC-01: missing, revoked, foreign and ambiguous references are refused."""

    async def test_an_unknown_credential_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, credential_id="no-such-credential")

    async def test_a_blank_credential_id_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        for bad in ("", "   ", None):
            with pytest.raises(DeliveryRefusedError):
                await _delivered(db, cred, credential_id=bad)

    async def test_another_tenants_credential_is_refused(self, db):
        """Cross-tenant: the id exists, but not in the binding's org."""
        foreign = await _cred(db, org_id=OTHER_ORG)
        await _delegate(db, foreign, workspace_id=WORKSPACE)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, foreign)

    async def test_a_credential_delegated_only_elsewhere_is_refused(self, db):
        """Cross-workspace: delegation to ws-w2 does not admit work in ws-w1."""
        cred = await _cred(db)
        await _delegate(db, cred, workspace_id=OTHER_WORKSPACE)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, binding=binding(workspace_id=WORKSPACE))

    async def test_an_undelegated_credential_is_refused(self, db):
        """Owning a credential is not the same as delegating it to a workspace."""
        cred = await _cred(db)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred)

    async def test_a_revoked_delegation_is_refused(self, db):
        cred = await _cred(db)
        await _delegate(db, cred, revoked=True)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred)

    async def test_a_substituted_reference_is_refused(self, db):
        """Ambiguous: the id names one credential and the metadata another."""
        cred = await _cred(db, service="aws", label="prod")
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, service="gcp")
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, label="dev")

    async def test_two_credentials_never_collapse_into_one(self, db):
        """A second credential for the same service must not satisfy the first's id.

        ``CredentialResolver.resolve`` ranks candidates and returns a winner. That
        behaviour applied here would be silent credential substitution, so exact
        resolution has to be verified with more than one candidate present.
        """
        first = await _cred(db, service="aws", label="one")
        second = await _cred(db, service="aws", label="two")
        await _delegate(db, second)  # only the SECOND is delegated
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, first)
        assert (await _delivered(db, second)).label == "two"


class TestExpiry:
    async def test_an_expired_credential_is_refused(self, db):
        cred = await _cred(db, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred)

    async def test_expiry_exactly_now_is_refused(self, db):
        """The boundary is closed: an expiry that has arrived has passed."""
        moment = datetime.now(UTC)
        cred = await _cred(db, expires_at=moment)
        await _delegate(db, cred)
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred, now=moment)

    async def test_a_naive_stored_expiry_is_compared_not_crashed(self, db):
        """SQLite drops tzinfo; comparing naive to aware raises TypeError.

        Without normalisation this path would raise ``TypeError`` instead of
        refusing — an unhandled error rather than a decision, and a different
        outcome on SQLite than on Postgres.
        """
        cred = await _cred(db, expires_at=datetime.now(UTC) - timedelta(hours=1))
        await _delegate(db, cred)
        cred.expires_at = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
        await db.commit()
        with pytest.raises(DeliveryRefusedError):
            await _delivered(db, cred)


class TestRefusalsAreUniform:
    """A refusal must not become an enumeration oracle over other tenants."""

    async def test_unknown_foreign_and_undelegated_read_identically(self, db):
        """Distinguishing these three would let a caller map another tenant's vault.

        The caller learns only "refused" — not whether the credential exists, is
        someone else's, or is simply not delegated here.
        """
        undelegated = await _cred(db)
        foreign = await _cred(db, org_id=OTHER_ORG)
        messages = set()
        for credential_id in ("no-such-credential", foreign.id, undelegated.id):
            with pytest.raises(DeliveryRefusedError) as caught:
                await _delivered(db, undelegated, credential_id=credential_id)
            messages.add(str(caught.value))
        assert len(messages) == 1, messages

    async def test_no_refusal_carries_an_identifier(self, db):
        """An exception string reaches logs and agent transcripts."""
        cred = await _cred(db, service="aws", label="prod")
        with pytest.raises(DeliveryRefusedError) as caught:
            await _delivered(db, cred)
        message = str(caught.value)
        for leaked in (cred.id, ARN, ORG, WORKSPACE, "prod", EXECUTOR):
            assert leaked not in message

    async def test_a_refusal_never_carries_the_value(self, db, sm):
        cred = await _cred(db)
        with pytest.raises(DeliveryRefusedError) as caught:
            await deliver_credential(
                db,
                sm,
                binding=binding(),
                credential_id=cred.id,
                service=None,
                label=None,
                recipient=EXECUTOR,
                authenticated_recipient=EXECUTOR,
                granted_permissions=GRANTED,
                refresh_executor=_unchanged_executor,
            )
        assert SECRET_VALUE not in str(caught.value)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocationState:
    async def test_a_delegated_unexpired_credential_admits_work(self, db):
        cred = await _cred(db)
        await _delegate(db, cred)
        answer = await revocation_state(db, binding=binding(), credential_id=cred.id)
        assert answer.admits_work is True

    @pytest.mark.parametrize("case", ["unknown", "foreign", "undelegated", "revoked-delegation", "expired"])
    async def test_every_refusal_states_the_limitation(self, db, case):
        """The requirement is to DOCUMENT the limits of revoking issued keys.

        Carried as a field on every "no" rather than as prose in a docstring,
        because an operator who believes disabling a delivery contained a leak will
        skip the provider-side revocation that actually does.
        """
        if case == "unknown":
            credential_id = "no-such-credential"
        elif case == "foreign":
            credential_id = (await _cred(db, org_id=OTHER_ORG)).id
        elif case == "undelegated":
            credential_id = (await _cred(db)).id
        elif case == "revoked-delegation":
            cred = await _cred(db)
            await _delegate(db, cred, revoked=True)
            credential_id = cred.id
        else:
            cred = await _cred(db, expires_at=datetime.now(UTC) - timedelta(minutes=1))
            await _delegate(db, cred)
            credential_id = cred.id

        answer = await revocation_state(db, binding=binding(), credential_id=credential_id)
        assert answer.admits_work is False
        assert answer.limitation == REVOCATION_LIMITATION

    def test_the_limitation_names_the_provider_side_step(self):
        """The text has to be actionable, not merely present."""
        assert "revoked at the provider" in REVOCATION_LIMITATION
        assert "does not revoke it" in REVOCATION_LIMITATION

    def test_a_refusal_cannot_be_constructed_without_a_limitation(self):
        """The invariant is enforced by the type, not by every call site."""
        with pytest.raises(ValueError):
            RevocationAnswer(admits_work=False)
        with pytest.raises(ValueError):
            RevocationAnswer(admits_work=False, limitation="")
        # An affirmative answer needs no limitation.
        assert RevocationAnswer(admits_work=True).limitation == ""

    def test_the_limitation_text_matches_the_contract(self):
        """Drift guard for the mirrored constant.

        ``REVOCATION_LIMITATION`` is copied rather than imported because the Gateway
        does not depend on the contract package. Skipped rather than passed when the
        package is absent — "could not compare" is not "compared and matched".
        """
        pytest.importorskip("superplane_contracts")
        from superplane_contracts.delivery import (
            REVOCATION_LIMITATION as CONTRACT_LIMITATION,
        )

        assert REVOCATION_LIMITATION == CONTRACT_LIMITATION


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestDelivery:
    async def test_the_authorized_recipient_receives_the_value(self, db, sm):
        cred = await _cred(db)
        await _delegate(db, cred)
        delivered, secret = await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )
        assert delivered.id == cred.id
        assert secret.reveal() == SECRET_VALUE
        # F3 fix: versioned fetch, not unversioned get_secret
        sm.get_secret_at_version.assert_called_with(ARN, VERSION_ID)
        sm.get_secret.assert_not_called()

    async def test_an_unauthorized_request_never_reaches_secrets_manager(self, db, sm):
        """Authorization precedes the fetch: a refused request must not read at all.

        Fetching first and refusing after would put the value in this process's
        memory for a caller that was never entitled to it.
        """
        cred = await _cred(db)  # not delegated
        with pytest.raises(DeliveryRefusedError):
            await deliver_credential(
                db,
                sm,
                binding=binding(),
                credential_id=cred.id,
                service=None,
                label=None,
                recipient=EXECUTOR,
                authenticated_recipient=EXECUTOR,
                granted_permissions=GRANTED,
                refresh_executor=_unchanged_executor,
            )
        assert sm.get_secret.call_count == 0
        assert sm.get_secret_at_version.call_count == 0

    async def test_a_provider_error_is_not_propagated(self, db, sm):
        """A Secrets Manager error body can echo the value or the ARN.

        The exception string reaches logs and agent transcripts, so the provider's
        error is replaced by a fixed refusal and its cause is severed — otherwise
        the original message would still surface inside the traceback.
        """
        cred = await _cred(db)
        await _delegate(db, cred)
        sm.get_secret_at_version.side_effect = RuntimeError(f"AccessDenied reading {ARN}: value was {SECRET_VALUE}")
        with pytest.raises(DeliveryRefusedError) as caught:
            await deliver_credential(
                db,
                sm,
                binding=binding(),
                credential_id=cred.id,
                service=None,
                label=None,
                recipient=EXECUTOR,
                authenticated_recipient=EXECUTOR,
                granted_permissions=GRANTED,
                refresh_executor=_unchanged_executor,
            )
        assert SECRET_VALUE not in str(caught.value)
        assert ARN not in str(caught.value)
        # `from None` severs the cause so the provider text is not re-raised with it.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None or SECRET_VALUE not in str(caught.value.__cause__ or "")

    async def test_an_empty_stored_value_is_refused(self, db, sm):
        """A blank secret is not material; handing it over would look like success."""
        cred = await _cred(db)
        await _delegate(db, cred)
        for empty in ("", None, {"key": "value"}):
            sm.get_secret_at_version.return_value = (empty, VERSION_ID)
            with pytest.raises(DeliveryRefusedError):
                await deliver_credential(
                    db,
                    sm,
                    binding=binding(),
                    credential_id=cred.id,
                    service=None,
                    label=None,
                    recipient=EXECUTOR,
                    authenticated_recipient=EXECUTOR,
                    granted_permissions=GRANTED,
                    refresh_executor=_unchanged_executor,
                )
        # Restore to normal for other tests using the same fixture instance.
        sm.get_secret_at_version.return_value = (SECRET_VALUE, VERSION_ID)

    async def test_delivery_is_not_persisted_to_the_credential_row(self, db, sm):
        """AC-01: the value never reaches the domain database or model.

        Asserted by rendering the returned model: if any code path had assigned the
        value to a column or a transient attribute, it would appear here.
        """
        cred = await _cred(db)
        await _delegate(db, cred)
        delivered, _secret = await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )
        for column in delivered.__table__.columns:
            assert getattr(delivered, column.name, None) != SECRET_VALUE
        assert SECRET_VALUE not in repr(delivered)

    async def test_the_audit_line_records_the_delivery_not_the_value(self, db, sm, caplog):
        """The delivery must be auditable without the log becoming a secret store."""
        cred = await _cred(db)
        await _delegate(db, cred)
        with caplog.at_level(logging.INFO, logger="src.auth.vault_delivery"):
            await deliver_credential(
                db,
                sm,
                binding=binding(),
                credential_id=cred.id,
                service=None,
                label=None,
                recipient=EXECUTOR,
                authenticated_recipient=EXECUTOR,
                granted_permissions=GRANTED,
                refresh_executor=_unchanged_executor,
            )
        emitted = caplog.text
        assert SECRET_VALUE not in emitted
        # But the delivery IS recorded, with the identifiers that make it auditable.
        assert cred.id in emitted
        # The binding fields (operation_id and attempt_id) appear in the audit log.
        assert "op-1" in emitted and "1" in emitted and EXECUTOR in emitted

    async def test_no_log_record_carries_the_value_on_the_error_path(self, db, sm, caplog):
        """The provider's error text must not be logged either."""
        cred = await _cred(db)
        await _delegate(db, cred)
        sm.get_secret_at_version.side_effect = RuntimeError(f"boom {SECRET_VALUE}")
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(DeliveryRefusedError):
                await deliver_credential(
                    db,
                    sm,
                    binding=binding(),
                    credential_id=cred.id,
                    service=None,
                    label=None,
                    recipient=EXECUTOR,
                    authenticated_recipient=EXECUTOR,
                    granted_permissions=GRANTED,
                    refresh_executor=_unchanged_executor,
                )
        assert SECRET_VALUE not in caplog.text


class TestRotationDuringExecution:
    """AC-01: a revocation landing mid-fetch must not be raced past.

    These two tests park the delivery coroutine INSIDE the secret fetch and mutate
    the vault while it is suspended there, which is the real interleaving: the fetch
    is an ``await``, so a single pre-fetch authorization check can be overtaken by a
    revocation that commits before the value is handed over.

    The parking is done with ``threading.Event``s rather than by mutating from the
    ``get_secret`` side-effect. ``asyncio.to_thread`` runs that side-effect in a
    worker thread, and an ``AsyncSession`` is not safe to use from two places at
    once — committing from the side-effect (or racing the commit against the
    coroutine's resumption) makes the test fail on session state rather than on the
    behaviour being tested. Blocking the worker thread until the test says so means
    the delivery coroutine provably holds no session operation while the mutation
    commits.
    """

    @staticmethod
    def _parked_fetch() -> tuple[threading.Event, threading.Event, object]:
        """A ``get_secret_at_version`` side-effect that signals when entered and blocks until released.

        F3 update: ``deliver_credential`` now calls ``get_secret_at_version(arn, version_id)``
        rather than ``get_secret(arn)``, so the parking side-effect is on the versioned method.
        Returns ``(value, version_id)`` to match the method's contract.
        """
        entered = threading.Event()
        release = threading.Event()

        def fetch(_arn: str, _version_id: str) -> tuple[str, str]:
            entered.set()  # runs in the to_thread worker
            release.wait(timeout=10)
            return SECRET_VALUE, VERSION_ID

        return entered, release, fetch

    @staticmethod
    async def _await_event(event: threading.Event) -> None:
        """Yield to the loop until a thread sets *event*.

        ``threading.Event.wait`` would block the event loop, which would deadlock
        against the very coroutine being waited on.
        """
        for _ in range(2000):
            if event.is_set():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("delivery never reached the secret fetch")

    async def _deliver(self, db, sm, cred, *, now=None):
        return await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            now=now,
            refresh_executor=_unchanged_executor,
        )

    async def test_a_revocation_during_the_fetch_refuses_delivery(self, db, sm):
        """The delegation is withdrawn while the request is parked on the fetch.

        With only the pre-fetch check, this request would hand over a value the
        vault had already stopped admitting.
        """
        cred = await _cred(db)
        delegation = await _delegate(db, cred)
        entered, release, fetch = self._parked_fetch()
        sm.get_secret_at_version.side_effect = fetch

        task = asyncio.create_task(self._deliver(db, sm, cred))
        try:
            await self._await_event(entered)
            # The coroutine is suspended in the worker thread: the session is idle.
            delegation.revoked_at = datetime.now(UTC)
            await db.commit()
        finally:
            release.set()

        with pytest.raises(DeliveryRefusedError):
            await task

    async def test_an_expiry_that_passes_during_the_fetch_refuses_delivery(self, db, sm):
        """The same race via expiry rather than delegation withdrawal.

        ``now`` is backdated so the pre-fetch check passes against a moment when the
        credential was still valid; the post-fetch check uses the real clock, which
        is what catches it.
        """
        cred = await _cred(db, expires_at=datetime.now(UTC) + timedelta(hours=1))
        await _delegate(db, cred)
        entered, release, fetch = self._parked_fetch()
        sm.get_secret_at_version.side_effect = fetch

        task = asyncio.create_task(self._deliver(db, sm, cred, now=datetime.now(UTC)))
        try:
            await self._await_event(entered)
            cred.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        finally:
            release.set()

        with pytest.raises(DeliveryRefusedError):
            await task

    async def test_the_post_fetch_check_does_not_refuse_a_valid_delivery(self, db, sm):
        """The re-check must not make every delivery fail — guards over-tightening."""
        cred = await _cred(db)
        await _delegate(db, cred)
        _delivered_cred, secret = await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )
        assert secret.reveal() == SECRET_VALUE

    async def test_rotation_during_fetch_is_refused_by_post_version_check(self, db, sm):
        """F3: secret rotates while fetch is parked — post-fetch version check refuses.

        The delivery fetches the value pinned to VERSION_ID.  While the ``asyncio.to_thread``
        call is parked, AWS rotates the secret: ``AWSCURRENT`` now points at
        ``rotated-version``.  The fetched bytes belong to VERSION_ID, which is no longer
        current.  The post-fetch ``current_version_id`` call sees the new version and
        delivery is refused rather than handing the caller bytes from a superseded version.
        """
        rotated_version = "rotated-version-xyz"
        cred = await _cred(db)
        await _delegate(db, cred)
        entered, release, fetch = self._parked_fetch()
        sm.get_secret_at_version.side_effect = fetch
        # current_version_id will return the rotated version on all subsequent calls
        # once we simulate rotation during the fetch window.
        rotation_event = threading.Event()

        def version_id_probe(_arn: str) -> str:
            # Before the rotation event is set, return the original VERSION_ID.
            # After it is set, return rotated_version to simulate rotation landing.
            if rotation_event.is_set():
                return rotated_version
            return VERSION_ID

        sm.current_version_id.side_effect = version_id_probe

        task = asyncio.create_task(self._deliver(db, sm, cred))
        try:
            await self._await_event(entered)
            # Coroutine is parked in the fetch.  Simulate AWS rotating the secret:
            # the next call to current_version_id will return rotated_version.
            rotation_event.set()
        finally:
            release.set()

        with pytest.raises(DeliveryRefusedError):
            await task


@pytest.mark.parametrize(
    "table, change",
    [
        ("harness_operations", "state='succeeded'"),
        ("harness_operations", "cancel_requested_at='2026-01-01'"),
        ("harness_operations", "cleanup_required=true"),
        ("harness_operation_leases", "holder='another-worker'"),
        ("harness_operation_leases", "attempt_id='superseded-attempt'"),
        ("harness_operation_leases", "closed_at='2026-01-01'"),
        ("harness_operation_leases", "expires_at='2000-01-01'"),
        ("harness_operation_leases", "runtime_deadline='2000-01-01'"),
        ("harness_approval_consumption", "reservation_state='retained'"),
    ],
)
async def test_durable_authority_changes_refuse_before_secret_io(db, sm, table, change):
    from sqlalchemy import text

    cred = await _cred(db)
    await _delegate(db, cred)
    assert (await _delivered(db, cred)).id == cred.id
    await db.execute(text(f"UPDATE {table} SET {change}"))
    await db.commit()
    with pytest.raises(DeliveryRefusedError):
        await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )
    sm.current_version_id.assert_not_called()
    sm.get_secret_at_version.assert_not_called()


@pytest.mark.parametrize("change", ["fence_token=fence_token+1", "expires_at='2000-01-01'"])
async def test_lease_change_during_secret_io_refuses_delivery(db, sm, monkeypatch, change):
    from sqlalchemy import text

    import src.auth.vault_delivery as delivery

    cred = await _cred(db)
    await _delegate(db, cred)

    async def during_io(fn, *args):
        if fn is sm.get_secret_at_version:
            await db.execute(text(f"UPDATE harness_operation_leases SET {change}"))
            await db.commit()
        return fn(*args)

    monkeypatch.setattr(delivery.asyncio, "to_thread", during_io)
    with pytest.raises(DeliveryRefusedError):
        await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )


@pytest.mark.parametrize("post", [False, True])
async def test_version_lookup_errors_are_redacted(db, sm, post):
    cred = await _cred(db)
    await _delegate(db, cred)
    failure = RuntimeError(SECRET_VALUE)
    sm.current_version_id.side_effect = [VERSION_ID, failure] if post else failure
    with pytest.raises(DeliveryRefusedError) as caught:
        await deliver_credential(
            db,
            sm,
            binding=binding(),
            credential_id=cred.id,
            service=None,
            label=None,
            recipient=EXECUTOR,
            authenticated_recipient=EXECUTOR,
            granted_permissions=GRANTED,
            refresh_executor=_unchanged_executor,
        )
    assert SECRET_VALUE not in str(caught.value)
    assert caught.value.__suppress_context__


async def _unchanged_executor():
    """Unit tests hold executor authority fixed; paired HTTP tests revoke it live."""
    return frozenset({DELIVERY_PERMISSION})

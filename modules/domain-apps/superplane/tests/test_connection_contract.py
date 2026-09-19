"""The provider-connection and workspace-binding contract's rules.

Issue #5047 (U7), EPIC #4910. R7 acceptances 1-5, the Implemented half.

Each test names the acceptance criterion it holds up. Several assert *absences* —
no aggregate validity field, no delete-then-rotate path, no ARN in a log — because
an absence nobody watches gets filled in by the next edit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    DISABLEMENT_LIMITATION,
    RENEW_CREDENTIAL_PERMISSION,
    ConnectionState,
    ConnectionStatus,
    ContractViolation,
    CredentialReference,
    ValidationReport,
    VaultOwnership,
    WorkspaceBinding,
    accept_connection_request,
    activate,
    authorize_delegation,
    authorize_use,
    connection_response,
    disable,
    install_log_redaction,
    rotate,
    scrub,
    validation_response,
)

CHECKED_AT = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
W1 = "ws-w1"
W2 = "ws-w2"
OWNER = "user-owner"
OTHER = "user-other"

# Test-only strings shaped like real credentials. They authenticate nothing; they
# exist so the refusal and redaction rules are exercised against realistic shapes
# rather than against the literal word "secret", which any implementation catches.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"[:16]
FAKE_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/cred-abc"


def _reference(credential_id: str = "cred-1") -> CredentialReference:
    return CredentialReference(
        credential_id=credential_id, service="aws", label="prod-account"
    )


def _binding(credential_id: str = "cred-1", workspace: str = W1) -> WorkspaceBinding:
    return WorkspaceBinding(
        credential_id=credential_id,
        workspace_id=workspace,
        bound_by=OWNER,
        bound_at=CHECKED_AT,
    )


def _report(
    *,
    valid: bool = True,
    permitted: bool = True,
    quota: bool = True,
    capacity: int | None = 4,
    detail: str = "",
) -> ValidationReport:
    return ValidationReport(
        credential_valid=valid,
        permissions_sufficient=permitted,
        quota_available=quota,
        observed_capacity=capacity,
        checked_at=CHECKED_AT,
        detail=detail,
    )


def _connection(
    *,
    status: ConnectionStatus = ConnectionStatus.PENDING,
    validation: ValidationReport | None = None,
    credential_id: str = "cred-1",
    workspace: str = W1,
) -> ConnectionState:
    return ConnectionState(
        connection_id="conn-1",
        provider="aws",
        reference=_reference(credential_id),
        binding=_binding(credential_id, workspace),
        status=status,
        validation=validation,
        limitation=DISABLEMENT_LIMITATION
        if status is ConnectionStatus.DISABLED
        else "",
    )


# ---------------------------------------------------------------------------
# Acceptance 1 — a value is refused, a reference is accepted
# ---------------------------------------------------------------------------


def test_reference_payload_is_accepted() -> None:
    """The supported shape: a credential id plus non-secret metadata."""
    reference = accept_connection_request(
        {"credential_id": "cred-1", "service": "aws", "label": "prod-account"}
    )
    assert reference.credential_id == "cred-1"


@pytest.mark.parametrize(
    "field",
    ["value", "secret", "secret_access_key", "api_key", "password", "session_token"],
)
def test_payload_carrying_a_secret_named_field_is_refused(field: str) -> None:
    """acc. 1: a payload containing a secret value is refused, not scrubbed.

    Parameterized over the field names a provider onboarding form actually
    produces. A single-name test would pass against an implementation that
    special-cased `value`.
    """
    with pytest.raises(ContractViolation) as exc:
        accept_connection_request(
            {
                "credential_id": "cred-1",
                "service": "aws",
                "label": "prod-account",
                field: "some-provider-secret",
            }
        )
    assert field in str(exc.value)


def test_secret_under_an_innocuous_key_is_refused() -> None:
    """A secret-shaped value is caught even when the key looks harmless.

    This is the case key-name matching structurally cannot see, so it needs its own
    test: `{"note": "AKIA..."}` has no suspicious key at all.
    """
    with pytest.raises(ContractViolation):
        accept_connection_request(
            {
                "credential_id": "cred-1",
                "service": "aws",
                "label": "prod-account",
                "note": f"use {FAKE_AWS_KEY} for now",
            }
        )


def test_nested_secret_is_refused() -> None:
    """Recursion matters: a value nested in a provider blob is still a value."""
    with pytest.raises(ContractViolation):
        accept_connection_request(
            {
                "credential_id": "cred-1",
                "service": "aws",
                "label": "prod-account",
                "provider": {"config": [{"access_key": FAKE_AWS_KEY}]},
            }
        )


def test_secret_named_field_is_refused_even_when_empty() -> None:
    """`{"secret": None}` is refused too.

    Accepting it would make the *shape* of a secret-carrying payload legal, and the
    next caller fills the field in.
    """
    with pytest.raises(ContractViolation):
        accept_connection_request(
            {
                "credential_id": "cred-1",
                "service": "aws",
                "label": "prod-account",
                "secret": None,
            }
        )


def test_reference_refuses_an_arn() -> None:
    """acc. 4: the reference is the vault's id, never an ARN.

    An ARN names account, region and secret, so it plus any over-broad IAM policy
    completes the read — and it usually survives rotation, so leaking it is durable.
    """
    with pytest.raises(ContractViolation, match="not an ARN"):
        CredentialReference(credential_id=FAKE_ARN, service="aws", label="prod")


# ---------------------------------------------------------------------------
# Acceptance 2 — ownership is checked before a reference is accepted
# ---------------------------------------------------------------------------


def test_owner_with_the_permission_may_delegate() -> None:
    ownership = VaultOwnership(credential_id="cred-1", owner_principal=OWNER)
    decision = authorize_delegation(
        principal=OWNER,
        workspace_id=W1,
        ownership=ownership,
        reference=_reference(),
        granted_permissions={RENEW_CREDENTIAL_PERMISSION},
    )
    assert decision.allowed


def test_delegation_without_ownership_is_refused() -> None:
    """acc. 2: holding the permission is not owning the credential.

    The failure this prevents: a workspace admin delegates a credential they were
    never authorized to use or share.
    """
    ownership = VaultOwnership(credential_id="cred-1", owner_principal=OWNER)
    decision = authorize_delegation(
        principal=OTHER,
        workspace_id=W1,
        ownership=ownership,
        reference=_reference(),
        granted_permissions={RENEW_CREDENTIAL_PERMISSION},
    )
    assert not decision.allowed


def test_owner_without_the_permission_is_refused() -> None:
    """Owning the credential is not authority over the target workspace either.

    Both conditions are required, so this is the mirror of the test above.
    """
    ownership = VaultOwnership(credential_id="cred-1", owner_principal=OWNER)
    decision = authorize_delegation(
        principal=OWNER,
        workspace_id=W1,
        ownership=ownership,
        reference=_reference(),
        granted_permissions=frozenset(),
    )
    assert not decision.allowed


def test_unresolved_ownership_is_a_denial() -> None:
    """A failed vault lookup must not become permission."""
    decision = authorize_delegation(
        principal=OWNER,
        workspace_id=W1,
        ownership=None,
        reference=_reference(),
        granted_permissions={RENEW_CREDENTIAL_PERMISSION},
    )
    assert not decision.allowed


def test_ownership_record_for_a_different_credential_is_refused() -> None:
    """The ownership record must be about the credential being delegated.

    Otherwise a caller pairs their own credential's ownership record with a
    reference to someone else's.
    """
    ownership = VaultOwnership(credential_id="cred-other", owner_principal=OWNER)
    decision = authorize_delegation(
        principal=OWNER,
        workspace_id=W1,
        ownership=ownership,
        reference=_reference("cred-1"),
        granted_permissions={RENEW_CREDENTIAL_PERMISSION},
    )
    assert not decision.allowed


def test_delegation_refusals_do_not_distinguish_absent_from_forbidden() -> None:
    """Every refusal reads the same, so denials cannot enumerate the estate."""
    reasons = {
        authorize_delegation(
            principal=principal,
            workspace_id=W1,
            ownership=ownership,
            reference=_reference("cred-1"),
            granted_permissions=permissions,
        ).reason
        for principal, ownership, permissions in (
            (
                OTHER,
                VaultOwnership(credential_id="cred-1", owner_principal=OWNER),
                {RENEW_CREDENTIAL_PERMISSION},
            ),
            (OWNER, None, {RENEW_CREDENTIAL_PERMISSION}),
            (
                OWNER,
                VaultOwnership(credential_id="cred-other", owner_principal=OWNER),
                {RENEW_CREDENTIAL_PERMISSION},
            ),
            (
                OWNER,
                VaultOwnership(credential_id="cred-1", owner_principal=OWNER),
                frozenset(),
            ),
        )
    }
    assert len(reasons) == 1


def test_permission_name_matches_u9s_authorization_model() -> None:
    """The pinned permission string must equal U9's enum member.

    The contract deliberately does not import `superplane_auth` (the packages ship
    separately), so this test is what keeps the two from drifting: a rename upstream
    fails here rather than silently detaching the ownership check from the model.
    """
    import importlib.util
    import pathlib
    import sys

    policy_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "auth"
        / "superplane_auth"
        / "policy.py"
    )
    spec = importlib.util.spec_from_file_location("_u9_policy", policy_path)
    assert spec is not None and spec.loader is not None
    policy = importlib.util.module_from_spec(spec)
    # Registered in `sys.modules` *before* exec: U9's module defines dataclasses
    # with string annotations, and `dataclasses` resolves those by looking the
    # defining module up in `sys.modules`. Executing an unregistered module makes
    # that lookup return None and raise inside `dataclasses`, which looks like a
    # bug in U9 rather than in this loader.
    sys.modules[spec.name] = policy
    try:
        spec.loader.exec_module(policy)
        assert RENEW_CREDENTIAL_PERMISSION == policy.Permission.RENEW_CREDENTIAL.value
    finally:
        sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# The binding half — where a credential may be used
# ---------------------------------------------------------------------------


def test_bound_workspace_may_use_an_active_connection() -> None:
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    decision = authorize_use(workspace_id=W1, connection=connection, binding=_binding())
    assert decision.allowed


def test_unbound_workspace_may_not_use_the_connection() -> None:
    """Same-org possession is not a binding: W2 has no binding, so W2 is refused."""
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    decision = authorize_use(
        workspace_id=W2, connection=connection, binding=_binding(workspace=W2)
    )
    assert not decision.allowed


def test_missing_binding_is_a_denial() -> None:
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    decision = authorize_use(workspace_id=W1, connection=connection, binding=None)
    assert not decision.allowed


def test_binding_for_a_different_credential_is_a_denial() -> None:
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    decision = authorize_use(
        workspace_id=W1, connection=connection, binding=_binding("cred-elsewhere")
    )
    assert not decision.allowed


def test_delegation_authority_does_not_confer_use() -> None:
    """The two checks are independent, and this is the direction that matters.

    A principal authorized to delegate a credential has not thereby made it usable
    anywhere: `authorize_use` consults only the binding, and the connection here has
    no binding for W2 even though its owner could delegate it.
    """
    ownership = VaultOwnership(credential_id="cred-1", owner_principal=OWNER)
    assert authorize_delegation(
        principal=OWNER,
        workspace_id=W2,
        ownership=ownership,
        reference=_reference(),
        granted_permissions={RENEW_CREDENTIAL_PERMISSION},
    ).allowed

    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    assert not authorize_use(
        workspace_id=W2, connection=connection, binding=_binding(workspace=W2)
    ).allowed


def test_connection_refuses_a_binding_for_another_credential() -> None:
    """A connection cannot be constructed whose binding is about a different key."""
    with pytest.raises(ContractViolation, match="does not bind"):
        ConnectionState(
            connection_id="conn-1",
            provider="aws",
            reference=_reference("cred-1"),
            binding=_binding("cred-2"),
        )


# ---------------------------------------------------------------------------
# Acceptance 3 — validity, permissions, quota and capacity stay separate
# ---------------------------------------------------------------------------


def test_report_has_four_separate_fields_and_no_aggregate() -> None:
    """acc. 3: the four readings are four fields, with no collapsing boolean.

    Asserted as an absence because an aggregate is the exact conflation the
    criterion forbids, and it is the kind of convenience field a later edit adds.
    """
    report = _report()
    for field in (
        "credential_valid",
        "permissions_sufficient",
        "quota_available",
        "observed_capacity",
    ):
        assert hasattr(report, field)
    for banned in ("ok", "healthy", "ready", "available", "valid", "usable"):
        assert not hasattr(report, banned), (
            f"ValidationReport must not aggregate via {banned!r}"
        )


def test_a_valid_credential_with_no_capacity_cannot_admit() -> None:
    """acc. 3's failure mode, directly: valid key, zero free GPUs, admission refused."""
    report = _report(capacity=0)
    assert report.validated
    assert not report.is_usable_for_admission()


def test_unmeasured_capacity_is_not_available_capacity() -> None:
    """`None` capacity means "not measured" and cannot admit.

    Distinct from measured-zero so an operator can tell "we did not look" from
    "there is nothing free"; both refuse admission.
    """
    report = _report(capacity=None)
    assert report.validated
    assert not report.is_usable_for_admission()
    assert validation_response(report)["observed_capacity"] is None


def test_capacity_below_the_requirement_refuses_admission() -> None:
    report = _report(capacity=2)
    assert report.is_usable_for_admission(required_capacity=2)
    assert not report.is_usable_for_admission(required_capacity=3)


def test_quota_and_capacity_are_independent() -> None:
    """In quota with nothing free, and out of quota with capacity, both refuse."""
    assert not _report(quota=True, capacity=0).is_usable_for_admission()
    assert not _report(quota=False, capacity=8).is_usable_for_admission()


def test_permissions_cannot_be_established_without_validity() -> None:
    """A credential that does not authenticate cannot have proven its permissions."""
    with pytest.raises(ContractViolation):
        _report(valid=False, permitted=True)


def test_validation_response_keeps_the_four_readings_separate() -> None:
    """The wire form must not re-aggregate what the type kept apart."""
    body = validation_response(_report(capacity=3))
    for field in (
        "credential_valid",
        "permissions_sufficient",
        "quota_available",
        "observed_capacity",
    ):
        assert field in body
    assert "ok" not in body and "status" not in body


# ---------------------------------------------------------------------------
# Acceptance 4 — no response body or log line carries a value or an ARN
# ---------------------------------------------------------------------------


def test_response_body_carries_no_value_and_no_arn() -> None:
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    body = repr(connection_response(connection))
    assert FAKE_AWS_KEY not in body
    assert "arn:" not in body
    assert "cred-1" in body  # the opaque id is safe and is what a caller needs


def test_response_body_is_an_allowlist_not_a_dump() -> None:
    """A field added to the state later must not be published by default."""
    body = connection_response(
        _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    )
    assert set(body) <= {
        "connection_id",
        "provider",
        "status",
        "workspace_id",
        "credential",
        "binding",
        "admits_new_work",
        "allows_renewal",
        "validation",
        "limitation",
    }
    assert set(body["credential"]) == {"credential_id", "service", "label"}


def test_provider_detail_containing_a_secret_is_withheld_from_the_response() -> None:
    """A provider error string is untrusted input on the way out."""
    report = ValidationReport(
        credential_valid=False,
        permissions_sufficient=False,
        quota_available=False,
        observed_capacity=None,
        checked_at=CHECKED_AT,
        detail="",
    )
    # Constructed empty (the contract refuses secret detail at construction), then
    # the emission guard is exercised directly on the field it protects.
    leaked = report.__class__.__new__(report.__class__)
    object.__setattr__(leaked, "credential_valid", False)
    object.__setattr__(leaked, "permissions_sufficient", False)
    object.__setattr__(leaked, "quota_available", False)
    object.__setattr__(leaked, "observed_capacity", None)
    object.__setattr__(leaked, "checked_at", CHECKED_AT)
    object.__setattr__(leaked, "detail", f"denied for {FAKE_AWS_KEY}")
    body = validation_response(leaked)
    assert FAKE_AWS_KEY not in repr(body)


def test_validation_report_refuses_secret_detail_at_construction() -> None:
    with pytest.raises(ContractViolation):
        _report(detail=f"provider said {FAKE_AWS_KEY}")


def test_no_log_line_contains_a_value_or_an_arn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """acc. 4, the log half — the surface where leaks actually happen.

    Exercises the interpolated-argument case (`logger.info("%s", payload)`), which
    is what a response-model review cannot catch.
    """
    logger = logging.getLogger("superplane.test.connection")
    logger.propagate = True
    install_log_redaction(logger)

    with caplog.at_level(logging.INFO, logger="superplane.test.connection"):
        logger.info("connection state=%s", {"secret_access_key": FAKE_AWS_KEY})
        logger.info("vault pointer %s", FAKE_ARN)
        logger.info("raw value %s", FAKE_AWS_KEY)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_AWS_KEY not in emitted
    assert FAKE_ARN not in emitted
    assert "[REDACTED]" in emitted


def test_log_redaction_does_not_drop_records(caplog: pytest.LogCaptureFixture) -> None:
    """Redaction must not trade a disclosure for a blind spot at incident time."""
    logger = logging.getLogger("superplane.test.connection.keep")
    install_log_redaction(logger)
    with caplog.at_level(logging.INFO, logger="superplane.test.connection.keep"):
        logger.info("capacity=%s cost=%s", 4, 1.006)
    assert len(caplog.records) == 1
    assert "capacity=4" in caplog.records[0].getMessage()


def test_log_redaction_is_idempotent() -> None:
    """Repeated setup must not stack filters that rescrub every record."""
    logger = logging.getLogger("superplane.test.connection.idem")
    first = install_log_redaction(logger)
    second = install_log_redaction(logger)
    assert first is second


def test_a_secret_in_the_message_template_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The f-string case: `logger.info(f"key={value}")`, with no args at all.

    Distinct from the interpolated-argument case above and more common in practice,
    because an f-string is what someone writes when adding a log line quickly. A
    filter that only scrubbed `record.args` would leave this one untouched.
    """
    logger = logging.getLogger("superplane.test.connection.template")
    logger.propagate = True
    install_log_redaction(logger)
    with caplog.at_level(logging.INFO, logger="superplane.test.connection.template"):
        logger.info("registering key=%s" % FAKE_AWS_KEY)  # noqa: UP031 - the f-string case, deliberately pre-rendered
        logger.info(f"pointer {FAKE_ARN}")

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_AWS_KEY not in emitted
    assert FAKE_ARN not in emitted


def test_a_non_string_log_message_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`logger.info(payload_dict)` — the message itself is not a string.

    A filter that assumed `record.msg` is always a template would skip this
    entirely, and passing a dict straight to a logger is a normal thing to write.
    """
    logger = logging.getLogger("superplane.test.connection.objmsg")
    logger.propagate = True
    install_log_redaction(logger)
    with caplog.at_level(logging.INFO, logger="superplane.test.connection.objmsg"):
        logger.info({"secret_access_key": FAKE_AWS_KEY, "arn": FAKE_ARN})

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_AWS_KEY not in emitted
    assert FAKE_ARN not in emitted


def test_secrets_nested_in_sequences_are_redacted() -> None:
    """A value inside a list or tuple must not survive by being one level down.

    Provider payloads arrive as lists of credential blobs, so a scrubber that only
    walked mappings would pass the realistic shape straight through.
    """
    scrubbed = scrub(
        {
            "accounts": [
                {"label": "prod", "secret_access_key": FAKE_AWS_KEY},
                {"label": "dev", "arn": FAKE_ARN},
            ],
            "pointers": (FAKE_ARN, "harmless"),
        }
    )
    rendered = repr(scrubbed)
    assert FAKE_AWS_KEY not in rendered
    assert FAKE_ARN not in rendered
    # The non-secret content is preserved: redaction must not blank the payload.
    assert "prod" in rendered
    assert "harmless" in rendered


def test_scrub_preserves_non_string_scalars() -> None:
    """Quota and capacity numbers must pass through unchanged.

    This contract reports quota and observed capacity, so a scrubber that stringified
    or rewrote numbers would corrupt the very fields acceptance 3 separates.
    """
    assert scrub({"observed_capacity": 4, "quota": 100, "ratio": 0.5, "on": True}) == {
        "observed_capacity": 4,
        "quota": 100,
        "ratio": 0.5,
        "on": True,
    }


# ---------------------------------------------------------------------------
# Acceptance 5 — atomic rotation, honest disablement
# ---------------------------------------------------------------------------


def test_rotation_switches_to_the_replacement_and_keeps_the_old_key() -> None:
    """acc. 5: the old credential is superseded, never deleted first.

    The dead window this forbids is the interval between deleting the old key and
    the replacement serving traffic.
    """
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    result = rotate(
        connection,
        replacement=_reference("cred-2"),
        replacement_validation=_report(),
        rotated_at=CHECKED_AT + timedelta(minutes=5),
        rotated_by=OWNER,
    )
    assert result.connection.reference.credential_id == "cred-2"
    assert result.superseded_reference.credential_id == "cred-1"
    assert result.old_credential_still_registered
    # Atomic: the rotated connection is usable immediately, never briefly pending.
    assert result.connection.status is ConnectionStatus.ACTIVE
    assert result.connection.admits_new_work()


def test_rotation_onto_an_unvalidated_replacement_is_refused() -> None:
    """The replacement must have validated *before* the reference switches."""
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    with pytest.raises(ContractViolation, match="has not validated"):
        rotate(
            connection,
            replacement=_reference("cred-2"),
            replacement_validation=_report(valid=False, permitted=False, quota=False),
            rotated_at=CHECKED_AT,
            rotated_by=OWNER,
        )


def test_rotation_carries_the_binding_to_the_replacement() -> None:
    """The workspace binding must survive a rotation, still naming one workspace."""
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    result = rotate(
        connection,
        replacement=_reference("cred-2"),
        replacement_validation=_report(),
        rotated_at=CHECKED_AT,
        rotated_by=OWNER,
    )
    assert result.connection.workspace_id == W1
    assert result.connection.binding.credential_id == "cred-2"
    assert authorize_use(
        workspace_id=W1,
        connection=result.connection,
        binding=result.connection.binding,
    ).allowed


def test_rotation_onto_the_same_credential_is_refused() -> None:
    """Otherwise a caller fleeing a compromised key is told it rotated away."""
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    with pytest.raises(ContractViolation, match="its own credential"):
        rotate(
            connection,
            replacement=_reference("cred-1"),
            replacement_validation=_report(),
            rotated_at=CHECKED_AT,
            rotated_by=OWNER,
        )


def test_rotation_does_not_require_free_capacity() -> None:
    """A busy provider must not block rotating away from a bad key."""
    connection = _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    result = rotate(
        connection,
        replacement=_reference("cred-2"),
        replacement_validation=_report(capacity=0),
        rotated_at=CHECKED_AT,
        rotated_by=OWNER,
    )
    assert result.connection.status is ConnectionStatus.ACTIVE


def test_disablement_blocks_admissions_and_renewals_and_surfaces_the_limitation() -> (
    None
):
    """acc. 5: both are blocked, and the limitation is surfaced, not documented."""
    disabled = disable(
        _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    )
    assert not disabled.admits_new_work()
    assert not disabled.allows_renewal()
    assert disabled.limitation == DISABLEMENT_LIMITATION
    assert "revoked at the provider" in disabled.limitation
    assert "limitation" in connection_response(disabled)


def test_disabled_connection_cannot_be_used_even_where_bound() -> None:
    disabled = disable(
        _connection(status=ConnectionStatus.ACTIVE, validation=_report())
    )
    assert not authorize_use(
        workspace_id=W1, connection=disabled, binding=_binding()
    ).allowed


def test_a_disabled_connection_must_carry_its_limitation() -> None:
    """Constructing a DISABLED state with no limitation is refused."""
    with pytest.raises(ContractViolation, match="limitation"):
        ConnectionState(
            connection_id="conn-1",
            provider="aws",
            reference=_reference(),
            binding=_binding(),
            status=ConnectionStatus.DISABLED,
            validation=_report(),
            limitation="",
        )


# ---------------------------------------------------------------------------
# Lifecycle invariants
# ---------------------------------------------------------------------------


def test_pending_connection_admits_nothing_but_allows_renewal() -> None:
    """The two answers differ, which is why they are two methods."""
    pending = _connection()
    assert not pending.admits_new_work()
    assert pending.allows_renewal()


def test_active_requires_a_validation_report() -> None:
    """ACTIVE must mean "checked and working", not "someone called activate"."""
    with pytest.raises(ContractViolation, match="requires a validation report"):
        ConnectionState(
            connection_id="conn-1",
            provider="aws",
            reference=_reference(),
            binding=_binding(),
            status=ConnectionStatus.ACTIVE,
            validation=None,
        )


def test_activate_refuses_an_unvalidated_report() -> None:
    with pytest.raises(ContractViolation):
        activate(_connection(), _report(valid=False, permitted=False, quota=False))


def test_activate_promotes_a_pending_connection() -> None:
    active = activate(_connection(), _report())
    assert active.status is ConnectionStatus.ACTIVE
    assert active.admits_new_work()


def test_naive_timestamps_are_refused() -> None:
    """An unqualified local time from an unknown host is not a comparable fact."""
    with pytest.raises(ContractViolation, match="timezone-aware"):
        WorkspaceBinding(
            credential_id="cred-1",
            workspace_id=W1,
            bound_by=OWNER,
            bound_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
        )
    with pytest.raises(ContractViolation, match="timezone-aware"):
        ValidationReport(
            credential_valid=True,
            permissions_sufficient=True,
            quota_available=True,
            observed_capacity=1,
            checked_at=datetime(2026, 9, 17, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
        )


def test_negative_capacity_is_refused() -> None:
    with pytest.raises(ContractViolation):
        _report(capacity=-1)


def test_blank_reference_fields_are_refused() -> None:
    with pytest.raises(ContractViolation):
        CredentialReference(credential_id="  ", service="aws", label="prod")


def test_reference_refuses_secret_material_in_its_label() -> None:
    """A caller pasting a key into the label field is refused at construction."""
    with pytest.raises(ContractViolation):
        CredentialReference(credential_id="cred-1", service="aws", label=FAKE_AWS_KEY)


def test_request_must_be_a_mapping_with_required_fields() -> None:
    with pytest.raises(ContractViolation, match="mapping"):
        accept_connection_request("credential_id=cred-1")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation, match="missing required field"):
        accept_connection_request({"credential_id": "cred-1"})


def test_disabled_connection_cannot_be_reactivated_or_rotated():
    disabled = disable(_connection())
    with pytest.raises(ContractViolation, match="disabled"):
        activate(disabled, _report())
    with pytest.raises(ContractViolation, match="disabled"):
        rotate(
            disabled,
            replacement=_reference("cred-other"),
            replacement_validation=_report(),
            rotated_at=CHECKED_AT,
            rotated_by=OWNER,
        )
    assert not disabled.admits_new_work() and not disabled.allows_renewal()


def test_auth_header_is_rejected_before_reference_acceptance():
    with pytest.raises(ContractViolation):
        accept_connection_request(
            {
                "credential_id": "cred-1",
                "service": "aws",
                "label": "prod",
                "auth_header": "opaque-value",
            }
        )


def test_child_logger_and_exception_are_redacted_at_handler():
    import io

    stream = io.StringIO()
    parent = logging.getLogger("superplane.handler-regression")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(auth_header)s"))
    parent.addHandler(handler)
    parent.setLevel(logging.INFO)
    parent.propagate = False
    try:
        install_log_redaction(parent)
        child = logging.getLogger(parent.name + ".child")
        try:
            raise ValueError(FAKE_AWS_KEY)
        except ValueError:
            child.exception(
                "failed %s", FAKE_ARN, extra={"auth_header": "opaque-private-value"}
            )
        assert FAKE_AWS_KEY not in stream.getvalue()
        assert FAKE_ARN not in stream.getvalue()
        assert "opaque-private-value" not in stream.getvalue()
        assert "REDACTED" in stream.getvalue()
    finally:
        parent.removeHandler(handler)


@pytest.mark.parametrize("value", ["false", 1, None])
def test_validation_readings_are_boolean_facts(value):
    with pytest.raises(ContractViolation, match="booleans"):
        _report(valid=value)


def test_unknown_status_cannot_allow_renewal():
    with pytest.raises(ContractViolation, match="status"):
        _connection(status="disabled")


@pytest.mark.parametrize("secret_key", [FAKE_ARN, FAKE_AWS_KEY])
def test_secret_shaped_mapping_keys_are_refused_without_echo_and_scrubbed(secret_key):
    payload = {"provider": {secret_key: "value"}}
    with pytest.raises(ContractViolation) as exc:
        accept_connection_request(payload)
    assert secret_key not in str(exc.value)
    assert "redacted-key" in str(exc.value)
    rendered = repr(scrub(payload))
    assert secret_key not in rendered
    assert "REDACTED" in rendered


@pytest.mark.parametrize(
    "permissions",
    [
        RENEW_CREDENTIAL_PERMISSION,
        "prefix-" + RENEW_CREDENTIAL_PERMISSION + "-suffix",
        RENEW_CREDENTIAL_PERMISSION.encode(),
        None,
        [RENEW_CREDENTIAL_PERMISSION],
    ],
)
def test_delegation_requires_a_permission_set(permissions):
    decision = authorize_delegation(
        principal=OWNER,
        workspace_id=W1,
        ownership=VaultOwnership(credential_id="cred-1", owner_principal=OWNER),
        reference=_reference(),
        granted_permissions=permissions,
    )
    assert decision.allowed is False
    assert decision.reason == "not authorized to delegate this credential"


def test_root_install_redacts_propagated_application_records():
    import io

    class StructuredFormatter(logging.Formatter):
        def format(self, record):
            return repr(record.__dict__)

    stream = io.StringIO()
    root = logging.getLogger()
    original_filters = list(root.filters)
    handler_filters = [(handler, list(handler.filters)) for handler in root.handlers]
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    root.addHandler(handler)
    child = logging.getLogger("superplane.root-redaction-regression")
    old_level, old_propagate = child.level, child.propagate
    child.setLevel(logging.WARNING)
    child.propagate = True
    try:
        installed = install_log_redaction(root)
        assert install_log_redaction(root) is installed
        child.warning(
            "payload=%s",
            {FAKE_ARN: FAKE_AWS_KEY},
            extra={FAKE_ARN: "opaque-private-value"},
        )
        assert stream.getvalue()
        assert FAKE_ARN not in stream.getvalue()
        assert FAKE_AWS_KEY not in stream.getvalue()
        assert "opaque-private-value" not in stream.getvalue()
        assert "REDACTED" in stream.getvalue()
    finally:
        root.removeHandler(handler)
        root.filters[:] = original_filters
        for existing, filters in handler_filters:
            existing.filters[:] = filters
        child.setLevel(old_level)
        child.propagate = old_propagate

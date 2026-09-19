"""Provider connections and workspace bindings — the two independent checks.

Issue #5047 (U7), EPIC #4910. R7 acceptances 1, 2, 3 and 5.

## The two questions, and why one cannot answer the other

A provider connection is authorized by two decisions that are about different
things, and the failure this contract exists to prevent is letting either stand in
for the other:

* **Vault ownership / delegation** decides *who manages this key* — may this
  principal register it, rotate it, delegate it, revoke it.
* **The workspace binding** decides *where it may be used* — which workspace's
  admissions may spend through it.

Possession of a credential inside the same organization is neither. That is the
specific hole: ADP's own handlers filter on `Workspace.org_id == org_id` (U9's
policy module documents this), so every org-mate reaches every workspace. If this
contract accepted org membership as a binding, a workspace admin delegating one
credential would in effect have delegated all of them to everyone in the org.

So the two checks live in two functions, are called in a fixed order, and neither
consults the other's evidence. `authorize_delegation` takes the ownership record;
`authorize_use` takes the binding. A caller cannot satisfy one by passing the
other, because the parameters have different types.

## Why the ownership check is expressed against U9's model

U9 (#5044) landed `superplane_auth.policy` with `WorkspaceAuthorizationModel`,
`WorkspaceGrant` and `Permission.RENEW_CREDENTIAL` — "register, rotate or delete a
provider credential binding". That is exactly this unit's permission, so the
delegation check is expressed in those terms rather than inventing a second
authorization vocabulary that would drift from the first.

This module does not *import* U9: the two packages ship separately
(`auth/superplane_auth` has its own `pyproject.toml`) and a hard import would make
this contract package unusable without it. Instead the permission name is pinned as
a string constant and asserted against U9's enum by the tests, so a rename upstream
breaks a test rather than silently detaching the check from the model.

## Validity is not capacity (acceptance 3)

`ValidationReport` reports four things in four separate fields. They are separate
because conflating the first with the last is a concrete outage: a valid API key
proves the credential authenticates, and says nothing whatever about whether the
provider has a single free GPU. An admission that reads "credential valid" as
"capacity available" succeeds against a provider with none, and the workload fails
later at the provider, where the failure is someone else's log.

The type therefore has no aggregate boolean — no `ok`, no `healthy`, no `ready`.
Anything that collapses four independent readings into one is the conflation with a
friendlier name, and `is_usable_for_admission()` is deliberately explicit that it
requires observed capacity as its own input.

## Rotation is atomic, and disablement is honest (acceptance 5)

`rotate()` takes a replacement that has **already validated** and returns a new
connection pointing at it. It cannot be called with an unvalidated replacement, and
it never expresses "remove the old reference" as a step — the old credential
remains registered in the vault, to be revoked separately once the new one is
serving. A sequence that deleted first would leave the connection dead for the
width of that window, which is precisely what "atomically" forbids.

`disable()` blocks new admissions and renewals and carries
`DISABLEMENT_LIMITATION`: a long-lived credential already handed to a running
workload stays usable until it is revoked at the provider. Stating that in the
returned state is the acceptance criterion — an operator who believes disablement
is containment will not perform the provider-side revocation that actually is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from .health import ContractViolation
from .secrets import assert_no_secret_material, looks_like_arn

# The domain permission required to register, rotate, delegate or revoke a
# provider credential binding. Pinned as a string rather than imported from
# `superplane_auth` (see the module docstring) and asserted against that enum by
# `tests/test_connection_contract.py`, so an upstream rename fails a test.
RENEW_CREDENTIAL_PERMISSION = "workspace:renew_credential"

# What disablement does not accomplish. Returned in `ConnectionState` rather than
# documented here alone, because acceptance 5 requires the limitation to be
# *surfaced* — a comment in a contract file is not surfaced to an operator.
DISABLEMENT_LIMITATION = (
    "Disablement blocks new admissions and credential renewals. Credentials already "
    "delivered to running workloads may remain usable until they are revoked at the "
    "provider; disablement here does not revoke them."
)


class ConnectionStatus(StrEnum):
    """Lifecycle of a provider connection."""

    PENDING = "pending"
    """Reference recorded, not yet validated. Admits nothing."""

    ACTIVE = "active"
    """Validated and usable, subject to the binding and to observed capacity."""

    DISABLED = "disabled"
    """Blocked for new admissions and renewals. See `DISABLEMENT_LIMITATION`."""


@dataclass(frozen=True)
class CredentialReference:
    """A pointer to a credential held in ADP's vault. Never the credential itself.

    `credential_id` is the vault's own opaque id — the `id` field of the vault's
    `CredentialResponse`. Deliberately not an ARN: `__post_init__` refuses one, for
    the reasons in `secrets.py`. An ARN would make this reference a
    complete-enough pointer that leaking it is a disclosure on its own.

    `service` and `label` are the vault's non-secret metadata, carried so a
    human-readable connection listing needs no second lookup. Neither is secret and
    neither is sufficient to read the credential.
    """

    credential_id: str
    service: str
    label: str

    def __post_init__(self) -> None:
        for name in ("credential_id", "service", "label"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"CredentialReference.{name} is required")
        if looks_like_arn(self.credential_id):
            raise ContractViolation(
                "CredentialReference.credential_id must be the vault's credential id, "
                "not an ARN"
            )
        # Belt and braces: the reference is the one shape guaranteed to cross this
        # boundary, so it is also checked for embedded secret material. A caller
        # putting a key in `label` is refused here rather than at the log boundary.
        assert_no_secret_material(
            {
                "credential_id": self.credential_id,
                "service": self.service,
                "label": self.label,
            },
            what="CredentialReference",
        )


@dataclass(frozen=True)
class VaultOwnership:
    """A vault-side record of who owns a credential and who it is delegated to.

    Server-held, like U9's `WorkspaceGrant`: it is derived from the vault's own
    response, never from the request body. That is what makes the ownership check
    in `authorize_delegation` meaningful — a claim in a payload would be the thing
    being checked, asserted by the party being checked.

    `owner_principal` is the principal the vault says owns the credential.
    `delegated_to_workspaces` is the set of workspaces the owner has already
    delegated it to.
    """

    credential_id: str
    owner_principal: str
    delegated_to_workspaces: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.credential_id or not self.credential_id.strip():
            raise ContractViolation("VaultOwnership.credential_id is required")
        if not self.owner_principal or not self.owner_principal.strip():
            raise ContractViolation("VaultOwnership.owner_principal is required")

    def is_owned_by(self, principal: str) -> bool:
        """True when `principal` is the vault-recorded owner."""
        return bool(principal) and principal == self.owner_principal


@dataclass(frozen=True)
class WorkspaceBinding:
    """The record that one credential may be used by one workspace.

    Exactly one credential and exactly one workspace — the pairing is the unit of
    delegation, which is what lets an admin delegate a single credential instead of
    every credential they hold. A binding covering "all of a principal's
    credentials" cannot be expressed by this type, deliberately.
    """

    credential_id: str
    workspace_id: str
    bound_by: str
    bound_at: datetime

    def __post_init__(self) -> None:
        for name in ("credential_id", "workspace_id", "bound_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"WorkspaceBinding.{name} is required")
        if self.bound_at.tzinfo is None:
            # Naive timestamps are refused for the same reason U8's contract
            # refuses them: a bare local time from an unknown host is not a fact
            # two implementations can compare.
            raise ContractViolation("WorkspaceBinding.bound_at must be timezone-aware")


@dataclass(frozen=True)
class ValidationReport:
    """Four independent readings, reported separately. See the module docstring.

    `credential_valid` — the credential authenticates.
    `permissions_sufficient` — it can perform the operations this connection needs.
    `quota_available` — the provider's *account limit* leaves room.
    `observed_capacity` — units the provider actually reported as free, right now.

    `quota_available` and `observed_capacity` are separate because a quota is a
    ceiling and capacity is a reading against it: an account may be permitted 100
    GPUs and have none free. There is no aggregate field, by design.

    `observed_capacity` of `None` means *not measured*, which is distinct from
    measured-as-zero. `is_usable_for_admission()` refuses both, but a caller
    reporting to an operator needs to tell "we did not look" from "we looked and
    there is nothing" — the same distinction U8's contract draws with its
    `not_checked` status.
    """

    credential_valid: bool
    permissions_sufficient: bool
    quota_available: bool
    observed_capacity: int | None
    checked_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.credential_valid,
                self.permissions_sufficient,
                self.quota_available,
            )
        ):
            raise ContractViolation("validation readings must be booleans")
        if (
            self.observed_capacity is not None
            and type(self.observed_capacity) is not int
        ):
            raise ContractViolation(
                "observed capacity must be an integer or unmeasured"
            )
        if self.checked_at.tzinfo is None:
            raise ContractViolation(
                "ValidationReport.checked_at must be timezone-aware"
            )
        if self.observed_capacity is not None and self.observed_capacity < 0:
            raise ContractViolation(
                "ValidationReport.observed_capacity cannot be negative"
            )
        if self.permissions_sufficient and not self.credential_valid:
            # Permissions cannot have been established through a credential that
            # does not authenticate — the check that would prove it could not have
            # run. Refusing the combination keeps the report from asserting a
            # reading nobody took.
            raise ContractViolation(
                "permissions_sufficient cannot be true when credential_valid is false"
            )
        assert_no_secret_material(
            {"detail": self.detail}, what="ValidationReport.detail"
        )

    @property
    def validated(self) -> bool:
        """True when the *credential* is fit to use: valid, permitted, in quota.

        Explicitly excludes `observed_capacity`. This is the property rotation
        requires of a replacement — a replacement key must be a working key, and
        demanding free capacity before allowing a rotation would block rotations
        precisely when a provider is busy. Capacity is an admission-time reading,
        which is why `is_usable_for_admission()` asks for it separately.
        """
        return (
            self.credential_valid
            and self.permissions_sufficient
            and self.quota_available
        )

    def is_usable_for_admission(self, *, required_capacity: int = 1) -> bool:
        """True when this connection can admit work needing `required_capacity`.

        Takes capacity as an explicit requirement and consults
        `observed_capacity` as its own term, so no caller can reach an admission
        decision from credential validity alone. `None` capacity is not usable:
        unmeasured is not available.
        """
        if required_capacity < 1:
            raise ContractViolation("required_capacity must be at least 1")
        if not self.validated:
            return False
        if self.observed_capacity is None:
            return False
        return self.observed_capacity >= required_capacity


@dataclass(frozen=True)
class ConnectionState:
    """A provider connection: one reference, one binding, one lifecycle status."""

    connection_id: str
    provider: str
    reference: CredentialReference
    binding: WorkspaceBinding
    status: ConnectionStatus = ConnectionStatus.PENDING
    validation: ValidationReport | None = None
    limitation: str = ""

    def __post_init__(self) -> None:
        for name in ("connection_id", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"ConnectionState.{name} is required")
        if not isinstance(self.status, ConnectionStatus):
            raise ContractViolation("connection status must be a ConnectionStatus")
        if self.binding.credential_id != self.reference.credential_id:
            # The binding must be about the credential this connection references.
            # A mismatch would mean the thing authorized for the workspace and the
            # thing actually used are different credentials.
            raise ContractViolation(
                "ConnectionState.binding does not bind this connection's credential"
            )
        if self.status is ConnectionStatus.ACTIVE:
            if self.validation is None or not self.validation.validated:
                raise ContractViolation(
                    "an ACTIVE connection requires a validation report whose "
                    "credential is valid, permitted and within quota"
                )
        assert_no_secret_material(
            {
                "connection_id": self.connection_id,
                "provider": self.provider,
                "workspace": self.binding.workspace_id,
                "bound_by": self.binding.bound_by,
                "limitation": self.limitation,
            },
            what="connection metadata",
        )
        if self.status is ConnectionStatus.DISABLED and not self.limitation:
            raise ContractViolation(
                "a DISABLED connection must surface its disablement limitation"
            )

    @property
    def workspace_id(self) -> str:
        """The one workspace this connection is bound to."""
        return self.binding.workspace_id

    def admits_new_work(self) -> bool:
        """True only for an ACTIVE connection. PENDING and DISABLED admit nothing."""
        return self.status is ConnectionStatus.ACTIVE

    def allows_renewal(self) -> bool:
        """True unless disabled. Acceptance 5: disablement blocks renewals too.

        Separate from `admits_new_work` because a PENDING connection legitimately
        needs its credential renewable while it is being brought up, whereas it
        must not admit work — so the two answers differ and a single flag would be
        wrong for one of them.
        """
        return self.status is not ConnectionStatus.DISABLED


# ---------------------------------------------------------------------------
# The two independent checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """Outcome of an authorization check. A denial carries no tenant detail."""

    allowed: bool
    reason: str = ""


# One refusal string for every ownership/delegation denial, and one for every
# binding denial. Single constants so no future branch becomes more informative
# than its siblings and turns into an enumeration oracle — the same choice U8's
# `scoping.py` and U4's `authz.py` make, for the same reason.
_NOT_AUTHORIZED_TO_DELEGATE = "not authorized to delegate this credential"
_NOT_BOUND = "credential is not bound to this workspace"


def authorize_delegation(
    *,
    principal: str,
    workspace_id: str,
    ownership: VaultOwnership | None,
    reference: CredentialReference,
    granted_permissions: frozenset[str] | set[str],
) -> Decision:
    """Check *who manages the key* before a reference is accepted (acceptance 2).

    Both conditions are required and neither implies the other:

    * the vault must record `principal` as owner or an explicit owner delegation
      to the exact target workspace. A grant or registry listing alone does not
      confer authority over the credential;
    * the principal must hold `workspace:renew_credential` on the target
      workspace, which is U9's server-held grant, not a claim in the request.

    `ownership=None` is a denial. It most often means the vault lookup failed or
    the credential does not exist, and treating an unresolved ownership record as
    permission is how a lookup failure becomes an authorization bypass.
    """
    if not isinstance(granted_permissions, (set, frozenset)) or any(
        not isinstance(permission, str) for permission in granted_permissions
    ):
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    if ownership is None:
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    if ownership.credential_id != reference.credential_id:
        # The ownership record must be about the credential being delegated.
        # Otherwise a caller could pair their own credential's ownership record
        # with a reference to someone else's.
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    if not principal or not (
        ownership.is_owned_by(principal)
        or (
            isinstance(ownership.delegated_to_workspaces, (set, frozenset))
            and workspace_id in ownership.delegated_to_workspaces
        )
    ):
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    if not workspace_id or not workspace_id.strip():
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    if RENEW_CREDENTIAL_PERMISSION not in granted_permissions:
        return Decision(allowed=False, reason=_NOT_AUTHORIZED_TO_DELEGATE)
    return Decision(allowed=True)


def authorize_use(
    *,
    workspace_id: str,
    connection: ConnectionState,
    binding: WorkspaceBinding | None,
) -> Decision:
    """Check *where the key may be used* (the binding half).

    Independent of `authorize_delegation`: a principal authorized to delegate a
    credential has not thereby made it usable anywhere, and a workspace holding a
    binding needs no delegation authority to spend through it.

    `binding` is passed separately from `connection.binding` on purpose. It is the
    server's independently-resolved record, and requiring the two to agree means a
    connection object that arrived with a rewritten binding is refused rather than
    trusted about its own scope.
    """
    if binding is None:
        return Decision(allowed=False, reason=_NOT_BOUND)
    if not workspace_id or binding.workspace_id != workspace_id:
        return Decision(allowed=False, reason=_NOT_BOUND)
    if binding.credential_id != connection.reference.credential_id:
        return Decision(allowed=False, reason=_NOT_BOUND)
    if binding.workspace_id != connection.binding.workspace_id:
        return Decision(allowed=False, reason=_NOT_BOUND)
    if not connection.admits_new_work():
        # A disabled or pending connection is not usable even where it is bound.
        return Decision(allowed=False, reason=_NOT_BOUND)
    return Decision(allowed=True)


# ---------------------------------------------------------------------------
# Lifecycle transitions (acceptance 5)
# ---------------------------------------------------------------------------


def activate(
    connection: ConnectionState, validation: ValidationReport
) -> ConnectionState:
    """Move a connection to ACTIVE against a validation report.

    Refuses a report whose credential did not validate, so ACTIVE always means
    "checked and working" rather than "someone called activate".
    """
    if connection.status is ConnectionStatus.DISABLED:
        raise ContractViolation("cannot activate a disabled connection")
    if not validation.validated:
        raise ContractViolation(
            "cannot activate: the validation report does not establish a valid, "
            "permitted, in-quota credential"
        )
    return replace(
        connection,
        status=ConnectionStatus.ACTIVE,
        validation=validation,
        limitation="",
    )


@dataclass(frozen=True)
class RotationResult:
    """The outcome of an atomic rotation.

    `superseded_reference` is the credential the connection *used* to point at. It
    is returned rather than deleted: the caller revokes it as a separate,
    subsequent step, once traffic is confirmed on the replacement. Naming it
    "superseded" rather than "deleted" is the point — this contract has no
    operation that removes the old credential, so no sequence expressible here can
    produce the dead window acceptance 5 forbids.
    """

    connection: ConnectionState
    superseded_reference: CredentialReference

    @property
    def old_credential_still_registered(self) -> bool:
        """Always True. Rotation never deletes; revocation is a later step."""
        return True


def rotate(
    connection: ConnectionState,
    *,
    replacement: CredentialReference,
    replacement_validation: ValidationReport,
    rotated_at: datetime,
    rotated_by: str,
) -> RotationResult:
    """Switch a connection to a **validated** replacement, atomically.

    The signature is the enforcement. `replacement_validation` is required, so
    there is no way to rotate onto a credential that has not been checked; and the
    switch is one `replace()` producing one new state, so there is no intermediate
    state in which the connection references nothing. The old reference comes back
    in the result for the caller to revoke afterwards.

    A rotation onto the same credential id is refused: it would report a rotation
    that did not change anything, and a caller believing it had rotated away from a
    compromised key would be wrong.
    """
    if connection.status is ConnectionStatus.DISABLED:
        raise ContractViolation("cannot rotate a disabled connection")
    if not replacement_validation.validated:
        raise ContractViolation(
            "cannot rotate: the replacement credential has not validated — "
            "validate the replacement before switching the reference"
        )
    if replacement.credential_id == connection.reference.credential_id:
        raise ContractViolation("cannot rotate a connection onto its own credential")
    if rotated_at.tzinfo is None:
        raise ContractViolation("rotated_at must be timezone-aware")
    if not rotated_by or not rotated_by.strip():
        raise ContractViolation("rotated_by is required")

    new_binding = WorkspaceBinding(
        credential_id=replacement.credential_id,
        workspace_id=connection.binding.workspace_id,
        bound_by=rotated_by,
        bound_at=rotated_at,
    )
    rotated = replace(
        connection,
        reference=replacement,
        binding=new_binding,
        status=ConnectionStatus.ACTIVE,
        validation=replacement_validation,
        limitation="",
    )
    return RotationResult(
        connection=rotated,
        superseded_reference=connection.reference,
    )


def disable(connection: ConnectionState) -> ConnectionState:
    """Block new admissions and renewals, and surface what that does not do.

    The returned state carries `DISABLEMENT_LIMITATION` in a field, not in a
    docstring, because acceptance 5 requires the limitation to be surfaced to
    whoever disabled the connection. An operator who believes this revoked the
    credential will skip the provider-side revocation that actually contains it.
    """
    return replace(
        connection,
        status=ConnectionStatus.DISABLED,
        limitation=DISABLEMENT_LIMITATION,
    )


# ---------------------------------------------------------------------------
# The inbound request boundary (acceptance 1)
# ---------------------------------------------------------------------------


def accept_connection_request(payload: Mapping[str, Any]) -> CredentialReference:
    """Validate an inbound connection request and return its credential reference.

    This is the function R7 acceptance 1 is about: the domain API accepts a
    **reference** and refuses a payload carrying a **value**. The secret check runs
    over the whole payload *before* any field is read, so a request cannot have a
    well-formed reference alongside a stray `secret_access_key` and be accepted for
    the former while the latter is quietly dropped into a log.
    """
    if not isinstance(payload, Mapping):
        raise ContractViolation("connection request must be a mapping")
    assert_no_secret_material(payload, what="connection request")

    missing = [k for k in ("credential_id", "service", "label") if not payload.get(k)]
    if missing:
        raise ContractViolation(
            f"connection request is missing required field(s): {', '.join(sorted(missing))}"
        )
    return CredentialReference(
        credential_id=payload["credential_id"],
        service=payload["service"],
        label=payload["label"],
    )

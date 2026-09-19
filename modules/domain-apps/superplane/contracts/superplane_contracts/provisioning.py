"""Provisioning as an authorized operation, not a call anyone can make.

Issue #5052 (U17a), EPIC #4910. R14, ADP half, acceptance 2.

## What this replaces

Workspace provisioning upstream today posts to a GitHub Actions
``workflow_dispatch`` endpoint (``services/github.py:34``) against a repository
pinned in configuration (``config.py:43``) with a personal access token, from two
call sites (``workspaces.py:147,249``). Two consequences, and the second is the
one this module addresses:

* the product operation depends on a token for a repository ADP does not own, and
  on Actions being available — removing that is **U17b**, upstream, and nothing
  here touches those call sites;
* provisioning progress is whatever a foreign Actions run reports. There is no
  operation record on this side, so there is nothing to cancel, bound or observe.

This module defines the replacement *shape*: an operation binding that must exist
before provisioning can start, a principal resolved from that binding, and
progress that is a report **about** the operation rather than a field the adapter
sets on itself.

## The three types and why they are separate

``OperationBinding`` is what B's facade issues and holds. ``ProvisioningIntent``
is what the caller asks for. ``ProvisioningProgress`` is what the facade reports
back. They are three types rather than one request object because the whole
property being protected is that the *caller's* half cannot supply the
*authority's* half. A single struct with both would make the tenant a field the
requester fills in, which is exactly what design §6 (lines 398-407) forbids.

## Why there is no client, no URL and no HTTP verb here

Same reason as ``observation.py``: this package holds facts and their
well-formedness rules. The facade that issues bindings and reports progress is
B's, and it **does not exist in ADP today** — there is no ``modules/harness/jobs/``.
So the adapter in ``provisioning_adapter.py`` takes a facade as a constructor
argument and the tests pass a mock. That mock is recorded as a mock; it closes no
live criterion.

## What this module deliberately cannot express

* **A caller-supplied tenant.** ``ProvisioningIntent`` has no ``user_id`` and no
  ``org_id`` field. The adapter additionally *rejects* those keys if they arrive
  in a parameter map, because a shape that merely omits a field still accepts it
  through any dict-shaped side channel.
* **A budget verdict.** No ``budget_ok``, no ``limit``, no ``budget_exceeded``.
  Admission-time budget enforcement is B's concern (M6) and this unit adds no
  local replacement, even behind a flag — the same rule ``observation.py`` states
  for ``BudgetUsage``.
* **Success the adapter asserts about itself.** ``ProvisioningProgress`` carries
  the operation's terminal state as reported *through the facade*. There is no
  ``self.status`` on the adapter for a test or a caller to read, because "the
  operation reports success while the provider has provisioned nothing" is the
  specific failure this arrangement exists to prevent.
* **A long-lived credential.** Nothing here holds a secret value. A credential
  scoped to one active run is B's trusted-delivery contract's business; a
  credential this side minted is one no revocation reaches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .health import ContractViolation
from .version import CONTRACT_VERSION

# The two provisioning verbs. Teardown is here rather than in a separate module
# because it is the same authority question with the opposite effect: the upstream
# dispatch call sites are workspace *create* and *delete*, and giving teardown its
# own contract would invite giving it its own weaker authority check.
PROVISION = "provision"
TEARDOWN = "teardown"

PROVISIONING_ACTIONS: frozenset[str] = frozenset({PROVISION, TEARDOWN})

# The permission an operation must carry to provision or tear down. This is the
# string form of `superplane_auth.policy.Permission.PROVISION`, which is the
# authority model U9 owns.
#
# Held as a string, and checked for agreement with U9's enum by
# `tests/test_provisioning_adapter.py`, rather than imported. Two reasons, and the
# second is the load-bearing one:
#
#   * `contracts/` is standard-library-only and separately importable from
#     `auth/` (see `_contracts_path.py` and the auth package's own README). An
#     import here would couple two packages that CI puts on sys.path
#     independently.
#   * A drifting duplicate is caught by a test that fails; a convenient import
#     would let `contracts/` start depending on the auth package's internals,
#     which is how a contract surface grows into the second domain API that
#     `repo-path-allocation.md` forbids.
REQUIRED_PERMISSION = "workspace:provision"


class OperationState(str, Enum):
    """Where an operation is, as reported *by the facade*.

    ``str``-valued for the same reason as ``CheckStatus``: the wire form is a
    stable string rather than an ordinal that shifts when a member is inserted.

    ``UNKNOWN`` exists and is deliberately **not** a synonym for failure. An
    operation whose outcome the facade cannot determine is not a provisioned
    workspace and is not a clean failure either — a consumer that collapses the
    two either leaks resources it believes were never created, or retries a
    provision that actually succeeded. This is the same distinction U8's
    ``CheckStatus`` draws between ``UNKNOWN`` and ``UNREACHABLE``, and the same one
    the HITL contract draws between ``rejected`` and ``unavailable``.
    """

    PENDING = "pending"
    """Accepted by the facade, not yet started."""

    RUNNING = "running"
    """In progress. Not a terminal state; nothing may be concluded from it."""

    SUCCEEDED = "succeeded"
    """The facade reports the operation completed."""

    FAILED = "failed"
    """The facade reports the operation did not complete."""

    CANCELLED = "cancelled"
    """Withdrawn through the facade before reaching a terminal outcome."""

    UNKNOWN = "unknown"
    """The facade cannot determine the outcome. NOT a failure — see above."""


# Terminal states, named once. A caller polling for completion asks whether the
# state is in this set rather than testing `== SUCCEEDED`, so UNKNOWN terminates
# a poll loop instead of spinning forever on an outcome that will never resolve.
TERMINAL_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.UNKNOWN,
    }
)

# States from which nothing may be concluded about the provider. Named so a
# consumer cannot read "not failed" as "succeeded".
INCONCLUSIVE_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.PENDING,
        OperationState.RUNNING,
        OperationState.UNKNOWN,
    }
)


@dataclass(frozen=True)
class ResolvedPrincipal:
    """Who an operation runs as, resolved by the server from the binding.

    Every field here comes from the operation record the facade holds. Nothing on
    this object can be influenced by a request body, which is the property that
    makes it safe to compare an intent against.

    This mirrors ``superplane_auth.policy.DomainPrincipal`` — deliberately, and it
    is not a duplicate of it. ``DomainPrincipal`` is resolved from *verified token
    claims* at an API boundary; this is resolved from an *operation binding* by
    B's facade. The two answer "who is calling this endpoint" and "who is this
    long-running operation running as", which diverge precisely when an operation
    outlives the session that requested it — the case R14 acceptance 2 is about.
    """

    subject: str
    """Opaque ADP principal id. Never parsed for meaning."""

    org_id: str
    """The organization the operation runs under, from the binding only."""

    workspace_id: str
    """The workspace the operation is bound to."""

    def __post_init__(self) -> None:
        if not self.subject or not self.subject.strip():
            raise ContractViolation("resolved principal must carry a subject")
        if not self.org_id or not self.org_id.strip():
            raise ContractViolation("resolved principal must carry an organization")
        if not self.workspace_id or not self.workspace_id.strip():
            raise ContractViolation("resolved principal must carry a workspace")


@dataclass(frozen=True)
class OperationBinding:
    """A server-held record that one provisioning operation was authorized.

    The analogue of ``superplane_auth.policy.OperationAuthorization``, for an
    operation that runs asynchronously under B's facade rather than inside one
    authenticated request. The adapter requires one of these and refuses without
    it: an operation with no binding has nothing tying it to a principal, so there
    is no record to cancel, observe or bound.

    **Constructing this object is not proof of provenance.** It must come from the
    facade, exactly as ``superplane_auth``'s README says of its grant and
    operation objects. The adapter cannot verify that — no offline object can —
    which is why the live criterion stays open. The validated binding is passed
    to the provider separately from caller parameters so its principal and
    workspace remain available at the execution boundary.
    """

    operation_id: str
    """The facade's identifier for this operation. The cancellation handle."""

    principal: ResolvedPrincipal
    """Server-resolved. Never assembled from anything the caller sent."""

    action: str
    """``provision`` or ``teardown``."""

    permission: str
    """The permission the facade authorized. Must be ``workspace:provision``."""

    expires_at: datetime | None = None
    """When the authorization lapses, if the facade bounds it.

    Optional because B has published no expiry semantics for an operation binding
    (see the provenance note in ``PROVISIONING-CONTRACT.md``), and inventing a
    default would either expire real operations early or claim a bound the facade
    does not offer. When present it is enforced; when absent, the binding exposes
    ``unbounded_authority``. This is not a live authority guarantee.
    """

    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.principal, ResolvedPrincipal):
            raise ContractViolation("binding must carry a resolved principal")
        if self.contract_version != CONTRACT_VERSION:
            raise ContractViolation("unsupported provisioning contract version")
        if not self.operation_id or not self.operation_id.strip():
            # An empty operation_id would make every progress report and every
            # cancellation bind to the same empty target — the same defect the
            # HITL contract rejects as `ticket_id_empty`.
            raise ContractViolation("operation binding must carry an operation_id")
        if self.action not in PROVISIONING_ACTIONS:
            raise ContractViolation(f"unknown provisioning action: {self.action!r}")
        if self.permission != REQUIRED_PERMISSION:
            # Refused at construction rather than at the authority check, so a
            # binding authorizing something weaker (a read, say) cannot be passed
            # around as if it authorized provisioning.
            raise ContractViolation(
                f"provisioning requires {REQUIRED_PERMISSION!r}; "
                f"binding carries {self.permission!r}"
            )
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            # Same reasoning as the lease contract: a naive expiry cannot be
            # compared against a receiver's aware clock without guessing a zone,
            # and guessing produces an authorization that lapses hours early or
            # late.
            raise ContractViolation("binding expires_at must be timezone-aware")

    def is_expired(self, now: datetime) -> bool:
        """True when this binding has lapsed as of the caller's ``now``.

        A binding with no ``expires_at`` is never expired *by this check* — which
        is a statement about what the facade published, not a claim that the
        authorization is unbounded. ``unbounded_authority`` is the honest name for
        that case, exposed on this binding.
        """
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        if self.expires_at is None:
            return False
        return now >= self.expires_at

    @property
    def unbounded_authority(self) -> bool:
        """True when the facade attached no expiry to this authorization."""
        return self.expires_at is None


@dataclass(frozen=True)
class ProvisioningIntent:
    """What the caller asks for. Carries no authority whatsoever.

    Note what is absent: no ``user_id``, no ``org_id``, no ``workspace`` the
    caller chose. The workspace an operation acts on comes from the binding's
    principal, so a caller cannot name the tenant it provisions for.

    ``parameters`` exists because provisioning genuinely needs shape (an instance
    type, a region) that the caller does know. It is the one dict-shaped surface
    here, so it is also the obvious smuggling route for an identity field — hence
    ``FORBIDDEN_PARAMETER_KEYS`` and the adapter check that enforces it.
    """

    action: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.action not in PROVISIONING_ACTIONS:
            raise ContractViolation(f"unknown provisioning action: {self.action!r}")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or not pair[0].strip()
            for pair in self.parameters
        ):
            raise ContractViolation("parameters must be immutable string pairs")
        if len({key for key, _ in self.parameters}) != len(self.parameters):
            raise ContractViolation("duplicate provisioning parameter")


# Parameter keys that may never appear in a `ProvisioningIntent`, because each one
# asserts an identity the caller does not get to assert. Checked as a lowercased
# exact-match set plus the prefixes below.
#
# This is the same shape as `strip_identity_headers` in U9's policy, and for the
# same reason its docstring gives: "strip the ones we thought of" is the failure
# mode, so the rule is stated as a prefix family rather than a fixed list. The
# difference is the disposition — U9 *removes* identity headers because a benign
# proxy may add one, whereas a caller putting `org_id` in a provisioning parameter
# map has no benign reading. So this is a refusal, not a removal.
FORBIDDEN_PARAMETER_KEYS: frozenset[str] = frozenset(
    {
        "user_id",
        "user",
        "username",
        "org_id",
        "org",
        "organization",
        "organization_id",
        "tenant",
        "tenant_id",
        "workspace",
        "workspace_id",
        "principal",
        "subject",
        "sub",
        "on_behalf_of",
        "impersonate",
        "account_type",
        "role",
        "permission",
        "permissions",
    }
)

FORBIDDEN_PARAMETER_PREFIXES: tuple[str, ...] = (
    "x-",
    "adp_",
    "auth_",
    "caller_",
)


def forbidden_parameters(intent: ProvisioningIntent) -> tuple[str, ...]:
    """Identity-asserting keys present in an intent's parameters.

    Returns the offending keys in the order they appear so a refusal can name
    them. Empty means the intent asserts no identity.

    Case-insensitive, because ``Org_Id`` is the same smuggling attempt as
    ``org_id`` and a case-sensitive check is a bypass with an obvious recipe.
    """
    offending: list[str] = []
    for key, _ in intent.parameters:
        lowered = key.strip().lower().replace("-", "_")
        compact = lowered.replace("_", "")
        if compact in {
            name.replace("_", "") for name in FORBIDDEN_PARAMETER_KEYS
        } or any(
            lowered.startswith(prefix.replace("-", "_"))
            for prefix in FORBIDDEN_PARAMETER_PREFIXES
        ):
            offending.append(key)
    return tuple(offending)


@dataclass(frozen=True)
class ProvisioningProgress:
    """Progress as reported through the facade.

    Constructed from the facade's report, never from the adapter's own view. The
    adapter holds no status field, so there is nothing else this could be built
    from — which is the point: a progress object assembled from adapter-internal
    state would report the adapter's belief about the provider rather than the
    provider's answer.

    ``detail`` is free text for a human. ``state`` is what a consumer branches on.
    """

    operation_id: str
    state: OperationState
    observed_at: datetime
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, OperationState):
            raise ContractViolation("progress state must be an OperationState")
        if not self.operation_id or not self.operation_id.strip():
            raise ContractViolation("progress must name the operation it describes")
        if self.observed_at.tzinfo is None:
            raise ContractViolation("progress observed_at must be timezone-aware")
        if self.state is OperationState.UNKNOWN and not (self.detail or "").strip():
            # An unresolved outcome with no explanation is the least actionable
            # report the contract can carry, and the one most likely to be
            # rounded to "failed" by whoever reads it. Requiring a detail is the
            # same discipline U8 applies to `NOT_CHECKED`, which must state a
            # reason rather than merely declining to claim health.
            raise ContractViolation(
                "an unknown outcome must carry a detail explaining what is unresolved"
            )

    @property
    def is_terminal(self) -> bool:
        """True when no further progress will be reported for this operation."""
        return self.state in TERMINAL_STATES

    @property
    def establishes_provisioned(self) -> bool:
        """True only when the facade reported success.

        Named positively and read positively. A consumer asking "is it done?" and
        getting ``is_terminal`` would treat ``UNKNOWN`` and ``FAILED`` as done,
        which they are — but done is not provisioned, and this property is the one
        that may gate acting as though a resource exists.
        """
        return self.state is OperationState.SUCCEEDED

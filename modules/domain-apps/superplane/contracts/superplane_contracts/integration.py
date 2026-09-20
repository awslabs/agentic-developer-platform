"""The production integration registry: every port, its owner, and its obligations.

Issue #5524 (w6-01), EPIC #4910, Wave 6.

## What this module is for

Wave 6 has sixteen sibling stories, and between them they are meant to supply the
production implementations behind ports this domain app already declares. The
ports exist; the implementations do not. Seven of the eight ``Protocol`` ports in
this package have no production implementation and are satisfied only by test
doubles, and the four ports declared inside the API server
(``credential_evidence``, ``provider_authority``, ``allocation_inventory``,
``operation_facade``) are module globals still holding ``None``.

That is a safe state. It is not a *specifiable* state: nothing written down says
what a correct implementation of any of those ports must carry, demand or refuse.
Sixteen developers working from sixteen readings of the same prose produce
sixteen subtly different adapters, and the resulting failures are the expensive
ones the story names — duplicated spend, leaked resources, an operation admitted
without the approval it claimed.

So this module publishes the agreement **as data**, beside the normative prose in
``INTEGRATION-CONTRACT.md``. The reason it is data and not only prose is drift: a
document only humans read agrees with the code exactly until the first change
nobody propagated, and that divergence is silent. As data, a port added to the
code without a registry entry — or described here without a declared port — fails
a test in ``tests/test_integration_contract.py``.

## What a registry entry is, and what it deliberately is not

Each :class:`PortContract` records who owns the implementation, which identifiers
it must bind an operation to, which permission it must demand, which principal it
acts as, and what it must answer when the truth is unknown.

It records **no implementation, no import and no factory**. This module imports
nothing from ``app.*``, holds no reference to any adapter, and cannot construct
one. That absence is the point: a registry able to hand back an implementation
would become a second composition root, and a port could then be satisfied by
something this package chose rather than by something a reviewed startup
composition installed. The registry describes obligations; it confers nothing.

For the same reason there is no ``implemented`` or ``ready`` boolean here. Whether
a port is composed is a property of a running process, answered by
``app.installation.capabilities()`` against the adapters actually installed. A
flag in a source file would be a claim about the world that no observation
supports, and keeping it truthful would depend on someone remembering to edit it
— which is exactly the failure mode ``health.py`` exists to make unconstructible.

## Unknown is a required answer, not an omission

``unknown_outcome`` is mandatory on every entry. The most damaging integration
mistake at these boundaries is not a crash; it is a confident wrong answer. An
adapter that cannot reach its provider must have a way to say so that a caller
cannot read as success, and the existing contracts already model this well in
places — ``ReconcileResult.UNRESOLVED``, ``CostExposure.UNRESOLVED``,
``CheckStatus.NOT_CHECKED``, ``OperationProgress`` with ``state="unknown"``. The
registry's job is to make that uniform across all sixteen implementations instead
of leaving each to re-derive it, because the ones that forget will not fail
loudly.

## Version compatibility

Every entry carries the contract version it is written against, and
:func:`check_port_version` refuses a mismatch rather than interpreting the parts it
recognizes, delegating to ``version.check_version`` so there is one rule and not a
second nearly-identical one. The reasoning is ``version.py``'s and is not repeated
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .health import ContractViolation
from .version import CONTRACT_VERSION, SUPPORTED_VERSIONS, VersionCheck, check_version


class PortOwner(str, Enum):
    """Which party supplies a port's production implementation.

    ``str``-valued so an owner serializes to a stable wire/report string, matching
    the choice every other enum in this package makes.

    The distinction that matters is between ``DOMAIN`` and everything else. A port
    owned elsewhere cannot be satisfied by code this module's own wave writes: if
    the domain app could supply its own ``operation_facade``, it would be
    authorizing its own operations, which is the separation the facade exists to
    create. Recording the owner is what makes "we implemented it locally to get
    the tests passing" a visible contract breach rather than an invisible one.
    """

    DOMAIN = "domain"
    """Superplane domain app — research policy, accounting, observation receipt."""

    HARNESS_JOBS = "harness_jobs"
    """Shared durable execution (#4912): operation/attempt identity, leases, fencing."""

    GATEWAY_VAULT = "gateway_vault"
    """Gateway credential authority (#4912): evidence, exact binding, trusted delivery."""

    PROVIDER = "provider"
    """The cloud provider itself — the only source of provider truth."""

    WORKSPACE_INFRA = "workspace_infra"
    """Managed workspace cluster lifecycle (#5400): create, bootstrap, register, retire."""


class UnknownOutcome(str, Enum):
    """What a port must answer when it cannot establish the truth.

    Each value names a *shape* of refusal, not a message. The shapes differ
    because the safe response differs: a port that can return a typed
    "unresolved" value should, while a port whose only honest answer is "I am not
    composed" must not return a value at all.
    """

    UNRESOLVED_VALUE = "unresolved_value"
    """Return a typed value whose state is explicitly unresolved/unknown.

    Permits neither retry nor release. Used where the caller must keep the
    operation open — ``ReconcileResult.UNRESOLVED``, ``CostExposure.UNRESOLVED``.
    """

    NONE_MEANS_UNVERIFIED = "none_means_unverified"
    """Return ``None``, which the caller must treat as refusal, never as success.

    Used by the API-side readers whose contract already says so: an unanswered
    question is not permission.
    """

    RAISE_UNAVAILABLE = "raise_unavailable"
    """Raise, so no caller can proceed on a value at all.

    Used where continuing without the port would mean acting unauthorized —
    provisioning with no facade, delivery with no lease.
    """


@dataclass(frozen=True)
class PortContract:
    """One production port's obligations, as a value tests can read.

    Frozen, like every other type in this package: a registry entry a caller could
    mutate is one a caller could relax, and a relaxed obligation that still
    validates is the drift this module exists to prevent.

    The constructor refuses entries that are internally inconsistent rather than
    accepting them and relying on a separate validator, following this package's
    established rule that illegal states are unconstructible — a registry author
    cannot forget to call a checker, because the constructor is the checker.
    """

    name: str
    """Stable port identifier. Matches the capability key where one exists."""

    owner: PortOwner
    """Who supplies the production implementation."""

    declared_at: str
    """Repo-relative ``path:line`` where the port is declared. Evidence, not an import."""

    purpose: str
    """One sentence: what this port abstracts."""

    bound_identifiers: tuple[str, ...]
    """Identifiers an operation through this port must be bound to.

    Non-empty on every entry. An unbound operation is the duplicate-spend and
    cross-tenant-access failure class: without an operation identity a repeat is
    indistinguishable from a new request, and without a workspace the tenant
    boundary is whatever the caller claimed.
    """

    required_permission: str | None
    """The permission the port must demand, or ``None`` for a read-only observation port.

    ``None`` is meaningful rather than a gap: a port that establishes provider
    truth confers no authority and must not be able to demand one, because a
    read that requires a write permission invites callers to hold the write.
    """

    acts_as: str
    """Whose authority the implementation acts under — never the caller's claim."""

    unknown_outcome: UnknownOutcome
    """What this port answers when it cannot establish the truth."""

    contract_version: str = CONTRACT_VERSION
    """The contract version this entry is written against."""

    live_verifier: str = ""
    """Who verifies this port against a real provider. Never the implementing story.

    Empty only for a port already implemented and covered by an existing lane.
    Separating producer from live verifier is AC-02's requirement and the reason a
    criterion cannot end at a mocked adapter: the party that wrote the mock is not
    the party that establishes it works.
    """

    refusal_exceptions: tuple[str, ...] = ()
    """Exception type names that constitute this port's refusal, for ``RAISE_UNAVAILABLE``.

    Mandatory on a ``RAISE_UNAVAILABLE`` port and forbidden on any other, because it
    only means anything where raising *is* the declared answer.

    It exists because "it raised" and "it refused" are otherwise the same
    observation on those ports, which makes them unfailable: an adapter with a typo
    raises ``AttributeError``, and a conformance check that accepts any exception
    reads that crash as correct behaviour. Naming the types makes the check an
    allowlist, so an exception nobody declared fails instead of passing.

    Type **names** rather than classes, matched against the raised exception's MRO,
    because the real refusal types live in ``app.services.*`` and this package does
    not import ``app.*`` — see this module's docstring on why the registry holds no
    reference to an implementation. The cost is that a name collision would be
    accepted; the alternative is either a dependency inversion this package refuses
    or a check that accepts every exception, and both are worse.
    """

    carries_contract_version: bool = False
    """Whether a request through this port carries a contract version at all.

    False on the ports whose calls exchange no version — most of the in-process
    ``Protocol`` ports take typed arguments and no envelope, so there is no version
    field to make stale. The conformance suite emits a stale-version probe **only**
    where this is True, because a probe for a dimension the call cannot present is
    not a check: an earlier revision emitted it for every port and reported it as a
    verified refusal against adapters that are never passed a version, which is the
    manufactured evidence this registry exists to prevent.

    Set it True when the port's request carries a ``contract_version`` (a wire
    envelope, or an explicit parameter), and the suite will exercise it.
    """

    def __post_init__(self) -> None:
        for field_name in ("name", "declared_at", "purpose", "acts_as"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"PortContract.{field_name} is required")
        if not isinstance(self.owner, PortOwner):
            raise ContractViolation("PortContract.owner must be a PortOwner")
        if not isinstance(self.unknown_outcome, UnknownOutcome):
            raise ContractViolation(
                "PortContract.unknown_outcome must be an UnknownOutcome — a port "
                "with no declared unknown answer is one whose silence reads as success"
            )
        if not isinstance(self.bound_identifiers, tuple) or not self.bound_identifiers:
            raise ContractViolation(
                f"port {self.name!r} must bind at least one identifier; an unbound "
                "operation cannot be deduplicated or tenant-scoped"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.bound_identifiers
        ):
            raise ContractViolation("bound_identifiers must be non-empty strings")
        if len(set(self.bound_identifiers)) != len(self.bound_identifiers):
            raise ContractViolation(f"duplicate bound identifier on port {self.name!r}")
        if self.required_permission is not None and (
            not isinstance(self.required_permission, str)
            or not self.required_permission.strip()
        ):
            # Blank is worse than absent, for handles.py's reason: absent says
            # "this port confers no authority", blank reads as a permission that
            # happens to be unnamed — and an unnamed permission compares equal to
            # nothing, so every check against it silently passes.
            raise ContractViolation(
                f"port {self.name!r} required_permission must be a non-empty "
                "string or None"
            )
        if self.unknown_outcome is UnknownOutcome.RAISE_UNAVAILABLE:
            if not self.refusal_exceptions:
                raise ContractViolation(
                    f"port {self.name!r} refuses by raising, so it must name the "
                    "exception types that constitute that refusal; otherwise any "
                    "crash inside the adapter reads as correct behaviour"
                )
            if any(
                not isinstance(item, str) or not item.strip()
                for item in self.refusal_exceptions
            ):
                raise ContractViolation("refusal_exceptions must be non-empty strings")
        elif self.refusal_exceptions:
            raise ContractViolation(
                f"port {self.name!r} does not refuse by raising, so naming refusal "
                "exceptions would describe an answer its contract forbids"
            )
        if ":" not in self.declared_at:
            raise ContractViolation(
                f"port {self.name!r} declared_at must be 'path:line' evidence"
            )
        if self.contract_version not in SUPPORTED_VERSIONS:
            raise ContractViolation(
                f"port {self.name!r} declares unsupported contract version "
                f"{self.contract_version!r}"
            )

    @property
    def is_composed_locally(self) -> bool:
        """Whether the domain app may supply this port itself.

        False for every port owned elsewhere. Exposed so a composition test can
        assert the negative directly instead of each caller re-deriving it from
        ``owner``, and so the rule has one home.
        """
        return self.owner is PortOwner.DOMAIN


# The production ports, as declared in this repository at #5524's baseline
# (f30bb66f299276a6cfe3b4a05150e63bbbda7e42). `declared_at` is evidence a reviewer
# can open, and `tests/test_integration_contract.py` reads each path to confirm the
# port is still declared there — so a port renamed or moved without updating this
# registry fails rather than leaving a stale citation.
#
#
# `carries_contract_version` is False on every entry, and that is a finding rather
# than an omission: none of these ports' calls exchange a contract version. They take
# typed arguments, not a versioned envelope — `grep -n contract_version` across
# `adapter.py`, `delivery.py`, `provisioning_adapter.py` and `auth.py` returns
# nothing. The version discipline in `version.py` governs the *observation
# submission* boundary (`auth.authenticate_submission`), which is a function, not one
# of these ports. So the stale-version probe is reported as not-exercised here rather
# than as a passing refusal. A Wave 6 story that introduces a versioned envelope on
# its port sets this True and the suite exercises it automatically.
#
# Ordering is by owner then name, purely for readability. Nothing depends on it.
PRODUCTION_PORTS: tuple[PortContract, ...] = (
    # ---------------------------------------------------------------- harness/jobs
    PortContract(
        name="operation_facade",
        owner=PortOwner.HARNESS_JOBS,
        declared_at="src/superplane-api/app/services/provisioning.py:226",
        purpose=(
            "Authorize and open one durable operation, and be the only channel "
            "through which its outcome is learned."
        ),
        bound_identifiers=("operation_id", "workspace_id", "org_id"),
        required_permission="workspace:provision",
        acts_as="the facade's own resolved principal, never the request body's org_id",
        unknown_outcome=UnknownOutcome.RAISE_UNAVAILABLE,
        # `app.services.provisioning`: Unavailable means no facade is configured,
        # Refused means a well-formed request it will not run. Both are answers.
        refusal_exceptions=("ProvisioningUnavailable", "ProvisioningRefused"),
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="operation_authority",
        owner=PortOwner.HARNESS_JOBS,
        declared_at="contracts/superplane_contracts/adapter.py:107",
        purpose=(
            "Supply the authority token for an active operation, which the domain "
            "app never mints, caches or infers."
        ),
        bound_identifiers=("allocation_id",),
        required_permission="workspace:provision",
        acts_as="the operation's holder under B's active lease and fence",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="provider_authority",
        owner=PortOwner.HARNESS_JOBS,
        declared_at="src/superplane-api/app/services/provider_authority.py:35",
        purpose=(
            "Verify a presented operation authority against B's live lease and "
            "fence, refusing fabricated, revoked or foreign values."
        ),
        bound_identifiers=("operation_id", "run_id", "attempt_id", "submitter_id"),
        required_permission="workspace:provision",
        acts_as="B's verified operation record, not the presented handle's scope",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="allocation_inventory",
        owner=PortOwner.HARNESS_JOBS,
        declared_at="src/superplane-api/app/services/provider_inventory.py:38",
        purpose=(
            "Enumerate an allocation's independently billable resources under a "
            "current recovery fence, with an independently attested report digest."
        ),
        bound_identifiers=("allocation_id", "workspace", "operation_authority"),
        required_permission="workspace:provision",
        acts_as="B's fenced cleanup authority for this executor",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="handle_store",
        owner=PortOwner.HARNESS_JOBS,
        declared_at="contracts/superplane_contracts/adapter.py:88",
        purpose=(
            "Persist a provider handle before the provider call, and acknowledge "
            "the instant it became durable."
        ),
        bound_identifiers=("idempotency_key", "allocation_id", "workspace"),
        required_permission=None,
        acts_as="the domain persistence owner, acknowledging its own write",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    # --------------------------------------------------------------- gateway/vault
    PortContract(
        name="credential_evidence",
        owner=PortOwner.GATEWAY_VAULT,
        declared_at="src/superplane-api/app/services/credential_evidence.py:26",
        purpose=(
            "Report current vault metadata plus an independently verified binding "
            "between a validation report and this exact credential and workspace."
        ),
        bound_identifiers=("org_id", "workspace_id", "credential_reference"),
        required_permission="workspace:renew_credential",
        acts_as="the vault's own ownership record, never the request's digest claim",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="trusted_delivery",
        owner=PortOwner.GATEWAY_VAULT,
        declared_at="contracts/superplane_contracts/delivery.py:398",
        purpose=(
            "Hand over credential material scoped to one recipient, run and "
            "workspace, and report whether that credential still admits work."
        ),
        bound_identifiers=("lease_id", "operation_id", "workspace_id", "recipient"),
        required_permission="workspace:provision",
        acts_as="the lease's bound principal, re-checked at the operation",
        unknown_outcome=UnknownOutcome.RAISE_UNAVAILABLE,
        # `contracts/superplane_contracts/delivery.py:153`.
        refusal_exceptions=("DeliveryRefused",),
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    # -------------------------------------------------------------------- provider
    PortContract(
        name="provider_client",
        owner=PortOwner.PROVIDER,
        declared_at="contracts/superplane_contracts/adapter.py:120",
        purpose=(
            "Invoke a provider operation and answer a fresh re-check of what the "
            "provider currently holds."
        ),
        bound_identifiers=("idempotency_key", "resource_name", "allocation_id"),
        required_permission=None,
        acts_as="the provider credential delivered for this operation",
        unknown_outcome=UnknownOutcome.UNRESOLVED_VALUE,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    PortContract(
        name="provider_operation",
        owner=PortOwner.PROVIDER,
        declared_at="contracts/superplane_contracts/delivery.py:432",
        purpose=(
            "Perform exactly one permitted provider action with a materialized "
            "credential and return the provider's own observation."
        ),
        bound_identifiers=("provider", "provider_account_id", "operation"),
        required_permission="workspace:provision",
        acts_as="the leased credential, for the single bound action",
        unknown_outcome=UnknownOutcome.UNRESOLVED_VALUE,
        live_verifier="Wave 6 operations evaluator (#5540)",
    ),
    # ------------------------------------------------------------- workspace infra
    PortContract(
        name="provisioning_provider",
        owner=PortOwner.WORKSPACE_INFRA,
        declared_at="contracts/superplane_contracts/provisioning_adapter.py:122",
        purpose="Create or destroy workspace infrastructure for a bound operation.",
        bound_identifiers=("operation_id", "workspace_id", "org_id"),
        required_permission="workspace:provision",
        acts_as="the operation binding's principal, for the bound action only",
        unknown_outcome=UnknownOutcome.RAISE_UNAVAILABLE,
        # `contracts/superplane_contracts/provisioning_adapter.py:71`.
        refusal_exceptions=("ProvisioningRefused",),
        live_verifier="Wave 6 live gate (#5540); workspace lifecycle per #5400",
    ),
    # ---------------------------------------------------------------------- domain
    PortContract(
        name="submitter_resolver",
        owner=PortOwner.DOMAIN,
        declared_at="contracts/superplane_contracts/auth.py:93",
        purpose=(
            "Resolve a presented credential to an authenticated submitter, or to "
            "no submitter at all."
        ),
        bound_identifiers=("credential", "workspaces"),
        required_permission=None,
        acts_as="the configured submitter grant, compared in constant time",
        unknown_outcome=UnknownOutcome.NONE_MEANS_UNVERIFIED,
        # Implemented (ConfiguredSubmitterResolver) and covered by the existing
        # observation-auth suites, so no Wave 6 live verifier is owed for it. Left
        # empty deliberately rather than naming a verifier who owes nothing.
        live_verifier="",
    ),
)

# Indexed by name for lookup. Built here rather than in each caller so there is one
# mapping and a duplicate name is a construction-time failure below, not a silently
# last-one-wins dict comprehension in whichever module happened to build it first.
PORTS_BY_NAME: dict[str, PortContract] = {port.name: port for port in PRODUCTION_PORTS}

if len(PORTS_BY_NAME) != len(PRODUCTION_PORTS):  # pragma: no cover - import-time guard
    raise ContractViolation("duplicate port name in PRODUCTION_PORTS")

# The four ports the API server reports through `app.installation.capabilities()`.
# Named here so the capability readout and this registry cannot drift: the
# composition test asserts this set equals the keys `capabilities()` returns.
#
# It is a subset, not the whole registry, because the other ports are consumed by
# aggregates the API server does not construct (`ProviderAdapter`,
# `ProviderExecutor`). Declaring all eleven as capabilities would make the readout
# claim the API server checks things it never touches.
API_CAPABILITY_PORTS: frozenset[str] = frozenset(
    {
        "credential_evidence",
        "provider_authority",
        "allocation_inventory",
        "operation_facade",
    }
)


def port(name: str) -> PortContract:
    """The registry entry for ``name``, or raise.

    Raises rather than returning ``None`` because every caller here is asking about
    a port it believes exists; a missing entry is a registry bug, and a ``None``
    that flows onward becomes an ``AttributeError`` somewhere less obvious.
    """
    try:
        return PORTS_BY_NAME[name]
    except KeyError:
        raise ContractViolation(f"unknown production port: {name!r}") from None


def ports_owned_by(owner: PortOwner) -> tuple[PortContract, ...]:
    """Every port a given party must supply, in registry order.

    Exists so a Wave 6 story can enumerate its own obligations rather than reading
    them off the table by eye.
    """
    if not isinstance(owner, PortOwner):
        raise ContractViolation("owner must be a PortOwner")
    return tuple(item for item in PRODUCTION_PORTS if item.owner is owner)


def externally_owned_ports() -> tuple[PortContract, ...]:
    """Ports the domain app may not implement itself.

    The composition check uses this to assert the domain app is not its own
    authority: if these could be satisfied locally, the separation each port
    exists to create would be gone while every test still passed.
    """
    return tuple(item for item in PRODUCTION_PORTS if not item.is_composed_locally)


def check_port_version(name: str, declared: object) -> VersionCheck:
    """Whether ``declared`` is a version this registry can serve for ``name``.

    Delegates to ``version.check_version`` rather than comparing strings locally,
    so the missing/blank/mismatched/unsupported rules are the ones ``version.py``
    already states and tests. A second nearly-identical comparison here would
    drift, and the drift would be invisible because each copy would still pass its
    own tests.

    The registry entry's own version is passed as the "header" side, so an
    implementation written against a version this registry does not declare is
    refused as a mismatch rather than accepted because it looked plausible.
    """
    return check_version(port(name).contract_version, declared)

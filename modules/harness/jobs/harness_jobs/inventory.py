"""Trusted allocation inventory, report attestation and cleanup authority.

Issue #5529 (w6-06), EPIC #4910, Wave 6. Implements the `allocation_inventory` port
from #5524's registry (`superplane_contracts.integration`, owner `harness_jobs`,
declared at `src/superplane-api/app/services/provider_inventory.py:38`).

## The question this module answers, and why nothing else can

The domain's release path reaches a point where it must decide whether to stop charging
for an allocation. It holds provider observations it collected, and it needs three
things it cannot establish for itself:

1. that the observations really came from the executor that held the operation,
2. that the allocation's billable resources are *all* enumerated, and
3. that the executor asking for cleanup is still authorized to have it.

It cannot establish any of them because it owns none of the evidence: the lease, the
fence, the approved plan and the provider-call record all live here. So it asks
(`services/provider_handles.py:780`), and until this module existed the answer was
always "unavailable" -- which is why `assess_allocation_release` retains exposure
rather than releasing budget. That default is correct and it is also permanent
paralysis: nothing is ever released.

## Why the digest is recomputed rather than compared

`AllocationInventoryReader.read` takes a `report_digest` from the caller, and the port's
docstring is blunt about the trap: "Never merely echo the digest or treat a workspace
credential as B authority" (`provider_inventory.py:52`). An implementation that hashed
the caller's input and compared it to the caller's input would pass every test written
against it and attest nothing at all.

The service first calls `observe_report` using its trusted provider-query adapter.
It captures the seal and listing binding before the query and stamps its response with
an authority-generated observation ID. `publish_report` accepts only that immutable
receipt and requires its original bindings still to match. An old payload cannot be
rebound to a new listing, even when the provider returns the same state twice: each
actual query has its own observation ID and digest. Publication retries of one receipt
are idempotent and cannot update the stored seal or listing binding.

The digest and the full operation/tenant/attempt/holder/fence binding are both checked.
A nonce alone is not authority, and a caller-provided JSON report is never accepted for
publication. The query adapter is composed in the trusted service, not supplied by a
worker request. Pre-receipt attestations cannot authorize release.

This is also why the observations are stored and not merely digested, for the reason
`store.py` stores the request payload rather than only its digest: a digest answers "is
this the same?" and cannot answer "what was attested?", and a release dispute needs the
second question answered.

## Why membership is a table and not `provider_ref`

`harness_provider_call_intent.provider_ref` records one provider identifier per call,
which is nearly an inventory. The gap is that one provider call routinely creates
several independently billable things -- a cluster that brings its own disks, a node
group that brings a load balancer. Releasing budget on one reference per call therefore
misses precisely the resources that keep costing money after the named one is gone.

So `enumerate_resources` writes a row per billable resource, carrying the provider's own
durable handle and its kind, and `complete` is *computed* from the plan, the call record
and that membership rather than asserted by the caller. An inventory that a caller could
declare complete is a caller that can authorize returning money by saying so.

Membership is keyed to the allocation, and its `operation_id` is provenance with no
foreign key, because it must outlive the operation row: retiring an operation is
ordinary housekeeping that this package is never consulted about, and a cascade there
made the DATABASE shrink an inventory whose entire contract is that it only grows.

## Why membership cannot prove its own completeness

Counting what the caller chose to send can never establish that the caller sent
everything. Checking that each succeeded call's own `provider_ref` is enumerated only
confirms the executor wrote down what it was already telling us about; it cannot see one
call creating several independently billed resources with only its headline one recorded
-- a cluster enumerated without its disk. That inventory read as complete, an ABSENT
report on the cluster released everything, and the disk kept billing.

The missing evidence has to come from outside the executor's own list, so
`record_provider_enumeration` records what the PROVIDER says it holds for the allocation
and refuses the write when the provider names anything membership does not. The stored
listing is a digest of those handles, compared against the handles enumerated *now*, so
a listing that was true when taken and has since been outgrown reads as a mismatch
rather than as a proof.

A listing is evidence *for the authority that took it*, not for the allocation at large.
Comparing only the provider and the handle digest meant a predecessor's listing
satisfied a successor's fence: after recovery advanced the fence, the new holder could
seal and release on a provider query it never made, against provider state it never saw.
Every enumeration proof is therefore required to name the operation, attempt, holder and
fence relying on it, so advancing the fence invalidates the proof and the successor must
re-enumerate before it can seal at all.

## Why an allocation is sealed before it can be released

`read_inventory` necessarily releases its transaction before the domain acts on the
answer. While membership stayed writable under the same lease, "this allocation holds
only the cluster, and the cluster is gone" could be true when computed and false when
acted on -- a release authorized and then invalidated by growth. No locking inside the
read fixes that, because the gap is outside the read.

`seal_allocation` is therefore an explicit write taken under the live fence, and it is
what makes later additions *refused* rather than merely unlikely: `enumerate_resources`
reads the seal inside the same transaction, so a concurrent seal either precedes the
write and refuses it or follows it and seals the grown membership. Only a sealed
allocation is ever reported complete, and the seal names the revision it covers --
recomputed on every read, so a row that appeared behind this package's back moves the
revision and the inventory reads incomplete rather than as a sealed whole.

The lease lock alone is not the serialization this needs. Two separately approved
operations in one tenant can name the same allocation, and then their lease locks are
two different locks: one checks for a seal, the other reads membership and seals over
it, a release is authorized, and the first commits a late resource into an allocation
that can never be sealed again. Membership writes, the enumeration proof and the seal
therefore all take a transaction-scoped advisory lock on
`(org_id, workspace_id, allocation_id)` **before** the lease lock, through one shared
helper so the order cannot diverge into a deadlock (`_hold_allocation`).

The seal is also what makes a report mean anything. `publish_report` required only a
live lease, so the honest sequence was not the only available one: an executor could
query the provider before creating anything, publish the truthful "absent", then create
and seal, and present that earlier report for cleanup -- a release with zero exposure
over a resource that exists. Every individual check passed, because none of them
established that the observation was taken *after* the membership it claims to clear.
So a report names the sealed revision it was published against, publication into an
unsealed allocation is refused outright, and verification requires that revision to
equal the one sealed now. The safe order -- finish creating, seal, then query the
provider and publish -- is consequently the only order the authority accepts.

## Why "incomplete" and "empty" must not be the same answer

An empty resource list reads naturally as "there is nothing to clean up", which is the
expensive mistake in this area: an operation with a provider call whose outcome was
never established may well have created a cluster, and reporting that allocation as
having no members is how it keeps running and keeps billing with nothing in the system
aware of it. `_completeness` therefore refuses to call an inventory complete while any
call `may_have_happened`, and the reader reports `complete=False` rather than an empty
membership -- which the domain converts into retained exposure (`accounting.py:199`).

## What this module does not do

It holds no ledger, computes no balance and returns no amount. `assess_cleanup` reports
what the provider evidence establishes and which per-resource budget disposition follows
from it; the domain applies them. A second place computing cost is a second answer that
can disagree with the real one (`accounting.py:31`).

It also mints no authority. The opaque `operation_authority` the domain presents is
resolved by an injected verifier, exactly as `execution_rpc.ExecutionRPCServer` resolves
a run token -- this package never issues, extends, caches or infers the credential it
checks (`adapter.py:107`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .allocation import (
    MAX_ALLOCATION_ID_LENGTH,
    allocation_epoch,
    allocation_id_for,
    creating_calls_unaccounted_for,
)
from .allocation import (
    bounded_text as _text,
)
from .allocation import (
    lock_allocation as _lock_allocation,
)
from .allocation import (
    sealed_revision as _allocation_sealed_revision,
)
from .execution import BudgetDisposition, audit
from .execution_plan import PlanProgress, confirmed_plan_progress
from .execution_rpc import ExecutionGrant
from .identity import ContractViolation, OperationRefused, ResolvedPrincipal
from .leases import ExecutionLease, lock_lease
from .store import Connection, OperationStore

__all__ = [
    "MAX_INVENTORY_RESOURCES",
    "AllocationResource",
    "CleanupAssessment",
    "CostExposure",
    "InventoryAuthority",
    "ProviderEnumeration",
    "ReleaseState",
    "ResourceObservation",
    "ResourcePresence",
    "VerifiedInventory",
    "allocation_id_for",
    "report_digest",
]


@dataclass(frozen=True)
class ProviderEnumeration:
    """One durable provider listing, returned before its provider I/O starts."""

    query_id: str
    provider: str
    generation: int


# A ceiling on how many resources one allocation may enumerate, and on how many
# observations one report may carry.
#
# Bounded because both are read into memory and digested, and because the caller is the
# process that decides how many rows to send. Unbounded membership is a way to make a
# verifying read expensive enough to stop answering, and a release path that times out
# retains budget forever -- a denial of service whose symptom is money never being
# returned rather than an error anyone notices.
#
# 512 rather than a rounder number: an allocation is one workspace's compute, storage
# and network, and an operation whose plan is capped at 16 steps
# (`execution_plan.admitted_steps`) producing more than 32 billable resources a step is
# a plan that should be split rather than a limit that should be raised.
MAX_INVENTORY_RESOURCES = 512

# Field-width limits, matching what the consumer independently enforces on the values it
# receives (`services/provider_handles.py:815-828`). Checked on the way IN as well, so a
# value too long to be accepted is refused at publication -- where the executor can
# do something about it -- rather than silently making every later read unverifiable.
#
# The resource-id bound is `allocation.MAX_ALLOCATION_ID_LENGTH` rather than a second
# 255 written here. The two have to agree -- the dispatch path now validates the
# allocation id with the shared bound before recording a call, and this module validates
# it again on the read -- and two literals that must agree are one that will eventually
# not.
_MAX_RESOURCE_ID = MAX_ALLOCATION_ID_LENGTH
_MAX_PROVIDER = 64
_MAX_PROVIDER_REFERENCE = 255
_MAX_KIND = 64
_MAX_OPERATION_KEY = 255
_MAX_DETAIL = 4096


class _Refused(Exception):
    """Internal: a refusal carrying the audit detail that goes with it.

    Exists so a refusal can be raised from deep inside a transaction block while its
    audit row is written *after* that transaction has rolled back. Auditing in place
    would enlist the audit write in the very transaction the refusal aborts, and a
    refused write changes nothing else anywhere -- so the audit row dying with it would
    erase the only evidence that the fence worked.

    Private, and converted to `OperationRefused` at every boundary: a caller must not be
    able to distinguish refusal reasons by exception type, since the reasons include
    "that allocation is not yours".
    """

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


class ResourcePresence(str, Enum):
    """What a query to the provider established about one resource.

    Duplicated from `superplane_contracts.reconciliation.ProviderPresence` rather than
    imported, for the reason `test_lease_contract_agreement.py` gives: this package is
    installed standalone and must not require the contracts package on `sys.path`.
    `test_inventory_contract_agreement.py` is the drift guard.

    `UNKNOWN` is not a synonym for `ABSENT`. It is the answer when the provider itself
    could not be consulted, and collapsing the two is how "we could not check" becomes
    "there is nothing there" -- which here authorizes releasing budget for a resource
    that is still running.
    """

    # The provider confirmed the resource exists. Cost continues to accrue.
    PRESENT = "present"

    # The provider confirmed the resource does not exist.
    ABSENT = "absent"

    # The provider could not be consulted. Explicitly not zero cost.
    UNKNOWN = "unknown"


class ReleaseState(str, Enum):
    """How far a release actually got, as opposed to how far it was driven."""

    RELEASED = "released"
    RETAINED = "retained"
    UNRESOLVED = "unresolved"


class CostExposure(str, Enum):
    """Whether an allocation can still be costing money.

    `NONE` is the only value asserting no further cost, and it is unreachable without
    provider-established absence for every enumerated resource.
    """

    NONE = "none"
    ACTIVE = "active"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResourceObservation:
    """What the provider said when queried by a recorded handle.

    `queried_by` records which identifier the query used, and it must be the member's
    `provider_reference` -- the provider's own durable handle -- for the answer to be
    evidence about that member. A query by any other identity, including the local
    `resource_id` the observation is filed under, can be answered confidently by the
    provider and still be about nothing: asked whether it holds `cluster-1` when the
    machine is `i-123`, the provider truthfully says no. `_reconcile` resolves each
    observation to its member and retains anything queried by the wrong identity as
    unestablished; the field exists so that check is possible at all.

    The validation mirrors the contract's (`reconciliation.py:123`) deliberately: this
    type is what the published digest is computed over, so an observation this package
    would accept but the contract would refuse to construct is an attestation of
    something no consumer can read back.
    """

    presence: ResourcePresence
    queried_by: str
    provider_state: str | None = None
    detail: str = ""
    observation_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, str) or (
            self.observation_id
            and (
                len(self.observation_id) != 32
                or any(c not in "0123456789abcdef" for c in self.observation_id)
            )
        ):
            raise ContractViolation(
                "observation_id must be empty or a trusted query nonce"
            )
        if not isinstance(self.presence, ResourcePresence):
            raise ContractViolation("presence must be a ResourcePresence")
        _text(self.queried_by, "queried_by", _MAX_RESOURCE_ID)
        if self.provider_state is not None:
            _text(self.provider_state, "provider_state", _MAX_PROVIDER_REFERENCE)
        if not isinstance(self.detail, str) or len(self.detail) > _MAX_DETAIL:
            raise ContractViolation("detail must be a bounded string")
        if self.presence is ResourcePresence.PRESENT and not self.provider_state:
            # "It exists" with no state is not an observation of the provider; it is an
            # assertion. The provider's own status string is the evidence.
            raise ContractViolation(
                "a PRESENT observation must carry the provider's reported state"
            )
        if self.presence is ResourcePresence.UNKNOWN:
            if self.provider_state:
                raise ContractViolation(
                    "an UNKNOWN observation cannot carry a provider state; the "
                    "provider was not successfully consulted"
                )
            if not self.detail.strip():
                # An unresolved observation blocks both retry and release, so why it
                # could not be answered is the operator's whole starting point.
                raise ContractViolation(
                    "an UNKNOWN observation must say why the provider could not be "
                    "consulted"
                )


@dataclass(frozen=True)
class AllocationResource:
    """One independently billable resource belonging to an allocation.

    `provider_reference` must be the provider's own durable identifier -- the value a
    teardown presents to ask "does this still exist?". A locally chosen name produces a
    confident provider answer about nothing.
    """

    resource_id: str
    provider: str
    provider_reference: str
    kind: str
    operation_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _text(self.resource_id, "resource_id", _MAX_RESOURCE_ID)
        _text(self.provider, "provider", _MAX_PROVIDER)
        _text(self.provider_reference, "provider_reference", _MAX_PROVIDER_REFERENCE)
        _text(self.kind, "kind", _MAX_KIND)
        if not isinstance(self.operation_keys, frozenset):
            raise ContractViolation("operation_keys must be a frozenset")
        for key in self.operation_keys:
            _text(key, "operation_key", _MAX_OPERATION_KEY)


@dataclass(frozen=True)
class VerifiedInventory:
    """Authoritative allocation membership under a current recovery fence.

    The field names are the consumer's, not this package's: they match
    `provider_inventory.VerifiedAllocationInventory` one for one so that #5535's adapter
    is a field copy rather than a translation. A translation is where a `complete` flag
    ends up mapped from the wrong source.

    `expires_at` is the *authority* window -- the lease expiry under which this answer
    was verified -- not a cache lifetime. The consumer re-checks it immediately before
    and after it commits (`provider_handles.py:842-848`), because a delayed commit must
    not derive permission from authority that has since lapsed.
    """

    workspace: str
    org_id: str
    allocation_id: str
    revision: str
    resources: tuple[AllocationResource, ...]
    complete: bool
    expires_at: datetime
    executor_id: str
    active: bool
    attested_report_digest: str


@dataclass(frozen=True)
class CleanupAssessment:
    """What provider evidence establishes about releasing an allocation.

    Reports only. `dispositions` is the per-resource budget decision the owning domain
    applies to its ledger; this module holds no balance and returns no amount.

    `unresolved_resources` names what could not be established rather than counting it,
    because an operator cannot go and look at "something" -- the same requirement
    `ReleaseAssessment.__post_init__` enforces on the consumer's side
    (`accounting.py:163`).
    """

    allocation_id: str
    state: ReleaseState
    exposure: CostExposure
    dispositions: tuple[tuple[str, BudgetDisposition], ...] = ()
    unresolved_resources: tuple[str, ...] = ()
    reason: str = ""
    inventory: VerifiedInventory | None = None

    @property
    def may_mark_released(self) -> bool:
        """True only when a provider re-check established absence for every member."""
        return self.state is ReleaseState.RELEASED

    @property
    def may_return_reservation_unused(self) -> bool:
        """True only when nothing can still be billing against the allocation."""
        return self.exposure is CostExposure.NONE


def report_digest(observations: Mapping[str, ResourceObservation]) -> str:
    """The canonical digest of a provider report.

    Byte-for-byte the form the consumer independently computes
    (`services/provider_handles.py:772-779`): sorted keys, no whitespace, each
    observation as its four fields. It has to be identical rather than merely
    deterministic -- the consumer arrives holding *its* digest and looks this row up by
    it, so a different canonical form is not a mismatch that gets diagnosed, it is an
    attestation that can never be found and a release that never happens.

    Sorted keys rather than insertion order because a mapping's iteration order is a
    property of how it was built, and two processes reporting the same observations must
    produce the same digest or the attestation is unlookupable from the other one.
    """
    if not isinstance(observations, Mapping):
        raise ContractViolation("observations must be a mapping of resource id")
    return hashlib.sha256(_canonical_observations(observations).encode()).hexdigest()


def _canonical_observations(observations: Mapping[str, ResourceObservation]) -> str:
    """The exact JSON the digest is taken over, and the payload that is stored.

    One function for both so the stored bytes and the digested bytes cannot diverge. If
    they could, a verifying read would recompute a digest from a payload that was never
    the one hashed, and every attestation would fail to match for a reason no test
    distinguishes from a forgery.
    """
    if len(observations) > MAX_INVENTORY_RESOURCES:
        raise ContractViolation(
            f"a provider report carries at most {MAX_INVENTORY_RESOURCES} observations"
        )
    payload: dict[str, dict[str, object]] = {}
    for name, observed in observations.items():
        _text(name, "observed resource id", _MAX_RESOURCE_ID)
        if not isinstance(observed, ResourceObservation):
            raise ContractViolation("observations must be ResourceObservation values")
        payload[name] = {
            "detail": observed.detail,
            "observation_id": observed.observation_id,
            "presence": observed.presence.value,
            "provider_state": observed.provider_state,
            "queried_by": observed.queried_by,
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _revision(resources: tuple[AllocationResource, ...]) -> str:
    """A content digest of membership, so a reader can name the snapshot it saw.

    Derived from the members rather than a counter or a timestamp: two reads that return
    the same members must report the same revision (so a retry is recognisable as the
    same snapshot), and any change to any member's identity must change it (so a
    consumer comparing revisions is comparing membership rather than clocks).
    """
    material = json.dumps(
        [
            [
                item.resource_id,
                item.provider,
                item.provider_reference,
                item.kind,
                sorted(item.operation_keys),
            ]
            for item in sorted(resources, key=lambda item: item.resource_id)
        ],
        separators=(",", ":"),
    )
    return "inventory-" + hashlib.sha256(material.encode()).hexdigest()


def _handles_digest(references: set[str] | frozenset[str]) -> str:
    """A content digest of one provider's handles, for comparing a listing to
    membership.

    Sorted, so the digest is a property of the SET rather than of the order a listing
    happened to arrive in -- otherwise a provider returning the same resources in a
    different order would read as a mismatch and make completeness unachievable.

    Separate from `_revision`, which digests full member identities including kind and
    step attribution. This one answers only "are these the same handles?", which is the
    question a provider listing can actually settle: the provider knows its own
    references and knows nothing about how they were recorded here.
    """
    material = json.dumps(sorted(references), separators=(",", ":"))
    return "handles-" + hashlib.sha256(material.encode()).hexdigest()


def _listings_digest(generations: Mapping[str, int]) -> str:
    """A content digest naming WHICH provider listings were current.

    The value a report carries so that re-asking a provider invalidates it. Every
    `(provider, generation)` pair the allocation has on record goes in, so the digest
    changes when any listing is replaced, when a provider's listing appears, and when
    one is removed -- each of which means the evidence a report was taken against is no
    longer the evidence in force.

    Generations rather than digests of the handles: two consecutive listings can return
    byte-identical handles and still be two different questions asked at two different
    moments, and only the second one is evidence about now. Digesting the handles would
    make the re-ask invisible, which is precisely the defect.

    An empty mapping still produces a stable value rather than an empty string, so
    "no listings on record" is a comparable state instead of a falsy one that a
    truthiness test could confuse with a missing column.
    """
    material = json.dumps(
        sorted((provider, int(value)) for provider, value in generations.items()),
        separators=(",", ":"),
    )
    return "listings-" + hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class _ObservedReport(Mapping):
    """A query result minted by this authority, bound before provider I/O."""

    issuer: object
    lease: ExecutionLease
    sealed: str
    binding: str
    observations: Mapping[str, ResourceObservation]

    def __post_init__(self):
        object.__setattr__(
            self, "observations", MappingProxyType(dict(self.observations))
        )

    def __getitem__(self, key):
        return self.observations[key]

    def __iter__(self):
        return iter(self.observations)

    def __len__(self):
        return len(self.observations)


@dataclass
class InventoryAuthority:
    """The trusted side of allocation inventory, report attestation and cleanup.

    Composed with a connection factory, a verifier and a trusted query adapter.
    `authenticate`
    resolves the opaque `operation_authority` the consumer presents into a grant tied to
    an operation, attempt, holder and fence -- the same injected-verifier shape
    `ExecutionRPCServer` uses, and for the same reason: a package that could mint the
    credential it checks is a package whose authority check is decorative.

    Both fields are required. A default verifier that accepted anything would make "is
    this authority checked" a property of how the composer happened to call this class
    rather than of the type, which is the failure `facade.OperationFacadeService`'s
    `__post_init__` refuses for the approval source and the ledger.
    """

    connect: Callable[[], AbstractAsyncContextManager[Connection]]
    authenticate: Callable[[str], Awaitable[ExecutionGrant]] = None  # type: ignore[assignment]
    store: OperationStore = None  # type: ignore[assignment]
    query_provider: Callable | None = None

    def __post_init__(self) -> None:
        if self.connect is None or self.authenticate is None:
            raise ContractViolation(
                "an inventory authority requires a connection factory and an "
                "authority verifier; neither has a safe default"
            )
        if self.store is None:
            self.store = OperationStore()

    async def _hold_allocation(
        self, connection: Connection, lease: ExecutionLease
    ) -> str:
        """Take both locks in the one safe order, and return the allocation.

        Every write on this surface needs two things serialized: authority over the
        OPERATION (the lease lock) and exclusive claim on the ALLOCATION (the advisory
        lock). Both, in the same order, from one place -- because two callers acquiring
        the same pair in opposite orders deadlock, and "which order did that method use"
        is exactly the detail a later edit gets wrong.

        The allocation lock is taken FIRST. That requires knowing the allocation before
        holding the lease lock, so the record is read once beforehand -- safe because
        the allocation id comes from the admitted, digest-bound plan
        (`allocation_id_for`) and is therefore immutable for the life of the operation.
        The read is still tenant-scoped, so an operation under another tenant is
        `unknown` here exactly as it was before.

        `lock_lease` runs after both, and its refusal is still the authority check: a
        caller may hold the allocation lock and have no right to write.
        """
        record = await self.store.get(connection, _principal(lease), lease.operation_id)
        if record is None:
            raise _Refused("no operation record under the resolved tenant", "unknown")
        allocation_id = allocation_id_for(record)
        await _lock_allocation(
            connection,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            allocation_id=allocation_id,
        )
        if not await lock_lease(connection, lease):
            raise _Refused(
                "a live lease is required to write allocation inventory", "fenced"
            )
        return allocation_id

    # ------------------------------------------------------------------
    # The executor's side: publish evidence under a live fence
    # ------------------------------------------------------------------

    async def observe_report(self, connection, lease):
        """Run the trusted provider query after the current listing and seal.

        The composer supplies query_provider(lease, resources, query_id), which
        reads each durable provider handle afresh. Request data cannot supply
        observations.
        No lock spans external I/O: publication rechecks the captured generation,
        so a concurrent listing/creation/recovery invalidates the entire batch.
        Query receipts cannot be rebound, even when the provider state is identical.
        """
        if connection.is_in_transaction():
            raise ContractViolation("provider query requires an owned transaction")
        nonce = secrets.token_hex(16)
        if self.query_provider is None:
            raise ContractViolation(
                "a trusted fresh provider-query adapter is required"
            )
        _require_lease(lease)
        try:
            async with connection.transaction():
                allocation_id = await self._hold_allocation(connection, lease)
                sealed = await self._sealed_revision(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                if sealed is None:
                    raise _Refused(
                        "allocation is not sealed; query refused", "unsealed"
                    )
                resources = await self._membership(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                missing = await self._providers_without_enumeration(
                    connection,
                    lease=lease,
                    allocation_id=allocation_id,
                    resources=resources,
                )
                if missing:
                    raise _Refused(
                        "no current provider enumeration backs the query for: "
                        + ", ".join(missing),
                        "missing enumeration",
                    )
                binding = await self._enumeration_binding(
                    connection, lease=lease, allocation_id=allocation_id
                )
                await self._advance_query(connection, lease, allocation_id, nonce)
        except _Refused as refusal:
            await self._audit(
                connection, lease, "report.refused", False, refusal.detail
            )
            raise OperationRefused(str(refusal)) from None
        observed = await self.query_provider(lease, resources, nonce)
        _canonical_observations(observed)
        if not observed or any(item.observation_id for item in observed.values()):
            raise ContractViolation(
                "provider query must return fresh, unstamped observations"
            )
        stamped = {
            key: replace(item, observation_id=nonce) for key, item in observed.items()
        }
        return _ObservedReport(self, lease, sealed, binding, stamped)

    async def publish_report(
        self,
        connection: Connection,
        lease: ExecutionLease,
        *,
        observations: Mapping[str, ResourceObservation],
    ) -> str:
        """Publish only the receipt produced by observe_report.

        Retries of one receipt are idempotent. A different listing cannot update
        its binding, and identical state from a new query has a different nonce
        and digest. Cached payloads alone cannot acquire publication authority.
        """
        _require_lease(lease)
        if not observations:
            # An empty report cannot attest anything, and the consumer treats an empty
            # observation set alongside a non-empty allocation as unresolved
            # (`accounting.py:243`). Refused here so the executor learns at publication
            # rather than discovering it as an unexplained retained exposure later.
            raise ContractViolation(
                "a provider report must carry at least one observation"
            )
        if (
            not isinstance(observations, _ObservedReport)
            or observations.issuer is not self
            or observations.lease != lease
        ):
            raise ContractViolation(
                "publication requires this authority's provider-query receipt"
            )
        payload = _canonical_observations(observations)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        # The refusal audit is written in the `except` clause, OUTSIDE the transaction
        # the refusal aborts -- the same structure `execution.py:938` uses. Auditing
        # before the `raise` from inside the block would roll the audit row back along
        # with everything else, which loses precisely the security-relevant half of the
        # record: a refused write leaves no trace anywhere else by design, so if the
        # audit dies with the transaction the fence doing its job is invisible.
        try:
            async with connection.transaction():
                allocation_id = await self._hold_allocation(connection, lease)
                # Read inside the same locked transaction the row is written in, so a
                # concurrent seal cannot move the revision between this read and the
                # INSERT: the allocation lock is held by whichever of the two got it
                # first, and the loser sees the other's committed state.
                sealed = await self._sealed_revision(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                if sealed is None:
                    raise _Refused(
                        "the allocation is not sealed; a provider report published "
                        "before membership is final cannot attest to it",
                        "unsealed",
                    )
                # The listing requirement is checked against membership rather than
                # against whatever listings happen to exist: a provider that appears in
                # membership and has no current listing from this grant means the report
                # would be taken against evidence nobody has gathered yet, which is the
                # publish-then-list order this repair removes.
                resources = await self._membership(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                missing = await self._providers_without_enumeration(
                    connection,
                    lease=lease,
                    allocation_id=allocation_id,
                    resources=resources,
                )
                if missing:
                    raise _Refused(
                        "no current provider enumeration backs this report for: "
                        f"{', '.join(missing)}; a report must be taken after the "
                        "provider was last asked what it holds",
                        f"missing enumeration for {len(missing)} provider(s)",
                    )
                # Read in the same locked transaction as the write, for the same reason
                # the seal is: a concurrent `record_provider_enumeration` holds the
                # allocation lock, so the generations committed here are the ones no
                # other writer can still be moving.
                binding = await self._enumeration_binding(
                    connection, lease=lease, allocation_id=allocation_id
                )
                current_query = await self._current_query(
                    connection, lease, allocation_id
                )
                if {item.observation_id for item in observations.values()} != {
                    current_query
                }:
                    raise _Refused(
                        "provider observation was superseded by a newer query",
                        "stale observation",
                    )
                if (observations.sealed, observations.binding) != (sealed, binding):
                    raise _Refused(
                        "provider observation predates the current listing",
                        "stale observation",
                    )
                # One statement: insert, or -- when this exact binding already exists --
                # return the stored row. `DO UPDATE` rather than `DO NOTHING` because
                # `DO NOTHING` returns no row on conflict, and a caller that cannot see
                # what is stored cannot tell whether the attestation is its own. The
                # update is a no-op assignment of the payload for that reason alone; the
                # payload cannot differ, since the digest is derived from it and is part
                # of the key.
                stored = await connection.fetchrow(
                    """
                    INSERT INTO harness_provider_report (
                        report_digest, operation_id, org_id, workspace_id, attempt_id,
                        executor_id, fence_token, allocation_id, observations,
                        sealed_revision, enumeration_binding
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (
                        report_digest, operation_id, org_id, workspace_id, attempt_id,
                        executor_id, fence_token
                    ) DO UPDATE SET observations = harness_provider_report.observations
                    RETURNING operation_id, org_id, workspace_id, attempt_id,
                              executor_id, fence_token, allocation_id, observations,
                              sealed_revision, enumeration_binding
                    """,
                    digest,
                    lease.operation_id,
                    lease.org_id,
                    lease.workspace_id,
                    lease.attempt_id,
                    lease.holder,
                    lease.fence_token,
                    allocation_id,
                    payload,
                    sealed,
                    binding,
                )
                # The returned row is CHECKED rather than assumed. An upsert that
                # reported success while the row on disk belonged to someone else would
                # be the same false-success defect in a new place, and the cost of
                # getting it wrong is an attestation that authorizes a cleanup its
                # publisher never made.
                if stored is None or (
                    stored["operation_id"],
                    stored["org_id"],
                    stored["workspace_id"],
                    stored["attempt_id"],
                    stored["executor_id"],
                    stored["fence_token"],
                    stored["allocation_id"],
                    str(stored["observations"]),
                    stored["sealed_revision"],
                    stored["enumeration_binding"],
                ) != (
                    lease.operation_id,
                    lease.org_id,
                    lease.workspace_id,
                    lease.attempt_id,
                    lease.holder,
                    lease.fence_token,
                    allocation_id,
                    payload,
                    sealed,
                    binding,
                ):
                    raise _Refused(
                        "the stored attestation does not match this publication",
                        "attestation mismatch",
                    )
                await self._audit(
                    connection, lease, "report.published", True, digest[:32]
                )
        except _Refused as refusal:
            await self._audit(
                connection, lease, "report.refused", False, refusal.detail
            )
            raise OperationRefused(str(refusal)) from None
        return digest

    async def enumerate_resources(
        self,
        connection: Connection,
        lease: ExecutionLease,
        *,
        resources: tuple[AllocationResource, ...],
    ) -> int:
        """Record allocation membership under this holder's fence. Returns rows added.

        Additive by construction: a member already present has its `operation_keys`
        merged and is otherwise left alone. Membership that could shrink is membership
        that can be made to look complete by deleting the inconvenient row, and
        `complete` is the flag that authorizes returning money.

        A resource whose provider identity *changes* is refused rather than updated. The
        stored reference is what a teardown will query; overwriting it means the earlier
        resource stops being reachable by any record here: it keeps running and nothing
        knows to ask about it. The consumer refuses the same case
        (`provider_handles.py:869`).

        Refused outright once the allocation is SEALED. A sealed allocation has had a
        release-authorizing snapshot taken over it, and membership that can still grow
        after that point is membership a release was authorized against and then
        invalidated -- the F2 defect. Growth after sealing is a real event that needs a
        real response (a new allocation, or an operator looking at why something was
        created after teardown began), so it is refused and audited rather than
        absorbed.
        """
        _require_lease(lease)
        if not isinstance(resources, tuple) or not resources:
            raise ContractViolation("enumerating membership requires resources")
        if len(resources) > MAX_INVENTORY_RESOURCES:
            raise ContractViolation(
                f"an allocation enumerates at most {MAX_INVENTORY_RESOURCES} resources"
            )
        for item in resources:
            if not isinstance(item, AllocationResource):
                raise ContractViolation("resources must be AllocationResource values")
        if len({item.resource_id for item in resources}) != len(resources):
            raise ContractViolation("resource ids must be distinct within one call")
        added = 0
        try:
            async with connection.transaction():
                allocation_id = await self._hold_allocation(connection, lease)
                # The seal is read INSIDE the transaction holding the ALLOCATION lock,
                # so a seal committed concurrently either precedes this write and
                # refuses it, or follows it and seals the grown membership. There is
                # no interleaving in which both succeed against different views.
                #
                # The allocation lock rather than the lease lock is what makes that true
                # across operations. Two separately approved operations naming the same
                # allocation hold two different lease locks, so with only those the
                # sequence this refusal exists to prevent was still available: one
                # operation checks for a seal, another reads membership and seals it, a
                # release is authorized, and the first then commits a late resource.
                _, quarantined = await self._epoch(connection, lease, allocation_id)
                if not quarantined and await self._sealed_revision(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                ):
                    raise _Refused(
                        "the allocation is sealed; membership cannot grow after a "
                        "release-authorizing snapshot",
                        "sealed",
                    )
                for item in resources:
                    prior = await connection.fetchrow(
                        "SELECT provider, provider_reference, kind, operation_keys "
                        "FROM harness_allocation_resource WHERE org_id=$1 AND "
                        "workspace_id=$2 AND allocation_id=$3 AND resource_id=$4 "
                        "FOR UPDATE",
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                        item.resource_id,
                    )
                    if prior is not None:
                        if (
                            prior["provider"],
                            prior["provider_reference"],
                            prior["kind"],
                        ) != (item.provider, item.provider_reference, item.kind):
                            raise _Refused(
                                "membership cannot change a persisted resource "
                                "identity",
                                f"identity change for {item.resource_id}",
                            )
                        merged = sorted(
                            set(prior["operation_keys"]) | item.operation_keys
                        )
                        await connection.execute(
                            "UPDATE harness_allocation_resource SET operation_keys=$5 "
                            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 "
                            "AND resource_id=$4",
                            lease.org_id,
                            lease.workspace_id,
                            allocation_id,
                            item.resource_id,
                            merged,
                        )
                        continue
                    await connection.execute(
                        """
                        INSERT INTO harness_allocation_resource (
                            org_id, workspace_id, allocation_id, resource_id,
                            operation_id, provider, provider_reference, kind,
                            operation_keys, attempt_id, fence_token
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        """,
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                        item.resource_id,
                        lease.operation_id,
                        item.provider,
                        item.provider_reference,
                        item.kind,
                        sorted(item.operation_keys),
                        lease.attempt_id,
                        lease.fence_token,
                    )
                    added += 1
                await self._audit(
                    connection,
                    lease,
                    "inventory.enumerated",
                    True,
                    f"{added} added of {len(resources)}",
                )
        except _Refused as refusal:
            await self._audit(
                connection, lease, "inventory.refused", False, refusal.detail
            )
            raise OperationRefused(str(refusal)) from None
        return added

    async def begin_provider_enumeration(
        self, connection: Connection, lease: ExecutionLease, *, provider: str
    ) -> ProviderEnumeration:
        """Durably open a uniquely identified listing before provider I/O.

        An active listing cannot be replaced by a report query or another listing.
        Failed listings require a fresh successful listing. A successor may replace
        an abandoned listing only after its recorded execution grant is no longer live.
        """
        if connection.is_in_transaction():
            raise ContractViolation(
                "provider listing query requires an owned transaction"
            )
        _require_lease(lease)
        _text(provider, "provider", _MAX_PROVIDER)
        try:
            async with connection.transaction():
                allocation_id = await self._hold_allocation(connection, lease)
                previous = await self._listing_row(
                    connection, lease, allocation_id, provider
                )
                if previous and previous["state"] == "in_progress":
                    live = await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM harness_operation_leases "
                        "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
                        "AND holder=$4 AND attempt_id=$5 AND fence_token=$6 "
                        "AND closed_at IS NULL AND expires_at>now() "
                        "AND runtime_deadline>now())",
                        previous["operation_id"],
                        lease.org_id,
                        lease.workspace_id,
                        previous["executor_id"],
                        previous["attempt_id"],
                        previous["fence_token"],
                    )
                    if live:
                        raise _Refused(
                            "provider listing is still in progress",
                            "listing in progress",
                        )
                generation, _ = await self._epoch(connection, lease, allocation_id)
                query_id = await self._advance_query(connection, lease, allocation_id)
                await connection.execute(
                    """INSERT INTO harness_provider_listing
                    (org_id,workspace_id,allocation_id,provider,query_id,operation_id,
                     attempt_id,executor_id,fence_token,generation,state)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'in_progress')
                    ON CONFLICT (org_id,workspace_id,allocation_id,provider)
                    DO UPDATE SET query_id=EXCLUDED.query_id,
                      operation_id=EXCLUDED.operation_id,
                      attempt_id=EXCLUDED.attempt_id, executor_id=EXCLUDED.executor_id,
                      fence_token=EXCLUDED.fence_token, generation=EXCLUDED.generation,
                      state='in_progress'""",
                    lease.org_id,
                    lease.workspace_id,
                    allocation_id,
                    provider,
                    query_id,
                    lease.operation_id,
                    lease.attempt_id,
                    lease.holder,
                    lease.fence_token,
                    generation,
                )
                await self._audit(
                    connection,
                    lease,
                    "inventory.listing_started",
                    True,
                    f"{provider}: {query_id}",
                )
                return ProviderEnumeration(query_id, provider, generation)
        except _Refused as refusal:
            raise OperationRefused(str(refusal)) from None

    async def _listing_row(self, connection, lease, allocation_id, provider):
        return await connection.fetchrow(
            "SELECT * FROM harness_provider_listing "
            "WHERE org_id=$1 AND workspace_id=$2 "
            "AND allocation_id=$3 AND provider=$4",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
            provider,
        )

    async def _require_listing(self, connection, lease, allocation_id, attempt):
        if not isinstance(attempt, ProviderEnumeration):
            raise ContractViolation("provider listing requires its exact begin token")
        row = await self._listing_row(
            connection, lease, allocation_id, attempt.provider
        )
        if not row or (
            row["query_id"],
            row["generation"],
            row["operation_id"],
            row["attempt_id"],
            row["executor_id"],
            row["fence_token"],
            row["state"],
        ) != (
            attempt.query_id,
            attempt.generation,
            lease.operation_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
            "in_progress",
        ):
            raise _Refused(
                "provider listing token is stale or belongs to another grant",
                "stale listing token",
            )

    async def _finish_listing(self, connection, lease, allocation_id, attempt, state):
        await connection.execute(
            "UPDATE harness_provider_listing SET state=$5 "
            "WHERE org_id=$1 AND workspace_id=$2 "
            "AND allocation_id=$3 AND query_id=$4",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
            attempt.query_id,
            state,
        )

    async def fail_provider_enumeration(self, connection, lease, *, attempt):
        """Record a failed provider call; release remains blocked until a fresh listing.

        This trusted service path runs only after the provider call has returned
        unsuccessfully. It is not an authority to cancel a still-running listing.
        A crash instead leaves in_progress until the execution grant expires.
        """
        if connection.is_in_transaction():
            raise ContractViolation(
                "provider listing failure requires an owned transaction"
            )
        _require_lease(lease)
        try:
            async with connection.transaction():
                allocation_id = await self._hold_allocation(connection, lease)
                await self._require_listing(connection, lease, allocation_id, attempt)
                await self._finish_listing(
                    connection, lease, allocation_id, attempt, "failed"
                )
                await self._audit(
                    connection,
                    lease,
                    "inventory.listing_failed",
                    True,
                    f"{attempt.provider}: {attempt.query_id}",
                )
        except _Refused as refusal:
            raise OperationRefused(str(refusal)) from None

    async def _epoch(self, connection, lease, allocation_id):
        return await allocation_epoch(
            connection,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            allocation_id=allocation_id,
        )

    async def record_provider_enumeration(
        self,
        connection: Connection,
        lease: ExecutionLease,
        *,
        provider: str,
        provider_references: frozenset[str],
        attempt: ProviderEnumeration,
    ) -> None:
        """Record that the provider was asked to list what it holds, and what it said.

        This is the trusted completeness proof, and it exists because membership cannot
        prove its own completeness. The previous rule checked that every succeeded
        call's
        own `(provider, provider_ref)` was enumerated -- a check that the executor wrote
        down what it was already telling us about. It cannot see the case that costs
        money: one provider call creating several independently billed resources, where
        the cluster handle is recorded and its disk is not. That inventory read as
        complete, an ABSENT report on the cluster produced RELEASED with zero exposure,
        and the disk kept billing.

        `provider_references` must be what the PROVIDER returned for this allocation --
        an enumeration call, not a restatement of what was recorded here. The write is
        refused when the provider names a handle that is not already a member, so the
        omitted disk is caught by the provider contradicting the executor rather than
        by trusting the executor's count. It is also refused before creation has
        finished: a listing taken while the plan is still running cannot vouch for
        resources that do not exist yet, and permitting it would let an early listing
        be the proof for a membership that grew afterwards.

        A handle that is a MEMBER but absent from the listing is not refused here.
        That is a resource the provider says is already gone, which is ordinary during
        teardown; `assess_cleanup` reconciles it against the observations and retains
        exposure if it cannot be established. Refusing it would make enumeration
        impossible to record for any allocation partway through cleanup.
        """
        if connection.is_in_transaction():
            raise ContractViolation(
                "provider enumeration requires an owned transaction"
            )
        _require_lease(lease)
        _text(provider, "provider", _MAX_PROVIDER)
        if not isinstance(provider_references, frozenset):
            raise ContractViolation("provider_references must be a frozenset")
        if len(provider_references) > MAX_INVENTORY_RESOURCES:
            raise ContractViolation(
                f"a provider enumeration carries at most {MAX_INVENTORY_RESOURCES} "
                "handles"
            )
        for reference in provider_references:
            _text(reference, "provider_reference", _MAX_PROVIDER_REFERENCE)
        if not isinstance(attempt, ProviderEnumeration) or attempt.provider != provider:
            raise ContractViolation(
                "provider listing requires its exact begin token and provider"
            )
        generation = attempt.generation
        refusal_after_commit = None
        try:
            async with connection.transaction():
                # Under the allocation lock as well as the lease lock: this listing is
                # read as a completeness proof, so it must not be recorded against a
                # membership another operation is concurrently changing.
                allocation_id = await self._hold_allocation(connection, lease)
                await self._require_listing(connection, lease, allocation_id, attempt)
                if await confirmed_plan_progress(
                    connection, lease.operation_id
                ) is not (PlanProgress.COMPLETE):
                    refusal_after_commit = _Refused(
                        "resource creation has not finished under the fence; an "
                        "enumeration taken mid-creation cannot prove completeness",
                        "creation unfinished",
                    )
                current_generation, _ = await self._epoch(
                    connection, lease, allocation_id
                )
                members = await self._membership(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                known = {
                    item.provider_reference
                    for item in members
                    if item.provider == provider
                }
                unaccounted = sorted(provider_references - known)
                if unaccounted:
                    # Commit the contradiction before returning a refusal. Keep the
                    # seal (creation fence), but allow bookkeeping for discovered
                    # members until clean re-enumeration and re-sealing complete.
                    await connection.execute(
                        "INSERT INTO harness_allocation_epoch "
                        "(org_id,workspace_id,allocation_id,generation,quarantined) "
                        "VALUES ($1,$2,$3,1,true) ON CONFLICT "
                        "(org_id,workspace_id,allocation_id) "
                        "DO UPDATE SET "
                        "generation=harness_allocation_epoch.generation+1, "
                        "quarantined=true",
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                    )
                    for reference in unaccounted:
                        await connection.execute(
                            "INSERT INTO harness_allocation_discovery "
                            "(org_id,workspace_id,allocation_id,provider,provider_ref) "
                            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                            lease.org_id,
                            lease.workspace_id,
                            allocation_id,
                            provider,
                            reference,
                        )
                    refusal_after_commit = _Refused(
                        "the provider holds resources this allocation does not "
                        f"enumerate: {', '.join(unaccounted[:8])}",
                        f"{len(unaccounted)} unaccounted handle(s)",
                    )
                elif generation != current_generation:
                    refusal_after_commit = _Refused(
                        "provider activity changed during enumeration; query again",
                        "stale provider enumeration",
                    )
                elif refusal_after_commit is None:
                    await connection.execute(
                        """
                        INSERT INTO harness_allocation_enumeration (
                            org_id, workspace_id, allocation_id, provider,
                            enumerated_digest, handle_count, operation_id, attempt_id,
                            executor_id, fence_token, allocation_generation
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (
                            org_id, workspace_id, allocation_id, provider, operation_id,
                            attempt_id, executor_id, fence_token
                        )
                        DO UPDATE SET
                            enumerated_digest = EXCLUDED.enumerated_digest,
                            handle_count = EXCLUDED.handle_count,
                            allocation_generation = EXCLUDED.allocation_generation,
                            -- Every new listing supersedes this grant's reports.
                            generation =
                                harness_allocation_enumeration.generation + 1,
                            recorded_at = now()
                        """,
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                        provider,
                        _handles_digest(known),
                        len(provider_references),
                        lease.operation_id,
                        lease.attempt_id,
                        lease.holder,
                        lease.fence_token,
                        generation,
                    )
                    await self._audit(
                        connection,
                        lease,
                        "inventory.enumeration_recorded",
                        True,
                        f"{provider}: {len(provider_references)} handle(s)",
                    )
                await self._finish_listing(
                    connection,
                    lease,
                    allocation_id,
                    attempt,
                    "failed" if refusal_after_commit is not None else "completed",
                )
            if refusal_after_commit is not None:
                raise refusal_after_commit
        except _Refused as refusal:
            await self._audit(
                connection,
                lease,
                "inventory.enumeration_refused",
                False,
                refusal.detail,
            )
            raise OperationRefused(str(refusal)) from None

    async def seal_allocation(
        self, connection: Connection, lease: ExecutionLease
    ) -> str:
        """Close the allocation to further membership. Returns the sealed revision.

        A release-authorizing snapshot must be the last word on what the allocation
        contains. `read_inventory` necessarily releases its transaction before the
        domain applies the result, and while membership remained writable under the
        same lease, the answer "this allocation holds only the cluster, and the cluster
        is gone" could be true when computed and false when acted on -- a release
        followed by growth. No amount of locking inside a read fixes that, because the
        gap is outside the read.

        So sealing is an explicit write taken under the live fence, and it is what
        makes later additions refused rather than merely unlikely. Only a sealed
        allocation is ever reported complete, so no release can be authorized against
        a list still open to additions.

        Requires creation to have finished and a current provider enumeration for
        every provider in membership -- the same conditions `_completeness`
        re-derives. Checked here as well so an executor learns at seal time, where it
        can still act, rather than by discovering a permanently incomplete inventory
        later.

        **Also requires that no creating provider call against this allocation is
        unaccounted for, from ANY operation** (`allocation
        .creating_calls_unaccounted_for`). This is the half of the creation fence that
        lives on this side; `execution` owns the other half, which refuses a creating
        call into an already-sealed allocation. Neither is sufficient alone: refusing
        the call only closes the window after this row commits, and there is a window
        before it in which another operation's intent is committed and its provider
        call is in flight. Sealing inside that window would certify an inventory that
        cannot include what that call is creating, and then authorize releasing the
        budget for it.

        Checked against the other operation's durable intent row rather than by holding
        a lock across its provider I/O: a lock held across a call that may take minutes
        would make sealing block on an unrelated provider's latency, and a lock is lost
        on a crash while the intent row is not.

        Idempotent for the same membership: a retried seal after a transport failure
        converges on the one row. Re-sealing DIFFERENT membership is refused -- that
        is an allocation that grew after being declared final.
        """
        _require_lease(lease)
        try:
            async with connection.transaction():
                # The allocation lock is what makes the seal ORDER against membership
                # writes from other operations, not merely against this one's. It is
                # taken before the membership read below, so the resources this seal
                # commits to are the resources no concurrent writer can still add to.
                allocation_id = await self._hold_allocation(connection, lease)
                resources = await self._membership(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                if not resources:
                    # Sealing an empty allocation would produce a complete, empty
                    # inventory -- which reads as "nothing to clean up" and releases
                    # everything. An allocation with no enumerated members has either
                    # created nothing or enumerated nothing, and this module cannot tell
                    # those apart, so it refuses to certify either.
                    raise _Refused(
                        "an allocation with no enumerated members cannot be sealed",
                        "empty membership",
                    )
                if await confirmed_plan_progress(
                    connection, lease.operation_id
                ) is not (PlanProgress.COMPLETE):
                    raise _Refused(
                        "resource creation has not finished under the fence",
                        "creation unfinished",
                    )
                outstanding = await creating_calls_unaccounted_for(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                    known={
                        (item.provider, item.provider_reference) for item in resources
                    },
                )
                if outstanding:
                    # The other half of the creation fence. `execution` refuses a
                    # creating call into an allocation this row has already closed; this
                    # refuses to close one while such a call is still in flight or has
                    # produced something membership does not name. Without it, sealing
                    # between another operation's committed intent and its provider call
                    # would certify an inventory that provably cannot include what that
                    # call creates -- and then release the budget for it.
                    #
                    # Allocation-wide, and it counts calls from every operation, because
                    # the seal is a claim about the allocation rather than about the
                    # sealer. The plan-progress check above only covers this operation's
                    # own calls, which is precisely the gap: the expensive case is
                    # another operation's call.
                    raise _Refused(
                        "a provider call that can create into this allocation is still "
                        f"unaccounted for: {', '.join(outstanding[:8])}",
                        f"{len(outstanding)} unaccounted creating call(s)",
                    )
                missing = await self._providers_without_enumeration(
                    connection,
                    lease=lease,
                    allocation_id=allocation_id,
                    resources=resources,
                )
                if missing:
                    raise _Refused(
                        "no current provider enumeration proves membership complete "
                        f"for: {', '.join(missing)}",
                        f"missing enumeration for {len(missing)} provider(s)",
                    )
                discovered = await connection.fetch(
                    "SELECT provider, provider_ref FROM harness_allocation_discovery "
                    "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 LIMIT $4",
                    lease.org_id,
                    lease.workspace_id,
                    allocation_id,
                    MAX_INVENTORY_RESOURCES + 1,
                )
                known = {(r.provider, r.provider_reference) for r in resources}
                if len(discovered) > MAX_INVENTORY_RESOURCES or any(
                    (r["provider"], r["provider_ref"]) not in known for r in discovered
                ):
                    raise _Refused(
                        "discovered provider resources must be recorded "
                        "before resealing",
                        "unrecorded discovery",
                    )
                revision = _revision(resources)
                _, quarantined = await self._epoch(connection, lease, allocation_id)
                if quarantined:
                    await connection.execute(
                        "UPDATE harness_allocation_seal SET sealed_revision=$4 "
                        "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                        revision,
                    )
                    await connection.execute(
                        "UPDATE harness_allocation_epoch SET quarantined=false "
                        "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                        lease.org_id,
                        lease.workspace_id,
                        allocation_id,
                    )
                sealed = await connection.fetchval(
                    """
                    INSERT INTO harness_allocation_seal (
                        org_id, workspace_id, allocation_id, sealed_revision,
                        operation_id, attempt_id, executor_id, fence_token
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT (org_id, workspace_id, allocation_id)
                    DO UPDATE SET sealed_revision = harness_allocation_seal
                                      .sealed_revision
                    RETURNING sealed_revision
                    """,
                    lease.org_id,
                    lease.workspace_id,
                    allocation_id,
                    revision,
                    lease.operation_id,
                    lease.attempt_id,
                    lease.holder,
                    lease.fence_token,
                )
                # `DO UPDATE` keeping the EXISTING revision, then comparing: re-sealing
                # the same membership returns it unchanged and converges, while a seal
                # over membership that has since moved is visible here as a difference
                # rather than overwriting the earlier claim.
                if sealed != revision:
                    raise _Refused(
                        "the allocation is already sealed over different membership",
                        "seal revision conflict",
                    )
                await self._audit(
                    connection, lease, "inventory.sealed", True, revision[:32]
                )
        except _Refused as refusal:
            await self._audit(
                connection, lease, "inventory.seal_refused", False, refusal.detail
            )
            raise OperationRefused(str(refusal)) from None
        return revision

    # ------------------------------------------------------------------
    # The consumer's side: the `allocation_inventory` port
    # ------------------------------------------------------------------

    async def read_inventory(
        self,
        *,
        executor_id: str,
        workspace_id: str,
        allocation_id: str,
        operation_authority: str,
        report_digest: str,
    ) -> VerifiedInventory | None:
        """Authoritative membership, or `None` meaning unavailable/unverified.

        `None` for every failure, and deliberately one answer for all of them: the
        consumer's contract declares `NONE_MEANS_UNVERIFIED`, and it turns `None` into
        retained exposure. A distinguishing exception would tell a caller which of
        "no such allocation", "not your allocation" and "your fence is stale" applies,
        which is a probe for another tenant's allocations.

        Every check is a reason a release must not proceed:

        * the authority does not resolve, or resolves to a different executor or
          workspace than the one asking -- one executor's credential must not authorize
          another's cleanup;
        * the lease is not held *now*. Checked with `lock_lease` rather than by reading
          the stored token, because authority is a question about the present: a
          well-formed token whose lease has since been taken over by recovery is
          precisely the stale-fence case, and it looks identical to a valid one until
          compared against the live row;
        * the allocation asked about is not the one the approved plan binds;
        * the attestation is missing, belongs to another operation, or mismatches the
          digest recomputed from its stored payload;
        * the attestation was published against a DIFFERENT sealed revision than the one
          in force now -- which is how a report taken before the membership it clears
          became final is refused rather than honoured.

        A database that cannot be reached is `None` as well, and not a raised error. The
        distinction between "unverified" and "unreachable" is real but not one this
        return value can carry safely: the consumer's only two behaviours are "use this
        inventory" and "retain exposure", so anything other than a verified answer must
        arrive as the second. Raising would also make an outage indistinguishable
        at the call site from a *refusal*, and refusals here include "that allocation is
        not yours".
        """
        try:
            _text(executor_id, "executor_id", _MAX_RESOURCE_ID)
            _text(workspace_id, "workspace_id", _MAX_RESOURCE_ID)
            _text(allocation_id, "allocation_id", _MAX_RESOURCE_ID)
            _text(report_digest, "report_digest", _MAX_RESOURCE_ID)
            _text(operation_authority, "operation_authority", 8192)
        except ContractViolation:
            return None
        grant = await self._grant(operation_authority)
        if grant is None:
            return None
        lease = grant.lease
        if (lease.holder, lease.workspace_id) != (executor_id, workspace_id):
            return None
        try:
            async with self.connect() as connection, connection.transaction():
                if not await lock_lease(connection, lease):
                    return None
                record = await self.store.get(
                    connection, grant.principal, lease.operation_id
                )
                if record is None:
                    return None
                approved = allocation_id_for(record)
                if approved != allocation_id:
                    return None
                # The seal is read ONCE and both the attestation check and the
                # completeness check are made against that one value. Reading it twice
                # would let them disagree about which revision was in force, and the
                # disagreement that matters is "the report matched the old seal and
                # completeness matched the new one" -- a release authorized by two
                # checks that were each looking at a different allocation.
                sealed = await self._sealed_revision(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                # The listing binding is read the same way and for the same reason: one
                # value, used by the attestation check here and never re-read, so the
                # report cannot be verified against one set of listings while
                # completeness is satisfied by another. That disagreement was the whole
                # retroactive-validation defect -- the report matched the evidence
                # before the re-ask, completeness matched the evidence after it, and
                # each check was looking at a different moment.
                binding = await self._enumeration_binding(
                    connection, lease=lease, allocation_id=allocation_id
                )
                if not await self._verify_report(
                    connection,
                    lease=lease,
                    allocation_id=allocation_id,
                    digest=report_digest,
                    sealed_revision=sealed,
                    enumeration_binding=binding,
                ):
                    return None
                resources = await self._membership(
                    connection,
                    org_id=lease.org_id,
                    workspace_id=lease.workspace_id,
                    allocation_id=allocation_id,
                )
                complete = await self._completeness(
                    connection,
                    lease=lease,
                    allocation_id=allocation_id,
                    resources=resources,
                    sealed_revision=sealed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Broad on purpose, and the one place in this module that is. Every failure
            # in this block means "the answer is not verified": a refusal, a malformed
            # stored plan, a pool with no free connection, a dropped socket. The
            # consumer's contract is that anything other than a verified inventory
            # arrives as `None`, and it converts `None` into retained exposure -- so a
            # leaked exception would turn a transient database problem into a 500 on the
            # release path instead of the conservative "keep charging" it must be.
            # Cancellation is re-raised above: it is about this process, not the
            # authority.
            return None
        return VerifiedInventory(
            workspace=lease.workspace_id,
            org_id=lease.org_id,
            allocation_id=allocation_id,
            revision=_revision(resources),
            resources=resources,
            complete=complete,
            expires_at=lease.expires_at,
            executor_id=lease.holder,
            active=True,
            attested_report_digest=report_digest,
        )

    async def assess_cleanup(
        self,
        *,
        executor_id: str,
        workspace_id: str,
        allocation_id: str,
        operation_authority: str,
        observations: Mapping[str, ResourceObservation],
    ) -> CleanupAssessment:
        """Authorize cleanup and report what provider evidence permits.

        The digest is computed from `observations` here, so the attestation being
        verified against is the one the executor published rather than a value that
        arrived with the question. That is what makes locally submitted observations
        unable to manufacture cleanup authority: they hash to a digest no report row
        carries, the verification misses, and the answer is unresolved.

        Reconciles provider truth against *enumerated membership* before permitting any
        release. A resource the provider still holds settles its spend; one the provider
        confirms absent releases it; one the provider could not be consulted about is
        retained and named. Unknown is never folded into absent.
        """
        try:
            digest = report_digest(observations)
        except ContractViolation as exc:
            return self._unresolved(allocation_id, str(exc))
        inventory = await self.read_inventory(
            executor_id=executor_id,
            workspace_id=workspace_id,
            allocation_id=allocation_id,
            operation_authority=operation_authority,
            report_digest=digest,
        )
        if inventory is None:
            return self._unresolved(
                allocation_id,
                "allocation authority or provider-report attestation is not verified",
            )
        if not inventory.complete:
            return self._unresolved(
                allocation_id,
                "allocation membership is incomplete; resources may exist that are "
                "not enumerated, so exposure is retained rather than released",
                resources=tuple(
                    sorted(item.resource_id for item in inventory.resources)
                ),
                inventory=inventory,
            )
        return _reconcile(inventory, observations)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _grant(self, operation_authority: str) -> ExecutionGrant | None:
        """Resolve the opaque authority, or `None`.

        A verifier that raises is an unverified authority, not an authorized one --
        except for cancellation, which must propagate so a shutting-down process is not
        recorded as having answered.
        """
        try:
            grant = await self.authenticate(operation_authority)
        except asyncio.CancelledError:
            # Re-raised, not turned into `None`, for the reason `facade.py:668` gives:
            # cancellation is not unavailability. Swallowing it would record a
            # shutting-down process as having answered "not verified", which is a
            # verdict about the authority instead of about the shutdown.
            raise
        except Exception:
            return None
        if not isinstance(grant, ExecutionGrant):
            return None
        return grant

    async def _verify_report(
        self,
        connection: Connection,
        *,
        lease: ExecutionLease,
        allocation_id: str,
        digest: str,
        sealed_revision: str | None,
        enumeration_binding: str,
    ) -> bool:
        """Whether a stored attestation binds this digest to *this* grant.

        The digest is recomputed from the stored `observations` and compared with
        `hmac.compare_digest`, matching the rule `identity.py:628` states: a caller may
        never hand in a digest that is merely echoed. Constant-time because the
        comparison decides whether cleanup is authorized, and a timing oracle over a
        digest is a way to search for one.

        The lookup is by the FULL binding -- digest plus operation, tenant, attempt,
        holder and fence -- rather than by digest alone. The binding is supplied from
        the resolved grant, never from the request, so it is not something a caller
        can aim. Two consequences, both required:

        * A predecessor attempt's attestation of the same bytes does not authorize the
          current one. Since `attempt_id` and `fence_token` are part of the identity, an
          old grant's row simply is not found for a new grant -- it is not "found and
          then hopefully rejected", which is a check that can be forgotten.
        * An unrelated operation that happened to observe identical provider state has
          its own attestation and cannot vouch for this allocation.

        The binding also carries WHEN the report was taken, expressed as the membership
        revision the allocation was sealed over at publication, and that has to equal
        the revision it is sealed over now. Without it the report proved only "an
        authorized executor observed this", never "it observed this AFTER the membership
        being cleared was final" -- so a provider query made before anything was created
        could be published, the resource created and sealed afterwards, and the earlier
        ABSENT reused to release a running resource. An unsealed allocation matches no
        report at all: `sealed_revision` is `NOT NULL`, so there is nothing for `None`
        to equal, and publication into an open allocation is refused in the first place.

        And it carries which provider LISTINGS were current when the report was taken,
        which has to be the set current now. The seal answers "was membership final?"
        and that is a different question from "had the provider been asked yet?" -- so a
        report could be published, the provider asked afterwards, and the later listing
        satisfy completeness for the very read that honoured the earlier report. When
        the later listing contradicted the report by naming the handle as present, the
        contradicting evidence was what made the report valid. Comparing the binding
        makes replacing a listing invalidate every report published before it, so
        re-asking the provider forces a new report rather than rescuing an old one.
        """
        if sealed_revision is None:
            return False
        row = await connection.fetchrow(
            "SELECT allocation_id, observations FROM harness_provider_report "
            "WHERE report_digest=$1 AND operation_id=$2 AND org_id=$3 "
            "AND workspace_id=$4 AND attempt_id=$5 AND executor_id=$6 "
            "AND fence_token=$7 AND sealed_revision=$8 AND enumeration_binding=$9",
            digest,
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
            sealed_revision,
            enumeration_binding,
        )
        if row is None:
            return False
        if row["allocation_id"] != allocation_id:
            return False
        payload = json.loads(str(row["observations"]))
        nonces = {item.get("observation_id", "") for item in payload.values()}
        if len(nonces) != 1 or not next(iter(nonces), ""):
            return False  # Pre-query-receipt attestations cannot authorize release.
        if nonces != {await self._current_query(connection, lease, allocation_id)}:
            return False
        recomputed = hashlib.sha256(str(row["observations"]).encode()).hexdigest()
        return hmac.compare_digest(recomputed, digest)

    async def _advance_query(self, connection, lease, allocation_id, nonce=None):
        nonce = nonce or secrets.token_hex(16)
        await connection.execute(
            """
            INSERT INTO harness_provider_query
            (operation_id, org_id, workspace_id, attempt_id, executor_id,
             fence_token, observation_id, allocation_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (org_id, workspace_id, allocation_id)
            DO UPDATE SET observation_id=EXCLUDED.observation_id,
                          operation_id=EXCLUDED.operation_id,
                          attempt_id=EXCLUDED.attempt_id,
                          executor_id=EXCLUDED.executor_id,
                          fence_token=EXCLUDED.fence_token
            """,
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
            nonce,
            allocation_id,
        )
        return nonce

    async def _current_query(self, connection, lease, allocation_id):
        # All approved operations sharing this allocation share provider truth.
        # Attestations retain their individual grants; freshness spans those grants.
        return await connection.fetchval(
            "SELECT observation_id FROM harness_provider_query "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
        )

    async def _membership(
        self,
        connection: Connection,
        *,
        org_id: str,
        workspace_id: str,
        allocation_id: str,
    ) -> tuple[AllocationResource, ...]:
        """Every enumerated member of the allocation, in a stable order.

        Bounded by `MAX_INVENTORY_RESOURCES + 1` rather than exactly: reading one more
        than the ceiling is what makes "membership exceeds what may be enumerated"
        detectable instead of silently truncating -- and a truncated membership is an
        inventory that looks complete while omitting rows.
        """
        rows = await connection.fetch(
            "SELECT resource_id, provider, provider_reference, kind, operation_keys "
            "FROM harness_allocation_resource WHERE org_id=$1 AND workspace_id=$2 "
            "AND allocation_id=$3 ORDER BY resource_id LIMIT $4",
            org_id,
            workspace_id,
            allocation_id,
            MAX_INVENTORY_RESOURCES + 1,
        )
        return tuple(
            AllocationResource(
                resource_id=row["resource_id"],
                provider=row["provider"],
                provider_reference=row["provider_reference"],
                kind=row["kind"],
                operation_keys=frozenset(row["operation_keys"] or ()),
            )
            for row in rows
        )

    async def _completeness(
        self,
        connection: Connection,
        *,
        lease: ExecutionLease,
        allocation_id: str,
        resources: tuple[AllocationResource, ...],
        sealed_revision: str | None,
    ) -> bool:
        """Whether creation finished under the fence and every resource is accounted.

        Computed from six independent sources rather than asserted by a caller, because
        this flag is what authorizes returning money. None of them is a stored boolean:
        every one is re-derived on each read, so a flag cannot outlive the state it
        described.

        1. **Creation is fenced and finished.** `confirmed_plan_progress` re-derives the
           approved step plan from the digest-bound request and requires a contiguous
           run of *succeeded* calls covering all of it. A `PREFIX` is an operation still
           creating things, and membership taken mid-creation is a snapshot that will
           grow after it was called complete.

        2. **No call may still have created something unobserved.**
           `ProviderCall.may_have_happened` is the existing predicate for exactly this
           (`execution.py:248`) and the reason it is separate from (1): a plan can read
           as complete while a *later* call sits `intended` or `UNKNOWN`. Any such call
           means a resource may exist that no enumeration could have included.

        3. **Every provider reference the call record knows is enumerated.** A succeeded
           call that produced a reference which membership does not mention is the
           narrowest gap, and the one an empty list hides most convincingly.

        4. **The provider itself was asked what it holds, by THIS authority, and
           agreed.** (3) only compares membership against the call's OWN handle, so it
           cannot see a call that created several billable things and had only its
           headline one recorded -- a cluster enumerated without its disk. That case
           needs evidence from outside the executor's own list, so a current
           `harness_allocation_enumeration` row is required for every provider in
           membership, its digest must still match the handles enumerated now, AND it
           must have been recorded by the attempt, holder and fence now asking. The last
           clause is what makes the proof fresh rather than inherited: a listing carries
           the authority that took it, and after a takeover advances the fence a
           successor could otherwise seal and release on its predecessor's listing
           without ever querying the provider itself. That is precisely the case where
           asking again matters -- a resource whose creation was in flight when the
           predecessor died may have appeared after its listing was taken.

        5. **The allocation is sealed over exactly this membership.** An unsealed
           allocation can still grow between this snapshot and the domain acting on
           it, so it is never complete. The seal names the revision it was taken over
           and that revision is recomputed here, which also catches a row inserted
           behind this package's back: the revision moves and the inventory reads as
           incomplete rather than as a sealed whole.

        6. **No creating provider call against the ALLOCATION is unaccounted for.**
           (2) and (3) are the same two questions asked only of the reading operation's
           own calls, and the expensive case is another operation's: two separately
           approved operations may name one allocation, so a call that is creating
           something right now can belong to neither the seal nor this read. Asked again
           allocation-wide (`allocation.creating_calls_unaccounted_for`) so a read
           cannot report a complete inventory while a call from any operation may still
           be producing a member of it.

           Re-derived here rather than trusted from seal time, on the same reasoning as
           every other clause: `seal_allocation` refuses while such a call is
           outstanding, but the seal is a row that persists, and a stored row cannot
           keep being true. A call recorded after the seal is refused by `execution`,
           and if that refusal were ever bypassed -- a direct INSERT, a restored backup,
           a future writer -- this clause is what makes the inventory read INCOMPLETE
           rather than releasing money over it.

        Over the ceiling is not complete either: `_membership` reads one row past the
        limit precisely so that case is visible here rather than truncated into
        something that looks whole.
        """
        _, quarantined = await self._epoch(connection, lease, allocation_id)
        if quarantined:
            return False
        if len(resources) > MAX_INVENTORY_RESOURCES or not resources:
            return False
        if await confirmed_plan_progress(connection, lease.operation_id) is not (
            PlanProgress.COMPLETE
        ):
            return False
        if sealed_revision is None or sealed_revision != _revision(resources):
            return False
        if await self._providers_without_enumeration(
            connection,
            lease=lease,
            allocation_id=allocation_id,
            resources=resources,
        ):
            return False
        calls = await connection.fetch(
            "SELECT stage, outcome, provider, provider_ref FROM "
            "harness_provider_call_intent WHERE operation_id=$1",
            lease.operation_id,
        )
        known = {(item.provider, item.provider_reference) for item in resources}
        for call in calls:
            stage = str(call["stage"])
            outcome = call["outcome"] or ""
            if stage in ("intended", "unresolved") or outcome == "unknown":
                # The `may_have_happened` rule, applied to the stored row. Spelled
                # against the columns rather than by rebuilding a `ProviderCall` so this
                # check needs no lease-bound reconstruction of rows from earlier
                # attempts -- which is the state recovery leaves behind.
                return False
            reference = call["provider_ref"]
            if (
                outcome == "succeeded"
                and reference
                and (str(call["provider"]), str(reference)) not in known
            ):
                return False
        # Clause (6): the same two questions, asked of every operation's calls against
        # this allocation rather than only of this operation's.
        return not await creating_calls_unaccounted_for(
            connection,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            allocation_id=allocation_id,
            known=known,
        )

    async def _sealed_revision(
        self,
        connection: Connection,
        *,
        org_id: str,
        workspace_id: str,
        allocation_id: str,
    ) -> str | None:
        """The revision this allocation was sealed over, or `None` if it is still open.

        Delegates to `allocation.sealed_revision`, which the provider-dispatch path
        reads too. One query, so "is this allocation closed?" cannot be answered one way
        by the authority deciding completeness and another by the executor deciding
        whether it may still create.
        """
        return await _allocation_sealed_revision(
            connection,
            org_id=org_id,
            workspace_id=workspace_id,
            allocation_id=allocation_id,
        )

    async def _enumeration_binding(
        self,
        connection: Connection,
        *,
        lease: ExecutionLease,
        allocation_id: str,
    ) -> str:
        """Bind all current provider listings for the allocation.

        Each operation must still supply its own listing for completeness. A new
        listing from any operation is new provider truth about the same resources,
        so it invalidates every older report. Including full grant identities
        prevents equal per-grant generation numbers from colliding in the digest.
        The allocation lock excludes concurrent writes during this read.
        """
        rows = await connection.fetch(
            "SELECT provider, generation, operation_id, attempt_id, executor_id, "
            "fence_token FROM harness_allocation_enumeration "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
        )
        generation, quarantined = await self._epoch(connection, lease, allocation_id)
        return hashlib.sha256(
            json.dumps(
                [
                    generation,
                    quarantined,
                    sorted(
                        (
                            str(row["provider"]),
                            int(row["generation"]),
                            str(row["operation_id"]),
                            str(row["attempt_id"]),
                            str(row["executor_id"]),
                            int(row["fence_token"]),
                        )
                        for row in rows
                    ),
                ]
            ).encode()
        ).hexdigest()

    async def _providers_without_enumeration(
        self,
        connection: Connection,
        *,
        lease: ExecutionLease,
        allocation_id: str,
        resources: tuple[AllocationResource, ...],
    ) -> tuple[str, ...]:
        """Providers in membership with no current listing from THIS authority.

        Two independent things have to be true of a listing before it proves anything,
        and a row merely existing establishes neither.

        **It has to be about this membership.** "Current" means the recorded listing
        digest still matches the handles enumerated for that provider *now*. Comparing
        rather than merely requiring a row is what stops a listing taken when membership
        was smaller from vouching for membership after it grew -- the row would still be
        there, and would still say the provider agreed, about a different set of
        resources.

        **It has to belong to the authority relying on it.** The listing records the
        attempt, holder and fence that took it, and all three must equal the lease now
        asking. Without that clause the check looked only at the provider name and the
        handles, so after recovery advanced the fence a SUCCESSOR could seal and release
        on a listing its PREDECESSOR recorded -- never having queried the provider
        itself. That inverts the guarantee: a takeover happens precisely because the
        previous holder stopped responding, which is also the situation where a resource
        whose creation was in flight may have appeared after the predecessor's listing
        was taken. The fresh-proof-under-the-recovery-fence requirement means the
        successor asks again; comparing the binding is what enforces it, and it is
        compared against values from the resolved lease rather than from a request.

        Per provider because one allocation can hold resources from several, and a
        listing from one says nothing about another's: a single allocation-wide row
        would let one provider's answer vouch for all of them.
        """
        by_provider: dict[str, set[str]] = {}
        for item in resources:
            by_provider.setdefault(item.provider, set()).add(item.provider_reference)
        # A creating call may return no handle and therefore have no member yet.
        # Its provider must still supply a fresh listing, including an empty one.
        providers = await connection.fetch(
            "SELECT DISTINCT provider FROM harness_provider_call_intent "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
        )
        for row in providers:
            by_provider.setdefault(str(row["provider"]), set())
        listings = await connection.fetch(
            "SELECT provider,state FROM harness_provider_listing "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
        )
        unresolved = {
            str(row["provider"]) for row in listings if row["state"] != "completed"
        }
        for row in listings:
            by_provider.setdefault(str(row["provider"]), set())
        rows = await connection.fetch(
            "SELECT provider, enumerated_digest, allocation_generation "
            "FROM harness_allocation_enumeration "
            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 "
            "AND operation_id=$4 AND attempt_id=$5 AND executor_id=$6 "
            "AND fence_token=$7",
            lease.org_id,
            lease.workspace_id,
            allocation_id,
            lease.operation_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
        )
        generation, _ = await self._epoch(connection, lease, allocation_id)
        recorded = {
            str(row["provider"]): str(row["enumerated_digest"])
            for row in rows
            if row["allocation_generation"] == generation
        }
        return tuple(
            sorted(
                provider
                for provider, handles in by_provider.items()
                if provider in unresolved
                or recorded.get(provider) != _handles_digest(handles)
            )
        )

    def _unresolved(
        self,
        allocation_id: str,
        reason: str,
        *,
        resources: tuple[str, ...] = (),
        inventory: VerifiedInventory | None = None,
    ) -> CleanupAssessment:
        """The conservative answer: cost accrued as unknown, explicitly not zero.

        `unresolved_resources` falls back to naming the allocation itself if membership
        could not be established at all. An UNRESOLVED assessment that names nothing is
        unactionable, and the consumer's own type refuses to be constructed that way
        (`accounting.py:163`).
        """
        return CleanupAssessment(
            allocation_id=allocation_id,
            state=ReleaseState.UNRESOLVED,
            exposure=CostExposure.UNRESOLVED,
            dispositions=tuple(
                (name, BudgetDisposition.RETAIN) for name in (resources or ())
            ),
            unresolved_resources=resources or (allocation_id,),
            reason=reason,
            inventory=inventory,
        )

    async def _audit(
        self,
        connection: Connection,
        lease: ExecutionLease,
        event: str,
        allowed: bool,
        detail: str | None = None,
    ) -> None:
        await audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            event=event,
            actor=lease.holder,
            allowed=allowed,
            attempt_id=lease.attempt_id,
            fence_token=lease.fence_token,
            detail=detail,
        )


def _reconcile(
    inventory: VerifiedInventory, observations: Mapping[str, ResourceObservation]
) -> CleanupAssessment:
    """Compare provider evidence against enumerated membership.

    The expected set comes from the inventory, never from the observed keys. Deriving it
    from what was observed makes the report self-certifying: an executor that reported
    nothing would establish that nothing exists, which is the "empty provider response
    clears a non-empty allocation" case the consumer's contract closes
    (`accounting.py:199`).

    Four things retain exposure, and they are collapsed into one unresolved answer
    because they have the same remedy -- go and look:

    * a member with no observation at all (the disappearing-resource case);
    * an observation for a resource that is not a member (a foreign or stale report);
    * an `UNKNOWN` observation (the provider could not be consulted);
    * an observation whose query did not use the member's recorded PROVIDER handle,
      which is a confident answer about the wrong resource.

    ## Why the identity check is against the provider handle and not the key

    `AllocationResource.provider_reference` is documented as the durable identifier a
    teardown must present, and `ResourceObservation.queried_by` as the handle the query
    actually used. The check here compared `queried_by` against the mapping KEY -- the
    local `resource_id` -- so the two documented meanings were never connected. With
    `resource_id='cluster-1'` and `provider_reference='i-123'`, an ABSENT answer the
    provider gave about a name it has never heard of (`cluster-1`) satisfied the check
    and released the budget while `i-123` kept running. The provider was not wrong and
    the record was not wrong; the question was asked about the wrong thing, and nothing
    compared the question to the record.

    So each observation is resolved to the member it claims to be about, and is evidence
    only when the query used THAT member's handle. Anything else is retained as
    unestablished rather than believed. The consumer already applies exactly this rule
    before normalizing an observation (`provider_handles.py:993`); an authority that is
    supposed to be the stronger of the two cannot be the one that skips it.

    The observations stay keyed by `resource_id` -- that is the vocabulary the domain's
    ledger and the `dispositions` it consumes are written in -- so the handle is looked
    up rather than swapped in. Translating the keys here would make the returned
    dispositions unmatchable against the resources the domain is accounting for.
    """
    by_id = {item.resource_id: item for item in inventory.resources}
    expected = set(by_id)
    observed = set(observations)
    unknown = sorted(
        (expected - observed)
        | (observed - expected)
        | {
            name
            for name, item in observations.items()
            if item.presence is ResourcePresence.UNKNOWN
            or name not in by_id
            or item.queried_by != by_id[name].provider_reference
        }
    )
    present = sorted(
        name
        for name, item in observations.items()
        if item.presence is ResourcePresence.PRESENT and name not in unknown
    )
    absent = sorted(
        name
        for name, item in observations.items()
        if item.presence is ResourcePresence.ABSENT and name not in unknown
    )
    # Per-resource decisions, in the vocabulary the owning domain already consumes
    # (`execution.disposition_for`). A resource the provider still holds has really been
    # created, so its spend is settled rather than released; one confirmed absent
    # releases; anything unresolved retains. Mixed sets are reported as the aggregate
    # below *and* kept per resource, because releasing the absent half of a
    # half-destroyed allocation is a decision the ledger owner makes, not this module.
    dispositions = tuple(
        sorted(
            [(name, BudgetDisposition.RETAIN) for name in unknown]
            + [(name, BudgetDisposition.SETTLE) for name in present]
            + [(name, BudgetDisposition.RELEASE) for name in absent]
        )
    )
    if unknown:
        return CleanupAssessment(
            allocation_id=inventory.allocation_id,
            state=ReleaseState.UNRESOLVED,
            exposure=CostExposure.UNRESOLVED,
            dispositions=dispositions,
            unresolved_resources=tuple(unknown + present),
            reason=(
                f"provider evidence was unavailable or mismatched for {len(unknown)} "
                "resource(s); the allocation is retained and reported, and incurred "
                "cost is accrued as unresolved rather than zero"
            ),
            inventory=inventory,
        )
    if present:
        return CleanupAssessment(
            allocation_id=inventory.allocation_id,
            state=ReleaseState.RETAINED,
            exposure=CostExposure.ACTIVE,
            dispositions=dispositions,
            unresolved_resources=tuple(present),
            reason=(
                f"the provider still holds {len(present)} resource(s) for this "
                "allocation; it is not released and cost continues to accrue"
            ),
            inventory=inventory,
        )
    return CleanupAssessment(
        allocation_id=inventory.allocation_id,
        state=ReleaseState.RELEASED,
        exposure=CostExposure.NONE,
        dispositions=dispositions,
        reason=(
            f"a provider re-check of {len(observations)} enumerated resource(s) found "
            "none remaining"
        ),
        inventory=inventory,
    )


def _principal(lease: ExecutionLease) -> ResolvedPrincipal:
    """The lease holder as a principal, for tenant-scoped store reads.

    Built from the lease rather than accepted as an argument: the lease was granted to
    this holder under this tenant, so it *is* the resolved authority here, and an
    argument would be a second place a tenant could be supplied.
    """
    return ResolvedPrincipal(
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        subject=lease.holder,
        permissions=frozenset({"workspace:provision"}),
    )


def _require_lease(lease: object) -> None:
    if not isinstance(lease, ExecutionLease):
        raise ContractViolation("an ExecutionLease is required")

"""Allocation inventory, report attestation and cleanup authority.

Issue #5529 (w6-06), EPIC #4910, Wave 6.

A real database, for the reasons `conftest.py` states and three specific to this module:
the stale-fence refusal is `lock_lease`'s `SELECT ... FOR UPDATE` comparing a presented
token against the live row; the concurrent creation/cleanup case is two connections
genuinely contending; and the per-allocation serialization is `pg_advisory_xact_lock`
taken by two separately admitted operations at once. None of the three has any meaning
against a fake, so a green suite on one would be evidence about the fake.

The tests carrying the acceptance criteria:

* `test_locally_submitted_observations_cannot_manufacture_cleanup_authority`,
  `test_a_forged_digest_is_never_attested` and
  `test_a_tampered_stored_payload_fails_the_recomputed_digest` -- **AC-02**, and the
  expensive half of this story: the domain must consume a real authenticated result, not
  an echo of what it sent.
* `test_a_stale_fence_cannot_{publish_a_report,enumerate_membership,seal_an_allocation,
  read_a_verified_inventory}` and `test_an_expired_lease_is_not_current_authority` --
  AC-01's stale fence, on every write and on the read.
* `test_a_disappearing_resource_is_unresolved_not_released`,
  `test_a_missing_resource_is_refused_a_seal_and_retains_exposure`,
  `test_a_seal_inserted_behind_the_authority_still_reads_as_incomplete`,
  `test_an_omitted_child_resource_is_never_released` -- AC-01's missing/disappearing
  resources. The second and third are one contract from both sides: the authority
  refuses the seal, and a seal row inserted behind it still reads as incomplete,
  because a check that only runs on the write path is not a check on what is read.
* `test_absence_established_by_querying_the_local_name_never_releases`,
  `test_the_same_absence_queried_by_the_provider_handle_does_release` and
  `test_presence_queried_by_the_local_name_is_not_authoritative_either` -- AC-01/AC-02's
  identity rule: an observation is evidence about the machine only if it was obtained by
  the **provider handle** the record holds. An answer about a local name is a truthful
  answer to a question no provider was asked, and it authorized release.
* `test_an_unfinished_plan_cannot_be_sealed_or_attested`,
  `test_an_unresolved_call_blocks_completeness`,
  `test_an_incomplete_inventory_never_releases_budget` -- AC-01's incomplete inventory,
  and that incomplete is not empty.
* `test_forged_membership_cannot_overwrite_a_persisted_identity` -- AC-01's forged
  membership.
* `test_concurrent_creation_and_cleanup_do_not_release_a_growing_allocation` (one
  allocation, one lease) and
  `test_one_operations_seal_refuses_another_operations_growth` (two separately approved
  operations naming one allocation) -- AC-01's concurrency. The second is the case the
  first cannot reach: two lease locks are two different locks, so only an
  allocation-scoped lock serializes them.
* `test_retained_storage_cost_keeps_the_allocation_retained` and
  `test_retained_network_cost_keeps_the_allocation_retained` -- AC-01's retained
  storage/network cost, which is the case a per-call `provider_ref` inventory misses.
* `test_a_report_taken_before_creation_can_never_authorize_a_release`,
  `test_a_report_cannot_be_published_into_an_unsealed_allocation`,
  `test_a_published_report_records_the_revision_it_was_taken_against` and
  `test_an_attestation_stops_verifying_once_membership_is_resealed` -- AC-01's stale
  evidence in its most expensive form: a truthful observation taken before the resource
  existed, replayed after it was created. The first replays the exact reviewed sequence.
* `test_a_successor_cannot_seal_or_release_on_a_predecessors_listing` and
  `test_a_listing_recorded_by_another_attempt_of_the_same_holder_is_not_current` --
  AC-01's stale evidence across a recovery boundary: a completeness proof is current
  only for the authority that took it.
* `test_a_report_cannot_be_published_before_its_provider_listing`,
  `test_a_later_listing_cannot_validate_an_earlier_absent_report` and
  `test_a_stale_receipt_cannot_be_republished_after_a_new_listing` -- stale evidence
  in the remaining direction, which the ordering rules above do not cover: the report is
  bound to the listings that existed **when it was taken**, so a listing recorded
  afterwards cannot reach back and attest it. Without that binding, an ABSENT report
  taken while a resource was invisible became releasable the moment a later, unrelated
  enumeration ran.
* `test_no_interleaving_permits_both_a_seal_and_a_later_provider_creation`,
  `test_a_seal_taken_during_dispatch_stops_the_provider_call`,
  `test_a_seal_appearing_inside_the_dispatch_window_stops_the_provider`,
  `test_a_sealed_allocation_can_still_be_torn_down_and_inspected`,
  `test_the_recorded_call_carries_the_allocation_it_was_approved_for` and
  `test_an_operation_naming_no_allocation_is_not_fenced` -- AC-01's concurrency where it
  costs money rather than consistency. Sealing used to withdraw the ability to *record*
  membership and never the authority to *create*, so a second approved operation could
  invoke the provider after the seal and have only its bookkeeping refused, leaving a
  billing resource outside an inventory that read ABSENT and authorized release. The
  first drives the real two-operation race through `OperationExecutor` and asserts on
  the provider hook's invocations, because money follows the invocation, not the row.
  The fourth is the fence's other edge: a sealed allocation that can no longer be torn
  down bills forever, which is the same loss from the other direction.
* `test_the_effect_of_a_call_is_read_from_its_approved_verb`,
  `test_anything_that_might_create_is_fenced` and
  `test_teardown_and_query_calls_are_never_fenced` -- that the classification driving
  that fence comes from the digest-bound approved plan and treats an **unrecognized**
  verb as creating. These found a real defect: a leading-token rule left
  `ec2:TerminateInstances` unrecognized, hence fenced, hence a sealed allocation with no
  legal path to teardown.

AC-03 is the required lane on the final head, not a test here. AC-04 is an independent
functional review plus a normal verified merge; offline evidence never closes it.

The safe order is the contract, so the helpers enforce it: `sealed()` enumerates,
finishes the plan, records the provider listing and seals; `published()` delegates to it
and only then publishes. An earlier `published()` published first, and that order is the
defect `test_a_report_taken_before_creation_can_never_authorize_a_release` exists to
prevent -- the helper can no longer express it, because the authority refuses it.

No provider, cloud or AWS service is contacted by any test here.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs import OperationStore
from harness_jobs.allocation import CallEffect, call_effect, may_create
from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    OperationExecutor,
    ProviderCallRefused,
    observe,
    record_intent,
)
from harness_jobs.execution_plan import step_key
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import ContractViolation, OperationRefused, ResolvedPrincipal
from harness_jobs.inventory import (
    MAX_INVENTORY_RESOURCES,
    AllocationResource,
    CostExposure,
    InventoryAuthority,
    ReleaseState,
    ResourceObservation,
    ResourcePresence,
    VerifiedInventory,
    report_digest,
)
from harness_jobs.leases import acquire, close, fence_expired_lease

from .conftest import admit_paid, requires_postgres
from .test_admission_postgres import principal

pytestmark = requires_postgres


async def _publish(service, connection, lease, *, observations):
    """A trusted provider-query double; preserve receipts on transport retries.

    Existing tests describe the provider response with a dictionary. This helper
    now obtains an actual query receipt before publishing it. The dictionary is
    updated with the returned nonce, just as the executor submits the resulting
    report to the domain cleanup API. New freshness regressions call the public
    APIs directly, including the refusal of raw/cached payloads.
    """
    receipts = getattr(service, "_test_receipts", {})
    service._test_receipts = receipts
    cached = receipts.get(id(observations))
    if (
        cached is not None
        and cached[0] is observations
        and dict(cached[1]) == observations
    ):
        receipt = cached[1]
    else:
        if not observations:
            return await service.publish_report(
                connection, lease, observations=observations
            )
        from dataclasses import replace

        async def query_provider(_lease, _resources, _query_id):
            # Fixtures in separate grants represent separate actual provider reads.
            return {
                key: replace(item, observation_id="")
                for key, item in observations.items()
            }

        service.query_provider = query_provider
        receipt = await service.observe_report(connection, lease)
        observations.update(receipt)
        receipts[id(observations)] = (observations, receipt)
    return await service.publish_report(connection, lease, observations=receipt)


ALLOCATION = "alloc-1"

# The plan is a single step, so `confirmed_plan_progress` reaches COMPLETE after one
# succeeded call. The allocation id travels in the same approval-bound parameters, which
# is the property `allocation_id_for` depends on.
STEP = {
    "step_id": "step-1",
    "provider": "aws",
    "operation_kind": "create_cluster",
    "target": "account/111122223333",
}


def plan_request(key="key-1", allocation=ALLOCATION, steps=(STEP,)):
    """An admitted request carrying an approved step plan and an allocation id."""
    from harness_jobs.identity import OperationRequest

    return OperationRequest(
        action="provision",
        idempotency_key=key,
        parameters={
            "allocation_id": allocation,
            "execution_steps": json.dumps(list(steps)),
        },
    )


async def leased(
    pool,
    key="key-1",
    holder="worker-1",
    attempt="attempt-1",
    allocation=ALLOCATION,
    steps=(STEP,),
    **kwargs,
):
    """An admitted, paid-for operation with a live lease held by `holder`.

    `allocation` is a parameter because the two-operation race test needs several
    independent allocations within one test, and because two operations naming the SAME
    allocation is the situation F2 is about -- that has to be expressible here rather
    than only as the default.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(
            store,
            connection,
            principal(),
            plan_request(key, allocation=allocation, steps=steps),
        )
        lease = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder=holder,
            attempt_id=attempt,
            **kwargs,
        )
    return admitted.record, lease


async def complete_the_plan(pool, record, lease, provider_ref="cluster-abc"):
    """Drive the single planned step to a succeeded call, as a worker would.

    Through the real `record_intent`/`observe` rather than by INSERTing a row: the
    completeness rule reads what those functions write, and a hand-written row would let
    this suite agree with itself about a shape the production path never produces.
    """
    steps = json.loads(record.admitted_request().parameters["execution_steps"])
    from harness_jobs.execution_plan import ExecutionStep

    key = step_key(record, ExecutionStep(**steps[0]))
    async with pool.acquire() as connection:
        await record_intent(
            connection,
            lease,
            idempotency_key=key,
            provider=STEP["provider"],
            operation_kind=STEP["operation_kind"],
            target=STEP["target"],
        )
        await observe(
            connection,
            lease,
            idempotency_key=key,
            outcome=CallOutcome.SUCCEEDED,
            provider_ref=provider_ref,
        )
    return key


async def fence_out(pool, lease):
    """Make `lease` stale the way production does: it lapses, recovery takes over.

    The expiry is forced first because `fence_expired_lease` only claims a lease that
    actually lapsed -- it returns `None` otherwise, which would leave the fence unmoved
    and quietly turn every stale-fence test into a test of the happy path. Same two-step
    as `test_execution_postgres.py:447`. Returns the successor's own `ExecutionLease`,
    so callers can drive real writes under the advanced fence.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at = now() - interval '1s' "
            "WHERE operation_id = $1",
            lease.operation_id,
        )
        takeover = await fence_expired_lease(
            connection, operation_id=lease.operation_id
        )
    assert takeover is not None, (
        "the fence did not advance; the test would prove nothing"
    )
    return takeover.lease


def authority_for(lease, *, holder=None, workspace=None):
    """A verifier standing in for #5528's credential service.

    The authority string is opaque to this module by design, so the test's verifier is
    the whole of what "resolve it" means here. It is deliberately a closure over a lease
    the *database* granted rather than a constructed one: a test that minted its own
    lease would be asserting that this module trusts a fabricated fence, which is the
    opposite of the property.

    `holder`/`workspace` overrides exist for the tests that need a grant which resolves
    successfully but to somebody else.
    """
    import dataclasses

    granted = lease
    if holder is not None or workspace is not None:
        granted = dataclasses.replace(
            lease,
            holder=holder or lease.holder,
            workspace_id=workspace or lease.workspace_id,
        )

    async def verify(token):
        if token != "authority-token":
            raise OperationRefused("unknown authority")
        return ExecutionGrant(
            principal=ResolvedPrincipal(
                org_id=granted.org_id,
                workspace_id=granted.workspace_id,
                subject=granted.holder,
                permissions=frozenset({"workspace:provision"}),
            ),
            lease=granted,
        )

    return verify


def authority(pool, lease, **kwargs):
    """An `InventoryAuthority` composed against the test pool and one lease."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connect():
        async with pool.acquire() as held:
            yield held

    return InventoryAuthority(
        connect=connect, authenticate=authority_for(lease, **kwargs)
    )


# A member's local id and its provider handle are DELIBERATELY different everywhere in
# this suite (`resource("cluster-1")` records the handle `cluster-1-handle`), because a
# suite where they coincide cannot tell the two apart -- and telling them apart is the
# whole of the identity rule `_reconcile` enforces.
#
# These three helpers therefore take the name of the member and query by that member's
# HANDLE, which is what a real teardown presents. They used to query by the local name,
# which is why every positive release test passed while the authority was accepting
# answers to the wrong question: the fixtures asked the provider about `cluster-1` and
# the record said the machine was `cluster-1-handle`, and nothing compared them.
#
# `queried_by` is overridable so the negative cases can pass a deliberately wrong
# identity; `handle_for` keeps the convention in one place so it cannot drift.


def handle_for(name):
    """The provider handle `resource(name)` records. One definition, used by both sides.

    A test that spelled the handle inline would still pass if the convention changed on
    only one side of the comparison, which is exactly the drift the identity rule is
    supposed to catch.
    """
    return f"{name}-handle"


def present(name, state="RUNNING", queried_by=None):
    return ResourceObservation(
        presence=ResourcePresence.PRESENT,
        queried_by=queried_by or handle_for(name),
        provider_state=state,
    )


def absent(name, queried_by=None):
    return ResourceObservation(
        presence=ResourcePresence.ABSENT, queried_by=queried_by or handle_for(name)
    )


def unknown(name, detail="the provider API timed out", queried_by=None):
    return ResourceObservation(
        presence=ResourcePresence.UNKNOWN,
        queried_by=queried_by or handle_for(name),
        detail=detail,
    )


def resource(name, kind="compute", provider="aws", reference=None):
    return AllocationResource(
        resource_id=name,
        provider=provider,
        provider_reference=reference or handle_for(name),
        kind=kind,
    )


def _revision_of(resources):
    """The revision a given membership derives to.

    The production function, not a restatement of it: a test that wrote a seal row with
    a revision it computed its own way could agree with itself while disagreeing with
    what `_completeness` recomputes, and would then prove nothing.
    """
    from harness_jobs.inventory import _revision

    return _revision(tuple(resources))


# ---------------------------------------------------------------------------
# Publication and membership under a fence
# ---------------------------------------------------------------------------


async def test_a_published_report_returns_a_digest_the_authority_computed(pool):
    """The digest is the authority's, and matches the canonical form exactly."""
    _record, service, lease, _revision = await sealed(
        pool, (resource("disk-1", kind="storage"),), provider_ref="disk-1-handle"
    )
    observations = {"disk-1": absent("disk-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
    assert digest == report_digest(observations)
    assert len(digest) == 64


async def test_publishing_stores_the_payload_not_only_the_digest(pool):
    """A dispute needs "what was attested?", which a digest alone cannot answer."""
    _record, service, lease, _revision = await sealed(
        pool, (resource("disk-1", kind="storage"),), provider_ref="disk-1-handle"
    )
    async with pool.acquire() as connection:
        digest = await _publish(
            service,
            connection,
            lease,
            observations={"disk-1": present("disk-1", "attached")},
        )
        stored = await connection.fetchval(
            "SELECT observations FROM harness_provider_report WHERE report_digest=$1",
            digest,
        )
    assert json.loads(stored)["disk-1"]["provider_state"] == "attached"


async def test_a_published_report_records_the_revision_it_was_taken_against(pool):
    """F1. The stored row says WHEN the report was taken, as a membership revision.

    The column is the whole of the ordering guarantee: without it a report proved only
    "an authorized executor observed this", never "it observed this after the
    membership being cleared was final". Asserted on the stored row rather than only
    through `read_inventory`, so a future change that stopped persisting the revision
    -- and therefore silently stopped constraining the order -- fails here loudly
    rather than passing everywhere the revision happens to be the current one.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    async with pool.acquire() as connection:
        digest = await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )
        stored = await connection.fetchval(
            "SELECT sealed_revision FROM harness_provider_report "
            "WHERE report_digest=$1 AND operation_id=$2",
            digest,
            lease.operation_id,
        )
    assert stored == revision


async def test_republishing_identical_observations_is_idempotent(pool):
    """A retried publication after a transport failure converges on one row."""
    _record, service, lease, _revision = await sealed(
        pool, (resource("disk-1", kind="storage"),), provider_ref="disk-1-handle"
    )
    observations = {"disk-1": absent("disk-1")}
    async with pool.acquire() as connection:
        first = await _publish(service, connection, lease, observations=observations)
        second = await _publish(service, connection, lease, observations=observations)
        rows = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE report_digest=$1", first
        )
    assert first == second
    assert rows == 1


async def test_an_empty_report_is_refused_at_publication(pool):
    """Refused where the executor can still act, not as a silent retained exposure."""
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(ContractViolation):
            await _publish(service, connection, lease, observations={})


async def test_a_report_cannot_be_published_into_an_unsealed_allocation(pool):
    """F1. Publication is refused while the allocation can still grow.

    The direct statement of the repair. A list still open to additions has no final
    revision for a report to vouch for, so there is no honest answer to "which
    membership was this observed against?" -- and the executor learns that here, at
    the moment it could still seal first, rather than by discovering later that its
    attestation never verifies.

    Nothing is stored: a refused publication must not leave a row that a subsequent
    seal could make retroactively valid.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await _publish(
                service,
                connection,
                lease,
                observations={"cluster-1": absent("cluster-1")},
            )
        assert "not sealed" in str(refusal.value)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
                lease.operation_id,
            )
            == 0
        )
        # And the refusal is on the record with its own reason, so an operator can
        # tell this apart from a stale fence.
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit WHERE operation_id=$1 "
                "AND event='report.refused' AND allowed = false AND detail='unsealed'",
                lease.operation_id,
            )
            == 1
        )


async def test_a_report_taken_before_creation_can_never_authorize_a_release(pool):
    """**F1, the negative case.** The reviewer's exact sequence, and its new outcome.

    Step for step what the old test helper did and accepted: publish an ABSENT
    observation, THEN create the resource, THEN seal, THEN expect a release. Every
    individual check that sequence faced used to pass -- the observations were
    genuine provider answers, the digest was the authority's own, the inventory was
    complete because membership and the provider listing agreed, and the presence said
    ABSENT -- so the result was RELEASED with zero reported exposure over a cluster
    that existed. Nothing in the record could see that the observation predated the
    membership it was clearing.

    The publication is now refused where it happens, so the evidence never exists, and
    the release the old rule granted is UNRESOLVED with exposure retained. The
    observations are kept and re-presented at the end deliberately: the executor still
    has them, they still hash to the same digest, and `assess_cleanup` computes that
    digest itself -- so this asserts that holding truthful pre-creation observations
    buys nothing at all.

    The allocation cannot instead be made to grow past its seal -- `enumerate_resources`
    refuses that, which is F2 -- so the remaining read-side half of the binding, "a
    report bound to a revision that is no longer final stops verifying", is asserted
    directly in the test below.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)

    # The provider is asked before anything is provisioned. The answer is TRUE: the
    # cluster is absent, because it does not exist yet.
    early = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await _publish(service, connection, lease, observations=early)
    assert "not sealed" in str(refusal.value)

    # Then the cluster is created, enumerated, listed by the provider, and sealed --
    # the rest of the old helper's order, unchanged.
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    await finish_allocation(pool, service, lease, (resource("cluster-1"),))

    # The old helper expected RELEASED here.
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=early,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.may_mark_released is False
    assert assessment.may_return_reservation_unused is False
    # No attestation was ever stored for those bytes, under any binding.
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_report WHERE report_digest=$1",
                report_digest(early),
            )
            == 0
        )
        # And the cluster is still a member -- the cost the old rule reported as zero.
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource WHERE "
                "allocation_id=$1 AND resource_id='cluster-1'",
                ALLOCATION,
            )
            == 1
        )


async def test_an_attestation_stops_verifying_once_membership_is_resealed(pool):
    """F1. The revision comparison is against the seal in force NOW, not any seal.

    Narrowest form of the same property, with no second operation and no refusal in
    the way: one grant, one allocation, a legitimately published report, and then the
    seal moved. The report is untouched and its grant is still live -- what changed is
    which membership is final -- and that alone must make it unusable.

    The seal is moved directly here, which is deliberate. `seal_allocation` refuses to
    re-seal moved membership, so through the public methods this state is unreachable;
    writing it makes the read-side check a genuine assertion rather than one that
    can never fire. If a repair ever relaxes sealing, the read side must still hold.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
        # It verifies now, so the assertion below is about the reseal and nothing else.
        assert await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        await connection.execute(
            "UPDATE harness_allocation_seal SET sealed_revision=$4 WHERE org_id=$1 "
            "AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            revision + "-moved",
        )

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_a_stale_fence_cannot_publish_a_report(pool):
    """AC-01's stale fence, on the publication path."""
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    # Recovery takes the operation over, which advances the fence past `lease`.
    await fence_out(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused):
            await _publish(
                service,
                connection,
                lease,
                observations={"cluster-1": absent("cluster-1")},
            )
        remaining = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
            lease.operation_id,
        )
    assert remaining == 0


async def test_a_refused_publication_is_still_audited(pool):
    """The refusal is the valuable half: it changes nothing else anywhere.

    Specifically a regression guard on transaction placement -- an audit written inside
    the aborted transaction rolls back with it, and the fence doing its job leaves no
    trace at all.
    """
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    await fence_out(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused):
            await _publish(
                service, connection, lease, observations={"disk-1": absent("disk-1")}
            )
        events = await connection.fetch(
            "SELECT event, allowed FROM harness_execution_audit WHERE operation_id=$1",
            lease.operation_id,
        )
    refusals = [row for row in events if row["event"] == "report.refused"]
    assert refusals and refusals[0]["allowed"] is False


async def test_membership_is_recorded_under_the_holders_fence(pool):
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        added = await service.enumerate_resources(
            connection,
            lease,
            resources=(resource("cluster-1"), resource("disk-1", kind="storage")),
        )
    assert added == 2


async def test_re_enumerating_merges_operation_keys_without_duplicating(pool):
    """Additive: a second enumeration of the same resource adds no row."""
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    first = AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="cluster-1-handle",
        kind="compute",
        operation_keys=frozenset({"key-a"}),
    )
    second = AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="cluster-1-handle",
        kind="compute",
        operation_keys=frozenset({"key-b"}),
    )
    async with pool.acquire() as connection:
        assert await service.enumerate_resources(connection, lease, resources=(first,))
        assert (
            await service.enumerate_resources(connection, lease, resources=(second,))
            == 0
        )
        keys = await connection.fetchval(
            "SELECT operation_keys FROM harness_allocation_resource "
            "WHERE resource_id=$1",
            "cluster-1",
        )
    assert sorted(keys) == ["key-a", "key-b"]


async def test_forged_membership_cannot_overwrite_a_persisted_identity(pool):
    """AC-01's forged membership.

    Overwriting the stored handle would leave the earlier resource reachable by no
    record here: it keeps running, and nothing in the system knows to ask about it.
    """
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
        with pytest.raises(OperationRefused):
            await service.enumerate_resources(
                connection,
                lease,
                resources=(resource("cluster-1", reference="attacker-handle"),),
            )
        stored = await connection.fetchval(
            "SELECT provider_reference FROM harness_allocation_resource "
            "WHERE resource_id=$1",
            "cluster-1",
        )
    assert stored == "cluster-1-handle"


async def test_a_stale_fence_cannot_enumerate_membership(pool):
    """AC-01's stale fence, on the membership path."""
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    await fence_out(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused):
            await service.enumerate_resources(
                connection, lease, resources=(resource("cluster-1"),)
            )
        rows = await connection.fetchval(
            "SELECT count(*) FROM harness_allocation_resource WHERE allocation_id=$1",
            ALLOCATION,
        )
    assert rows == 0


async def test_membership_beyond_the_ceiling_is_refused(pool):
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    many = tuple(resource(f"r-{index}") for index in range(MAX_INVENTORY_RESOURCES + 1))
    async with pool.acquire() as connection:
        with pytest.raises(ContractViolation):
            await service.enumerate_resources(connection, lease, resources=many)


# ---------------------------------------------------------------------------
# The verified read: AC-02
# ---------------------------------------------------------------------------


async def test_a_verified_read_returns_the_enumerated_membership(pool):
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)

    inventory = await service.read_inventory(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    assert isinstance(inventory, VerifiedInventory)
    assert inventory.complete is True
    assert inventory.active is True
    assert inventory.attested_report_digest == digest
    assert [item.resource_id for item in inventory.resources] == ["cluster-1"]
    assert inventory.expires_at > datetime.now(UTC)
    assert inventory.revision.startswith("inventory-")
    # The revision the read reports is the one the allocation was sealed over, which is
    # also the one the attestation was bound to. Three names for one value is the
    # property that makes the ordering check meaningful.
    assert inventory.revision == revision


async def test_a_forged_digest_is_never_attested(pool):
    """AC-02. The caller's digest is looked up, never hashed and echoed back."""
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    forged = report_digest({"cluster-1": absent("cluster-1")})

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=forged,
        )
        is None
    )


async def test_locally_submitted_observations_cannot_manufacture_cleanup_authority(
    pool,
):
    """**AC-02.** The domain's own observations, never published, authorize nothing.

    This is the shape of the attack the port's docstring names: a caller that
    computes a digest over observations it made up and presents it as an attestation.
    It hashes to a digest no report row carries, so verification misses and the answer
    is unresolved with exposure retained.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")

    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations={"cluster-1": absent("cluster-1")},
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.may_mark_released is False
    assert assessment.may_return_reservation_unused is False


async def test_a_tampered_stored_payload_fails_the_recomputed_digest(pool):
    """The digest is recomputed from the stored bytes, not trusted as a key.

    A row whose payload was edited after the fact no longer hashes to its own primary
    key, and the attestation stops verifying -- which is the difference between storing
    a digest and checking one.
    """
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
        await connection.execute(
            "UPDATE harness_provider_report SET observations=$2 WHERE report_digest=$1",
            digest,
            json.dumps({"cluster-1": {"presence": "absent"}}),
        )

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_a_report_from_another_operation_does_not_attest_this_one(pool):
    """One operation's evidence must not authorize another's cleanup.

    Both operations name the same allocation, so the seal one of them takes is the
    seal the other publishes against -- the reports are bound to the same revision and
    differ only in whose grant made them. That is the sharpest form of the property:
    the binding is doing the work, not an incidental revision mismatch.

    The publishing operation asks the provider itself first. It has to: a report is
    only publishable against a listing bound to the grant publishing it (F5), so
    borrowing the other operation's listing is refused before the read-side binding is
    ever reached, and a test that stopped there would no longer be testing the read.

    **Both plans finish before either allocation is sealed**, which is the only order
    the creation fence permits: a creating call may not be recorded into an allocation
    already declared final (F1). An earlier version of this setup sealed on operation
    two and only then drove operation one's `create_cluster` step, and that sequence is
    now refused at `record_intent` -- correctly, because it is the sequence that used to
    leave a created, billing resource outside every sealed inventory. The property under
    test is unaffected: what makes the read refuse is whose grant the report is bound
    to, and that is identical either way.
    """
    (
        (first_service, first),
        (second_service, second),
    ) = await _two_operations_on_one_allocation(pool, ALLOCATION)
    # The sealer's listing is already recorded, so it can seal; both operations converge
    # on one revision because membership is the same.
    async with pool.acquire() as connection:
        await second_service.seal_allocation(connection, second)
    # Operation one then takes a listing of its own -- required, since the report it is
    # about to publish must be bound to its own grant -- and seals convergently.
    await finish_allocation(pool, first_service, first, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(
            first_service, connection, first, observations=observations
        )

    assert (
        await second_service.read_inventory(
            executor_id=second.holder,
            workspace_id=second.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_identical_observations_from_another_operation_are_their_own_attestation(
    pool,
):
    """F4. Identical canonical bytes across operations must not be refused.

    Two unrelated operations observing unrelated resources can produce byte-identical
    canonical observations -- and a retry against unchanged provider state does so
    routinely. When the digest alone was the primary key, the first publisher owned it
    globally and every later publication was refused; a refused publication means no
    attestation, which means cleanup could never be authorized for that operation. Safe,
    and permanently stuck.

    Both publications must therefore succeed and be distinct rows, while each remains
    verifiable only under its own binding -- which the read-side test below asserts.

    Both operations seal the shared allocation -- convergently, since membership is the
    same -- and both publish against that one revision. Only the grant differs, so a key
    made of digest plus revision plus listing binding would still collide, and this test
    would still catch it.

    Each operation asks the provider for a listing of its own, because F5 requires the
    listing backing a report to be bound to the grant publishing it. That fixes the
    order: the first must publish BEFORE the second re-lists, since the second's listing
    replaces the row and takes the binding with it. The first report becomes unusable at
    that point, which is the supersede-rather-than-rescue rule doing its job; what this
    test is about is that the attestation still EXISTS as its own row rather than having
    been silently discarded in favour of somebody else's.

    **Both plans finish before either seal**, which the creation fence now requires: the
    second operation's step is a `create_cluster`, and recording one into an allocation
    the first has already sealed is refused (F1). Only the setup order changes -- the
    two publications, their digests and their bindings are exactly as before.
    """
    members = (resource("cluster-1"),)
    (
        (first_service, first),
        (second_service, second),
    ) = await _two_operations_on_one_allocation(pool, ALLOCATION)

    # The first operation lists and seals, then publishes -- before the second re-lists,
    # because the second's listing will replace the row and take the binding with it.
    await finish_allocation(pool, first_service, first, members)
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        first_digest = await _publish(
            first_service, connection, first, observations=observations
        )

    # The second now takes a listing of its own and seals convergently on the same
    # revision, since membership never moved.
    await finish_allocation(pool, second_service, second, members)
    async with pool.acquire() as connection:
        second_digest = await _publish(
            second_service, connection, second, observations=observations
        )
        # Same provider state, but distinct fresh-query identities and digests.
        assert first_digest != second_digest
        rows = await connection.fetch(
            "SELECT operation_id, attempt_id, executor_id, fence_token FROM "
            "harness_provider_report WHERE report_digest=ANY($1) ORDER BY operation_id",
            [first_digest, second_digest],
        )
    # Two attestations of the same bytes, distinguished by who attested them.
    assert len(rows) == 2
    assert {row["operation_id"] for row in rows} == {
        first.operation_id,
        second.operation_id,
    }


async def test_a_successor_attempt_can_publish_unchanged_provider_state(pool):
    """F4. A recovery successor re-querying an unchanged provider must not be refused.

    The case that made cleanup permanently unavailable: the first attempt publishes, the
    lease lapses and recovery takes over, and the successor observes exactly the same
    provider state -- byte-identical canonical observations. Under a digest-only key its
    own predecessor's row owned the digest and the successor was refused forever.

    The successor's attestation is its own row, and the predecessor's must not
    authorize it: `_verify_report` looks up the full binding, so the old attempt's row
    is not found for the new grant rather than found and hopefully rejected.

    The successor re-queries the provider for its own listing before re-sealing, which
    is the F3 requirement and is also what a real recovery does: the takeover happened
    because the previous holder stopped responding, so what it last saw is not evidence
    about now. Membership has not changed, so the re-seal converges on the one
    revision -- and both reports are therefore bound to the same revision, which keeps
    this test about the GRANT binding and not about the revision.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
    successor = await fence_out(pool, lease)
    assert successor.fence_token > lease.fence_token

    successor_service = authority(pool, successor)
    assert (
        await finish_allocation(
            pool, successor_service, successor, (resource("cluster-1"),)
        )
        == revision
    )
    async with pool.acquire() as connection:
        successor_digest = await _publish(
            successor_service, connection, successor, observations=observations
        )
    assert successor_digest != digest

    # The successor's own grant verifies against its own attestation.
    assert (
        await successor_service.read_inventory(
            executor_id=successor.holder,
            workspace_id=successor.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=successor_digest,
        )
        is not None
    )
    # The superseded grant does not, even though its row still exists and its digest
    # matches: it is no longer the current authority.
    assert (
        await authority(pool, lease).read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_an_old_grants_attestation_cannot_authorize_a_current_one(pool):
    """F4. The stored row must bind the attempt and fence, not merely the operation.

    Constructed as the narrow case: the predecessor's attestation exists and the
    successor never publishes one of its own. A verifier that looked up by digest and
    operation alone would find the old row, match on operation and executor, and
    authorize the successor's cleanup on evidence the successor never produced.

    The successor does everything else legitimately -- it re-lists the provider and
    re-seals the unchanged membership -- so the inventory is complete and the ONLY
    thing missing is an attestation of its own. That is what makes the refusal
    attributable to the grant binding.
    """
    _record, predecessor_service, lease, _revision = await sealed(
        pool, (resource("cluster-1"),)
    )
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(
            predecessor_service, connection, lease, observations=observations
        )
    successor = await fence_out(pool, lease)
    service = authority(pool, successor)
    await finish_allocation(pool, service, successor, (resource("cluster-1"),))

    assert (
        await service.read_inventory(
            executor_id=successor.holder,
            workspace_id=successor.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_a_stale_fence_cannot_read_a_verified_inventory(pool):
    """AC-01's stale fence on the read: authority is a question about the present."""
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
    await fence_out(pool, lease)

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_an_unresolvable_authority_yields_none(pool):
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="not-the-token",
            report_digest="0" * 64,
        )
        is None
    )


async def test_another_executors_authority_does_not_read_this_executor(pool):
    """A grant that resolves cleanly, but to somebody else."""
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)

    assert (
        await service.read_inventory(
            executor_id="worker-2",
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


async def test_an_allocation_the_plan_does_not_bind_is_refused(pool):
    """The allocation comes from the approval-bound plan, never from the question."""
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id="somebody-elses-allocation",
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Completeness: AC-01's incomplete inventory
# ---------------------------------------------------------------------------


async def test_an_unfinished_plan_cannot_be_sealed_or_attested(pool):
    """AC-01. A plan still creating things has membership that will still grow.

    The original shape of this test published a report and asserted the read came back
    `complete=False` with empty membership -- "nothing to clean up" refused. That is
    still the behaviour being protected, but under the final contract the sequence
    stops one step earlier and in a strictly stronger place: an allocation whose plan
    has not finished cannot be sealed, and an unsealed allocation cannot be attested at
    all. So the expensive mistake -- treating an empty membership as zero cost while an
    unobserved call may have created a cluster -- is now unreachable rather than
    reachable-but-flagged.

    Both refusals are asserted, because they are independent gates and a repair could
    remove either: the seal refuses an unfinished plan, and the publication refuses an
    unsealed allocation.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
        # No calls recorded at all: `confirmed_plan_progress` is UNKNOWN.
        with pytest.raises(OperationRefused) as seal_refusal:
            await service.seal_allocation(connection, lease)
        with pytest.raises(OperationRefused) as publish_refusal:
            await _publish(
                service,
                connection,
                lease,
                observations={"cluster-1": unknown("cluster-1")},
            )
    assert "creation has not finished" in str(seal_refusal.value)
    assert "not sealed" in str(publish_refusal.value)
    # And with no attestation there is no authority: the answer is retained exposure
    # naming the allocation, never a release.
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations={"cluster-1": unknown("cluster-1")},
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert record is not None


async def test_an_unresolved_call_blocks_completeness(pool):
    """A call that `may_have_happened` means a resource may exist unenumerated.

    Distinct from the plan-progress gate above: the plan reads as covered -- every step
    has a call row -- but one of those calls is UNRESOLVED, so a resource may exist
    that no enumeration could have included.

    Driven to the read side rather than stopping at the seal, because that is where the
    rule has to hold. The allocation is legitimately sealed and attested while the plan
    is genuinely complete, and the unresolved call is then recorded by the SUCCESSOR
    after a takeover -- exactly how this arises in production, a recovery issuing a step
    and dying mid-call. The attestation is the successor's own, so nothing else about
    the read is in doubt: `complete` is false because of the call state alone.

    **The unresolved call is a teardown**, and that is the point rather than an
    accommodation. This clause asks whether a call MAY HAVE HAPPENED, not whether it may
    have created: an unresolved `delete_cluster` means the provider may or may not have
    destroyed the resource, so the published report may already describe state that no
    longer holds, and releasing against it is exactly as unsafe. A creating call cannot
    be used to reach this clause any more -- the fence refuses one into a sealed
    allocation (F1), and refuses the seal while one is outstanding -- so a teardown is
    now the only legal route here, and it proves the two rules are independent: the
    fence PERMITS this call, because a closed allocation must still be tearable down,
    and completeness blocks on it anyway.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": unknown("cluster-1")}
    successor = await fence_out(pool, lease)
    successor_service = authority(pool, successor)
    assert (
        await finish_allocation(
            pool, successor_service, successor, (resource("cluster-1"),)
        )
        == revision
    )
    async with pool.acquire() as connection:
        digest = await _publish(
            successor_service, connection, successor, observations=observations
        )
        # It is complete right now, so the assertion below is about the call and
        # nothing else.
        verified = await successor_service.read_inventory(
            executor_id=successor.holder,
            workspace_id=successor.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
    assert verified is not None and verified.complete is True

    # The successor issues a teardown of the cluster and observes nothing back.
    # Permitted into the sealed allocation by design; still fatal to completeness.
    async with pool.acquire() as connection:
        await record_intent(
            connection,
            successor,
            idempotency_key="teardown-of-cluster-1",
            provider=STEP["provider"],
            operation_kind="delete_cluster",
            target=STEP["target"],
        )
        # UNKNOWN moves the row to UNRESOLVED: the worker observed nothing.
        await observe(
            connection,
            successor,
            idempotency_key="teardown-of-cluster-1",
            outcome=CallOutcome.UNKNOWN,
        )

    # The teardown changed the provider generation; the old report is revoked.
    inventory = await successor_service.read_inventory(
        executor_id=successor.holder,
        workspace_id=successor.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    assert inventory is None


async def test_a_missing_resource_is_refused_a_seal_and_retains_exposure(pool):
    """AC-01's missing resource: a succeeded call whose handle nobody enumerated.

    The narrowest real gap -- an executor that enumerated the disk and forgot the
    cluster the same call created. The provider listing it could honestly record covers
    only the disk, so the listing agrees with membership and clause (4) is satisfied;
    what gives the gap away is the call record's own `provider_ref`, naming a handle
    membership never mentions.

    **The refusal is now at the seal rather than at the read** (F1, rule 2). Previously
    this membership could be sealed and attested and only then read as
    `complete=False` -- safe on that path, but the seal row was a standing declaration
    that the allocation was final while a succeeded creating call's handle was
    unaccounted for. Anything that trusted the seal instead of re-deriving completeness
    would have released against it. Refusing to close the allocation at all is the
    stronger contract, so this asserts the refusal, that no seal row exists, and that
    cleanup consequently retains the exposure rather than releasing it.

    The read-side rule has not been weakened, and is not being traded away here:
    `_completeness` still re-derives this same condition -- clause (3) per-operation and
    clause (6) allocation-wide -- which is what
    `test_a_seal_inserted_behind_the_authority_still_reads_as_incomplete` asserts
    directly, against a seal row written around this refusal.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("disk-1", kind="storage"),)
        )
    # The call's own provider handle names the cluster, which membership never mentions.
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")

    async with pool.acquire() as connection:
        # The listing the executor could honestly record covers only the disk it knows
        # about, so the seal's provider-enumeration requirement is met and the refusal
        # below is attributable to the unaccounted handle alone.
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"disk-1-handle"}),
        )
        with pytest.raises(OperationRefused) as refusal:
            await service.seal_allocation(connection, lease)
    # Named, not merely refused: the message carries the handle an operator has to go
    # find, because "something is unaccounted for" is not actionable.
    assert "unaccounted for" in str(refusal.value)
    assert "cluster-1-handle" in str(refusal.value)

    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_seal WHERE allocation_id=$1",
                ALLOCATION,
            )
            == 0
        )

    # And with no seal there is no attestation to publish against, so the money stays
    # put -- which is the outcome AC-01 is actually about.
    observations = {"disk-1": absent("disk-1")}
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED


async def test_a_seal_inserted_behind_the_authority_still_reads_as_incomplete(pool):
    """The read re-derives completeness; it never trusts the seal row.

    The test above asserts that this authority refuses to SEAL an allocation with an
    unaccounted creating call. This asserts the other half: that the refusal is not the
    only thing standing between that state and released money. The seal row is written
    directly, at exactly the revision `_completeness` will recompute -- the shape a
    restored backup, a migration, a future writer or a bypassed code path could leave --
    and the read must still report the inventory incomplete.

    This is why `_completeness` re-derives all six clauses instead of reading a stored
    boolean, and why clause (6) exists even though `seal_allocation` already refuses:
    a stored row cannot keep being true. Writing the row by hand is legitimate here for
    the same reason it is banned elsewhere in this suite -- the point is precisely a
    state the production path will not produce.

    The revision is computed with the production function, so the seal is VALID in every
    respect the other clauses check: it names the right membership, the listing agrees
    with it, and the plan is complete. Only the unaccounted handle is wrong, so a read
    that returned `complete=True` would be doing so for that reason alone.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    members = (resource("disk-1", kind="storage"),)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"disk-1-handle"}),
        )
        # The seal the authority refused to write, written anyway.
        await connection.execute(
            """
            INSERT INTO harness_allocation_seal (
                org_id, workspace_id, allocation_id, sealed_revision,
                operation_id, attempt_id, executor_id, fence_token
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            _revision_of(members),
            lease.operation_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
        )
        digest = await _publish(
            service, connection, lease, observations={"disk-1": absent("disk-1")}
        )

    inventory = await service.read_inventory(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    # Verified against a seal that exists and matches -- and still not complete.
    assert inventory is not None
    assert inventory.complete is False


async def test_a_provider_listing_naming_an_unenumerated_child_is_refused(pool):
    """F1. One call, two billable resources, one of them recorded.

    The case the previous completeness rule could not see. The step created a cluster
    AND the disk that came with it; the executor enumerated the cluster, whose handle
    is the call's own `provider_ref`, so every per-call check agreed. Membership was
    short by one independently billed resource and nothing in the record said so.

    Asking the provider what it holds is what closes it: the listing names a handle
    membership does not, so the proof of completeness is refused rather than recorded.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    # The call's own handle IS enumerated -- this is not the missing-handle case above.
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")

    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await service.record_provider_enumeration(
                connection,
                lease,
                attempt=await service.begin_provider_enumeration(
                    connection, lease, provider="aws"
                ),
                provider="aws",
                # The provider also holds the disk the cluster brought with it.
                provider_references=frozenset(
                    {"cluster-1-handle", "disk-child-handle"}
                ),
            )
    assert "disk-child-handle" in str(refusal.value)


async def test_an_omitted_child_resource_is_never_released(pool):
    """F1. Without the provider's agreement, an ABSENT report releases nothing.

    The expensive half of the same case: the report says the cluster is gone and the
    membership it is reconciled against contains only the cluster, so the old rule
    produced RELEASED with zero exposure while the omitted disk carried on billing.

    No provider enumeration means no seal, no seal means the report cannot even be
    published, and with no attestation the answer is retained exposure naming the
    allocation -- never a release. The chain is asserted at both ends: the publication
    is refused, and the assessment that would have released is UNRESOLVED.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        # The executor has not asked the provider what it holds, so nothing can be
        # sealed and nothing can be attested.
        with pytest.raises(OperationRefused):
            await service.seal_allocation(connection, lease)
        with pytest.raises(OperationRefused):
            await _publish(service, connection, lease, observations=observations)

    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is not ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.may_mark_released is False
    assert assessment.unresolved_resources == (ALLOCATION,)


async def test_enumerating_the_omitted_child_restores_completeness(pool):
    """F1. The repair is fail-closed, not fail-forever.

    Refusing the listing is only correct if the executor can then do the right thing. It
    enumerates the disk it missed, the provider's listing now matches membership, the
    allocation seals, the report publishes against that seal, and the inventory is
    complete -- over BOTH resources.
    """
    observations = {
        "cluster-1": absent("cluster-1"),
        "disk-child": absent("disk-child"),
    }
    members = (
        resource("cluster-1"),
        resource("disk-child", kind="storage"),
    )
    _record, service, lease, _revision = await sealed(
        pool, members, provider_ref="cluster-1-handle"
    )
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)

    inventory = await service.read_inventory(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    assert inventory is not None
    assert inventory.complete is True
    assert sorted(item.resource_id for item in inventory.resources) == [
        "cluster-1",
        "disk-child",
    ]


async def test_a_listing_taken_before_the_last_resource_does_not_prove_completeness(
    pool,
):
    """F1. A listing is evidence about the moment it was taken, not a permanent badge.

    Recorded while membership held only the cluster, it agreed with membership then.
    If merely HAVING a row counted, the disk enumerated afterwards would be vouched
    for by a listing taken before it existed. The recorded digest is compared against
    the handles enumerated now, so the stale listing reads as a mismatch.

    Which is asserted at the seal -- where the mismatch first has consequences -- and
    then at the read, on the executor's own attestation of the smaller allocation.
    Reaching the read requires the stale listing to be the ONLY problem, so the
    allocation is legitimately sealed and attested over the cluster first, and the disk
    is then added by an `enumerate_resources` that a sealed allocation would refuse. It
    is written directly for that reason: this test's subject is the listing, and the
    seal's own refusal of post-seal growth is the F2 tests' subject.
    """
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    async with pool.acquire() as connection:
        digest = await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )
        # A member appearing other than through this package's write path, so the
        # question is only whether the old listing still counts.
        await connection.execute(
            "INSERT INTO harness_allocation_resource (org_id, workspace_id, "
            "allocation_id, resource_id, operation_id, provider, provider_reference, "
            "kind, attempt_id, fence_token) "
            "VALUES ($1,$2,$3,'disk-late',$4,'aws','disk-late-handle','storage',$5,$6)",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            lease.operation_id,
            lease.attempt_id,
            lease.fence_token,
        )
        # The listing recorded earlier is about the cluster alone, so re-proving
        # completeness over the grown membership is refused rather than inherited.
        with pytest.raises(OperationRefused) as refusal:
            await service.seal_allocation(connection, lease)
    assert "no current provider enumeration" in str(
        refusal.value
    ) or "different membership" in str(refusal.value)

    inventory = await service.read_inventory(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    assert inventory is not None
    assert inventory.complete is False


async def test_an_enumeration_taken_before_creation_finished_is_refused(pool):
    """F1. A listing taken mid-creation cannot vouch for what does not exist yet.

    Without this, the earliest possible listing -- taken before the plan created
    anything -- would trivially agree with an empty-ish membership and become the
    proof for everything created afterwards.
    """
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
        # No succeeded call recorded, so `confirmed_plan_progress` is not COMPLETE.
        with pytest.raises(OperationRefused) as refusal:
            await service.record_provider_enumeration(
                connection,
                lease,
                attempt=await service.begin_provider_enumeration(
                    connection, lease, provider="aws"
                ),
                provider="aws",
                provider_references=frozenset({"cluster-1-handle"}),
            )
    assert "creation has not finished" in str(refusal.value)


async def test_an_incomplete_inventory_never_releases_budget(pool):
    """An unresolved teardown revokes old evidence and retains the whole allocation.

    The returned inventory is unverified until a new listing and report can be
    obtained; it cannot be used as authority to return any reservation.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        await _publish(service, connection, lease, observations=observations)
        await record_intent(
            connection,
            lease,
            idempotency_key="an-extra-call",
            provider=STEP["provider"],
            operation_kind="delete_cluster",
            target=STEP["target"],
        )
    assert revision

    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.inventory is None
    assert assessment.unresolved_resources == (ALLOCATION,)
    assert not assessment.may_return_reservation_unused


# ---------------------------------------------------------------------------
# Reconciliation and release
# ---------------------------------------------------------------------------


async def finish_allocation(pool, service, lease, resources):
    """Record the provider enumeration and seal, as a production executor would.

    These are the two proofs `complete` now requires, and they are driven through the
    real methods rather than by INSERTing rows: `_completeness` re-derives both, so a
    hand-written row would let this suite agree with itself about a shape the production
    path never produces.

    The provider is taken to hold exactly the handles enumerated, which is the case
    where it agrees with membership. Tests where it holds something membership omits
    call `record_provider_enumeration` directly -- that is the F1 case, and it is
    refused.
    """
    by_provider = {}
    for item in resources:
        by_provider.setdefault(item.provider, set()).add(item.provider_reference)
    async with pool.acquire() as connection:
        for provider, handles in by_provider.items():
            await service.record_provider_enumeration(
                connection,
                lease,
                attempt=await service.begin_provider_enumeration(
                    connection, lease, provider=provider
                ),
                provider=provider,
                provider_references=frozenset(handles),
            )
        return await service.seal_allocation(connection, lease)


async def sealed(pool, resources, *, provider_ref=None, key="key-1", holder="worker-1"):
    """An operation whose plan is complete and whose allocation is sealed and final.

    The production sequence up to the point a report may be taken: enumerate
    membership, finish creating (so the plan is confirmed complete), record the
    provider's own listing, seal.

    Returns the sealed revision along with the operation, because the revision is now
    part of what a report attests to -- a test asserting on it should not have to
    recompute it.
    """
    record, lease = await leased(pool, key=key, holder=holder)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=resources)
    await complete_the_plan(
        pool,
        record,
        lease,
        provider_ref=provider_ref or resources[0].provider_reference,
    )
    revision = await finish_allocation(pool, service, lease, resources)
    return record, service, lease, revision


async def published(pool, observations, resources, *, provider_ref=None, key="key-1"):
    """An operation whose allocation is sealed, with a report published against it.

    **The order is the contract, not a convenience.** Creation finishes, the provider
    is asked to list what it holds, the allocation is sealed, and only then is the
    provider queried about presence and the answer published. An earlier version of
    this helper published FIRST -- and that order is the F1 defect itself: a report
    taken before anything existed would truthfully say ABSENT, and nothing downstream
    could tell that the observation predated the membership it was clearing. The
    authority now refuses publication into an unsealed allocation, so this helper
    cannot express the unsafe order even by accident; the negative case is asserted
    directly in `test_a_report_taken_before_creation_can_never_authorize_a_release`.
    """
    _record, service, lease, _revision = await sealed(
        pool, resources, provider_ref=provider_ref, key=key
    )
    async with pool.acquire() as connection:
        await _publish(service, connection, lease, observations=observations)
    return service, lease


async def test_provider_established_absence_releases_the_allocation(pool):
    observations = {"cluster-1": absent("cluster-1")}
    service, lease = await published(pool, observations, (resource("cluster-1"),))
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.NONE
    assert assessment.may_mark_released is True
    assert assessment.may_return_reservation_unused is True
    assert dict(assessment.dispositions) == {"cluster-1": BudgetDisposition.RELEASE}


async def test_retained_storage_cost_keeps_the_allocation_retained(pool):
    """AC-01's retained storage cost.

    The cluster is gone and its disk is not. A per-call `provider_ref` inventory would
    have released here, because the call's one handle named the cluster.
    """
    observations = {"cluster-1": absent("cluster-1"), "disk-1": present("disk-1")}
    service, lease = await published(
        pool,
        observations,
        (resource("cluster-1"), resource("disk-1", kind="storage")),
        provider_ref="cluster-1-handle",
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.RETAINED
    assert assessment.exposure is CostExposure.ACTIVE
    assert assessment.may_return_reservation_unused is False
    assert assessment.unresolved_resources == ("disk-1",)
    assert dict(assessment.dispositions) == {
        "cluster-1": BudgetDisposition.RELEASE,
        "disk-1": BudgetDisposition.SETTLE,
    }


async def test_retained_network_cost_keeps_the_allocation_retained(pool):
    """AC-01's retained network cost: a load balancer outliving its cluster."""
    observations = {"cluster-1": absent("cluster-1"), "lb-1": present("lb-1", "active")}
    service, lease = await published(
        pool,
        observations,
        (resource("cluster-1"), resource("lb-1", kind="network")),
        provider_ref="cluster-1-handle",
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.exposure is CostExposure.ACTIVE
    assert assessment.unresolved_resources == ("lb-1",)


async def test_an_unknown_observation_never_collapses_into_absent(pool):
    """The provider could not be consulted. Explicitly not zero cost."""
    observations = {"cluster-1": unknown("cluster-1")}
    service, lease = await published(pool, observations, (resource("cluster-1"),))
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.unresolved_resources == ("cluster-1",)
    assert dict(assessment.dispositions) == {"cluster-1": BudgetDisposition.RETAIN}


async def test_a_disappearing_resource_is_unresolved_not_released(pool):
    """AC-01's disappearing resource: enumerated, then absent from the report.

    "We did not ask about it" is not "it is gone". Deriving the expected set from the
    observations instead of from membership would make the report self-certifying.
    """
    published_observations = {
        "cluster-1": absent("cluster-1"),
        "disk-1": absent("disk-1"),
    }
    service, lease = await published(
        pool,
        published_observations,
        (resource("cluster-1"), resource("disk-1", kind="storage")),
        provider_ref="cluster-1-handle",
    )
    # A later, narrower report that simply omits the disk.
    narrowed = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        await _publish(service, connection, lease, observations=narrowed)
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=narrowed,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert "disk-1" in assessment.unresolved_resources


async def test_an_observation_for_a_non_member_is_unresolved(pool):
    """A foreign or stale report must not be reconciled as if it were about this."""
    observations = {"cluster-1": absent("cluster-1"), "ghost-1": absent("ghost-1")}
    service, lease = await published(
        pool, observations, (resource("cluster-1"),), provider_ref="cluster-1-handle"
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert "ghost-1" in assessment.unresolved_resources


async def test_an_observation_answered_about_another_identity_is_unresolved(pool):
    """A query by the wrong identity is answered confidently about the wrong thing."""
    observations = {
        "cluster-1": ResourceObservation(
            presence=ResourcePresence.ABSENT, queried_by="some-other-resource"
        )
    }
    service, lease = await published(pool, observations, (resource("cluster-1"),))
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED


async def test_absence_established_by_querying_the_local_name_never_releases(pool):
    """**F3.** The local id is not a handle the provider can be asked about.

    The reviewer's exact case, with the two identities deliberately far apart: the
    member is recorded as `cluster-1` and the machine is `i-123`. The teardown queries
    the name it happens to have to hand -- `cluster-1` -- and the provider answers,
    correctly and confidently, that it holds nothing by that name.

    Under the old check that ABSENT released the budget, because `queried_by` equalled
    the mapping key and the key was all that was compared. Nothing was malformed and
    nobody lied; the question was about a name the provider has never issued, and
    `i-123` kept running and kept billing.

    The distinction matters because it is the ordinary shape of the data rather than an
    exotic one: local ids are chosen here, handles are issued by the provider, and a
    teardown holding one is not holding the other.
    """
    member = AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="i-123",
        kind="compute",
    )
    # Queried by the LOCAL name. The provider's answer is truthful and irrelevant.
    observations = {
        "cluster-1": ResourceObservation(
            presence=ResourcePresence.ABSENT, queried_by="cluster-1"
        )
    }
    service, lease = await published(
        pool, observations, (member,), provider_ref="i-123"
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is not ReleaseState.RELEASED, (
        "budget was released on an absence established about the local name, not i-123"
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.may_return_reservation_unused is False
    # Named, so an operator knows which resource was not established rather than being
    # told something was wrong somewhere.
    assert "cluster-1" in assessment.unresolved_resources
    assert dict(assessment.dispositions) == {"cluster-1": BudgetDisposition.RETAIN}


async def test_the_same_absence_queried_by_the_provider_handle_does_release(pool):
    """**F3, the positive half.** The rule is fail-closed, not fail-forever.

    Identical to the test above in every respect except the identity the query used:
    `i-123`, the handle the record says to ask about. This pair is what establishes that
    the refusal above is about the IDENTITY and not about the unusual handle, the
    distinct names, or anything else that differs from the ordinary fixtures -- without
    it, a bug that refused everything would pass the negative test and look correct.
    """
    member = AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="i-123",
        kind="compute",
    )
    observations = {
        "cluster-1": ResourceObservation(
            presence=ResourcePresence.ABSENT, queried_by="i-123"
        )
    }
    service, lease = await published(
        pool, observations, (member,), provider_ref="i-123"
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.NONE
    # Keyed by the LOCAL id, not the handle: that is the vocabulary the domain's ledger
    # is written in, so a disposition keyed by `i-123` would be unmatchable there.
    assert dict(assessment.dispositions) == {"cluster-1": BudgetDisposition.RELEASE}


async def test_presence_queried_by_the_local_name_is_not_authoritative_either(pool):
    """F3 is not ABSENT-specific: a wrong-identity PRESENT is also not evidence.

    Retained either way, so this does not change what the budget does -- but it changes
    what the system CLAIMS. `RETAINED` asserts the provider confirmed the resource is
    still there; `UNRESOLVED` says nobody established anything. A confident answer about
    the wrong identity supports the second, and an operator reading "the provider still
    holds this" would stop looking.
    """
    member = AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="i-123",
        kind="compute",
    )
    observations = {
        "cluster-1": ResourceObservation(
            presence=ResourcePresence.PRESENT,
            queried_by="cluster-1",
            provider_state="RUNNING",
        )
    }
    service, lease = await published(
        pool, observations, (member,), provider_ref="i-123"
    )
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is ReleaseState.UNRESOLVED
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert dict(assessment.dispositions) == {"cluster-1": BudgetDisposition.RETAIN}


async def test_no_authority_never_reports_zero_cost(pool):
    """Unavailability is retained exposure, never an implicit release."""
    _record, lease = await leased(pool)
    service = authority(pool, lease)
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="not-the-token",
        observations={"cluster-1": absent("cluster-1")},
    )
    assert assessment.exposure is CostExposure.UNRESOLVED
    assert assessment.unresolved_resources == (ALLOCATION,)


# ---------------------------------------------------------------------------
# Concurrency: AC-01
# ---------------------------------------------------------------------------


async def _two_operations_on_one_allocation(pool, allocation):
    """Two separately approved operations, same tenant, same allocation.

    The shape F2 is about, and the reason a one-lease race is not a test of it: each
    operation has its own operation row and its own lease, so the lease locks are two
    DIFFERENT locks and grant no mutual exclusion at all. The only thing that can
    serialize them is a lock keyed to the allocation they share.

    Returns `(creator, sealer)` as `(service, lease)` pairs, with the cluster already a
    member, both plans complete, and the sealer's own provider listing recorded -- so
    the only writes left are the two that race.
    """
    creator_record, creator_lease = await leased(
        pool,
        key=f"{allocation}-creator",
        holder="worker-creator",
        allocation=allocation,
    )
    creator = authority(pool, creator_lease)
    async with pool.acquire() as connection:
        await creator.enumerate_resources(
            connection, creator_lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(
        pool, creator_record, creator_lease, provider_ref="cluster-1-handle"
    )

    sealer_record, sealer_lease = await leased(
        pool, key=f"{allocation}-sealer", holder="worker-sealer", allocation=allocation
    )
    sealer = authority(pool, sealer_lease)
    await complete_the_plan(
        pool, sealer_record, sealer_lease, provider_ref="cluster-1-handle"
    )
    async with pool.acquire() as connection:
        await sealer.record_provider_enumeration(
            connection,
            sealer_lease,
            attempt=await sealer.begin_provider_enumeration(
                connection, sealer_lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
    return (creator, creator_lease), (sealer, sealer_lease)


async def test_one_operations_seal_refuses_another_operations_growth(pool):
    """**F2, deterministic.** The seal is allocation-wide, not operation-wide.

    Ordered rather than raced, so the property is asserted without depending on which
    connection wins. Operation B seals the allocation; operation A -- a different
    approved operation, a different lease, the same allocation -- then tries to add a
    resource and is refused.

    This is the half that a lease-scoped lock cannot provide. `enumerate_resources`
    reads "is there a seal?" and then inserts, and with only the lease lock held those
    two steps are not serialized against ANOTHER operation's seal: the check can pass,
    the other operation's seal can commit, and the insert can then land on a sealed
    allocation. The refusal here is that gap closed.
    """
    allocation = "alloc-two-ops"
    (
        (creator, creator_lease),
        (sealer, sealer_lease),
    ) = await _two_operations_on_one_allocation(pool, allocation)
    async with pool.acquire() as connection:
        revision = await sealer.seal_allocation(connection, sealer_lease)

    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await creator.enumerate_resources(
                connection,
                creator_lease,
                resources=(resource("disk-late", kind="storage"),),
            )
        assert "sealed" in str(refusal.value)
        # Refused, not deferred: the row does not exist, so the sealed revision is
        # still the revision of what the allocation holds.
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource WHERE "
                "allocation_id=$1",
                allocation,
            )
            == 1
        )
    # And the refusal is attributed to the operation that attempted it, which is how an
    # operator finds out something was created after teardown began.
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit WHERE operation_id=$1 "
                "AND event='inventory.refused' AND allowed = false AND detail='sealed'",
                creator_lease.operation_id,
            )
            == 1
        )
    assert revision


async def test_concurrent_creation_and_cleanup_do_not_release_a_growing_allocation(
    pool,
):
    """**AC-01's concurrent creation/cleanup, and the F2 repair.** A real two-operation
    race.

    The defect this replaces, in two steps. First, the original test ACCEPTED the bad
    outcome: it allowed RELEASED and then added `disk-late`, documenting the bug rather
    than catching it. Then the replacement raced two coroutines under ONE lease -- which
    the lease lock does serialize, so it passed, but it was not a test of the property.
    Two separately approved operations naming the same allocation hold two different
    lease locks and are not serialized by them at all. That is the case here: separate
    operation rows, separate leases, one allocation, one adding a resource while the
    other closes it.

    The invariant is that the two writes are mutually exclusive in effect. Exactly one
    can succeed:

    * the seal lands first -- the late creation is refused, and membership is still
      exactly what was sealed, so a release is authorized over a list that cannot
      change;
    * the creation lands first -- the seal is over membership whose new member the
      sealer's provider listing does not cover, so the seal is refused, and no release
      is authorized at all.

    **Both succeeding is the state that must be impossible**, and it is what the
    allocation lock exists to prevent: the creator checks for a seal, the sealer reads
    membership and commits a seal over it, and the creator then commits its row into an
    allocation that has been declared final. The result is not a released-then-grown
    allocation -- the revision check would catch that -- but an allocation that can
    never be released again, because its membership can no longer equal any seal. Safe,
    permanently stuck, and silent. Asserted directly below by reading both the seal and
    the membership it claims to cover.

    Run over several allocations with the two coroutines launched in BOTH orders, so
    each side gets to win and both branches of the invariant are actually asserted
    rather than only one being exercised by whichever `gather` happens to schedule
    first. The lock is what makes each order end in exactly one winner; without it the
    loser's check-then-write straddles the winner's commit.
    """
    outcomes = []
    for round_number in range(4):
        allocation = f"alloc-race-{round_number}"
        (
            (creator, creator_lease),
            (sealer, sealer_lease),
        ) = await _two_operations_on_one_allocation(pool, allocation)

        async def create_more(creator=creator, creator_lease=creator_lease):
            async with pool.acquire() as connection:
                return await creator.enumerate_resources(
                    connection,
                    creator_lease,
                    resources=(resource("disk-late", kind="storage"),),
                )

        async def take_seal(sealer=sealer, sealer_lease=sealer_lease):
            async with pool.acquire() as connection:
                return await sealer.seal_allocation(connection, sealer_lease)

        if round_number % 2:
            seal, added = await asyncio.gather(
                take_seal(), create_more(), return_exceptions=True
            )
        else:
            added, seal = await asyncio.gather(
                create_more(), take_seal(), return_exceptions=True
            )
        grew = not isinstance(added, Exception)
        closed = not isinstance(seal, Exception)
        outcomes.append("grew" if grew else "closed")

        # The impossible state, read straight from the database rather than inferred
        # from what the calls returned.
        assert not (grew and closed), (
            "a seal and a post-seal member both committed: the allocation can never "
            "be released again"
        )
        async with pool.acquire() as connection:
            members = [
                row["resource_id"]
                for row in await connection.fetch(
                    "SELECT resource_id FROM harness_allocation_resource WHERE "
                    "allocation_id=$1 ORDER BY resource_id",
                    allocation,
                )
            ]
            stored_seal = await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE "
                "allocation_id=$1",
                allocation,
            )

        observations = {"cluster-1": absent("cluster-1")}
        if closed:
            # The seal won. Membership is exactly what it covers, and the sealer can
            # publish against it and be granted the release.
            assert members == ["cluster-1"]
            assert isinstance(added, OperationRefused)
            async with pool.acquire() as connection:
                await _publish(
                    sealer, connection, sealer_lease, observations=observations
                )
            assessment = await sealer.assess_cleanup(
                executor_id=sealer_lease.holder,
                workspace_id=sealer_lease.workspace_id,
                allocation_id=allocation,
                operation_authority="authority-token",
                observations=observations,
            )
            assert assessment.state is ReleaseState.RELEASED
            assert assessment.exposure is CostExposure.NONE
        else:
            # The creation won. Nothing is sealed, so nothing can be attested and no
            # release is reachable by any route -- including the creator's own.
            assert members == ["cluster-1", "disk-late"]
            assert stored_seal is None
            assert isinstance(seal, OperationRefused)
            async with pool.acquire() as connection:
                with pytest.raises(OperationRefused):
                    await _publish(
                        creator, connection, creator_lease, observations=observations
                    )
            assessment = await creator.assess_cleanup(
                executor_id=creator_lease.holder,
                workspace_id=creator_lease.workspace_id,
                allocation_id=allocation,
                operation_authority="authority-token",
                observations=observations,
            )
            assert assessment.state is not ReleaseState.RELEASED
            assert assessment.exposure is CostExposure.UNRESOLVED
    # Not an assertion: which side wins is the scheduler's business, and requiring both
    # would make this test flaky. Recorded so a reader can see what a run covered.
    print(f"race outcomes: {outcomes}")


async def test_membership_cannot_grow_after_the_allocation_is_sealed(pool):
    """F2. The seal is what makes growth refused rather than merely unlikely.

    Deterministic counterpart to the race above: once a release-authorizing snapshot can
    be taken, an addition is a real event -- something was created after teardown began
    --
    so it is refused and audited rather than absorbed into an inventory a release has
    already been decided from.

    Same operation as the one that sealed, which is the easier half of the property;
    the cross-operation case is `test_one_operations_seal_refuses_another_operations_
    growth`, and it is the one that needs the allocation lock.
    """
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    async with pool.acquire() as connection:
        await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )

    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await service.enumerate_resources(
                connection, lease, resources=(resource("disk-late", kind="storage"),)
            )
        assert "sealed" in str(refusal.value)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource WHERE "
                "allocation_id=$1 AND resource_id=$2",
                ALLOCATION,
                "disk-late",
            )
            == 0
        )
        # The refusal is on the record, not just in the exception.
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit WHERE operation_id=$1 "
                "AND event=$2 AND allowed = false AND detail = 'sealed'",
                lease.operation_id,
                "inventory.refused",
            )
            == 1
        )


async def test_sealing_is_idempotent_but_refuses_moved_membership(pool):
    """F2. A retried seal converges; a seal over membership that has moved does not.

    Both halves matter. Without idempotence a transport failure after a successful seal
    would leave an allocation that can never be re-sealed and therefore never released.
    Without the revision comparison, a seal taken after a row appeared behind this
    package's back would overwrite the earlier claim and certify the larger membership
    as though it had been sealed all along.
    """
    members = (resource("cluster-1"),)
    _record, service, lease, first = await sealed(pool, members)
    assert await finish_allocation(pool, service, lease, members) == first
    async with pool.acquire() as connection:
        await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )

    # A row arriving other than through `enumerate_resources` -- a replica, a repair
    # script, a future caller. The seal must not bless it.
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO harness_allocation_resource (org_id, workspace_id, "
            "allocation_id, resource_id, operation_id, provider, provider_reference, "
            "kind, attempt_id, fence_token) "
            "VALUES ($1,$2,$3,'disk-smuggled',$4,'aws','disk-smuggled-handle',"
            "'storage',$5,$6)",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            lease.operation_id,
            lease.attempt_id,
            lease.fence_token,
        )
        with pytest.raises(OperationRefused) as refusal:
            await service.seal_allocation(connection, lease)
    assert "different membership" in str(refusal.value) or "no current provider" in str(
        refusal.value
    )

    # And the inventory reads incomplete, because the seal no longer names this
    # membership's revision -- so no release is authorized over the smuggled row.
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations={"cluster-1": absent("cluster-1")},
    )
    assert assessment.state is not ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.UNRESOLVED


async def test_an_allocation_with_no_members_cannot_be_sealed(pool):
    """F2. Sealing nothing would certify "nothing to clean up" and release
    everything."""
    record, lease = await leased(pool)
    service = authority(pool, lease)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await service.seal_allocation(connection, lease)
    assert "no enumerated members" in str(refusal.value)


async def test_a_stale_fence_cannot_seal_an_allocation(pool):
    """F2. The seal claims authority now, so a superseded holder cannot take it.

    A stale holder that could seal would close an allocation the current holder is
    still creating into, and its snapshot would then read as final.
    """
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(
            connection, lease, resources=(resource("cluster-1"),)
        )
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
    await fence_out(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused):
            await service.seal_allocation(connection, lease)
        with pytest.raises(OperationRefused):
            await service.record_provider_enumeration(
                connection,
                lease,
                attempt=await service.begin_provider_enumeration(
                    connection, lease, provider="aws"
                ),
                provider="aws",
                provider_references=frozenset({"cluster-1-handle"}),
            )


# ---------------------------------------------------------------------------
# F1: sealing withdraws the authority to CREATE, not only to record
# ---------------------------------------------------------------------------
#
# The distinction these tests turn on: every test above races DATABASE WRITES, and the
# seal was already enough to order those. What it did not order was the PROVIDER CALL.
# A second approved operation on the same allocation could record an intent and invoke
# the provider after the first sealed; the membership write was then refused, so the
# resource existed, billed, and appeared in no inventory -- while the sealed membership
# and its ABSENT report authorized releasing the budget that would have paid for it.
#
# So these tests assert against the provider hook rather than against a row. The hook
# records whether it was called, which makes "was spend incurred" an observation instead
# of an inference from what the database happens to contain afterwards.


def spy_provider(calls, *, provider_ref="disk-late-handle", gate=None):
    """A provider hook that records its invocations. The heart of these tests.

    Asserting on `calls` rather than on the intent row is the point: an intent row can
    be refused, rolled back or reconciled away, and none of that tells you whether the
    provider was contacted. Money follows the invocation, so the invocation is what is
    asserted.

    `gate` is an optional `asyncio.Event` the hook waits on before returning, used to
    hold a call inside the dispatch window while something else commits.
    """

    async def provider(call):
        calls.append(call.idempotency_key)
        if gate is not None:
            await asyncio.wait_for(gate.wait(), 3)
        return CallOutcome.SUCCEEDED, None, provider_ref

    return provider


async def _creator_with_an_unstarted_plan(pool, allocation, *, key, holder):
    """An approved, leased operation on `allocation` whose planned step has not run.

    Distinct from `_two_operations_on_one_allocation`'s creator, whose plan is already
    complete. These tests need a creating call still AHEAD of the operation, because the
    question is whether it can be dispatched after somebody else seals.
    """
    record, lease = await leased(pool, key=key, holder=holder, allocation=allocation)
    service = authority(pool, lease)
    return record, service, lease


async def test_no_interleaving_permits_both_a_seal_and_a_later_provider_creation(pool):
    """**F1, the invariant.** A sealed allocation and a later creation cannot both win.

    Two separately approved operations name one allocation. One seals it, declaring its
    inventory final and its unused budget releasable. The other tries to create into it
    through the real `OperationExecutor.execute_provider` -- the trusted dispatch path,
    with a hook that records whether the provider was contacted.

    Exactly two outcomes are permissible, and both are asserted rather than only
    whichever the scheduler produces:

    * **the seal lands first** -- the creation is refused, the provider is NEVER
      contacted, and the sealed allocation can be released safely because nothing was
      created after it closed;
    * **the creation lands first** -- the provider may well be contacted, and the seal
      is then refused, because a creating call against this allocation is unaccounted
      for. Nothing is sealed, so no release is authorized by any route.

    **The forbidden state is a seal plus a provider invocation that followed it.** That
    is the one this repair exists to prevent, and it is asserted directly: if a seal row
    exists, no hook invocation may have happened after it was written. Note what the
    older tests could not catch -- they raced `enumerate_resources` against
    `seal_allocation`, which are both database writes, and a refused write costs
    nothing. A refused write after a successful provider call costs a resource that
    runs until somebody notices.

    Run in both launch orders over several allocations so each side gets to win. Each
    round's leases are closed before the next, because three live leases per round would
    otherwise hit the tenant concurrency limit and turn this into a test of that
    instead.
    """
    outcomes = []
    for round_number in range(4):
        allocation = f"alloc-create-race-{round_number}"
        # The sealer: plan complete, listing recorded, ready to seal.
        (
            (_member_service, member_lease),
            (sealer, sealer_lease),
        ) = await _two_operations_on_one_allocation(pool, allocation)

        # The creator: a separately approved operation whose creating step is still
        # ahead of it.
        (
            _creator_record,
            _creator_service,
            creator_lease,
        ) = await _creator_with_an_unstarted_plan(
            pool, allocation, key=f"{allocation}-late", holder="worker-late"
        )
        invoked = []
        executor = OperationExecutor(
            creator_lease,
            connect=pool.acquire,
            provider_call=spy_provider(invoked),
        )

        async def create_through_the_provider(executor=executor):
            return await executor.execute_provider(
                idempotency_key=f"late-call-{round_number}",
                provider="aws",
                operation_kind="create_disk",
                target=STEP["target"],
            )

        async def take_seal(sealer=sealer, sealer_lease=sealer_lease):
            async with pool.acquire() as connection:
                return await sealer.seal_allocation(connection, sealer_lease)

        if round_number % 2:
            seal, created = await asyncio.gather(
                take_seal(), create_through_the_provider(), return_exceptions=True
            )
        else:
            created, seal = await asyncio.gather(
                create_through_the_provider(), take_seal(), return_exceptions=True
            )
        closed = not isinstance(seal, Exception)
        dispatched = bool(invoked)
        outcomes.append("closed" if closed else "created")

        async with pool.acquire() as connection:
            stored_seal = await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE "
                "allocation_id=$1",
                allocation,
            )

        # The invariant, read from the provider hook and the seal row rather than
        # inferred from return values.
        assert not (closed and dispatched), (
            "the allocation was sealed AND the provider was contacted for a creating "
            "call: a billing resource now exists that no inventory names, while the "
            "seal authorizes releasing the budget for it"
        )

        if closed:
            assert stored_seal is not None
            # Refused before the provider was reached, which is the only refusal that
            # saves money.
            assert isinstance(created, ProviderCallRefused), created
            assert invoked == []
            assert "sealed" in str(created)
            # And the sealed allocation is releasable, because nothing was created into
            # it after it closed.
            observations = {"cluster-1": absent("cluster-1")}
            async with pool.acquire() as connection:
                await _publish(
                    sealer, connection, sealer_lease, observations=observations
                )
            assessment = await sealer.assess_cleanup(
                executor_id=sealer_lease.holder,
                workspace_id=sealer_lease.workspace_id,
                allocation_id=allocation,
                operation_authority="authority-token",
                observations=observations,
            )
            assert assessment.state is ReleaseState.RELEASED
        else:
            # The creation won the race. Nothing is sealed, so no release is reachable.
            assert stored_seal is None
            assert isinstance(seal, OperationRefused), seal
            assert "unaccounted for" in str(seal)
            observations = {"cluster-1": absent("cluster-1")}
            assessment = await sealer.assess_cleanup(
                executor_id=sealer_lease.holder,
                workspace_id=sealer_lease.workspace_id,
                allocation_id=allocation,
                operation_authority="authority-token",
                observations=observations,
            )
            assert assessment.state is not ReleaseState.RELEASED
            assert assessment.exposure is CostExposure.UNRESOLVED

        # Retire this round's leases so the next round is not refused for tenant
        # capacity. Membership and seals survive lease closure by design, which
        # `test_membership_survives_retirement_of_the_operation_that_created_it`
        # asserts -- so closing here does not disturb what was just checked.
        async with pool.acquire() as connection:
            for lease in (member_lease, sealer_lease, creator_lease):
                await close(
                    connection,
                    operation_id=lease.operation_id,
                    reason="round complete",
                    fence_token=lease.fence_token,
                    holder=lease.holder,
                )
    print(f"creation-race outcomes: {outcomes}")


async def test_a_seal_taken_during_dispatch_stops_the_provider_call(pool):
    """**F1, the window rule 1 alone cannot close.** Deterministic, not raced.

    `record_intent` refuses a creating call into a sealed allocation -- but the intent
    commits, its transaction ends, and the provider hook then runs with nothing holding
    the allocation. This forces a seal into exactly that window: the hook is held open
    on an event, the seal is attempted while it waits, and the second creating call is
    then attempted under the now-sealed allocation.

    The seal cannot succeed here, and that is rule 2 doing its work rather than a
    limitation of the test: the first call's intent is `intended`, so it is unaccounted
    for, so the allocation cannot be declared final while it is in flight. Asserted
    explicitly, because "the seal failed" and "the seal was refused for the right
    reason" are different facts.

    Then the call settles and its handle is enumerated, the allocation legitimately
    seals, and a FRESH creating call is refused before dispatch -- proving the
    pre-dispatch check refuses on a seal that appeared after an intent, not merely on
    one that predated it.
    """
    allocation = "alloc-dispatch-window"
    (
        (creator, creator_lease),
        (sealer, sealer_lease),
    ) = await _two_operations_on_one_allocation(pool, allocation)

    _record, _service, late_lease = await _creator_with_an_unstarted_plan(
        pool, allocation, key=f"{allocation}-late", holder="worker-late"
    )
    invoked = []
    gate = asyncio.Event()
    executor = OperationExecutor(
        late_lease,
        connect=pool.acquire,
        provider_call=spy_provider(invoked, gate=gate),
    )
    task = asyncio.create_task(
        executor.execute_provider(
            idempotency_key="in-flight-disk",
            provider="aws",
            operation_kind="create_disk",
            target=STEP["target"],
        )
    )
    try:
        # Wait until the hook is genuinely inside the window.
        for _ in range(300):
            if invoked:
                break
            await asyncio.sleep(0.01)
        assert invoked == ["in-flight-disk"], "the hook never entered the window"

        # Sealing now would certify an inventory that cannot include what this call is
        # creating. Refused -- and named.
        async with pool.acquire() as connection:
            with pytest.raises(OperationRefused) as refusal:
                await sealer.seal_allocation(connection, sealer_lease)
        assert "unaccounted for" in str(refusal.value)
        assert "in-flight-disk" in str(refusal.value)
    finally:
        gate.set()
        call, _disposition = await task

    # The call settled and produced a handle. Once it is enumerated, the allocation can
    # be sealed honestly.
    assert call.provider_ref == "disk-late-handle"
    members = (resource("cluster-1"), resource("disk-late", kind="storage"))
    async with pool.acquire() as connection:
        await creator.enumerate_resources(
            connection,
            creator_lease,
            resources=(resource("disk-late", kind="storage"),),
        )
    revision = await finish_allocation(pool, sealer, sealer_lease, members)
    assert revision

    # Now the window rule: a fresh creating call, on an allocation sealed AFTER this
    # executor's earlier intent, is refused before the provider is contacted.
    later = []
    later_executor = OperationExecutor(
        late_lease,
        connect=pool.acquire,
        provider_call=spy_provider(later, provider_ref="never-created"),
    )
    with pytest.raises(ProviderCallRefused) as refused:
        await later_executor.execute_provider(
            idempotency_key="disk-after-seal",
            provider="aws",
            operation_kind="create_disk",
            target=STEP["target"],
        )
    assert "sealed" in str(refused.value)
    assert later == [], "the provider was contacted for a call into a sealed allocation"


async def test_a_seal_appearing_inside_the_dispatch_window_stops_the_provider(pool):
    """The pre-dispatch check, exercised directly, because nothing else can reach it.

    `test_a_seal_taken_during_dispatch_stops_the_provider_call` shows rule 2 refusing a
    seal while a creating call is in flight. That is what makes the window between the
    intent commit and the hook unreachable by any legitimate sequence -- and therefore
    what makes this check defence in depth against rule 2 being bypassed by a restored
    backup, a repair script or a future writer.

    Untested defence in depth silently stops being defence: deleting
    `_refuse_sealed_before_dispatch` passes every other test in this suite, which is
    what motivated this test. So the check is called directly on a committed intent
    with a seal in place. Going through `execute_provider` cannot work and the reason is
    worth stating: `_record` would refuse first on its own seal check, so the test would
    pass while proving nothing about the second check -- a green assertion about the
    wrong mechanism, which is worse than no test.

    What is asserted is the whole contract of the method: it refuses, it names the seal,
    it audits, and it leaves the intent `intended` rather than resolving it as absent.
    """
    allocation = "alloc-window-seal"
    (
        (_member_service, _member_lease),
        (sealer, sealer_lease),
    ) = await _two_operations_on_one_allocation(pool, allocation)

    _record_row, _service, late_lease = await _creator_with_an_unstarted_plan(
        pool, allocation, key=f"{allocation}-late", holder="worker-late"
    )

    # The intent commits while the allocation is still open, which is legal and is the
    # state the window rule is about.
    async with pool.acquire() as connection:
        call = await record_intent(
            connection,
            late_lease,
            idempotency_key="window-disk",
            provider="aws",
            operation_kind="create_disk",
            target=STEP["target"],
        )

    # The seal that rule 2 refuses -- asserted, so this test also witnesses rule 2 --
    # inserted by hand at the revision `_completeness` would recompute.
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as rule_two:
            await sealer.seal_allocation(connection, sealer_lease)
        assert "window-disk" in str(rule_two.value)
        await connection.execute(
            """
            INSERT INTO harness_allocation_seal (
                org_id, workspace_id, allocation_id, sealed_revision,
                operation_id, attempt_id, executor_id, fence_token
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            sealer_lease.org_id,
            sealer_lease.workspace_id,
            allocation,
            _revision_of((resource("cluster-1"),)),
            sealer_lease.operation_id,
            sealer_lease.attempt_id,
            sealer_lease.holder,
            sealer_lease.fence_token,
        )

    invoked = []
    executor = OperationExecutor(
        late_lease, connect=pool.acquire, provider_call=spy_provider(invoked)
    )
    with pytest.raises(ProviderCallRefused) as refusal:
        await executor._refuse_sealed_before_dispatch(call)
    assert "sealed" in str(refusal.value)
    assert "window-disk" in str(refusal.value)
    assert invoked == []

    async with pool.acquire() as connection:
        # Left `intended`, NOT resolved as absent: the provider was never asked, and a
        # row claiming it answered is a lie a recovery pass would act on.
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE "
                "idempotency_key=$1",
                "window-disk",
            )
            == "intended"
        )
        # Audited, so an operator can discover that a creating call met a closed
        # allocation rather than having to infer it from a stuck intent.
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_execution_audit WHERE operation_id=$1 "
                "AND event='provider.sealed_allocation_refused' AND allowed = false",
                late_lease.operation_id,
            )
            == 1
        )


async def test_a_sealed_allocation_can_still_be_torn_down_and_inspected(pool):
    """**F1's necessary counterpart.** The fence withdraws creation, not cleanup.

    A seal that blocked every provider call would be worse than the defect it fixes: an
    allocation could be declared final and then never actually cleaned up, so its
    resources would bill forever -- the same loss, reached from the other direction.

    So teardown and query calls must still dispatch into a sealed allocation. Asserted
    through the real hook, which is what proves they reached the provider rather than
    merely that no exception was raised.
    """
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))

    for kind in ("delete_cluster", "describe_cluster"):
        invoked = []
        executor = OperationExecutor(
            lease,
            connect=pool.acquire,
            provider_call=spy_provider(invoked, provider_ref="cluster-1-handle"),
        )
        call, _disposition = await executor.execute_provider(
            idempotency_key=f"{kind}-call",
            provider="aws",
            operation_kind=kind,
            target=STEP["target"],
        )
        assert invoked == [f"{kind}-call"], (
            f"a {kind} call was blocked by the seal; a sealed allocation that "
            "cannot be torn down bills forever"
        )
        assert call.outcome is CallOutcome.SUCCEEDED

    # An unrecognized verb, by contrast, is refused: the fail-closed default.
    invoked = []
    executor = OperationExecutor(
        lease,
        connect=pool.acquire,
        provider_call=spy_provider(invoked),
    )
    with pytest.raises(ProviderCallRefused) as refusal:
        await executor.execute_provider(
            idempotency_key="mystery-call",
            provider="aws",
            operation_kind="frobnicate_cluster",
            target=STEP["target"],
        )
    assert "frobnicate_cluster" in str(refusal.value)
    assert invoked == []


async def test_the_recorded_call_carries_the_allocation_it_was_approved_for(pool):
    """The accounting check reads a column, so the column has to be written.

    `creating_calls_unaccounted_for` finds another operation's calls by
    `allocation_id`, and a call recorded without one is invisible to it -- which would
    silently reopen the whole defect while every test that looks at one operation still
    passed. So this asserts the stored row directly, and that the value came from the
    APPROVED plan rather than from anything the caller passed.
    """
    record, lease = await leased(pool)
    async with pool.acquire() as connection:
        await record_intent(
            connection,
            lease,
            idempotency_key="recorded-call",
            provider=STEP["provider"],
            operation_kind=STEP["operation_kind"],
            target=STEP["target"],
        )
        stored = await connection.fetchval(
            "SELECT allocation_id FROM harness_provider_call_intent WHERE "
            "idempotency_key=$1",
            "recorded-call",
        )
    assert stored == ALLOCATION
    # And it is the plan's value, not a worker's: the same one `allocation_id_for`
    # derives from the digest-bound request.
    assert stored == record.admitted_request().parameters["allocation_id"]


async def test_an_operation_naming_no_allocation_is_not_fenced(pool):
    """No allocation means no seal can govern the call, and it must not be blocked.

    Most operations are not allocation-bound. Such a call is outside every inventory --
    nothing can record it as membership and nothing can release budget against it -- so
    there is no seal whose closure should withdraw its authority. Blocking it would make
    this repair a general outage of provider dispatch rather than a fence on one
    allocation.
    """
    store = OperationStore()
    from harness_jobs.identity import OperationRequest

    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(
            store,
            connection,
            principal(),
            OperationRequest(
                action="provision",
                idempotency_key="no-allocation",
                parameters={"note": "nothing allocation-bound here"},
            ),
        )
        lease = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder="worker-1",
            attempt_id="attempt-1",
        )
    invoked = []
    executor = OperationExecutor(
        lease, connect=pool.acquire, provider_call=spy_provider(invoked)
    )
    call, _disposition = await executor.execute_provider(
        idempotency_key="unbound-call",
        provider="aws",
        operation_kind="create_cluster",
        target=STEP["target"],
    )
    assert invoked == ["unbound-call"]
    assert call.outcome is CallOutcome.SUCCEEDED
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT allocation_id FROM harness_provider_call_intent WHERE "
                "idempotency_key=$1",
                "unbound-call",
            )
            is None
        )


@pytest.mark.parametrize(
    ("kind", "effect"),
    [
        ("create_cluster", CallEffect.CREATES),
        ("ec2:RunInstances", CallEffect.CREATES),
        ("compute.instances.insert", CallEffect.CREATES),
        ("UPDATE_NODEGROUP", CallEffect.CREATES),
        ("scale_nodegroup", CallEffect.CREATES),
        ("delete_cluster", CallEffect.REMOVES),
        ("terminate-instances", CallEffect.REMOVES),
        # The case that caught a real defect: the verb is not the leading token, and
        # under a first-token rule this was UNRECOGNIZED and therefore FENCED -- which
        # would have left a sealed allocation impossible to tear down.
        ("ec2:TerminateInstances", CallEffect.REMOVES),
        ("compute.instances.delete", CallEffect.REMOVES),
        ("describe_cluster", CallEffect.OBSERVES),
        ("list_disks", CallEffect.OBSERVES),
        ("ec2:DescribeInstances", CallEffect.OBSERVES),
        # Mixed vocabularies resolve to CREATES, in the recoverable direction.
        ("delete_snapshot_copy", CallEffect.CREATES),
        ("frobnicate_cluster", CallEffect.UNRECOGNIZED),
        ("", CallEffect.UNRECOGNIZED),
        (None, CallEffect.UNRECOGNIZED),
        (17, CallEffect.UNRECOGNIZED),
    ],
)
def test_the_effect_of_a_call_is_read_from_its_approved_verb(kind, effect):
    """The classification the fence depends on, including its fail-closed default.

    `update` and `scale` are CREATES deliberately: membership of these sets is a claim
    about BILLING, not about REST semantics, and growing a nodegroup creates nodes that
    cost money.

    Several conventions appear because the same act must classify the same way whether a
    provider spells it `create_disk`, `ec2:RunInstances` or `compute.instances.insert`.
    The teardown forms of those conventions are the ones that matter most, and they are
    why this is not a leading-token match: `ec2:TerminateInstances` misclassified as
    UNRECOGNIZED would be refused into a sealed allocation, and an allocation that has
    been closed and can no longer be torn down bills forever -- the same loss this
    repair exists to prevent, reached from the other side.
    """
    assert (
        call_effect(
            kind,
            provider="gcp"
            if isinstance(kind, str) and kind.startswith("compute.")
            else "aws",
        )
        is effect
    )


@pytest.mark.parametrize(
    "kind",
    ["create_cluster", "frobnicate_cluster", "", None, "resize_disk"],
)
def test_anything_that_might_create_is_fenced(kind):
    """`may_create` answers yes for CREATES and for UNRECOGNIZED.

    The asymmetry is the point. An unrecognized verb wrongly treated as teardown costs a
    resource nobody knows about and a released budget; wrongly treated as creating costs
    a refusal with the verb in the message, which an operator can read and fix. The
    first is silent and expensive, the second is visible and recoverable.
    """
    assert may_create(kind, provider="aws") is True


@pytest.mark.parametrize("kind", ["delete_cluster", "describe_cluster", "list_disks"])
def test_teardown_and_query_calls_are_never_fenced(kind):
    assert may_create(kind, provider="aws") is False


# ---------------------------------------------------------------------------
# F3: a predecessor's enumeration cannot satisfy a successor's fence
# ---------------------------------------------------------------------------


async def test_a_successor_cannot_seal_or_release_on_a_predecessors_listing(pool):
    """**F3.** Recovery must ask the provider again; it does not inherit the answer.

    The exact sequence the reviewer named. The predecessor records a provider listing.
    The fence advances the way production advances it -- the lease lapses and recovery
    claims the operation. The successor now has everything except a listing of its own:
    membership is unchanged, the plan is complete, the listing row is right there and
    still says "the provider holds exactly these handles". Under a check that looked
    only at `provider` and `enumerated_digest`, that row satisfied the successor, so it
    could seal and be granted a release having never contacted the provider.

    That inverts the guarantee. A takeover happens precisely BECAUSE the previous holder
    stopped responding, which is also the situation where a resource whose creation was
    in flight may have appeared after the predecessor's listing was taken. The listing
    is evidence about a moment that ended when its author did.

    Asserted in three places, because the successor must be stopped at every point it
    could otherwise slip through, and then unblocked so the rule is fail-closed rather
    than fail-forever:

    1. it cannot seal;
    2. it cannot publish -- the allocation is not sealed, so there is no attestation;
    3. even given a seal (the predecessor's, taken before the fence moved) the
       inventory does not read as complete for the successor, so no release is
       authorized;
    4. after it records its OWN listing, all of it works.
    """
    record, lease = await leased(pool)
    predecessor = authority(pool, lease)
    members = (resource("cluster-1"),)
    async with pool.acquire() as connection:
        await predecessor.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await predecessor.record_provider_enumeration(
            connection,
            lease,
            attempt=await predecessor.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )

    successor = await fence_out(pool, lease)
    assert successor.fence_token > lease.fence_token
    service = authority(pool, successor)

    # The predecessor's listing is still on record, and still describes the handles
    # membership holds now -- so what refuses the successor is the binding, not
    # staleness of the content.
    async with pool.acquire() as connection:
        listing = await connection.fetchrow(
            "SELECT executor_id, attempt_id, fence_token FROM "
            "harness_allocation_enumeration WHERE allocation_id=$1",
            ALLOCATION,
        )
    assert listing is not None
    assert listing["fence_token"] == lease.fence_token

    # (1) It cannot seal on somebody else's listing.
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await service.seal_allocation(connection, successor)
    assert "no current provider enumeration" in str(refusal.value)

    # (2) With nothing sealed there is nothing to attest, so no report exists either.
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused):
            await _publish(service, connection, successor, observations=observations)

    # (3) And giving it a seal changes nothing, which is the part worth isolating. The
    # predecessor's seal is written directly, because the predecessor can no longer seal
    # through the public method -- so the successor now lacks exactly ONE thing, a
    # listing of its own. The refusal must therefore name the listing rather than the
    # seal: if publication were still gated only on sealing, this is the step that would
    # pass.
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO harness_allocation_seal (org_id, workspace_id, "
            "allocation_id, sealed_revision, operation_id, attempt_id, executor_id, "
            "fence_token) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            _revision_of(members),
            lease.operation_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
        )
        with pytest.raises(OperationRefused) as refusal:
            await _publish(service, connection, successor, observations=observations)
    assert "no current provider enumeration" in str(refusal.value)
    # Nothing was stored, so there is no attestation a later listing could make
    # retroactively valid -- the case
    # `test_a_later_listing_cannot_validate_an_earlier_absent_report` covers on the
    # read side.
    assessment = await service.assess_cleanup(
        executor_id=successor.holder,
        workspace_id=successor.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert assessment.state is not ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.UNRESOLVED

    # (4) The successor asks the provider itself, then publishes what it saw. Now it is
    # complete and releasable -- fail-closed, not fail-forever. The publication has to
    # come after the listing, which is the whole ordering rule rather than an artefact
    # of this test: evidence is gathered, then attested.
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            successor,
            attempt=await service.begin_provider_enumeration(
                connection, successor, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        await _publish(service, connection, successor, observations=observations)
    granted = await service.assess_cleanup(
        executor_id=successor.holder,
        workspace_id=successor.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert granted.state is ReleaseState.RELEASED
    assert granted.exposure is CostExposure.NONE


async def test_a_listing_recorded_by_another_attempt_of_the_same_holder_is_not_current(
    pool,
):
    """F3. The fence alone is not the binding; the attempt is part of it too.

    The narrow variant that a fence-only check would miss. Recovery can hand the
    operation back to the SAME worker under a new attempt -- a pod restart that
    re-registers, say -- so the holder is unchanged and only `attempt_id` and the fence
    have moved. The listing must still not carry over: the reason it does not carry
    over is that its author's session ended, and "same worker, new attempt" is a new
    session.

    Asserted on the refusal at the seal, which is where the successor first needs the
    proof.
    """
    record, lease = await leased(pool, holder="worker-1", attempt="attempt-1")
    service = authority(pool, lease)
    members = (resource("cluster-1"),)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )

    successor = await fence_out(pool, lease)
    # Recovery's takeover keeps the tenant and the operation; what it cannot keep is
    # the attempt that recorded the evidence.
    assert successor.attempt_id != lease.attempt_id
    async with pool.acquire() as connection:
        with pytest.raises(OperationRefused) as refusal:
            await authority(pool, successor).seal_allocation(connection, successor)
    assert "no current provider enumeration" in str(refusal.value)


# ---------------------------------------------------------------------------
# F5: a later provider listing cannot retroactively validate an earlier report
# ---------------------------------------------------------------------------


async def test_a_report_cannot_be_published_before_its_provider_listing(pool):
    """**F5.** The order is gathered-then-attested, and it is enforced here.

    Sealing used to stand in for this and could not finish the job: a seal requires a
    listing, so a listing always existed by publication time -- but only somebody's, at
    some point, and nothing tied the report to the one that was current when it was
    published. The successor case is where that gap opens, because recovery advances the
    fence and the successor starts with no listing of its own while the predecessor's
    seal remains in force.

    Asserted on the refusal AND on the absence of a row, because the row is what a later
    listing could otherwise rescue.
    """
    record, lease = await leased(pool)
    members = (resource("cluster-1"),)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        await service.seal_allocation(connection, lease)

    # A second member appears in the ledger the way a repair script or a future writer
    # would put it there -- so its provider has no listing at all, while the allocation
    # is sealed and the other provider's listing is perfectly current.
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO harness_allocation_resource (org_id, workspace_id, "
            "allocation_id, resource_id, operation_id, provider, provider_reference, "
            "kind, attempt_id, fence_token) "
            "VALUES ($1,$2,$3,'bucket-1',$4,'gcp','bucket-1-handle','storage',$5,$6)",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
            lease.operation_id,
            lease.attempt_id,
            lease.fence_token,
        )
        with pytest.raises(OperationRefused) as refusal:
            await _publish(
                service,
                connection,
                lease,
                observations={
                    "cluster-1": absent("cluster-1"),
                    "bucket-1": absent("bucket-1"),
                },
            )
        assert "gcp" in str(refusal.value)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
                lease.operation_id,
            )
            == 0
        )


async def test_a_later_listing_cannot_validate_an_earlier_absent_report(pool):
    """**F5, the expensive case.** The reviewer's exact sequence, and its new outcome.

    A report was publishable against a seal alone, and the listing that was supposed to
    make it fresh stayed replaceable afterwards. So this order had no check that could
    see it:

        publish "the cluster is absent"     -- truthful about what the executor saw
        record the required listing          -- and the provider says it is PRESENT
        read                                 -- and release the budget

    The later listing did not merely fail to invalidate the report. It was what made the
    report VALID, because it satisfied the completeness check for the very read that
    honoured the contradicting attestation. Fresh evidence rescued an older report that
    said the opposite of it, which is the exact inversion of what a freshness proof is
    for -- and the result is a released reservation over a running cluster.

    The listing here is truthful and legitimate: the provider does hold the handle, and
    membership names it, so `record_provider_enumeration` accepts it. That is what
    makes this the dangerous shape rather than a malformed input -- every individual act
    in the sequence is correct, and only their ORDER is wrong.

    Asserted in three places: the earlier report stops verifying once the listing is
    replaced, no release is authorized through it, and the only way forward is a NEW
    report -- which, being about a handle the provider has just listed as present,
    cannot say the cluster is gone. So evidence supersedes evidence rather than
    validating it, and the path is not permanently closed either.
    """
    record, lease = await leased(pool)
    members = (resource("cluster-1"),)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        await service.seal_allocation(connection, lease)

    # The executor queries the provider about the cluster and publishes what it got.
    absent_report = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=absent_report)
    # It verifies right now, which is what makes the next step a change rather than a
    # test of something that never worked.
    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is not None
    )

    # Then it asks the provider to list what it holds -- and the provider still holds
    # the cluster. Legitimate, truthful, and accepted.
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        generations = await connection.fetch(
            "SELECT generation FROM harness_allocation_enumeration "
            "WHERE allocation_id=$1",
            ALLOCATION,
        )
    # Re-asking is a new question even when the answer is identical, so the generation
    # moved -- which is what the report's binding is compared against.
    assert [row["generation"] for row in generations] == [2]

    # (1) The earlier report no longer verifies. Its bytes, its grant and its seal are
    # all unchanged; what changed is that the provider has been asked again since.
    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    ), "a later provider listing revalidated a report published before it"

    # (2) So no release is authorized through it, and the exposure is retained rather
    # than reported as zero.
    assessment = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=absent_report,
    )
    assert assessment.state is not ReleaseState.RELEASED
    assert assessment.exposure is CostExposure.UNRESOLVED

    # (3) The way forward is a new report -- and the new report is the executor's
    # statement about NOW, taken after the listing rather than before it. That is the
    # whole of what the ordering buys, and it is worth being precise about the limit: a
    # report may still say ABSENT after a listing that named the handle, because tearing
    # the cluster down BETWEEN the listing and the query is the ordinary teardown
    # sequence and refusing it would make cleanup unrecordable
    # The stale-receipt regression covers this ordering. What is no longer
    # possible is the inverted order: an observation made before the provider was last
    # asked can never be the evidence a release is granted on.
    #
    # Here the executor re-queries honestly and the cluster is still running, so
    # presence is what it publishes and the budget stays retained.
    present_report = {"cluster-1": present("cluster-1")}
    async with pool.acquire() as connection:
        await _publish(service, connection, lease, observations=present_report)
    retained = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=present_report,
    )
    assert retained.state is ReleaseState.RETAINED
    assert retained.exposure is CostExposure.ACTIVE
    assert retained.may_return_reservation_unused is False


async def test_a_stale_receipt_cannot_be_republished_after_a_new_listing(pool):
    """A newer listing cannot revive an earlier ABSENT query receipt."""
    record, lease = await leased(pool)
    members = (resource("cluster-1"),)
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        await service.seal_allocation(connection, lease)
        await _publish(service, connection, lease, observations=observations)
        # The provider is asked again -- so the report above is now stale evidence.
        await service.record_provider_enumeration(
            connection,
            lease,
            attempt=await service.begin_provider_enumeration(
                connection, lease, provider="aws"
            ),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        with pytest.raises(OperationRefused, match="predates|superseded"):
            await _publish(service, connection, lease, observations=observations)
    granted = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert granted.state is not ReleaseState.RELEASED
    assert granted.exposure is CostExposure.UNRESOLVED


async def test_concurrent_publications_of_one_report_write_a_single_row(pool):
    """Two workers publishing identical observations converge, not conflict.

    Both publications hold the allocation lock in turn now, so this also asserts the
    lock does not turn a convergent retry into a deadlock or a spurious refusal -- the
    loser waits, then reads the winner's committed row and recognizes it as its own.
    """
    _record, service, lease, _revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}

    async def query(_lease, _members, _nonce):
        return observations

    service.query_provider = query
    async with pool.acquire() as c:
        receipt = await service.observe_report(c, lease)

    async def publish():
        async with pool.acquire() as connection:
            return await service.publish_report(connection, lease, observations=receipt)

    first, second = await asyncio.gather(publish(), publish())
    assert first == second
    async with pool.acquire() as connection:
        rows = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE report_digest=$1", first
        )
    assert rows == 1


async def test_concurrent_queries_for_distinct_allocations_both_persist(pool):
    """Identical provider state under unrelated allocations remains independent."""
    pairs = []
    for index in (1, 2):
        record, lease = await leased(
            pool,
            key=f"query-key-{index}",
            holder=f"worker-{index}",
            allocation=f"alloc-{index}",
        )
        service = authority(pool, lease)
        async with pool.acquire() as c:
            await service.enumerate_resources(
                c, lease, resources=(resource("cluster-1"),)
            )
        await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
        await finish_allocation(pool, service, lease, (resource("cluster-1"),))
        pairs.append((service, lease))

    async def publish(pair):
        service, lease = pair
        async with pool.acquire() as c:
            return await _publish(
                service, c, lease, observations={"cluster-1": absent("cluster-1")}
            )

    digests = await asyncio.gather(*(publish(pair) for pair in pairs))
    assert digests[0] != digests[1]
    async with pool.acquire() as c:
        stored = await c.fetch(
            "SELECT operation_id, executor_id FROM harness_provider_report "
            "WHERE report_digest=ANY($1)",
            digests,
        )
    assert len(stored) == 2
    assert {row["executor_id"] for row in stored} == {"worker-1", "worker-2"}


# ---------------------------------------------------------------------------
# Composition and pure rules
# ---------------------------------------------------------------------------


def test_an_authority_without_a_verifier_is_refused():
    """No default verifier: whether authority is checked is a property of the type."""
    with pytest.raises(ContractViolation):
        InventoryAuthority(connect=lambda: None, authenticate=None)


def test_a_present_observation_without_a_provider_state_is_refused():
    with pytest.raises(ContractViolation):
        ResourceObservation(presence=ResourcePresence.PRESENT, queried_by="disk-1")


def test_an_unknown_observation_must_say_why():
    with pytest.raises(ContractViolation):
        ResourceObservation(
            presence=ResourcePresence.UNKNOWN, queried_by="disk-1", detail="  "
        )


def test_an_unknown_observation_cannot_carry_a_provider_state():
    with pytest.raises(ContractViolation):
        ResourceObservation(
            presence=ResourcePresence.UNKNOWN,
            queried_by="disk-1",
            provider_state="RUNNING",
            detail="timed out",
        )


def test_the_revision_is_content_derived_not_a_clock():
    """Same members, same revision; any identity change moves it."""
    first = (resource("a"), resource("b"))
    from harness_jobs.inventory import _revision

    assert _revision(first) == _revision(tuple(reversed(first)))
    assert _revision(first) != _revision((resource("a"), resource("b", kind="storage")))


def test_the_digest_is_stable_across_mapping_order():
    """Two processes reporting the same observations must produce the same digest."""
    forward = {"a": absent("a"), "b": absent("b")}
    backward = {"b": absent("b"), "a": absent("a")}
    assert report_digest(forward) == report_digest(backward)


def test_a_report_beyond_the_ceiling_is_refused():
    too_many = {
        f"r-{index}": absent(f"r-{index}")
        for index in range(MAX_INVENTORY_RESOURCES + 1)
    }
    with pytest.raises(ContractViolation):
        report_digest(too_many)


def test_a_resource_reference_must_be_bounded_and_non_blank():
    with pytest.raises(ContractViolation):
        AllocationResource(
            resource_id="a", provider="aws", provider_reference="  ", kind="compute"
        )
    with pytest.raises(ContractViolation):
        AllocationResource(
            resource_id="a",
            provider="aws",
            provider_reference="x" * 256,
            kind="compute",
        )


async def test_an_operation_with_no_approved_allocation_is_refused(pool):
    """The allocation id comes from the plan; a request without one attests nothing."""
    from harness_jobs.identity import OperationRequest

    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await admit_paid(
            store,
            connection,
            principal(),
            OperationRequest(action="provision", idempotency_key="no-alloc"),
        )
        lease = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder="worker-1",
            attempt_id="attempt-1",
        )
    service = authority(pool, lease)
    async with pool.acquire() as connection:
        with pytest.raises(ContractViolation):
            await _publish(
                service,
                connection,
                lease,
                observations={"cluster-1": absent("cluster-1")},
            )


async def retire_the_operation(pool, lease):
    """Remove the operation row, the way operation housekeeping eventually does.

    `harness_approval_consumption` holds it with `ON DELETE RESTRICT` deliberately --
    a cascade there would turn a row deletion into a budget grant -- so retiring an
    operation means clearing that record first. Everything else keyed to the operation
    is either cascaded or, for the rows this story owns, must survive; which of those
    two it is, is exactly what the caller is asserting.
    """
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM harness_approval_consumption WHERE operation_id=$1",
            lease.operation_id,
        )
        await connection.execute(
            "DELETE FROM harness_operations WHERE operation_id=$1", lease.operation_id
        )


async def test_membership_survives_retirement_of_the_operation_that_created_it(pool):
    """F3. Membership is keyed to the ALLOCATION, and must outlive the operation row.

    `harness_allocation_resource.operation_id` carried `REFERENCES harness_operations
    ON DELETE CASCADE`, which contradicted the one property the table exists for: it
    only grows. Retiring an operation -- ordinary housekeeping, and not an event this
    package is consulted about -- took its resource rows with it. The resources were
    still running; the inventory read as a whole smaller allocation, or as an empty
    one, and a release was authorized over resources nothing in the system could still
    name.

    The operation is provenance now, so "which operation established this member?"
    stays answerable after the operation row is gone -- which is precisely when an
    operator is asking.
    """
    members = (resource("cluster-1"), resource("disk-1", kind="storage"))
    _record, service, lease, _revision = await sealed(
        pool, members, provider_ref="cluster-1-handle"
    )
    async with pool.acquire() as connection:
        await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )

    await retire_the_operation(pool, lease)

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT resource_id, operation_id, provider_reference FROM "
            "harness_allocation_resource WHERE org_id=$1 AND workspace_id=$2 AND "
            "allocation_id=$3 ORDER BY resource_id",
            lease.org_id,
            lease.workspace_id,
            ALLOCATION,
        )
    assert [row["resource_id"] for row in rows] == ["cluster-1", "disk-1"]
    # The handle a teardown would present to the provider is still there, and so is the
    # provenance -- the value, not a parent row.
    assert [row["provider_reference"] for row in rows] == [
        "cluster-1-handle",
        "disk-1-handle",
    ]
    assert {row["operation_id"] for row in rows} == {lease.operation_id}


async def test_the_attestation_survives_retirement_of_its_operation(pool):
    """F3. The report is evidence about resources that may still be billing.

    Evidence that disappears when an operation row is retired is evidence that was not
    durable. The failure direction is safe -- a later read finds no attestation and
    retains exposure -- but it means routine housekeeping silently disables cleanup, and
    it destroys the record of what the provider actually said during a release dispute.
    """
    _record, service, lease, revision = await sealed(pool, (resource("cluster-1"),))
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)

    await retire_the_operation(pool, lease)

    async with pool.acquire() as connection:
        stored = await connection.fetchrow(
            "SELECT operation_id, allocation_id, observations, sealed_revision FROM "
            "harness_provider_report WHERE report_digest=$1",
            digest,
        )
    assert stored is not None
    assert stored["operation_id"] == lease.operation_id
    assert stored["allocation_id"] == ALLOCATION
    # Not merely the digest: the payload that was attested is still readable.
    assert "cluster-1" in stored["observations"]
    # And so is the revision it was taken against, which is what makes the surviving
    # row still interpretable -- an attestation whose ordering evidence was lost would
    # be an observation nobody can place in time.
    assert stored["sealed_revision"] == revision


async def test_membership_and_seal_survive_a_superseded_operations_removal(pool):
    """F3. The completeness proofs must be as durable as the membership they certify.

    A seal or provider listing that vanished with the operation row would silently
    move a complete allocation back to incomplete, which is a release that can never
    happen for an allocation whose resources really are gone.
    """
    members = (resource("cluster-1"),)
    _record, service, lease, revision = await sealed(pool, members)
    async with pool.acquire() as connection:
        await _publish(
            service, connection, lease, observations={"cluster-1": absent("cluster-1")}
        )

    await retire_the_operation(pool, lease)

    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 "
                "AND workspace_id=$2 AND allocation_id=$3",
                lease.org_id,
                lease.workspace_id,
                ALLOCATION,
            )
            == revision
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_enumeration WHERE "
                "org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                lease.org_id,
                lease.workspace_id,
                ALLOCATION,
            )
            == 1
        )


async def test_an_expired_lease_is_not_current_authority(pool):
    """`expires_at` is the authority window, and a lapsed one grants nothing.

    The window is short and the whole valid sequence happens inside it, so what the
    read is refusing is the lapse alone: the allocation is sealed, the attestation is
    this grant's own, and the inventory was complete a moment earlier.
    """
    record, lease = await leased(pool, duration=timedelta(seconds=3))
    service = authority(pool, lease)
    members = (resource("cluster-1"),)
    observations = {"cluster-1": absent("cluster-1")}
    async with pool.acquire() as connection:
        await service.enumerate_resources(connection, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    await finish_allocation(pool, service, lease, members)
    async with pool.acquire() as connection:
        digest = await _publish(service, connection, lease, observations=observations)
    assert await service.read_inventory(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        report_digest=digest,
    )
    await asyncio.sleep(3.2)

    assert (
        await service.read_inventory(
            executor_id=lease.holder,
            workspace_id=lease.workspace_id,
            allocation_id=ALLOCATION,
            operation_authority="authority-token",
            report_digest=digest,
        )
        is None
    )


@pytest.mark.parametrize("reference", ["cluster-1-handle", None])
async def test_later_creation_invalidates_listing_with_reused_or_null_handle(
    pool, reference
):
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as c:
        await service.enumerate_resources(c, lease, resources=(resource("cluster-1"),))
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as c:
        await service.record_provider_enumeration(
            c,
            lease,
            attempt=await service.begin_provider_enumeration(c, lease, provider="aws"),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
    later, later_lease = await leased(pool, key="later", holder="later-worker")
    invoked = []

    async def provider(call):
        invoked.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, None, reference

    executor = OperationExecutor(
        later_lease, connect=pool.acquire, provider_call=provider
    )
    from harness_jobs.execution_plan import ExecutionStep

    settled, _ = await executor.execute_provider(
        idempotency_key=step_key(later, ExecutionStep(**STEP)),
        **{k: STEP[k] for k in ("provider", "operation_kind", "target")},
    )
    assert invoked
    assert settled.outcome is CallOutcome.SUCCEEDED
    async with pool.acquire() as c:
        with pytest.raises(OperationRefused):
            await service.seal_allocation(c, lease)


async def test_contradictory_listing_revokes_old_absence_and_recovers(pool):
    observations = {"cluster-1": absent("cluster-1")}
    service, lease = await published(pool, observations, (resource("cluster-1"),))
    async with pool.acquire() as c:
        with pytest.raises(OperationRefused):
            await service.record_provider_enumeration(
                c,
                lease,
                attempt=await service.begin_provider_enumeration(
                    c, lease, provider="aws"
                ),
                provider="aws",
                provider_references=frozenset({"cluster-1-handle", "disk-late-handle"}),
            )
    result = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert not result.may_mark_released
    assert not result.may_return_reservation_unused
    async with pool.acquire() as c:
        await service.enumerate_resources(
            c, lease, resources=(resource("disk-late", kind="storage"),)
        )
    await finish_allocation(
        pool,
        service,
        lease,
        (resource("cluster-1"), resource("disk-late", kind="storage")),
    )
    observations["disk-late"] = absent("disk-late")
    async with pool.acquire() as c:
        await _publish(service, c, lease, observations=observations)
    result = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=observations,
    )
    assert result.may_mark_released


async def test_provider_query_racing_a_creating_call_cannot_publish_old_listing(pool):
    record, lease = await leased(pool)
    service = authority(pool, lease)
    async with pool.acquire() as c:
        await service.enumerate_resources(c, lease, resources=(resource("cluster-1"),))
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as c:
        cutoff = await service.begin_provider_enumeration(c, lease, provider="aws")
    # The provider listing started, then another operation creates. Even a
    # byte-identical parent handle must not let that old answer establish freshness.
    later, other = await leased(pool, key="query-race", holder="other")
    await complete_the_plan(pool, later, other, provider_ref="cluster-1-handle")
    async with pool.acquire() as c:
        with pytest.raises(OperationRefused, match="activity changed"):
            await service.record_provider_enumeration(
                c,
                lease,
                provider="aws",
                attempt=cutoff,
                provider_references=frozenset({"cluster-1-handle"}),
            )


async def test_quarantine_cannot_forget_a_previously_discovered_resource(pool):
    observations = {"cluster-1": absent("cluster-1")}
    service, lease = await published(pool, observations, (resource("cluster-1"),))
    async with pool.acquire() as c:
        with pytest.raises(OperationRefused):
            await service.record_provider_enumeration(
                c,
                lease,
                provider="aws",
                attempt=await service.begin_provider_enumeration(
                    c, lease, provider="aws"
                ),
                provider_references=frozenset({"disk-late-handle"}),
            )
        await service.record_provider_enumeration(
            c,
            lease,
            provider="aws",
            attempt=await service.begin_provider_enumeration(c, lease, provider="aws"),
            provider_references=frozenset(),
        )
        with pytest.raises(OperationRefused, match="discovered provider resources"):
            await service.seal_allocation(c, lease)
        # Caller transactions could undo a committed refusal. Reject ownership
        # we cannot commit independently before accepting any provider evidence.
        async with c.transaction():
            with pytest.raises(ContractViolation, match="owned transaction"):
                await service.record_provider_enumeration(
                    c,
                    lease,
                    provider="aws",
                    attempt=0,
                    provider_references=frozenset(),
                )


async def test_only_a_fresh_query_can_replace_old_absence_after_relisting(pool):
    """Exercise query, publication and cleanup without the fixture publisher."""
    _, service, lease, _ = await sealed(pool, (resource("cluster-1"),))
    calls = []

    async def query(current, members, query_id):
        assert current == lease
        assert members[0].provider_reference == "cluster-1-handle"
        calls.append("queried")
        return {"cluster-1": absent("cluster-1")}

    service.query_provider = query
    async with pool.acquire() as c:
        old = await service.observe_report(c, lease)
        old_digest = await service.publish_report(c, lease, observations=old)
        await service.record_provider_enumeration(
            c,
            lease,
            attempt=await service.begin_provider_enumeration(c, lease, provider="aws"),
            provider="aws",
            provider_references=frozenset({"cluster-1-handle"}),
        )
        with pytest.raises(OperationRefused, match="predates|superseded"):
            await service.publish_report(c, lease, observations=old)
        with pytest.raises(ContractViolation, match="query receipt"):
            await service.publish_report(c, lease, observations=dict(old))
        fresh = await service.observe_report(c, lease)
        fresh_digest = await service.publish_report(c, lease, observations=fresh)
        assert (
            await service.publish_report(c, lease, observations=fresh) == fresh_digest
        )
    assert calls == ["queried", "queried"]
    assert old_digest != fresh_digest
    args = dict(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
    )
    assert not (
        await service.assess_cleanup(**args, observations=old)
    ).may_mark_released
    assert (await service.assess_cleanup(**args, observations=fresh)).may_mark_released


async def test_query_through_executor_preserves_listing_and_publishes_fresh_absence(
    pool,
):
    _, service, lease, _ = await sealed(pool, (resource("cluster-1"),))
    invoked = []
    executor = OperationExecutor(
        lease,
        connect=pool.acquire,
        provider_call=spy_provider(invoked, provider_ref="cluster-1-handle"),
    )

    async def query(current, members, query_id):
        call, _ = await executor.execute_provider(
            idempotency_key="post-listing-" + query_id,
            provider="aws",
            operation_kind="eks:DescribeCluster",
            target=members[0].provider_reference,
        )
        assert call.outcome is CallOutcome.SUCCEEDED
        return {"cluster-1": absent("cluster-1")}

    service.query_provider = query
    async with pool.acquire() as c:
        before = await service._epoch(c, lease, ALLOCATION)
        receipt = await service.observe_report(c, lease)
        after = await service._epoch(c, lease, ALLOCATION)
        assert before == after
        await service.publish_report(c, lease, observations=receipt)
    assert len(invoked) == 1 and invoked[0].startswith("post-listing-")
    result = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=receipt,
    )
    assert result.may_mark_released


@pytest.mark.parametrize(
    "kind",
    [
        "DescribeCluster",
        "get_snapshot",
        "ec2:GetSnapshot",
        "GET_INSTANCE",
        "verify_network",
        "list_instances",
        "compute.instances.insert",
        "get_or_create",
        "delete_snapshot_copy",
        "read_then_remove",
        "mystery",
        "status",
    ],
)
@pytest.mark.parametrize("provider", ["aws", "other", "gcp"])
async def test_database_epoch_and_approved_effect_classification_agree(
    pool, kind, provider
):
    _, lease = await leased(pool)
    async with pool.acquire() as c:
        await record_intent(
            c,
            lease,
            idempotency_key="effect-test",
            provider=provider,
            operation_kind=kind,
            target="account/111122223333",
        )
        generation = await c.fetchval(
            "SELECT generation FROM harness_allocation_epoch WHERE allocation_id=$1",
            ALLOCATION,
        )
    assert bool(generation) == (
        call_effect(kind, provider=provider) is not CallEffect.OBSERVES
    )


async def test_relisting_during_provider_query_invalidates_the_receipt(pool):
    _, service, lease, _ = await sealed(pool, (resource("cluster-1"),))

    async def query(_lease, _members, _query_id):
        async with pool.acquire() as other:
            await service.record_provider_enumeration(
                other,
                lease,
                attempt=await service.begin_provider_enumeration(
                    other, lease, provider="aws"
                ),
                provider="aws",
                provider_references=frozenset({"cluster-1-handle"}),
            )
        return {"cluster-1": absent("cluster-1")}

    service.query_provider = query
    async with pool.acquire() as c:
        receipt = await service.observe_report(c, lease)
        with pytest.raises(OperationRefused, match="predates|superseded"):
            await service.publish_report(c, lease, observations=receipt)


@pytest.mark.parametrize("query_fails", [False, True])
async def test_a_new_query_revokes_older_absence_even_without_relisting(
    pool, query_fails
):
    _, service, lease, _ = await sealed(pool, (resource("cluster-1"),))

    async def query(_lease, _resources, _nonce):
        return {"cluster-1": absent("cluster-1")}

    service.query_provider = query
    async with pool.acquire() as c:
        old = await service.observe_report(c, lease)
        await service.publish_report(c, lease, observations=old)

        async def newer(_lease, _resources, _nonce):
            if query_fails:
                raise TimeoutError("synthetic provider failure")
            return {"cluster-1": present("cluster-1")}

        service.query_provider = newer
        if query_fails:
            with pytest.raises(TimeoutError):
                await service.observe_report(c, lease)
        else:
            current = await service.observe_report(c, lease)
            await service.publish_report(c, lease, observations=current)
        with pytest.raises(OperationRefused, match="superseded"):
            await service.publish_report(c, lease, observations=old)
    result = await service.assess_cleanup(
        executor_id=lease.holder,
        workspace_id=lease.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=old,
    )
    assert not result.may_mark_released


@pytest.mark.parametrize("query_fails", [False, True])
async def test_new_query_from_another_operation_revokes_old_absence(pool, query_fails):
    members = (resource("cluster-1"),)
    (
        (first_service, first),
        (second_service, second),
    ) = await _two_operations_on_one_allocation(pool, ALLOCATION)
    await finish_allocation(pool, first_service, first, members)
    await finish_allocation(pool, second_service, second, members)

    async def absent_query(_lease, _members, _nonce):
        return {"cluster-1": absent("cluster-1")}

    first_service.query_provider = absent_query
    async with pool.acquire() as c:
        old = await first_service.observe_report(c, first)
        await first_service.publish_report(c, first, observations=old)
    args = dict(
        executor_id=first.holder,
        workspace_id=first.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=old,
    )
    assert (await first_service.assess_cleanup(**args)).may_mark_released

    async def current_query(_lease, _members, _nonce):
        if query_fails:
            raise TimeoutError("synthetic query failure")
        return {"cluster-1": present("cluster-1")}

    second_service.query_provider = current_query
    async with pool.acquire() as c:
        if query_fails:
            with pytest.raises(TimeoutError):
                await second_service.observe_report(c, second)
        else:
            current = await second_service.observe_report(c, second)
            await second_service.publish_report(c, second, observations=current)
        with pytest.raises(OperationRefused, match="superseded"):
            await first_service.publish_report(c, first, observations=old)
    invalidated = await first_service.assess_cleanup(**args)
    assert invalidated.state is ReleaseState.UNRESOLVED
    assert invalidated.exposure is CostExposure.UNRESOLVED
    assert not invalidated.may_return_reservation_unused


async def test_competing_operations_can_only_publish_latest_allocation_query(pool):
    members = (resource("cluster-1"),)
    ((a, first), (b, second)) = await _two_operations_on_one_allocation(
        pool, ALLOCATION
    )
    await finish_allocation(pool, a, first, members)
    await finish_allocation(pool, b, second, members)

    async def query(_lease, _members, _nonce):
        return {"cluster-1": absent("cluster-1")}

    a.query_provider = b.query_provider = query
    async with pool.acquire() as c:
        old = await a.observe_report(c, first)
        latest = await b.observe_report(c, second)

    async def publish(service, lease, receipt):
        async with pool.acquire() as c:
            return await service.publish_report(c, lease, observations=receipt)

    results = await asyncio.gather(
        publish(a, first, old), publish(b, second, latest), return_exceptions=True
    )
    assert isinstance(results[0], OperationRefused)
    assert isinstance(results[1], str)


@pytest.mark.parametrize("late_child", [False, True])
async def test_inflight_listing_blocks_every_intermediate_release(pool, late_child):
    members = (resource("cluster-1"),)
    ((a, first), (b, second)) = await _two_operations_on_one_allocation(
        pool, ALLOCATION
    )
    await finish_allocation(pool, a, first, members)
    await finish_allocation(pool, b, second, members)

    async def query(_lease, _members, _nonce):
        return {"cluster-1": absent("cluster-1")}

    a.query_provider = query
    async with pool.acquire() as c:
        receipt = await a.observe_report(c, first)
        await a.publish_report(c, first, observations=receipt)
    args = dict(
        executor_id=first.holder,
        workspace_id=first.workspace_id,
        allocation_id=ALLOCATION,
        operation_authority="authority-token",
        observations=receipt,
    )
    assert (await a.assess_cleanup(**args)).may_mark_released
    async with pool.acquire() as c:
        attempt = await b.begin_provider_enumeration(c, second, provider="aws")
        with pytest.raises(OperationRefused, match="enumeration"):
            await a.observe_report(c, first)
        with pytest.raises(OperationRefused, match="enumeration"):
            await a.publish_report(c, first, observations=receipt)
        with pytest.raises(OperationRefused):
            await a.seal_allocation(c, first)
        with pytest.raises(OperationRefused, match="in progress"):
            await a.begin_provider_enumeration(c, first, provider="aws")
    assert not (await a.assess_cleanup(**args)).may_mark_released
    async with pool.acquire() as c:
        if late_child:
            with pytest.raises(OperationRefused, match="does not enumerate"):
                await b.record_provider_enumeration(
                    c,
                    second,
                    attempt=attempt,
                    provider="aws",
                    provider_references=frozenset(
                        {"cluster-1-handle", "late-disk-handle"}
                    ),
                )
        else:
            await b.record_provider_enumeration(
                c,
                second,
                attempt=attempt,
                provider="aws",
                provider_references=frozenset({"cluster-1-handle"}),
            )
    assert not (await a.assess_cleanup(**args)).may_mark_released
    async with pool.acquire() as c:
        if late_child:
            with pytest.raises(OperationRefused):
                await a.observe_report(c, first)
        else:
            fresh = await a.observe_report(c, first)
            await a.publish_report(c, first, observations=fresh)
    if not late_child:
        assert (
            await a.assess_cleanup(**{**args, "observations": fresh})
        ).may_mark_released


async def test_failed_listing_blocks_reports_until_a_fresh_exact_attempt_completes(
    pool,
):
    _, service, lease, _ = await sealed(pool, (resource("cluster-1"),))

    async def query(*args):
        pytest.fail("report queries must not run with an unresolved listing")

    service.query_provider = query
    async with pool.acquire() as c:
        old = await service.begin_provider_enumeration(c, lease, provider="aws")
        await service.fail_provider_enumeration(c, lease, attempt=old)
        with pytest.raises(OperationRefused, match="enumeration"):
            await service.observe_report(c, lease)
        new = await service.begin_provider_enumeration(c, lease, provider="aws")
        assert new.query_id != old.query_id
        assert new.generation == old.generation
        with pytest.raises(OperationRefused, match="token"):
            await service.record_provider_enumeration(
                c,
                lease,
                provider="aws",
                attempt=old,
                provider_references=frozenset({"cluster-1-handle"}),
            )
        with pytest.raises(ContractViolation, match="begin token"):
            await service.record_provider_enumeration(
                c, lease, provider="other", attempt=new, provider_references=frozenset()
            )
        await service.record_provider_enumeration(
            c,
            lease,
            provider="aws",
            attempt=new,
            provider_references=frozenset({"cluster-1-handle"}),
        )
        with pytest.raises(OperationRefused, match="token"):
            await service.record_provider_enumeration(
                c, lease, provider="aws", attempt=new, provider_references=frozenset()
            )
        await _publish(
            service, c, lease, observations={"cluster-1": absent("cluster-1")}
        )


async def test_abandoned_listing_requires_expired_grant_and_new_query(pool):
    members = (resource("cluster-1"),)
    ((a, first), (b, second)) = await _two_operations_on_one_allocation(
        pool, ALLOCATION
    )
    await finish_allocation(pool, a, first, members)
    await finish_allocation(pool, b, second, members)

    async def query(*args):
        pytest.fail("report queries must not run while the successor listing is active")

    b.query_provider = query
    async with pool.acquire() as c:
        old = await a.begin_provider_enumeration(c, first, provider="aws")
        with pytest.raises(OperationRefused, match="in progress"):
            await b.begin_provider_enumeration(c, second, provider="aws")
        await c.execute(
            "UPDATE harness_operation_leases SET expires_at=now()-interval '1s' "
            "WHERE operation_id=$1",
            first.operation_id,
        )
        new = await b.begin_provider_enumeration(c, second, provider="aws")
        with pytest.raises(OperationRefused):
            await a.record_provider_enumeration(
                c,
                first,
                provider="aws",
                attempt=old,
                provider_references=frozenset({"late-child"}),
            )
        with pytest.raises(OperationRefused, match="enumeration"):
            await b.observe_report(c, second)
        await b.record_provider_enumeration(
            c,
            second,
            provider="aws",
            attempt=new,
            provider_references=frozenset({"cluster-1-handle"}),
        )
        await _publish(b, c, second, observations={"cluster-1": absent("cluster-1")})


@pytest.mark.parametrize(
    ("kind", "provider"),
    [
        ("ec2:PromoteReadReplica", "aws"),
        ("read_write_volume", "aws"),
        ("delete_then_promote", "aws"),
        ("ec2:DescribeInstancesAndPromote", "aws"),
        ("ec2:DescribeInstances", "other"),
    ],
)
@pytest.mark.parametrize("is_sealed", [False, True])
async def test_ambiguous_provider_mutation_is_fenced_or_invalidates_listing(
    pool, kind, provider, is_sealed
):
    record, lease = await leased(pool)
    service = authority(pool, lease)
    members = (resource("cluster-1"),)
    async with pool.acquire() as c:
        await service.enumerate_resources(c, lease, resources=members)
    await complete_the_plan(pool, record, lease, provider_ref="cluster-1-handle")
    async with pool.acquire() as c:
        attempt = await service.begin_provider_enumeration(c, lease, provider="aws")
        await service.record_provider_enumeration(
            c,
            lease,
            provider="aws",
            attempt=attempt,
            provider_references=frozenset({"cluster-1-handle"}),
        )
        if is_sealed:
            await service.seal_allocation(c, lease)
    from harness_jobs.execution_plan import ExecutionStep

    step = {**STEP, "operation_kind": kind, "provider": provider}
    other_record, other = await leased(
        pool, key="ambiguous", holder="second", steps=(step,)
    )
    invoked = []
    executor = OperationExecutor(
        other,
        connect=pool.acquire,
        provider_call=spy_provider(invoked, provider_ref="cluster-1-handle"),
    )

    async def call():
        return await executor.execute_provider(
            idempotency_key=step_key(other_record, ExecutionStep(**step)),
            provider=step["provider"],
            operation_kind=kind,
            target=step["target"],
        )

    if is_sealed:
        with pytest.raises(ProviderCallRefused, match="sealed"):
            await call()
        assert invoked == []
    else:
        await call()
        assert len(invoked) == 1
        async with pool.acquire() as c:
            epoch, _ = await service._epoch(c, lease, ALLOCATION)
            assert epoch > attempt.generation
            with pytest.raises(OperationRefused, match="enumerat"):
                await service.seal_allocation(c, lease)


def test_noncreating_effect_requires_an_explicit_known_provider():
    assert call_effect("eks:DescribeCluster") is CallEffect.UNRECOGNIZED
    assert (
        call_effect("eks:DescribeCluster", provider="other") is CallEffect.UNRECOGNIZED
    )
    assert call_effect("eks:DescribeCluster", provider="aws") is CallEffect.OBSERVES
    assert call_effect("delete_then_promote", provider="aws") is CallEffect.UNRECOGNIZED

"""Production ownership boundaries: competing producers and worker lifecycle."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution, create_execution
from src.orchestration.handoff import commit_handoff
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_admission import admit, maintain_worker_claim, recover_exited_claims
from src.orchestration.work_claims import ClaimOwner, OwnerKind, WorkClaimError
from tests.orchestration import test_work_claims as fixtures

engine = fixtures.engine
session = fixtures.session
session_factory = fixtures.session_factory

ORG = "org-alpha"
FLOW = "flow-1"
PLAN_VERSION = 1


async def start(session, *, run="run-1", owner=FLOW, kind=OwnerKind.ENGINE_FLOW, org=ORG, issue=5161):
    return await admit(session, org_id=org, repository_id=1234, issue=issue, owner=ClaimOwner(kind, owner), invocation_id=run)


def select_execution(identity: ExecutionIdentity):
    """This attempt's execution row, re-read rather than reused.

    ``populate_existing`` so a test that already holds the identity object still sees
    what the last production write actually left on the row.
    """
    from sqlalchemy import select

    return (
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == identity.org_id,
            OrchestrationExecution.node_id == identity.node_id,
            OrchestrationExecution.cycle == identity.cycle,
        )
        .execution_options(populate_existing=True)
    )


async def handed_off_lane(session, *, run="run-1", issue=5161, commit=True):
    """A lane that reached a genuinely committed durable continuation (#5144 F1).

    Built through production code end to end — `admit` mints the claim, `create_execution`
    admits the attempt against it, `commit_handoff` writes the receipt — because the
    guard under test reads the receipt back through the same attribution rule
    (`handoff_receipt_ref` over the row's own fences) that `results._delivery_receipt`
    uses. A hand-written `handoff_receipt_ref` column value would let the guard be
    asserted against a receipt no production path could ever have minted, which is
    exactly the confusion F1 is about.

    Returns `(claim_id, invocation_id, execution_identity, receipt_ref)`.
    """
    flow = OrchestrationFlow(org_id=ORG, slug=f"flow-{issue}", title="F1 lane", state="draft")
    session.add(flow)
    await session.flush()
    session.add(OrchestrationAcceptedPlan(org_id=ORG, flow_id=flow.id, version=PLAN_VERSION, plan_document={}, plan_hash=f"plan-{issue}"))
    node = OrchestrationNode(
        org_id=ORG, flow_id=flow.id, epic_ref="E1", wave_ref="W1", node_ref="N1", kind="story", title="Story", issue_ref=f"#{issue}"
    )
    session.add(node)
    await session.flush()

    receipt = await start(session, run=run, owner=flow.id, issue=issue)
    identity = ExecutionIdentity(
        org_id=ORG,
        node_id=node.id,
        cycle=1,
        accepted_plan_version=PLAN_VERSION,
        claim_id=receipt["claim_id"],
        claim_generation=receipt["generation"],
    )
    created = await create_execution(session, identity=identity, flow_id=flow.id)
    assert created.kind is OutcomeKind.APPLIED
    if not commit:
        return receipt["claim_id"], run, identity, None
    handoff = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    assert handoff.accepted is True
    return receipt["claim_id"], run, identity, handoff.receipt_ref


async def test_engine_and_webhook_cannot_admit_two_runs(session):
    receipt = await start(session)
    with pytest.raises(WorkClaimError, match="already owned"):
        await start(session, run="webhook", owner="human-event", kind=OwnerKind.DIRECT_DISPATCH)
    row = await session.get(OrchestrationWorkClaim, receipt["claim_id"])
    assert row.active_run_id == "run-1"


async def test_lost_producer_ack_reuses_same_pending_receipt(session):
    first = await start(session)
    retry = await start(session)
    assert retry["claim_id"] == first["claim_id"]
    assert retry["generation"] == first["generation"]
    assert retry["disposition"] == "duplicate"


async def test_sequential_personas_share_owner_and_advance_generation(session, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    first = await start(session, run="developer")
    await maintain_worker_claim(session, org_id="org-alpha", invocation_id="developer", terminal=True)
    second = await start(session, run="reviewer")
    await maintain_worker_claim(session, org_id="org-alpha", invocation_id="reviewer", terminal=True)
    third = await start(session, run="repair")
    assert first["claim_id"] == second["claim_id"] == third["claim_id"]
    assert [first["generation"], second["generation"], third["generation"]] == [1, 2, 3]
    with pytest.raises(WorkClaimError):
        await maintain_worker_claim(session, org_id="org-alpha", invocation_id="developer", terminal=True)
    row = await session.get(OrchestrationWorkClaim, first["claim_id"])
    assert row.active_run_id == "repair"


async def test_worker_missing_claim_is_refused_before_work(session, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    with pytest.raises(WorkClaimError, match="no admitted"):
        await maintain_worker_claim(session, org_id="org-alpha", invocation_id="unadmitted")


@pytest.mark.parametrize("exited", [False, True])
async def test_crash_recovery_requires_positive_workload_exit(session, exited):
    receipt = await start(session)
    store = SimpleNamespace(
        _read=Mock(
            return_value={
                "tenant_id": {"S": "org-alpha"},
                "status": {"S": "active"},
                "pod_name": {"S": "worker-1"},
                "workload_binding": {"S": "uid-1"},
            }
        )
    )
    workloads = SimpleNamespace(has_exited=Mock(return_value=exited))
    assert (await recover_exited_claims(session, store=store, workloads=workloads)).released == int(exited)
    row = await session.get(OrchestrationWorkClaim, receipt["claim_id"])
    assert row.state == (ClaimState.RELEASED.value if exited else ClaimState.HELD.value)
    workloads.has_exited.assert_called_once_with(name="worker-1", uid="uid-1")


async def test_other_tenants_execution_cannot_release_claim(session):
    receipt = await start(session)
    store = SimpleNamespace(_read=Mock(return_value={"tenant_id": {"S": "other"}, "status": {"S": "completed"}}))
    assert (await recover_exited_claims(session, store=store, workloads=None)).released == 0
    assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.HELD.value


async def test_recovery_cursor_reaches_later_claims_behind_live_workers(session):
    from sqlalchemy import select

    for i in range(3):
        await admit(
            session, org_id="org-alpha", repository_id=1234, issue=5000 + i, owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "flow"), invocation_id=f"run-{i}"
        )
    ordered = list((await session.scalars(select(OrchestrationWorkClaim).order_by(OrchestrationWorkClaim.id))).all())
    dead = ordered[-1].active_run_id

    def execution(_pk, sk):
        invocation = sk.removeprefix("EXEC#")
        return {"tenant_id": {"S": "org-alpha"}, "status": {"S": "active"}, "pod_name": {"S": invocation}, "workload_binding": {"S": invocation}}

    store = SimpleNamespace(_read=Mock(side_effect=execution))
    workloads = SimpleNamespace(has_exited=Mock(side_effect=lambda *, name, uid: uid == dead))
    cursor, released = None, 0
    for _ in range(4):
        report = await recover_exited_claims(session, store=store, workloads=workloads, limit=1, after_id=cursor)
        cursor = report.next_id
        released += report.released
    assert released == 1
    assert cursor is None
    assert workloads.has_exited.call_count == 3


class TestTerminalStatusCannotStrandACommittedContinuation:
    """A terminal report must not release the claim its own receipt is attributed to (#5144 F1).

    The reviewer's first blocker. `results._delivery_receipt` credits a stored receipt
    only via `handoff.receipt_for`, which rebuilds the reference from the execution
    row's `claim_id` **and** `claim_generation`. So a release is not a tidy-up: it makes
    a correctly committed continuation unattributable, and the story is then held on
    evidence sitting in the row that can no longer be credited to any attempt.

    That is a worse failure than the one #5144 closes — the original defect let a
    worker exit 0 with work outstanding, this would punish a worker that did everything
    right. Both release paths are covered, because a guard on only the reporting one is
    defeated by a worker that exits *without* reporting, which is the original defect's
    own failure mode.
    """

    async def test_terminal_report_preserves_the_claim_while_the_continuation_is_due(self, session, monkeypatch):
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        claim_id, run, _, _ = await handed_off_lane(session)

        await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)
        claim = await session.get(OrchestrationWorkClaim, claim_id)
        assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.HELD.value, 1, run)

    async def test_the_receipt_stays_attributable_to_the_attempt_that_minted_it(self, session, monkeypatch):
        """The consequence the refusal exists for, asserted through the production reader.

        Asserting only "the claim stayed HELD" would pass for a guard that held the
        claim for any unrelated reason. What F1 protects is *attribution*, so this
        checks the receipt is still creditable by the same rule `results` uses.
        """
        from src.orchestration.handoff import receipt_for

        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        _, run, identity, receipt_ref = await handed_off_lane(session)

        await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)

        assert await receipt_for(session, identity=identity) == receipt_ref

    async def test_old_valid_receipt_does_not_hold_replacement_ownership(self, session, monkeypatch):
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        claim_id, _, identity, receipt_ref = await handed_off_lane(session)
        claim = await session.get(OrchestrationWorkClaim, claim_id)
        claim.generation += 1
        claim.claim_event_id = claim.active_run_id = "replacement"
        await session.flush()
        await maintain_worker_claim(session, org_id=ORG, invocation_id="replacement", terminal=True)
        assert claim.state == ClaimState.RELEASED.value
        assert (await session.scalar(select_execution(identity))).handoff_receipt_ref == receipt_ref

    async def test_recovery_rechecks_handoff_after_external_observation(self, session, monkeypatch):
        from src.orchestration import work_admission

        claim_id, run, identity, _ = await handed_off_lane(session, commit=False)
        release = work_admission.release_work

        async def commit_before_release(*args, **kwargs):
            result = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
            assert result.accepted
            return await release(*args, **kwargs)

        monkeypatch.setattr(work_admission, "release_work", commit_before_release)
        store = SimpleNamespace(
            _read=Mock(
                return_value={
                    "tenant_id": {"S": ORG},
                    "status": {"S": "active"},
                    "pod_name": {"S": run},
                    "workload_binding": {"S": run},
                }
            )
        )
        report = await recover_exited_claims(session, store=store, workloads=SimpleNamespace(has_exited=Mock(return_value=True)))
        assert report.released == 0
        assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.HELD.value

    async def test_recovery_skips_the_claim_instead_of_abandoning_the_sweep(self, session):
        """Positive exit evidence is exactly when this fires — and it must not release.

        A worker that hands off correctly and then exits without reporting is precisely
        the case `recover_exited_claims` was built for, so this pass is where the
        stranding would actually happen in production.

        Skipped rather than raised: a bounded sweep covers many tenants' claims, and an
        exception would abandon every claim after this one. Asserted with a second,
        later claim that has no continuation, so a guard that merely stopped the loop
        early would fail here.
        """
        held_claim, _, _, _ = await handed_off_lane(session, run="handed-off", issue=5161)
        plain = await start(session, run="plain-exit", issue=5162)
        store = SimpleNamespace(
            _read=Mock(
                side_effect=lambda _pk, sk: {
                    "tenant_id": {"S": ORG},
                    "status": {"S": "active"},
                    "pod_name": {"S": sk.removeprefix("EXEC#")},
                    "workload_binding": {"S": sk.removeprefix("EXEC#")},
                }
            )
        )
        workloads = SimpleNamespace(has_exited=Mock(return_value=True))

        report = await recover_exited_claims(session, store=store, workloads=workloads)

        assert report.released == 1
        assert (await session.get(OrchestrationWorkClaim, held_claim)).state == ClaimState.HELD.value
        assert (await session.get(OrchestrationWorkClaim, plain["claim_id"])).state == ClaimState.RELEASED.value

    async def test_a_lane_that_never_handed_off_still_releases_normally(self, session, monkeypatch):
        """The guard must be silent for every legacy lane, or it becomes a global stall.

        `outstanding_continuation` returns `None` when no receipt was ever minted, and
        that is the ordinary case for every run predating this contract. Without this
        test the refusal could be over-broad and nothing in the suite would say so.
        """
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        receipt = await start(session, run="legacy")

        await maintain_worker_claim(session, org_id=ORG, invocation_id="legacy", terminal=True)

        assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.RELEASED.value

    async def test_a_concluded_execution_releases_the_claim_it_no_longer_needs(self, session, monkeypatch):
        """A continuation that has been *discharged* owes nothing, so it must not hold.

        The distinction the SQL terminal-status filter draws. Once the lane concludes,
        holding the claim forever would make the issue permanently unclaimable — the
        refusal has to end when the work does, not when the receipt was written.
        """
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        claim_id, run, identity, _ = await handed_off_lane(session)
        row = (await session.execute(select_execution(identity))).scalar_one()
        applied = await advance_execution(
            session,
            identity=identity,
            advance=PhaseAdvance(phase=ExecutionPhase.CONCLUDED, status=ExecutionStatus.CONCLUDED, expected_revision=row.revision),
        )
        assert applied.kind is OutcomeKind.APPLIED

        await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)

        assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.RELEASED.value

    async def test_a_receipt_from_another_generation_does_not_hold_this_claim(self, session, monkeypatch):
        """Presence is not attribution — the guard applies the same rule `receipt_for` does.

        A stored reference minted under a *different* generation is already
        unattributable, so it is not a continuation this claim backs and refusing on it
        would hold the lane on a receipt nothing can credit. Checking equality against
        `handoff_receipt_ref` rather than `IS NOT NULL` is what makes that distinction,
        and this test is what fails if the guard is weakened to mere presence.
        """
        monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
        claim_id, run, identity, receipt_ref = await handed_off_lane(session)
        row = (await session.execute(select_execution(identity))).scalar_one()
        # Rewritten in place: what a superseded generation's receipt would look like if
        # it had survived on the row after a handover advanced ownership (#5127).
        row.handoff_receipt_ref = receipt_ref.replace("generation=1", "generation=99")
        await session.flush()

        await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)

        assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.RELEASED.value


class TestAbortedRunsAreReportedOnlyAfterTheyStop:
    """When this sweep may repair an aborted run's terminal row — #3963 S4.

    The repair itself is covered in `tests/agentauth/test_abort_reconciliation.py`. This
    is the half only the call site can establish: *when* it is legitimate to write a
    terminal `aborted` status on behalf of a run that never wrote one.

    The answer is "after the run actually stopped", and the only thing here that knows
    that is `workloads.has_exited` — a Kubernetes read requiring the `agent-worker`
    container `terminated` with the pod UID and service account verified. Three weaker
    gates were available and all of them are wrong:

    * elapsed time — `liveness.py` is explicit that "loss of contact is not evidence of
      exit", and a timer would report a stop while the run was still cancelling;
    * lease or credential expiry — `has_exited`'s own docstring rules it out
      ("deliberately absent from this decision");
    * the abort marker alone — `record_abort_intent` records *acceptance* and leaves the
      execution ACTIVE on purpose, so the marker is present throughout the window in
      which the run is still running.

    The third is the dangerous one, because the marker is exactly what the repair keys
    on, so "marker present" is one careless edit away from being the whole gate.

    The other half of the call site's job is *retry*, which is subtler than it looks and
    which the first version of this wiring got wrong: this sweep selects `HELD` claims,
    so repairing after the release meant a failed repair was never reconsidered. The
    last two tests pin both directions of the fix — a transient failure holds the claim
    so the next pass retries it, and a permanent one does not, so a misconfiguration
    cannot wedge an issue's lane indefinitely.
    """

    def store_for(self, raw, *, client=None):
        """A store whose `client` is None unless a test expects the repair to run.

        `client=None` makes any attempt to write raise `AttributeError` out of the
        sweep rather than quietly succeeding. That is a stronger assertion than counting
        calls on a mock: a repair that should not have happened cannot pass silently.
        """
        return SimpleNamespace(_read=Mock(return_value=raw), client=client)

    def aborted_execution(self, **extra):
        raw = {
            "tenant_id": {"S": ORG},
            "status": {"S": "active"},
            "pod_name": {"S": "worker-1"},
            "workload_binding": {"S": "uid-1"},
            "arrived_at": {"S": "2026-09-24T11:59:00Z"},
            "abort_command_id": {"S": "cmd-abort-0001"},
            "abort_requested_at": {"S": "2026-09-24T12:00:00Z"},
        }
        raw.update(extra)
        return raw

    async def test_an_accepted_abort_on_a_live_pod_is_not_reported_as_stopped(self, session):
        """The marker is present and the pod is alive: nothing may be written.

        This is the case the marker-only gate would get wrong, and it is not a rare one —
        it is every abort, for the whole interval between acceptance and quiescence,
        which is precisely when the worker is cancelling the SDK and writing its own
        terminal row.
        """
        await start(session, run="still-running")
        workloads = SimpleNamespace(has_exited=Mock(return_value=False))

        report = await recover_exited_claims(session, store=self.store_for(self.aborted_execution()), workloads=workloads)

        assert report.aborts_repaired == 0
        assert report.released == 0
        workloads.has_exited.assert_called_once_with(name="worker-1", uid="uid-1")

    async def test_a_run_that_reported_its_own_outcome_is_not_repaired(self, session):
        """A `completed` protected record means the run reported; there is nothing to fix.

        Reached through a different branch of the sweep entirely — the one that reads the
        run's own terminal report rather than Kubernetes — so this also pins that the
        repair hangs off the exit-evidence branch and not off the loop body.
        """
        await start(session, run="reported")
        raw = self.aborted_execution(status={"S": "completed"}, terminal_outcome={"S": "completed"})

        report = await recover_exited_claims(session, store=self.store_for(raw), workloads=None)

        assert report.aborts_repaired == 0
        assert report.released == 1

    async def test_an_exited_run_that_was_never_aborted_does_not_reach_the_events_table(self, session):
        """No marker, no repair — and no write attempt at all.

        The overwhelmingly common case: an exited pod with nothing to reconcile. Pinned
        because a repair invoked unconditionally would put an events-table write on the
        hot path of ordinary claim recovery, for every released claim on the platform.
        """
        receipt = await start(session, run="ordinary-exit")
        raw = self.aborted_execution()
        del raw["abort_command_id"]
        workloads = SimpleNamespace(has_exited=Mock(return_value=True))

        report = await recover_exited_claims(session, store=self.store_for(raw), workloads=workloads)

        assert report.aborts_repaired == 0
        assert report.released == 1
        assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.RELEASED.value

    async def test_an_exited_aborted_run_is_repaired_through_the_real_sweep(self, session, monkeypatch):
        """The positive case, end to end through `recover_exited_claims`.

        Every test above is a refusal, and a gate that refuses everything would pass all
        of them. This is what makes the set falsifiable: a genuinely dead aborted run,
        driven through the production sweep against real (moto) DynamoDB, must leave an
        `aborted` row behind and count itself in the report.

        The pod exited without reporting — the exact outcome the worker's bounded retries
        cannot cover, because the pod is what disappears — so before this pass the
        operator's dashboard still said `in_progress`.
        """
        import boto3
        from moto import mock_aws

        from src.agentauth.composition import WEBHOOK_EVENTS_TABLE_ENV

        events, arrived, run = "events", "2026-09-24T11:59:00Z", "dead-abort"
        receipt = await start(session, run=run)
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
            client.create_table(
                TableName=events,
                KeySchema=[
                    {"AttributeName": "event_id", "KeyType": "HASH"},
                    {"AttributeName": "arrived_at", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "event_id", "AttributeType": "S"},
                    {"AttributeName": "arrived_at", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            client.put_item(
                TableName=events,
                Item={
                    "event_id": {"S": run},
                    "arrived_at": {"S": arrived},
                    "tenant_id": {"S": ORG},
                    # What the operator was still looking at: the run had been stopped and
                    # was gone, and this said it was working.
                    "status": {"S": "in_progress"},
                },
            )
            monkeypatch.setenv(WEBHOOK_EVENTS_TABLE_ENV, events)
            store = self.store_for(self.aborted_execution(), client=client)
            workloads = SimpleNamespace(has_exited=Mock(return_value=True))

            report = await recover_exited_claims(session, store=store, workloads=workloads)

            row = client.get_item(
                TableName=events,
                Key={"event_id": {"S": run}, "arrived_at": {"S": arrived}},
                ConsistentRead=True,
            )["Item"]

        assert report.aborts_repaired == 1
        assert row["status"]["S"] == "aborted"
        assert row["stop_reason"]["S"] == "operator_aborted"
        assert row["abort_reconciled_by"]["S"] == "abort_reconciliation"
        # The claim is released too: reporting is additional to the lifecycle work, never
        # in place of it, and never allowed to block it.
        assert report.released == 1
        assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.RELEASED.value

    async def test_a_failed_repair_is_retried_on_the_next_pass(self, session, monkeypatch):
        """The property the first version of this wiring did not have.

        This sweep selects claims in state ``HELD``. My initial implementation repaired
        *after* the release loop, so a repair that failed on a transient error had its
        claim flipped to ``RELEASED`` in the same pass — removing the row from the only
        query that would ever look at it again. The stale row then survived forever,
        which is the exact defect the reconciler exists to close, now hidden behind a
        reconciler that looked like it covered it. No test caught that, because every
        other test here either succeeds on the first attempt or asserts a refusal.

        So: fail the write, run the pass, and require that the claim is still held and
        the row still wrong. Then let the table appear and run the pass AGAIN with no
        other change — the repair must land, from state that survived the first pass.

        Two passes against one ``session`` is also the restart proof: what carries the
        retry is the Postgres row, not anything the first pass kept in memory, so a
        gateway process that died between the two would change nothing.
        """
        import boto3
        from moto import mock_aws

        from src.agentauth.composition import WEBHOOK_EVENTS_TABLE_ENV

        events, arrived, run = "events-late", "2026-09-24T11:59:00Z", "outage-abort"
        receipt = await start(session, run=run)
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
            monkeypatch.setenv(WEBHOOK_EVENTS_TABLE_ENV, events)
            store = self.store_for(self.aborted_execution(), client=client)
            workloads = SimpleNamespace(has_exited=Mock(return_value=True))

            # Pass one: the table does not exist yet, so the write genuinely fails at the
            # transport (ResourceNotFoundException from moto, not a scripted response).
            first = await recover_exited_claims(session, store=store, workloads=workloads)

            assert first.aborts_repaired == 0
            assert first.released == 0, "releasing here is what would lose the retry"
            claim = await session.get(OrchestrationWorkClaim, receipt["claim_id"])
            assert claim.state == ClaimState.HELD.value, (
                "the claim must stay held, because this query is the only thing that will ever reconsider this run"
            )

            # The outage ends. Nothing else changes — no new state, no attempt counter.
            client.create_table(
                TableName=events,
                KeySchema=[
                    {"AttributeName": "event_id", "KeyType": "HASH"},
                    {"AttributeName": "arrived_at", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "event_id", "AttributeType": "S"},
                    {"AttributeName": "arrived_at", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            client.put_item(
                TableName=events,
                Item={
                    "event_id": {"S": run},
                    "arrived_at": {"S": arrived},
                    "tenant_id": {"S": ORG},
                    "status": {"S": "in_progress"},
                },
            )

            second = await recover_exited_claims(session, store=store, workloads=workloads)

            row = client.get_item(
                TableName=events,
                Key={"event_id": {"S": run}, "arrived_at": {"S": arrived}},
                ConsistentRead=True,
            )["Item"]

        assert second.aborts_repaired == 1
        assert row["status"]["S"] == "aborted"
        # And only now is the lane handed back.
        assert second.released == 1
        assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.RELEASED.value

    async def test_a_permanent_repair_failure_does_not_wedge_the_lane(self, session, monkeypatch):
        """Holding a claim is bounded to failures a retry could actually fix.

        The retry above blocks the issue's lane while it waits, which is the right
        trade against reporting a stopped run as live — but only for a transport
        failure. A determination that will not change on retry must NOT hold the lane:
        an unset ``WEBHOOK_EVENTS_TABLE`` is a misconfiguration that could persist for
        weeks, and blocking every aborted run's issue for its duration would convert a
        reporting bug into an outage of the work it was protecting.

        So with the table name unset the repair still fails, and the claim is released
        anyway.
        """
        import boto3
        from moto import mock_aws

        from src.agentauth.composition import WEBHOOK_EVENTS_TABLE_ENV

        receipt = await start(session, run="misconfigured")
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
            monkeypatch.delenv(WEBHOOK_EVENTS_TABLE_ENV, raising=False)
            store = self.store_for(self.aborted_execution(), client=client)
            workloads = SimpleNamespace(has_exited=Mock(return_value=True))

            report = await recover_exited_claims(session, store=store, workloads=workloads)

        assert report.aborts_repaired == 0
        assert report.released == 1
        assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.RELEASED.value

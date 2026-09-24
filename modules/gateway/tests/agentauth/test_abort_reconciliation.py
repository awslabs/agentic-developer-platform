"""Durable repair of an aborted run's terminal row after it exited — Issue #3963 (S4).

## What was open, and why bounded retries did not close it

Root's review of the previous checkpoint: *"Permanent terminal reconciliation remains
open... That documents the defect, it does not implement eventual repair after actual
quiescence."* The pod-side story is bounded by construction — the worker retries the
terminal write three times before its DeleteMessage, then once more after a confirmed
ack, and then the pod is gone. Every one of those is a narrower window on the same
outage, never a closed one, because the pod is the thing that disappears.

So the repair has to happen somewhere the pod is not, and that is what these tests
cover: the run stopped, its dashboard row still says ``in_progress``, and something
outside the pod notices and fixes it.

## Why these run against real DynamoDB

Every property here is a property of a *conditional* write:

- a repair lands only on a row that is not already terminal;
- ``complete`` and ``failed`` survive a repair attempt (a mock asserting the
  condition string was *sent* cannot show that it refuses);
- two concurrent gateway processes produce one repair and one no-op, not two writes
  or an error;
- the key is the protected record's ``(event_id, arrived_at)`` and nothing else.

``moto`` gives real conditional-write semantics. A ``Stubber`` would replay whatever
response the test author scripted, which for a "cannot be overwritten" claim is the
difference between checking the lock is mentioned and checking the lock holds.

## The gate is exit evidence, and the tests hold it to that

``test_marker_alone_is_not_permission_to_report_a_stop`` is the important negative.
An abort marker means the operator's command was *accepted*, not that the run
stopped — ``record_abort_intent`` deliberately leaves the execution ACTIVE so the
worker keeps the credential it needs to perform the cancellation. Repairing on the
marker alone would report a deliberate stop while the task was still running. The
caller-side test asserts the repair is reached only through the
``workloads.has_exited`` branch, which is positive Kubernetes container-exit
evidence, not a timer and not a lease.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from src.activity import liveness
from src.agentauth.abort_reconciliation import (
    _PROTECTED_STATUSES,
    ABORTED_STATUS,
    RECONCILED_BY,
    TRANSIENT_REPAIR_FAILURE,
    repair_aborted_terminal_status,
)

AUTHORITY = "adp-test-agent-authority"
EVENTS = "adp-test-webhook-events"
TENANT = "org-tenant-001"
INVOCATION = "inv-developer-7"
ARRIVED = "2026-09-24T11:59:00Z"
REQUESTED = "2026-09-24T12:00:00Z"


@pytest.fixture
def client():
    with mock_aws():
        client = boto3.client(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_table(
            TableName=AUTHORITY,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=EVENTS,
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
        yield client


def execution(*, aborted: bool = True, arrived_at: str | None = ARRIVED, tenant: str = TENANT) -> dict:
    """The raw EXEC# item, shaped as the store writes it.

    Built as a plain dict rather than read back through the store because this is the
    value the *caller* passes in, and the point of passing it is that the repair uses
    the same record the exit decision was made against.
    """
    item = {
        "pk": {"S": f"TENANT#{tenant}"},
        "sk": {"S": f"EXEC#{INVOCATION}"},
        "invocation_id": {"S": INVOCATION},
        "tenant_id": {"S": tenant},
        "current_attempt": {"N": "1"},
        "status": {"S": "active"},
        "workload_binding": {"S": "pod-uid-1"},
        "pod_name": {"S": "worker-1"},
    }
    if arrived_at:
        item["arrived_at"] = {"S": arrived_at}
    if aborted:
        item.update(
            {
                "abort_command_id": {"S": "cmd-abort-0001"},
                "abort_body_digest": {"S": "a" * 64},
                "abort_requested_at": {"S": REQUESTED},
                "abort_requested_attempt": {"N": "1"},
            }
        )
    return item


def put_event(client, *, status: str = "in_progress", tenant: str = TENANT, **extra) -> None:
    item = {
        "event_id": {"S": INVOCATION},
        "arrived_at": {"S": ARRIVED},
        "tenant_id": {"S": tenant},
        "status": {"S": status},
        "persona": {"S": "developer"},
    }
    item.update(extra)
    client.put_item(TableName=EVENTS, Item=item)


def read_event(client, *, arrived_at: str = ARRIVED) -> dict:
    return client.get_item(
        TableName=EVENTS,
        Key={"event_id": {"S": INVOCATION}, "arrived_at": {"S": arrived_at}},
        ConsistentRead=True,
    ).get("Item", {})


def repair(client, execution_item, *, tenant: str = TENANT, events_table: str = EVENTS):
    return repair_aborted_terminal_status(
        authority_client=client,
        events_table=events_table,
        execution=execution_item,
        invocation_id=INVOCATION,
        tenant_id=tenant,
    )


class TestTheRepairItself:
    def test_a_stale_in_progress_row_becomes_aborted(self, client):
        """The whole point: the operator's dashboard stops lying.

        The run is over — the caller established that — and the row still claims it is
        running, because the pod died before it could say otherwise. After this, the
        surface the operator actually looks at agrees with what happened.
        """
        put_event(client)
        result = repair(client, execution())

        assert result.repaired is True
        assert result.reason == "repaired"
        row = read_event(client)
        assert row["status"]["S"] == ABORTED_STATUS
        assert row["stop_reason"]["S"] == "operator_aborted"

    def test_the_repaired_row_says_it_was_repaired(self, client):
        """A repaired report and a self-reported one must be distinguishable.

        Not cosmetic. "The abort reported itself normally" and "the run died without
        reporting and the platform fixed it later" are different incidents: the second
        means the dashboard was wrong for as long as it took, which is the thing an
        incident review needs to be able to see. Without this attribute the two rows
        are byte-identical.
        """
        put_event(client)
        repair(client, execution())

        row = read_event(client)
        assert row["abort_reconciled_by"]["S"] == RECONCILED_BY

    def test_repair_dates_the_terminal_report_without_redating_the_request(self, client):
        put_event(client, status_updated_at={"S": "2026-09-24T11:59:30Z"})
        before = datetime.now(UTC).replace(microsecond=0)
        repair(client, execution())
        after = datetime.now(UTC).replace(microsecond=0)
        row = read_event(client)
        reported = datetime.fromisoformat(row["status_updated_at"]["S"].replace("Z", "+00:00"))
        assert before <= reported <= after
        assert row["abort_requested_at"]["S"] == REQUESTED
        assert row["abort_reconciled_by"]["S"] == RECONCILED_BY


class TestTheVocabularyIsNotRestated:
    """The status and the refusal set come from the module that owns them.

    ``activity.liveness`` declares itself the single owner of "is a run still active",
    and it is what the dashboard renders. A local copy of either value here would be a
    second definition free to drift from the operator's screen.

    This is not hypothetical: my first draft of this module hand-wrote both, and the
    hand-written refusal list silently omitted ``rejected`` and ``rate_limited``. A
    repair could then have overwritten an ingress refusal with ``aborted`` — reporting
    that an operator stopped a run that was never admitted. Nothing in the suite
    noticed, because a parametrized list of statuses-to-refuse cannot fail on the
    member its author forgot to write down. Deriving the parametrization from the
    imported set is what makes that class of omission impossible.
    """

    def test_the_status_written_is_the_one_the_dashboard_reads(self):
        assert ABORTED_STATUS is liveness.ABORTED_STATUS

    def test_every_positively_observed_terminal_status_is_refused(self):
        """Not a subset chosen here — the whole set, whatever it grows to contain."""
        assert set(_PROTECTED_STATUSES) == set(liveness.OBSERVED_TERMINAL_STATUSES)

    def test_the_condition_is_stable_across_processes(self):
        """``frozenset`` order varies with the hash seed; the expression must not.

        Two replicas emitting different-but-equivalent ConditionExpressions for the
        same repair is needlessly hard to match against CloudTrail.
        """
        assert list(_PROTECTED_STATUSES) == sorted(_PROTECTED_STATUSES)


class TestWhatItRefusesToOverwrite:
    """Terminal statuses are positive observations; this pass observed only an exit."""

    @pytest.mark.parametrize("status", sorted(liveness.OBSERVED_TERMINAL_STATUSES))
    def test_an_already_terminal_row_is_left_alone(self, client, status):
        put_event(client, status=status)
        result = repair(client, execution())

        assert result.repaired is False
        assert result.reason == "already_terminal"
        assert read_event(client)["status"]["S"] == status

    def test_complete_is_the_one_that_matters_most(self, client):
        """A run can finish and be aborted in the same breath, and then it DID finish.

        The operator's command and the run's last work can race. If the work landed
        first, overwriting ``complete`` with ``aborted`` would tell the operator their
        stop took effect when it did not, and would hide a finished run's results. So
        this is not merely "don't clobber terminal rows" tidiness — it is a specific
        wrong answer this refuses to give.
        """
        put_event(client, status="complete", summary={"S": "opened PR #42"})
        assert repair(client, execution()).repaired is False

        row = read_event(client)
        assert row["status"]["S"] == "complete"
        assert row["summary"]["S"] == "opened PR #42"
        assert "abort_reconciled_by" not in row

    def test_failed_is_not_upgraded_into_a_tidier_story(self, client):
        """``failed`` is more specific evidence than ``aborted``, so it wins.

        A run that was aborted and also failed on its way out reported the failure it
        actually observed. Replacing that with the abort would destroy the more
        informative outcome in favour of the one this pass can infer.
        """
        put_event(client, status="failed", error_message={"S": "git push rejected"})
        assert repair(client, execution()).repaired is False
        assert read_event(client)["error_message"]["S"] == "git push rejected"

    def test_a_row_that_does_not_exist_is_not_fabricated(self, client):
        """Repair a report, never invent a run.

        The gateway role holds no ``PutItem`` on this table for exactly this reason:
        the row is created by the ingress Lambda, and a write path that can create one
        could manufacture an invocation that never arrived.

        Honest note on what this test pins: it pins the *outcome*, not the
        ``attribute_exists(event_id)`` clause specifically. Deleting that clause keeps
        this test green, because ``#tid = :tid`` is itself false against an absent
        attribute and so already refuses an update on a missing key. Both clauses are
        deliberate and the property holds twice over — but a reader should not mistake
        this for proof that either one alone is load-bearing.
        """
        result = repair(client, execution())

        assert result.repaired is False
        assert result.reason == "already_terminal"
        assert read_event(client) == {}


class TestTheGateOnReporting:
    def test_marker_alone_is_not_permission_to_report_a_stop(self, client):
        """No marker, no repair — and the marker is only half the gate.

        An execution with no ``abort_command_id`` was never aborted, so writing
        ``aborted`` would fabricate an operator decision. The other half — that the run
        has actually stopped — is the caller's, and is asserted in
        ``TestItOnlyRunsOnPositiveExitEvidence`` below, because a marker means the abort
        was ACCEPTED, not that the task ended.
        """
        put_event(client)
        result = repair(client, execution(aborted=False))

        assert result.repaired is False
        assert result.reason == "not_aborted"
        assert read_event(client)["status"]["S"] == "in_progress"

    def test_a_tenant_disagreement_between_the_two_tables_writes_nothing(self, client):
        """The events table is not the protected one, so its rows are not trusted.

        Mirrors ``registration._update_row``: if the authority record and the caller
        name different tenants, the two tables describe different things and no write
        may land. Checked before the key is built, so a mismatch cannot even address a
        row.
        """
        put_event(client)
        result = repair(client, execution(tenant="other-tenant"))

        assert result.repaired is False
        assert result.reason == "tenant_mismatch"
        assert read_event(client)["status"]["S"] == "in_progress"

    def test_the_row_key_comes_only_from_the_protected_record(self, client):
        """No ``arrived_at`` on the record means no repair, not a guessed sort key.

        The table's key is ``(event_id, arrived_at)``; ``event_id`` alone does not
        identify a row. Deriving the sort key by querying for the newest match would let
        a second row planted under the same ``event_id`` decide which row this writes —
        the "exact-row problem" ``registration.py`` documents. Refusing is the only safe
        answer when the protected record does not carry the key.
        """
        put_event(client)
        result = repair(client, execution(arrived_at=None))

        assert result.repaired is False
        assert result.reason == "row_key_unavailable"
        assert read_event(client)["status"]["S"] == "in_progress"

    def test_a_second_row_under_the_same_event_id_is_not_touched(self, client):
        """Point operation, proven by leaving a sibling row untouched.

        This is the observable consequence of the assertion above. If the write ever
        became a query-then-update, this decoy — same ``event_id``, later
        ``arrived_at`` — is what it would hit.
        """
        put_event(client)
        decoy = "2026-09-24T23:00:00Z"
        client.put_item(
            TableName=EVENTS,
            Item={
                "event_id": {"S": INVOCATION},
                "arrived_at": {"S": decoy},
                "tenant_id": {"S": TENANT},
                "status": {"S": "in_progress"},
            },
        )
        assert repair(client, execution()).repaired is True

        assert read_event(client)["status"]["S"] == ABORTED_STATUS
        assert read_event(client, arrived_at=decoy)["status"]["S"] == "in_progress"


class TestItSurvivesRunningTwiceAndRunningBadly:
    def test_two_gateway_processes_produce_one_repair(self, client):
        """Idempotent under the concurrency it will actually meet.

        ``maintain_work_claims`` runs in every gateway process, so two of them can
        reach the same dead claim in the same minute. The second losing the conditional
        write is the correct outcome, not an error — and it must report ``False`` rather
        than claiming a repair it did not perform, or the ``abort_terminal_repaired``
        count would multiply by the replica count.
        """
        put_event(client)
        first = repair(client, execution())
        second = repair(client, execution())

        assert (first.repaired, second.repaired) == (True, False)
        assert second.reason == "already_terminal"
        assert read_event(client)["status"]["S"] == ABORTED_STATUS

    def test_an_unavailable_events_table_returns_instead_of_raising(self, client):
        """A failed repair must not abandon the sweep that carries it.

        The host pass exists to release work claims, which is what the rest of the
        platform waits on. If reporting could raise, an events-table outage would stop
        claim recovery for every tenant — trading a stale dashboard row for stuck work.

        The reason is ``TRANSIENT_REPAIR_FAILURE``, and that exact value is what the
        caller branches on to keep the work claim held for another pass. Retry is a
        property of the CALL SITE, not of this function, and an earlier version of this
        docstring claimed "the next pass tries again" while the wiring released the
        claim first — which removed the row from the only query that would ever look at
        it again. ``test_a_failed_repair_is_retried_on_the_next_pass`` in
        ``tests/orchestration/test_work_admission.py`` is what actually holds that
        claim; this test only pins the reason it depends on.
        """
        result = repair(client, execution(), events_table="table-that-does-not-exist")

        assert result.repaired is False
        assert result.reason == TRANSIENT_REPAIR_FAILURE

    def test_an_unconfigured_events_table_is_refused_not_guessed(self, client):
        """An empty table name means the environment is not wired, so do nothing.

        Worth pinning because the module-level default elsewhere in the codebase is a
        dev-shaped table name. Falling back to a default here would aim a production
        gateway's repair writes at a dev table, or vice versa.
        """
        result = repair(client, execution(), events_table="")

        assert result.repaired is False
        assert result.reason == "row_key_unavailable"


# The caller-side half of the gate — which branch of the sweep may reach the repair at
# all — lives in `tests/orchestration/test_work_admission.py`, beside the Postgres
# fixtures `recover_exited_claims` needs. See
# `TestAbortedRunsAreReportedOnlyAfterTheyStop` there. It is the more important half:
# this file can show the repair writes a correct row, but only the call site can show
# it happens after the run actually stopped rather than merely after it was told to.


def test_repair_removes_dead_worker_control_registration(client):
    # Seed the public transport fields independently of the implementation list.
    fields = {
        "control_version": {"N": "1"}, "control_address": {"S": "10.0.0.42"},
        "control_port": {"N": "8770"}, "control_token": {"S": "old-secret"},
        "control_token_expires_at": {"S": "2026-09-25T00:00:00Z"},
        "control_registered_at": {"S": ARRIVED}, "control_credential_epoch": {"N": "1"},
    }
    put_event(client, **fields)
    assert repair(client, execution()).repaired
    row = read_event(client)
    assert row["status"] == {"S": "aborted"}
    assert not set(fields).intersection(row)

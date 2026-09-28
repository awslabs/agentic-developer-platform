"""R10 acceptance 2 — durable state and handoff precede deleting the input message.

Issue #5050 (U5), EPIC #4910.

The criterion is an ORDER, so these tests assert relative positions in a shared call log
rather than merely that each call happened. A suite that checked "state written" and
"message deleted" independently would pass against an implementation that deleted first,
which is the exact bug acceptance 2 exists to prevent: the message is gone and nothing
durable records what it was for.

The crash tests are the other half. They assert the SAFETY consequence of the ordering — an
agent that dies before completion leaves the message undeleted, so its visibility timeout
expires, the message returns to the queue and KEDA spawns a replacement job.

These run against the fakes in `_hosting_fakes.py`, which are mocks for B's durable store,
channel and inbox. Per `acceptance-split.md` rule 2 that establishes this module's ordering
logic and nothing about B's implementations.
"""

from __future__ import annotations

import pytest
from _hosting_fakes import RECEIPT_HANDLE, SESSION_ID, Boom
from superplane_hosting import (
    ORDER,
    CompletionLog,
    OrderingViolation,
    Step,
    complete_session,
)

pytest_plugins = ("_hosting_fixtures",)


class TestDeleteIsLast:
    """The acceptance criterion: the input message is deleted last."""

    def test_delete_follows_durable_state_and_handoff(
        self, outcome, store, handoff, inbox, log
    ):
        """durable state -> durable handoff -> delete, asserted as positions."""
        complete_session(outcome, store, handoff, inbox)

        assert log.index("write_session_result") < log.index("send"), (
            "The handoff was sent before the state was durable."
        )
        assert log.index("send") < log.index("delete"), (
            "The input message was deleted before the durable handoff — a crash here loses "
            "the work silently instead of returning the message for KEDA to respawn."
        )

    def test_delete_is_the_final_side_effect(self, outcome, store, handoff, inbox, log):
        """Nothing happens after the acknowledgement."""
        complete_session(outcome, store, handoff, inbox)

        assert log.calls[-1] == "delete"

    def test_alerts_are_durable_before_the_delete(
        self, outcome_with_alert, store, handoff, inbox, log
    ):
        """An alert is recorded while the message is still redeliverable.

        This is what couples acceptance 3 to acceptance 2: if the alert were written after
        the delete, a crash in between would lose the alert while the work stayed
        acknowledged — an operator would never learn about a session that had already been
        marked handled.
        """
        complete_session(outcome_with_alert, store, handoff, inbox)

        assert log.index("write_alert") < log.index("delete")
        assert len(store.alerts) == 1

    def test_completion_log_reports_the_required_order(
        self, outcome_with_alert, store, handoff, inbox
    ):
        """The returned log is the evidence, and it matches the declared order."""
        result = complete_session(outcome_with_alert, store, handoff, inbox)

        assert result.steps == list(ORDER)
        assert result.input_deleted is True
        assert result.durable_work_finished_before_delete() is True

    def test_a_session_without_alerts_skips_only_the_alert_step(
        self, outcome, store, handoff, inbox
    ):
        """Skipping a step is allowed; reordering is not."""
        result = complete_session(outcome, store, handoff, inbox)

        assert result.steps == [
            Step.STATE_WRITTEN,
            Step.HANDOFF_SENT,
            Step.INPUT_DELETED,
        ]
        assert result.durable_work_finished_before_delete() is True

    def test_the_message_is_acknowledged_exactly_once(
        self, outcome, store, handoff, inbox
    ):
        """The receipt handle the session was given is the one acknowledged."""
        complete_session(outcome, store, handoff, inbox)

        assert inbox.deleted == [RECEIPT_HANDLE]

    def test_the_result_reaches_both_durable_surfaces(
        self, outcome, store, handoff, inbox
    ):
        """The stored result and the handed-off payload agree."""
        complete_session(outcome, store, handoff, inbox)

        assert store.results[SESSION_ID] == {"status": "ok"}
        assert handoff.sent == [(SESSION_ID, {"status": "ok"})]


class TestCrashLeavesTheMessageRedeliverable:
    """A crash before completion must NOT acknowledge the message.

    Each test kills the run at a different step and asserts the same invariant: the inbox
    was never told to delete, so the message becomes visible again and KEDA respawns.
    """

    def test_crash_writing_durable_state(self, outcome, store, handoff, inbox):
        store.fail_on_result = True

        with pytest.raises(Boom):
            complete_session(outcome, store, handoff, inbox)

        assert inbox.deleted == [], (
            "A crash during the durable write still acknowledged the message."
        )

    def test_crash_writing_the_alert(self, outcome_with_alert, store, handoff, inbox):
        store.fail_on_alert = True

        with pytest.raises(Boom):
            complete_session(outcome_with_alert, store, handoff, inbox)

        assert inbox.deleted == []

    def test_crash_sending_the_handoff(self, outcome, store, handoff, inbox, log):
        """The durable state survives; the message is still unacknowledged.

        This is the most valuable case: partial progress is recorded, and because the
        message returns to the queue the respawned job can complete the work rather than
        the session being lost.
        """
        handoff.fail = True

        with pytest.raises(Boom):
            complete_session(outcome, store, handoff, inbox)

        assert "write_session_result" in log, (
            "The durable state should have been written before the failure."
        )
        assert inbox.deleted == [], (
            "A crash before the durable handoff acknowledged the message — the work is now lost silently."
        )

    def test_an_incomplete_log_is_not_reported_as_early_delete(self):
        """A log with no delete is safe: nothing was acknowledged early.

        Guards the helper against reading a crash as an ordering violation, which would
        make the crash tests above report the wrong failure.
        """
        partial = CompletionLog()
        partial.record(Step.STATE_WRITTEN)

        assert partial.input_deleted is False
        assert partial.durable_work_finished_before_delete() is True


class TestOrderingViolationsAreLoud:
    """An out-of-order step raises rather than silently reordering.

    A caller that reaches this has a real defect, and the failure names the consequence so
    whoever hits it does not need to rediscover why the order matters.
    """

    def test_delete_before_handoff_is_refused(self):
        log = CompletionLog()
        log.record(Step.STATE_WRITTEN)
        log.record(Step.INPUT_DELETED)

        with pytest.raises(OrderingViolation, match="loses work silently"):
            log.record(Step.HANDOFF_SENT)

    def test_state_after_handoff_is_refused(self):
        log = CompletionLog()
        log.record(Step.HANDOFF_SENT)

        with pytest.raises(OrderingViolation):
            log.record(Step.STATE_WRITTEN)

    def test_repeating_a_step_is_refused(self):
        """Two deletes mean two acknowledgements of one message."""
        log = CompletionLog()
        log.record(Step.INPUT_DELETED)

        with pytest.raises(OrderingViolation):
            log.record(Step.INPUT_DELETED)

    def test_the_declared_order_ends_with_the_delete(self):
        """Pins the contract itself.

        If someone reorders the `Step` enum, `ORDER` follows it silently — this is the test
        that refuses to let the required order change without a failing test.
        """
        assert ORDER[-1] is Step.INPUT_DELETED
        assert ORDER[0] is Step.STATE_WRITTEN


class TestPrecedentIsFollowed:
    """The ordering is copied from an established precedent, not invented here."""

    def test_matches_the_cyber_triage_handler_order(self):
        """`cyber/workers/triage/handler.py` does durable write -> send -> delete.

        Read as text rather than imported: the handler needs boto3 and reads environment
        variables at import time, and this lane has no AWS. What matters is the order of
        its three calls, which is exactly what a text read establishes.
        """
        from pathlib import Path

        # tests/[0] agent/[1] superplane/[2] domain-apps/[3] modules/[4] root/[5]
        repo_root = Path(__file__).resolve().parents[5]
        handler = (
            repo_root
            / "modules"
            / "domain-apps"
            / "cyber"
            / "workers"
            / "triage"
            / "handler.py"
        )
        source = handler.read_text(encoding="utf-8")

        put_item = source.index("ddb.put_item(")
        send_message = source.index("sqs.send_message(")
        delete_message = source.index("sqs.delete_message(")

        assert put_item < send_message < delete_message, (
            "The precedent this module copies has changed; revisit the ordering in handoff.py."
        )

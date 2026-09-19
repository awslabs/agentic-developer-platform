"""Unit tests for lib/handoff_client.py — the worker half of #5144.

The defect this module closes is that a worker can exit 0 while review, deployment
or evaluation are still outstanding, because the advisory status path swallows its
own failures. So the tests are organised around the three properties that keep the
strict path from decaying back into the advisory one:

1. **Strict, unlike `record_status()`.** The advisory client returns ``None``, which
   cannot be distinguished from success. Every result here carries an explicit
   ``accepted`` that is false unless the gateway committed a receipt AND returned it.
   An unusable or receipt-less response is asserted to be *not* accepted, in both
   directions, because "not obviously a failure" is precisely how the original defect
   read as success.

2. **Fail-soft at the boundary, loud in the note.** By the time this runs the branch
   is pushed and the PR is open, so :func:`handoff_note` may not raise into
   `entrypoint.py`. But every failure must return a *visible* note saying the work
   stays due — a silently swallowed failure would reproduce the defect's worst
   property, a run that looks finished while its remaining work is invisible.

3. **The worker names no work and selects no flow.** No tenant, node, cycle, plan
   version, claim generation, execution or action id, and no field for one. Asserted
   on the payload itself rather than trusted to the server, because it is the
   worker's half of the contract and the property that stops a worker committing a
   handoff against a run it was not dispatched for.

The fourth group is compatibility: an unmarked run must send nothing and behave
exactly as it did before this module existed, which is what makes a staged deployment
of server-then-worker safe in the only direction it can be safe in.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.handoff_client import (  # noqa: E402
    HANDOFF_REQUIRED_ENV,
    HandoffReceipt,
    handoff_note,
    handoff_required,
    report_handoff,
)
from lib.status_gateway_client import StatusGatewayError  # noqa: E402

RECEIPT = "handoff:execution=exec-1:cycle=1:plan=3:claim=claim-1:generation=1"


@pytest.fixture(autouse=True)
def _handoff_env(monkeypatch):
    """Default every test to "the engine asked for a handoff, authority is on"."""
    monkeypatch.setenv(HANDOFF_REQUIRED_ENV, "true")
    monkeypatch.setattr("lib.handoff_client.authority_enabled", lambda: True)


def _committed(**overrides) -> dict:
    payload = {"outcome": "committed", "receipt_ref": RECEIPT, "accepted": True}
    payload.update(overrides)
    return payload


class TestHandoffRequired:
    """The marker gates everything: an unmarked run behaves exactly as before."""

    def test_absent_env_is_false(self, monkeypatch):
        monkeypatch.delenv(HANDOFF_REQUIRED_ENV, raising=False)
        assert handoff_required() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", " true ", "1", "yes", "on"])
    def test_true_spellings(self, monkeypatch, value):
        monkeypatch.setenv(HANDOFF_REQUIRED_ENV, value)
        assert handoff_required() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
    def test_unrecognised_values_are_off(self, monkeypatch, value):
        """Anything unrecognised must read as *off*, never as on."""
        monkeypatch.setenv(HANDOFF_REQUIRED_ENV, value)
        assert handoff_required() is False

    def test_unmarked_run_reports_nothing_and_appends_nothing(self, monkeypatch):
        """A webhook trigger or legacy dispatch is byte-for-byte unchanged."""
        monkeypatch.delenv(HANDOFF_REQUIRED_ENV, raising=False)
        with patch("lib.handoff_client.post_self") as post:
            assert handoff_note(summary="done") == ""
        post.assert_not_called()


class TestPayload:
    """What is sent — and what must never be."""

    def test_sends_no_work_identifier_of_any_kind(self):
        """The gateway resolves the work from the run credential.

        A tenant/node/cycle/plan/claim/execution field here would be a lookup
        assertion the *caller* controls, which is the weaker trust model this route
        exists to reject. Every hosted worker assumes the same platform role, so a
        self-declared identifier reduces to "whatever the caller typed".
        """
        with patch("lib.handoff_client.post_self", return_value=_committed()) as post:
            report_handoff(summary="done")
        path, payload = post.call_args[0]
        assert path == "/handoff"
        forbidden = {
            "org_id",
            "tenant_id",
            "node_id",
            "cycle",
            "accepted_plan_version",
            "claim_id",
            "claim_generation",
            "execution_id",
            "action_id",
            "run_id",
            "message_id",
            "flow_id",
        }
        assert not forbidden & set(payload)

    def test_sends_no_status_flow_or_action_selection(self):
        """The worker does not choose what happens next.

        The server resolves the execution, action and current claim; a worker-supplied
        status or action would let a run declare its own completion, which is the
        defect restated.
        """
        with patch("lib.handoff_client.post_self", return_value=_committed()) as post:
            report_handoff(summary="done")
        _, payload = post.call_args[0]
        assert not {"status", "phase", "action", "outcome", "terminal", "complete"} & set(payload)

    def test_summary_is_the_only_field_and_is_optional(self):
        with patch("lib.handoff_client.post_self", return_value=_committed()) as post:
            report_handoff()
        _, payload = post.call_args[0]
        assert payload == {}

    def test_summary_is_bounded(self):
        """Matches the route's max_length, so an oversized note is not a 422."""
        with patch("lib.handoff_client.post_self", return_value=_committed()) as post:
            report_handoff(summary="x" * 9000)
        _, payload = post.call_args[0]
        assert len(payload["summary"]) == 4096


class TestReceiptIsStrict:
    """`accepted` requires a committed outcome AND a returned receipt."""

    def test_committed_with_receipt_is_accepted(self):
        assert HandoffReceipt(outcome="committed", receipt_ref=RECEIPT).accepted is True

    def test_already_committed_is_accepted(self):
        """A repeat converging on the same receipt is a success, not a failure."""
        assert HandoffReceipt(outcome="already_committed", receipt_ref=RECEIPT).accepted is True

    def test_committed_without_a_receipt_is_not_accepted(self):
        """An outcome word alone is not evidence of anything durable."""
        assert HandoffReceipt(outcome="committed", receipt_ref="").accepted is False

    @pytest.mark.parametrize("outcome", ["superseded", "stale", "refused"])
    def test_refusals_are_not_accepted_even_with_a_receipt(self, outcome):
        assert HandoffReceipt(outcome=outcome, receipt_ref=RECEIPT).accepted is False

    def test_an_unknown_future_outcome_is_not_accepted(self):
        """Fail closed: a newer server's outcome this worker cannot interpret is not a
        handoff. An `!= "refused"` test would have admitted it."""
        assert HandoffReceipt(outcome="partially_maybe_ok", receipt_ref=RECEIPT).accepted is False

    def test_response_without_a_usable_outcome_raises(self):
        with patch("lib.handoff_client.post_self", return_value={"receipt_ref": RECEIPT}):
            with pytest.raises(StatusGatewayError):
                report_handoff()

    def test_non_string_receipt_is_dropped_not_coerced(self):
        """A truthy non-string would make `bool(receipt_ref)` true on garbage."""
        with patch("lib.handoff_client.post_self", return_value={"outcome": "committed", "receipt_ref": 12345}):
            assert report_handoff().accepted is False


class TestNoteIsFailSoftButVisible:
    """Never raises into `entrypoint.py`; always says what the failure means."""

    def test_accepted_note_names_the_receipt_and_denies_completion(self):
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            note = handoff_note(summary="done")
        assert RECEIPT in note
        # The note must not read as "story complete".
        assert "does not by itself complete" in note

    def test_transport_failure_returns_a_visible_note(self):
        with patch("lib.handoff_client.post_self", side_effect=StatusGatewayError("gateway refused")):
            note = handoff_note(summary="done")
        assert isinstance(note, str)
        assert "not recorded" in note
        assert "keep it due" in note

    def test_refusal_says_another_attempt_may_own_the_work(self):
        """A superseded outcome must never read as a handoff."""
        with patch(
            "lib.handoff_client.post_self",
            return_value={"outcome": "superseded", "receipt_ref": None, "reason": "handoff_receipt_superseded"},
        ):
            note = handoff_note(summary="done")
        assert "not accepted" in note
        assert "superseded" in note

    def test_authority_disabled_is_an_explicit_blocker_not_a_fallback(self, monkeypatch):
        """No broader credential is tried. The gateway path is the only path."""
        monkeypatch.setattr("lib.handoff_client.authority_enabled", lambda: False)
        with patch("lib.handoff_client.post_self") as post:
            note = handoff_note(summary="done")
        post.assert_not_called()
        assert "not recorded" in note
        assert "leave this work due" in note

    @pytest.mark.parametrize(
        "failure",
        [
            StatusGatewayError("boom"),
            StatusGatewayError(""),
        ],
    )
    def test_every_transport_failure_returns_a_string(self, failure):
        with patch("lib.handoff_client.post_self", side_effect=failure):
            assert isinstance(handoff_note(), str)

    def test_note_never_claims_a_terminal_state(self):
        """This module cannot mark a lane complete, and its text must not imply it."""
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            note = handoff_note(summary="done")
        lowered = note.lower()
        assert "story complete" not in lowered
        assert "delivery complete" not in lowered


class TestTransportIsInherited:
    """All three auth layers come from `status_gateway_client`, not from here."""

    def test_goes_through_post_self(self):
        """`post_self` carries SigV4 + run credential + workload token, refuses
        redirects and sets `trust_env = False`. Opening a second transport here would
        mean a second, weaker set of those properties."""
        import inspect

        import lib.handoff_client as module

        source = inspect.getsource(module)
        assert "post_self" in source
        # No independent HTTP client, which would bypass the inherited layers.
        assert "httpx.Client(" not in source
        assert "requests.post" not in source
        assert "urllib" not in source

    def test_does_not_read_credentials_itself(self):
        """Credentials are never read, echoed or logged by this module."""
        import inspect

        import lib.handoff_client as module

        source = inspect.getsource(module)
        for secret in ("AWS_SECRET", "ADP_RUN_CREDENTIAL", "X-Adp-Run-Credential", "private_key", "aws_access_key"):
            assert secret not in source

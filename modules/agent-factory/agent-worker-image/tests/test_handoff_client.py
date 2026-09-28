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

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.handoff_client import (
    HANDOFF_EXPECT_ENV,
    HANDOFF_RECEIPT_CONTRACT_VERSION,
    HANDOFF_REQUIRED_ENV,
    expected_identity,
    handoff_note,
    handoff_required,
    report_handoff,
)
from lib.status_gateway_client import StatusGatewayError

RECEIPT = "handoff:execution=exec-1:cycle=1:plan=3:claim=claim-1:generation=1"

# The fences the engine publishes on the envelope for this run's dispatch. Every
# accepted response below must echo exactly these; a case that changes one is asking
# "what happens when the receipt is for other work?".
EXPECT = {
    "contract_version": HANDOFF_RECEIPT_CONTRACT_VERSION,
    "execution_id": "exec-1",
    "policy_id": "policy-1",
    "policy_hash": "a" * 64,
    "org_id": "tenant-1",
    "flow_id": "flow-1",
    "node_id": "node-1",
    "cycle": 1,
    "accepted_plan_version": 3,
    "claim_id": "claim-1",
    "claim_generation": 1,
}


@pytest.fixture(autouse=True)
def _handoff_env(monkeypatch):
    """Default every test to "the engine asked for a handoff, authority is on"."""
    monkeypatch.setenv(HANDOFF_REQUIRED_ENV, "true")
    monkeypatch.setenv(HANDOFF_EXPECT_ENV, json.dumps(EXPECT))
    monkeypatch.setattr("lib.handoff_client.authority_enabled", lambda: True)


def _receipt(**overrides) -> dict:
    """A typed receipt matching this run's dispatch, as the gateway returns it."""
    receipt = dict(EXPECT)
    receipt.update(
        {
            "receipt_ref": RECEIPT,
            "action": "awaiting_review",
            "action_id": "handoff-action:exec-1:1:1",
            "next_check_at": "2026-09-19T12:00:00+00:00",
        }
    )
    receipt.update(overrides)
    return receipt


def _committed(**overrides) -> dict:
    payload = {
        "outcome": "committed",
        "receipt_ref": RECEIPT,
        "accepted": True,
        "receipt": _receipt(),
    }
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
    """`accepted` requires a positive acceptance AND a receipt for *this* dispatch.

    Asserted through :func:`report_handoff` rather than by constructing a
    ``HandoffReceipt``: ``accepted`` is deliberately a stored field set only after
    validation, so a test that built the dataclass by hand could assert a value the
    validator would never produce and pin nothing at all.
    """

    def test_committed_with_a_matching_receipt_is_accepted(self):
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            result = report_handoff()
        assert (result.accepted, result.receipt_ref, result.mismatch) == (True, RECEIPT, "")

    def test_already_committed_is_accepted(self):
        """A repeat converging on the same receipt is a success, not a failure."""
        with patch(
            "lib.handoff_client.post_self", return_value=_committed(outcome="already_committed")
        ):
            assert report_handoff().accepted is True

    def test_a_positive_outcome_that_says_not_accepted_is_refused(self):
        """The F-defect in one case: the flag must be read, never inferred."""
        with patch("lib.handoff_client.post_self", return_value=_committed(accepted=False)):
            result = report_handoff()
        assert result.accepted is False
        assert "did not positively accept" in result.mismatch

    def test_committed_without_a_receipt_is_not_accepted(self):
        """An outcome word alone is not evidence of anything durable."""
        with patch("lib.handoff_client.post_self", return_value=_committed(receipt=None)):
            assert report_handoff().accepted is False

    @pytest.mark.parametrize("outcome", ["superseded", "stale", "refused"])
    def test_refusals_are_not_accepted_even_with_a_receipt(self, outcome):
        with patch("lib.handoff_client.post_self", return_value=_committed(outcome=outcome)):
            assert report_handoff().accepted is False

    def test_an_unknown_future_outcome_is_not_accepted(self):
        """Fail closed: a newer server's outcome this worker cannot interpret is not a
        handoff. An `!= "refused"` test would have admitted it."""
        with patch(
            "lib.handoff_client.post_self", return_value=_committed(outcome="partially_maybe_ok")
        ):
            assert report_handoff().accepted is False

    def test_a_refused_readback_does_not_carry_a_quotable_receipt(self):
        """The reference is dropped on a failed readback.

        Otherwise a caller could quote a receipt from a report that did not validate,
        which is the same false evidence in a different place.
        """
        with patch(
            "lib.handoff_client.post_self", return_value=_committed(receipt=_receipt(cycle=9))
        ):
            result = report_handoff()
        assert (result.accepted, result.receipt_ref) == (False, "")

    def test_response_without_a_usable_outcome_raises(self):
        with (
            patch("lib.handoff_client.post_self", return_value={"receipt_ref": RECEIPT}),
            pytest.raises(StatusGatewayError),
        ):
            report_handoff()

    def test_a_non_object_response_raises(self):
        with (
            patch("lib.handoff_client.post_self", return_value=[]),
            pytest.raises(StatusGatewayError),
        ):
            report_handoff()

    def test_non_string_receipt_is_dropped_not_coerced(self):
        """A truthy non-string would make `bool(receipt_ref)` true on garbage."""
        with patch(
            "lib.handoff_client.post_self",
            return_value={"outcome": "committed", "receipt_ref": 12345},
        ):
            assert report_handoff().accepted is False


class TestReadbackIsBoundToThisDispatch:
    """The receipt must be for the work this run was dispatched to do.

    These are the cases a "recognised outcome plus non-empty string" check could not
    see: the server genuinely committed *something*, and the question is whether it
    committed *this run's* continuation.
    """

    @pytest.mark.parametrize(
        ("field", "wrong"),
        [
            ("org_id", "other-tenant"),
            ("flow_id", "other-flow"),
            ("node_id", "other-node"),
            ("cycle", 2),
            ("accepted_plan_version", 4),
            ("claim_id", "other-claim"),
            ("claim_generation", 2),
            ("execution_id", "other-execution"),
            ("policy_id", "other-policy"),
            ("policy_hash", "b" * 64),
        ],
    )
    def test_each_fence_is_compared_individually(self, field, wrong):
        """One field at a time: a validator that forgot one would pass a whole-object
        test and still admit the field it forgot."""
        with patch(
            "lib.handoff_client.post_self",
            return_value=_committed(receipt=_receipt(**{field: wrong})),
        ):
            result = report_handoff()
        assert result.accepted is False
        assert field in result.mismatch

    def test_a_contradictory_body_is_refused(self):
        """Two receipt references that disagree. Neither can be trusted."""
        with patch(
            "lib.handoff_client.post_self",
            return_value=_committed(receipt=_receipt(receipt_ref=RECEIPT + ":x")),
        ):
            result = report_handoff()
        assert result.accepted is False
        assert "disagrees" in result.mismatch

    def test_a_contract_version_this_worker_cannot_field_check_is_refused(self):
        with patch(
            "lib.handoff_client.post_self",
            return_value=_committed(receipt=_receipt(contract_version=2)),
        ):
            result = report_handoff()
        assert result.accepted is False
        assert "contract version" in result.mismatch

    @pytest.mark.parametrize(
        ("mutation", "phrase"),
        [
            ({"next_check_at": None}, "no next-check time"),
            ({"next_check_at": ""}, "no next-check time"),
            ({"next_check_at": "not-a-time"}, "not a timestamp"),
            ({"next_check_at": "2026-09-19T12:00:00"}, "timezone-aware"),
            ({"next_check_at": "20260919T120000+0000"}, "canonical"),
            ({"contract_version": True}, "contract version"),
            ({"action_id": "handoff-action:other-execution:1:1"}, "another continuation action"),
            ({"action": "deploying"}, "does not recognise"),
            ({"action": None}, "does not recognise"),
            ({"action_id": ""}, "no continuation action"),
            ({"receipt_ref": ""}, "no receipt reference"),
            ({"cycle": "1"}, "cycle"),
            ({"cycle": None}, "cycle"),
            ({"claim_generation": True}, "claim_generation"),
            ({"org_id": ""}, "org_id"),
        ],
    )
    def test_incomplete_or_malformed_fields_are_refused(self, mutation, phrase):
        """Missing, wrongly-typed and unknown-vocabulary values all refuse.

        ``cycle: "1"`` and ``claim_generation: True`` are here for Python's own
        hazards: ``bool`` is an ``int``, and loose coercion would accept the string. A
        fence satisfiable by the wrong type is not a fence.
        """
        receipt = _receipt(**mutation)
        body = _committed(receipt=receipt)
        if "receipt_ref" in mutation:
            body["receipt_ref"] = mutation["receipt_ref"]
        with patch("lib.handoff_client.post_self", return_value=body):
            result = report_handoff()
        assert result.accepted is False
        assert phrase in result.mismatch

    def test_a_required_handoff_with_no_published_expectation_is_refused(self, monkeypatch):
        """Absent expectation means "cannot be checked", not "skip the check".

        The unsafe rollout direction: a gateway that sets the marker but publishes no
        fences. Trusting the server's word there is exactly what the readback exists
        to make unnecessary.
        """
        monkeypatch.delenv(HANDOFF_EXPECT_ENV, raising=False)
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            result = report_handoff()
        assert result.accepted is False
        assert HANDOFF_EXPECT_ENV in result.mismatch

    @pytest.mark.parametrize("raw", ["not json", "[]", '"string"', "null", "7"])
    def test_an_unusable_expectation_refuses_rather_than_raising(self, monkeypatch, raw):
        """Parsed defensively: a malformed env var must not raise into a run that has
        already delivered its work, and must not be read as permission either."""
        monkeypatch.setenv(HANDOFF_EXPECT_ENV, raw)
        assert expected_identity() == {}
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            assert report_handoff().accepted is False

    def test_an_expectation_missing_a_fence_cannot_license_an_acceptance(self, monkeypatch):
        """A partial expectation is not a partial check — it is no check for that
        field, so it refuses rather than comparing what happens to be present."""
        monkeypatch.setenv(
            HANDOFF_EXPECT_ENV,
            json.dumps({k: v for k, v in EXPECT.items() if k != "claim_generation"}),
        )
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            result = report_handoff()
        assert result.accepted is False
        assert "claim_generation" in result.mismatch


class TestNoteIsFailSoftButVisible:
    """Never raises into `entrypoint.py`; always says what the failure means."""

    def test_accepted_note_names_the_receipt_and_denies_completion(self):
        with patch("lib.handoff_client.post_self", return_value=_committed()):
            note = handoff_note(summary="done")
        assert RECEIPT in note
        # The note must not read as "story complete".
        assert "does not by itself complete" in note

    def test_transport_failure_returns_a_visible_note(self):
        with patch(
            "lib.handoff_client.post_self", side_effect=StatusGatewayError("gateway refused")
        ):
            note = handoff_note(summary="done")
        assert isinstance(note, str)
        assert "not recorded" in note
        assert "keep it due" in note

    def test_refusal_says_another_attempt_may_own_the_work(self):
        """A superseded outcome must never read as a handoff."""
        with patch(
            "lib.handoff_client.post_self",
            return_value={
                "outcome": "superseded",
                "receipt_ref": None,
                "reason": "handoff_receipt_superseded",
            },
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
        for secret in (
            "AWS_SECRET",
            "ADP_RUN_CREDENTIAL",
            "X-Adp-Run-Credential",
            "private_key",
            "aws_access_key",
        ):
            assert secret not in source

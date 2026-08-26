"""Executes the HITL ticket golden fixture against the normative models (#4178).

Run by `.github/workflows/hitl-ticket-contract-tests.yml`. A golden fixture
prevents nothing if nothing executes it — that is the failure mode
`provenance-contract-tests.yml:11` calls out by name, and this file is the
answer to it for this contract.

Structure mirrors `modules/gateway/tests/internal/test_provenance_contract.py`:
accepted documents must validate, every rejected variant must be REJECTED, and a
missing fixture fails loudly rather than skipping the contract silently.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from models import (  # noqa: E402  (path shim above must run first)
    NON_PERMISSIVE_RESULTS,
    SYSTEM_RESULTS,
    ApproverMode,
    HitlResponse,
    HitlResult,
    HitlTicket,
    answers_ticket,
)

GOLDEN_PATH = _HERE / "hitl-ticket.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)


def _strip_comments(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop `$comment` keys, which are documentation and not part of the shape."""
    return {k: v for k, v in doc.items() if not k.startswith("$")}


ACCEPTED_TICKETS = [
    _strip_comments(GOLDEN["accepted_ticket"]),
    _strip_comments(GOLDEN["accepted_ticket_gate_stage"]),
]
ACCEPTED_RESPONSES = [_strip_comments(r) for r in GOLDEN["accepted_responses"]]
PRIMARY_TICKET = ACCEPTED_TICKETS[0]
PRIMARY_RESPONSE = ACCEPTED_RESPONSES[0]


def _variant(base: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Apply a rejected-variant spec to a base document."""
    body = dict(base)
    body.update(spec.get("patch", {}))
    for key in spec.get("unset", []):
        body.pop(key, None)
    return body


def _ids(variants: list[dict[str, Any]]) -> list[str]:
    return [v["name"] for v in variants]


TICKET_VARIANTS = GOLDEN["rejected_ticket_variants"]
RESPONSE_VARIANTS = GOLDEN["rejected_response_variants"]


# ---------------------------------------------------------------------------
# The fixture itself
# ---------------------------------------------------------------------------


class TestFixtureIntegrity:
    def test_golden_fixture_exists(self):
        """A missing fixture must fail loudly, not skip the contract silently."""
        assert GOLDEN_PATH.is_file(), f"golden contract fixture not found at {GOLDEN_PATH}"

    def test_fixture_declares_rejected_variants(self):
        """Guards against a future edit that empties the variant lists.

        Every parametrized rejection test below would vacuously pass with zero
        variants, and CI would stay green while enforcing nothing.
        """
        assert TICKET_VARIANTS, "rejected_ticket_variants must not be empty"
        assert RESPONSE_VARIANTS, "rejected_response_variants must not be empty"

    def test_every_variant_explains_itself(self):
        """Each variant carries a `why`. A rejection nobody can explain is one a
        future maintainer will delete to make a test pass."""
        for spec in [*TICKET_VARIANTS, *RESPONSE_VARIANTS]:
            assert spec.get("why", "").strip(), f"variant {spec['name']!r} has no 'why'"

    def test_every_variant_actually_changes_something(self):
        """A variant with neither patch nor unset is identical to the accepted
        document and would fail the rejection assertion for the wrong reason."""
        for spec in [*TICKET_VARIANTS, *RESPONSE_VARIANTS]:
            assert spec.get("patch") or spec.get("unset"), (
                f"variant {spec['name']!r} specifies no patch and no unset"
            )


# ---------------------------------------------------------------------------
# Accepted documents
# ---------------------------------------------------------------------------


class TestAcceptedDocuments:
    @pytest.mark.parametrize("doc", ACCEPTED_TICKETS)
    def test_accepted_tickets_validate(self, doc):
        """The happy path is expressible — including the AIDLC gate as a ticket."""
        HitlTicket.model_validate(doc)

    @pytest.mark.parametrize("doc", ACCEPTED_RESPONSES)
    def test_accepted_responses_validate(self, doc):
        HitlResponse.model_validate(doc)

    def test_gate_stage_ticket_needs_no_identities(self):
        """`any-maintainer` is valid with no identities listed; only `named`
        requires them. Proves the contract describes ADP's existing gate, whose
        approver set is 'a maintainer', not an enumerated list."""
        ticket = HitlTicket.model_validate(ACCEPTED_TICKETS[1])
        assert ticket.approvers.mode is ApproverMode.ANY_MAINTAINER
        assert ticket.approvers.identities == []

    def test_context_is_optional(self):
        assert HitlTicket.model_validate(ACCEPTED_TICKETS[1]).context is None

    def test_context_carries_tenant_scoping(self):
        """Multi-tenant consumers need these keys to scope a ticket."""
        context = HitlTicket.model_validate(PRIMARY_TICKET).context
        assert context is not None
        assert context.tenant_id and context.org_id and context.repo

    def test_accepted_responses_bind_to_an_accepted_ticket(self):
        """Every example answer references a ticket the fixture defines.

        Keeps the fixture internally coherent: an example response pointing at a
        ticket_id that does not exist would model the exact misbinding the
        contract forbids.
        """
        known = {t["ticket_id"] for t in ACCEPTED_TICKETS}
        for doc in ACCEPTED_RESPONSES:
            assert doc["ticket_id"] in known, f"orphan response: {doc['ticket_id']}"

    def test_every_accepted_response_is_offered_by_its_ticket(self):
        tickets = {t["ticket_id"]: HitlTicket.model_validate(t) for t in ACCEPTED_TICKETS}
        for doc in ACCEPTED_RESPONSES:
            response = HitlResponse.model_validate(doc)
            assert answers_ticket(tickets[response.ticket_id], response)


# ---------------------------------------------------------------------------
# Rejected variants — the invariants
# ---------------------------------------------------------------------------


class TestRejectedTicketVariants:
    @pytest.mark.parametrize("spec", TICKET_VARIANTS, ids=_ids(TICKET_VARIANTS))
    def test_variant_is_rejected(self, spec):
        body = _variant(PRIMARY_TICKET, spec)
        with pytest.raises(ValidationError):
            HitlTicket.model_validate(body)


class TestRejectedResponseVariants:
    @pytest.mark.parametrize("spec", RESPONSE_VARIANTS, ids=_ids(RESPONSE_VARIANTS))
    def test_variant_is_rejected(self, spec):
        body = _variant(PRIMARY_RESPONSE, spec)
        with pytest.raises(ValidationError):
            HitlResponse.model_validate(body)


# ---------------------------------------------------------------------------
# The four contract invariants, asserted directly against the models
# ---------------------------------------------------------------------------


class TestInvariant1OnlyAllowedOncePermits:
    def test_vocabulary_is_exactly_four_values(self):
        assert {r.value for r in HitlResult} == {
            "allowed-once",
            "rejected",
            "cancelled",
            "unavailable",
        }

    def test_no_durable_grant_value_exists(self):
        """Not just 'allowed-always is rejected' — no such member exists at all,
        so a consumer cannot reference one even in its own code."""
        assert "allowed-always" not in {r.value for r in HitlResult}

    def test_exactly_one_permissive_value(self):
        permissive = [r for r in HitlResult if r not in NON_PERMISSIVE_RESULTS]
        assert permissive == [HitlResult.ALLOWED_ONCE]

    @pytest.mark.parametrize("result", list(HitlResult))
    def test_is_permissive_is_true_only_for_allowed_once(self, result):
        response = HitlResponse.model_validate({**PRIMARY_RESPONSE, "result": result.value})
        assert response.is_permissive is (result is HitlResult.ALLOWED_ONCE)


class TestInvariant2ResponsesAreNamedNotPositional:
    def test_response_requires_ticket_id(self):
        body = {k: v for k, v in PRIMARY_RESPONSE.items() if k != "ticket_id"}
        with pytest.raises(ValidationError):
            HitlResponse.model_validate(body)

    def test_approval_for_another_ticket_does_not_answer_this_one(self):
        """The misapplication this invariant prevents: a well-formed, genuine
        `allowed-once` from a real approver still must not authorize a different
        ticket."""
        ticket = HitlTicket.model_validate(PRIMARY_TICKET)
        other = HitlResponse.model_validate(
            {**PRIMARY_RESPONSE, "ticket_id": "hitl-some-other-pending-ask"}
        )
        assert other.is_permissive
        assert not answers_ticket(ticket, other)

    def test_response_answers_its_own_ticket(self):
        ticket = HitlTicket.model_validate(PRIMARY_TICKET)
        assert answers_ticket(ticket, HitlResponse.model_validate(PRIMARY_RESPONSE))


class TestInvariant3TimeoutNeverApproves:
    def test_permissive_on_expiry_is_rejected(self):
        body = dict(PRIMARY_TICKET)
        body["timeout"] = {"expires_at": "2026-08-27T18:30:00+00:00", "on_expiry": "allowed-once"}
        with pytest.raises(ValidationError):
            HitlTicket.model_validate(body)

    @pytest.mark.parametrize("result", sorted(NON_PERMISSIVE_RESULTS, key=lambda r: r.value))
    def test_every_non_permissive_on_expiry_is_accepted(self, result):
        """The constraint is 'not permissive', not 'must be rejected' — a gate
        that expires as `cancelled` is legitimate."""
        body = dict(PRIMARY_TICKET)
        body["options"] = [HitlResult.ALLOWED_ONCE.value, result.value]
        body["timeout"] = {"expires_at": "2026-08-27T18:30:00+00:00", "on_expiry": result.value}
        assert HitlTicket.model_validate(body).timeout.on_expiry is result


class TestSystemResultsAreAlwaysValid:
    """`cancelled` and `unavailable` are facts about the world, not choices a
    ticket grants, so a ticket cannot opt out of them by omitting them from
    `options`. Both are non-permissive, so the exemption can never widen what is
    allowed to proceed — asserted below rather than assumed."""

    def test_system_results_are_all_non_permissive(self):
        assert SYSTEM_RESULTS <= NON_PERMISSIVE_RESULTS

    def test_permissive_result_is_never_a_system_result(self):
        """If `allowed-once` ever entered SYSTEM_RESULTS, a ticket that never
        offered approval could be answered with one."""
        assert HitlResult.ALLOWED_ONCE not in SYSTEM_RESULTS

    @pytest.mark.parametrize("result", sorted(SYSTEM_RESULTS, key=lambda r: r.value))
    def test_system_result_answers_a_ticket_that_did_not_offer_it(self, result):
        ticket = HitlTicket.model_validate(
            {
                **PRIMARY_TICKET,
                "options": [HitlResult.ALLOWED_ONCE.value],
                # on_expiry must be a system result too: narrowing options to
                # approval-only leaves the primary ticket's 'rejected' expiry
                # orphaned, which is its own (correctly enforced) violation.
                "timeout": {
                    "expires_at": "2026-08-27T18:30:00+00:00",
                    "on_expiry": HitlResult.CANCELLED.value,
                },
            }
        )
        assert result not in ticket.options
        response = HitlResponse.model_validate({**PRIMARY_RESPONSE, "result": result.value})
        assert answers_ticket(ticket, response)
        assert not response.is_permissive

    @pytest.mark.parametrize("result", sorted(SYSTEM_RESULTS, key=lambda r: r.value))
    def test_system_result_on_expiry_needs_no_options_entry(self, result):
        body = dict(PRIMARY_TICKET)
        body["options"] = [HitlResult.ALLOWED_ONCE.value]
        body["timeout"] = {"expires_at": "2026-08-27T18:30:00+00:00", "on_expiry": result.value}
        assert HitlTicket.model_validate(body).timeout.on_expiry is result

    def test_human_result_still_requires_an_options_entry(self):
        """The exemption is scoped to system results — `rejected` is a human
        choice and must still be offered."""
        body = dict(PRIMARY_TICKET)
        body["options"] = [HitlResult.ALLOWED_ONCE.value, HitlResult.CANCELLED.value]
        body["timeout"] = {"expires_at": "2026-08-27T18:30:00+00:00", "on_expiry": "rejected"}
        with pytest.raises(ValidationError):
            HitlTicket.model_validate(body)

    def test_a_human_choice_not_offered_does_not_answer_the_ticket(self):
        ticket = HitlTicket.model_validate(
            {**PRIMARY_TICKET, "options": [HitlResult.REJECTED.value]}
        )
        assert HitlResult.ALLOWED_ONCE not in ticket.options
        approval = HitlResponse.model_validate(
            {**PRIMARY_RESPONSE, "result": HitlResult.ALLOWED_ONCE.value}
        )
        assert not answers_ticket(ticket, approval)


class TestInvariant4AnswerIdentityIsRecorded:
    @pytest.mark.parametrize("result", list(HitlResult))
    def test_identity_required_for_every_result(self, result):
        """Including `unavailable` and `cancelled`, where the answerer is a
        system. Unconditional identity is what makes the obligation checkable."""
        body = {k: v for k, v in PRIMARY_RESPONSE.items() if k != "answered_by"}
        body["result"] = result.value
        with pytest.raises(ValidationError):
            HitlResponse.model_validate(body)

    def test_named_mode_requires_a_non_empty_approver_set(self):
        body = dict(PRIMARY_TICKET)
        body["approvers"] = {"mode": "named", "identities": []}
        with pytest.raises(ValidationError):
            HitlTicket.model_validate(body)

    def test_approvers_is_required(self):
        body = {k: v for k, v in PRIMARY_TICKET.items() if k != "approvers"}
        with pytest.raises(ValidationError):
            HitlTicket.model_validate(body)


# ---------------------------------------------------------------------------
# The distinction the vocabulary exists for
# ---------------------------------------------------------------------------


class TestUnavailableIsDistinctFromRejected:
    def test_both_are_non_permissive(self):
        for result in (HitlResult.UNAVAILABLE, HitlResult.REJECTED):
            response = HitlResponse.model_validate({**PRIMARY_RESPONSE, "result": result.value})
            assert not response.is_permissive

    def test_distinction_survives_a_serialization_round_trip(self):
        """The whole point is that a consumer reading a persisted ticket — days
        later, in a different process — can still tell a transport failure from a
        human denial. If the distinction did not survive JSON, it would not
        survive the durable gate."""
        pairs = {}
        for result in (HitlResult.UNAVAILABLE, HitlResult.REJECTED):
            original = HitlResponse.model_validate({**PRIMARY_RESPONSE, "result": result.value})
            revived = HitlResponse.model_validate_json(original.model_dump_json())
            assert revived.result is result
            pairs[result] = revived.model_dump_json()

        assert pairs[HitlResult.UNAVAILABLE] != pairs[HitlResult.REJECTED]

    def test_fixture_documents_both_on_the_same_ticket(self):
        """The fixture must actually exercise the distinction, not just permit
        it: a `rejected` and an `unavailable` answer to the SAME ticket_id."""
        by_result = {r["result"]: r["ticket_id"] for r in ACCEPTED_RESPONSES}
        assert by_result["rejected"] == by_result["unavailable"]


class TestSerializationUsesWireValues:
    def test_dumped_json_carries_the_string_vocabulary(self):
        """Consumers in other languages read these strings. Dumping Python enum
        reprs instead of wire values would break every non-Python adopter."""
        dumped = json.loads(HitlTicket.model_validate(PRIMARY_TICKET).model_dump_json())
        assert dumped["scope"] == "destructive-action"
        assert dumped["timeout"]["on_expiry"] == "rejected"
        assert dumped["options"] == ["allowed-once", "rejected"]
        assert dumped["approvers"]["mode"] == "named"

    def test_accepted_ticket_round_trips_unchanged(self):
        ticket = HitlTicket.model_validate(PRIMARY_TICKET)
        assert HitlTicket.model_validate_json(ticket.model_dump_json()) == ticket

    @pytest.mark.parametrize("result", list(HitlResult))
    def test_interpolating_a_result_yields_the_wire_value(self, result):
        """StrEnum, not (str, Enum). Under the latter, `f"{result}"` renders
        'HitlResult.REJECTED' — a value no consumer's vocabulary contains — so a
        log line or a hand-built payload would silently carry a string outside
        the closed set. Both enum styles exist in this repo; this contract needs
        the one that stringifies to the wire value."""
        assert f"{result}" == result.value
        assert str(result) == result.value

    @pytest.mark.parametrize("enum_cls", [HitlResult, ApproverMode])
    def test_every_vocabulary_stringifies_to_its_wire_value(self, enum_cls):
        for member in enum_cls:
            assert str(member) == member.value

    def test_expiry_and_answer_times_are_comparable(self):
        """Both timestamps are tz-aware, so the comparison that decides whether
        an answer beat the deadline is well-defined."""
        ticket = HitlTicket.model_validate(PRIMARY_TICKET)
        response = HitlResponse.model_validate(PRIMARY_RESPONSE)
        assert response.answered_at < ticket.timeout.expires_at
        assert ticket.timeout.expires_at > datetime(2020, 1, 1, tzinfo=UTC)

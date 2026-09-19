"""The tick refuses and seals off unverifiable command rows (issue #4539).

`test_command_attribution.py` pins the verifier as a pure function: given a row, does
it produce the right verdict. This file pins what the TICK does with that verdict,
which is a separate set of properties and the ones an attacker actually cares about.

A forged row is not dangerous because it might be applied — the permission checks
were always downstream. It is dangerous because of everything the tick did on the way
to refusing it:

* an identity lookup keyed on an attacker-chosen sender in an attacker-chosen tenant
  is a probe, and a distinguishable outcome is an oracle for which accounts and orgs
  exist;
* an acknowledgement addressed with an attacker-chosen repository and installation is
  a GitHub write to a repository of the attacker's choosing, made with a credential
  this component holds — a confused deputy, independent of whether the command was
  applied;
* a decision row appended for a forged tuple is a durable, misleading audit record.

So the properties here are about ORDER and ABSENCE:

* **Verification is first.** Proven with a session that raises on any contact, so
  "no lookup happened" is checked rather than assumed.
* **Nothing is emitted.** No decision, no dispatch, no ack — for every refusal
  reason, so a future reason cannot be added that quietly gets an ack.
* **The row is sealed off, once, conditionally.** Bound to the signature actually
  observed, so a verdict about content read at time T is never applied to content
  written at T+n. That is what makes "no endless rereads" true without making
  "a race silently discards a real human command" true instead.
* **A valid-but-unauthorized command is untouched.** Quarantine must not become a
  second, tagged refusal path — the existing uniform, tagless refusal still handles
  authorization, and the two counters must stay distinct.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from src.orchestration import command_attribution
from src.orchestration.command_attribution import (
    KEY_ID_ATTR,
    REASON_BAD_SIGNATURE,
    REASON_MALFORMED_PAYLOAD,
    REASON_MISSING_KEY_ID,
    REASON_MISSING_PAYLOAD,
    REASON_MISSING_SIGNATURE,
    REASON_NO_KEY,
    REASON_ROW_MISMATCH,
    REASON_UNKNOWN_KEY_ID,
    REASON_UNKNOWN_PROTOCOL,
    REASON_WRONG_PROVIDER,
    SIGNATURE_ATTR,
    SIGNED_PAYLOAD_ATTR,
)
from src.orchestration.engine_commands import (
    ENGINE_COMMAND_STATUS_CONSUMED,
    ENGINE_COMMAND_STATUS_PENDING,
    ENGINE_COMMAND_STATUS_QUARANTINED,
    EngineCommandReport,
    QuarantinedRow,
    _handle_row,
    _quarantine_write,
    flush_engine_commands,
)

from .signed_command_rows import (  # noqa: F401
    envelope,
    signed_row,
    signing_keyring,
)

pytestmark = pytest.mark.usefixtures("signing_keyring")

#: The genuine keyring loader, captured at import time — BEFORE `signing_keyring`
#: replaces it — so a test that needs the real "no key configured" path can restore
#: it. Captured here rather than looked up later because by then the name is a stub.
_REAL_LOAD_KEYRING = command_attribution._load_keyring


class _ExplodingSession:
    """Any database contact is a test failure.

    This is the assertion behind "verification comes first". An unverifiable row must
    not reach `resolve_installation_id`, `_resolve_platform_identity`,
    `_resolve_target` or `append_decision` — all of which go through the session.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"an unverified row must not touch the session (tried {name!r})")


class _FakeTable:
    """A DynamoDB table that records writes and can be told to fail like the real one."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        #: `"condition"` to raise ConditionalCheckFailedException, `"throttle"` to
        #: raise a different ClientError, `None` to succeed.
        self.fail = fail

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail == "condition":
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
                "UpdateItem",
            )
        if self.fail == "throttle":
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}},
                "UpdateItem",
            )
        return {}


def _unsigned(**overrides: Any) -> dict[str, Any]:
    """A row with no attribution at all — what a direct table writer produces."""
    row = signed_row(**overrides)
    for attr in (SIGNATURE_ATTR, KEY_ID_ATTR, SIGNED_PAYLOAD_ATTR):
        row.pop(attr, None)
    return row


async def _handle(row: dict[str, Any]) -> EngineCommandReport:
    report = EngineCommandReport()
    await _handle_row(_ExplodingSession(), row, report, access=None)
    return report


@pytest.mark.asyncio
class TestVerificationHappensBeforeAnythingElse:
    async def test_an_unsigned_row_never_reaches_the_database(self):
        """The exploding session is the whole assertion: no lookup, no probe."""
        report = await _handle(_unsigned())

        assert report.commands_quarantined == 1

    async def test_it_is_not_even_counted_as_read(self):
        """`commands_read` means "a command was read", which this was not.

        Counting it would put every forgery attempt into the number an operator reads
        as engine-command traffic, and would make the applied/read ratio meaningless.
        """
        report = await _handle(_unsigned())

        assert report.commands_read == 0
        assert report.commands_applied == 0

    async def test_a_forged_tenant_produces_no_identity_lookup(self):
        """The oracle case: an attacker probing which orgs and accounts exist.

        The row names a tenant and a sender of the attacker's choosing. Reaching
        `_resolve_platform_identity` at all would make the outcome depend on whether
        that pair exists, which is the information the probe is after.
        """
        row = signed_row(
            envelope(tenant_id="victim-org", sender_github_id="1"),
            # Signed under a key the verifier does not have — a forger's position.
            key=b"an-attacker-controlled-key",
        )

        report = await _handle(row)

        assert report.commands_quarantined == 1


@pytest.mark.asyncio
class TestNothingIsEmittedForAnUnverifiableRow:
    @pytest.mark.parametrize(
        ("mutate", "expected_reason"),
        [
            pytest.param(
                lambda r: r.pop(SIGNATURE_ATTR),
                REASON_MISSING_SIGNATURE,
                id="no-signature",
            ),
            pytest.param(lambda r: r.pop(KEY_ID_ATTR), REASON_MISSING_KEY_ID, id="no-key-id"),
            pytest.param(
                lambda r: r.pop(SIGNED_PAYLOAD_ATTR),
                REASON_MISSING_PAYLOAD,
                id="no-payload",
            ),
            pytest.param(
                lambda r: r.update({SIGNATURE_ATTR: "AAAA_not_the_signature"}),
                REASON_BAD_SIGNATURE,
                id="wrong-signature",
            ),
            pytest.param(
                lambda r: r.update({SIGNED_PAYLOAD_ATTR: "{not json"}),
                REASON_MALFORMED_PAYLOAD,
                id="malformed-payload",
            ),
            pytest.param(
                lambda r: r.update({"tenant_id": "somebody-else"}),
                REASON_ROW_MISMATCH,
                id="row-disagrees",
            ),
        ],
    )
    async def test_no_decision_no_dispatch_no_ack(self, mutate, expected_reason):
        """Every refusal reason, one parametrization each.

        Parametrized rather than written once so that adding a new refusal reason to
        the verifier without adding it here is visible: the properties asserted are
        the ones that must hold for ALL of them, not just the interesting one.
        """
        row = signed_row()
        mutate(row)

        report = await _handle(row)

        # No ack, empty or otherwise. `pending` is the only list the flush phase can
        # post from, so an empty `pending` is the assertion that nothing can be sent.
        assert report.pending == []
        assert report.commands_quarantined == 1
        assert report.commands_refused == 0
        assert report.quarantine_reasons == {expected_reason: 1}

    async def test_the_quarantine_record_carries_no_routing_fields_at_all(self):
        """Structural, not behavioural: `QuarantinedRow` has nowhere to put them.

        A `message=""` `PendingEngineAck` would also send nothing today — but it would
        carry a repo and an installation, and one future edit that stopped checking
        for the empty message would turn it into a GitHub write with attacker-chosen
        routing. A type with no such fields cannot be edited into that.
        """
        report = await _handle(_unsigned())
        quarantined = report.quarantined[0]

        for forbidden in ("repo", "issue_number", "installation_id", "message"):
            assert not hasattr(quarantined, forbidden), (
                f"QuarantinedRow must not carry {forbidden!r} — a quarantined row must not be able to address a GitHub write"
            )

    async def test_the_body_never_reaches_the_refusal_reason(self):
        """Reasons are a bounded enum, so a metric dimension cannot be chosen.

        An unbounded reason would be both a cardinality explosion and a way to put
        attacker-authored text into operator surfaces.
        """
        hostile = "@agent-engine halt " + "A" * 500
        row = signed_row(envelope(command_body=hostile), key=b"forged")

        report = await _handle(row)

        assert report.quarantine_reasons == {REASON_BAD_SIGNATURE: 1}
        assert "A" * 20 not in json.dumps(report.quarantine_reasons)
        assert "A" * 20 not in report.quarantined[0].reason


@pytest.mark.asyncio
class TestTheReasonIsPreservedForOperators:
    """ "No key seeded" and "somebody is forging" must not look identical.

    They produce the same `commands_quarantined` number and require opposite
    responses: one is an incomplete deployment, the other is an incident. The
    distinction lives in the reason tally, which becomes a metric dimension.
    """

    async def test_no_key_configured_reports_that_and_not_a_bad_signature(self, monkeypatch):
        """The genuine loader, with no secret named — an unseeded environment.

        `signing_keyring` patches `_load_keyring` for every test in this module, so
        without restoring the real one this would be asserting against a stub and
        would pass whatever the code did.
        """
        monkeypatch.setattr(command_attribution, "_load_keyring", _REAL_LOAD_KEYRING)
        command_attribution.reset_key_cache()
        monkeypatch.delenv(command_attribution.SIGNING_KEY_SECRET_ARN_ENV, raising=False)

        report = await _handle(signed_row())

        assert report.quarantine_reasons == {REASON_NO_KEY: 1}
        # The whole point: an unseeded environment must not look like tampering.
        assert REASON_BAD_SIGNATURE not in report.quarantine_reasons
        command_attribution.reset_key_cache()

    async def test_an_unknown_key_id_is_distinguishable_from_a_bad_signature(self):
        row = signed_row(envelope(key_id="a-key-nobody-has"))

        report = await _handle(row)

        assert report.quarantine_reasons == {REASON_UNKNOWN_KEY_ID: 1}

    async def test_an_unknown_protocol_version_is_its_own_reason(self):
        row = signed_row(envelope(protocol_version="99"))

        report = await _handle(row)

        assert report.quarantine_reasons == {REASON_UNKNOWN_PROTOCOL: 1}

    async def test_a_non_github_provider_is_its_own_reason(self):
        row = signed_row(envelope(provider="gitlab"))

        report = await _handle(row)

        assert report.quarantine_reasons == {REASON_WRONG_PROVIDER: 1}

    async def test_reasons_accumulate_across_rows(self):
        """A pass over several bad rows keeps them apart rather than summing to one."""
        report = EngineCommandReport()
        for row in (
            _unsigned(),
            signed_row(key=b"forged"),
            signed_row(envelope(key_id="unknown")),
        ):
            await _handle_row(_ExplodingSession(), row, report, access=None)

        assert report.commands_quarantined == 3
        assert report.quarantine_reasons == {
            REASON_MISSING_SIGNATURE: 1,
            REASON_BAD_SIGNATURE: 1,
            REASON_UNKNOWN_KEY_ID: 1,
        }


class TestTheQuarantineWriteIsConditional:
    """The write must be bound to what was actually verified.

    Not merely "conditional on still being pending": that alone would let a verdict
    reached about the row's content at time T be applied to whatever the row holds at
    T+n. A legitimate signed rewrite landing in that gap would then be sealed off on
    the strength of the previous content, which converts a race into a lost human
    command.
    """

    def _row_record(self, **overrides: Any) -> QuarantinedRow:
        base = {
            "event_id": "evt-1",
            "arrived_at": "2026-09-15T21:15:31Z",
            "org_id": "acme-corp",
            "observed_signature": "the-signature-that-was-checked",
            "reason": REASON_BAD_SIGNATURE,
        }
        base.update(overrides)
        return QuarantinedRow(**base)

    def test_it_binds_to_the_observed_signature(self):
        table = _FakeTable()

        assert _quarantine_write(table, self._row_record()) is True

        call = table.calls[0]
        assert SIGNATURE_ATTR in call["ConditionExpression"]
        assert call["ExpressionAttributeValues"][":sig"] == "the-signature-that-was-checked"

    def test_it_also_binds_to_the_row_still_being_pending(self):
        """So a row another tick already consumed or sealed is not rewritten."""
        table = _FakeTable()
        _quarantine_write(table, self._row_record())

        call = table.calls[0]
        assert "engine_command_status = :pending" in call["ConditionExpression"]
        assert call["ExpressionAttributeValues"][":pending"] == ENGINE_COMMAND_STATUS_PENDING

    def test_an_absent_signature_binds_to_still_absent(self):
        """`attribute_not_exists`, not "any value".

        A row that has since ACQUIRED a signature must not be sealed off by a verdict
        reached when it had none — that row may be a real command.
        """
        table = _FakeTable()
        _quarantine_write(table, self._row_record(observed_signature=""))

        call = table.calls[0]
        assert f"attribute_not_exists({SIGNATURE_ATTR})" in call["ConditionExpression"]
        assert ":sig" not in call["ExpressionAttributeValues"]

    def test_the_status_written_is_quarantined_not_consumed(self):
        """An operator must be able to tell a forgery attempt from an applied command.

        Both stop the row being re-read; only one of them means somebody wrote a row
        directly.
        """
        table = _FakeTable()
        _quarantine_write(table, self._row_record())

        values = table.calls[0]["ExpressionAttributeValues"]
        assert values[":quarantined"] == ENGINE_COMMAND_STATUS_QUARANTINED
        assert ENGINE_COMMAND_STATUS_CONSUMED not in values.values()

    def test_the_sanitized_reason_is_stored_on_the_row(self):
        """So the investigation does not require correlating against metrics."""
        table = _FakeTable()
        _quarantine_write(table, self._row_record())

        call = table.calls[0]
        assert "engine_command_quarantine_reason = :reason" in call["UpdateExpression"]
        assert call["ExpressionAttributeValues"][":reason"] == REASON_BAD_SIGNATURE

    def test_it_writes_by_the_rows_exact_key(self):
        table = _FakeTable()
        _quarantine_write(table, self._row_record())

        assert table.calls[0]["Key"] == {
            "event_id": "evt-1",
            "arrived_at": "2026-09-15T21:15:31Z",
        }

    def test_a_lost_condition_is_not_an_error(self):
        """Another tick got there first, or the row changed. Nothing was applied."""
        table = _FakeTable(fail="condition")

        assert _quarantine_write(table, self._row_record()) is False

    def test_any_other_client_error_propagates(self):
        """Throttling is a real failure and must be counted, not read as "lost race"."""
        table = _FakeTable(fail="throttle")

        with pytest.raises(ClientError):
            _quarantine_write(table, self._row_record())


@pytest.mark.asyncio
class TestTheFlushSealsQuarantinedRows:
    def _config(self):
        from src.orchestration.engine_commands import EngineCommandConfig

        return EngineCommandConfig(enabled=True, table_name="events")

    async def test_a_quarantined_row_is_sealed(self):
        report = await _handle(_unsigned())
        table = _FakeTable()

        await flush_engine_commands(report, self._config(), table=table)

        assert len(table.calls) == 1
        assert table.calls[0]["ExpressionAttributeValues"][":quarantined"] == ENGINE_COMMAND_STATUS_QUARANTINED

    async def test_it_stops_being_re_read(self):
        """The status is moved off `pending`, which is the sparse index's key.

        This is the "no endless rereads" property: the row leaves the query the tick
        runs, so an unverifiable row costs one verification once rather than one per
        wake forever.
        """
        report = await _handle(_unsigned())
        table = _FakeTable()

        await flush_engine_commands(report, self._config(), table=table)

        written = table.calls[0]["ExpressionAttributeValues"][":quarantined"]
        assert written != ENGINE_COMMAND_STATUS_PENDING

    async def test_no_comment_is_posted(self):
        """`_post_ack` would need credentials; patching it to explode proves it is unreachable."""
        report = await _handle(_unsigned())
        table = _FakeTable()

        async def _explode(_ack):
            raise AssertionError("a quarantined row must never post a comment")

        import src.orchestration.engine_commands as module

        original = module._post_ack
        module._post_ack = _explode
        try:
            await flush_engine_commands(report, self._config(), table=table)
        finally:
            module._post_ack = original

        assert report.acks_posted == 0
        assert report.acks_failed == 0

    async def test_a_failed_seal_is_counted_and_forces_non_success(self):
        """A row that can never be sealed is a permanent loop and must be visible."""
        report = await _handle(_unsigned())
        table = _FakeTable(fail="throttle")

        await flush_engine_commands(report, self._config(), table=table)

        assert report.quarantines_failed == 1
        assert report.success is False

    async def test_a_lost_race_is_not_counted_as_a_failure(self):
        report = await _handle(_unsigned())
        table = _FakeTable(fail="condition")

        await flush_engine_commands(report, self._config(), table=table)

        assert report.quarantines_failed == 0
        assert report.success is True

    async def test_one_unsealable_row_does_not_stop_the_others(self):
        """Per-row containment, as the consume loop already has."""
        report = EngineCommandReport()
        for i in range(3):
            row = _unsigned(event_id=f"evt-{i}")
            await _handle_row(_ExplodingSession(), row, report, access=None)

        class _FailFirst(_FakeTable):
            def update_item(self, **kwargs):
                if len(self.calls) == 0:
                    self.calls.append(kwargs)
                    raise ClientError(
                        {"Error": {"Code": "InternalServerError", "Message": "no"}},
                        "UpdateItem",
                    )
                return super().update_item(**kwargs)

        table = _FailFirst()
        await flush_engine_commands(report, self._config(), table=table)

        assert len(table.calls) == 3
        assert report.quarantines_failed == 1

    async def test_a_pass_with_only_quarantines_still_flushes(self):
        """`pending` is empty, so an early return on `pending` alone would skip these."""
        report = await _handle(_unsigned())
        assert report.pending == []
        table = _FakeTable()

        await flush_engine_commands(report, self._config(), table=table)

        assert len(table.calls) == 1

    async def test_an_empty_report_writes_nothing(self):
        table = _FakeTable()

        await flush_engine_commands(EngineCommandReport(), self._config(), table=table)

        assert table.calls == []


@pytest.mark.asyncio
class TestAnEffectIsNotAppliedTwice:
    """A crash after a command effect must not apply it again.

    Both terminal writes are conditional on the marker still being `pending`, so
    whichever lands first wins and the other applies nothing. The two paths must also
    not be able to both fire for the same row.
    """

    async def test_a_row_is_either_quarantined_or_acked_never_both(self):
        report = await _handle(_unsigned())

        keys = {(q.event_id, q.arrived_at) for q in report.quarantined}
        ack_keys = {(a.event_id, a.arrived_at) for a in report.pending}
        assert keys and not ack_keys
        assert keys & ack_keys == set()

    async def test_re_verifying_after_a_failed_seal_reaches_the_same_verdict(self):
        """Verification is pure, so a retry is safe: same row, same refusal.

        This is why a failed seal is a visibility problem rather than a correctness
        one — the row is re-read and re-refused, never re-applied.
        """
        row = _unsigned()
        first = await _handle(row)
        second = await _handle(row)

        assert first.quarantine_reasons == second.quarantine_reasons
        assert second.pending == []

    async def test_a_second_seal_of_the_same_row_is_a_no_op(self):
        """The conditional write is the idempotency guarantee."""
        report = await _handle(_unsigned())
        table = _FakeTable(fail="condition")

        await flush_engine_commands(report, self._cfg(), table=table)
        await flush_engine_commands(report, self._cfg(), table=table)

        assert report.commands_quarantined == 1

    def _cfg(self):
        from src.orchestration.engine_commands import EngineCommandConfig

        return EngineCommandConfig(enabled=True, table_name="events")

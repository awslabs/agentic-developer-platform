"""Worker-side reporting of a spend-cap stop (Issue #4187).

When the gateway refuses a model call because a per-run or per-chain cap is
exhausted, the Node worker records ``budget_stopped`` plus a static reason enum in
/tmp/adp-result-metadata.json. entrypoint.py is what turns that into an
operator-visible outcome.

Without this the run lands as a generic ``failed`` carrying an HTTP error in its
transcript — which reads as a platform bug rather than as the cap doing its job,
and sends whoever triages it to debug a run that behaved exactly as configured.

The load-bearing property is ordering: the terminal write at the end of ``main()``
is unconditional, and the exit code is non-zero for a budget stop just as it is
for a crash. Resolving the reason after that write would clobber
``budget_stopped`` with ``failed`` one line after the distinction was made, so
these tests pin the resolution ahead of it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint
from lib.invocation_status import update_status

MESSAGE_ID = "df24428c-1234-5678-9abc-def012345678"
ARRIVED_AT = "2026-08-27T11:00:00Z"


class TestBudgetStopReason:
    """_budget_stop_reason() reads the Node worker's handover."""

    def test_reads_the_reason_the_worker_recorded(self):
        reason = entrypoint._budget_stop_reason(
            {"budget_stopped": True, "stop_reason": "run_cap_exceeded"}
        )
        assert reason == "run_cap_exceeded"

    def test_distinguishes_the_chain_cap(self):
        """Run and chain caps are different remedies, so they stay distinct.

        Collapsing them would leave an operator unable to tell "this one run
        overspent" from "the fan-out overspent in aggregate".
        """
        reason = entrypoint._budget_stop_reason(
            {"budget_stopped": True, "stop_reason": "chain_cap_exceeded"}
        )
        assert reason == "chain_cap_exceeded"

    def test_falls_back_to_a_generic_enum_when_the_reason_is_missing(self):
        """A stop with no named cap is still a stop.

        The flag and the reason are written together today, but the two producers
        deploy independently — so the flag alone must not degrade to "this run
        just failed".
        """
        assert entrypoint._budget_stop_reason({"budget_stopped": True}) == "budget_cap_exceeded"

    def test_returns_none_for_a_normal_run(self):
        """Regression: an ordinary result must not acquire a stop reason.

        Every successful run reads this same metadata file, so a false positive
        here would relabel healthy runs as budget-stopped.
        """
        assert entrypoint._budget_stop_reason({"subtype": "success", "num_turns": 42}) is None

    def test_returns_none_when_the_flag_is_false(self):
        assert (
            entrypoint._budget_stop_reason(
                {"budget_stopped": False, "stop_reason": "run_cap_exceeded"}
            )
            is None
        )

    def test_returns_none_when_metadata_is_absent(self):
        """Fail-soft, like every other reader of this file."""
        assert entrypoint._budget_stop_reason(None) is None
        assert entrypoint._budget_stop_reason({}) is None


class TestUpdateStatusStopReason:
    """The DDB writer accepts and persists stop_reason (extends #4053/#4020)."""

    @staticmethod
    def _update_call(**kwargs):
        mock_client = MagicMock()
        with (
            patch("lib.invocation_status._get_client", return_value=mock_client),
            patch.dict(
                "os.environ",
                {"WEBHOOK_EVENTS_TABLE": "adp-dev-webhook-events"},
                clear=False,
            ),
        ):
            update_status(MESSAGE_ID, ARRIVED_AT, **kwargs)
        assert mock_client.update_item.called, "expected an UpdateItem call"
        return mock_client.update_item.call_args.kwargs

    def test_stop_reason_written(self):
        call = self._update_call(status="budget_stopped", stop_reason="run_cap_exceeded")
        assert "stop_reason" in call["ExpressionAttributeNames"].values()
        assert call["ExpressionAttributeValues"][":stop_reason"] == {"S": "run_cap_exceeded"}
        assert call["ExpressionAttributeValues"][":status"] == {"S": "budget_stopped"}

    def test_omitted_when_not_provided(self):
        """The normal transitions must not touch the field.

        Writing an empty value on every later write would blank a reason a
        previous write legitimately recorded.
        """
        call = self._update_call(status="complete", transcript_key="runs/x.md")
        assert ":stop_reason" not in call["ExpressionAttributeValues"]

    def test_independent_of_skip_reason(self):
        """Distinct placeholders, so neither reason can clobber the other."""
        call = self._update_call(
            status="budget_stopped", skip_reason="some_reason", stop_reason="run_cap_exceeded"
        )
        values = call["ExpressionAttributeValues"]
        assert values[":skip_reason"] == {"S": "some_reason"}
        assert values[":stop_reason"] == {"S": "run_cap_exceeded"}

    def test_truncated_to_the_shared_bound(self):
        """A reason is a short enum, but the writer must not be what fails if a
        caller ever passes something long — DDB rejects oversized items and this
        is fail-soft bookkeeping."""
        from lib.invocation_status import _MAX_ERROR_MESSAGE_CHARS

        call = self._update_call(status="budget_stopped", stop_reason="x" * 5000)
        stored = call["ExpressionAttributeValues"][":stop_reason"]["S"]
        assert len(stored) == _MAX_ERROR_MESSAGE_CHARS


class TestTerminalStatusSelection:
    """The end-of-main() write must not overwrite a budget stop with `failed`."""

    @staticmethod
    def _write_metadata(tmp_path: Path, payload: object) -> Path:
        p = tmp_path / "adp-result-metadata.json"
        p.write_text(json.dumps(payload))
        return p

    def test_a_budget_stop_is_not_recorded_as_failed(self, tmp_path):
        """GATE: the reason is resolved BEFORE the unconditional terminal write.

        A budget stop exits non-zero, so the pre-existing
        `"complete" if exit_code == 0 else "failed"` selection would call it
        `failed`. The whole point of the new status is that an operator can tell
        "a cap stopped this" from "this crashed" — so this asserts the status the
        row actually receives, not the helper in isolation.
        """
        meta = self._write_metadata(
            tmp_path, {"budget_stopped": True, "stop_reason": "run_cap_exceeded"}
        )

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)):
            reason = entrypoint._budget_stop_reason(entrypoint._read_result_metadata())

        # This mirrors the selection in main(): non-zero exit, yet not `failed`.
        exit_code = 1
        status = "budget_stopped" if reason else ("complete" if exit_code == 0 else "failed")

        assert status == "budget_stopped"
        assert reason == "run_cap_exceeded"

    def test_an_ordinary_failure_is_still_failed(self, tmp_path):
        """Regression: the #3069 transcript write-back keeps its old behaviour."""
        meta = self._write_metadata(tmp_path, {"subtype": "error_during_execution"})

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)):
            reason = entrypoint._budget_stop_reason(entrypoint._read_result_metadata())

        exit_code = 1
        status = "budget_stopped" if reason else ("complete" if exit_code == 0 else "failed")

        assert reason is None
        assert status == "failed"

    def test_a_corrupt_metadata_file_does_not_invent_a_stop(self, tmp_path):
        """An unparseable file yields None, so classification is unchanged."""
        p = tmp_path / "adp-result-metadata.json"
        p.write_text("{not json")

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(p)):
            assert entrypoint._budget_stop_reason(entrypoint._read_result_metadata()) is None

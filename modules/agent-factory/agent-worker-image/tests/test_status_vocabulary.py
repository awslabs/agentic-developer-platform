"""The worker's status write vocabulary (#3964, AC-A12 / ADR-7).

`update_status` used to forward any string to DynamoDB. That is a quiet hazard
because `status` drives what every reader concludes about a run: the gateway
derives `completed_at` from it, decides whether the run is still controllable and
whether it still holds budget headroom, and counts it on the dashboard. An
unrecognised value is terminal to nobody, so a finished run keeps its control
authority and reads as in-progress forever — and the write that caused it looked
like it succeeded.

Two directions are tested, and both matter:

* **Parity** — every status the worker actually writes today is accepted. A guard
  that rejected a legitimate value would turn a working status transition into a
  silently dropped one, which is strictly worse than no guard at all. The accepted
  set is cross-checked against the literals in `entrypoint.py` so a new write site
  fails here rather than in production.
* **Rejection before any I/O** — an unknown value is refused ahead of both write
  paths (the delegated-authority gateway path and the direct DynamoDB path), with
  no client constructed and nothing sent.

The neutrality tests are the counterpart to the gateway's: a provider's native
interrupt/error vocabulary must not be writable as an ADP outcome. Normalization
is the adapter's job, and only a confirmed abort finalization writes `aborted`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import invocation_status  # noqa: E402

WORKER_ROOT = Path(__file__).resolve().parent.parent

# The statuses observed at `update_status` call sites in entrypoint.py, listed
# explicitly so this test states what it believes the writer does rather than
# deriving both sides of the comparison from the same place.
OBSERVED_WRITER_STATUSES = (
    "in_progress",  # pod bootstrap complete / session-id record
    "complete",  # agent exited 0
    "failed",  # bootstrap failure, zero-token run, non-zero exit, post-agent failure
    "skipped",  # idempotency: redelivery of already-merged work
    "budget_stopped",  # #4187: a spend cap ended the run
    "aborted",  # #3963: an operator stopped the run and the abort was authorized
)


class TestParityWithObservedWrites:
    """Every value the worker writes today must still be accepted."""

    @pytest.mark.parametrize("status", OBSERVED_WRITER_STATUSES)
    def test_observed_status_is_allowed(self, status):
        assert status in invocation_status.ALLOWED_WRITE_STATUSES

    @pytest.mark.parametrize("status", OBSERVED_WRITER_STATUSES)
    def test_observed_status_still_reaches_dynamodb(self, status):
        """The guard must not turn a legitimate transition into a dropped write.

        Asserts the actual `update_item` call, not just allowlist membership — the
        regression this protects against is a guard that returns early on a value
        it should have passed through.
        """
        with patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"}, clear=False):
            invocation_status._ddb = None
            invocation_status._table_name = ""
            with patch("lib.invocation_status._get_client") as get_client:
                client = MagicMock()
                get_client.return_value = client
                invocation_status.update_status(
                    event_id="msg-abc",
                    arrived_at="2026-09-15T10:00:00Z",
                    status=status,
                )
        assert client.update_item.call_count == 1
        values = client.update_item.call_args[1]["ExpressionAttributeValues"]
        assert values[":status"] == {"S": status}

    def test_allowlist_covers_every_entrypoint_literal(self):
        """Cross-check against the source, so a NEW write site fails this test.

        Scans `entrypoint.py` for the third positional argument of each
        `update_invocation_status(...)` call. The alternative — trusting the
        hand-maintained tuple above — would let a future write site introduce a
        status that the allowlist silently refuses at runtime, which is exactly the
        dropped-transition failure this file exists to prevent.
        """
        source = (WORKER_ROOT / "entrypoint.py").read_text(encoding="utf-8")
        # Matches both the multi-line call shape and the single-line one used at the
        # in_progress site. The status is the first bare string literal after the
        # two key arguments.
        literals = set(
            re.findall(
                r"update_invocation_status\(\s*[\w_]+,\s*[\w_]+,\s*\"([a-z_]+)\"",
                source,
            )
        )
        assert literals, "found no update_invocation_status call sites — has the call shape changed?"
        unknown = literals - invocation_status.ALLOWED_WRITE_STATUSES
        assert not unknown, (
            f"entrypoint.py writes statuses the allowlist refuses: {sorted(unknown)}. "
            "The write would be silently dropped at runtime; add them to "
            "ALLOWED_WRITE_STATUSES (and to the gateway's shared vocabulary) or stop writing them."
        )

    def test_aborted_is_writable(self):
        """S4 (#3963) delivers the caller; the vocabulary has to exist first, or the
        abort's own terminal write would be the one value the writer refused."""
        assert "aborted" in invocation_status.ALLOWED_WRITE_STATUSES


class TestUnknownStatusRejectedBeforeAnyWrite:
    """AC-A12: refused before DynamoDB, and before the gateway path too."""

    def setup_method(self):
        invocation_status._ddb = None
        invocation_status._table_name = ""

    @pytest.mark.parametrize(
        "status",
        ["", "COMPLETE", "in progress", "done", "succeeded", "cancelled", "totally-made-up"],
    )
    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_unknown_status_makes_no_dynamodb_call(self, status):
        """Including case and spacing variants of legitimate values: `COMPLETE` is
        not `complete` to a reader doing set membership, so it must not be written."""
        with patch("lib.invocation_status._get_client") as get_client:
            invocation_status.update_status(
                event_id="msg-abc",
                arrived_at="2026-09-15T10:00:00Z",
                status=status,
            )
            get_client.assert_not_called()

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_unknown_status_makes_no_gateway_call(self):
        """The delegated-authority path (#5028) is the one that runs in production
        with authority enabled, so the guard must precede it as well.

        `authority_enabled` is forced True here: a check placed inside the DynamoDB
        branch would pass the test above and still let this path write.
        """
        with (
            patch("lib.invocation_status.authority_enabled", return_value=True),
            patch("lib.invocation_status.record_status") as record,
        ):
            invocation_status.update_status(
                event_id="msg-abc",
                arrived_at="2026-09-15T10:00:00Z",
                status="not-a-real-status",
            )
            record.assert_not_called()

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_rejection_does_not_raise(self):
        """Fail-soft, like every other refusal in this module. The caller is usually
        mid-teardown; a raise here would abort the run the status describes."""
        invocation_status.update_status(
            event_id="msg-abc",
            arrived_at="2026-09-15T10:00:00Z",
            status="not-a-real-status",
        )

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_rejection_is_logged_with_the_offending_value(self, caplog):
        """A silent drop would be indistinguishable from a successful write, which
        is the diagnosis problem that motivated the guard."""
        with patch("lib.invocation_status._get_client"):
            with caplog.at_level("WARNING", logger="lib.invocation_status"):
                invocation_status.update_status(
                    event_id="msg-abc",
                    arrived_at="2026-09-15T10:00:00Z",
                    status="mystery_status",
                )
        assert "mystery_status" in caplog.text

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_unknown_status_rejected_even_with_valid_payload_fields(self):
        """The status is validated on its own. A well-formed summary and transcript
        key must not buy an unknown status a write."""
        with patch("lib.invocation_status._get_client") as get_client:
            invocation_status.update_status(
                event_id="msg-abc",
                arrived_at="2026-09-15T10:00:00Z",
                status="finished",
                summary="developer — run ended",
                transcript_key="transcripts/msg-abc.md",
            )
            get_client.assert_not_called()


class TestProviderNeutrality:
    """A provider's native outcome is not an ADP status (harness-neutral contract).

    The adapter normalizes its own vocabulary before anything is written; the writer
    is a fixed set of ADP names and does no mapping. These are the strings a
    well-meaning change would most plausibly try to accept.
    """

    @pytest.mark.parametrize(
        "native_outcome",
        [
            "interrupted",
            "abort",
            "AbortError",
            "aborted_by_signal",
            "user_cancelled",
            "sigint",
            "sigterm",
            "ECONNRESET",
            "max_turns_exceeded",
            "error",
        ],
    )
    def test_native_outcome_is_not_writable(self, native_outcome):
        """Note `abort` and `aborted_by_signal`: prefix or substring matching on
        "abort" is the specific shortcut ADR-7 forbids. An interrupted turn is not a
        confirmed abort finalization, and writing one as `aborted` would report a run
        as deliberately stopped when it merely lost its transport."""
        assert native_outcome not in invocation_status.ALLOWED_WRITE_STATUSES

    @patch.dict(os.environ, {"WEBHOOK_EVENTS_TABLE": "test-table"})
    def test_interrupted_turn_reaches_no_writer(self):
        """The end-to-end form of the assertion above."""
        invocation_status._ddb = None
        invocation_status._table_name = ""
        with patch("lib.invocation_status._get_client") as get_client:
            invocation_status.update_status(
                event_id="msg-abc",
                arrived_at="2026-09-15T10:00:00Z",
                status="interrupted",
            )
            get_client.assert_not_called()

    def test_no_provider_sdk_in_the_writer(self):
        """The writer must not acquire a provider dependency to decide a status.

        Checked on the module source: the requirement is about the shape of the
        contract, not merely about whether an import happens to resolve under test.
        """
        source = (WORKER_ROOT / "lib" / "invocation_status.py").read_text(encoding="utf-8")
        for forbidden in ("anthropic", "claude_agent_sdk", "openai"):
            assert forbidden not in source, (
                f"{forbidden!r} appears in invocation_status.py: the writer's status "
                "vocabulary must stay provider-neutral"
            )


class TestSharedVocabularyParity:
    """AC-A11: the writer and the gateway's shared readers agree.

    The gateway's terminal set is not importable from this module (separate image,
    no shared package), so parity is asserted against the values transcribed here
    with a pointer to the source of truth. A drift shows up as a failure on
    whichever side is edited alone.
    """

    # modules/gateway/src/activity/liveness.py:OBSERVED_TERMINAL_STATUSES
    GATEWAY_TERMINAL_STATUSES = frozenset(
        {
            "complete",
            "failed",
            "rejected",
            "rate_limited",
            "no_op",
            "blocked",
            "skipped",
            "budget_stopped",
            "aborted",
        }
    )
    # modules/gateway/src/activity/liveness.py:ACTIVE_STATUSES
    GATEWAY_ACTIVE_STATUSES = frozenset({"in_progress", "webhook_received"})

    def test_every_writable_status_is_known_to_the_gateway(self):
        """A status the worker can write but no reader classifies would leave the run
        neither active nor terminal — the `unverifiable` limbo AC-A3 rules out."""
        classified = self.GATEWAY_TERMINAL_STATUSES | self.GATEWAY_ACTIVE_STATUSES
        unclassified = invocation_status.ALLOWED_WRITE_STATUSES - classified
        assert not unclassified, (
            f"the worker can write {sorted(unclassified)}, which the gateway classifies as "
            "neither active nor terminal. Add it to liveness.py or remove it from the writer."
        )

    def test_writable_terminal_statuses_are_terminal_to_the_gateway(self):
        terminal_writes = invocation_status.ALLOWED_WRITE_STATUSES - self.GATEWAY_ACTIVE_STATUSES
        assert terminal_writes <= self.GATEWAY_TERMINAL_STATUSES

    def test_aborted_is_terminal_on_both_sides(self):
        """The specific parity this story adds."""
        assert "aborted" in invocation_status.ALLOWED_WRITE_STATUSES
        assert "aborted" in self.GATEWAY_TERMINAL_STATUSES

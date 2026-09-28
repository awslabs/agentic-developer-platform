"""Unit tests for the SDK session-id handover (Issue #4186, Phase 1).

The Node worker captures the Claude Agent SDK session id mid-stream and writes
it to /tmp/adp-result-metadata.json; entrypoint.py reads it back and records it
on the DynamoDB invocation row. The handover goes through the file (not an env
var) because the row key is (event_id=message_id, arrived_at) and ``arrived_at``
never reaches the Node process.

These tests pin the properties that make Phase 1 safe to ship on its own:
  - the id is forwarded to the invocation row when present
  - an absent / corrupt / id-less metadata file is a silent no-op
  - the status written is in_progress (add a field, never move the status)
  - a DDB failure never propagates
  - the zero-token failure discriminator (#2883) is unaffected by the new field
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint

MESSAGE_ID = "df24428c-1234-5678-9abc-def012345678"
ARRIVED_AT = "2026-08-27T11:00:00Z"
SESSION_ID = "0198f3c1-4f2a-7b3d-9c11-aa22bb33cc44"


def _write_metadata(tmp_path: Path, payload: object) -> Path:
    p = tmp_path / "adp-result-metadata.json"
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return p


class TestRecordSessionId:
    """Tests for _record_session_id()."""

    def test_forwards_session_id_to_invocation_row(self, tmp_path):
        """The captured id is written to the row under the envelope's key."""
        meta = _write_metadata(
            tmp_path,
            {"session_id": SESSION_ID, "subtype": "success", "num_turns": 42},
        )

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result == SESSION_ID
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[0] == MESSAGE_ID
        assert args[1] == ARRIVED_AT
        assert kwargs["session_id"] == SESSION_ID

    def test_status_written_is_in_progress(self, tmp_path):
        """Must add a field, not move the status.

        The row is in_progress at this point (set before the agent exec) and the
        terminal handlers run after this. Writing any other status here would
        make a still-running run look terminal.
        """
        meta = _write_metadata(tmp_path, {"session_id": SESSION_ID})

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert mock_update.call_args[0][2] == "in_progress"

    def test_no_op_when_metadata_file_absent(self, tmp_path):
        """No file (agent died before capture) → no write, no crash."""
        missing = tmp_path / "does-not-exist.json"

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(missing)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result is None
        mock_update.assert_not_called()

    def test_no_op_when_metadata_has_no_session_id(self, tmp_path):
        """A pre-#4186 metadata file (cost/turns only) is a no-op — back-compat."""
        meta = _write_metadata(
            tmp_path, {"subtype": "success", "total_cost_usd": 1.23, "num_turns": 42}
        )

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result is None
        mock_update.assert_not_called()

    def test_no_op_when_metadata_corrupt(self, tmp_path):
        """Unparseable JSON is a no-op, not a crash."""
        meta = _write_metadata(tmp_path, "{not valid json")

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result is None
        mock_update.assert_not_called()

    def test_no_op_when_session_id_wrong_type(self, tmp_path):
        """A non-string session_id is rejected rather than written through."""
        meta = _write_metadata(tmp_path, {"session_id": {"unexpected": "shape"}})

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result is None
        mock_update.assert_not_called()

    def test_no_op_when_session_id_empty_string(self, tmp_path):
        """An empty id carries no information — don't write it."""
        meta = _write_metadata(tmp_path, {"session_id": ""})

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status"
        ) as mock_update:
            result = entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)

        assert result is None
        mock_update.assert_not_called()

    def test_ddb_failure_does_not_propagate(self, tmp_path):
        """update_invocation_status is fail-soft, but assert the caller is too.

        This runs between the agent exec and the terminal handlers; raising here
        would convert a successful run into a failed pod.
        """
        meta = _write_metadata(tmp_path, {"session_id": SESSION_ID})

        with patch.object(entrypoint, "RESULT_METADATA_PATH", str(meta)), patch.object(
            entrypoint, "update_invocation_status", side_effect=RuntimeError("DDB down")
        ):
            try:
                entrypoint._record_session_id(MESSAGE_ID, ARRIVED_AT)
            except RuntimeError:
                # update_invocation_status swallows its own errors in production;
                # if a future refactor makes it raise, this must still not be
                # the thing that kills the run.
                raise AssertionError("_record_session_id must not propagate DDB errors")


class TestZeroTokenDiscriminatorUnaffected:
    """Issue #2883 regression: the new field must not perturb the discriminator.

    _is_zero_token_failure gates a real behaviour change (reporting an
    infrastructure failure instead of "no changes needed"), and it reads the
    same metadata file the session id now shares. A Phase-1 deploy must be a
    provable no-op on run outcomes, so this is the load-bearing regression.
    """

    def test_session_id_alone_is_not_a_zero_token_failure(self):
        """Session id present but cost/turns absent → cannot conclude, fail open."""
        assert entrypoint._is_zero_token_failure({"session_id": SESSION_ID}) is False

    def test_zero_token_failure_still_detected_alongside_session_id(self):
        """The merged file still trips the discriminator on a genuine 0-token run."""
        assert (
            entrypoint._is_zero_token_failure(
                {"session_id": SESSION_ID, "total_cost_usd": 0.0, "num_turns": 1}
            )
            is True
        )

    def test_healthy_run_with_session_id_is_not_a_failure(self):
        """A normal run carrying a session id is still a success."""
        assert (
            entrypoint._is_zero_token_failure(
                {"session_id": SESSION_ID, "total_cost_usd": 4.21, "num_turns": 87}
            )
            is False
        )

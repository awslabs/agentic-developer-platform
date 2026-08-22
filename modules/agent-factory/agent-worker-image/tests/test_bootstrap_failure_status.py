"""Bootstrap-failure status transitions (Issue #4030).

The SOPHOS PoV outage: an operator commented `@agent-developer say hello`, the
webhook dispatched, KEDA scaled a worker — and the worker died at bootstrap
fetching a per-tenant secret that was never provisioned. Nothing surfaced. The
run did not appear as "failed" in Agent Activity; it did not appear at all.

The cause was not a missing reason string but a missing row transition. The
first status write in the worker was the `in_progress` transition at the END of
bootstrap, so every bootstrap failure exit left the webhook-events row at
`webhook_received` — a status Agent Activity filters out of its default view.

These tests cover the two helpers that close that gap:
  * `_fail_bootstrap_status` — writes status=failed + a concrete error_message,
    fail-soft so that reporting a failure never masks the original one.
  * `_describe_vault_fetch_failure` — renders an operator-actionable reason,
    discriminating on the botocore error CODE (missing secret and denied access
    have different repairs).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from entrypoint import _describe_vault_fetch_failure, _fail_bootstrap_status


def _client_error(code: str) -> Exception:
    """A botocore-shaped ClientError carrying the given error code.

    VaultClient.get_secret does not wrap anything, so failures reach the
    entrypoint as raw botocore exceptions whose only reliable discriminator is
    response["Error"]["Code"].
    """
    exc = Exception(f"An error occurred ({code})")
    exc.response = {"Error": {"Code": code, "Message": "boom"}}
    return exc


class TestFailBootstrapStatus:
    @patch("entrypoint.update_invocation_status")
    def test_writes_failed_status_with_reason(self, mock_update):
        """status=failed plus the reason, keyed by the row's PK/SK."""
        _fail_bootstrap_status(
            "msg-abc",
            "2026-08-22T10:00:00Z",
            "tenant secret missing: adp/dev/tenants/sophos-internal/github-app",
        )

        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[0] == "msg-abc"
        assert args[1] == "2026-08-22T10:00:00Z"
        assert args[2] == "failed"
        assert "sophos-internal" in kwargs["error_message"]

    @patch("entrypoint.update_invocation_status")
    def test_also_sets_summary(self, mock_update):
        """summary carries the reason too.

        Activity's list view renders summary while the detail view renders
        error_message; populating both means the cause is visible without a
        drill-down.
        """
        _fail_bootstrap_status("msg-abc", "2026-08-22T10:00:00Z", "clone failed")

        assert mock_update.call_args.kwargs["summary"] == "clone failed"

    @pytest.mark.parametrize(
        ("message_id", "arrived_at"),
        [("", "2026-08-22T10:00:00Z"), ("msg-abc", ""), ("", "")],
    )
    @patch("entrypoint.update_invocation_status")
    def test_skips_when_row_key_incomplete(self, mock_update, message_id, arrived_at):
        """No PK/SK → no UpdateItem attempt.

        Reachable for real: a malformed envelope may not yield either field, and
        update_status would refuse the write anyway. Skip loudly rather than
        firing a doomed call.
        """
        _fail_bootstrap_status(message_id, arrived_at, "malformed SQS envelope")

        mock_update.assert_not_called()

    @patch("entrypoint.update_invocation_status", side_effect=RuntimeError("DDB down"))
    def test_is_fail_soft(self, mock_update):
        """Must not raise: the caller is about to re-raise the real error."""
        _fail_bootstrap_status("msg-abc", "2026-08-22T10:00:00Z", "tenant secret missing")


class TestDescribeVaultFetchFailure:
    _PATH = "adp/dev/tenants/sophos-internal/github-app"

    def test_missing_secret_names_path_and_repair(self):
        """RNFE → the exact SOPHOS reason, with the secret path and a repair command."""
        reason = _describe_vault_fetch_failure(
            _client_error("ResourceNotFoundException"), self._PATH
        )

        assert reason.startswith(f"tenant secret missing: {self._PATH}")
        assert "create-secret" in reason
        assert self._PATH in reason

    def test_access_denied_is_not_reported_as_missing(self):
        """AccessDenied must NOT say "missing".

        Different failure, different repair — telling an operator to create a
        secret that already exists sends them down the wrong path entirely.
        """
        reason = _describe_vault_fetch_failure(
            _client_error("AccessDeniedException"), self._PATH
        )

        assert "missing" not in reason
        assert "access denied" in reason.lower()
        assert "GetSecretValue" in reason

    def test_decryption_failure_points_at_kms(self):
        """DecryptionFailure is a KMS grant problem, not a secret problem."""
        reason = _describe_vault_fetch_failure(_client_error("DecryptionFailure"), self._PATH)

        assert "kms:Decrypt" in reason
        assert "missing" not in reason

    def test_unknown_error_falls_back_to_generic_reason(self):
        """An unrecognized code still yields a reason naming the path."""
        reason = _describe_vault_fetch_failure(_client_error("ThrottlingException"), self._PATH)

        assert self._PATH in reason
        assert "ThrottlingException" in reason
        assert "missing" not in reason

    def test_non_botocore_exception_does_not_crash(self):
        """A plain exception (e.g. the KeyError from a malformed secret payload)."""
        reason = _describe_vault_fetch_failure(KeyError("private_key"), self._PATH)

        assert self._PATH in reason
        assert "missing" not in reason

"""Issue #4020 — worker-side skip reasons: the idempotency guard and /aws-label.

Two exits in the worker's bootstrap left the Activity row untouched:

* The **idempotency guard** (a redelivered SQS message for work that already
  merged) returned 0 without writing any status, so the row stayed at
  ``webhook_received`` — a status Activity filters out of its default view. A
  correctly-deduplicated redelivery was therefore indistinguishable from a
  message that vanished. It now transitions to ``skipped``.

* The **/aws-label FATAL exit** (#3574) fires BEFORE the ``in_progress`` write, so
  #4053's bootstrap-failure status writes did not reach it. It had exactly the
  bug #4053 fixed everywhere else: the run disappeared. It now calls the same
  ``_fail_bootstrap_status`` helper.

The distinction between ``skipped`` and ``failed`` is the point of the first
group: a deduplicated redelivery is correct behaviour and must not be reported as
a failure, or every redelivery would show up as an incident.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.invocation_status import update_status


class TestUpdateStatusSkipReason:
    """The writer accepts and persists skip_reason (extends #4053's update_status)."""

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
            update_status("msg-1", "2026-08-22T10:00:00Z", **kwargs)
        assert mock_client.update_item.called, "expected an UpdateItem call"
        return mock_client.update_item.call_args.kwargs

    def test_skip_reason_written(self):
        call = self._update_call(status="skipped", skip_reason="idempotency_merged_pr")
        assert "skip_reason" in call["ExpressionAttributeNames"].values()
        assert call["ExpressionAttributeValues"][":skip_reason"] == {"S": "idempotency_merged_pr"}
        assert call["ExpressionAttributeValues"][":status"] == {"S": "skipped"}

    def test_omitted_when_not_provided(self):
        """The normal in_progress/complete transitions must not touch the field.

        If they set it to empty, a row that legitimately carries a reason from
        the ingress Lambda could be blanked by a later worker write.
        """
        call = self._update_call(status="in_progress", run_id="pod-1")
        assert ":skip_reason" not in call["ExpressionAttributeValues"]

    def test_independent_of_error_message(self):
        """Both can be set in one call without either clobbering the other —
        they use distinct expression placeholders."""
        call = self._update_call(status="failed", error_message="boom", skip_reason="some_reason")
        values = call["ExpressionAttributeValues"]
        assert values[":error_message"] == {"S": "boom"}
        assert values[":skip_reason"] == {"S": "some_reason"}

    def test_truncated_to_the_shared_bound(self):
        """A reason should be a short enum, but the writer must not be the thing
        that fails if a caller ever passes something long — DDB rejects oversized
        items, and this is fail-soft bookkeeping."""
        from lib.invocation_status import _MAX_ERROR_MESSAGE_CHARS

        call = self._update_call(status="skipped", skip_reason="x" * 5000)
        stored = call["ExpressionAttributeValues"][":skip_reason"]["S"]
        assert len(stored) == _MAX_ERROR_MESSAGE_CHARS

    def test_fail_soft_on_ddb_error(self):
        """update_status swallows DDB errors. A bookkeeping failure must never
        take down a run that otherwise succeeded."""
        mock_client = MagicMock()
        mock_client.update_item.side_effect = RuntimeError("DDB down")
        with (
            patch("lib.invocation_status._get_client", return_value=mock_client),
            patch.dict(
                "os.environ",
                {"WEBHOOK_EVENTS_TABLE": "adp-dev-webhook-events"},
                clear=False,
            ),
        ):
            update_status(
                "msg-1", "2026-08-22T10:00:00Z", "skipped", skip_reason="r"
            )  # must not raise


SAMPLE_ENVELOPE = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "message_id": "msg-abc-123",
    "actor": {
        "github_id": 12345678,
        "github_login": "jane-dev",
        "user_id": "cognito-sub-jane-123",
        "is_bot": False,
    },
    "source_ref": {
        "installation_id": 99887766,
        "repo": "acme-corp/flagship-app",
        "issue": 42,
        "pr": None,
        "sha": None,
    },
    "intent": {"trigger": "issue_labeled", "label": "developer"},
    "arrived_at": "2026-04-30T14:22:00Z",
}


class TestIdempotencySkipStatus:
    """The redelivery guard transitions the row instead of leaving it silent.

    Driven through the real ``main()`` so the assertion covers the guard's actual
    exit path — the bug was precisely that this path bypassed the status write.
    """

    @staticmethod
    def _run_to_guard(monkeypatch, tmp_path):
        """Drive main() to the idempotency-guard exit; return the status mock.

        Bootstrap is stubbed to succeed up to the guard, then
        ``_is_already_completed`` returns True to select the guard branch. Returns
        (exit_code, update_mock).
        """
        import json

        import entrypoint

        # main() long-polls SQS itself, so the envelope is injected by stubbing
        # the receive rather than via an env var.
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with (
            patch.object(
                entrypoint,
                "_receive_one_message",
                return_value=(json.dumps(SAMPLE_ENVELOPE), "receipt-1"),
            ),
            patch.object(entrypoint, "VaultClient") as mock_vault_cls,
            patch.object(entrypoint, "mint_installation_token", return_value="ghs_tok"),
            patch.object(entrypoint, "run_cmd", return_value=MagicMock(stdout="", returncode=0)),
            patch.object(entrypoint, "_is_already_completed", return_value=True),
            patch.object(entrypoint, "_delete_message") as mock_delete,
            patch.object(entrypoint, "update_invocation_status") as mock_update,
        ):
            mock_vault_cls.return_value.get_secret.return_value = {
                "app_id": "123",
                "private_key": "fake-key",
            }
            rc = entrypoint.main()

        return rc, mock_update, mock_delete

    def test_guard_writes_skipped_not_failed(self, monkeypatch, tmp_path):
        """status=skipped with the reason — NOT failed.

        ``failed`` would be actively harmful here: a deduplicated redelivery is
        correct behaviour, and reporting it as a failure would make every
        redelivery look like an incident in the Activity feed.
        """
        rc, mock_update, _ = self._run_to_guard(monkeypatch, tmp_path)

        assert rc == 0
        mock_update.assert_called_once()
        args = mock_update.call_args.args
        kwargs = mock_update.call_args.kwargs
        assert args[0] == "msg-abc-123"
        assert args[1] == "2026-04-30T14:22:00Z"
        assert args[2] == "skipped"
        assert kwargs["skip_reason"] == "idempotency_merged_pr"

    def test_guard_summary_explains_the_skip_in_prose(self, monkeypatch, tmp_path):
        """The list view renders summary, so the cause is visible without a
        drill-down (same reasoning as #4053's bootstrap writes)."""
        _, mock_update, _ = self._run_to_guard(monkeypatch, tmp_path)

        summary = mock_update.call_args.kwargs["summary"]
        assert "merged" in summary.lower()
        assert "duplicate" in summary.lower()

    def test_guard_still_deletes_the_sqs_message(self, monkeypatch, tmp_path):
        """Regression: the status write must not disturb the guard's real job.

        If the message were left on the queue it would be redelivered forever —
        so this pins that the added bookkeeping sits alongside the delete rather
        than in front of it.
        """
        _, _, mock_delete = self._run_to_guard(monkeypatch, tmp_path)

        mock_delete.assert_called_once()


class TestAwsLabelFatalStatus:
    """The #3574 FATAL exit now reports itself, like every other bootstrap exit."""

    @staticmethod
    def _run_to_fatal_exit(monkeypatch, tmp_path, *, aws_label):
        """Drive main() to the #3574 assume-role failure with the given label.

        ``operations`` is one of PERSONAS_NEEDING_AWS, so Step 7 runs; the
        assume-role call is stubbed to raise. Returns (raised, update_mock).
        """
        import json

        import entrypoint

        envelope = json.loads(json.dumps(SAMPLE_ENVELOPE))
        envelope["persona"] = "operations"
        if aws_label is not None:
            envelope["aws_label"] = aws_label

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/q.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with (
            patch.object(
                entrypoint,
                "_receive_one_message",
                return_value=(json.dumps(envelope), "receipt-1"),
            ),
            patch.object(entrypoint, "VaultClient") as mock_vault_cls,
            patch.object(entrypoint, "mint_installation_token", return_value="ghs_tok"),
            patch.object(entrypoint, "run_cmd", return_value=MagicMock(stdout="", returncode=0)),
            patch.object(entrypoint, "_is_already_completed", return_value=False),
            patch.object(
                entrypoint,
                "_fetch_assumed_aws_credentials",
                side_effect=RuntimeError("AccessDenied assuming role"),
            ),
            patch.object(entrypoint, "_delete_message"),
            patch.object(entrypoint, "_fail_bootstrap_status") as mock_fail,
            patch.object(entrypoint, "update_invocation_status"),
        ):
            mock_vault_cls.return_value.get_secret.return_value = {
                "app_id": "123",
                "private_key": "fake-key",
            }
            raised = None
            try:
                entrypoint.main()
            except Exception as exc:  # noqa: BLE001 — the FATAL re-raise
                raised = exc

        return raised, mock_fail

    def test_fatal_exit_writes_failed_status_before_re_raising(self, monkeypatch, tmp_path):
        """The exit reports itself AND still re-raises.

        Both halves matter. Without the status write the run vanishes from
        Activity — the exact #4053 bug, uncovered here because this exit fires
        before the ``in_progress`` write. Without the re-raise the worker would
        continue into the WRONG AWS account, which is the bug #3574 exists to
        prevent.
        """
        raised, mock_fail = self._run_to_fatal_exit(monkeypatch, tmp_path, aws_label="prod-admin")

        mock_fail.assert_called_once()
        assert raised is not None, "the FATAL exit must still propagate"

        args = mock_fail.call_args.args
        assert args[0] == "msg-abc-123"
        assert args[1] == "2026-04-30T14:22:00Z"
        assert "prod-admin" in args[2]

    def test_no_label_stays_non_fatal_and_writes_nothing(self, monkeypatch, tmp_path):
        """Regression: the label-less path is unchanged (#3574 back-compat).

        Without an explicit label, assume-role failure is a warning and the run
        continues with the ranked picker. It must NOT acquire a failed status —
        that would report a still-running run as failed.
        """
        _, mock_fail = self._run_to_fatal_exit(monkeypatch, tmp_path, aws_label=None)

        mock_fail.assert_not_called()

    @patch("entrypoint.update_invocation_status")
    def test_message_names_the_label_and_the_repair(self, mock_update):
        """The reason must be actionable.

        The label is operator-supplied and charset-validated upstream (#3574), so
        echoing it is safe — and it is the one detail that makes the failure
        diagnosable ("which label?" is the first question).
        """
        from entrypoint import _fail_bootstrap_status

        _fail_bootstrap_status(
            "msg-1",
            "2026-08-22T10:00:00Z",
            "could not assume the AWS role for the /aws-label 'prod-admin' "
            "requested in the triggering comment — check that this label is "
            "linked in your vault and that its role trusts the platform: denied",
        )

        assert mock_update.call_args.args[2] == "failed"
        msg = mock_update.call_args.kwargs["error_message"]
        assert "prod-admin" in msg
        assert "vault" in msg

    @patch("entrypoint.update_invocation_status", side_effect=RuntimeError("DDB down"))
    def test_status_write_cannot_mask_the_original_failure(self, _mock_update):
        """#4053's fail-soft property, re-pinned for this call site.

        The caller re-raises the real STS error immediately after. If reporting
        the failure could itself raise, the operator would see a DDB error
        instead of the actual cause.
        """
        from entrypoint import _fail_bootstrap_status

        _fail_bootstrap_status("msg-1", "2026-08-22T10:00:00Z", "role assumption failed")

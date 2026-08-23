"""Issue #4020 — a guard-blocked spawn must leave a row in Agent Activity.

The six guards in ``spawn_persona`` (installation validation, unknown persona,
self-mention, self-re-trigger, cross-persona loop, chain-depth cap) all returned
BEFORE ``_capture_invocation_event`` ran. So a blocked trigger wrote nothing
anywhere the UI could reach: the operator's ``@agent-...`` comment produced a
200, no run, and no trace. The only record was a CloudWatch log line.

Two things are tested here, and the second matters more than the first:

1. The block is now recorded — ``status="blocked"`` plus the guard's existing
   ``block_reason`` as the ``skip_reason``.
2. The write is BEST-EFFORT. The issue's impact analysis is explicit: "DDB write
   added in a guard path throws → webhook 500s → GitHub redelivery storms /
   missed dispatches (must stay best-effort)". A guard block is benign and
   expected; if bookkeeping could raise, one DDB blip would convert every
   blocked delivery into a 500 that GitHub retries. ``TestWriteIsBestEffort``
   is the regression guard on that, and it is the reason all guards were funnelled
   through a single wrapped call site rather than six inline writes.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.spawn_persona import spawn_persona


@dataclass
class MockResolvedIdentity:
    tenant_id: str = "test-org"
    org_id: str = "test-org"
    user_id: str = "user-123"
    user_provisioning_mode: str = "strict"
    user_kind: str = "human"
    bot_kind: str = ""


def _human_sender():
    return {"login": "alice", "id": 100, "type": "User"}


def _bot_sender():
    return {"login": "aws-e-adp-agent-dev[bot]", "id": 900, "type": "Bot"}


def _base_correlation_ctx(**overrides):
    ctx = {
        "correlation_id": "corr-test-001",
        "root_human_id": "user-alice",
        "triggered_by": None,
        "is_human_rooted": True,
        "is_new_chain": False,
        "parent_invocation_id": None,
        "chain_depth": 1,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    ctx.update(overrides)
    return ctx


def _base_payload():
    return {
        "action": "created",
        "issue": {
            "number": 55,
            "title": "Test issue",
            "html_url": "https://github.com/org/repo/issues/55",
        },
        "repository": {"full_name": "org/repo"},
        "sender": _human_sender(),
        "installation": {"id": 123},
    }


def _spawn_kwargs(**overrides):
    defaults = {
        "persona": "developer",
        "correlation_ctx": _base_correlation_ctx(),
        "channel_key": "github:repo=org/repo,issue=55",
        "resolved_identity": MockResolvedIdentity(),
        "tenant_id": "test-org",
        "actor_user_id": "user-alice-456",
        "actor_org_id": "test-org",
        "sender": _human_sender(),
        "event_type": "issue_comment",
        "action": "created",
        "installation_id": 123,
        "repo": "org/repo",
        "payload": _base_payload(),
        "intent_trigger": "mentioned",
        "intent_label": None,
    }
    defaults.update(overrides)
    return defaults


class TestBlockedRowWritten:
    """Each guard writes exactly one blocked row carrying its own reason."""

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._capture_blocked_event")
    def test_invalid_installation_writes_blocked_row(self, mock_capture, _mock_metric):
        result = spawn_persona(**_spawn_kwargs(installation_id=0))

        assert result.success is False
        assert result.block_reason == "invalid_installation_id"
        mock_capture.assert_called_once()
        written = mock_capture.call_args.kwargs["block_reason"]
        assert written == "invalid_installation_id"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._capture_blocked_event")
    def test_unknown_persona_writes_blocked_row(self, mock_capture, _mock_metric):
        result = spawn_persona(**_spawn_kwargs(persona="not-a-real-persona"))

        assert result.success is False
        assert result.block_reason == "unknown_persona"
        assert mock_capture.call_args.kwargs["block_reason"] == "unknown_persona"

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._capture_blocked_event")
    def test_bot_guard_block_writes_blocked_row(self, mock_capture, _mock_metric):
        """A bot-guard block (self-re-trigger here) reaches the same write.

        Guards 2-5 live in ``_apply_bot_guards``, a separate function, so this
        pins that its block also funnels through the shared write rather than
        returning early past it.
        """
        result = spawn_persona(
            **_spawn_kwargs(
                sender=_bot_sender(),
                resolved_identity=MockResolvedIdentity(
                    user_kind="bot", bot_kind="developer"
                ),
                correlation_ctx=_base_correlation_ctx(
                    last_triggered_persona="developer"
                ),
            )
        )

        assert result.success is False
        assert result.block_reason is not None
        mock_capture.assert_called_once()
        # The reason is the guard's own string, reused verbatim — not a new
        # parallel vocabulary invented for the UI.
        assert mock_capture.call_args.kwargs["block_reason"] == result.block_reason

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.spawn_persona._capture_blocked_event")
    @patch("common.sqs_publisher.publish_envelope", return_value="msg-123")
    def test_successful_spawn_writes_no_blocked_row(
        self, _mock_sqs, mock_blocked, mock_capture, _mock_write, _mock_metric
    ):
        """Regression: the normal dispatch path is untouched.

        A stray blocked row on a successful spawn would double-count the run in
        the Activity feed and in any status rollup built on it.
        """
        result = spawn_persona(**_spawn_kwargs())

        assert result.success is True
        mock_blocked.assert_not_called()
        mock_capture.assert_called_once()

    @patch("common.spawn_persona._emit_metric")
    @patch("common.spawn_persona._capture_blocked_event")
    def test_only_the_first_matching_guard_writes(self, mock_capture, _mock_metric):
        """installation_id=0 AND an unknown persona → one row, not two.

        The guards are an if/elif chain, so the row count is exactly one per
        blocked delivery. Two rows would make the same trigger appear twice.
        """
        result = spawn_persona(
            **_spawn_kwargs(installation_id=0, persona="not-a-real-persona")
        )

        assert result.block_reason == "invalid_installation_id"
        assert mock_capture.call_count == 1


class TestWriteIsBestEffort:
    """The impact-analysis constraint: bookkeeping must never fail the webhook.

    A raise here would 500 the delivery. GitHub retries 5xx, so a transient DDB
    problem on a *benign* guard block would produce a redelivery storm — strictly
    worse than the missing-row bug this issue fixes.
    """

    @patch("common.spawn_persona._emit_metric")
    @patch(
        "common.spawn_persona._capture_blocked_event",
        side_effect=RuntimeError("DDB unavailable"),
    )
    def test_write_failure_does_not_propagate(self, _mock_capture, _mock_metric):
        """Even a hard raise from the write leaves spawn_persona returning normally.

        Note this patches the function itself, so it proves the CALL SITE is
        wrapped — not merely that the function's own internals catch. Both layers
        matter; only the call-site guarantee survives a future refactor of the
        function body.
        """
        result = spawn_persona(**_spawn_kwargs(installation_id=0))

        assert result.success is False
        assert result.block_reason == "invalid_installation_id"

    @patch("common.spawn_persona._emit_metric")
    def test_missing_events_table_is_silent(self, _mock_metric):
        """No EVENTS_TABLE configured → no write, no error.

        Local/unit environments and any deploy where the table var is unset must
        not start failing guard blocks.
        """
        with patch.dict("os.environ", {"EVENTS_TABLE": ""}, clear=False):
            result = spawn_persona(**_spawn_kwargs(installation_id=0))

        assert result.success is False
        assert result.block_reason == "invalid_installation_id"

    @patch("common.spawn_persona._emit_metric")
    def test_logger_construction_failure_is_swallowed(self, _mock_metric):
        """A failure inside the write body (not just at the boundary) is caught."""
        with patch.dict(
            "os.environ", {"EVENTS_TABLE": "adp-dev-webhook-events"}, clear=False
        ):
            with patch(
                "common.webhook_events.WebhookEventLogger",
                side_effect=RuntimeError("boto3 exploded"),
            ):
                result = spawn_persona(**_spawn_kwargs(installation_id=0))

        assert result.success is False
        assert result.block_reason == "invalid_installation_id"


class TestBlockedRowContents:
    """What lands on the row — checked through the real write path."""

    @staticmethod
    def _captured_item(**spawn_overrides):
        """Run a blocked spawn with a mock DDB table and return the written item."""
        from unittest.mock import MagicMock

        mock_logger = MagicMock()
        with patch.dict(
            "os.environ", {"EVENTS_TABLE": "adp-dev-webhook-events"}, clear=False
        ):
            with patch("common.spawn_persona._emit_metric"):
                with patch(
                    "common.webhook_events.WebhookEventLogger", return_value=mock_logger
                ):
                    spawn_persona(**_spawn_kwargs(**spawn_overrides))
        assert mock_logger.log_event.called, "expected a blocked row write"
        return mock_logger.log_event.call_args.kwargs

    def test_status_and_reason(self):
        item = self._captured_item(installation_id=0)
        assert item["status"] == "blocked"
        assert item["skip_reason"] == "invalid_installation_id"

    def test_attributed_to_the_human_root_of_the_chain(self):
        """Issue #2042 parity: the row must land in the originating human's feed.

        Attributing a blocked agent-triggered spawn to the bot actor would hide
        it from the person who started the chain — the one person who is actually
        wondering why nothing happened.
        """
        item = self._captured_item(installation_id=0)
        assert item["user_id"] == "user-alice"
        assert item["root_human_id"] == "user-alice"

    def test_falls_back_to_actor_when_not_human_rooted(self):
        item = self._captured_item(
            installation_id=0,
            correlation_ctx=_base_correlation_ctx(
                is_human_rooted=False, root_human_id=None
            ),
        )
        assert item["user_id"] == "user-alice-456"

    def test_carries_lineage_and_source_context(self):
        """Without correlation_id the row cannot join the chain view, and without
        source_url the operator has no link back to the thread they commented on."""
        item = self._captured_item(installation_id=0)
        assert item["correlation_id"] == "corr-test-001"
        assert item["issue_number"] == 55
        assert item["source_url"] == "https://github.com/org/repo/issues/55"
        assert item["topic"] == "Test issue"

    def test_unattributed_when_no_user_at_all(self):
        """user_id is a GSI partition key, so an empty string would make the row
        unqueryable. A sentinel keeps it retrievable."""
        item = self._captured_item(
            installation_id=0,
            actor_user_id="",
            correlation_ctx=_base_correlation_ctx(
                is_human_rooted=False, root_human_id=None
            ),
        )
        assert item["user_id"] == "unattributed"

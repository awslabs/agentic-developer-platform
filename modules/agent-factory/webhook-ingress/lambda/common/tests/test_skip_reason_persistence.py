"""Issue #4020 — the skip/block reason must reach the DDB row, not just the logs.

Before this change a non-dispatching delivery computed a perfectly good reason
and then threw it away everywhere the UI could see it: the reason went to
CloudWatch and the HTTP response body, while the Activity row got a bare
``no_op`` status. Operators had a "✗ No-op" badge and no way to tell
"nobody mentioned an agent" from "the loop guard stopped it".

These tests pin the two properties that make the fix trustworthy:

1. The reason is PERSISTED (that's the bug), and absent-not-null when there
   isn't one (so pre-existing rows and normal runs are untouched).
2. Every reason is a STATIC ENUM. The issue's impact analysis calls out reason
   strings as a cross-tenant info-disclosure surface — they are rendered in the
   Activity feed, so a reason built by interpolating payload content would leak
   repo names, logins, or label text to anyone who can see the row. The
   ``TestReasonsAreStaticEnums`` class enforces that structurally rather than by
   convention.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from common import skip_reasons
from common.webhook_events import WebhookEventLogger


def _logger_with_mock_table():
    with patch("common.webhook_events.boto3"):
        logger = WebhookEventLogger("adp-dev-webhook-events")
    mock_table = MagicMock()
    logger._table = mock_table
    return logger, mock_table


def _log(logger, **overrides):
    """log_event with the boilerplate filled in, so tests show only what they test."""
    kwargs = {
        "event_id": "evt-1",
        "arrived_at": "2026-08-22T00:00:00Z",
        "tenant_id": "t",
        "channel": "github",
        "event_type": "issue_comment",
        "action": "created",
        "status": "no_op",
    }
    kwargs.update(overrides)
    logger.log_event(**kwargs)


class TestSkipReasonPersisted:
    def test_skip_reason_written_to_item(self):
        logger, table = _logger_with_mock_table()
        _log(logger, skip_reason=skip_reasons.NO_MENTION)
        item = table.put_item.call_args[1]["Item"]
        assert item["skip_reason"] == "no_mention"

    def test_absent_when_not_provided(self):
        """A normal dispatched run must not gain a null attribute.

        Omission matters beyond tidiness: the API serializes a missing attribute
        as null, so old rows keep working, and DDB is not charged for storing an
        empty field on every triggering run.
        """
        logger, table = _logger_with_mock_table()
        _log(logger, status="webhook_received")
        item = table.put_item.call_args[1]["Item"]
        assert "skip_reason" not in item

    def test_empty_string_is_not_written(self):
        """Falsy reason == no reason. Guards against a caller passing "" and
        producing a row that claims to have an explanation but shows nothing."""
        logger, table = _logger_with_mock_table()
        _log(logger, skip_reason="")
        item = table.put_item.call_args[1]["Item"]
        assert "skip_reason" not in item

    def test_coexists_with_error_message(self):
        """`error_message` (#4053) and `skip_reason` (#4020) are different axes.

        A row can carry either; they must not overwrite one another, because the
        UI styles them differently — an error is red, a skip is neutral.
        """
        logger, table = _logger_with_mock_table()
        _log(
            logger,
            status="blocked",
            error_message="something broke",
            skip_reason=skip_reasons.NO_MENTION,
        )
        item = table.put_item.call_args[1]["Item"]
        assert item["error_message"] == "something broke"
        assert item["skip_reason"] == "no_mention"

    def test_blocked_status_round_trips(self):
        """`blocked` is a new status value; DDB is schemaless so nothing
        validates it. This pins that we actually write it as given."""
        logger, table = _logger_with_mock_table()
        _log(logger, status="blocked", skip_reason="self_re_trigger")
        item = table.put_item.call_args[1]["Item"]
        assert item["status"] == "blocked"
        assert item["skip_reason"] == "self_re_trigger"


class TestReasonsAreStaticEnums:
    """The security invariant from the issue's own impact analysis.

    "Reason string leaks internal identifiers cross-tenant → info disclosure in
    the Activity feed (reasons must be static enums, not raw payload echoes)."

    A reason is operator-visible, so anything interpolated into it is disclosed.
    Testing "no payload data leaked" directly is impossible; instead we pin the
    structural properties that make leakage impossible by construction.
    """

    @staticmethod
    def _public_reasons() -> dict[str, str]:
        return {
            name: value
            for name, value in vars(skip_reasons).items()
            if name.isupper() and isinstance(value, str)
        }

    def test_module_exposes_reasons(self):
        """Sanity guard: if the introspection below finds nothing, the other
        tests in this class would vacuously pass."""
        assert len(self._public_reasons()) >= 10

    def test_all_reasons_are_lowercase_snake_case(self):
        """A reason that can only be [a-z0-9_] cannot carry a repo name, a login,
        a URL, or free-text label content — the shapes an echo would take."""
        import re

        pattern = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
        for name, value in self._public_reasons().items():
            assert pattern.match(value), (
                f"{name}={value!r} is not a static snake_case enum"
            )

    def test_no_reason_contains_format_placeholders(self):
        """No `{}`, `%s`, or f-string braces — those are how an echo gets in.

        If a future change needs a dynamic reason, this test fails and forces the
        author to justify it against the info-disclosure finding rather than
        adding it silently.
        """
        for name, value in self._public_reasons().items():
            assert "{" not in value and "}" not in value, f"{name} looks templated"
            assert "%" not in value, f"{name} looks like a printf template"

    def test_reason_values_are_unique(self):
        """Two constants sharing a value would make the UI mapping ambiguous and
        silently mislabel one of the two paths."""
        values = list(self._public_reasons().values())
        assert len(values) == len(set(values))

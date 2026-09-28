"""Issue #4020 — skip_reason must survive the DDB → API → UI trip.

The Lambda and worker now write a reason onto the row, but the Activity API sat
in between silently dropping it: ``_map_item`` did not read the attribute, so the
field never reached the frontend regardless of what the producers wrote.

Also covers the two new statuses. ``blocked`` (a Lambda guard stopped the spawn)
and ``skipped`` (the worker deduplicated a redelivery) are non-runs and had to
join ``no_op`` in two places that were previously separate literals:

* the **non-triggering** set — so they are hidden from the default board and
  shown under "Show all events" (#1658, #3708), and
* the **terminal** set — so ``completed_at`` is derived for them; a terminal row
  otherwise renders as "Active — not yet terminal" forever.

That list was duplicated at four call sites (flat list, chain view, descendant
fetch), which is why the constant is asserted here rather than each literal: the
failure mode being guarded is the sets drifting apart.
"""

from boto3.dynamodb.conditions import ConditionExpressionBuilder

from src.activity.service import NON_TRIGGERING_STATUSES, ActivityService


def _rendered_filter(mock_table):
    """Render the FilterExpression boto3 was handed into its literal values.

    ``str()`` on a condition object only shows nested operand objects, so the
    values never appear. Building the expression the way boto3 does at request
    time is what actually reveals which statuses were filtered.
    """
    condition = mock_table.query.call_args[1]["FilterExpression"]
    built = ConditionExpressionBuilder().build_expression(condition, is_key_condition=False)
    return set(built.attribute_value_placeholders.values())


def _row(**overrides):
    """A minimal webhook-events row as DDB actually returns it."""
    item = {
        "event_id": "evt-1",
        "arrived_at": "2026-08-22T10:00:00Z",
        "tenant_id": "org-tenant-001",
        "user_id": "user-abc-123",
        "channel": "github",
        "status": "no_op",
        "status_updated_at": "2026-08-22T10:00:01Z",
        "topic": "Something broke",
    }
    item.update(overrides)
    return item


class TestSkipReasonMapping:
    def test_skip_reason_surfaced_on_the_item(self):
        item = ActivityService._map_item(_row(skip_reason="no_mention"))
        assert item.skip_reason == "no_mention"

    def test_absent_attribute_maps_to_none(self):
        """Rows written before this change carry no attribute at all.

        DDB is schemaless so there was no migration; the API must serialize the
        absence as null rather than raising or inventing a value.
        """
        item = ActivityService._map_item(_row())
        assert item.skip_reason is None

    def test_normal_run_has_no_reason(self):
        """A dispatched run must not surface a reason — the UI would show
        "Complete" beside an explanation of why nothing ran."""
        item = ActivityService._map_item(_row(status="complete", summary="did the work"))
        assert item.skip_reason is None

    def test_block_reason_strings_pass_through_verbatim(self):
        """spawn_persona's existing block_reason vocabulary is reused, not
        re-mapped — the API must not translate or normalize it, or the UI's
        mapping table would silently miss."""
        item = ActivityService._map_item(_row(status="blocked", skip_reason="self_re_trigger"))
        assert item.status == "blocked"
        assert item.skip_reason == "self_re_trigger"


class TestNonTriggeringStatuses:
    def test_new_statuses_are_non_triggering(self):
        """blocked/skipped are non-runs and belong with no_op.

        If they were absent here they would appear on the default board as if
        they were real runs, inflating every count built on that view.
        """
        assert "blocked" in NON_TRIGGERING_STATUSES
        assert "skipped" in NON_TRIGGERING_STATUSES

    def test_pre_existing_statuses_retained(self):
        """Regression: the original two must not have been dropped while adding
        the new ones."""
        assert "no_op" in NON_TRIGGERING_STATUSES
        assert "webhook_received" in NON_TRIGGERING_STATUSES

    def test_triggering_statuses_absent(self):
        """A real run must never be filtered off the board."""
        for status in ("in_progress", "complete", "failed", "rejected", "rate_limited"):
            assert status not in NON_TRIGGERING_STATUSES

    def test_default_query_filters_the_new_statuses(self, mock_dynamodb_resource, mock_dynamodb_table):
        """The constant is actually wired into the DDB FilterExpression.

        Asserting on the constant alone would pass even if the query still used
        a stale inline literal — which is exactly the drift this change removed.
        """
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user("user-abc-123")

        assert "FilterExpression" in mock_dynamodb_table.query.call_args[1]
        filtered = _rendered_filter(mock_dynamodb_table)
        assert {"blocked", "skipped"} <= filtered
        # The pre-existing exclusions must still be there too.
        assert {"no_op", "webhook_received"} <= filtered

    def test_explicit_status_filter_overrides_the_exclusion(self, mock_dynamodb_resource, mock_dynamodb_table):
        """Asking for status=blocked must return blocked rows.

        The exclusion is a default, not a rule — otherwise the new "Blocked"
        filter option in the UI would always come back empty.
        """
        mock_dynamodb_table.query.return_value = {"Items": [], "Count": 0}
        service = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)
        service.query_by_user("user-abc-123", status="blocked")

        filtered = _rendered_filter(mock_dynamodb_table)
        # An equality filter on the requested status, not a NOT-IN exclusion.
        assert "blocked" in filtered
        assert "webhook_received" not in filtered


class TestTerminalStatuses:
    """completed_at derivation — blocked/skipped never transition again."""

    def test_blocked_derives_completed_at(self):
        item = ActivityService._map_item(_row(status="blocked", skip_reason="chain_depth_exceeded"))
        assert item.completed_at == "2026-08-22T10:00:01Z"

    def test_skipped_derives_completed_at(self):
        item = ActivityService._map_item(_row(status="skipped", skip_reason="idempotency_merged_pr"))
        assert item.completed_at == "2026-08-22T10:00:01Z"

    def test_in_progress_still_has_no_completed_at(self):
        """Regression: an active run must stay active. A completed_at here would
        make the detail view compute a bogus duration for a live run."""
        item = ActivityService._map_item(_row(status="in_progress"))
        assert item.completed_at is None

    def test_no_op_unchanged(self):
        """no_op was already terminal before this change."""
        item = ActivityService._map_item(_row(status="no_op"))
        assert item.completed_at == "2026-08-22T10:00:01Z"


class TestSchemaContract:
    def test_field_is_optional_on_the_model(self):
        """InvocationItem must construct without a reason — every existing
        caller builds these without one."""
        from src.activity.schemas import InvocationItem

        item = InvocationItem(invocation_id="i", invoked_at="2026-08-22T10:00:00Z")
        assert item.skip_reason is None

    def test_field_serializes_into_the_response_body(self):
        """The field has to appear in the JSON the frontend parses — a
        model-only addition would be invisible to the UI."""
        from src.activity.schemas import InvocationItem

        item = InvocationItem(
            invocation_id="i",
            invoked_at="2026-08-22T10:00:00Z",
            status="no_op",
            skip_reason="label_unmapped",
        )
        dumped = item.model_dump()
        assert dumped["skip_reason"] == "label_unmapped"

    def test_null_is_serialized_not_omitted(self):
        """The frontend type declares `skip_reason: string | null`, so the key
        must be present even when empty."""
        from src.activity.schemas import InvocationItem

        item = InvocationItem(invocation_id="i", invoked_at="2026-08-22T10:00:00Z")
        assert "skip_reason" in item.model_dump()

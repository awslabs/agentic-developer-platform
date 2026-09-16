"""Historical recovery must be scoped and must never replay an existing event."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts.backfill_mantle_budget_usage import candidates, instant, replay_row
from src.shared.models.usage import UsageLog

STAMP = datetime(2026, 9, 11, 12, tzinfo=UTC)


def usage_row(**changes):
    values = dict(
        org_id="tenant",
        department_id="",
        user_id="sub",
        team_id="team",
        account_type="human",
        model="openai.gpt-6-astra",
        input_tokens=1000,
        output_tokens=100,
        cost_usd=Decimal("0.0045"),
        latency_ms=20,
        status_code=200,
        timestamp=STAMP,
        request_id="request-1",
    )
    values.update(changes)
    return UsageLog(**values)


async def test_candidate_query_excludes_other_people_providers_runs_and_settled_rows(db_session):
    intended = usage_row()
    excluded = [
        usage_row(org_id="other"),
        usage_row(user_id="other"),
        usage_row(account_type="service"),
        usage_row(agent_run_id="hosted-run"),
        usage_row(model="anthropic.claude-opus-5"),
        usage_row(chat_log_s3_key="already.json"),
        usage_row(request_id=None),
        usage_row(input_tokens=0, output_tokens=0),
        usage_row(timestamp=STAMP - timedelta(days=1)),
        usage_row(timestamp=STAMP + timedelta(days=1)),
    ]
    db_session.add_all([intended, *excluded])
    await db_session.commit()
    rows = list((await db_session.scalars(candidates("tenant", "sub", STAMP, STAMP + timedelta(days=1)))).all())
    assert [row.id for row in rows] == [intended.id]


def missing_s3():
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    return s3


def test_default_dry_run_never_writes():
    s3 = missing_s3()
    assert replay_row(s3, "logs", usage_row(), apply=False) == "missing"
    s3.put_object.assert_not_called()


def test_apply_preserves_date_tokens_and_identity_and_requires_absent_key():
    s3 = missing_s3()
    assert replay_row(s3, "logs", usage_row(), apply=True) == "queued"
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Key"] == "tenant/sub/2026/09/11/request-1.json"
    assert kwargs["IfNoneMatch"] == "*"
    event = json.loads(kwargs["Body"])
    assert instant(event["timestamp"]) == STAMP
    assert event["user_id"] == "sub" and event["org_id"] == "tenant"
    assert event["root_human_id"] == ""  # no duplicate cloud debit
    assert event["response"]["usage"]["input_tokens"] == 1000
    assert event["response"]["usage"]["output_tokens"] == 100


def test_existing_object_is_not_replayed_even_when_usage_link_is_missing():
    s3 = MagicMock()
    assert replay_row(s3, "logs", usage_row(), apply=True) == "existing"
    s3.put_object.assert_not_called()


def test_conditional_put_handles_another_replay_winning_the_race():
    s3 = missing_s3()
    s3.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    assert replay_row(s3, "logs", usage_row(), apply=True) == "existing"
    s3.put_object.assert_called_once()


def test_head_permission_error_is_not_treated_as_absence():
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError({"Error": {"Code": "403"}}, "HeadObject")
    with pytest.raises(ClientError):
        replay_row(s3, "logs", usage_row(), apply=True)
    s3.put_object.assert_not_called()

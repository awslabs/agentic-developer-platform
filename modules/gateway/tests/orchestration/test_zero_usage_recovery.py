"""Provider evidence can settle a failed request; failure alone cannot."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import flow_meter
from src.orchestration import zero_usage_recovery as recovery
from src.orchestration.models import OrchestrationDecision
from src.shared.models.budget import BudgetSettlementReceipt
from tests.orchestration.test_budget_read import display, usage  # noqa: F401
from tests.orchestration.test_list_flows import session  # noqa: F401

ACCOUNT = "123456789012"
PROVIDER_ID = "00000000-0000-4000-8000-000000000002"


def event():
    return {
        "eventId": "event-one",
        "logStreamName": "model-invocations",
        "message": json.dumps(
            {
                "schemaType": "ModelInvocationLog",
                "schemaVersion": "1.0",
                "operation": "Responses",
                "timestamp": "2026-11-01T12:00:00Z",
                "accountId": ACCOUNT,
                "region": "us-east-1",
                "requestId": PROVIDER_ID,
                "modelId": f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/us.openai.gpt-6-sol",
                "input": {
                    "inputContentType": "application/json",
                    "inputTokenCount": 0,
                    "cacheReadInputTokenCount": 0,
                    "cacheWriteInputTokenCount": 0,
                },
                "output": {"outputContentType": "application/json", "outputTokenCount": 0},
            }
        ),
    }


def verify(value):
    return recovery.verified_zero_event(value, request_id=PROVIDER_ID, model="openai.gpt-6-sol", account_id=ACCOUNT, region="us-east-1")


def test_provider_zero_evidence_is_sanitized_and_content_bound():
    value = event()
    payload = json.loads(value["message"])
    payload["input"]["inputBodyJson"] = {"private": "must not be emitted"}
    value["message"] = json.dumps(payload)
    result = verify(value)
    assert result["token_counts"]["outputTokenCount"] == 0
    assert "private" not in json.dumps(result)
    assert result["event_sha256"] != verify(event())["event_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("requestId", "wrong"),
        ("accountId", "other"),
        ("region", "other"),
        ("operation", "InvokeModel"),
        ("schemaType", "untrusted"),
        ("modelId", "different-model"),
    ],
)
def test_foreign_or_wrong_provider_evidence_refused(field, value):
    source = event()
    payload = json.loads(source["message"])
    payload[field] = value
    source["message"] = json.dumps(payload)
    with pytest.raises(ValueError):
        verify(source)


@pytest.mark.parametrize(
    "side,field",
    [("input", "inputTokenCount"), ("input", "cacheReadInputTokenCount"), ("input", "cacheWriteInputTokenCount"), ("output", "outputTokenCount")],
)
@pytest.mark.parametrize("value", [None, 1, False, "0", -1])
def test_missing_nonzero_or_untyped_counter_never_releases_a_hold(side, field, value):
    source = event()
    payload = json.loads(source["message"])
    payload[side][field] = value
    source["message"] = json.dumps(payload)
    with pytest.raises(ValueError):
        verify(source)


@pytest.mark.parametrize("events", [[], [event(), {**event(), "eventId": "second"}]])
def test_missing_or_ambiguous_event_is_not_zero_usage(events):
    from datetime import UTC, datetime

    logs = SimpleNamespace(filter_log_events=lambda **_: {"events": events})
    row = SimpleNamespace(timestamp=datetime.now(UTC), provider_request_id=PROVIDER_ID, model="openai.gpt-6-sol")
    with pytest.raises(ValueError):
        recovery.read_zero_evidence(logs, log_group="example", row=row, account_id=ACCOUNT, region="us-east-1")


async def test_preview_is_read_only_and_apply_creates_replayable_receipt_for_normal_reconciliation(display, monkeypatch):  # noqa: F811
    d = display
    await usage(d, request="failed-request", cost="0", status_code=499, provider_request_id=PROVIDER_ID, model="openai.gpt-6-sol")
    await d.store.reserve("failed-request", Decimal(6), [d.target])
    await d.store.retain_failed_bound("failed-request", d.target)
    await d.db.commit()
    monkeypatch.setattr(recovery, "get_flow_reservations", lambda: d.store)
    monkeypatch.setattr(flow_meter, "get_flow_reservations", lambda: d.store)
    monkeypatch.setattr(recovery, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=d.policy, refusal=None)))
    # The fixture owns this connection across command invocations.
    monkeypatch.setattr(d.store, "close", AsyncMock())
    aws = SimpleNamespace(
        region_name="us-east-1",
        client=lambda service: {
            "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT, "Arn": "operator"}),
            "bedrock": SimpleNamespace(
                get_model_invocation_logging_configuration=lambda: {"loggingConfig": {"cloudWatchConfig": {"logGroupName": "example"}}}
            ),
            "logs": SimpleNamespace(filter_log_events=lambda **_: {"events": [event()]}),
        }[service],
    )
    args = SimpleNamespace(
        account=ACCOUNT,
        org=d.flow.org_id,
        flow=d.flow.id,
        request=["failed-request"],
        reason="Provider reconciliation",
        apply=False,
        expected_snapshot=None,
    )
    before = await d.client.hgetall(d.target.key())
    preview = await recovery.reconcile_zero_usage(args, session_factory=lambda: d.db, aws=aws)
    assert preview["released_usd"] == "6" and not preview["applied"]
    assert await d.client.hgetall(d.target.key()) == before
    assert await d.db.get(BudgetSettlementReceipt, (args.org, "failed-request")) is None
    args.apply = True
    args.expected_snapshot = "stale"
    with pytest.raises(ValueError, match="Preview changed"):
        await recovery.reconcile_zero_usage(args, session_factory=lambda: d.db, aws=aws)
    assert await d.db.get(BudgetSettlementReceipt, (args.org, "failed-request")) is None
    args.expected_snapshot = preview["snapshot"]
    result = await recovery.reconcile_zero_usage(args, session_factory=lambda: d.db, aws=aws)
    assert result["applied"]
    receipt = await d.db.get(BudgetSettlementReceipt, (args.org, "failed-request"))
    assert receipt.cost_usd == 0 and receipt.reservation_scope_keys == [d.target.key()]
    audit = await d.db.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "provider_zero_usage_reconciled"))
    assert audit and json.loads(audit.reason)["items"][0]["evidence"]["event_id"] == "event-one"
    # Receipt persistence, not an unjournaled Redis deletion, owns recovery.
    assert await d.client.hgetall(d.target.key()) == before
    snapshot = await flow_meter.reconcile_flow_meter(d.db, org_id=args.org, flow_id=args.flow, policy=d.policy)
    assert snapshot.total_usd == 0
    assert not await d.client.hexists(d.target.key(), "bounded:failed-request")
    again = await flow_meter.reconcile_flow_meter(d.db, org_id=args.org, flow_id=args.flow, policy=d.policy)
    assert again.total_usd == 0

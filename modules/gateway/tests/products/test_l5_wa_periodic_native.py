"""CloudTrail readers expose exact selected facts and never infer completeness."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.l5_wa_periodic_native import audit_snapshot

IDENTITY = {"account_id": "123456789012", "region": "us-east-1", "role_arn": "arn:aws:iam::123456789012:role/selected"}
PROFILE = "arn:aws:wellarchitected:us-east-1:123456789012:agent-profile/selected"
START = datetime(2026, 10, 31, tzinfo=UTC)


def event(**change):
    value = {
        "eventSource": "wellarchitected.amazonaws.com",
        "eventName": "StartAgentRecommendationGeneration",
        "recipientAccountId": IDENTITY["account_id"],
        "awsRegion": IDENTITY["region"],
        "userIdentity": {"sessionContext": {"sessionIssuer": {"arn": IDENTITY["role_arn"]}}, "accessKeyId": "never-return"},
        "requestParameters": {"profileArn": PROFILE, "name": "selected-name"},
        "responseElements": {"id": "native-id"},
        "eventTime": START.isoformat(),
        "eventID": "event-id",
        "requestID": "request-id",
        **change,
    }
    return {"CloudTrailEvent": json.dumps(value)}


def test_exact_native_audit_filters_before_projection_and_retains_delivery_uncertainty():
    client = SimpleNamespace(
        lookup_events=Mock(
            return_value={
                "Events": [
                    event(requestParameters={"profileArn": "foreign-profile", "name": "PRIVATE-FOREIGN"}),
                    event(recipientAccountId="000000000000"),
                    event(),
                ]
            }
        )
    )
    provider = SimpleNamespace(client=lambda *args, **kwargs: client)
    observed = audit_snapshot(provider, IDENTITY, PROFILE, START, START + timedelta(minutes=1))
    assert len(observed["events"]) == 1
    assert observed["events"][0]["request_id"] == "request-id"
    assert "PRIVATE-FOREIGN" not in json.dumps(observed) and "never-return" not in json.dumps(observed)
    assert observed["delivery_complete"] is False and observed["pagination_complete"] is True
    assert client.lookup_events.call_args.kwargs["LookupAttributes"] == [
        {"AttributeKey": "EventName", "AttributeValue": "StartAgentRecommendationGeneration"}
    ]


@pytest.mark.parametrize("change", ["repeat-token", "missing-id", "too-large", "out-of-window"])
def test_incomplete_native_audit_never_returns_partial_facts(change):
    selected = event()
    page = {"Events": [selected]}
    if change == "repeat-token":
        page["NextToken"] = "same"
    elif change == "missing-id":
        page["Events"] = [event(requestID=None)]
    elif change == "too-large":
        page["Events"] = [{"CloudTrailEvent": "x" * 65537}]
    else:
        page["Events"] = [event(eventTime=(START - timedelta(minutes=1)).isoformat())]
    client = SimpleNamespace(lookup_events=Mock(return_value=page))
    with pytest.raises(ValueError):
        audit_snapshot(SimpleNamespace(client=lambda *args, **kwargs: client), IDENTITY, PROFILE, START, START + timedelta(minutes=1))

"""Bounded chain reads preserve tenant scope and report provider failures."""

from threading import Event

import pytest
from botocore.exceptions import ClientError

from src.activity.service import ActivityService


def test_bounded_chain_counts_empty_pages_and_preserves_truncation(monkeypatch, mock_dynamodb_resource, mock_dynamodb_table):
    monkeypatch.setattr("src.activity.service.time.monotonic", lambda: 100)
    mock_dynamodb_table.query.return_value = {"Items": [], "LastEvaluatedKey": {"event_id": "next"}}
    reader = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

    chain = reader.get_chain_bounded("run-1", tenant_id="tenant-a", deadline=103, cancelled=Event().is_set)

    assert chain.depth_capped is True
    assert chain.items == []
    assert mock_dynamodb_table.query.call_count == 20
    assert all("FilterExpression" in call.kwargs for call in mock_dynamodb_table.query.call_args_list)


def test_bounded_chain_does_not_start_without_call_budget(monkeypatch, mock_dynamodb_resource, mock_dynamodb_table):
    monkeypatch.setattr("src.activity.service.time.monotonic", lambda: 100)
    reader = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

    with pytest.raises(TimeoutError):
        reader.get_chain_bounded("run-1", tenant_id="tenant-a", deadline=102, cancelled=Event().is_set)
    mock_dynamodb_table.query.assert_not_called()


def test_bounded_chain_stops_after_cancellation(monkeypatch, mock_dynamodb_resource, mock_dynamodb_table):
    monkeypatch.setattr("src.activity.service.time.monotonic", lambda: 100)
    cancellation = Event()

    def first_page(**_kwargs):
        cancellation.set()
        return {"Items": [], "LastEvaluatedKey": {"event_id": "next"}}

    mock_dynamodb_table.query.side_effect = first_page
    reader = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

    with pytest.raises(TimeoutError):
        reader.get_chain_bounded("run-1", tenant_id="tenant-a", deadline=103, cancelled=cancellation.is_set)
    assert mock_dynamodb_table.query.call_count == 1


def test_bounded_chain_preserves_provider_error(monkeypatch, mock_dynamodb_resource, mock_dynamodb_table):
    monkeypatch.setattr("src.activity.service.time.monotonic", lambda: 100)
    mock_dynamodb_table.query.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "Missing table"}}, "Query")
    reader = ActivityService(table_name="test-table", dynamodb_resource=mock_dynamodb_resource)

    with pytest.raises(ClientError):
        reader.get_chain_bounded("run-1", tenant_id="tenant-a", deadline=103, cancelled=Event().is_set)
    assert reader.get_chain("run-1", tenant_id="tenant-a").items == []

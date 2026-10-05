"""The first persisted acceptance event must be consumable by public SSE readers."""

import sys
from pathlib import Path

import pytest

from tests.tasks import test_store as storage_tests


@pytest.fixture
def client():
    yield from storage_tests.client.__wrapped__()


@pytest.fixture
def store(client):
    return storage_tests.store.__wrapped__(client)


def test_accepted_event_validates_against_public_event_contract(store):
    request = storage_tests._request()
    store.accept(request)
    row = store.read_events(task_id=request.task_id, limit=1)[0]
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "scripts/task-api"))
    from _schema import Registry, validate

    registry = Registry(root / "docs/task-api/contracts/v1/schemas")
    body = {key: row[key] for key in ("task_id", "invocation_id", "generation", "sequence", "type", "timestamp", "data")}
    body.update(schema_version="1.0", runtime_attempt_id=row.get("runtime_attempt_id"), event_id=row["task_event_id"])
    assert validate(body, {"$ref": "events.schema.json#/$defs/event"}, registry, "events.schema.json") == []
    assert body["data"]["version"] == store.read_task(request.task_id)["version"]


@pytest.mark.parametrize("number", [1.5, 1e20, -1e20, 9007199254740991])
def test_stored_json_rehashes_to_original_request_digest(store, number):
    from src.tasks.records import payload_digest

    request = storage_tests._request(request_payload={"instructions": "inspect", "inputs": {"number": number}})
    accepted = store.accept(request)
    restored = store.read_task(accepted.task_id)["input_payload"]
    assert payload_digest(restored) == payload_digest(request.request_payload)

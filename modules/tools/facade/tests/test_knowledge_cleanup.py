"""Knowledge reads close only after every original operation is resolved."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from tools_facade.executions import STATELESS_READ_TOOLS, ExecutionStore
from tools_facade.reconciler import run_once


def authority():
    return SimpleNamespace(
        principal=SimpleNamespace(tenant_id="tenant-one", user_id="person-one", team_id="team-one"),
        run_id=None, workload_id=None, run_expires_at=None, receipt_id="knowledge-fixture-receipt",
    )


def read_row(store, identifier):
    return store._client().get_item(
        TableName=store._table(), Key={"execution_id": {"S": str(identifier)}}, ConsistentRead=True,
    )["Item"]


@pytest.mark.parametrize("tool", sorted(STATELESS_READ_TOOLS - {"websearch___search"}))
@pytest.mark.parametrize("scheduled", [False, True])
def test_completed_knowledge_read_closes_without_another_provider_call(tool, scheduled):
    store, decision = ExecutionStore(clock=lambda: 1000), authority()
    key, operation, _ = store.claim(None, uuid4(), tool, {"query": "private-fixture"}, decision)
    identifier = UUID(operation["operation_execution_id"]["S"])
    store.dispatched(key)
    store.confirmed(key, {"result": {"content": [{"type": "text", "text": "fixture result"}]}})
    assert store.stateless_ready(identifier)
    store.close(identifier, tool, decision)
    if scheduled:
        run_once(ExecutionStore(clock=lambda: 1001),
                 stop_provider=lambda *_: pytest.fail("read cleanup must not dispatch"))
    else:
        assert store.confirm_closed(identifier, tool, decision)["cleanup_status"] == "confirmed"
    row = read_row(store, identifier)
    assert store.cleanup_proved(row)
    assert row["accepted_operations"]["N"] == "1" and row["unresolved_operations"]["N"] == "0"
    assert row["cleanup_evidence"]["S"] == "all_operations_settled"


@pytest.mark.parametrize("fault", ["unknown", "failed", "sibling", "untracked"])
def test_knowledge_cleanup_retains_unknown_failed_or_untracked_operations(fault):
    store, decision, tool = ExecutionStore(clock=lambda: 1000), authority(), "knowledge___search"
    key, operation, _ = store.claim(None, uuid4(), tool, {"query": "original"}, decision)
    identifier = UUID(operation["operation_execution_id"]["S"])
    store.dispatched(key)
    if fault != "unknown":
        store.confirmed(key, {"result": {"isError": fault == "failed", "content": []}})
    if fault == "sibling":
        sibling, _, _ = store.claim(identifier, uuid4(), tool, {"query": "second"}, decision)
        store.dispatched(sibling)
    if fault == "untracked":
        store._client().update_item(
            TableName=store._table(), Key={"execution_id": {"S": str(identifier)}},
            UpdateExpression="REMOVE operation_tracking, accepted_operations, unresolved_operations",
        )
    assert not store.stateless_ready(identifier)
    store.close(identifier, tool, decision)
    assert store.confirm_closed(identifier, tool, decision)["cleanup_status"] == "unknown"
    run_once(ExecutionStore(clock=lambda: 1001))
    row = read_row(store, identifier)
    assert row["state"]["S"] == "closing" and not store.cleanup_proved(row)


@pytest.mark.parametrize("tool", ["knowledge___remember", "knowledge___experience"])
def test_unrelated_or_write_tools_cannot_inherit_stateless_read_cleanup(tool):
    store, decision = ExecutionStore(clock=lambda: 1000), authority()
    key, operation, _ = store.claim(None, uuid4(), tool, {}, decision)
    identifier = UUID(operation["operation_execution_id"]["S"])
    store.dispatched(key)
    with pytest.raises(HTTPException, match="knowledge memory completion unavailable"):
        store.confirmed(key, {"result": {"content": []}})
    store.close(identifier, tool, decision)
    assert not store.stateless_ready(identifier)
    assert not store.confirm_due_search(read_row(store, identifier))
    assert store.confirm_closed(identifier, tool, decision)["cleanup_status"] == "unknown"


@pytest.mark.parametrize("tool", ["credentials___assume_role", "credentials___materialize"])
def test_private_credentials_are_refused_before_generic_cleanup_tracking(tool, monkeypatch):
    store = ExecutionStore(clock=lambda: 1000)
    monkeypatch.setattr(store, "_client", lambda: pytest.fail("private delivery entered generic execution store"))
    with pytest.raises(HTTPException, match="private_credential_delivery_required") as failure:
        store.claim(None, uuid4(), tool, {}, authority())
    assert failure.value.status_code == 409

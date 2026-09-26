"""Canonical knowledge CLI preview and durable reindex reservation regressions."""

import hashlib
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from src.knowledge import routes
from tests.knowledge.conftest import FakeAsyncSession, FakeResult, FakeRow, FakeTokenContext


@pytest.mark.anyio
async def test_reindex_replay_never_resets_or_dispatches(make_client, fake_user, monkeypatch):
    key = str(uuid.uuid4())
    receipt = hashlib.sha256(f"{fake_user.user_id}:{key}".encode()).hexdigest()
    row = FakeRow(status="queued", metadata={"_cli_reindex_requests": [receipt]})
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(rows=[row])]
    dispatch = AsyncMock()
    monkeypatch.setattr(routes, "dispatch_ingestion", dispatch)
    async with make_client(db, fake_user) as client:
        response = await client.post(f"/api/agent-context/assets/{row.id}/reindex?request_id={key}")
    assert response.status_code == 200
    assert not db.committed
    dispatch.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("keyed", [True, False])
@pytest.mark.parametrize("status", ["registered", "queued", "indexing", "removed"])
async def test_all_callers_refuse_inflight_or_removed_reindex(make_client, fake_user, monkeypatch, keyed, status):
    row = FakeRow(status=status)
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(rows=[row])]
    dispatch = AsyncMock()
    monkeypatch.setattr(routes, "dispatch_ingestion", dispatch)
    query = f"?request_id={uuid.uuid4()}" if keyed else ""
    async with make_client(db, fake_user) as client:
        response = await client.post(f"/api/agent-context/assets/{row.id}/reindex{query}")
    assert response.status_code == 409
    dispatch.assert_not_awaited()


@pytest.mark.anyio
async def test_lost_compare_and_swap_cannot_dispatch(make_client, fake_user, monkeypatch):
    row = FakeRow(status="failed")
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(rows=[row]), FakeResult()]
    db.rollback = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(routes, "dispatch_ingestion", dispatch)
    async with make_client(db, fake_user) as client:
        response = await client.post(f"/api/agent-context/assets/{row.id}/reindex?request_id={uuid.uuid4()}")
    assert response.status_code == 409
    assert not db.committed
    dispatch.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["failed", "indexed", "complete"])
async def test_reindex_persists_key_before_dispatch(make_client, fake_user, monkeypatch, status):
    if status == "complete":
        from src.internal.status_callback_routes import StatusCallbackRequest

        # Use the same successful state carried by the real worker callback.
        status = StatusCallbackRequest(asset_id=str(uuid.uuid4()), status="complete").status
    row = FakeRow(status=status)
    row.installation_id = None
    key = str(uuid.uuid4())
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(rows=[row]), FakeResult(rows=[row]), FakeResult(rows=[row])]

    async def dispatch(**kwargs):
        assert db.committed
        return True

    mocked = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(routes, "dispatch_ingestion", mocked)
    async with make_client(db, fake_user) as client:
        response = await client.post(f"/api/agent-context/assets/{row.id}/reindex?request_id={key}")
    assert response.status_code == 200
    mocked.assert_awaited_once()
    updates = [(str(sql), params) for sql, params in db.executed_statements if "RETURNING id" in str(sql)]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "updated_at = :updated_at AND status = :status" in sql
    assert params["status"] == status
    receipt = hashlib.sha256(f"{fake_user.user_id}:{key}".encode()).hexdigest()
    assert receipt in json.loads(params["metadata"])["_cli_reindex_requests"]


@pytest.mark.anyio
async def test_reindex_scope_authorization_precedes_receipt_replay(make_client, monkeypatch):
    caller = FakeTokenContext(user_id="intruder", org_id="other")
    key = str(uuid.uuid4())
    receipt = hashlib.sha256(f"{caller.user_id}:{key}".encode()).hexdigest()
    row = FakeRow(metadata={"_cli_reindex_requests": [receipt]})
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(rows=[row])]
    async with make_client(db, caller) as client:
        response = await client.post(f"/api/agent-context/assets/{row.id}/reindex?request_id={key}")
    assert response.status_code == 404


@pytest.mark.anyio
async def test_json_preview_reuses_canonical_readonly_checks(make_client, fake_user):
    db = FakeAsyncSession()
    db.execute_results = [FakeResult(), FakeResult()]
    async with make_client(db, fake_user) as client:
        response = await client.post(
            "/agent-context/assets/bulk/preview-json",
            json={"scope": "personal", "items": [{"source_ref": "https://example.test/guide", "asset_type": "url"}]},
        )
    assert response.status_code == 200
    assert response.json()["valid"][0]["source_ref"] == "https://example.test/guide"
    assert db.committed is False
    assert all("SELECT" in str(sql) for sql, _ in db.executed_statements)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "item",
    [
        {"source_ref": "https://example.test", "asset_type": "repo"},
        {"source_ref": "https://example.test", "asset_type": "url", "display_name": "first|injected"},
        {"source_ref": "https://example.test", "asset_type": "url", "tags": {"x": "a,b:c"}},
    ],
)
async def test_preview_refuses_lossy_bulk_conversion(make_client, fake_user, item):
    db = FakeAsyncSession()
    async with make_client(db, fake_user) as client:
        response = await client.post("/agent-context/assets/bulk/preview-json", json={"scope": "personal", "items": [item]})
    assert response.status_code == 422
    assert not db.executed_statements


@pytest.mark.anyio
async def test_tenant_bulk_preview_keeps_admin_guard(make_client, fake_user):
    db = FakeAsyncSession()
    async with make_client(db, fake_user) as client:
        response = await client.post(
            "/agent-context/assets/bulk/preview-json",
            json={"scope": "tenant", "items": [{"source_ref": "https://example.test", "asset_type": "url"}]},
        )
    assert response.status_code == 403
    assert not db.committed

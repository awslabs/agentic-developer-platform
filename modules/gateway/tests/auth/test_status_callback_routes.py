"""Mounted callback contract tests; actual row isolation is tested on PostgreSQL."""

from collections import namedtuple
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.status_callback_routes import router
from src.knowledge.ingestion_callback_grant import mint_ingestion_grant
from src.shared.database_agent_context import get_agent_context_db

_ASSET_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OTHER = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_VALID_KEY = "test-internal-api-key"
_Row = namedtuple("Row", "id")


class FakeDBSession:
    def __init__(self):
        self.exists = True
        self.committed = False
        self.last_query = None
        self.last_params = None

    async def execute(self, query, params=None):
        self.last_query = str(query).lower()
        self.last_params = params
        result = MagicMock()
        result.fetchone.return_value = _Row(_ASSET_ID) if self.exists else None
        return result

    async def commit(self):
        self.committed = True


@pytest.fixture
def db():
    return FakeDBSession()


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "offline-callback-key" * 3)


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router)

    async def database():
        yield db

    async def transport(x_internal_api_key: str | None = Header(None)):
        if x_internal_api_key != _VALID_KEY:
            raise HTTPException(403)

    app.dependency_overrides[get_agent_context_db] = database
    app.dependency_overrides[verify_internal_or_irsa] = transport
    with TestClient(app) as client:
        yield client


def grant(tenant="tenant-abc", asset=_ASSET_ID):
    return mint_ingestion_grant(asset_id=asset, tenant_id=tenant)


def post(client, payload, headers=None):
    return client.post(
        "/internal/v1/knowledge-assets/status-callback", json=payload, headers=headers if headers is not None else {"X-Internal-Api-Key": _VALID_KEY}
    )


def payload(**changes):
    return {"asset_id": _ASSET_ID, "status": "indexing", "callback_grant": grant(), **changes}


@pytest.mark.parametrize("status", ["indexing", "complete", "failed"])
def test_valid_attempt_updates_only_knowledge_asset(client, db, status):
    response = post(client, payload(status=status, error="failure" if status == "failed" else None))
    assert response.status_code == 200, response.text
    assert db.committed
    assert "knowledge_assets" in db.last_query
    assert all(table not in db.last_query for table in ("repositories", "index_runs", "index_run_stages"))
    assert "tenant_id = :tenant_id" in db.last_query
    assert "tenant_id is null" not in db.last_query
    assert "ingestion_attempt_id = cast(:attempt_id as uuid)" in db.last_query
    assert "callback_grant_sha256 = :grant_digest" in db.last_query
    assert "status in ('registered', 'queued', 'indexing')" in db.last_query
    assert db.last_params["tenant_id"] == "tenant-abc"
    assert len(db.last_params["grant_digest"]) == 64
    if status == "failed":
        assert "retry_count = retry_count + 1" in db.last_query


def test_status_detail_is_preserved(client, db):
    detail = {"duration_sec": 12, "steps": {"cgc": "ok"}}
    response = post(client, payload(status="complete", status_detail=detail))
    assert response.status_code == 200
    import json

    assert json.loads(db.last_params["status_detail"]) == detail


def test_shared_scope_requires_explicit_signed_null(client, db):
    assert post(client, payload(callback_grant=grant(None))).status_code == 200
    assert "tenant_id is null" in db.last_query
    assert "tenant_id = :tenant_id" not in db.last_query


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Api-Key": "wrong"}])
def test_transport_authentication_required(client, db, headers):
    assert post(client, payload(), headers=headers).status_code == 403
    assert db.last_query is None


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"callback_grant": None}, "callback_grant_required"),
        ({"callback_grant": "adpk1.bogus.bogus"}, "callback_grant_invalid"),
        ({"tenant_id": "victim"}, "callback_grant_tenant_mismatch"),
    ],
)
def test_no_fallback_on_missing_or_invalid_authority(client, db, changes, error):
    response = post(client, payload(**changes))
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == error
    assert db.last_query is None
    assert not db.committed


def test_grant_cannot_select_another_asset(client, db):
    response = post(client, payload(callback_grant=grant(asset=_OTHER)))
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "callback_grant_asset_mismatch"
    assert db.last_query is None


@pytest.mark.parametrize("changes", [{"status": "arbitrary"}, {"asset_id": "not-a-uuid"}])
def test_malformed_request_rejected(client, db, changes):
    assert post(client, payload(**changes)).status_code == 400
    assert db.last_query is None


def test_missing_or_stale_attempt_returns_not_found(client, db):
    db.exists = False
    assert post(client, payload()).status_code == 404
    assert not db.committed

"""Revision and key recovery boundaries without AWS/GitHub mutations."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.admin.connections import maintenance as service

OLD = str(uuid4())
KEY = "supplied-private-key-" * 8


@pytest.fixture(autouse=True)
def isolate_audit_sink(monkeypatch):
    # These HTTP tests stub provider/SQL dependencies; real audit persistence is
    # covered by test_admin_audit_durability.py. Keep the durable route wrapper.
    from src.admin import audit_operation

    persist = AsyncMock()
    monkeypatch.setattr(audit_operation, "persist", persist)
    return persist


class Store:
    def __init__(self):
        self.current = OLD
        self.versions = {OLD: {"SecretString": "old", "VersionStages": ["AWSCURRENT"]}}
        self.writes = []
        self.lose_ack = False

    def get_secret_value(self, SecretId, VersionId=None):  # noqa: N803 -- AWS API argument names
        if SecretId.endswith("-id"):
            return {"SecretString": "123"}
        version = VersionId or self.current
        if version not in self.versions:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")
        return {"VersionId": version, **self.versions[version]}

    def put_secret_value(self, **kwargs):
        self.writes.append("stage")
        for row in self.versions.values():
            row["VersionStages"] = [s for s in row["VersionStages"] if s != "AWSPENDING"]
        self.versions[kwargs["ClientRequestToken"]] = {"SecretString": kwargs["SecretString"], "VersionStages": ["AWSPENDING"]}

    def update_secret_version_stage(self, **kwargs):
        assert kwargs["RemoveFromVersionId"] == self.current
        self.writes.append("activate")
        self.versions[self.current]["VersionStages"] = ["AWSPREVIOUS"]
        self.current = kwargs["MoveToVersionId"]
        self.versions[self.current]["VersionStages"].append("AWSCURRENT")
        if self.lose_ack:
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "UpdateSecretVersionStage")


@pytest.fixture
def store(monkeypatch):
    sm = Store()
    monkeypatch.setattr(service, "_store", lambda: sm)
    monkeypatch.setattr(service, "_get_environment", lambda: "test")
    monkeypatch.setattr(service, "_mint_app_jwt", lambda *args: "redacted-fixture")
    response = Mock()
    response.json.return_value = {"id": 123}
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(service, "invalidate_app_credentials_cache", Mock())
    monkeypatch.setattr(service, "_invalidate_verification_cache", Mock())
    return sm


def request(**changes):
    return service.AppKeyRequest(
        **{"expected_app_id": "123", "expected_key_version": OLD, "operation_id": str(uuid4()), "private_key": KEY, **changes}
    )


async def test_lost_activation_ack_replays_without_another_write(store):
    req = request()
    store.lose_ack = True
    with pytest.raises(HTTPException):
        await service.rotate_supplied_key(req)
    writes = list(store.writes)
    result = await service.rotate_supplied_key(req)
    assert result["replayed"] is True
    assert store.current == str(req.operation_id)
    assert store.versions[OLD]["SecretString"] == "old"
    assert store.writes == writes
    assert KEY not in str(result)


async def test_pending_retry_verifies_then_activates(store):
    req = request()
    store.versions[str(req.operation_id)] = {"SecretString": KEY, "VersionStages": ["AWSPENDING"]}
    assert (await service.rotate_supplied_key(req))["rotated"] is True
    assert store.current == str(req.operation_id)


@pytest.mark.parametrize("changes", [{"expected_app_id": "456"}, {"expected_key_version": str(uuid4())}])
async def test_stale_review_never_writes(store, changes):
    with pytest.raises(HTTPException) as exc:
        await service.rotate_supplied_key(request(**changes))
    assert exc.value.status_code == 409
    assert store.writes == []


async def test_completed_old_operation_never_reactivated(store):
    req = request()
    store.versions[str(req.operation_id)] = {"SecretString": KEY, "VersionStages": ["AWSPREVIOUS"]}
    with pytest.raises(HTTPException):
        await service.rotate_supplied_key(req)
    assert store.writes == []


async def test_replay_changed_payload_is_refused(store):
    req = request()
    await service.rotate_supplied_key(req)
    req.private_key = service.SecretStr("different-secret-" * 8)
    with pytest.raises(HTTPException) as exc:
        await service.rotate_supplied_key(req)
    assert exc.value.status_code == 409


async def test_wrong_github_app_never_stages_key(store, monkeypatch):
    monkeypatch.setattr(service, "_mint_app_jwt", Mock(side_effect=ValueError(KEY)))
    with pytest.raises(HTTPException) as exc:
        await service.rotate_supplied_key(request())
    assert KEY not in str(exc.value)
    assert store.writes == []


def test_invalid_key_body_never_echoes_secret(monkeypatch):
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections import routes
    from tests.admin.connections.test_app_lifecycle import _make_client, _make_user

    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "rotate_supplied_key", AsyncMock())
    client = _make_client(app, user=_make_user(is_admin=True), mock_db=AsyncMock(spec=AsyncSession))
    response = client.post("/admin/connections/github/app/maintenance/rotate-key", json={"private_key": KEY, "expected_app_id": KEY})
    assert response.status_code == 422
    assert KEY not in response.text
    routes.rotate_supplied_key.assert_not_awaited()


def test_maintenance_requires_platform_admin():
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections import routes
    from tests.admin.connections.test_app_lifecycle import _make_client, _make_user

    app = FastAPI()
    app.include_router(routes.router)
    client = _make_client(app, user=_make_user(is_admin=False), mock_db=AsyncMock(spec=AsyncSession))
    assert client.get("/admin/connections/github/app/maintenance").status_code == 403


async def test_lifecycle_lock_uses_dedicated_transaction_until_request_finishes(monkeypatch):
    from contextlib import asynccontextmanager

    from src.admin.connections import routes

    connection = AsyncMock()
    events = []

    @asynccontextmanager
    async def begin():
        events.append("acquired")
        try:
            yield connection
        finally:
            events.append("released")

    engine = Mock(begin=begin)
    db = AsyncMock()
    db.get_bind = Mock(return_value=Mock(dialect=Mock(name="postgresql")))
    db.get_bind.return_value.dialect.name = "postgresql"
    db.bind = engine
    guard = routes._app_lifecycle_lock(db)
    await anext(guard)
    await db.commit()
    assert events == ["acquired"]
    assert "pg_advisory_xact_lock" in str(connection.execute.call_args.args[0])
    await guard.aclose()
    assert events == ["acquired", "released"]


def test_real_route_serializes_reviewed_request_without_key_in_response(monkeypatch, isolate_audit_sink):
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections import routes
    from tests.admin.connections.test_app_lifecycle import _make_client, _make_user

    operation = str(uuid4())
    rotate = AsyncMock(return_value={"rotated": True, "app_id": "123", "operation_id": operation, "key_version": operation})
    audit = AsyncMock(wraps=routes.write_admin_audit)
    monkeypatch.setattr(routes, "rotate_supplied_key", rotate)
    monkeypatch.setattr(routes, "write_admin_audit", audit)
    app = FastAPI()
    app.include_router(routes.router)
    client = _make_client(app, user=_make_user(is_admin=True), mock_db=AsyncMock(spec=AsyncSession))
    response = client.post(
        "/admin/connections/github/app/maintenance/rotate-key",
        json={
            "expected_app_id": "123",
            "expected_key_version": OLD,
            "operation_id": operation,
            "private_key": KEY,
        },
    )
    assert response.status_code == 200
    assert isolate_audit_sink.await_count >= 2
    assert response.json()["operation_id"] == operation
    assert KEY not in response.text
    assert rotate.call_args.args[0].private_key.get_secret_value() == KEY
    assert KEY not in str(audit.call_args)


def test_all_existing_app_writers_share_lifecycle_lock():
    from src.admin.connections import routes

    writers = {
        "/github/app/register-callback",
        "/github/app/register-manual",
        "/github/app/revalidate",
        "/github/app/rotate-key",
        "/github/app/disconnect",
        "/github/app/maintenance/rotate-key",
        "/github/app/maintenance/disconnect",
    }
    seen = set()
    for route in routes.router.routes:
        suffix = route.path.removeprefix("/admin/connections")
        if suffix in writers:
            assert routes._app_lifecycle_lock in {dep.call for dep in route.dependant.dependencies}
            seen.add(suffix)
    assert seen == writers

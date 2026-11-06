"""Deferred Product peer availability and refusal contract."""

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOnboardingSession, ProductOperation
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.products.test_i04 import GatewayClient
from tests.products.test_i07_cli import cli as onboard_cli

SCRIPT = Path(__file__).parents[2] / "cli/adp-product.py"
spec = importlib.util.spec_from_file_location("adp_product_i17a", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

REQUEST = {
    "name": "Example Planner",
    "purpose": "Coordinate releases",
    "intended_users": ["Maintainers"],
    "starting_point": "idea",
    "project": {"mode": "create"},
}


@pytest.mark.asyncio
async def test_deferred_availability_matches_installed_cli_and_api(test_client, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_create_12345"})
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    api = await test_client.get(path)
    assert api.status_code == 200
    availability = api.json()["peer_availability"]
    assert set(availability) == {"context", "l5"}
    assert all(
        value["status"] == "unavailable" and value["reason"] == "integration_deferred" and "saved Product remains available" in value["message"]
        for value in availability.values()
    )
    assert api.json()["context_status"] == "unavailable"
    assert api.headers["Profile-Revision"] == "1"

    class InstalledClient:
        def request(self, method, url, body=None, **kwargs):
            response = asyncio.run_coroutine_threadsafe(test_client.request(method, url), loop).result(timeout=15)
            assert response.status_code == 200, response.text
            return response.json()

    loop = asyncio.get_running_loop()
    args = cli.parser().parse_args(["show", "--product", created.json()["product_id"], "--project", created.json()["project_id"], "--json"])
    shown = await asyncio.to_thread(cli.execute, args, InstalledClient())
    assert shown["detail"]["peer_availability"] == availability
    assert shown["detail"]["revision"] == api.json()["revision"]
    old = await test_client.get(path + "/revisions/1")
    assert old.status_code == 200
    assert old.json()["peer_availability"] == availability


@pytest.mark.asyncio
async def test_deferred_request_refuses_without_creating_work(test_client, db_session, source_member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_refusal_12345"})
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    original_operations = await db_session.scalar(select(func.count()).select_from(ProductOperation))
    original_changes = await db_session.scalar(select(func.count()).select_from(ProductChange))
    provider = Mock(side_effect=AssertionError("Deferred peer must not dispatch"))
    monkeypatch.setattr("boto3.client", provider)
    for peer in ("context", "l5", "context"):
        refused = await test_client.post(path + f"/peer-requests/{peer}", headers={"If-Profile-Revision": "1"})
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"] == {
            "code": "peer_integration_deferred",
            "peer": peer,
            **(await test_client.get(path)).json()["peer_availability"][peer],
        }
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == original_operations
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == original_changes
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_retry_and_older_session_remain_inert_after_restart(test_client, test_engine, source_member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_restart_12345"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    started = await test_client.post(
        path + "/onboarding-sessions",
        json={},
        headers={
            "Idempotency-Key": "deferred_old_session_12345",
            "If-Profile-Revision": "1",
        },
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        operations_before = (await db.scalars(select(ProductOperation).order_by(ProductOperation.id))).all()
        operation_ids = [operation.id for operation in operations_before]
        change_ids = (await db.scalars(select(ProductChange.id).order_by(ProductChange.id))).all()
    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 10, 20, tzinfo=UTC))
    provider = Mock(side_effect=AssertionError("Deferred peer must not dispatch"))
    monkeypatch.setattr("boto3.client", provider)

    for peer in ("context", "l5", "context"):
        response = await test_client.post(
            path + f"/peer-requests/{peer}",
            headers={
                "If-Profile-Revision": "1",
                "Idempotency-Key": f"deferred_retry_{peer}_12345",
            },
        )
        assert response.status_code == 503 and response.json()["detail"]["code"] == "peer_integration_deferred"
        assert (await test_client.get(path)).json()["peer_availability"][peer]["status"] == "unavailable"
        resumed = await test_client.get(path + f"/onboarding-sessions/{session_id}")
        assert resumed.status_code == 200 and resumed.json()["operation_ids"] == []
        assert resumed.json()["id"] == session_id and resumed.json()["state"] == "open"

    from src.app import create_app

    restarted = create_app()
    restarted.dependency_overrides[get_current_user] = lambda: source_member

    async def fresh_db():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            yield db

    restarted.dependency_overrides[get_db] = fresh_db
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as browser:
        for _ in range(3):
            response = await browser.post(path + "/peer-requests/context", headers={"If-Profile-Revision": "1"})
            assert response.status_code == 503 and response.json()["detail"]["code"] == "peer_integration_deferred"
            assert (await browser.get(path)).json()["peer_availability"]["context"]["status"] == "unavailable"
            assert (await browser.get(path + f"/onboarding-sessions/{session_id}")).json()["operation_ids"] == []

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        operations_after = (await db.scalars(select(ProductOperation).order_by(ProductOperation.id))).all()
        assert [operation.id for operation in operations_after] == operation_ids
        assert all(
            operation.state == "succeeded" and operation.provider_operation_id is None and "callback_scope" not in operation.request
            for operation in operations_after
        )
        assert (await db.scalars(select(ProductChange.id).order_by(ProductChange.id))).all() == change_ids
        session = await db.get(ProductOnboardingSession, (source_member.org_id, session_id))
        assert session.state == "open" and session.proposals == []
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_peer_requests_do_not_use_external_dispatch(test_client, source_member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_dispatch_spy_12345"})
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    original_request = httpx.AsyncClient.request
    observed = []

    async def local_requests_only(client, method, url, *args, **kwargs):
        assert isinstance(url, str) and url.startswith("/"), "Deferred request attempted external HTTP dispatch"
        observed.append((method, url))
        return await original_request(client, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", local_requests_only)
    provider = Mock(side_effect=AssertionError("Deferred request attempted provider dispatch"))
    monkeypatch.setattr("boto3.client", provider)
    for peer in ("context", "l5"):
        response = await test_client.post(path + f"/peer-requests/{peer}", headers={"If-Profile-Revision": "1"})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "peer_integration_deferred"
    assert len(observed) == 2
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_deferred_peer_scope_denies_foreign_and_revoked_actor(test_client, db_session, app, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_scope_create_12345"})
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"
    missing = f"/v1/products/no-such-product/projects/{created.json()['project_id']}"
    assert (await test_client.get(path)).status_code == 200

    foreign = source_member.model_copy(update={"org_id": "other-workspace", "user_id": "foreign-user"})
    db_session.add(Organization(id=foreign.org_id, name="Other workspace"))
    db_session.add(User(id=foreign.user_id, org_id=foreign.org_id, team_id="foreign-team", email="foreign@example.test", user_kind="human"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign
    for method, suffix in (("GET", ""), ("GET", "/revisions/1"), ("POST", "/peer-requests/context"), ("POST", "/peer-requests/l5")):
        kwargs = {"headers": {"If-Profile-Revision": "1"}} if method == "POST" else {}
        denied = await test_client.request(method, path + suffix, **kwargs)
        unknown = await test_client.request(method, missing + suffix, **kwargs)
        assert denied.status_code == unknown.status_code == 404
        assert denied.json() == unknown.json() == {"detail": {"code": "not_found"}}
        assert created.json()["product_id"] not in denied.text and REQUEST["purpose"] not in denied.text

    app.dependency_overrides[get_current_user] = lambda: source_member
    db_session.add(
        TenantMembership(
            id="deferred-revoked-member",
            tenant_id=source_member.org_id,
            user_id=source_member.user_id,
            role="member",
            revoked_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    for method, suffix in (("GET", ""), ("GET", "/revisions/1"), ("POST", "/peer-requests/context")):
        kwargs = {"headers": {"If-Profile-Revision": "1"}} if method == "POST" else {}
        denied = await test_client.request(method, path + suffix, **kwargs)
        unknown = await test_client.request(method, missing + suffix, **kwargs)
        assert denied.status_code == unknown.status_code and denied.status_code in (403, 404)
        assert denied.json() == unknown.json()
        assert REQUEST["purpose"] not in denied.text


@pytest.mark.asyncio
async def test_deferred_peer_stale_and_replay_preserve_profile_lineage(test_client, db_session, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_revision_create_12345"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    initial_feed = (await test_client.get(path + "/changes")).json()["items"]
    assert len(initial_feed) == 1
    updated = await test_client.patch(
        path,
        json={"purpose": "Manually updated Profile"},
        headers={
            "If-Profile-Revision": "1",
            "Idempotency-Key": "deferred_manual_edit_12345",
        },
    )
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    before = (await test_client.get(path + "/changes")).json()["items"]
    assert [change["revision"] for change in before] == [1, 2]
    for peer in ("context", "l5"):
        stale = await test_client.post(
            path + f"/peer-requests/{peer}",
            headers={
                "If-Profile-Revision": "1",
                "Idempotency-Key": "deferred_stale_request_12345",
            },
        )
        assert stale.status_code == 412 and stale.json() == {"detail": {"code": "stale_revision"}}
    for _ in range(2):
        refused = await test_client.post(
            path + "/peer-requests/context",
            headers={
                "If-Profile-Revision": "2",
                "Idempotency-Key": "deferred_replay_request_12345",
            },
        )
        assert refused.status_code == 503 and refused.json()["detail"]["code"] == "peer_integration_deferred"
        assert "receipt" not in refused.json()["detail"]
    current = await test_client.get(path)
    historical = await test_client.get(path + "/revisions/1")
    assert current.status_code == historical.status_code == 200
    assert current.json()["product"]["purpose"] == "Manually updated Profile"
    assert historical.json()["product"]["purpose"] == REQUEST["purpose"]
    assert current.json()["peer_availability"] == historical.json()["peer_availability"]
    after = (await test_client.get(path + "/changes")).json()["items"]
    assert after == before and before[0] == initial_feed[0]
    assert {ref["record_id"] for ref in after[0]["relationship_versions"][0]["source_refs"]} == {product_id, project_id}
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2


@pytest.mark.asyncio
async def test_deferred_peers_preserve_cross_client_resume_after_restart(test_client, test_engine, source_member, tmp_path):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "deferred_continuity_12345"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    path = f"/v1/products/{product_id}/projects/{project_id}"
    original = (await test_client.get(path)).json()
    started = await test_client.post(
        path + "/onboarding-sessions",
        json={},
        headers={
            "Idempotency-Key": "deferred_session_12345",
            "If-Profile-Revision": "1",
        },
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["id"]
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    patch = tmp_path / "edit.json"
    patch.write_text('{"name":"Planner after CLI edit"}', encoding="utf-8")
    args = cli.parser().parse_args(
        [
            "update",
            "--product",
            product_id,
            "--project",
            project_id,
            "--revision",
            "1",
            "--idempotency-key",
            "deferred_cli_edit_12345",
            "--input",
            str(patch),
            "--json",
        ]
    )
    edited = (await asyncio.to_thread(cli.execute, args, gateway))["detail"]
    assert edited["revision"] == 2 and edited["product"]["name"] == "Planner after CLI edit"
    args = onboard_cli.parser().parse_args(
        [
            "resume",
            "--product",
            product_id,
            "--project",
            project_id,
            "--session",
            session_id,
            "--json",
        ]
    )
    resumed = (await asyncio.to_thread(onboard_cli.execute, args, gateway))["detail"]
    assert resumed["id"] == session_id and resumed["revision"] == 2
    assert resumed["steps"]["information"]["status"] == "complete"

    from src.app import create_app

    restarted = create_app()
    restarted.dependency_overrides[get_current_user] = lambda: source_member

    async def fresh_db():
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            yield db

    restarted.dependency_overrides[get_db] = fresh_db
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as browser:
        profile = await browser.get(path)
        session = await browser.get(path + f"/onboarding-sessions/{session_id}")
        assert profile.status_code == session.status_code == 200
        assert session.json() == resumed
        assert profile.json()["peer_availability"] == original["peer_availability"]
        assert profile.json()["product"] == {**original["product"], "name": "Planner after CLI edit"}
        assert profile.json()["project"] == original["project"]
        assert profile.json()["configuration"] == original["configuration"]
        assert profile.json()["provenance"] == original["provenance"]
        assert profile.json()["revision"] == session.json()["revision"] == 2
        assert profile.json()["progress"]["maturity"] == "saved"
        history = await browser.get(path + "/changes")
        assert history.status_code == 200
        assert [change["revision"] for change in history.json()["items"]] == [1, 2]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        stored_product = await db.get(Product, (source_member.org_id, product_id))
        stored_session = await db.get(ProductOnboardingSession, (source_member.org_id, session_id))
        original_change = await db.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 1))
        assert stored_product.name == "Planner after CLI edit" and stored_product.revision == 2
        assert stored_session.product_id == product_id and stored_session.project_id == project_id
        assert stored_session.steps["information"]["status"] == "complete"
        assert {ref["record_id"] for ref in original_change.relationship_versions[0]["source_refs"]} == {product_id, project_id}

"""Source selection and independent readback through the gateway surface."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.app import create_app
from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductOperation, ProductSourceBinding
from src.products.verification import observe_binding
from src.shared.database import get_db
from src.shared.models.vault import ChannelTenantMap, UserCredential
from tests.products.test_i08_api import mutation, setup_source
from tests.products.test_i08_downstream import saved_source
from tests.products.test_i08_verification import FakeGitHub


@pytest.mark.asyncio
async def binding_verified_identity(test_client, test_engine, db_session, source_member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, 6, 40, tzinfo=UTC)
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.verification_operations.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    path, selection = await setup_source(test_client, db_session, source_member)
    product_id, project_id = path.split("/")[3], path.split("/")[5]
    created = await test_client.post(path, json=selection, headers=mutation(1, "scn01_create_source_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    assert created.json()["selection"] == selection and created.headers["Profile-Revision"] == "2"
    assert created.json()["status"] == "unverified"
    before_observation = await test_client.get(path.removesuffix("/bindings"))
    assert before_observation.status_code == 200
    before_checks = {check["id"]: check["status"] for check in before_observation.json()["progress"]["checks"]}
    assert before_checks["repository"] == "missing"

    provider = FakeGitHub()

    async def read_selected_source(binding, installation):
        return await observe_binding(binding, installation, github_client=provider, now=clock)

    monkeypatch.setattr("src.products.source_routes.observe_binding", read_selected_source)
    verification = await test_client.post(
        f"{path}/{binding_id}/verify",
        headers=mutation(2, "scn01_verify_source_123"),
    )
    assert verification.status_code == 202, verification.text
    assert verification.json()["status"] == "succeeded" and verification.json()["coverage"] == "complete"
    assert provider.calls == [("installation", 17), ("repository", 17, 35), ("commit", 17, "GET", "/repos/example/app/commits/main")]
    operation_id = verification.json()["operation_id"]
    status_response = await test_client.get(path.removesuffix("/bindings") + "/operations/" + operation_id)
    assert status_response.status_code == 200 and status_response.json()["state"] == "succeeded"
    assert status_response.json()["coverage"] == "complete"
    listing = await test_client.get(path)
    assert listing.status_code == 200 and listing.headers["Profile-Revision"] == "2"
    assert listing.json()["items"] == [
        {
            "id": binding_id,
            "project_id": project_id,
            "revision": 2,
            "generation": 1,
            "selection": selection,
            "status": "healthy",
            "observed_at": clock.isoformat(),
            "stale_after": (clock + timedelta(minutes=15)).isoformat(),
            "source_revision": "a" * 40,
        }
    ]
    profile = await test_client.get(path.removesuffix("/bindings"))
    assert profile.status_code == 200
    checks = {check["id"]: check["status"] for check in profile.json()["progress"]["checks"]}
    assert checks["repository"] == "complete"

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        stored = await independent.get(ProductSourceBinding, (source_member.org_id, binding_id))
        saved_operation = await independent.get(ProductOperation, (source_member.org_id, operation_id))
        product = await independent.get(Product, (source_member.org_id, product_id))
        connection = await independent.get(ChannelTenantMap, selection["connection_ref"]["id"])
        assert (stored.tenant_id, stored.project_id, stored.installation_id) == (
            source_member.org_id,
            project_id,
            selection["connection_ref"]["id"],
        )
        assert (stored.namespace, stored.native_id, stored.desired) == (
            selection["namespace"],
            selection["native_id"],
            selection,
        )
        assert stored.observed == {
            "observed_at": clock.isoformat(),
            "stale_after": (clock + timedelta(minutes=15)).isoformat(),
            "source_revision": "a" * 40,
            "display_name": "example/app",
            "namespace": "27",
            "native_id": "0035",
        }
        assert saved_operation.state == "succeeded" and saved_operation.binding_generation == stored.generation == 1
        assert saved_operation.receipt == {"coverage": "complete"} and saved_operation.attempt_count == 1
        assert product.revision == 2 and connection.installation_id == "17"
        assert (await independent.scalar(select(func.count()).select_from(ProductSourceBinding))) == 1
        assert (await independent.scalar(select(func.count()).select_from(ChannelTenantMap))) == 1
        assert (await independent.scalar(select(func.count()).select_from(UserCredential))) == 0

    record_property("scenario_id", "SCN-01")
    record_property("criterion", "AC-01")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    requirements_revision = (
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip()
    )
    record_property("requirements_revision", requirements_revision)
    record_property("observation", json.dumps({"native_id": stored.native_id, "source_revision": stored.observed["source_revision"]}))
    record_property("attempt_report", "Gateway 201/202/200; independent persisted readback; provider reads only")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def binding_freshness_redaction(test_client, test_engine, db_session, source_member, monkeypatch, record_property):
    root, path, created, binding, observed_at = await saved_source(
        test_client,
        db_session,
        source_member,
        verified=False,
    )
    clock = observed_at + timedelta(minutes=1)
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.verification_operations.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.product_onboarding.routes.utcnow", lambda: clock)
    provider = FakeGitHub()

    async def read_selected_source(selected, installation):
        return await observe_binding(selected, installation, github_client=provider, now=observed_at)

    monkeypatch.setattr("src.products.source_routes.observe_binding", read_selected_source)
    binding_id = created.json()["id"]
    assert (await test_client.get(path)).json()["items"][0]["status"] == "unverified"
    verification = await test_client.post(
        f"{path}/{binding_id}/verify",
        headers=mutation(2, "scn02_verify_source_123"),
    )
    assert verification.status_code == 202, verification.text
    assert verification.json()["coverage"] == "complete"
    assert provider.calls == [("installation", 17), ("repository", 17, 35), ("commit", 17, "GET", "/repos/example/app/commits/main")]
    session_root = root + "/onboarding-sessions"
    started = await test_client.post(session_root, json={}, headers=mutation(2, "scn02_start_session_123"))
    assert started.status_code == 201, started.text
    session_path = session_root + "/" + started.json()["id"]
    replay_headers = mutation(2, "scn02_cached_edit_123")
    edit = await test_client.patch(root + "/configuration", json={"name": "Updated project"}, headers=replay_headers)
    assert edit.status_code == 200, edit.text
    assert "0035" in edit.text and "private-revision" in edit.text
    for response in (
        await test_client.get(root),
        await test_client.get(session_path),
        await test_client.get(root + "/revisions/2"),
    ):
        assert response.status_code == 200, response.text
        check = next(item for item in response.json()["progress"]["checks"] if item["id"] == "repository")
        assert check["status"] == "complete"

    clock = observed_at + timedelta(minutes=15)
    listed = await test_client.get(path)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["status"] == "degraded"
    assert listed.json()["items"][0]["observed_at"] == observed_at.isoformat()
    for response in (
        await test_client.get(root),
        await test_client.get(session_path),
        await test_client.get(root + "/revisions/2"),
    ):
        assert response.status_code == 200, response.text
        check = next(item for item in response.json()["progress"]["checks"] if item["id"] == "repository")
        assert check["status"] == "stale" and response.json()["progress"]["maturity"] == "saved"

    mapping = await db_session.get(ChannelTenantMap, binding.installation_id)
    mapping.ownership_disputed = True
    await db_session.commit()
    restricted = await test_client.get(path)
    assert restricted.status_code == 200
    assert restricted.json()["items"][0]["status"] == "restricted"
    responses = (
        await test_client.get(root),
        await test_client.get(session_path),
        await test_client.get(root + "/revisions/2"),
        await test_client.patch(root + "/configuration", json={"name": "Updated project"}, headers=replay_headers),
        await test_client.post(path, json=created.json()["selection"], headers=mutation(1, "create_downstream_12345")),
    )
    for response in (restricted, *responses):
        assert response.status_code == 200, response.text
        assert "0035" not in response.text and "private-revision" not in response.text
        if "progress" in response.json():
            check = next(item for item in response.json()["progress"]["checks"] if item["id"] == "repository")
            assert check["status"] != "complete"
        if "configuration" in response.json():
            assert response.json()["configuration"]["basic"]["repositories"] == []
            assert response.json()["provenance"] == []

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        stored = await independent.get(ProductSourceBinding, (source_member.org_id, binding_id))
        assert stored.status == "healthy" and stored.observed["observed_at"] == observed_at.isoformat()
        assert stored.observed["source_revision"] == "a" * 40
        assert (await independent.get(ChannelTenantMap, binding.installation_id)).ownership_disputed

    record_property("scenario_id", "SCN-02")
    record_property("criterion", "AC-02")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("observation", json.dumps({"observed_at": observed_at.isoformat(), "source_revision": stored.observed["source_revision"]}))
    record_property("attempt_report", "Gateway verify/read/replay: healthy, stale, restricted; independent persisted readback")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")


@pytest.mark.asyncio
async def binding_retry_unlink(test_client, test_engine, db_session, source_member, monkeypatch, record_property):
    clock = datetime(2026, 10, 20, 8, tzinfo=UTC)
    monkeypatch.setattr("src.products.source_routes.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.verification_operations.utcnow", lambda: clock)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: clock)
    path, selection = await setup_source(test_client, db_session, source_member)
    root = path.removesuffix("/bindings")
    created = await test_client.post(path, json=selection, headers=mutation(1, "scn03_create_binding_123"))
    assert created.status_code == 201, created.text
    binding_id = created.json()["id"]
    create_replay = await test_client.post(path, json=selection, headers=mutation(1, "scn03_create_binding_123"))
    assert create_replay.status_code == 200 and create_replay.json()["id"] == binding_id
    verify_headers = mutation(2, "scn03_verify_binding_123")
    calls = []

    async def interrupted(binding, installation):
        calls.append("interrupted")
        raise RuntimeError("simulated worker interruption")

    monkeypatch.setattr("src.products.source_routes.observe_binding", interrupted)
    with pytest.raises(RuntimeError, match="simulated worker interruption"):
        await test_client.post(f"{path}/{binding_id}/verify", headers=verify_headers)
    status_root = root + "/operations/"
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as independent:
        pending = (
            await independent.scalars(
                select(ProductOperation).where(
                    ProductOperation.tenant_id == source_member.org_id,
                    ProductOperation.operation == "verifyBinding",
                )
            )
        ).one()
        operation_id = pending.id
        assert pending.state == "running" and pending.binding_generation == 1 and pending.attempt_count == 1
        assert pending.lease_owner is not None and pending.lease_until is not None
    duplicate = await test_client.post(f"{path}/{binding_id}/verify", headers=verify_headers)
    assert duplicate.status_code == 202 and duplicate.json()["status"] == "running"
    assert duplicate.json()["operation_id"] == operation_id and calls == ["interrupted"]

    restarted_app = create_app()
    restarted_app.dependency_overrides[get_current_user] = lambda: source_member
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def restored_db():
        async with sessions() as session:
            yield session

    restarted_app.dependency_overrides[get_db] = restored_db
    async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://test") as restarted:
        running = await restarted.get(status_root + operation_id)
        assert running.status_code == 200 and running.json()["state"] == "running"
        assert "coverage" not in running.json()
        clock += timedelta(seconds=61)
        provider = FakeGitHub()

        async def observed(binding, installation):
            calls.append("observed")
            return await observe_binding(binding, installation, github_client=provider, now=clock)

        monkeypatch.setattr("src.products.source_routes.observe_binding", observed)
        resumed = await restarted.post(f"{path}/{binding_id}/verify", headers=verify_headers)
        assert resumed.status_code == 202 and resumed.json() == {
            "operation_id": operation_id,
            "status": "succeeded",
            "coverage": "complete",
        }
        assert (await restarted.post(f"{path}/{binding_id}/verify", headers=verify_headers)).json() == resumed.json()
        assert calls == ["interrupted", "observed"]
        assert provider.calls == [("installation", 17), ("repository", 17, 35), ("commit", 17, "GET", "/repos/example/app/commits/main")]
        assert (await restarted.get(status_root + operation_id)).json()["state"] == "succeeded"
        listed = (await restarted.get(path)).json()["items"]
        assert len(listed) == 1 and listed[0]["selection"] == selection
        assert listed[0]["status"] == "healthy" and listed[0]["observed_at"] == clock.isoformat()

        deletion = []

        async def late_read(binding, installation):
            calls.append("late_read")
            result = await observe_binding(binding, installation, github_client=provider, now=clock)
            deletion.append(await restarted.delete(f"{path}/{binding_id}", headers=mutation(2, "scn03_unlink_binding_123")))
            return result

        monkeypatch.setattr("src.products.source_routes.observe_binding", late_read)
        late = await restarted.post(f"{path}/{binding_id}/verify", headers=mutation(2, "scn03_late_verify_123"))
        assert late.status_code == 202 and late.json()["status"] == "cancelled"
        assert calls == ["interrupted", "observed", "late_read"]
        assert len(deletion) == 1 and deletion[0].status_code == 200
        assert deletion[0].headers["Profile-Revision"] == "3"
        assert deletion[0].json()["status"] == "unlinked" and "selection" not in deletion[0].json()
        replay = await restarted.delete(f"{path}/{binding_id}", headers=mutation(2, "scn03_unlink_binding_123"))
        assert replay.status_code == 200 and replay.json() == deletion[0].json()
        assert (await restarted.get(path)).json()["items"] == [deletion[0].json()]
        assert (await restarted.get(status_root + late.json()["operation_id"])).json()["coverage"] == "denied"
        assert (await restarted.post(f"{path}/{binding_id}/verify", headers=verify_headers)).status_code == 404
        assert (await restarted.post(f"{path}/{binding_id}/verify", headers=mutation(3, "scn03_retry_unlinked_123"))).status_code == 404
        profile = await restarted.get(root)
        assert profile.status_code == 200 and "0035" not in profile.text

    async with sessions() as independent:
        stored = await independent.get(ProductSourceBinding, (source_member.org_id, binding_id))
        assert stored.status == "unlinked" and stored.generation == 2
        assert stored.observed["observed_at"] == clock.isoformat()
        assert stored.observed["source_revision"] == "a" * 40
        attempts = (
            await independent.scalars(
                select(ProductOperation).where(
                    ProductOperation.tenant_id == source_member.org_id,
                    ProductOperation.operation == "verifyBinding",
                )
            )
        ).all()
        assert len(attempts) == 2
        saved_attempts = {attempt.id: attempt for attempt in attempts}
        assert saved_attempts[operation_id].state == "succeeded" and saved_attempts[operation_id].attempt_count == 2
        assert saved_attempts[late.json()["operation_id"]].state == "cancelled"
        assert saved_attempts[late.json()["operation_id"]].binding_generation == 1
        assert (await independent.get(Product, (source_member.org_id, path.split("/")[3]))).revision == 3
        connection = await independent.get(ChannelTenantMap, selection["connection_ref"]["id"])
        assert connection.installation_id == "17" and not connection.ownership_disputed
        assert (await independent.scalar(select(func.count()).select_from(ProductSourceBinding))) == 1
        assert (await independent.scalar(select(func.count()).select_from(ChannelTenantMap))) == 1
        assert (await independent.scalar(select(func.count()).select_from(UserCredential))) == 0

    record_property("scenario_id", "SCN-03")
    record_property("criterion", "AC-03")
    record_property("source_revision", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    record_property(
        "requirements_revision",
        os.getenv("ADP_REQUIREMENTS_REVISION")
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD:docs/product-onboarding/requirements-2026-10-13.md"],
            text=True,
        ).strip(),
    )
    record_property("observation", json.dumps({"attempts": 2, "provider_reads": provider.calls, "final_generation": stored.generation}))
    record_property("attempt_report", "Gateway restart/replay and recovered lease; unlink fences late read; independent persisted readback")
    record_property("fixture_cleanup", "function-scoped in-memory SQLite engine disposed by pytest fixture")

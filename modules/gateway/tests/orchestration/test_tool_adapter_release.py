"""A signed deployment job may advance only its exact complete release group."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration import tool_adapter_release as release
from src.shared.models.vault import ToolAdapterActivation


def test_native_release_api_is_registered_outside_the_internal_plane():
    from src.app import UNIT_MODULES

    assert release.__name__ == "src.orchestration.tool_adapter_release"
    assert UNIT_MODULES.count(release.__name__) == 1
    assert "src.internal.tool_adapter_release" not in UNIT_MODULES
    endpoints = [route for route in release.router.routes if getattr(route, "endpoint", None) is release.update_transition]
    assert len(endpoints) == 1
    route = endpoints[0]
    assert route.path == "/tools/adapter-release/transition"
    assert route.methods == {"POST"}


def plan():
    routes = {name: {"target_id": name + "-target", "backend": "legacy", "digest": "sha256:" + "c" * 64} for name in release.CONNECTORS}
    routes["atlassian"]["backend"] = "disabled"
    return {
        "api_id": "api123",
        "stage": "dev",
        "before": {"status": "legacy", "release": None, "routes": routes, "deployment_id": "old123"},
        "after": {
            "status": "active",
            "release": {
                "contract_version": "1.0",
                "source_revision": "a" * 40,
                "image_digest": "sha256:" + "b" * 64,
                "group": "tool-connectors",
                "group_generation": 1,
                "backend_generation": 1,
            },
            "deployment_id": "new123",
            "routes": {name: {**value, "backend": "adapter", "digest": "sha256:" + "d" * 64} for name, value in routes.items()},
        },
    }


def request(action, *, selected=None, observed=None, **job_changes):
    return release.TransitionRequest.model_validate(
        {
            "job": {
                "correlation": "orch:00000000-0000-0000-0000-000000000001",
                "source_revision": "a" * 40,
                "dispatch_revision": "e" * 40,
                "workflow": "tools-platform-deploy.yml",
                "role_arn": "arn:aws:iam::123456789012:role/connection",
                "mutation_role_arn": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
                **job_changes,
            },
            "action": action,
            "plan": selected or plan(),
            "observed": observed,
        }
    )


@pytest.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(ToolAdapterActivation.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_fence_survives_loss_and_reopen_requires_drain_and_complete_readback(database):
    async with database() as session:
        saved = await release.transition(session, request("fence"), now=100)
    assert saved["status"] == "fenced"
    assert saved["transition"]["drain_until"] == 120
    async with database() as session:
        assert await release.transition(session, request("fence"), now=105) == saved
        with pytest.raises(HTTPException):
            await release.transition(session, request("activate", observed=plan()["after"]), now=119, configured_release=saved["release"])
        with pytest.raises(HTTPException):
            await release.transition(session, request("activate", observed=plan()["after"]), now=121, configured_release=None)
        actual = await release.transition(session, request("activate", observed=plan()["after"]), now=121, configured_release=saved["release"])
        assert actual["status"] == "active"
        assert actual["transition"]["verified_routes"] == plan()["after"]["routes"]


async def test_failed_readback_stays_fenced_and_restores_original_manifest(database):
    async with database() as session:
        await release.transition(session, request("fence"), now=100)
        wrong = deepcopy(plan()["after"])
        wrong["routes"]["jira"]["digest"] = "sha256:" + "f" * 64
        with pytest.raises(HTTPException):
            await release.transition(session, request("activate", observed=wrong), now=122, configured_release=wrong["release"])
        assert (await session.scalar(select(ToolAdapterActivation))).status == "fenced"
        saved = await release.transition(session, request("rollback"), now=123)
    assert saved["release"]["backend_generation"] == 2
    async with database() as session:
        expected = release.rollback_snapshot(request("restore").plan)
        restored = await release.transition(session, request("restore", observed=expected), now=144, configured_release=expected["release"])
        assert restored["status"] == "legacy"
        assert restored["transition"]["verified_routes"]["atlassian"]["backend"] == "disabled"
        assert await release.transition(session, request("rollback"), now=145) == restored
        with pytest.raises(HTTPException):
            await release.transition(session, request("activate", observed=plan()["after"]), now=146, configured_release=plan()["after"]["release"])


async def test_stale_controller_cannot_reopen_or_refence_new_generation(database):
    async with database() as session:
        await release.transition(session, request("fence"), now=100)
        with pytest.raises(HTTPException):
            await release.transition(session, request("rollback", correlation="orch:00000000-0000-0000-0000-000000000002"), now=130)
        await release.transition(session, request("activate", observed=plan()["after"]), now=130, configured_release=plan()["after"]["release"])
        next_plan = {**plan(), "before": plan()["after"], "after": deepcopy(plan()["after"])}
        next_plan["after"]["release"].update(group_generation=2, backend_generation=2)
        await release.transition(session, request("fence", selected=next_plan), now=140)
        for action in ("fence", "rollback", "activate"):
            with pytest.raises(HTTPException):
                await release.transition(
                    session,
                    request(action, observed=plan()["after"] if action == "activate" else None),
                    now=170,
                    configured_release=plan()["after"]["release"],
                )


async def test_new_authorized_job_recovers_completed_activation_for_rollback(database):
    successor = "orch:00000000-0000-0000-0000-000000000002"
    async with database() as session:
        original = await release.transition(session, request("fence"), now=100)
        await release.transition(session, request("activate", observed=plan()["after"]), now=121, configured_release=plan()["after"]["release"])
        rollback = await release.transition(session, request("rollback", correlation=successor), now=130)
        assert rollback["status"] == "fenced"
        assert rollback["transition"]["owner"]["correlation"] == successor
        assert rollback["transition"]["recovered_from_owner"] == original["transition"]["owner"]
        with pytest.raises(HTTPException):
            await release.transition(session, request("restore", observed=release.rollback_snapshot(request("restore").plan)), now=151)
        expected = release.rollback_snapshot(request("restore").plan)
        restored = await release.transition(
            session, request("restore", correlation=successor, observed=expected), now=151, configured_release=expected["release"]
        )
        assert restored["status"] == "legacy"
        assert restored["release"]["group_generation"] == 2


@pytest.mark.parametrize("change", ["role", "mutation_role", "routes", "deployment"])
async def test_rollback_recovery_requires_same_authority_and_verified_live_baseline(database, change):
    async with database() as session:
        await release.transition(session, request("fence"), now=100)
        await release.transition(session, request("activate", observed=plan()["after"]), now=121, configured_release=plan()["after"]["release"])
        job = {"correlation": "orch:00000000-0000-0000-0000-000000000002"}
        row = await session.scalar(select(ToolAdapterActivation))
        if change == "role":
            job["role_arn"] = "arn:aws:iam::123456789012:role/different"
        elif change == "mutation_role":
            job["mutation_role_arn"] = "arn:aws:iam::123456789012:role/adp-other-trusted-deployment"
        elif change == "routes":
            row.transition = {**row.transition, "verified_routes": {}}
        else:
            row.transition = {**row.transition, "verified_deployment_id": "different"}
        await session.flush()
        with pytest.raises(HTTPException):
            await release.transition(session, request("rollback", **job), now=130)


@pytest.mark.parametrize("mutation", ["missing_route", "mixed_backend", "aliased_target", "stale_generation", "jira_fallback"])
def test_invalid_plans_rejected(mutation):
    value = plan()
    if mutation == "missing_route":
        del value["after"]["routes"]["slack"]
    elif mutation == "mixed_backend":
        value["after"]["routes"]["jira"]["backend"] = "legacy"
    elif mutation == "aliased_target":
        value["after"]["routes"]["jira"]["target_id"] = "github-target"
    elif mutation == "stale_generation":
        value["after"]["release"]["group_generation"] = 2
    else:
        value["before"]["routes"]["atlassian"]["backend"] = "legacy"
    with pytest.raises(ValueError):
        request("fence", selected=value)


async def test_endpoint_uses_native_job_authority_before_any_state_change(database, monkeypatch):
    app = FastAPI()
    app.include_router(release.router)
    guard = AsyncMock()
    guard.authorize.side_effect = HTTPException(403, "revoked")
    app.dependency_overrides[release.service] = lambda: guard

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[release.get_db] = db
    claims = AsyncMock(return_value={"run_id": "123"})
    monkeypatch.setattr(release, "github_claims", claims)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.example.test") as client:
        denied = await client.post("/tools/adapter-release/transition", json=request("fence").model_dump())
        assert denied.status_code == 403
        claims.assert_not_called()
        denied = await client.post(
            "/tools/adapter-release/transition", headers={"Authorization": "Bearer test.jwt.proof"}, json=request("fence").model_dump()
        )
        assert denied.status_code == 403
        claims.assert_awaited_once_with("test.jwt.proof", audience=release.AUDIENCE)
        guard.authorize.assert_awaited_once()
    async with database() as session:
        assert await session.scalar(select(ToolAdapterActivation)) is None


@pytest.mark.parametrize("workflow", ["tools-live-evaluation.yml", "gateway-deploy.yml"])
async def test_other_workflows_cannot_mutate_fence(database, workflow):
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await release.transition(session, request("fence", workflow=workflow))
        assert error.value.status_code == 403

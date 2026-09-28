"""Private stage controls over real HTTP authentication and PostgreSQL state."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import settings
from app.database import get_session
from app.main import app
from app.models.organization_grant import OrganizationGrantRecord
from app.models.provider_connection import ProviderConnectionBinding
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.operation_activation import STAGED_ADMISSION_PROBE
from app.routers import provider_connections
from app.schemas.workspace import CreateWorkspaceRequest
from tests.test_auth import _mint, enforcing as enforcing, rsa_keys as rsa_keys
from tests.test_provider_connections_postgres import (
    connection_db as connection_db,
    installation_postgres_url as installation_postgres_url,
    postgres_available,
    register,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def stage_api(connection_db, enforcing, monkeypatch):  # noqa: F811
    ctx = connection_db
    monkeypatch.setenv("SUPERPLANE_MANAGEMENT_ONLY", "true")
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    async with ctx.sessions() as session:
        connection, _ = await register(ctx, session)
        connection_id = connection.id
        session.add(
            OrganizationGrantRecord(
                org_id=ctx.org,
                principal="pg-owner",
                principal_type="human",
                permissions="organization:administer",
                granted_by="test-owner",
            )
        )
        await session.commit()

    async def sessions():
        async with ctx.sessions() as session:
            yield session

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = sessions
    token = _mint(enforcing, sub="pg-owner", **{"custom:org_id": str(ctx.org)})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stage-api",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            yield ctx, client, connection_id
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous


@pytest.mark.parametrize("race", [None, "revoke", "reduce", "rebind"])
async def test_metadata_requires_current_grant_and_original_binding_after_vault(
    stage_api, monkeypatch, race
):
    ctx, client, connection_id = stage_api
    entered, resume = asyncio.Event(), asyncio.Event()

    class Vault:
        async def read(self, **request):
            assert request["org_id"] == str(ctx.org)
            assert request["workspace_id"] == str(ctx.workspace)
            assert request["reference"] == ctx.reference
            assert request["principal"] == "pg-owner"
            entered.set()
            await asyncio.wait_for(resume.wait(), 10)
            return ctx.evidence

    monkeypatch.setattr(
        provider_connections, "get_credential_evidence_reader", lambda: Vault()
    )
    path = f"/internal/installation/workspaces/{ctx.workspace}/credential-evidence/{connection_id}"
    task = asyncio.create_task(client.get(path))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if race:
            async with ctx.sessions() as writer:
                if race in {"revoke", "reduce"}:
                    values = (
                        {"revoked_at": datetime.now(UTC)}
                        if race == "revoke"
                        else {"permissions": "workspace:read"}
                    )
                    await writer.execute(update(WorkspaceGrantRecord).values(**values))
                else:
                    sibling = uuid.uuid4()
                    writer.add(
                        Workspace(
                            id=sibling,
                            org_id=ctx.org,
                            name="sibling",
                            isolation_mode="dedicated",
                        )
                    )
                    await writer.flush()
                    await writer.execute(
                        update(ProviderConnectionBinding)
                        .where(ProviderConnectionBinding.connection_id == connection_id)
                        .values(workspace_id=sibling)
                    )
                await writer.commit()
    finally:
        resume.set()
    response = await asyncio.wait_for(task, 10)
    if race:
        assert response.status_code == 403, response.text
        assert "credential_id" not in response.json()
    else:
        assert response.status_code == 200, response.text
        assert response.json()["credential_id"] == ctx.reference.credential_id
        assert response.json()["workspace_id"] == str(ctx.workspace)
        assert response.json()["raw_material_returned"] is False


async def test_exact_installer_probe_passes_schema_and_hits_staged_guard(stage_api):
    ctx, client, _ = stage_api
    CreateWorkspaceRequest.model_validate(STAGED_ADMISSION_PROBE)
    async with ctx.sessions() as session:
        before = await session.scalar(select(func.count()).select_from(Workspace))
    response = await client.post("/workspaces", json=STAGED_ADMISSION_PROBE)
    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": "operation admission is disabled for adapter verification"
    }
    async with ctx.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Workspace)) == before
        )


def installer_program(name):
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    try:
        from installation import adapter_staging

        return getattr(adapter_staging, name)
    finally:
        sys.path.remove(str(root))


def test_actual_private_control_program_imports_and_sends_the_reviewed_probe(
    monkeypatch, capsys
):
    import io
    import json
    import sys

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/workspaces" and request.method == "POST":
            assert json.loads(request.content) == STAGED_ADMISSION_PROBE
            CreateWorkspaceRequest.model_validate(json.loads(request.content))
            return httpx.Response(
                503,
                json={
                    "detail": "operation admission is disabled for adapter verification"
                },
            )
        return httpx.Response(200, json={"control_version": 1})

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "path": "/internal/installation/workspaces/workspace/credential-evidence/connection",
                    "token": "test-transport",
                }
            )
        ),
    )
    exec(installer_program("PRIVATE_CONTROL_PROGRAM"), {"__name__": "__main__"})
    assert json.loads(capsys.readouterr().out)["unapproved_admission_refused"] is True
    assert [r.method for r in requests] == ["GET", "GET", "POST"]


from tests.test_onboarding_postgres import database as prepared_database  # noqa: E402,F401
from tests.test_operation_dispatch_postgres import (  # noqa: E402
    dispatch as dispatch,
    settlement as settlement,
    ledger as ledger,
)


async def test_quiescence_sql_on_actual_migrated_domain_schema(prepared_database):  # noqa: F811
    from sqlalchemy import text

    scope = {"__name__": "quiescence_test"}
    exec(installer_program("QUIESCENCE_PROGRAM"), scope)
    check = scope["check"]
    assert all(v == 0 for v in (await check(prepared_database.engine)).values())
    org, workspace, cluster, deployment = (uuid.uuid4() for _ in range(4))
    async with prepared_database.engine.begin() as c:
        await c.execute(
            text("INSERT INTO organizations(id,name) VALUES(:id,'quiescence')"),
            {"id": org},
        )
        await c.execute(
            text(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status) VALUES(:id,:org,'quiescence','dedicated','Provisioning')"
            ),
            {"id": workspace, "org": org},
        )
        await c.execute(
            text("INSERT INTO clusters(id,org_id,name) VALUES(:id,:org,'quiescence')"),
            {"id": cluster, "org": org},
        )
        await c.execute(
            text(
                "INSERT INTO deployments(id,org_id,workspace_id,cluster_id,name,status) VALUES(:id,:org,:workspace,:cluster,'quiescence','Pending')"
            ),
            {"id": deployment, "org": org, "workspace": workspace, "cluster": cluster},
        )
    counts = await check(prepared_database.engine)
    assert counts["workspaces"] == counts["deployments"] == 1
    async with prepared_database.engine.begin() as c:
        await c.execute(text("UPDATE workspaces SET status='Active'"))
        await c.execute(text("UPDATE deployments SET status='Deleted'"))
    assert all(v == 0 for v in (await check(prepared_database.engine)).values())


async def test_quiescence_sql_on_actual_admission_outbox_and_lease(
    dispatch,  # noqa: F811
    installation_postgres_url,  # noqa: F811
):
    from sqlalchemy.ext.asyncio import create_async_engine

    _, _, connections, identity, register_workspace = dispatch
    async with connections.connect() as connection:
        schema = await connection.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    scope = {"__name__": "quiescence_test"}
    exec(installer_program("QUIESCENCE_PROGRAM"), scope)
    try:
        counts = await scope["check"](engine)
        assert counts["harness_operations"] == counts["harness_dispatch_outbox"] == 1
        await register_workspace()
        assert (await scope["check"](engine))["workspaces"] == 1
        async with connections.connect() as connection:
            await connection.execute(
                "INSERT INTO harness_operation_leases(operation_id,org_id,workspace_id,fence_token) VALUES($1,$2,$3,1)",
                identity["operation_id"],
                identity["org_id"],
                identity["workspace_id"],
            )
        assert (await scope["check"](engine))["harness_operation_leases"] == 1
    finally:
        await engine.dispose()

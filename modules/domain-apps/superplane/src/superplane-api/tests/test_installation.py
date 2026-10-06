"""Installation fails closed on absent production capabilities and unsafe schemas."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import installation, skypilot_proxy
from app.schema_boundary import schema_connect_args, schema_name


@pytest.mark.parametrize(
    "value",
    [
        "public",
        "pg_catalog",
        "pg_temp",
        "information_schema",
        "domain,public",
        "domain;DROP TABLE users",
        'x"y',
    ],
)
def test_schema_cannot_escape_domain(value):
    with pytest.raises(ValueError):
        schema_name(value)


def test_api_and_migration_use_actual_asyncpg_setting():
    # Search-path selection only. Transport is a separate decision now
    # (issue #5676, A22) and has its own coverage in
    # tests/test_database_transport_tls.py; asserting on the combined dict here
    # is what previously let a schema change move the encryption posture.
    assert schema_connect_args("superplane") == {
        "server_settings": {"search_path": "superplane"}
    }
    assert schema_connect_args("") == {}


def test_absent_adapters_cannot_be_declared_available(monkeypatch, capsys):
    monkeypatch.setenv("CREDENTIAL_EVIDENCE_AVAILABLE", "true")
    monkeypatch.setenv("B_OPERATION_AUTHORITY_AVAILABLE", "true")
    assert installation.main(["capabilities"]) == 2
    assert not all(json.loads(capsys.readouterr().out)["capabilities"].values())


def test_database_error_never_leaks_connection_string(monkeypatch, capsys):
    async def fail(**kwargs):
        raise RuntimeError("postgresql://owner:private-password@database")

    monkeypatch.setattr(installation, "database_check", fail)
    assert installation.main(["database"]) == 2
    assert "private-password" not in capsys.readouterr().out


def test_skypilot_proxy_requires_credential_and_strips_it(monkeypatch):
    monkeypatch.setenv("SKYPILOT_SERVICE_TOKEN", "x" * 32)
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "healthy"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        skypilot_proxy.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(backend), **kw),
    )
    with TestClient(skypilot_proxy.app) as client:
        assert client.get("/api/health").status_code == 401
        assert calls == []
        result = client.get(
            "/api/health", headers={"Authorization": "Bearer " + "x" * 32}
        )
        assert result.status_code == 200
        assert str(calls[0].url) == "http://127.0.0.1:46580/api/health"
        assert "authorization" not in calls[0].headers


def test_image_contract_is_offline_without_database_or_shared_credentials():
    import os
    import subprocess
    import sys

    environment = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DATABASE_URL",
            "ADP_GATEWAY_INTERNAL_URL",
            "ADP_GATEWAY_INTERNAL_API_KEY",
            "SUPERPLANE_OPERATION_GATEWAY_URL",
            "SUPERPLANE_OPERATION_GATEWAY_REGION",
        }
        and not k.startswith("AWS_")
    }
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    result = subprocess.run(
        [sys.executable, "-m", "app.installation", "image-contract"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["image_contract_version"] == 1
    assert report["configuration_verified"] is False
    assert report["authority_verified"] is False
    assert report["production_ready"] is False
    assert "capabilities" not in report


async def test_runtime_dependencies_read_empty_authority_and_lifecycle_tables(
    monkeypatch,
):
    from types import SimpleNamespace

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import database
    from app.adapters.harness_operation_facade import HarnessOperationFacade
    from app.services import provisioning

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(database, "engine", engine)
    facade = object.__new__(HarnessOperationFacade)
    monkeypatch.setattr(provisioning, "get_operation_facade", lambda: facade)

    class Connections:
        opened = True

        async def ensure_ready(self):
            return None

    composition = SimpleNamespace(
        _connections=Connections(), _installed={"operation_facade": facade}
    )
    try:
        async with engine.begin() as connection:
            for table, column in (
                ("organization_grants", "org_id, principal, permissions, revoked_at"),
                (
                    "workspace_grants",
                    "org_id, workspace_id, principal, permissions, revoked_at",
                ),
                (
                    "operation_approvals",
                    "org_id, workspace_id, requester, plan_digest, expires_at, revoked",
                ),
                (
                    "workspace_lifecycle_control_operations",
                    "operation_id, org_id, workspace_id, phase, plan_digest",
                ),
            ):
                columns = ", ".join(
                    f"{name.strip()} text" for name in column.split(",")
                )
                await connection.execute(text(f"CREATE TABLE {table} ({columns})"))
        assert await installation.runtime_dependencies(composition) == {
            "operation_store": True,
            "authority": True,
            "lifecycle_registry": True,
        }
        composition._installed["operation_facade"] = object()
        assert not any((await installation.runtime_dependencies(composition)).values())
        composition._installed["operation_facade"] = facade
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP TABLE workspace_lifecycle_control_operations")
            )
        assert await installation.runtime_dependencies(composition) == {
            "operation_store": True,
            "authority": True,
            "lifecycle_registry": False,
        }
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE organization_grants"))
        assert await installation.runtime_dependencies(composition) == {
            "operation_store": True,
            "authority": False,
            "lifecycle_registry": False,
        }

        async def wrong_schema():
            raise RuntimeError("incompatible harness schema")

        composition._connections.ensure_ready = wrong_schema
        assert not any((await installation.runtime_dependencies(composition)).values())
        composition._connections.opened = False
        assert not any((await installation.runtime_dependencies(composition)).values())
    finally:
        await engine.dispose()


async def test_runtime_dependencies_do_not_trust_an_uninstalled_facade(monkeypatch):
    from types import SimpleNamespace

    from app.services import provisioning

    monkeypatch.setattr(provisioning, "get_operation_facade", lambda: object())
    composition = SimpleNamespace(_connections=SimpleNamespace(opened=True))
    assert not any((await installation.runtime_dependencies(composition)).values())
    assert not any((await installation.runtime_dependencies(None)).values())


@pytest.mark.parametrize(
    "missing", ("operation_store", "authority", "lifecycle_registry")
)
async def test_installation_readiness_masks_offline_capabilities_without_live_dependencies(
    monkeypatch,
    missing,
):
    from types import SimpleNamespace

    from app.routers import installation as router

    async def dependencies(_composition):
        return {
            key: key != missing
            for key in ("operation_store", "authority", "lifecycle_registry")
        }

    async def capabilities():
        return dict.fromkeys(
            (
                "credential_evidence",
                "operation_facade",
                "provider_authority",
                "allocation_inventory",
            ),
            True,
        )

    monkeypatch.setattr(router, "runtime_dependencies", dependencies)
    monkeypatch.setattr(router, "capabilities_async", capabilities)
    app = SimpleNamespace(
        state=SimpleNamespace(trust_composition=None, domain_policy=object())
    )
    response = await router.installation_readiness(
        SimpleNamespace(app=app), SimpleNamespace(workspaces=[])
    )
    assert response["capabilities"] == {
        "credential_evidence": True,
        "operation_facade": False,
        "provider_authority": missing != "operation_store",
        "allocation_inventory": missing != "operation_store",
    }
    assert response["dependencies"][missing] is False
    assert response["observations"] == {}

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

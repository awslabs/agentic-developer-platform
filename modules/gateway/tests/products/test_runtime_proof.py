"""Native runtime proof authenticates the edge and reads only local schema state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from src.auth import caller_provenance
from src.products import runtime_proof_routes as proof

ROLE = "arn:aws:iam::000000000000:role/runtime-reader"
CALLER = "arn:aws:sts::000000000000:assumed-role/runtime-reader/session"
PAYLOAD = {"pod_uid": "pod-uid", "namespace": "adp-gateway", "nonce": "a" * 64}


def test_native_reader_sanitizes_unavailable_transport():
    from src.orchestration.deployment_runtime_reader import KubernetesRuntime
    from src.orchestration.review_cycle import CycleBlockedError

    kube = object.__new__(KubernetesRuntime)
    kube.protected_schema = Mock(side_effect=httpx.ConnectError("private transport details"))
    with pytest.raises(CycleBlockedError, match="^deployment_runtime_proof_unavailable$"):
        kube.schema({"metadata": {"name": "pod-one", "uid": "pod-uid"}}, product=True)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ADP_RUNTIME_PROOF_ROLE_ARNS", ROLE)
    monkeypatch.setenv("ADP_POD_UID", PAYLOAD["pod_uid"])
    monkeypatch.setenv("ADP_POD_NAMESPACE", PAYLOAD["namespace"])
    monkeypatch.setattr(
        caller_provenance, "get_settings", lambda: SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="test-edge-proof")
    )
    producer = AsyncMock(return_value={"database_heads": ["head"], "image_heads": ["head"]})
    monkeypatch.setattr(proof, "process_proof", producer)
    app = FastAPI()
    app.include_router(proof.router)
    return app, producer


@pytest.mark.parametrize(
    "failure",
    [None, "forged", "missing", "shared_key", "wrong_account", "wrong_role", "no_roles", "wrong_pod", "wrong_namespace", "no_uid", "database_error"],
)
async def test_runtime_probe_binds_verified_reader_and_process(configured, monkeypatch, failure):
    app, producer = configured
    headers = {"X-Caller-Identity": CALLER, "X-Adp-Edge-Provenance": "test-edge-proof"}
    payload = dict(PAYLOAD)
    if failure == "forged":
        headers["X-Adp-Edge-Provenance"] = "untrusted"
    elif failure == "missing":
        headers = {}
    elif failure == "shared_key":
        monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-shared-key")
        headers = {"X-Internal-Api-Key": "test-shared-key"}
    elif failure == "wrong_account":
        headers["X-Caller-Identity"] = CALLER.replace("000000000000", "000000000001")
    elif failure == "wrong_role":
        headers["X-Caller-Identity"] = CALLER.replace("runtime-reader", "other")
    elif failure == "no_roles":
        monkeypatch.delenv("ADP_RUNTIME_PROOF_ROLE_ARNS")
    elif failure == "wrong_pod":
        payload["pod_uid"] = "another-pod"
    elif failure == "wrong_namespace":
        payload["namespace"] = "another-namespace"
    elif failure == "no_uid":
        monkeypatch.delenv("ADP_POD_UID")
    elif failure == "database_error":
        producer.side_effect = RuntimeError("private database details")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/internal/v1/runtime-proof/schema", json=payload, headers=headers)
    if failure is None:
        assert result.status_code == 200
        assert all(result.json()[key] == value for key, value in PAYLOAD.items())
        producer.assert_awaited_once()
    elif failure == "database_error":
        assert result.status_code == 503 and "private" not in result.text
    else:
        assert result.status_code == (409 if failure == "wrong_pod" else 503 if failure == "no_uid" else 403)
        producer.assert_not_awaited()


async def test_process_probe_uses_bounded_read_only_postgres_transaction(monkeypatch, tmp_path):
    connection = AsyncMock()
    rows = Mock()
    rows.scalars.return_value.all.return_value = ["head"]
    connection.execute.return_value = rows
    context = AsyncMock()
    context.__aenter__.return_value = connection
    monkeypatch.setattr(proof, "get_engine", lambda: SimpleNamespace(connect=lambda: context))
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"reviewed CLI bytes")
    monkeypatch.setattr(proof, "Path", lambda _: manifest)
    monkeypatch.setattr(proof.ScriptDirectory, "from_config", lambda _: SimpleNamespace(get_heads=lambda: ["head"]))
    monkeypatch.setattr(proof, "release_identity", lambda: {"l5_wheel_sha256": "a" * 64})
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    monkeypatch.setenv("BG_PRODUCT_ONBOARDING_ENABLED", "false")
    result = await proof.process_proof()
    assert result["database_heads"] == result["image_heads"] == ["head"]
    assert result["l5_wheel_sha256"] == "a" * 64
    assert result["cli_manifest_sha256"] == proof.hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert result["product_profile_enabled"] == "true" and result["product_onboarding_enabled"] == "false"
    assert [str(call.args[0]) for call in connection.execute.await_args_list] == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '8s'",
        "SET LOCAL lock_timeout = '3s'",
        "SELECT version_num FROM alembic_version",
    ]

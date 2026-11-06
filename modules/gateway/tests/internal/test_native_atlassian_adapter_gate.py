"""Adapter authorization and token dispatch both consult original admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agentauth.native_test_atlassian_dispatch import NativeAtlassianDispatch
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from tests.internal.test_tool_adapter_authority import authority, request_body  # noqa: F401


def native_request():
    return request_body(
        claims_update={
            "run_id": "original-run",
            "workload_id": "original-workload",
            "run_expires_at": 4102444800,
            "native_atlassian": {"original": "offline-boundary"},
        }
    )


async def test_adapter_rechecks_and_consumes_native_authority_before_token_use(authority, monkeypatch):  # noqa: F811
    client, _, _, _, _ = authority
    claim = Mock()
    verify = AsyncMock(return_value=SimpleNamespace(claim=claim))
    monkeypatch.setattr(NativeAtlassianDispatch, "verify", verify)
    response = client.post("/internal/v1/tool-adapters/authorize", json=native_request())
    assert response.status_code == 200, response.text
    authorized = response.json()
    assert verify.await_count == 1
    claim.assert_not_called()
    body = {"schema_version": "1.0", "admission_id": authorized["admission_id"], "binding": authorized["binding"], "binding_revision": "revision-1"}
    sent = client.post("/internal/v1/tool-adapters/dispatch", json=body)
    assert sent.status_code == 200, sent.text
    assert verify.await_count == 2
    assert verify.await_args.kwargs["claims"]["native_atlassian"] == {"original": "offline-boundary"}
    claim.assert_called_once_with(authorized["admission_id"])
    assert client.post("/internal/v1/tool-adapters/dispatch", json=body).status_code == 403
    claim.assert_called_once()


@pytest.mark.parametrize("stage", ["authorize", "dispatch"])
async def test_missing_or_revoked_native_authority_never_dispatches(authority, monkeypatch, stage):  # noqa: F811
    client, _, _, _, _ = authority
    claim = Mock()
    verify = AsyncMock(return_value=SimpleNamespace(claim=claim))
    if stage == "authorize":
        verify.side_effect = AdmissionRefusedError("offline revoked")
    monkeypatch.setattr(NativeAtlassianDispatch, "verify", verify)
    response = client.post("/internal/v1/tool-adapters/authorize", json=native_request())
    if stage == "dispatch":
        assert response.status_code == 200, response.text
        authorized = response.json()
        verify.side_effect = AdmissionRefusedError("offline revoked")
        response = client.post(
            "/internal/v1/tool-adapters/dispatch",
            json={
                "schema_version": "1.0",
                "admission_id": authorized["admission_id"],
                "binding": authorized["binding"],
                "binding_revision": "revision-1",
            },
        )
    assert response.status_code == 403, response.text
    claim.assert_not_called()

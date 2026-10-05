"""Pinned REST behavior, token rotation, and rejection without ambient credentials."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from botocore.credentials import Credentials
from harness_jobs.identity import (
    OperationRefused,
    OperationRequest,
    encode_payload,
    payload_digest,
)

from superplane_executor.authority import GatewayAuthority
from superplane_executor.skypilot import SkyPilot


async def test_skypilot_uses_header_handle_and_never_retries_mutation(tmp_path):
    token = tmp_path / "token"
    token.write_text("a" * 32)
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path == "/launch":
            assert isinstance(json.loads(request.content)["task"], str)
            assert request.headers["Authorization"] == "Bearer " + "a" * 32
            return httpx.Response(
                200, json=None, headers={"X-Skypilot-Request-ID": "request-123456"}
            )
        assert request.url.path == "/api/status"
        assert request.headers["Authorization"] == "Bearer " + "b" * 32
        return httpx.Response(
            200,
            json=[
                {
                    "request_id": "request-123456",
                    "status": "SUCCEEDED",
                    "return_value": "never decode pickle",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sky = SkyPilot("https://sky.example", token, client)
        handle = await sky.submit("/launch", {"task": "{}", "cluster_name": "scoped"})
        token.write_text("b" * 32)
        assert await sky.complete(handle, AsyncMock())
        assert len(requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(307, headers={"Location": "https://other.example"}),
        httpx.Response(503),
        httpx.Response(200, json={"request_id": "legacy-body-is-not-supported"}),
    ],
)
async def test_uncertain_launch_is_not_retried(tmp_path, response):
    token = tmp_path / "token"
    token.write_text("a" * 32)
    calls = []

    def transport(request):
        calls.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sky = SkyPilot("https://sky.example", token, client)
        with pytest.raises(OperationRefused):
            await sky.submit("/launch", {"task": "{}"})
        assert len(calls) == 1


async def test_gateway_authority_uses_sigv4_projected_run_and_verified_binding(
    tmp_path,
):
    run, workload = tmp_path / "run", tmp_path / "workload"
    run.write_text("signed-run-token")
    workload.write_text("bound-pod-token")
    session = Mock()
    session.get_credentials.return_value = Credentials("test-access", "test-secret")
    request = OperationRequest(action="provision", idempotency_key="approved")
    now = datetime.now(UTC)
    data = dict(
        version=1,
        reservation_state="confirmed",
        max_resource_units=2,
        max_runtime_seconds=300,
        max_cost_micros=1000000,
        operation_id="operation",
        org_id="adp-org",
        workspace_id="workspace",
        job_id="admitted-job",
        holder="invocation#1",
        attempt_id="attempt",
        fence_token=2,
        attempts=1,
        max_attempts=5,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        runtime_deadline=(now + timedelta(seconds=300)).isoformat(),
        request_payload=encode_payload(request),
        plan_digest=payload_digest(request),
    )

    def transport(req):
        assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert req.headers["X-Adp-Run-Credential"] == "signed-run-token"
        assert req.headers["X-Adp-Workload-Token"] == "bound-pod-token"
        assert json.loads(req.content) == {"operation_id": "operation"}
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        authority = GatewayAuthority(
            endpoint="https://gateway.example",
            region="us-east-1",
            run_credential_file=run,
            workload_token_file=workload,
            session=session,
            client=client,
        )
        verified = await authority.resolve("operation")
        assert verified.grant.principal.subject == "invocation#1"
        assert verified.job_id == "admitted-job"
        data["plan_digest"] = "f" * 64
        with pytest.raises(OperationRefused):
            await authority.resolve("operation")


@pytest.mark.parametrize("foreign", [False, True])
async def test_only_the_admitted_aws_role_can_be_delivered(tmp_path, foreign):
    authority = GatewayAuthority(
        endpoint="https://gateway.example",
        region="us-east-1",
        run_credential_file=tmp_path / "run",
        workload_token_file=tmp_path / "workload",
    )
    authority.credential_request = Mock(
        return_value={
            "credential_id": "approved",
            "provider_account_id": "123456789012",
        }
    )
    role = (
        "arn:aws:iam::"
        + ("999999999999" if foreign else "123456789012")
        + ":role/provider"
    )
    authority.post = AsyncMock(
        return_value={
            "credential_id": "approved",
            "credential_type": "aws_role",
            "value": json.dumps({"role_arn": role}),
        }
    )
    try:
        if foreign:
            with pytest.raises(OperationRefused):
                await authority.delivery_role(object())
        else:
            assert await authority.delivery_role(object()) == {"role_arn": role}
    finally:
        await authority.aclose()


async def test_unbound_transport_is_limited_to_actual_bootstrap_paths(tmp_path):
    run, workload = tmp_path / "unissued-run", tmp_path / "workload"
    workload.write_text("pod-token")
    session = Mock()
    session.get_credentials.return_value = Credentials("test-access", "test-secret")
    seen = []

    def transport(request):
        seen.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert request.headers["X-Adp-Workload-Token"] == "pod-token"
        assert "X-Adp-Run-Credential" not in request.headers
        return httpx.Response(200, json={"body": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        authority = GatewayAuthority(
            endpoint="https://gateway.example",
            region="us-east-1",
            run_credential_file=run,
            workload_token_file=workload,
            session=session,
            client=client,
        )
        assert await authority.post(
            "/internal/v1/controller-execution/task/acquire", {}, bootstrap=True
        ) == {"body": None}
        with pytest.raises(OperationRefused):
            await authority.post(
                "/internal/v1/controller-execution/lease", {}, bootstrap=True
            )
        assert len(seen) == 1 and not run.exists()

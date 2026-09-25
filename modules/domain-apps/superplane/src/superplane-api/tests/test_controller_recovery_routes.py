"""Recovery HTTP boundary denies unscoped callers and preserves exact receipts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.routers import controller_recovery as routes


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "observe",
            {
                "claim": {
                    "operation_id": "operation",
                    "org_id": "org",
                    "workspace_id": "workspace",
                    "holder": "recovery",
                    "attempt_id": "attempt",
                    "fence_token": 1,
                },
                "query_id": "query",
                "idempotency_key": "key",
            },
        ),
        (
            "lifecycle",
            {
                "claim": {
                    "operation_id": "operation",
                    "org_id": "org",
                    "workspace_id": "workspace",
                    "holder": "recovery",
                    "attempt_id": "attempt",
                    "fence_token": 1,
                },
                "query_id": "query",
                "idempotency_key": "key",
            },
        ),
        (
            "inventory",
            {
                "claim": {
                    "operation_id": "operation",
                    "org_id": "org",
                    "workspace_id": "workspace",
                    "holder": "recovery",
                    "attempt_id": "attempt",
                    "fence_token": 1,
                },
                "query_id": "query",
                "allocation_id": "allocation",
            },
        ),
    ],
)
async def test_unscoped_recovery_cannot_open_store_or_provider(path, body):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._authenticated_submitter] = lambda: SimpleNamespace(
        lease_scopes=frozenset({"controller_management/org"})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://domain.example"
    ) as client:
        response = await client.post("/internal/controller/recovery/" + path, json=body)
        assert response.status_code == 403
        invalid = await client.post(
            "/internal/controller/recovery/" + path, json={"provider_role": "injected"}
        )
        assert invalid.status_code == 422


async def test_exact_recovery_receipt_reaches_owning_ledger_without_relabelling():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._authenticated_submitter] = lambda: SimpleNamespace(
        lease_scopes=frozenset({"controller_recovery/domain-uuid"})
    )
    ledger = SimpleNamespace(deliver_settlement=AsyncMock(return_value="receipt"))
    app.state.trust_composition = SimpleNamespace(
        operation_connect=lambda: None, ledger=ledger
    )
    body = dict(
        receipt_id="receipt",
        payload_digest="a" * 64,
        operation_id="original-op",
        job_id="original-job",
        attempt_id="original-admission-attempt",
        org_id="domain-uuid",
        workspace_id="workspace",
        accounting={"budget": "retain"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://domain.example"
    ) as client:
        response = await client.post(
            "/internal/controller/recovery/settlement", json=body
        )
        assert response.status_code == 200
        assert response.json() == {"receipt_id": "receipt"}
    ledger.deliver_settlement.assert_awaited_once_with(**body)


async def test_recovery_provider_assumes_only_read_policy_and_refuses_kubernetes_mutation(
    monkeypatch,
):
    import json
    from fastapi import HTTPException
    from unittest.mock import Mock

    role = "arn:aws:iam::123456789012:role/recovery-read"
    for key, value in {
        "SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN": role,
        "SUPERPLANE_RECOVERY_WORKSPACE_CREDENTIALS_DIR": "/explicit/workspaces",
        "SUPERPLANE_MANAGEMENT_API_SERVER": "https://management.example",
        "SKYPILOT_URL": "https://skypilot.example",
        "SKYPILOT_SERVICE_TOKEN_FILE": "/explicit/skypilot-token",
    }.items():
        monkeypatch.setenv(key, value)
    sts = Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "synthetic-access",
            "SecretAccessKey": "synthetic-secret",
            "SessionToken": "synthetic-session",
        }
    }
    session = SimpleNamespace(client=lambda *args, **kwargs: sts)
    monkeypatch.setattr("boto3.Session", lambda **kwargs: session)
    current_claim = AsyncMock()
    monkeypatch.setattr(routes, "claim_context", current_claim)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                trust_composition=SimpleNamespace(operation_connect=lambda: None)
            )
        )
    )
    from superplane_executor.plan import Plan

    # V4 intentionally has no flat region: exercise the production ReadProvider.
    plan = Plan(
        data={
            "version": 4,
            "provider_account_id": "123456789012",
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
            "regions": [{"region": "us-west-2"}],
        },
        cluster_name="fixture",
        steps=(),
    )
    async with routes.observation_provider(request, "claim") as provider:
        await provider.session_for(None, plan)
        policy = json.loads(sts.assume_role.call_args.kwargs["Policy"])
        assert sts.assume_role.call_args.kwargs["RoleArn"] == role
        assert policy["Statement"] == [
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:Describe*",
                    "ec2:SearchTransitGatewayRoutes",
                    "ec2:GetTransitGatewayRouteTableAssociations",
                    "eks:DescribeCluster",
                    "sts:GetCallerIdentity",
                ],
                "Resource": "*",
            }
        ]
        assert current_claim.await_count == 2
        for method, path in (
            ("POST", "/api/v1/pods"),
            ("GET", "/api/v1/secrets"),
            ("GET", "/api/v1/pods/p/exec"),
        ):
            with pytest.raises(HTTPException) as refusal:
                await provider.workspace.request(None, None, method, path)
            assert refusal.value.status_code == 403

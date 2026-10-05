"""Probe the pinned pre-enforcement API in a separate, isolated Python process."""

import asyncio
import base64
import json
import socket
import sys
import uuid
from types import ModuleType
from unittest.mock import patch

import jwt
import yaml
from httpx import ASGITransport, AsyncClient

legacy_jose = ModuleType("jose")
legacy_jose.JWTError = jwt.PyJWTError
legacy_jose.jwt = jwt
sys.modules["jose"] = legacy_jose


async def probe():
    from app.main import app
    from app.middleware.auth import create_access_token
    from app.models.cluster import Cluster
    from app.models.organization import Organization
    from app.models.workspace import Workspace

    from tests.conftest import Base, async_session_test, engine_test

    org_id = uuid.uuid4()
    outsider_org_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    org_mate_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    async with engine_test.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_session_test() as session:
        session.add_all(
            [
                Organization(id=org_id, name="example-org", billing_plan="free"),
                Organization(
                    id=outsider_org_id, name="other-example-org", billing_plan="free"
                ),
            ]
        )
        await session.flush()
        session.add(
            Cluster(
                id=cluster_id,
                org_id=org_id,
                name="example-cluster",
                endpoint="https://cluster.example.invalid",
                eks_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/example-cluster",
            ),
        )
        await session.flush()
        session.add(
            Workspace(
                id=workspace_id,
                org_id=org_id,
                cluster_id=cluster_id,
                name="example-workspace",
                isolation_mode="dedicated",
                status="Active",
            ),
        )
        await session.commit()

    from app.services import eks_auth, proxy

    calls = []
    ca_data = base64.b64encode(
        b"-----BEGIN CERTIFICATE-----\nfictional\n-----END CERTIFICATE-----"
    ).decode()

    def assume_role(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "AccessKeyId": "fictional",
            "SecretAccessKey": "fictional",
            "SessionToken": "fictional",
        }

    def describe_cluster_ca(**kwargs):
        assert kwargs["credentials"]["AccessKeyId"] == "fictional"
        return ca_data

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with (
            patch.object(proxy, "assume_role_for_cluster", side_effect=assume_role),
            patch.object(
                eks_auth, "describe_cluster_ca", side_effect=describe_cluster_ca
            ),
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            results = []
            for user_id, tenant_id in (
                (owner_id, org_id),
                (org_mate_id, org_id),
                (org_mate_id, outsider_org_id),
            ):
                token, _ = create_access_token(tenant_id, user_id=user_id)
                response = await client.post(
                    f"/workspaces/{workspace_id}/kubeconfig",
                    headers={"Authorization": f"Bearer {token}"},
                )
                body = response.json()
                config = (
                    yaml.safe_load(body["kubeconfig"])
                    if response.status_code == 200
                    else None
                )
                results.append(
                    {
                        "status": response.status_code,
                        "cluster": config["clusters"][0]["cluster"]["server"]
                        if config
                        else None,
                        "exec": config["users"][0]["user"]["exec"]["command"]
                        if config
                        else None,
                    }
                )
    print(json.dumps({"results": results, "assume_calls": len(calls)}))
    await engine_test.dispose()


if __name__ == "__main__":
    asyncio.run(probe())

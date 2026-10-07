"""Current mounted HTTP kubeconfig behavior paired with the historical baseline."""

import base64
import socket
import uuid
from unittest.mock import patch

import pytest
import yaml
from app.models.cluster import Cluster
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import eks_auth, proxy

from tests.conftest import async_session_test
from tests.test_auth import _mint, _seed_workspace

pytest_plugins = ("tests.test_auth",)


@pytest.fixture
async def credential_ready_workspace():
    org_id, workspace_id = await _seed_workspace(
        "workspace:provision", principal="example-owner"
    )
    cluster_id = uuid.uuid4()
    async with async_session_test() as session:
        session.add(
            Cluster(
                id=cluster_id,
                org_id=org_id,
                name="example-cluster",
                endpoint="https://cluster.example.invalid",
                eks_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/example-cluster",
            )
        )
        session.add(
            WorkspaceGrantRecord(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                org_id=org_id,
                principal="example-reader",
                principal_type="human",
                permissions="workspace:read",
            )
        )
        await session.flush()
        workspace = await session.get(Workspace, workspace_id)
        workspace.cluster_id = cluster_id
        workspace.status = "Active"
        await session.commit()
    return org_id, workspace_id


@pytest.mark.asyncio
async def test_current_mounted_kubeconfig_enforces_grant_before_provider(
    client, enforcing, credential_ready_workspace
):
    org_id, workspace_id = credential_ready_workspace
    path = f"/workspaces/{workspace_id}/kubeconfig"
    ca_data = base64.b64encode(
        b"-----BEGIN CERTIFICATE-----\nfictional\n-----END CERTIFICATE-----"
    ).decode()

    def assume_role(*args, **kwargs):
        assert args == ("123456789012", "ws")
        assert kwargs["session_suffix"] == "kubeconfig"
        return {"AccessKeyId": "fictional", "SecretAccessKey": "fictional"}

    def describe_cluster_ca(**kwargs):
        assert kwargs["credentials"]["AccessKeyId"] == "fictional"
        return ca_data

    def authorization(principal, tenant=org_id):
        token = _mint(enforcing, sub=principal, **{"custom:org_id": str(tenant)})
        return {"Authorization": f"Bearer {token}"}

    with (
        patch.object(
            proxy, "assume_role_for_cluster", side_effect=assume_role
        ) as assumed,
        patch.object(
            eks_auth, "describe_cluster_ca", side_effect=describe_cluster_ca
        ) as described,
        patch.object(
            socket.socket, "connect", side_effect=AssertionError("network forbidden")
        ),
    ):
        for headers, expected_status in (
            (authorization("example-org-mate"), 403),
            (authorization("example-reader"), 403),
            (authorization("example-owner", uuid.uuid4()), 403),
            ({}, 401),
        ):
            response = await client.post(path, headers=headers)
            assert response.status_code == expected_status, response.text
            assert "kubeconfig" not in response.json()
            assumed.assert_not_called()
            described.assert_not_called()

        spoofed = authorization("example-reader")
        spoofed["X-ADP-Principal"] = "example-owner"
        response = await client.post(path, headers=spoofed)
        assert response.status_code == 403
        assumed.assert_not_called()

        response = await client.post(path, headers=authorization("example-owner"))
        assert response.status_code == 200, response.text
        kubeconfig = yaml.safe_load(response.json()["kubeconfig"])
        assert (
            kubeconfig["clusters"][0]["cluster"]["server"]
            == "https://cluster.example.invalid"
        )
        assert kubeconfig["users"][0]["user"]["exec"]["command"] == "aws"
        assert "fictional" not in response.json()["kubeconfig"]
        assumed.assert_called_once()
        described.assert_called_once()


def test_mounted_kubeconfig_route_matches_gateway_inventory():
    import json
    from pathlib import Path

    from app.domain_guard import enforce_domain_authorization
    from app.endpoint_inventory import RouteClass, Scope, classify, mounted_operations
    from app.main import app
    from app.schemas.workspace import KubeconfigResponse
    from superplane_auth.policy import Permission

    template = "/workspaces/{workspace_id}/kubeconfig"
    assert ("POST", template) in mounted_operations(app)
    assert ("GET", template) not in mounted_operations(app)
    route = next(
        route
        for route in app.routes
        if route.path == template and "POST" in route.methods
    )
    assert route.response_model is KubeconfigResponse
    assert any(
        dependency.call is enforce_domain_authorization
        for dependency in route.dependant.dependencies
    )
    assert classify("POST", template) == (
        RouteClass.DOMAIN,
        (Scope.WORKSPACE, Permission.PROVISION),
    )

    gateway_routes = Path(__file__).resolve().parents[6] / (
        "modules/gateway/src/domain_proxy/superplane_routes.json"
    )
    forwarded = {tuple(pair) for pair in json.loads(gateway_routes.read_text())}
    assert ("POST", template) in forwarded
    assert ("GET", template) not in forwarded


def test_browser_and_mcp_have_no_second_kubeconfig_exporter():
    from pathlib import Path

    root = Path(__file__).resolve().parents[6]
    browser = root / "modules/domain-apps/superplane/ui"
    mcp = root / "modules/domain-apps/superplane/tools/superplane-mcp/superplane_mcp"
    exported_paths = [
        path.relative_to(root)
        for source, suffixes in ((browser, {".ts", ".tsx"}), (mcp, {".py"}))
        for path in source.rglob("*")
        if path.suffix in suffixes and "kubeconfig" in path.read_text().lower()
    ]
    assert not exported_paths

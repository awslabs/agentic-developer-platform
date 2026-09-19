"""Tests for proxy, heartbeat, cost, and rate limiting."""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.middleware.auth import create_access_token


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


# ---- Schema tests ----


class TestProxySchemas:
    """Test Pydantic schema validation for proxy endpoints."""

    def test_create_deployment_request_valid(self):
        from app.schemas.proxy import CreateDeploymentRequest

        req = CreateDeploymentRequest(
            name="llama-8b",
            model_name="meta-llama/Llama-3.1-8B-Instruct",
            precision="fp16",
            serving_framework="vllm",
            replicas=1,
            gpu_per_replica=1,
        )
        assert req.name == "llama-8b"
        assert req.model_name == "meta-llama/Llama-3.1-8B-Instruct"
        assert req.precision == "fp16"

    def test_create_deployment_request_defaults(self):
        from app.schemas.proxy import CreateDeploymentRequest

        req = CreateDeploymentRequest(
            name="my-model",
            model_name="meta-llama/Llama-3.1-8B-Instruct",
        )
        assert req.precision == "fp16"
        assert req.serving_framework == "vllm"
        assert req.replicas == 1
        assert req.gpu_per_replica == 1
        assert req.tensor_parallel_size == 1
        assert req.namespace == "default"

    def test_create_deployment_request_invalid_precision(self):
        from pydantic import ValidationError

        from app.schemas.proxy import CreateDeploymentRequest

        with pytest.raises(ValidationError):
            CreateDeploymentRequest(
                name="my-model",
                model_name="test-model",
                precision="fp32-invalid",
            )

    def test_create_deployment_request_invalid_framework(self):
        from pydantic import ValidationError

        from app.schemas.proxy import CreateDeploymentRequest

        with pytest.raises(ValidationError):
            CreateDeploymentRequest(
                name="my-model",
                model_name="test-model",
                serving_framework="tgi",  # Only vllm and sglang
            )

    def test_create_deployment_request_invalid_name(self):
        from pydantic import ValidationError

        from app.schemas.proxy import CreateDeploymentRequest

        with pytest.raises(ValidationError):
            CreateDeploymentRequest(
                name="Invalid_Name",  # Must be lowercase alphanumeric with dashes
                model_name="test-model",
            )

    def test_create_deployment_replicas_range(self):
        from pydantic import ValidationError

        from app.schemas.proxy import CreateDeploymentRequest

        with pytest.raises(ValidationError):
            CreateDeploymentRequest(
                name="my-model",
                model_name="test-model",
                replicas=0,  # Must be >= 1
            )

        with pytest.raises(ValidationError):
            CreateDeploymentRequest(
                name="my-model",
                model_name="test-model",
                replicas=33,  # Must be <= 32
            )

    def test_heartbeat_request_valid(self):
        from app.schemas.proxy import HeartbeatRequest

        req = HeartbeatRequest(
            cluster_id=uuid.uuid4(),
            health_status="Healthy",
            actual_state_json={"nodes": 3, "gpus": 8},
            node_count=3,
            gpu_count=8,
        )
        assert req.health_status == "Healthy"
        assert req.actual_state_json == {"nodes": 3, "gpus": 8}

    def test_heartbeat_request_invalid_status(self):
        from pydantic import ValidationError

        from app.schemas.proxy import HeartbeatRequest

        with pytest.raises(ValidationError):
            HeartbeatRequest(
                cluster_id=uuid.uuid4(),
                health_status="NotAValidStatus",
            )

    def test_heartbeat_request_valid_statuses(self):
        from app.schemas.proxy import HeartbeatRequest

        for status in ("Healthy", "Degraded", "Unhealthy", "Unknown"):
            req = HeartbeatRequest(
                cluster_id=uuid.uuid4(),
                health_status=status,
            )
            assert req.health_status == status

    def test_cost_response_schema(self):
        from app.schemas.proxy import CostResponse

        resp = CostResponse(
            workspace_id=str(uuid.uuid4()),
            workspace_name="test-ws",
            total_cost_usd="123.45",
            currency="USD",
            node_count=2,
            breakdown_by_gpu={"A100": "100.00", "H100": "23.45"},
            breakdown_by_cloud={"aws": "123.45"},
        )
        assert resp.total_cost_usd == "123.45"
        assert resp.node_count == 2


# ---- Proxy service tests ----


class TestProxyService:
    """Test proxy service helper functions."""

    def test_create_deployment_manifest_vllm(self):
        from app.services.proxy import create_deployment_manifest

        manifest = create_deployment_manifest(
            name="llama-8b",
            model_name="meta-llama/Llama-3.1-8B-Instruct",
            precision="fp16",
            serving_framework="vllm",
            replicas=2,
            gpu_per_replica=1,
            tensor_parallel_size=1,
        )

        assert manifest["apiVersion"] == "apps/v1"
        assert manifest["kind"] == "Deployment"
        assert manifest["metadata"]["name"] == "llama-8b"
        assert manifest["spec"]["replicas"] == 2

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "vllm"
        assert container["image"] == "vllm/vllm-openai:latest"
        assert "--model" in container["args"]
        assert "meta-llama/Llama-3.1-8B-Instruct" in container["args"]
        assert "--dtype" in container["args"]
        assert "fp16" in container["args"]

        # Check GPU resources
        assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"

        # Check labels
        assert (
            manifest["metadata"]["labels"]["superplane.io/component"] == "model-serving"
        )
        assert manifest["metadata"]["labels"]["superplane.io/framework"] == "vllm"

    def test_create_deployment_manifest_sglang(self):
        from app.services.proxy import create_deployment_manifest

        manifest = create_deployment_manifest(
            name="llama-70b",
            model_name="meta-llama/Llama-3.1-70B-Instruct",
            precision="bf16",
            serving_framework="sglang",
            replicas=1,
            gpu_per_replica=4,
            tensor_parallel_size=4,
            max_model_len=8192,
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["name"] == "sglang"
        assert container["image"] == "lmsysorg/sglang:latest"
        assert "--model-path" in container["args"]
        assert "--tp" in container["args"]
        assert "--context-length" in container["args"]
        assert "8192" in container["args"]
        assert container["resources"]["limits"]["nvidia.com/gpu"] == "4"

    def test_create_deployment_manifest_with_max_model_len(self):
        from app.services.proxy import create_deployment_manifest

        manifest = create_deployment_manifest(
            name="my-model",
            model_name="test/model",
            max_model_len=4096,
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert "--max-model-len" in container["args"]
        assert "4096" in container["args"]

    def test_create_deployment_manifest_tolerations(self):
        from app.services.proxy import create_deployment_manifest

        manifest = create_deployment_manifest(
            name="my-model",
            model_name="test/model",
        )

        tolerations = manifest["spec"]["template"]["spec"]["tolerations"]
        assert len(tolerations) == 1
        assert tolerations[0]["key"] == "nvidia.com/gpu"

    def test_create_deployment_manifest_probes(self):
        from app.services.proxy import create_deployment_manifest

        manifest = create_deployment_manifest(
            name="my-model",
            model_name="test/model",
        )

        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert "readinessProbe" in container
        assert "livenessProbe" in container
        assert container["readinessProbe"]["httpGet"]["path"] == "/health"
        assert container["readinessProbe"]["httpGet"]["port"] == 8000


class TestProxyErrorHandling:
    """Test ProxyError exception class."""

    def test_proxy_error_defaults(self):
        from app.services.proxy import ProxyError

        err = ProxyError("something went wrong")
        assert err.message == "something went wrong"
        assert err.status_code == 502

    def test_proxy_error_custom_status(self):
        from app.services.proxy import ProxyError

        err = ProxyError("not found", status_code=404)
        assert err.status_code == 404


# ---- Cluster authentication and TLS (U16b, issue #5057) ----
#
# The brokered proxy path must authenticate with a signed, cluster-bound EKS token and
# always verify the cluster's CA. Token construction and the TLS trust decision itself
# are covered in test_kubeconfig.py; these cases cover how the proxy service resolves
# what to sign and what to trust, and that a failure is a refusal rather than a
# degraded connection.


_STS_CREDENTIALS = {
    "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "SessionToken": "FwoGZXIvYXdzEExampleSessionTokenValueOnly",
}


def _cluster(**overrides):
    """Build a Cluster row for identifier-resolution tests."""
    from app.models.cluster import Cluster

    fields = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "name": "display-name",
        "eks_cluster_arn": "arn:aws:eks:eu-west-1:123456789012:cluster/real-eks-name",
        "endpoint": "https://ABC.gr7.eu-west-1.eks.amazonaws.com",
        "status": "Active",
    }
    fields.update(overrides)
    return Cluster(**fields)


class TestClusterIdentifierResolution:
    """Region and cluster name must come from the ARN, which is authoritative.

    Signing against the wrong regional STS endpoint, or under the row's display name
    rather than the cluster's real EKS name, produces a token the cluster rejects.
    """

    def test_region_from_arn(self):
        from app.services.proxy import _get_region_from_cluster

        assert _get_region_from_cluster(_cluster()) == "eu-west-1"

    def test_region_falls_back_to_configured_region(self):
        from app.config import settings
        from app.services.proxy import _get_region_from_cluster

        assert (
            _get_region_from_cluster(_cluster(eks_cluster_arn=None))
            == settings.aws_region
        )

    def test_cluster_name_from_arn_not_display_name(self):
        from app.services.proxy import _get_cluster_name_from_arn

        assert _get_cluster_name_from_arn(_cluster()) == "real-eks-name"

    def test_cluster_name_falls_back_to_row_name(self):
        from app.services.proxy import _get_cluster_name_from_arn

        assert (
            _get_cluster_name_from_arn(_cluster(eks_cluster_arn=None)) == "display-name"
        )

    def test_account_from_arn(self):
        from app.services.proxy import _get_aws_account_from_cluster

        assert _get_aws_account_from_cluster(_cluster()) == "123456789012"


class TestBrokeredAssume:
    """The assume must carry the tenant's stored ExternalId (U16a, #5051)."""

    def test_external_id_is_sent_when_present(self):
        from app.services.proxy import assume_role_for_cluster

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = {"Credentials": _STS_CREDENTIALS}
        with patch("app.services.proxy.boto3.client", return_value=fake_sts):
            assume_role_for_cluster("123456789012", "ws-a", external_id="tenant-xyz")

        assert fake_sts.assume_role.call_args.kwargs["ExternalId"] == "tenant-xyz"

    def test_external_id_omitted_entirely_when_absent(self):
        """An empty ExternalId is not equivalent to omitting the parameter."""
        from app.services.proxy import assume_role_for_cluster

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = {"Credentials": _STS_CREDENTIALS}
        with patch("app.services.proxy.boto3.client", return_value=fake_sts):
            assume_role_for_cluster("123456789012", "ws-a", external_id=None)

        assert "ExternalId" not in fake_sts.assume_role.call_args.kwargs

    def test_role_arn_and_session_name_shape(self):
        from app.services.proxy import assume_role_for_cluster

        fake_sts = MagicMock()
        fake_sts.assume_role.return_value = {"Credentials": _STS_CREDENTIALS}
        with patch("app.services.proxy.boto3.client", return_value=fake_sts):
            assume_role_for_cluster("123456789012", "ws-a", session_suffix="kubeconfig")

        kwargs = fake_sts.assume_role.call_args.kwargs
        assert (
            kwargs["RoleArn"]
            == "arn:aws:iam::123456789012:role/superplane-workspace-ws-a"
        )
        assert kwargs["RoleSessionName"] == "superplane-kubeconfig-ws-a"

    @pytest.mark.asyncio
    async def test_external_id_read_from_stored_record_scoped_to_org(self):
        """Read from stored metadata, and only from the workspace's own org's record.

        A value derived in code would drift from the tenant's trust policy or be
        guessable; an unscoped lookup would let one tenant's record broker another's role.
        """
        from app.services.proxy import _get_workspace_external_id

        workspace = MagicMock()
        workspace.aws_account_id = uuid.uuid4()
        workspace.org_id = uuid.uuid4()

        account = MagicMock()
        account.external_id = "stored-external-id"
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account))
        )

        assert await _get_workspace_external_id(workspace, db) == "stored-external-id"
        assert db.execute.await_count == 1

        # Assert the org constraint is really in the WHERE clause, not just intended. A
        # count-only assertion passes for an unscoped query too, which is the exact bug
        # that would let one tenant's record broker another tenant's role. The filter has
        # to be read off the WHERE clause specifically: `org_id` appears in every
        # `select(CloudAccount)` column list whether or not it is filtered on.
        where = str(db.execute.await_args.args[0].whereclause)
        assert "cloud_accounts.org_id" in where
        assert "cloud_accounts.id" in where

    @pytest.mark.asyncio
    async def test_no_external_id_when_workspace_has_no_account(self):
        from app.services.proxy import _get_workspace_external_id

        workspace = MagicMock()
        workspace.aws_account_id = None
        db = MagicMock()
        db.execute = AsyncMock()

        assert await _get_workspace_external_id(workspace, db) is None
        # No account linked means no lookup at all.
        db.execute.assert_not_awaited()


class TestProxyTlsRefusal:
    """A CA that cannot be resolved refuses the operation; it never downgrades TLS."""

    def test_missing_ca_raises_proxy_error(self):
        from app.services.proxy import ProxyError, create_k8s_client

        with pytest.raises(ProxyError) as exc_info:
            create_k8s_client(
                cluster_endpoint="https://example.eks.amazonaws.com",
                cluster_ca_data="",
                credentials=_STS_CREDENTIALS,
                cluster_name="my-cluster",
                region="us-east-1",
            )
        assert exc_info.value.status_code == 502
        assert "TLS verification" in str(exc_info.value)

    def test_malformed_ca_raises_proxy_error(self):
        from app.services.proxy import ProxyError, create_k8s_client

        with pytest.raises(ProxyError, match="not valid base64"):
            create_k8s_client(
                cluster_endpoint="https://example.eks.amazonaws.com",
                cluster_ca_data="!!!not-base64!!!",
                credentials=_STS_CREDENTIALS,
                cluster_name="my-cluster",
                region="us-east-1",
            )

    def test_incomplete_credentials_raise_proxy_error(self):
        """Signing with a partial credential yields a token the cluster silently rejects."""
        from app.services.proxy import ProxyError, create_k8s_client

        creds = dict(_STS_CREDENTIALS)
        creds["SessionToken"] = ""
        with pytest.raises(ProxyError, match="Incomplete STS credentials"):
            create_k8s_client(
                cluster_endpoint="https://example.eks.amazonaws.com",
                cluster_ca_data=base64.b64encode(
                    b"-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
                ).decode(),
                credentials=creds,
                cluster_name="my-cluster",
                region="us-east-1",
            )

    def test_missing_credentials_surface_as_unavailable(self):
        """No pod identity is a 503 (config problem), not a generic gateway failure."""
        from botocore.exceptions import NoCredentialsError

        from app.services.proxy import ProxyError, assume_role_for_cluster

        fake_sts = MagicMock()
        fake_sts.assume_role.side_effect = NoCredentialsError()
        with patch("app.services.proxy.boto3.client", return_value=fake_sts):
            with pytest.raises(ProxyError) as exc_info:
                assume_role_for_cluster("123456789012", "ws-a")
        assert exc_info.value.status_code == 503

    def test_assume_failure_surfaces_as_proxy_error(self):
        from botocore.exceptions import ClientError

        from app.services.proxy import ProxyError, assume_role_for_cluster

        fake_sts = MagicMock()
        fake_sts.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"
        )
        with patch("app.services.proxy.boto3.client", return_value=fake_sts):
            with pytest.raises(ProxyError, match="Failed to assume cross-account role"):
                assume_role_for_cluster("123456789012", "ws-a")

    @pytest.mark.asyncio
    async def test_cluster_without_arn_or_name_is_refused(self):
        """Without a resolvable EKS name there is nothing to bind a token to."""
        from app.services import proxy as proxy_module

        workspace = MagicMock()
        workspace.name = "ws-a"
        workspace.org_id = uuid.uuid4()
        # An ARN supplies the account but the row has no usable name to sign.
        cluster = _cluster(eks_cluster_arn="arn:aws:eks:eu-west-1:123456789012:", name="")

        with patch.object(
            proxy_module, "get_workspace_cluster", return_value=(workspace, cluster)
        ):
            with pytest.raises(
                proxy_module.ProxyError, match="Cannot determine EKS cluster name"
            ):
                await proxy_module.get_k8s_clients(
                    uuid.uuid4(), workspace.org_id, MagicMock()
                )

    @pytest.mark.asyncio
    async def test_cluster_without_account_is_refused(self):
        from app.services import proxy as proxy_module

        workspace = MagicMock()
        workspace.name = "ws-a"
        workspace.org_id = uuid.uuid4()
        cluster = _cluster(eks_cluster_arn=None)

        with patch.object(
            proxy_module, "get_workspace_cluster", return_value=(workspace, cluster)
        ):
            with pytest.raises(
                proxy_module.ProxyError, match="Cannot determine AWS account"
            ):
                await proxy_module.get_k8s_clients(
                    uuid.uuid4(), workspace.org_id, MagicMock()
                )

    @pytest.mark.asyncio
    async def test_unresolvable_cluster_ca_refuses_the_operation(self):
        """When EKS cannot supply a CA, the brokered call fails rather than proceeding."""
        from app.services import proxy as proxy_module
        from app.services.eks_auth import EksAuthError

        workspace = MagicMock()
        workspace.name = "ws-a"
        workspace.org_id = uuid.uuid4()
        cluster = _cluster()

        with (
            patch.object(
                proxy_module, "get_workspace_cluster", return_value=(workspace, cluster)
            ),
            patch.object(
                proxy_module, "_get_workspace_external_id", return_value=None
            ),
            patch.object(
                proxy_module,
                "assume_role_for_cluster",
                return_value=_STS_CREDENTIALS,
            ),
            patch.object(
                proxy_module,
                "describe_cluster_ca",
                side_effect=EksAuthError("reported no CA certificate"),
            ),
        ):
            with pytest.raises(proxy_module.ProxyError, match="no CA certificate"):
                await proxy_module.get_k8s_clients(
                    uuid.uuid4(), workspace.org_id, MagicMock()
                )


# ---- Router endpoint tests (auth checks) ----


class TestNodeEndpoints:
    """Test GET /workspaces/{id}/nodes."""

    @pytest.mark.asyncio
    async def test_list_nodes_requires_auth(self, client):
        ws_id = uuid.uuid4()
        response = await client.get(f"/workspaces/{ws_id}/nodes")
        assert response.status_code in (401, 403)


class TestDeploymentEndpoints:
    """Test deployment CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_deployment_requires_auth(self, client):
        ws_id = uuid.uuid4()
        response = await client.post(
            f"/workspaces/{ws_id}/deployments",
            json={
                "name": "test-dep",
                "model_name": "meta-llama/Llama-3.1-8B-Instruct",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_deployments_requires_auth(self, client):
        ws_id = uuid.uuid4()
        response = await client.get(f"/workspaces/{ws_id}/deployments")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_deployment_requires_auth(self, client):
        ws_id = uuid.uuid4()
        response = await client.delete(f"/workspaces/{ws_id}/deployments/test-dep")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_deployment_validates_body(self, client):
        """Invalid body returns 422."""
        ws_id = uuid.uuid4()
        headers = _auth_header()
        response = await client.post(
            f"/workspaces/{ws_id}/deployments",
            json={"name": "Invalid_Name", "model_name": "test"},
            headers=headers,
        )
        assert response.status_code == 422


class TestHeartbeatEndpoint:
    """Test POST /internal/heartbeat.

    This route now requires the shared internal token (issue #5055, U14). It
    previously had NO authentication, so any caller able to reach the service
    could write cluster health for any cluster id — which feeds the reconciler
    and the Degraded transitions derived from `last_heartbeat`. Three of its four
    siblings under the same `/internal` prefix already enforced the token.

    The two tests below therefore now send it. `test_heartbeat_requires_token` is
    the regression guard for the hole itself.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_requires_token(self, client):
        """No credential is refused, and as 401 rather than a validation error."""
        response = await client.post(
            "/internal/heartbeat",
            json={
                "cluster_id": str(uuid.uuid4()),
                "health_status": "Healthy",
                "node_count": 3,
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_heartbeat_rejects_wrong_token(self, client, internal_token_header):
        """A wrong shared token is refused."""
        response = await client.post(
            "/internal/heartbeat",
            json={
                "cluster_id": str(uuid.uuid4()),
                "health_status": "Healthy",
                "node_count": 3,
            },
            headers={"Authorization": "Bearer not-the-internal-token"},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_heartbeat_validates_body(self, client, internal_token_header):
        """Invalid body returns 422 — after authentication succeeds."""
        response = await client.post(
            "/internal/heartbeat",
            json={"cluster_id": "not-a-uuid", "health_status": "Invalid"},
            headers=internal_token_header,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_heartbeat_valid_body_shape(self, client, internal_token_header):
        """Valid body shape is accepted (even if cluster doesn't exist in test DB)."""
        response = await client.post(
            "/internal/heartbeat",
            json={
                "cluster_id": str(uuid.uuid4()),
                "health_status": "Healthy",
                "actual_state_json": {"nodes": 3},
                "node_count": 3,
            },
            headers=internal_token_header,
        )
        # Schema validation passes; the cluster does not exist in the test DB.
        assert response.status_code in (404, 500)


class TestCostEndpoint:
    """Test GET /workspaces/{id}/cost."""

    @pytest.mark.asyncio
    async def test_cost_requires_auth(self, client):
        ws_id = uuid.uuid4()
        response = await client.get(f"/workspaces/{ws_id}/cost")
        assert response.status_code in (401, 403)


# ---- Rate limiter tests ----


class TestRateLimiter:
    """Test rate limiting middleware components."""

    def test_rate_limit_entry_allows_within_limit(self):
        from app.middleware.rate_limit import RateLimitEntry

        entry = RateLimitEntry()
        for _ in range(5):
            assert entry.is_allowed(window_seconds=60, max_requests=5)

        # 6th request should be denied
        assert not entry.is_allowed(window_seconds=60, max_requests=5)

    def test_rate_limit_entry_count(self):
        from app.middleware.rate_limit import RateLimitEntry

        entry = RateLimitEntry()
        entry.is_allowed(window_seconds=60, max_requests=10)
        entry.is_allowed(window_seconds=60, max_requests=10)
        assert entry.count == 2


# ---- Cost service tests ----


class TestCostService:
    """Test cost aggregation logic."""

    @pytest.mark.asyncio
    async def test_get_workspace_cost_workspace_not_found(self):
        """Returns error dict when workspace not found."""
        from app.services.cost import get_workspace_cost

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await get_workspace_cost(
            workspace_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            db=mock_db,
        )
        assert "error" in result
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_get_workspace_cost_no_cluster(self):
        """Returns zero cost when workspace has no cluster."""
        from app.services.cost import get_workspace_cost

        mock_workspace = MagicMock()
        mock_workspace.cluster_id = None
        mock_workspace.name = "test-ws"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_workspace
        mock_db.execute = AsyncMock(return_value=mock_result)

        ws_id = uuid.uuid4()
        result = await get_workspace_cost(
            workspace_id=ws_id,
            org_id=uuid.uuid4(),
            db=mock_db,
        )
        assert result["total_cost_usd"] == "0.00"
        assert result["workspace_name"] == "test-ws"

"""Tests for proxy, heartbeat, cost, and rate limiting."""

import uuid
from unittest.mock import AsyncMock, MagicMock

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
    """Test POST /internal/heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_validates_body(self, client):
        """Invalid body returns 422."""
        response = await client.post(
            "/internal/heartbeat",
            json={"cluster_id": "not-a-uuid", "health_status": "Invalid"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_heartbeat_valid_body_shape(self, client):
        """Valid body shape is accepted (even if cluster doesn't exist in test DB)."""
        response = await client.post(
            "/internal/heartbeat",
            json={
                "cluster_id": str(uuid.uuid4()),
                "health_status": "Healthy",
                "actual_state_json": {"nodes": 3},
                "node_count": 3,
            },
        )
        # Will fail with 500 because no real DB, but the schema validation should pass
        # (status could be 404 if DB is available but cluster doesn't exist)
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

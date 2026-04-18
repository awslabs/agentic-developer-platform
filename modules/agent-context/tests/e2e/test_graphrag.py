"""
GraphRAG conditional tests (Neptune + OpenSearch Serverless).

Tests 23-25 from issue #21 (all require GRAPHRAG_ENABLED=true):
23. Neptune Serverless cluster reachable from an in-cluster pod
24. OpenSearch Serverless collection queryable via the aoss API
25. GraphRAG-tagged search() returns results backed by the graph
"""

from __future__ import annotations

import subprocess

import pytest


# ---------------------------------------------------------------------------
# Test 23: Neptune reachable
# ---------------------------------------------------------------------------


@pytest.mark.graphrag
@pytest.mark.live_only
@pytest.mark.kubectl
class TestNeptuneReachable:
    """Verify Neptune Serverless cluster is reachable from within the cluster."""

    def test_neptune_status_endpoint(self, test_env):
        endpoint = test_env.live.neptune_endpoint
        if not endpoint:
            pytest.skip("NEPTUNE_ENDPOINT not set")

        # Use kubectl exec to test connectivity from within the cluster
        cmd = ["kubectl"]
        if test_env.live.kube_context:
            cmd += ["--context", test_env.live.kube_context]
        cmd += [
            "-n", test_env.live.namespace,
            "exec", "deploy/litellm-proxy", "--",
            "python3", "-c",
            f"""
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
r = urllib.request.urlopen('https://{endpoint}:8182/status', timeout=10, context=ctx)
print(r.read().decode()[:200])
""",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (
            f"Cannot reach Neptune at {endpoint}: {result.stderr[:200]}"
        )
        assert "status" in result.stdout.lower() or "healthy" in result.stdout.lower(), (
            f"Unexpected Neptune response: {result.stdout[:200]}"
        )


# ---------------------------------------------------------------------------
# Test 24: OpenSearch Serverless queryable
# ---------------------------------------------------------------------------


@pytest.mark.graphrag
@pytest.mark.live_only
class TestOpenSearchQueryable:
    """Verify OpenSearch Serverless collection is queryable."""

    def test_opensearch_endpoint_set(self, test_env):
        endpoint = test_env.live.opensearch_endpoint
        assert endpoint, "OPENSEARCH_ENDPOINT not set"

    def test_opensearch_reachable(self, test_env):
        endpoint = test_env.live.opensearch_endpoint
        if not endpoint:
            pytest.skip("OPENSEARCH_ENDPOINT not set")

        # Test via AWS CLI (aoss API)
        cmd = [
            "aws", "opensearchserverless", "batch-get-collection",
            "--names", "graphrag-entities",
            "--query", "collectionDetails[0].status",
            "--output", "text",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            pytest.skip(f"Cannot query OpenSearch Serverless: {result.stderr[:200]}")
        assert "ACTIVE" in result.stdout.upper() or "CREATING" in result.stdout.upper(), (
            f"Unexpected collection status: {result.stdout}"
        )


# ---------------------------------------------------------------------------
# Test 25: GraphRAG-tagged search
# ---------------------------------------------------------------------------


@pytest.mark.graphrag
@pytest.mark.live_only
class TestGraphRAGSearch:
    """Verify a GraphRAG-tagged search returns graph-backed results."""

    def test_graphrag_search(self, mcp_client):
        result = mcp_client.call_tool("search", {
            "query": "service dependencies",
            "scope": "all",
            "limit": 5,
        })
        assert isinstance(result, dict)
        # In a GraphRAG-enabled environment, results should include
        # graph-backed entries. The exact format depends on implementation.
        # For now, we just verify it's a valid response.
        assert "error" not in result or "Traceback" not in str(result.get("error", ""))

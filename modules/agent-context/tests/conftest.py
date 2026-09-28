"""
Shared pytest configuration and fixtures for agent-context E2E tests.

Provides:
- test_env: resolved TestEnvConfig (unit vs live)
- kube_client: K8s Python client (mocked in unit, real in live)
- mcp_client: HTTP wrapper around the 5 MCP tools
- openviking_client: wrapper around OpenViking REST API
- ingest_job_runner: helper to create K8s Jobs from CronJob template
- temp_repo_list: writes a modified repos.txt
- graphrag_available: skip flag for GraphRAG tests
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from .config import MODULE_ROOT, TestEnvConfig, load_config

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_env() -> TestEnvConfig:
    """Resolved test environment configuration."""
    return load_config()


# ---------------------------------------------------------------------------
# Pytest markers — auto-skip live_only in unit mode
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Auto-skip live_only, workflow, graphrag tests when not in live mode."""
    env_mode = os.environ.get("TEST_ENV", "unit").lower()
    is_live = env_mode not in ("unit", "")
    graphrag_on = os.environ.get("GRAPHRAG_ENABLED", "false").lower() == "true"

    skip_live = pytest.mark.skip(reason="TEST_ENV is not set to a live environment")
    skip_graphrag = pytest.mark.skip(reason="GRAPHRAG_ENABLED is not true")

    for item in items:
        if "live_only" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "live" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "workflow" in item.keywords and not is_live:
            item.add_marker(skip_live)
        if "graphrag" in item.keywords and not graphrag_on:
            item.add_marker(skip_graphrag)
        if "kubectl" in item.keywords and not is_live:
            item.add_marker(skip_live)


# ---------------------------------------------------------------------------
# Kubernetes client
# ---------------------------------------------------------------------------


@dataclass
class MockKubeClient:
    """Minimal mock Kubernetes client for unit tests."""

    namespace: str = "agent-context"
    _deployments: dict = field(default_factory=dict)
    _services: dict = field(default_factory=dict)
    _pvcs: dict = field(default_factory=dict)
    _cronjobs: dict = field(default_factory=dict)
    _pods: dict = field(default_factory=dict)
    _service_accounts: dict = field(default_factory=dict)

    def __post_init__(self):
        # Pre-populate with expected resources for unit tests
        expected_deploys = [
            "litellm-proxy", "openviking-server", "deepwiki", "codegraph",
        ]
        for name in expected_deploys:
            self._deployments[name] = {
                "metadata": {"name": name, "namespace": self.namespace},
                "status": {"readyReplicas": 1, "replicas": 1},
            }

        expected_services = {
            "openviking": {"port": 1933},
            "deepwiki": {"port": 8001},
            "litellm-proxy": {"port": 4000},
            "context-mcp": {"port": 5100},
        }
        for name, info in expected_services.items():
            self._services[name] = {
                "metadata": {"name": name, "namespace": self.namespace},
                "spec": {"ports": [{"port": info["port"]}]},
            }

        pvcs = {
            "openviking-data": {"storage": "200Gi", "phase": "Bound"},
            "platform-data": {"storage": "8.0Ei", "phase": "Bound"},
        }
        for name, info in pvcs.items():
            self._pvcs[name] = {
                "metadata": {"name": name, "namespace": self.namespace},
                "spec": {"resources": {"requests": {"storage": info["storage"]}}},
                "status": {"phase": info["phase"]},
            }

        self._cronjobs["ingestion-refresh"] = {
            "metadata": {"name": "ingestion-refresh", "namespace": self.namespace},
            "spec": {"schedule": "0 6 * * *"},
        }

        self._service_accounts["agent-context-sa"] = {
            "metadata": {
                "name": "agent-context-sa",
                "namespace": self.namespace,
                "annotations": {
                    "eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/adp-dev-agent-context-irsa",
                },
            },
        }

        # No pods in bad states
        self._pods = {}

    def get_deployment(self, name: str) -> dict | None:
        return self._deployments.get(name)

    def list_deployments(self) -> list[dict]:
        return list(self._deployments.values())

    def get_service(self, name: str) -> dict | None:
        return self._services.get(name)

    def get_pvc(self, name: str) -> dict | None:
        return self._pvcs.get(name)

    def list_pvcs(self) -> list[dict]:
        return list(self._pvcs.values())

    def get_cronjob(self, name: str) -> dict | None:
        return self._cronjobs.get(name)

    def get_service_account(self, name: str) -> dict | None:
        return self._service_accounts.get(name)

    def list_pods(self) -> list[dict]:
        return list(self._pods.values())

    def get_pods_in_bad_state(self) -> list[dict]:
        """Return pods in CrashLoopBackOff, Error, or ImagePullBackOff."""
        bad_states = {"CrashLoopBackOff", "Error", "ImagePullBackOff"}
        result = []
        for pod in self._pods.values():
            statuses = pod.get("status", {}).get("containerStatuses", [])
            for cs in statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason") in bad_states:
                    result.append(pod)
        return result


class LiveKubeClient:
    """Thin wrapper around kubectl for live tests."""

    def __init__(self, namespace: str, context: str = ""):
        self.namespace = namespace
        self.context = context

    def _run(self, args: list[str]) -> dict | list | str:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", self.namespace] + args + ["-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)

    def get_deployment(self, name: str) -> dict | None:
        data = self._run(["get", "deployment", name])
        return data if data else None

    def list_deployments(self) -> list[dict]:
        data = self._run(["get", "deployments"])
        return data.get("items", []) if isinstance(data, dict) else []

    def get_service(self, name: str) -> dict | None:
        data = self._run(["get", "service", name])
        return data if data else None

    def get_pvc(self, name: str) -> dict | None:
        data = self._run(["get", "pvc", name])
        return data if data else None

    def list_pvcs(self) -> list[dict]:
        data = self._run(["get", "pvc"])
        return data.get("items", []) if isinstance(data, dict) else []

    def get_cronjob(self, name: str) -> dict | None:
        data = self._run(["get", "cronjob", name])
        return data if data else None

    def get_service_account(self, name: str) -> dict | None:
        data = self._run(["get", "serviceaccount", name])
        return data if data else None

    def list_pods(self) -> list[dict]:
        data = self._run(["get", "pods"])
        return data.get("items", []) if isinstance(data, dict) else []

    def get_pods_in_bad_state(self) -> list[dict]:
        bad_states = {"CrashLoopBackOff", "Error", "ImagePullBackOff"}
        result = []
        for pod in self.list_pods():
            statuses = pod.get("status", {}).get("containerStatuses", [])
            for cs in statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason") in bad_states:
                    result.append(pod)
        return result


@pytest.fixture(scope="session")
def kube_client(test_env: TestEnvConfig):
    """Kubernetes client: mocked in unit mode, real kubectl wrapper in live mode."""
    if test_env.is_unit:
        return MockKubeClient(namespace="agent-context")
    return LiveKubeClient(
        namespace=test_env.live.namespace,
        context=test_env.live.kube_context,
    )


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------


@dataclass
class MCPToolSchema:
    """Schema for an MCP tool."""

    name: str
    description: str
    parameters: dict = field(default_factory=dict)


# Expected MCP tool definitions
EXPECTED_MCP_TOOLS = [
    MCPToolSchema(
        name="search",
        description="Find relevant code, documentation, and past learnings",
        parameters={
            "query": {"type": "string", "required": True},
            "scope": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False},
            "project": {"type": "string", "required": False},
        },
    ),
    MCPToolSchema(
        name="understand",
        description="Get deep understanding of a specific repo, directory, or file",
        parameters={
            "target": {"type": "string", "required": True},
            "depth": {"type": "string", "required": False},
            "project": {"type": "string", "required": False},
        },
    ),
    MCPToolSchema(
        name="impact",
        description="Analyse what would be affected by changing a symbol, file, or pattern",
        parameters={
            "target": {"type": "string", "required": True},
            "cross_repo": {"type": "boolean", "required": False},
            "project": {"type": "string", "required": False},
        },
    ),
    MCPToolSchema(
        name="browse",
        description="Navigate the indexed content filesystem",
        parameters={
            "action": {"type": "string", "required": True},
            "uri": {"type": "string", "required": True},
            "depth": {"type": "integer", "required": False},
            "project": {"type": "string", "required": False},
        },
    ),
    MCPToolSchema(
        name="remember",
        description="Save session context, decisions, and learnings to long-term memory",
        parameters={
            "session_id": {"type": "string", "required": True},
            "messages": {"type": "array", "required": True},
            "outcome": {"type": "string", "required": False},
        },
    ),
    MCPToolSchema(
        name="experience",
        description="Save or recall experiential knowledge (per-user, persona-scoped, synthesized)",
        parameters={
            "action": {"type": "string", "enum": ["save", "recall", "list_syntheses"], "required": True},
            "persona": {"type": "string", "enum": ["operations", "developer", "architect", "reviewer"], "required": True},
            "content": {"type": "string", "required": False},
            "learning_type": {"type": "string", "required": False},
            "context": {"type": "object", "required": False},
            "query": {"type": "string", "required": False},
            "visibility": {"type": "string", "enum": ["private", "shared"], "required": False},
            "limit": {"type": "integer", "required": False},
            "cross_persona": {"type": "boolean", "required": False},
        },
    ),
]


class MockMCPClient:
    """Fake MCP client that returns well-formed responses for unit tests."""

    def __init__(self):
        self.base_url = "http://mock-mcp:5100"
        self._tools = EXPECTED_MCP_TOOLS

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Simulate an MCP tool call. Returns well-formed empty/mock results."""
        if name == "search":
            return {"results": [], "total": 0, "query": arguments.get("query", "")}
        elif name == "understand":
            return {"target": arguments.get("target", ""), "summary": "Mock overview"}
        elif name == "impact":
            return {"target": arguments.get("target", ""), "affected": [], "blast_radius": 0}
        elif name == "browse":
            return {"action": arguments.get("action", "ls"), "entries": []}
        elif name == "remember":
            return {"stored": True, "session_id": arguments.get("session_id", "")}
        elif name == "experience":
            action = arguments.get("action", "")
            if action == "save":
                return {"status": "saved", "id": f"01MOCK{uuid.uuid4().hex[:6].upper()}", "persona": arguments.get("persona", "developer"), "visibility": arguments.get("visibility", "private")}
            elif action == "recall":
                return {"status": "ok", "query": arguments.get("query", ""), "results": [], "total": 0}
            elif action == "list_syntheses":
                return {"status": "ok", "persona": arguments.get("persona", "developer"), "syntheses": [], "total": 0}
            else:
                return {"error": f"Invalid action: {action}"}
        else:
            return {"error": f"Unknown tool: {name}"}

    def call_tool_raw(self, payload: Any) -> httpx.Response:
        """Simulate a raw HTTP call (for malformed-JSON tests)."""
        # In unit mode, we simulate the server's response
        if not isinstance(payload, (str, bytes)):
            # Valid JSON
            return httpx.Response(
                status_code=200,
                json={"results": []},
            )
        # Malformed input gets a 400
        return httpx.Response(
            status_code=400,
            json={"error": "Invalid JSON"},
        )


class LiveMCPClient:
    """Real MCP client that hits the deployed endpoint."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def get_tools(self) -> list[dict]:
        resp = httpx.get(f"{self.base_url}/tools", headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = httpx.post(
            f"{self.base_url}/call",
            json={"name": name, "arguments": arguments},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def call_tool_raw(self, payload: Any) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}/call",
            content=payload if isinstance(payload, (str, bytes)) else json.dumps(payload),
            headers=self._headers(),
            timeout=10,
        )


@pytest.fixture(scope="session")
def mcp_client(test_env: TestEnvConfig):
    """MCP client: mocked in unit mode, real HTTP in live mode."""
    if test_env.is_unit:
        return MockMCPClient()
    return LiveMCPClient(
        base_url=test_env.live.mcp_url,
        api_key=test_env.live.ov_api_key,
    )


# ---------------------------------------------------------------------------
# OpenViking client
# ---------------------------------------------------------------------------


class MockOpenVikingClient:
    """Fake OpenViking client for unit tests."""

    def __init__(self):
        self.base_url = "http://mock-openviking:1933"

    def health(self) -> dict:
        return {"healthy": True}

    def search(self, query: str, scope: str = "all", limit: int = 10) -> dict:
        return {"results": [], "total": 0}


class LiveOpenVikingClient:
    """Real OpenViking REST API client."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def health(self) -> dict:
        resp = httpx.get(f"{self.base_url}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, scope: str = "all", limit: int = 10) -> dict:
        resp = httpx.post(
            f"{self.base_url}/find",
            json={"query": query, "scope": scope, "limit": limit},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


@pytest.fixture(scope="session")
def openviking_client(test_env: TestEnvConfig):
    """OpenViking client: mocked in unit mode, real HTTP in live mode."""
    if test_env.is_unit:
        return MockOpenVikingClient()
    return LiveOpenVikingClient(
        base_url=test_env.live.openviking_url,
        api_key=test_env.live.ov_api_key,
    )


# ---------------------------------------------------------------------------
# Ingestion job runner (live only)
# ---------------------------------------------------------------------------


@dataclass
class IngestJobRunner:
    """Helper to create K8s Jobs from the ingestion-refresh CronJob template."""

    namespace: str
    context: str = ""
    is_live: bool = False

    def create_job(self, name_suffix: str = "") -> str:
        """Create a Job from the CronJob template. Returns the job name."""
        if not self.is_live:
            return f"manual-test-{uuid.uuid4().hex[:8]}"

        job_name = f"manual-test-{name_suffix or uuid.uuid4().hex[:8]}"
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += [
            "-n", self.namespace,
            "create", "job", job_name,
            "--from=cronjob/ingestion-refresh",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        return job_name

    def wait_for_completion(self, job_name: str, timeout_seconds: int = 600) -> bool:
        if not self.is_live:
            return True
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += [
            "-n", self.namespace,
            "wait", "--for=condition=complete",
            f"job/{job_name}",
            f"--timeout={timeout_seconds}s",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 30)
        return result.returncode == 0

    def get_logs(self, job_name: str) -> str:
        if not self.is_live:
            return "Mock job logs: completed successfully"
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", self.namespace, "logs", f"job/{job_name}", "--tail=200"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.stdout


@pytest.fixture(scope="session")
def ingest_job_runner(test_env: TestEnvConfig) -> IngestJobRunner:
    """Helper to create and monitor ingestion Jobs."""
    return IngestJobRunner(
        namespace=test_env.live.namespace if test_env.is_live else "agent-context",
        context=test_env.live.kube_context if test_env.is_live else "",
        is_live=test_env.is_live,
    )


# ---------------------------------------------------------------------------
# Temp repo list
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_repo_list(test_env: TestEnvConfig, tmp_path: Path):
    """Returns a function that writes a modified repos.txt.

    In unit mode: writes to a temp file.
    In live mode: writes to the S3 Files PVC via kubectl exec.
    """

    def _create(extra: list[str] | None = None) -> Path:
        repos_file = MODULE_ROOT / "index_content" / "repos.txt"
        content = repos_file.read_text() if repos_file.exists() else ""
        if extra:
            content += "\n" + "\n".join(extra) + "\n"

        out = tmp_path / "repos.txt"
        out.write_text(content)
        return out

    return _create


# ---------------------------------------------------------------------------
# GraphRAG availability
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def graphrag_available(test_env: TestEnvConfig) -> bool:
    """Check whether GraphRAG tests should run."""
    if test_env.is_unit:
        return False
    return test_env.live.graphrag_enabled

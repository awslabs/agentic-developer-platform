"""
Configuration loader for the agent-context E2E test suite.

Reads from a single source: Terraform output (preferred) or env vars (fallback).
In unit mode (TEST_ENV=unit or unset), returns mock defaults.
In live mode (TEST_ENV=dev), reads real values from terraform output -json.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Root of the agent-context module (two levels up from tests/config.py)
MODULE_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = MODULE_ROOT / "terraform"
SCRIPTS_DIR = MODULE_ROOT / "scripts"


@dataclass(frozen=True)
class LiveConfig:
    """Configuration for live-mode tests against a deployed cluster."""

    namespace: str = "agent-context"
    kube_context: str = ""
    mcp_url: str = "http://context-mcp.agent-context.svc.cluster.local:5100"
    openviking_url: str = "http://openviking.agent-context.svc.cluster.local:1933"
    ov_api_key: str = ""
    irsa_role_arn: str = ""
    bucket_name: str = ""
    dynamodb_table_name: str = ""
    graphrag_enabled: bool = False
    neptune_endpoint: str = ""
    opensearch_endpoint: str = ""
    github_org: str = "aws-e"
    github_repo: str = "adp"


@dataclass
class TestEnvConfig:
    """Resolved test environment configuration."""

    mode: str = "unit"  # "unit" or "dev"/"live"
    live: LiveConfig = field(default_factory=LiveConfig)

    @property
    def is_live(self) -> bool:
        return self.mode not in ("unit", "")

    @property
    def is_unit(self) -> bool:
        return not self.is_live


def _load_terraform_outputs() -> dict:
    """Run terraform output -json and return the parsed dict.

    Returns an empty dict if terraform is not available or the command fails.
    """
    if not TERRAFORM_DIR.is_dir():
        return {}

    try:
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(TERRAFORM_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        outputs = json.loads(result.stdout)
        # terraform output -json returns {"key": {"value": ..., "type": ...}, ...}
        return {k: v.get("value", "") for k, v in outputs.items()}
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def load_config() -> TestEnvConfig:
    """Load test configuration from env vars and (optionally) Terraform outputs.

    Priority: env var > terraform output > default.
    """
    mode = os.environ.get("TEST_ENV", "unit").lower()

    if mode in ("unit", ""):
        return TestEnvConfig(mode="unit")

    # Live mode — try terraform outputs first, fall back to env vars
    tf = _load_terraform_outputs()

    live = LiveConfig(
        namespace=os.environ.get("NAMESPACE", tf.get("namespace", "agent-context")),
        kube_context=os.environ.get("KUBE_CONTEXT", ""),
        mcp_url=os.environ.get(
            "MCP_URL",
            "http://context-mcp.agent-context.svc.cluster.local:5100",
        ),
        openviking_url=os.environ.get(
            "OPENVIKING_URL",
            "http://openviking.agent-context.svc.cluster.local:1933",
        ),
        ov_api_key=os.environ.get("OV_API_KEY", ""),
        irsa_role_arn=os.environ.get("IRSA_ROLE_ARN", tf.get("irsa_role_arn", "")),
        bucket_name=os.environ.get("BUCKET_NAME", tf.get("bucket_name", "")),
        dynamodb_table_name=os.environ.get(
            "DYNAMODB_TABLE_NAME",
            tf.get("dynamodb_table_name", "adp-context-service-state"),
        ),
        graphrag_enabled=os.environ.get("GRAPHRAG_ENABLED", "false").lower() == "true",
        neptune_endpoint=os.environ.get("NEPTUNE_ENDPOINT", tf.get("neptune_endpoint", "")),
        opensearch_endpoint=os.environ.get(
            "OPENSEARCH_ENDPOINT",
            tf.get("opensearch_collection_endpoint", ""),
        ),
        github_org=os.environ.get("GITHUB_ORG", "aws-e"),
        github_repo=os.environ.get("GITHUB_REPO", "adp"),
    )

    return TestEnvConfig(mode=mode, live=live)

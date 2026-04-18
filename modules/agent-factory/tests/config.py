"""
Configuration discovery for agent-factory tests.

Single source: reads from `terraform output -json` in modules/agent-factory/infra/,
falls back to environment variables.  Fails fast if required keys are missing in
live mode.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent  # modules/agent-factory
INFRA_DIR = MODULE_ROOT / "infra"

# Terraform output key -> env var fallback mapping
_TF_ENV_MAP = {
    "gateway_ws_endpoint": "WS_URL",
    "gateway_input_queue_url": "TASKS_QUEUE_URL",
    "gateway_response_queue_url": "RESPONSES_QUEUE_URL",
    "gateway_sessions_table": "SESSIONS_TABLE",
    "runner_role_arn": "RUNNER_ROLE_ARN",
    "secrets_prefix": "SECRETS_PREFIX",
    "gateway_agent_role_arn": "GATEWAY_AGENT_ROLE_ARN",
    "gateway_agent_role_name": "GATEWAY_AGENT_ROLE_NAME",
}

# Expected Terraform outputs (test 27 asserts all present)
EXPECTED_TF_OUTPUTS = list(_TF_ENV_MAP.keys())


@dataclass
class LiveConfig:
    """Configuration for live (deployed) tests."""

    ws_url: str = ""
    tasks_queue_url: str = ""
    responses_queue_url: str = ""
    sessions_table: str = ""
    runner_role_arn: str = ""
    secrets_prefix: str = ""
    gateway_agent_role_arn: str = ""
    gateway_agent_role_name: str = ""
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_agent_client_id: str = ""
    test_user_email: str = ""
    test_user_password: str = ""
    kube_context: str = ""
    github_org: str = ""
    github_repo: str = "adp"


@dataclass
class TestEnvConfig:
    """Resolved test environment configuration."""

    mode: str = "unit"  # "unit" or "dev" / "staging" / "prod"
    live: LiveConfig = field(default_factory=LiveConfig)

    @property
    def is_unit(self) -> bool:
        return self.mode in ("unit", "")

    @property
    def is_live(self) -> bool:
        return not self.is_unit


def _load_terraform_outputs() -> dict[str, str]:
    """Try to load terraform outputs from the infra directory."""
    if not INFRA_DIR.exists():
        return {}
    try:
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=str(INFRA_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {}
        raw = json.loads(result.stdout)
        return {k: v.get("value", "") for k, v in raw.items() if isinstance(v, dict)}
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {}


def load_config() -> TestEnvConfig:
    """Load test configuration.

    In unit mode, returns defaults (no external calls).
    In live mode, resolves from terraform output -> env vars.
    """
    mode = os.environ.get("TEST_ENV", "unit").lower()
    if mode in ("unit", ""):
        return TestEnvConfig(mode="unit")

    # Try terraform outputs first, then env var fallbacks
    tf_outputs = _load_terraform_outputs()

    def _resolve(tf_key: str, env_key: str = "", default: str = "") -> str:
        if tf_key in tf_outputs:
            return str(tf_outputs[tf_key])
        return os.environ.get(env_key or _TF_ENV_MAP.get(tf_key, ""), default)

    live = LiveConfig(
        ws_url=_resolve("gateway_ws_endpoint", "WS_URL"),
        tasks_queue_url=_resolve("gateway_input_queue_url", "TASKS_QUEUE_URL"),
        responses_queue_url=_resolve("gateway_response_queue_url", "RESPONSES_QUEUE_URL"),
        sessions_table=_resolve("gateway_sessions_table", "SESSIONS_TABLE"),
        runner_role_arn=_resolve("runner_role_arn", "RUNNER_ROLE_ARN"),
        secrets_prefix=_resolve("secrets_prefix", "SECRETS_PREFIX"),
        gateway_agent_role_arn=_resolve("gateway_agent_role_arn", "GATEWAY_AGENT_ROLE_ARN"),
        gateway_agent_role_name=_resolve("gateway_agent_role_name", "GATEWAY_AGENT_ROLE_NAME"),
        cognito_user_pool_id=os.environ.get("COGNITO_USER_POOL_ID", ""),
        cognito_client_id=os.environ.get("COGNITO_CLIENT_ID", ""),
        cognito_agent_client_id=os.environ.get("COGNITO_AGENT_CLIENT_ID", ""),
        test_user_email=os.environ.get("TEST_USER_EMAIL", ""),
        test_user_password=os.environ.get("TEST_USER_PASSWORD", ""),
        kube_context=os.environ.get("KUBE_CONTEXT", ""),
        github_org=os.environ.get("GITHUB_ORG", "aws-e"),
        github_repo=os.environ.get("GITHUB_REPO", "adp"),
    )

    # Fail fast if critical live config is missing
    missing = []
    if not live.ws_url:
        missing.append("WS_URL / gateway_ws_endpoint")
    if not live.tasks_queue_url:
        missing.append("TASKS_QUEUE_URL / gateway_input_queue_url")
    if missing:
        raise RuntimeError(
            f"Live mode (TEST_ENV={mode}) requires: {', '.join(missing)}. "
            "Set via env vars or ensure `terraform output` works in modules/agent-factory/infra/."
        )

    return TestEnvConfig(mode=mode, live=live)

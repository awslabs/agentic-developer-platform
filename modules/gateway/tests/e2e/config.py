"""
E2E test configuration with dual-mode support (unit / live).

In unit mode (TEST_ENV=unit or unset), tests run against the FastAPI app
via httpx's ASGI transport — no AWS services are contacted.

In live mode (TEST_ENV=dev), tests hit the deployed gateway and read
configuration from environment variables, falling back to AWS SSM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _get_env() -> str:
    """Return the current test environment: 'unit' or 'dev'."""
    return os.environ.get("TEST_ENV", "unit").lower()


def is_live() -> bool:
    """Return True when tests should hit a deployed environment."""
    return _get_env() != "unit"


@dataclass(frozen=True)
class LiveTestConfig:
    """Configuration for live-mode E2E tests.

    Values are resolved in priority order:
    1. Explicit environment variable
    2. AWS SSM Parameter Store (requires boto3 + credentials)
    """

    cloudfront_domain: str = ""
    api_gateway_url: str = ""
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_agent_client_id: str = ""
    cognito_domain: str = ""
    test_user_email: str = ""
    test_user_password: str = ""
    test_bedrock_model: str = ""
    aws_region: str = "us-east-1"
    environment: str = "dev"

# Default Bedrock model for live proxy tests.
# Cross-region inference profile that resolves dynamically.
DEFAULT_BEDROCK_MODEL = "global.anthropic.claude-sonnet-4-6"


def get_test_bedrock_model() -> str:
    """Return the Bedrock model ID to use for live proxy tests.

    Priority: TEST_BEDROCK_MODEL env var > live config > default.
    """
    env_val = os.environ.get("TEST_BEDROCK_MODEL", "")
    if env_val:
        return env_val
    return DEFAULT_BEDROCK_MODEL


def _ssm_get(name: str, region: str = "us-east-1") -> str | None:
    """Fetch a single SSM parameter; return None on any failure."""
    try:
        import boto3  # noqa: F811

        ssm = boto3.client("ssm", region_name=region)
        resp = ssm.get_parameter(Name=name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception:
        return None


def load_live_config() -> LiveTestConfig:
    """Build a LiveTestConfig from env vars, falling back to SSM.

    Raises ``RuntimeError`` if any required field is missing.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    env = os.environ.get("ENVIRONMENT", "dev")

    def _resolve(env_key: str, ssm_key: str | None = None) -> str:
        val = os.environ.get(env_key, "")
        if val:
            return val
        if ssm_key:
            ssm_val = _ssm_get(ssm_key, region)
            if ssm_val:
                return ssm_val
        return ""

    cfg = LiveTestConfig(
        cloudfront_domain=_resolve("CLOUDFRONT_DOMAIN", f"/adp/{env}/gateway/cloudfront-domain"),
        api_gateway_url=_resolve("API_GATEWAY_URL", f"/adp/{env}/gateway/api-gateway-url"),
        cognito_user_pool_id=_resolve("COGNITO_USER_POOL_ID"),
        cognito_client_id=_resolve("COGNITO_CLIENT_ID"),
        cognito_agent_client_id=_resolve("COGNITO_AGENT_CLIENT_ID"),
        cognito_domain=_resolve("COGNITO_DOMAIN"),
        test_user_email=_resolve("TEST_USER_EMAIL"),
        test_user_password=_resolve("TEST_USER_PASSWORD"),
        aws_region=region,
        environment=env,
    )

    # Validate required fields. `api_gateway_url` is the main API contract target;
    # `cloudfront_domain` is only strictly needed by frontend and CDN-layer tests.
    missing: list[str] = []
    for fld in ("api_gateway_url", "cloudfront_domain", "cognito_user_pool_id", "cognito_client_id"):
        if not getattr(cfg, fld):
            missing.append(fld)

    if missing:
        raise RuntimeError(
            f"Live-mode E2E tests require the following config but they are missing: {', '.join(missing)}. "
            "Set the corresponding environment variables or ensure SSM parameters are populated."
        )

    return cfg

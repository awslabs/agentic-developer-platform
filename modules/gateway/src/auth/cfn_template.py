"""CloudFormation template renderer for AWS role connect flow.

Issue #562: Self-serve AWS account connect UI — CloudFormation Quick-Create flow.

AWS Console's Quick-Create flow requires a ``templateURL`` query parameter
pointing to a publicly-readable HTTPS URL (it does not accept an inline
``templateBody``). We don't want a public S3 bucket, so we serve the template
from the gateway itself at an unauthenticated endpoint. To prevent arbitrary
scraping we gate that endpoint on a per-request HMAC-signed, short-lived
token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from src.shared.config import get_settings

_TEMPLATE_DIR = Path(__file__).parent / "cfn_templates"
_TEMPLATE_FILE = "aws_role_v1.yaml"

# How long a signed template URL is valid.  Needs to outlive the time from
# Launch-click through AWS Console rendering the stack-create preview — 10
# minutes is comfortably above the ~seconds the user sees.
_TEMPLATE_URL_TTL_SECONDS = 10 * 60

# Cache the loaded template content (immutable at runtime).
_template_cache: str | None = None


def _load_template() -> str:
    """Load and cache the YAML template from disk."""
    global _template_cache
    if _template_cache is None:
        template_path = _TEMPLATE_DIR / _TEMPLATE_FILE
        _template_cache = template_path.read_text(encoding="utf-8")
    return _template_cache


def get_template_body() -> str:
    """Return the raw YAML template — consumed by the public serve endpoint."""
    return _load_template()


def get_gateway_role_arn() -> str:
    """Return the gateway pod's IRSA role ARN from environment.

    Falls back to a placeholder for local dev / tests.
    """
    return os.environ.get(
        "ADP_GATEWAY_ROLE_ARN",
        "arn:aws:iam::000000000000:role/adp-dev-gateway-irsa",
    )


# ---------------------------------------------------------------------------
# Signed-URL helpers
# ---------------------------------------------------------------------------


def _signing_secret() -> bytes:
    """Key used to HMAC the template token. Reuses the gateway's JWT secret."""
    settings = get_settings()
    secret = settings.token_secret_key or "dev-insecure-template-secret"
    return secret.encode("utf-8")


def _sign(credential_id: str, expires_at: int) -> str:
    payload = f"{credential_id}.{expires_at}".encode()
    digest = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_template_token(credential_id: str) -> tuple[str, int]:
    """Return ``(signature, expires_at)`` for this credential's template URL."""
    expires_at = int(time.time()) + _TEMPLATE_URL_TTL_SECONDS
    return _sign(credential_id, expires_at), expires_at


def verify_template_token(credential_id: str, expires_at: int, signature: str) -> bool:
    """Validate a template-fetch token. Constant-time comparison + expiry check."""
    if expires_at < int(time.time()):
        return False
    expected = _sign(credential_id, expires_at)
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Launch URL builder
# ---------------------------------------------------------------------------


def _gateway_base_url() -> str:
    """Public base URL the AWS Console can reach. Required in production."""
    settings = get_settings()
    return settings.gateway_base_url.rstrip("/")


def build_template_url(credential_id: str) -> str:
    """Build the signed HTTPS URL AWS Console will fetch the template from.

    The URL is prefixed with ``/api`` because CloudFront routes ``/api/*`` to
    the backend ALB and a CloudFront Function strips the prefix before the
    request reaches FastAPI — so the served route remains
    ``/auth/credentials/aws/cfn-template.yaml``.
    """
    signature, expires_at = mint_template_token(credential_id)
    base = _gateway_base_url()
    query = urlencode({"cid": credential_id, "exp": expires_at, "sig": signature})
    return f"{base}/api/auth/credentials/aws/cfn-template.yaml?{query}"


def build_launch_url(
    *,
    credential_id: str,
    nickname: str,
    external_id: str,
    account_id: str,
    user_id: str,
    role_name: str = "ADP-Agent-Role",
    region: str = "us-east-1",
) -> str:
    """Build a CloudFormation Quick-Create URL.

    AWS Console requires ``templateURL`` — we can't pass ``templateBody``
    inline. We serve the template from a signed, short-lived gateway endpoint.
    """
    gateway_role_arn = get_gateway_role_arn()

    # Sanitize nickname for stack name (alphanumeric + hyphens only)
    stack_nickname = "".join(c if c.isalnum() or c == "-" else "-" for c in nickname)

    template_url = build_template_url(credential_id)

    params = {
        "stackName": f"ADP-Agent-{stack_nickname}",
        "templateURL": template_url,
        "param_Nickname": nickname,
        "param_ExternalId": external_id,
        "param_GatewayRolePrincipal": gateway_role_arn,
        "param_UserSessionTag": user_id,
    }

    base_url = f"https://{region}.console.aws.amazon.com/cloudformation/home"
    query_string = urlencode(params, quote_via=quote)
    return f"{base_url}?region={region}#/stacks/quickcreate?{query_string}"


def compute_role_arn(account_id: str, nickname: str) -> str:
    """Compute the expected role ARN from account ID and nickname."""
    return f"arn:aws:iam::{account_id}:role/ADP-Agent-{nickname}"

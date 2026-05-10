"""CloudFormation template renderer for AWS role connect flow.

Issue #562: Self-serve AWS account connect UI — CloudFormation Quick-Create flow.

Loads the static YAML template and builds a CloudFormation Quick-Create URL
with all parameters URL-encoded inline (templateBody approach — no public S3).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlencode

_TEMPLATE_DIR = Path(__file__).parent / "cfn_templates"
_TEMPLATE_FILE = "aws_role_v1.yaml"

# Cache the loaded template content (immutable at runtime).
_template_cache: str | None = None


def _load_template() -> str:
    """Load and cache the YAML template from disk."""
    global _template_cache
    if _template_cache is None:
        template_path = _TEMPLATE_DIR / _TEMPLATE_FILE
        _template_cache = template_path.read_text(encoding="utf-8")
    return _template_cache


def get_gateway_role_arn() -> str:
    """Return the gateway pod's IRSA role ARN from environment.

    Falls back to a placeholder for local dev / tests.
    """
    return os.environ.get(
        "ADP_GATEWAY_ROLE_ARN",
        "arn:aws:iam::000000000000:role/adp-dev-gateway-irsa",
    )


def build_launch_url(
    *,
    nickname: str,
    external_id: str,
    account_id: str,
    user_id: str,
    role_name: str = "ADP-Agent-Role",
    region: str = "us-east-1",
) -> str:
    """Build a CloudFormation Quick-Create URL with the template inline.

    Parameters
    ----------
    nickname : str
        User-chosen label for the connection (used as stack name suffix).
    external_id : str
        UUIDv4 for confused-deputy protection.
    account_id : str
        Target AWS account ID (12 digits).
    user_id : str
        ADP user ID — passed as the session tag value.
    role_name : str
        IAM role name (default: ADP-Agent-Role).
    region : str
        AWS region for the Console URL.

    Returns
    -------
    str
        Full CloudFormation Quick-Create URL.
    """
    template_body = _load_template()
    gateway_role_arn = get_gateway_role_arn()

    # Sanitize nickname for stack name (alphanumeric + hyphens only)
    stack_nickname = "".join(c if c.isalnum() or c == "-" else "-" for c in nickname)

    # Build the Quick-Create URL parameters
    params = {
        "stackName": f"ADP-Agent-{stack_nickname}",
        "templateBody": template_body,
        "param_Nickname": nickname,
        "param_ExternalId": external_id,
        "param_GatewayRolePrincipal": gateway_role_arn,
        "param_UserSessionTag": user_id,
    }

    base_url = f"https://{region}.console.aws.amazon.com/cloudformation/home"
    # Quick-Create uses a fragment (#) path with query params
    query_string = urlencode(params, quote_via=quote)
    return f"{base_url}?region={region}#/stacks/quickcreate?{query_string}"


def compute_role_arn(account_id: str, nickname: str) -> str:
    """Compute the expected role ARN from account ID and nickname."""
    return f"arn:aws:iam::{account_id}:role/ADP-Agent-{nickname}"

"""CloudFormation template URL builder for the AWS role connect flow.

Issue #562: Self-serve AWS account connect UI — CloudFormation Quick-Create flow.

AWS Console's Quick-Create flow requires a ``templateURL`` query parameter
pointing to an S3-hosted template (it rejects non-S3 hosts with "TemplateURL
must be a supported URL"). We host the template in the existing private
frontend bucket and generate a pre-signed S3 GET URL per connect attempt.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode

import boto3
from botocore.config import Config

_DEFAULT_PRESIGNED_TTL_SECONDS = 10 * 60
_DEFAULT_TEMPLATE_KEY = "cfn-templates/aws_role_v1.yaml"


def _template_bucket() -> str:
    """Bucket where the CFN template YAML is stored. Required at runtime."""
    return os.environ.get("ADP_CFN_TEMPLATE_BUCKET", "")


def _template_key() -> str:
    return os.environ.get("ADP_CFN_TEMPLATE_KEY", _DEFAULT_TEMPLATE_KEY)


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def get_gateway_role_arn() -> str:
    """Return the gateway pod's IRSA role ARN from environment.

    Falls back to a placeholder for local dev / tests.
    """
    return os.environ.get(
        "ADP_GATEWAY_ROLE_ARN",
        "arn:aws:iam::000000000000:role/adp-dev-gateway-irsa",
    )


def build_template_url(credential_id: str) -> str:  # credential_id kept for call-site compat / logging
    """Generate a pre-signed S3 URL the AWS Console can fetch the template from.

    CloudFormation accepts S3 pre-signed URLs as ``templateURL`` (but rejects
    non-S3 hosts). The bucket stays private; the signature authorizes the
    fetch. TTL is short (10 min) so the URL can't be indexed or replayed.
    """
    bucket = _template_bucket()
    if not bucket:
        raise RuntimeError(
            "ADP_CFN_TEMPLATE_BUCKET is not set — cannot build CFN templateURL"
        )

    # Virtual-hosted addressing keeps the host on *.s3.amazonaws.com, which is
    # what CloudFormation's URL validator accepts.
    s3 = boto3.client(
        "s3",
        region_name=_region(),
        config=Config(signature_version="s3v4"),
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": _template_key()},
        ExpiresIn=_DEFAULT_PRESIGNED_TTL_SECONDS,
    )


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
    """Build a CloudFormation Quick-Create URL pointing at the pre-signed template."""
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

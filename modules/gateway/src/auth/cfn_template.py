"""CloudFormation template URL builder for the AWS role connect flow.

Issue #562: Self-serve AWS account connect UI — CloudFormation Quick-Create flow.

AWS Console's Quick-Create flow requires a ``templateURL`` query parameter
pointing to an S3-hosted template (it rejects non-S3 hosts with "TemplateURL
must be a supported URL"). We host the template in the existing private
frontend bucket and generate a pre-signed S3 GET URL per connect attempt.

Issue #4742: two template versions now ship side by side.

``v1`` is the original read-only agent-delegation role: ``ReadOnlyAccess``, with
a trust policy pinned to one ADP user via ``aws:RequestTag/adp:user_id``.
``v2`` is the routing-capable destination role: scoped ``bedrock:InvokeModel*``
and no single-user pin. They are separate templates on purpose — v1's pin is a
security property of its purpose, not a bug to widen away. The version selects
both the S3 key and the CFN parameter set, because v2 does not declare
``UserSessionTag`` and CloudFormation rejects a parameter a template does not
declare.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode

import boto3
from botocore.config import Config

_DEFAULT_PRESIGNED_TTL_SECONDS = 10 * 60
_DEFAULT_TEMPLATE_KEY = "cfn-templates/aws_role_v1.yaml"
_DEFAULT_TEMPLATE_KEY_V2 = "cfn-templates/aws_role_v2.yaml"

#: Template version that grants Bedrock invoke and is assumable for any platform
#: principal. Anything else is treated as the legacy read-only v1 shape.
ROUTING_TEMPLATE_VERSION = "v2"


def _template_bucket() -> str:
    """Bucket where the CFN template YAML is stored. Required at runtime."""
    return os.environ.get("ADP_CFN_TEMPLATE_BUCKET", "")


def _template_key(template_version: str = "v1") -> str:
    """S3 key of the template for ``template_version``.

    Each version keeps its own env override so an operator can pin one without
    silently repointing the other.
    """
    if template_version == ROUTING_TEMPLATE_VERSION:
        return os.environ.get("ADP_CFN_TEMPLATE_KEY_V2", _DEFAULT_TEMPLATE_KEY_V2)
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


def get_gateway_account_id() -> str:
    """Return the AWS account ID that hosts the ADP platform (gateway role's home).

    Extracted from ADP_GATEWAY_ROLE_ARN (format: arn:aws:iam::ACCOUNT:role/NAME).
    Falls back to ADP_GATEWAY_ACCOUNT_ID env var or "000000000000" for local dev.
    """
    explicit = os.environ.get("ADP_GATEWAY_ACCOUNT_ID", "")
    if explicit:
        return explicit
    # Parse from role ARN: arn:aws:iam::ACCOUNT_ID:role/...
    role_arn = get_gateway_role_arn()
    parts = role_arn.split(":")
    if len(parts) >= 5 and parts[4]:
        return parts[4]
    return "000000000000"


def build_template_url(credential_id: str, template_version: str = "v1") -> str:  # credential_id kept for call-site compat / logging
    """Generate a pre-signed S3 URL the AWS Console can fetch the template from.

    CloudFormation accepts S3 pre-signed URLs as ``templateURL`` (but rejects
    non-S3 hosts). The bucket stays private; the signature authorizes the
    fetch. TTL is short (10 min) so the URL can't be indexed or replayed.
    """
    bucket = _template_bucket()
    if not bucket:
        raise RuntimeError("ADP_CFN_TEMPLATE_BUCKET is not set — cannot build CFN templateURL")

    # Virtual-hosted addressing keeps the host on *.s3.amazonaws.com, which is
    # what CloudFormation's URL validator accepts.
    s3 = boto3.client(
        "s3",
        region_name=_region(),
        config=Config(signature_version="s3v4"),
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": _template_key(template_version)},
        ExpiresIn=_DEFAULT_PRESIGNED_TTL_SECONDS,
    )


def read_role_template(template_version: str = "v1") -> str:
    """Download the exact configured object Quick Create would launch.

    Honor the deployed template and its key override, rather than distributing a
    different policy from the copy packaged in the gateway image. ``v1`` is the
    personal read-only role, ``v2`` the routing destination role — the same
    version selector :func:`build_launch_url` takes, so a downloaded package and
    a console launch cannot diverge.
    """
    bucket = _template_bucket()
    if not bucket:
        raise RuntimeError("ADP_CFN_TEMPLATE_BUCKET is not set")
    s3 = boto3.client("s3", region_name=_region(), config=Config(signature_version="s3v4"))
    response = s3.get_object(Bucket=bucket, Key=_template_key(template_version))
    with response["Body"] as body:
        return body.read().decode("utf-8")


def read_routing_template() -> str:
    """The routing (v2) template. Kept as the name #4745's callers already import."""
    return read_role_template(ROUTING_TEMPLATE_VERSION)


def build_launch_url(
    *,
    credential_id: str,
    nickname: str,
    external_id: str,
    account_id: str,
    user_id: str,
    role_name: str = "ADP-Agent-Role",
    region: str = "us-east-1",
    template_version: str = "v1",
) -> str:
    """Build a CloudFormation Quick-Create URL pointing at the pre-signed template.

    ``template_version`` selects which role template the user launches — ``"v1"``
    (default, read-only + single-user pin) or ``"v2"`` (routing-capable). It
    defaults to v1 so the personal connect flow is unchanged.
    """
    gateway_role_arn = get_gateway_role_arn()
    gateway_account_id = get_gateway_account_id()

    # Sanitize nickname for stack name (alphanumeric + hyphens only)
    stack_nickname = "".join(c if c.isalnum() or c == "-" else "-" for c in nickname)

    template_url = build_template_url(credential_id, template_version)

    params = {
        "stackName": f"ADP-Agent-{stack_nickname}",
        "templateURL": template_url,
        "param_Nickname": nickname,
        "param_ExternalId": external_id,
        "param_GatewayRolePrincipal": gateway_role_arn,
        "param_GatewayAccountId": gateway_account_id,
    }
    # v2 has no UserSessionTag parameter (that pin is exactly what it drops), and
    # CloudFormation errors on a parameter the template does not declare — so the
    # key must be omitted, not blanked.
    if template_version != ROUTING_TEMPLATE_VERSION:
        params["param_UserSessionTag"] = user_id

    base_url = f"https://{region}.console.aws.amazon.com/cloudformation/home"
    query_string = urlencode(params, quote_via=quote)
    return f"{base_url}?region={region}#/stacks/quickcreate?{query_string}"


def compute_role_arn(account_id: str, nickname: str) -> str:
    """Compute the expected role ARN from account ID and nickname."""
    return f"arn:aws:iam::{account_id}:role/ADP-Agent-{nickname}"

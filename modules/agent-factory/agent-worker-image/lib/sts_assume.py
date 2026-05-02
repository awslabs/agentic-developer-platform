"""STS role assumption with session tags for customer AWS access.

Assumes a customer-owned IAM role using credentials stored in vault,
passing session tags for full CloudTrail audit traceability.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def assume_customer_role(
    role_arn: str,
    external_id: str,
    *,
    tenant_id: str,
    actor_login: str,
    actor_id: str,
    run_id: str,
    repo: str,
    issue: int,
    persona: str,
    duration_seconds: int = 3600,
    region: str = "us-east-1",
) -> dict[str, str]:
    """Assume a customer AWS role via STS with session tags.

    Args:
        role_arn: ARN of the customer IAM role.
        external_id: Per-tenant external ID for trust verification.
        tenant_id: Internal tenant identifier.
        actor_login: GitHub login of the triggering user.
        actor_id: GitHub user ID of the triggering user.
        run_id: Unique identifier for this agent run.
        repo: Full repo name (owner/name).
        issue: Issue number.
        persona: Agent persona name.
        duration_seconds: STS session duration (default 1 hour).
        region: AWS region.

    Returns:
        Dict with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN.
    """
    sts = boto3.client("sts", region_name=region)

    logger.info("Assuming role %s for tenant %s", role_arn, tenant_id)
    resp = sts.assume_role(
        RoleArn=role_arn,
        ExternalId=external_id,
        RoleSessionName=f"adp-agent-{run_id}"[:64],
        DurationSeconds=duration_seconds,
        Tags=[
            {"Key": "adp:tenant_id", "Value": tenant_id},
            {"Key": "adp:actor_github_login", "Value": actor_login},
            {"Key": "adp:actor_github_id", "Value": str(actor_id)},
            {"Key": "adp:run_id", "Value": run_id},
            {"Key": "adp:github_issue", "Value": f"{repo}#{issue}"},
            {"Key": "adp:persona", "Value": persona},
        ],
    )

    creds: dict[str, Any] = resp["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
    }

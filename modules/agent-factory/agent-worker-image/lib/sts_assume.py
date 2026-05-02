"""STS helper for assuming customer AWS roles in agent runtime.

Reads role_arn + external_id from vault at tenants/<tenant_id>/aws-access,
calls sts:AssumeRole with session tags for full CloudTrail audit traceability,
and exports temporary credentials into the agent environment.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Vault path pattern for customer AWS access credentials
VAULT_PATH_TEMPLATE = "tenants/{tenant_id}/aws-access"

# Session tag keys for CloudTrail audit
SESSION_TAGS = [
    "adp:tenant_id",
    "adp:agent",
    "adp:run_id",
    "adp:github_issue",
    "adp:actor",
]


@dataclass(frozen=True)
class AssumedCredentials:
    """Temporary AWS credentials from STS AssumeRole."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str
    role_arn: str
    region: str


class VaultClient:
    """Reads tenant secrets from AWS Secrets Manager vault.

    In production, this reads from the vault service at
    tenants/<tenant_id>/aws-access. Currently backed by Secrets Manager;
    the interface is stable regardless of backend.
    """

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session()
        self._client = self._session.client("secretsmanager")

    def get_aws_access(self, tenant_id: str) -> dict[str, Any]:
        """Retrieve AWS access configuration for a tenant.

        Returns:
            Dict with keys: role_arn, external_id, default_region,
            session_duration_seconds, permission_tier, allowed_regions.

        Raises:
            VaultError: If the secret cannot be retrieved.
        """
        secret_id = VAULT_PATH_TEMPLATE.format(tenant_id=tenant_id)
        try:
            response = self._client.get_secret_value(SecretId=secret_id)
            return json.loads(response["SecretString"])
        except ClientError as e:
            raise VaultError(
                f"Failed to retrieve AWS access for tenant {tenant_id}: {e}"
            ) from e


class VaultError(Exception):
    """Raised when vault operations fail."""


def assume_customer_role(
    tenant_id: str,
    agent: str,
    run_id: str,
    github_issue: str,
    actor: str,
    vault: VaultClient | None = None,
    sts_client: Any | None = None,
) -> AssumedCredentials:
    """Assume the customer's IAM role with session tags.

    Args:
        tenant_id: The tenant identifier (maps to vault path).
        agent: Agent persona name (e.g. 'developer', 'ops').
        run_id: Unique identifier for this agent run.
        github_issue: Issue reference (e.g. 'org/repo#123').
        actor: GitHub login of the user who triggered the run.
        vault: Optional VaultClient instance (for testing).
        sts_client: Optional STS client (for testing).

    Returns:
        AssumedCredentials with temporary AWS credentials.

    Raises:
        VaultError: If vault lookup fails.
        STSAssumeRoleError: If STS AssumeRole call fails.
    """
    if vault is None:
        vault = VaultClient()

    aws_access = vault.get_aws_access(tenant_id)

    role_arn = aws_access["role_arn"]
    external_id = aws_access["external_id"]
    region = aws_access.get("default_region", "us-east-1")
    duration = aws_access.get("session_duration_seconds", 3600)

    if sts_client is None:
        session = boto3.Session(region_name=region)
        sts_client = session.client("sts")

    session_name = f"adp-agent-{run_id}"
    # Session names are limited to 64 chars
    if len(session_name) > 64:
        session_name = session_name[:64]

    tags = [
        {"Key": "adp:tenant_id", "Value": tenant_id},
        {"Key": "adp:agent", "Value": agent},
        {"Key": "adp:run_id", "Value": run_id},
        {"Key": "adp:github_issue", "Value": github_issue},
        {"Key": "adp:actor", "Value": actor},
    ]

    try:
        response = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=duration,
            Tags=tags,
        )
    except ClientError as e:
        raise STSAssumeRoleError(
            f"Failed to assume role {role_arn} for tenant {tenant_id}: {e}"
        ) from e

    credentials = response["Credentials"]

    return AssumedCredentials(
        access_key_id=credentials["AccessKeyId"],
        secret_access_key=credentials["SecretAccessKey"],
        session_token=credentials["SessionToken"],
        expiration=credentials["Expiration"].isoformat(),
        role_arn=role_arn,
        region=region,
    )


class STSAssumeRoleError(Exception):
    """Raised when STS AssumeRole fails."""


def export_credentials_to_env(creds: AssumedCredentials) -> dict[str, str]:
    """Export assumed credentials into the current process environment.

    Sets AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN,
    and AWS_DEFAULT_REGION in os.environ.

    Returns:
        Dict of environment variable names to values that were set.
    """
    env_vars = {
        "AWS_ACCESS_KEY_ID": creds.access_key_id,
        "AWS_SECRET_ACCESS_KEY": creds.secret_access_key,
        "AWS_SESSION_TOKEN": creds.session_token,
        "AWS_DEFAULT_REGION": creds.region,
    }
    for key, value in env_vars.items():
        os.environ[key] = value

    logger.info(
        "Exported customer AWS credentials (role=%s, region=%s, expires=%s)",
        creds.role_arn,
        creds.region,
        creds.expiration,
    )
    return env_vars


def assume_and_export(
    tenant_id: str,
    agent: str,
    run_id: str,
    github_issue: str,
    actor: str,
) -> AssumedCredentials:
    """Convenience function: assume role and export credentials to env.

    This is the primary entry point for agent runtime code.

    Args:
        tenant_id: The tenant identifier.
        agent: Agent persona name.
        run_id: Unique run identifier.
        github_issue: Issue reference (e.g. 'org/repo#123').
        actor: GitHub login of the triggering user.

    Returns:
        AssumedCredentials (also exported to os.environ).
    """
    creds = assume_customer_role(
        tenant_id=tenant_id,
        agent=agent,
        run_id=run_id,
        github_issue=github_issue,
        actor=actor,
    )
    export_credentials_to_env(creds)
    return creds

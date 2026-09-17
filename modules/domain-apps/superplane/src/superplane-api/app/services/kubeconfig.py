"""Kubeconfig generation — assumes cross-account role via STS and builds a scoped kubeconfig."""

import logging
from datetime import datetime, timedelta, timezone

import boto3
import yaml
from botocore.exceptions import ClientError, NoCredentialsError

from app.config import settings

logger = logging.getLogger(__name__)

# Short-lived token duration (15 minutes — STS minimum)
TOKEN_DURATION_SECONDS = 900


def _assume_workspace_role(workspace_aws_account_id: str, workspace_name: str) -> dict:
    """Assume the cross-account IAM role in the workspace's AWS account.

    Role ARN pattern: arn:aws:iam::{account_id}:role/superplane-workspace-{name}
    """
    sts_client = boto3.client("sts", region_name=settings.aws_region)
    role_arn = f"arn:aws:iam::{workspace_aws_account_id}:role/superplane-workspace-{workspace_name}"

    try:
        response = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"superplane-kubeconfig-{workspace_name}",
            DurationSeconds=TOKEN_DURATION_SECONDS,
        )
        return response["Credentials"]
    except NoCredentialsError as exc:
        logger.error(
            "AWS credentials not configured for kubeconfig generation: %s", exc
        )
        raise RuntimeError(
            "AWS credentials not configured. Ensure the API server has IAM role access (IRSA)."
        ) from exc
    except ClientError as exc:
        logger.error("Failed to assume role %s: %s", role_arn, exc)
        raise


def generate_kubeconfig(
    cluster_endpoint: str,
    cluster_ca_cert: str,
    cluster_name: str,
    workspace_aws_account_id: str,
    workspace_name: str,
) -> tuple[str, datetime]:
    """Generate a scoped kubeconfig with a short-lived STS token.

    Args:
        cluster_endpoint: EKS cluster API server endpoint.
        cluster_ca_cert: Base64-encoded CA certificate.
        cluster_name: Name of the EKS cluster.
        workspace_aws_account_id: AWS account ID where the workspace cluster lives.
        workspace_name: Workspace name (used for role naming).

    Returns:
        Tuple of (kubeconfig_yaml_string, expiry_datetime).
    """
    credentials = _assume_workspace_role(workspace_aws_account_id, workspace_name)

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": {
                    "server": cluster_endpoint,
                    "certificate-authority-data": cluster_ca_cert,
                },
                "name": cluster_name,
            }
        ],
        "contexts": [
            {
                "context": {
                    "cluster": cluster_name,
                    "user": f"superplane-{workspace_name}",
                },
                "name": f"superplane-{workspace_name}",
            }
        ],
        "current-context": f"superplane-{workspace_name}",
        "users": [
            {
                "name": f"superplane-{workspace_name}",
                "user": {
                    "token": credentials["SessionToken"],
                },
            }
        ],
    }

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_DURATION_SECONDS)
    return yaml.dump(kubeconfig, default_flow_style=False), expires_at

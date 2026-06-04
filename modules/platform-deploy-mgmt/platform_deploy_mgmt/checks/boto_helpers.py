"""Cross-account boto3 session helpers.

Critical contract:
- customer_session is ONLY for read-only AWS API calls against the customer account.
- platform_session is for writing evidence (S3) and deployment status (DDB).

Never pass customer_session to any write operation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import boto3
from botocore.session import Session as BotocoreSession


@dataclass
class Context:
    """Execution context holding both sessions and deployment metadata."""

    customer_account_id: str
    region: str
    environment: str
    customer_session: boto3.Session
    platform_session: boto3.Session
    deploy_id: str = ""
    mode: str = "deploy"
    workflow_run_id: str = ""
    workflow_run_url: str = ""

    @property
    def state_bucket_name(self) -> str:
        """Expected Terraform state bucket name for the customer account."""
        return f"adp-terraform-state-{self.customer_account_id}"

    @property
    def evidence_bucket_name(self) -> str:
        """S3 bucket for storing verification evidence (platform account)."""
        return "adp-platform-deploy-evidence"

    @property
    def deployments_table_name(self) -> str:
        """DynamoDB table tracking deployment status (platform account)."""
        return "adp-platform-deployments"


def build_context_from_env() -> Context:
    """Build a Context from environment variables set by load-deploy-config action.

    The customer session uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
    (set by the cross-account assume in the action).

    The platform session uses the runner's default credentials (IRSA).
    """
    customer_account_id = os.environ["CUSTOMER_ACCOUNT_ID"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    environment = os.environ.get("ENVIRONMENT", "dev")

    # Customer session: uses the assumed-role credentials from load-deploy-config
    customer_session = boto3.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
        region_name=region,
    )

    # Platform session: uses the runner's own IRSA credentials (default chain)
    # We explicitly create a session WITHOUT the assumed-role creds
    platform_session = _build_platform_session(region)

    return Context(
        customer_account_id=customer_account_id,
        region=region,
        environment=environment,
        customer_session=customer_session,
        platform_session=platform_session,
        deploy_id=os.environ.get("DEPLOY_ID", ""),
        mode=os.environ.get("MODE", "deploy"),
        workflow_run_id=os.environ.get("GITHUB_RUN_ID", ""),
        workflow_run_url=_build_workflow_url(),
    )


def _build_platform_session(region: str) -> boto3.Session:
    """Build a boto3 session using the runner's own credentials (not assumed role).

    The ARC runner has IRSA which provides credentials via the EKS pod identity
    webhook (AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN). We need a session that
    uses those, not the cross-account assumed creds in AWS_ACCESS_KEY_ID.
    """
    # Create a fresh botocore session that ignores the explicit env creds
    botocore_session = BotocoreSession()
    # Remove the explicit env credential provider from the chain so it falls
    # through to the container/IRSA provider
    botocore_session.set_config_variable("aws_access_key_id", None)
    botocore_session.set_config_variable("aws_secret_access_key", None)
    botocore_session.set_config_variable("aws_session_token", None)

    # If IRSA env vars are available, use them directly
    web_identity_token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
    role_arn = os.environ.get("AWS_ROLE_ARN")

    if web_identity_token_file and role_arn:
        # Use STS to assume role via web identity (IRSA path)
        sts = boto3.client(
            "sts",
            region_name=region,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            aws_session_token=None,
        )
        # Read the web identity token
        with open(web_identity_token_file) as f:
            token = f.read()
        resp = sts.assume_role_with_web_identity(
            RoleArn=role_arn,
            RoleSessionName="platform-deploy-mgmt-verify",
            WebIdentityToken=token,
        )
        creds = resp["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )

    # Fallback: if running locally or in an environment without IRSA,
    # use a profile-based session (useful for local testing)
    platform_profile = os.environ.get("ADP_PLATFORM_PROFILE")
    if platform_profile:
        return boto3.Session(profile_name=platform_profile, region_name=region)

    # Last resort: return a default session (works when runner IS the platform account)
    return boto3.Session(region_name=region)


def _build_workflow_url() -> str:
    """Build the GitHub Actions run URL from environment."""
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repository and run_id:
        return f"{server_url}/{repository}/actions/runs/{run_id}"
    return ""

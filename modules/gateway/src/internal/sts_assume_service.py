"""STS AssumeRole service — performs server-side assume-role with session tagging.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.

Responsibilities:
  - Parse the aws_role credential JSON (role_arn, external_id, session_duration, default_region)
  - Construct and execute an STS AssumeRole call with session tags
  - Return temporary credentials to the caller
  - Never log secret_access_key or session_token
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Maximum session name length per AWS API contract.
_MAX_SESSION_NAME_LEN = 64


@dataclass(frozen=True)
class AssumeRoleResult:
    """Temporary credentials returned by STS AssumeRole."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str  # ISO 8601
    region: str
    profile_name: str


class STSAssumeError(Exception):
    """Raised when STS AssumeRole fails."""

    def __init__(self, message: str, code: str = "sts_error") -> None:
        super().__init__(message)
        self.code = code


def assume_role(
    *,
    role_arn: str,
    external_id: str | None,
    session_duration_seconds: int,
    default_region: str,
    user_id: str,
    agent_id: str,
    task_id: str,
    label: str,
    aws_region: str = "us-east-1",
) -> AssumeRoleResult:
    """Execute STS AssumeRole with session tagging.

    Parameters
    ----------
    role_arn : str
        The ARN of the role to assume.
    external_id : str | None
        Optional ExternalId for confused-deputy protection.
    session_duration_seconds : int
        Requested session duration (capped by role's max).
    default_region : str
        Region hint for the caller.
    user_id, agent_id, task_id : str
        Identity context — used for session name + tags.
    label : str
        Credential label, used in the profile name.
    aws_region : str
        Region for the STS client itself.

    Returns
    -------
    AssumeRoleResult

    Raises
    ------
    STSAssumeError
        On any STS failure.
    """
    # Build session name: adp-{agent_id}-{task_id_short} (≤64 chars)
    task_short = task_id[:8] if len(task_id) > 8 else task_id
    session_name = f"adp-{agent_id}-{task_short}"
    if len(session_name) > _MAX_SESSION_NAME_LEN:
        session_name = session_name[:_MAX_SESSION_NAME_LEN]

    # Session tags for CloudTrail attribution in the customer account.
    tags: list[dict[str, str]] = [
        {"Key": "adp:user_id", "Value": user_id},
        {"Key": "adp:agent_id", "Value": agent_id},
        {"Key": "adp:task_id", "Value": task_id},
        {"Key": "adp:persona", "Value": agent_id},
    ]

    kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": session_duration_seconds,
        "Tags": tags,
    }
    if external_id:
        kwargs["ExternalId"] = external_id

    try:
        sts = boto3.client("sts", region_name=aws_region)
        response = sts.assume_role(**kwargs)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        logger.error(
            "STS AssumeRole failed role_arn=%s error_code=%s",
            role_arn,
            error_code,
        )
        raise STSAssumeError(
            f"STS AssumeRole failed: {error_code} — {error_msg}",
            code=error_code,
        ) from exc

    creds = response["Credentials"]
    expiration = creds["Expiration"]
    # Expiration is a datetime object from boto3; convert to ISO string.
    expiration_str = expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration)

    profile_name = f"adp-aws-{label}" if label else "adp-aws-default"

    logger.info(
        "STS AssumeRole succeeded role_arn=%s session_name=%s expires=%s",
        role_arn,
        session_name,
        expiration_str,
    )

    return AssumeRoleResult(
        access_key_id=creds["AccessKeyId"],
        secret_access_key=creds["SecretAccessKey"],
        session_token=creds["SessionToken"],
        expiration=expiration_str,
        region=default_region,
        profile_name=profile_name,
    )

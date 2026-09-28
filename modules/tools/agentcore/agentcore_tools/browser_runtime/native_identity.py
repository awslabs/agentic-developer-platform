"""Select platform identity only for the fixed native Browser subprocess."""

from __future__ import annotations

import os


def browser_environment(source):
    """Keep task SDK credentials unchanged in the caller and fail closed on IRSA loss.

    Mirrors the worker's platform subprocess identity selection. The AgentCore
    SDK creates separate boto3 sessions for lifecycle calls and CDP signing, so
    both must see the same identity inside this dedicated browser process.
    """
    env = dict(source)
    env.pop("NODE_OPTIONS", None)
    preserved = any(
        key in env for key in ("ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE")
    )
    if (
        not preserved
        and env.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() != "true"
    ):
        return env
    role = env.get("ADP_WORKER_IRSA_ROLE_ARN" if preserved else "AWS_ROLE_ARN")
    token = env.get(
        "ADP_WORKER_IRSA_TOKEN_FILE" if preserved else "AWS_WEB_IDENTITY_TOKEN_FILE"
    )
    name = env.get(
        "ADP_WORKER_IRSA_SESSION_NAME" if preserved else "AWS_ROLE_SESSION_NAME"
    )
    if not role or not token:
        raise RuntimeError("Platform Browser IRSA identity unavailable")
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ROLE_SESSION_NAME",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    ):
        env.pop(key, None)
    env.update(
        AWS_ROLE_ARN=role,
        AWS_WEB_IDENTITY_TOKEN_FILE=token,
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_REGION=env.get("ADP_WORKER_AWS_REGION")
        or env.get("AWS_REGION")
        or "us-east-1",
    )
    env["AWS_DEFAULT_REGION"] = env["AWS_REGION"]
    if name:
        env["AWS_ROLE_SESSION_NAME"] = name
    return env


def cleanup_client(config):
    """Use the existing refreshable platform provider for emergency Browser stop.

    No credentials or AWS environment changes are returned to the task shell.
    """
    import boto3
    import botocore.session

    protected = (
        any(
            key in os.environ
            for key in ("ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE")
        )
        or os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
    )
    region = os.environ.get("ADP_WORKER_AWS_REGION") or os.environ.get(
        "AWS_REGION", "us-east-1"
    )
    if not protected:
        return boto3.client("bedrock-agentcore", region_name=region, config=config)
    from adp_trigger.transport_identity import worker_credentials

    session = botocore.session.get_session()
    session._credentials = worker_credentials(session)
    return boto3.Session(botocore_session=session).client(
        "bedrock-agentcore", region_name=region, config=config
    )

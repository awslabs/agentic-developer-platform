"""Keep platform control traffic on IRSA after customer AWS credentials load."""

import logging
import os
import re
from urllib.parse import urlparse


# Botocore DEBUG canonical-request logs contain signed run/pod headers and STS
# tokens. Suppress signing debug even when an operator enables root debug logs;
# INFO and higher SDK diagnostics remain available. Applies to all transports.
def _omit_signing_debug(record):
    return record.levelno >= logging.INFO


logging.getLogger("botocore.auth").addFilter(_omit_signing_debug)


def preserve_worker_identity(env):
    """Retain refreshable platform identity before task tools replace AWS_*.

    These values select platform transport only; AWS SDKs used for deployments
    continue to consume the customer's ordinary environment/profile. Preserve
    the first identity across nested ``adp-cred assume --exec`` calls.
    """
    if not any(key in env for key in ("ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE")):
        if env.get("AWS_ROLE_ARN") and env.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
            env["ADP_WORKER_IRSA_ROLE_ARN"] = env["AWS_ROLE_ARN"]
            env["ADP_WORKER_IRSA_TOKEN_FILE"] = env["AWS_WEB_IDENTITY_TOKEN_FILE"]
            if env.get("AWS_ROLE_SESSION_NAME"):
                env["ADP_WORKER_IRSA_SESSION_NAME"] = env["AWS_ROLE_SESSION_NAME"]
    env.setdefault(
        "ADP_WORKER_AWS_REGION",
        env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1",
    )


def gateway_signing_region(url):
    """Sign for the gateway's region, independently of the deployment region."""
    match = re.fullmatch(
        r"[a-z0-9-]+\.execute-api(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
        urlparse(url).hostname or "",
    )
    if match:
        return match.group(1)
    return os.environ.get("ADP_WORKER_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"


def worker_credentials(session):
    # Operations tasks replace AWS_ACCESS_KEY_ID et al. with customer creds.
    # Those belong to task tools, not the platform authority transport. Use the
    # refreshable web-identity provider directly, without mutating process env.
    preserved = any(
        key in os.environ for key in ("ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE")
    )
    if preserved or os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() == "true":
        from botocore.credentials import AssumeRoleWithWebIdentityProvider

        role = os.environ.get("ADP_WORKER_IRSA_ROLE_ARN" if preserved else "AWS_ROLE_ARN")
        token = os.environ.get(
            "ADP_WORKER_IRSA_TOKEN_FILE" if preserved else "AWS_WEB_IDENTITY_TOKEN_FILE"
        )
        if not role or not token:
            raise RuntimeError("Platform worker IRSA identity unavailable")
        config = {"role_arn": role, "web_identity_token_file": token}
        name = os.environ.get(
            "ADP_WORKER_IRSA_SESSION_NAME" if preserved else "AWS_ROLE_SESSION_NAME"
        )
        if name:
            config["role_session_name"] = name
        # An explicit private profile avoids both customer AWS_PROFILE and
        # replacement role variables, while retaining SDK credential refresh.
        return AssumeRoleWithWebIdentityProvider(
            load_config=lambda: {"profiles": {"adp-worker": config}},
            client_creator=session.create_client,
            profile_name="adp-worker",
            disable_env_vars=True,
        ).load()
    return session.get_credentials()

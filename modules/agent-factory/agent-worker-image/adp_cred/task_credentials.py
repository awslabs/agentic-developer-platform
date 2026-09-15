"""SDK credential_process for direct customer STS calls using the existing role."""

import atexit
import configparser
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from adp_cred.client import _do_request, _get_config
from adp_trigger.transport_identity import preserve_worker_identity


def configure_task_credentials(env):
    """Give task SDKs a refreshable source; platform clients retain their IRSA.

    Explicit customer keys and profiles still take precedence. The original AWS
    config is copied so preconfigured role chains remain available.
    """
    if env.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        return
    preserve_worker_identity(env)
    config = configparser.RawConfigParser()
    config.read(env.get("AWS_CONFIG_FILE") or os.path.expanduser("~/.aws/config"))
    if "default" not in config:
        config["default"] = {}
    # Preserve explicit customer profiles, including a customer default. Only an
    # otherwise unconfigured default receives the worker source process. Shared
    # credential-file keys retain normal SDK precedence, just like environment keys.
    authentication = (
        "role_arn",
        "web_identity_token_file",
        "credential_source",
        "aws_access_key_id",
        "credential_process",
        "sso_session",
        "sso_start_url",
    )
    if not any(config["default"].get(key) for key in authentication):
        config["default"]["credential_process"] = "adp-cred worker-session"
    descriptor, path = tempfile.mkstemp(prefix="adp-task-aws-", suffix=".config")
    with os.fdopen(descriptor, "w") as stream:
        config.write(stream)
    atexit.register(Path(path).unlink, missing_ok=True)
    env["AWS_CONFIG_FILE"] = path
    for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        env.pop(key, None)
    return path


def cmd_task_credentials():
    """Only the invoking AWS SDK consumes stdout; never log credential values."""
    try:
        if os.environ.get("ADP_AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
            raise ValueError("worker identity unavailable")
        base, api_key, user_id, _, _, use_sigv4 = _get_config()
        invocation = os.environ.get("ADP_MESSAGE_ID")
        if not invocation:
            raise ValueError("worker invocation unavailable")
        result = _do_request(
            "POST",
            f"{base}/internal/v1/worker-task-credentials",
            api_key,
            use_sigv4,
            {"user_id": user_id, "invocation_id": invocation},
        )
        fields = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
        if (
            not isinstance(result, dict)
            or result.get("Version") != 1
            or not all(isinstance(result.get(key), str) and result[key] for key in fields)
        ):
            raise ValueError("task credentials unavailable")
        expiry = datetime.fromisoformat(result["Expiration"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError("task credentials expired")
    except Exception:
        print("error: customer task credentials unavailable", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"Version": 1, **{key: result[key] for key in fields}}))

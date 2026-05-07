"""adp-cred assume — STS AssumeRole via the vault gateway.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.

Usage:
    adp-cred assume --service aws --label prod --purpose "deploy to prod"

Writes temporary credentials to ~/.aws/credentials as a named profile.
Prints only the profile name to stdout (for script consumption).
NEVER prints secret_access_key or session_token to stdout/stderr.
"""

from __future__ import annotations

import configparser
import os
import sys

from adp_cred.client import _check_enabled, _get_config, _request


def cmd_assume(args: list[str]) -> None:
    """Assume an AWS role stored in the user's vault.

    Writes temp creds to ~/.aws/credentials under the returned profile name.
    Prints the profile name to stdout.
    """
    service: str = "aws"
    label: str | None = None
    purpose: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--service" and i + 1 < len(args):
            service = args[i + 1]
            i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        elif args[i] == "--purpose" and i + 1 < len(args):
            purpose = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            print(
                "Usage: adp-cred assume [--service SERVICE] [--label LABEL] [--purpose PURPOSE]",
                file=sys.stderr,
            )
            sys.exit(2)

    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id = _get_config()

    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
        "purpose": purpose,
    }
    endpoint = f"{base_url}/internal/v1/credential-assume-role"
    result = _request("POST", endpoint, api_key, payload)

    # Write to ~/.aws/credentials.
    profile_name = result["profile_name"]
    _write_aws_credentials(
        profile_name=profile_name,
        access_key_id=result["access_key_id"],
        secret_access_key=result["secret_access_key"],
        session_token=result["session_token"],
        region=result.get("region", ""),
    )

    # Print ONLY the profile name to stdout — never credentials.
    print(profile_name, end="")


def _write_aws_credentials(
    *,
    profile_name: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str,
    region: str,
) -> None:
    """Write or update an AWS credentials profile.

    Does NOT overwrite existing profiles that aren't ours.
    """
    aws_dir = os.path.expanduser("~/.aws")
    os.makedirs(aws_dir, exist_ok=True)

    creds_path = os.path.join(aws_dir, "credentials")
    config = configparser.ConfigParser()
    if os.path.exists(creds_path):
        config.read(creds_path)

    config[profile_name] = {
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
        "aws_session_token": session_token,
    }
    if region:
        config[profile_name]["region"] = region

    with open(creds_path, "w") as f:
        config.write(f)

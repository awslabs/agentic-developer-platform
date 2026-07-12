"""adp-cred assume — STS AssumeRole via the vault gateway.

Issue #481: aws_role credential type + STS assume_role as a vault delivery path.
Issue #583: --exec flag to run commands with assumed-role creds in env (defeats IRSA precedence).

Usage:
    adp-cred assume --service aws --label prod --purpose "deploy to prod"
    adp-cred assume --service aws --label prod --exec aws sts get-caller-identity

Without --exec: writes temporary credentials to ~/.aws/credentials as a named profile,
prints only the profile name to stdout (legacy behavior).

With --exec: runs the specified command with assumed-role credentials injected as
environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN,
AWS_REGION, AWS_DEFAULT_REGION) and IRSA env vars removed. Scoped to that one command.
NEVER prints secret_access_key or session_token to stdout/stderr.
"""

from __future__ import annotations

import configparser
import os
import shutil
import sys

from adp_cred.client import _check_enabled, _do_request, _get_config


def cmd_assume(args: list[str]) -> None:
    """Assume an AWS role stored in the user's vault.

    Without --exec: writes temp creds to ~/.aws/credentials under the returned
    profile name, prints the profile name to stdout.

    With --exec: runs the given command with assumed-role creds in its env,
    stripping IRSA vars so boto3 uses the assumed credentials.
    """
    service: str = "aws"
    label: str | None = None
    purpose: str | None = None
    exec_args: list[str] | None = None

    i = 0
    while i < len(args):
        if args[i] == "--exec":
            # Everything after --exec is the command to run.
            exec_args = args[i + 1 :]
            break
        elif args[i] == "--service" and i + 1 < len(args):
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
                "Usage: adp-cred assume [--service SERVICE] [--label LABEL] "
                "[--purpose PURPOSE] [--exec CMD [ARGS...]]",
                file=sys.stderr,
            )
            sys.exit(2)

    # Validate --exec has a command.
    if exec_args is not None and len(exec_args) == 0:
        print(
            "error: --exec requires a command. "
            "Usage: adp-cred assume [--service S] [--label L] --exec <cmd> [args...]",
            file=sys.stderr,
        )
        sys.exit(2)

    _check_enabled()
    base_url, api_key, user_id, agent_id, task_id, use_sigv4 = _get_config()

    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "service": service,
        "label": label,
        "purpose": purpose,
    }
    invocation_id = os.environ.get("ADP_MESSAGE_ID")
    if invocation_id:
        payload["invocation_id"] = invocation_id
    endpoint = f"{base_url}/internal/v1/credential-assume-role"
    result = _do_request("POST", endpoint, api_key, use_sigv4, payload)

    # Write to ~/.aws/credentials (backward compat — always done).
    profile_name = result["profile_name"]
    _write_aws_credentials(
        profile_name=profile_name,
        access_key_id=result["access_key_id"],
        secret_access_key=result["secret_access_key"],
        session_token=result["session_token"],
        region=result.get("region", ""),
    )

    if exec_args is not None:
        # Build a scoped env with assumed-role creds; remove IRSA vars.
        env = os.environ.copy()

        # Inject assumed-role credentials (boto3 chain priority #2 — env vars).
        env["AWS_ACCESS_KEY_ID"] = result["access_key_id"]
        env["AWS_SECRET_ACCESS_KEY"] = result["secret_access_key"]
        env["AWS_SESSION_TOKEN"] = result["session_token"]

        # Region: prefer the response's region, fall back to existing env.
        region = result.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region

        # Remove anything that could route boto3 elsewhere.
        for var in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE"):
            env.pop(var, None)

        # Replace this process with the user's command.
        exec_cmd = exec_args[0]
        exec_path = shutil.which(exec_cmd) or exec_cmd
        # nosemgrep: tmp.gitlab.bandit.B606 — exec-ing the user's own command is the documented contract of `adp-cred assume --exec`; the prior trailing nosemgrep landed on the closing paren, not the call's start line (124), so it never applied
        os.execvpe(  # nosec: B606
            exec_path, exec_args, env
        )
    else:
        # Legacy behavior: print ONLY the profile name to stdout.
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

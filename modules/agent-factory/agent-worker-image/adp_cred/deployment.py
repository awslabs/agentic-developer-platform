"""adp-cred deployment: refreshable credentials for an engine-approved Actions job.

Only the AWS credential_process subcommand writes credentials to stdout (for the
AWS SDK pipe). Configure writes a private config with no secrets and checks the
server-owned source/inputs before the workflow checks out deployment code.
This module intentionally uses only the Python standard library on ARC runners.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

AUDIENCE = "adp-deployment-credentials"
TRANSPORT = {"adp_correlation", "adp_source_revision", "adp_definition_revision"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_json(url, *, token, body=None):
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with build_opener(NoRedirect).open(request, timeout=60) as response:
        value = response.read(65537)
        if len(value) > 65536:
            raise ValueError("oversized credential response")
        return json.loads(value)


def exchange(kind):
    gateway = os.environ["ADP_DEPLOYMENT_GATEWAY_URL"].rstrip("/")
    parsed = urlsplit(gateway)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("protected deployment gateway URL required")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    inputs = event["inputs"]
    body = {
        "correlation": inputs["adp_correlation"],
        "source_revision": inputs["adp_source_revision"],
        "inputs": {
            key: (
                str(value).capitalize()
                if key
                in {"legacy_security_update", "legacy_dry_run", "crew_controls_only", "skip_deploy"}
                and str(value).lower() in {"true", "false"}
                else str(value)
            )
            for key, value in inputs.items()
            if key not in TRANSPORT
        },
    }
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    if urlsplit(oidc_url).scheme != "https":
        raise ValueError("GitHub OIDC required")
    oidc_url += ("&" if "?" in oidc_url else "?") + urlencode({"audience": AUDIENCE})
    oidc = read_json(oidc_url, token=os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"])["value"]
    return read_json(
        gateway + "/orchestration/deployment-credentials/" + kind, token=oidc, body=body
    )


# Make the SDK use only the configured connection, even on an IRSA/EC2 runner.
CLEARED = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_DEFAULT_PROFILE",
)


def configure():
    context = exchange("context")
    target = context["target"]
    if not re.fullmatch(r"[0-9]{12}", target["account_id"]) or not re.fullmatch(
        r"[a-z0-9-]+", target["region"]
    ):
        raise ValueError("invalid deployment target")
    if (
        target["resource_kind"] != "eks-namespace"
        or target["resource_id"] != os.environ["EKS_CLUSTER"] + "/" + os.environ["NAMESPACE"]
    ):
        raise ValueError("workflow target differs from approved target")
    directory = Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-credentials"
    directory.mkdir(mode=0o700, exist_ok=True)
    script = directory / "deployment.py"
    script.write_bytes(Path(__file__).read_bytes())
    script.chmod(0o600)
    config = directory / "config"
    process = shlex.join([sys.executable, str(script), "credential-process"])
    config.write_text(
        f"[profile adp-deployment]\nregion = {target['region']}\ncredential_process = {process}\n"
    )
    config.chmod(0o600)
    empty = directory / "credentials"
    empty.write_text("")
    empty.chmod(0o600)
    environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
    updates = {
        "AWS_CONFIG_FILE": str(config),
        "AWS_SHARED_CREDENTIALS_FILE": str(empty),
        "AWS_PROFILE": "adp-deployment",
        "AWS_SDK_LOAD_CONFIG": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": target["region"],
        "AWS_DEFAULT_REGION": target["region"],
        "ADP_ACCOUNT_ID": target["account_id"],
        "ADP_REGION": target["region"],
    }
    environment.update(updates)
    identity = json.loads(
        subprocess.check_output(
            ["aws", "sts", "get-caller-identity", "--output", "json"], env=environment, timeout=90
        )
    )
    if identity.get("Account") != target["account_id"]:
        raise ValueError("connected role account differs from deployment target")
    with open(os.environ["GITHUB_ENV"], "a") as stream:
        for key in CLEARED:
            stream.write(f"{key}=\n")
        for key, value in updates.items():
            if "\n" in value or "\r" in value:
                raise ValueError("invalid environment value")
            stream.write(f"{key}={value}\n")
    print("Engine deployment source, target and connected AWS role verified.")


def main(args=None):
    args = sys.argv[1:] if args is None else args
    try:
        if args == ["configure"]:
            configure()
        elif args == ["credential-process"]:
            result = exchange("assume")
            print(
                json.dumps(
                    {
                        "Version": 1,
                        "AccessKeyId": result["access_key_id"],
                        "SecretAccessKey": result["secret_access_key"],
                        "SessionToken": result["session_token"],
                        "Expiration": result["expiration"],
                    }
                )
            )
        else:
            raise ValueError("invalid deployment command")
    except Exception:
        # HTTP exception bodies, subprocess output and credential values are private.
        print(
            "adp-cred: connected deployment credentials unavailable; verify the engine grant and selected connection.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

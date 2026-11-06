"""Materialize a protected setup manifest and sessions for the live runner."""

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from tests.e2e.tools.atlassian_setup_live import FIELDS, load_setup

LOCAL_FILES = {
    "ledger_file": "ledger.json",
    "admin_token_file": "admin.token",
    "owner_token_file": "owner.token",
    "observer_token_file": "observer.token",
    "consent_file": "consent.json",
    "authorization_file": "authorization.json",
    "attempt_file": "attempt.json",
}
SESSION_ENV = {
    "admin_token_file": "ADP_TOOLS_ATLASSIAN_ADMIN_SESSION",
    "owner_token_file": "ADP_TOOLS_ATLASSIAN_OWNER_SESSION",
    "observer_token_file": "ADP_TOOLS_ATLASSIAN_OBSERVER_SESSION",
}
OIDC_SCRIPT = Path(__file__).with_name("atlassian_setup_oidc.py")


def bind_setup(manifest, sessions, *, directory, gateway_url):
    config = json.loads(manifest)
    fields = FIELDS - LOCAL_FILES.keys()
    if type(config) is not dict or set(config) not in (
        fields,
        fields | {"policy_observation"},
    ):
        raise ValueError("Protected setup manifest must contain only target data")
    if set(sessions) != set(SESSION_ENV) or any(
        type(token) is not str
        or not token
        or len(token) > 16384
        or any(character.isspace() for character in token)
        for token in sessions.values()
    ):
        raise ValueError("Three independent human sessions are required")
    root = Path(tempfile.mkdtemp(prefix="atlassian-setup-", dir=directory))
    try:
        config.update({key: str(root / name) for key, name in LOCAL_FILES.items()})
        if "policy_observation" in config:
            policy = config["policy_observation"]
            if (
                type(policy) is not dict
                or set(policy)
                != {"tenant_id", "grant", "probes", "expected_results", "checks"}
                or type(policy["checks"]) is not dict
                or set(policy["checks"]) != {"read", "search"}
            ):
                raise ValueError(
                    "Protected policy fixtures must contain only target data"
                )
            for permission, check in policy["checks"].items():
                if type(check) is not dict or set(check) != {
                    "path",
                    "expected_result_sha256",
                }:
                    raise ValueError("Policy fixture cannot select a local file")
                check["ledger_file"] = str(root / ("policy-" + permission + ".json"))
            from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant

            grant = policy.pop("grant")
            IdentityReadbackGrant(grant, config["owner_id"])
            policy["grant_file"] = str(root / "policy-grant.json")
            with os.fdopen(
                os.open(
                    policy["grant_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                ),
                "w",
            ) as output:
                json.dump(grant, output)
        for key, token in sessions.items():
            with os.fdopen(
                os.open(config[key], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                output.write(token)
        path = root / "config.json"
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            json.dump(config, output)
        load_setup(path, gateway_url=gateway_url)
        return str(path)
    except Exception:
        shutil.rmtree(root)
        raise


def bind_oidc(config):
    if (
        config["account"] != os.environ["EXPECTED_ACCOUNT"]
        or config["region"] != os.environ["AWS_REGION"]
        or config["core_label"] != "oidc-core"
        or config["adapter_label"] != "oidc-adapter"
        or not re.fullmatch(r"[0-9]{12}", config["account"])
    ):
        raise ValueError("Observer roles and protected target must agree")
    environment = config["core_role_arn"].split(":role/", 1)[1].split("-", 2)[1]
    if environment not in {"dev", "test", "prod"}:
        raise ValueError("Observer environment unavailable")
    for role in ("core", "adapter"):
        expected = (
            f"arn:aws:iam::{config['account']}:role/adp-{environment}"
            f"-atlassian-mcp-observer-{role}"
        )
        if (
            config[f"{role}_role_arn"] != expected
            or os.environ[f"ADP_TOOLS_ATLASSIAN_{role.upper()}_OBSERVER_ROLE_ARN"]
            != expected
        ):
            raise ValueError("Observer roles do not match protected role pins")
    root = Path(config["ledger_file"]).parent
    script = OIDC_SCRIPT.resolve(strict=True)
    profiles = []
    for role in ("core", "adapter"):
        process = shlex.join(
            [sys.executable, str(script), config[f"{role}_role_arn"], environment]
        )
        profiles.append(
            f"[profile oidc-{role}]\nregion = {config['region']}\n"
            f"credential_process = {process}\n"
        )
    path = root / "aws-config"
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        output.write("\n".join(profiles))
    with os.fdopen(
        os.open(root / "aws-credentials", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
        "w",
    ):
        pass
    return str(path)


def main():
    from tests.e2e.tools.atlassian_setup_observer import _aws

    path = bind_setup(
        os.environ["ADP_TOOLS_ATLASSIAN_SETUP_JSON"],
        {key: os.environ[name] for key, name in SESSION_ENV.items()},
        directory=os.environ["RUNNER_TEMP"],
        gateway_url=os.environ["GATEWAY_URL"],
    )
    try:
        _, config = load_setup(path, gateway_url=os.environ["GATEWAY_URL"])
        aws_config = bind_oidc(config)
        previous = os.environ.get("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE")
        os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = aws_config
        try:
            for role in ("core", "adapter"):
                _aws(config, role, "sts", "get-caller-identity")
        finally:
            if previous is None:
                os.environ.pop("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", None)
            else:
                os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = previous
        with open(os.environ["GITHUB_ENV"], "a") as output:
            output.write(f"ADP_TOOLS_ATLASSIAN_SETUP_FILE={path}\n")
            output.write(f"ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE={aws_config}\n")
    except Exception:
        shutil.rmtree(Path(path).parent)
        raise


if __name__ == "__main__":
    main()

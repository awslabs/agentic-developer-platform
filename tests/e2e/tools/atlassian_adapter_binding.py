"""Prepare, but never deploy, protected adapter configuration after real consent."""

import json
import os
import subprocess
import sys
from pathlib import Path

from modules.tools.adapters.observation_configuration import Preparation
from tests.e2e.tools.atlassian_setup_live import load_setup, private_file


def prepare_candidate():
    _, setup = load_setup(os.environ["ADP_TOOLS_ATLASSIAN_SETUP_FILE"], gateway_url=os.environ["GATEWAY_URL"])
    raw = os.environ["ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON"]
    if len(raw.encode()) > 16384:
        raise ValueError("Adapter request exceeds bound")
    request = Preparation.model_validate_json(raw)
    if (
        request.owner_id != setup["owner_id"]
        or request.expected_provider_user_id != setup["expected_subject"]
        or request.site_id != setup["site_id"]
        or request.observer_role_arn != setup["adapter_role_arn"]
        or request.bindings.identity_workload != setup["workload_name"]
        or request.bindings.identity_provider != setup["provider_name"]
        or request.bindings.config_revision != setup["metadata_sha256"]
        or setup["account"] != os.environ["EXPECTED_ACCOUNT"]
        or setup["adapter_label"] != "oidc-adapter"
    ):
        raise ValueError("Adapter request differs from the protected setup")
    root = Path(setup["attempt_file"]).parent
    aws_config = Path(os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"])
    if aws_config != root / "aws-config":
        raise ValueError("Isolated observer profile required")
    private_file(aws_config)
    private_file(root / "aws-credentials")
    source = root / "adapter-request.json"
    descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(request.model_dump_json())
    output = Path(os.environ["RUNNER_TEMP"]) / "tools-eval-evidence"
    if output.is_symlink():
        raise ValueError("Private evidence directory required")
    output.mkdir(mode=0o700, exist_ok=True)
    output.chmod(0o700)
    target = output / "adapter-observation.json"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_") and key != "ADP_TOOLS_ATLASSIAN_ADAPTER_REQUEST_JSON"}
    environment.update({"AWS_PROFILE": "oidc-adapter", "AWS_CONFIG_FILE": str(aws_config),
                        "AWS_SHARED_CREDENTIALS_FILE": str(root / "aws-credentials"), "AWS_EC2_METADATA_DISABLED": "true"})
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modules.tools.adapters.observation_configuration", "--request", str(source), "--output", str(target)],
            env=environment, capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode != 0:
            raise ValueError("Existing Identity grant or provider configuration unavailable")
        configured = json.loads(private_file(target, max_bytes=3 * 1024 * 1024))
        if configured.get("mode") != "observation" or configured.get("release") != request.release.model_dump():
            raise ValueError("Prepared adapter release differs")
    finally:
        source.unlink(missing_ok=True)
    return target


def main():
    try:
        target = prepare_candidate()
        with open(os.environ["GITHUB_ENV"], "a") as stream:
            stream.write(f"ADP_TOOLS_ADAPTER_OBSERVATION_FILE={target}\n")
    except Exception:
        raise SystemExit("Protected adapter preparation unavailable; no deployment or acceptance performed") from None
    print("Private adapter configuration prepared for the existing reviewed deployment path; not live acceptance")


if __name__ == "__main__":
    main()

"""Bind the authority campaign to protected owner sessions and this release."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_scenario import bind_authority
from tests.e2e.tools.atlassian_setup_binding import OIDC_SCRIPT


def release_binding(environment):
    release = flow._read_private(environment["ADP_TOOLS_RELEASE_MANIFEST_FILE"])
    binding = {
        "source_revision": environment["SOURCE_REVISION"],
        "definition_revision": environment["GITHUB_SHA"],
        "run_id": environment["GITHUB_RUN_ID"],
        "run_attempt": environment["GITHUB_RUN_ATTEMPT"],
        "release_sha256": hashlib.sha256(flow.canonical(release)).hexdigest(),
    }
    flow.validate_binding(binding)
    return binding


def materialize(environment):
    binding = release_binding(environment)
    raw = environment["ADP_TOOLS_ATLASSIAN_AUTHORITY_JSON"]
    if len(raw.encode()) > 65536:
        raise ValueError("Protected authority manifest exceeds its bound")
    manifest = flow._decode(raw)
    if "binding" not in manifest:
        manifest = {**manifest, "binding": binding}
    path = bind_authority(
        json.dumps(manifest),
        environment["ADP_TOOLS_ATLASSIAN_AUTHORITY_SESSIONS_JSON"],
        directory=environment["RUNNER_TEMP"],
        binding=binding,
        gateway_url=environment["GATEWAY_URL"],
        account=environment["EXPECTED_ACCOUNT"],
        region=environment["AWS_REGION"],
        reader_role=environment["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"],
    )
    grant = manifest["clients"]["claude-code"]["grant"]
    command = shlex.join(
        [
            sys.executable,
            str(OIDC_SCRIPT.resolve(strict=True)),
            grant["adapter_role_arn"],
            grant["environment"],
        ]
    )
    for name, contents in {
        "aws-credentials": "",
        "aws-config": f"[profile oidc-adapter]\nregion = {grant['region']}\ncredential_process = {command}\n",
    }.items():
        with os.fdopen(
            os.open(path.parent / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
        ) as stream:
            stream.write(contents)
    return path


def main():
    try:
        path = materialize(os.environ)
        with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
            stream.write(f"ADP_TOOLS_ATLASSIAN_AUTHORITY_FILE={path}\n")
            stream.write(
                f"ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE={path.parent / 'aws-config'}\n"
            )
    except (KeyError, OSError, ValueError, TypeError):
        raise SystemExit("Protected authority campaign binding unavailable") from None


if __name__ == "__main__":
    main()

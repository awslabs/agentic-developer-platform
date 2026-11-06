"""Bind installed legacy clients and independent providers to the protected run."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_binding import release_binding
from tests.e2e.tools.atlassian_setup_binding import OIDC_SCRIPT
from tests.e2e.tools import legacy_live_bindings as legacy


def context(environment):
    return {
        "binding": release_binding(environment),
        "target": {"gateway": environment["GATEWAY_REVISION"],
                   "gateway_image": environment["GATEWAY_IMAGE"], "facade_image": environment["FACADE_IMAGE"]},
        "gateway_url": environment["GATEWAY_URL"], "account": environment["EXPECTED_ACCOUNT"],
        "region": environment["AWS_REGION"],
        "reader_role": environment["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"],
    }


def load_manifest(environment):
    return legacy.validate_manifest(flow._read_private(environment["ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"]),
                                    **context(environment))


def materialize(environment):
    bound = context(environment)
    raw = environment["ADP_TOOLS_ATLASSIAN_REGRESSION_JSON"]
    if len(raw.encode()) > 65536:
        raise ValueError("Protected regression input exceeds its bound")
    value = flow._decode(raw)
    if type(value) is not dict:
        raise ValueError("Protected regression input must be an object")
    value.setdefault("binding", bound["binding"])
    value.setdefault("target", bound["target"])
    grants = [cell["grant"] for cell in value["cells"].values() if cell is not None]
    if len({grant["environment"] for grant in grants}) > 1:
        raise ValueError("One protected provider environment required")
    path = legacy.bind_legacy(json.dumps(value), environment["ADP_TOOLS_ATLASSIAN_REGRESSION_SESSIONS_JSON"],
                              directory=environment["RUNNER_TEMP"], **bound)
    try:
        if grants:
            command = shlex.join([sys.executable, str(OIDC_SCRIPT.resolve(strict=True)), bound["reader_role"],
                                 grants[0]["environment"]])
            for name, contents in {
                "aws-credentials": "",
                "aws-config": f"[profile oidc-adapter]\nregion = {bound['region']}\ncredential_process = {command}\n",
            }.items():
                with os.fdopen(os.open(path.parent / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
                    stream.write(contents)
    except Exception:
        shutil.rmtree(path.parent)
        raise
    return path


class LegacyCampaign:
    def __init__(self, environment, *, client_factory):
        self.environment = dict(environment)
        self.context = context(environment)
        self.manifest = load_manifest(environment)
        self.client_factory = client_factory

    def collect(self):
        previous = os.environ.get("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE")
        os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = str(
            Path(self.environment["ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"]).parent / "aws-config")
        try:
            return legacy.collect_legacy(self.environment["ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"],
                                         client_factory=self.client_factory, **self.context)
        finally:
            if previous is None:
                os.environ.pop("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", None)
            else:
                os.environ["ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE"] = previous


def main():
    if not os.environ.get("ADP_TOOLS_ATLASSIAN_REGRESSION_JSON"):
        print("Protected legacy inputs unavailable; original regression cases remain pending")
        return
    try:
        path = materialize(os.environ)
        with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
            stream.write(f"ADP_TOOLS_ATLASSIAN_REGRESSION_FILE={path}\n")
    except (KeyError, OSError, TypeError, ValueError):
        raise SystemExit("Protected legacy regression binding unavailable") from None


if __name__ == "__main__":
    main()

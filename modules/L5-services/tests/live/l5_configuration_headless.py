"""Read disabled Security, DevOps and FinOps drafts without provider/UI work."""

import json
import os
import subprocess
import sys
from pathlib import Path

import security_headless as security
from clients_setup import CLI, LiveSetupDriver, fixture_cli_environment


SERVICES = frozenset({"security", "devops", "finops"})
KIND = "l5_configuration_headless_progress"


def load_fixture(filename, source_sha, *, now=None):
    fixture = security.load_fixture(filename, source_sha, now=now, kind=KIND)
    if fixture.get("service_id") not in SERVICES:
        raise ValueError("only Security, DevOps and FinOps disabled configuration readback is supported")
    return fixture


class ConfigurationDriver(LiveSetupDriver):
    def __init__(self, case, page):
        if page is not None or case.get("service_id") not in SERVICES:
            raise ValueError("select one supported headless configuration service")
        super().__init__(case, None)
        self.service_id = case["service_id"]
        self.path = self.path.removesuffix("security") + self.service_id

    def _scope(self):
        return {**super()._scope(), "service_id": self.service_id}

    def cli_configuration(self):
        command = [sys.executable, str(CLI), "config", "show", "--product", self.case["scope"]["product_id"],
                   "--project", self.case["scope"]["project_id"], "--service", self.service_id, "--json"]
        response = subprocess.run(command, env=fixture_cli_environment(self.case), capture_output=True,
                                  text=True, timeout=self.remaining(), check=False)
        self.remaining()
        if response.returncode != 0:
            raise AssertionError("selected CLI configuration read failed; inspect its dedicated fixture session")
        envelope = json.loads(response.stdout)
        if envelope["status"] != "ok":
            raise AssertionError("selected CLI configuration read did not succeed")
        return {**envelope["detail"], "etag": envelope["etag"]}

    def cli(self, *args, **kwargs):
        raise AssertionError("only CLI configuration readback is selected")

    def mcp_call(self, name, arguments, status=200):
        if name != "l5___get_configuration" or arguments != self._scope() or status != 200:
            raise AssertionError("only MCP configuration readback is selected")
        return super().mcp_call(name, arguments, status)


def run(fixture, *, driver_type=ConfigurationDriver):
    if fixture.get("service_id") not in SERVICES:
        raise ValueError("unsupported headless configuration service")
    selected = {**fixture, "cases": [{**case, "service_id": fixture["service_id"]} for case in fixture["cases"]]}
    result = security.run(selected, driver_type=driver_type)
    return {**result, "kind": KIND, "service_id": fixture["service_id"],
            "pending_acceptance": ["served_ui", "native_provider_execution", "recovery_cleanup", "downstream_reuse"]}


def main(arguments):
    if len(arguments) != 3:
        raise ValueError("usage: l5_configuration_headless.py REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_OUTPUT")
    source_sha, filename, destination = arguments
    output = Path(destination)
    security.private_path(str(output.parent), directory=True)
    fixture = load_fixture(filename, source_sha)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as target:
        result = run(fixture)
        json.dump(result, target, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    print("Disabled L5 configuration checks passed; full service acceptance remains incomplete.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        raise SystemExit("L5 configuration checks did not complete; inspect the selected service's fixture session and configuration.") from None

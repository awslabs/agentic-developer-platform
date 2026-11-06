"""Bind read-only release observations to the protected workflow and target."""

import os
from pathlib import Path
import tempfile

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_binding import release_binding
from tests.e2e.tools.atlassian_release_scenario import collect_release, manifest


def load_manifest(environment):
    value = manifest(
        flow._read_private(environment["ADP_TOOLS_ATLASSIAN_RELEASE_FILE"]),
        account=environment["EXPECTED_ACCOUNT"], region=environment["AWS_REGION"],
    )
    if (value["gateway_url"] != environment["GATEWAY_URL"]
            or value["handoff"]["plan"]["after"]["release"]["source_revision"]
            != release_binding(environment)["source_revision"]):
        raise ValueError("Release observations must match the selected gateway and source")
    return value


def materialize(environment):
    raw = environment["ADP_TOOLS_ATLASSIAN_RELEASE_JSON"]
    if len(raw.encode()) > 65536:
        raise ValueError("Protected release manifest exceeds its bound")
    value = flow._decode(raw)
    root = Path(tempfile.mkdtemp(prefix="atlassian-release-", dir=environment["RUNNER_TEMP"]))
    path = root / "config.json"
    try:
        flow._write_private(path, value)
        load_manifest({**environment, "ADP_TOOLS_ATLASSIAN_RELEASE_FILE": str(path)})
    except (KeyError, OSError, TypeError, ValueError):
        path.unlink(missing_ok=True)
        root.rmdir()
        raise
    return path


class ReleaseCampaign:
    def __init__(self, environment):
        self.environment = dict(environment)
        self.binding = release_binding(environment)
        self.manifest = load_manifest(environment)

    def collect(self):
        environment = self.environment
        read_environment = {
            **environment,
            "ADP_TOOLS_ACCOUNT_ID": environment["EXPECTED_ACCOUNT"],
            "ADP_TOOLS_AWS_REGION": environment["AWS_REGION"],
            "ADP_TOOLS_DEPLOY_ROLE_ARN": environment["EXPECTED_ROLE"],
            "ADP_TOOLS_EKS_CLUSTER_NAME": environment["EKS_CLUSTER"],
        }
        return collect_release(
            environment["ADP_TOOLS_ATLASSIAN_RELEASE_FILE"], binding=self.binding,
            account=environment["EXPECTED_ACCOUNT"], region=environment["AWS_REGION"],
            environment=read_environment,
            journal_path=environment.get("ADP_TOOLS_ATLASSIAN_RELEASE_JOURNAL_FILE"),
        )


def main():
    if not os.environ.get("ADP_TOOLS_ATLASSIAN_RELEASE_JSON"):
        print("Release preparation is unavailable; the handoff scenario remains blocked")
        return
    try:
        path = materialize(os.environ)
        with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
            stream.write(f"ADP_TOOLS_ATLASSIAN_RELEASE_FILE={path}\n")
    except (KeyError, OSError, TypeError, ValueError):
        raise SystemExit("Protected release observation binding unavailable") from None


if __name__ == "__main__":
    main()

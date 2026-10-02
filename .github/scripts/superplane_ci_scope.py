"""Select offline Superplane checks by changed contracts, not trigger paths."""

import json
import os
import re
import subprocess
from pathlib import Path

PERSONA_METADATA = frozenset(
    {
        "docs/agent-catalogue.md",
        "modules/agent-factory/webhook-ingress/lambda/common/personas.py",
        "modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_prompt_files.py",
        "modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_catalogue_parity.py",
    }
)


SHARED_INTEGRATION = frozenset(
    {
        "platform/scripts/deploy-all.sh",
        "platform/scripts/deploy-prerequisites.sh",
        "platform/scripts/undeploy.sh",
        "platform/scripts/undeploy-phases.sh",
        "platform/scripts/teardown.py",
        "platform/scripts/tests/test_teardown.py",
        ".github/workflows/undeploy.yml",
        "docs/adp-platform-deployment/deployment-manifest.md",
        "modules/gateway/tests/features/test_superplane_registration.py",
        "modules/gateway/tests/features/test_superplane_deploy_scope.py",
    }
)
# Components are executable CI lanes, not mutually exclusive scopes. A PR can
# select several consumers of a shared contract without running unrelated suites.
COMPONENTS = frozenset(
    {"domain", "api", "controller", "monitor", "gateway", "worker", "lifecycle", "ui"}
)
MODULE = "modules/domain-apps/superplane/"
CI_DEFINITIONS = frozenset(
    {
        ".github/workflows/superplane-domain-ci.yml",
        ".github/scripts/superplane_ci_scope.py",
        ".github/scripts/tests/test_superplane_ci_scope.py",
    }
)
WORKER_DEPENDENCIES = frozenset(
    {
        ".github/workflows/agent-worker-image.yml",
        "modules/agent-factory/gateway/scripts/stage-security-bundles.sh",
    }
)
GATEWAY_DEPENDENCIES = frozenset(
    {
        "modules/gateway/src/features/routes.py",
        "modules/gateway/src/app.py",
        "modules/gateway/frontend/src/services/features.ts",
        "modules/gateway/frontend/src/App.tsx",
        "modules/gateway/frontend/src/components/Navigation.tsx",
        "modules/gateway/frontend/src/components/next/journeys.ts",
    }
)


def classify(paths: list[str]) -> set[str]:
    if not paths or any(path in CI_DEFINITIONS for path in paths):
        return set(COMPONENTS) | {"persona"}
    selected = set()
    for path in paths:
        if (
            path.startswith(MODULE + "ui/")
            or path == ".github/workflows/superplane-ui-browser-ci.yml"
        ):
            selected.add("ui")
        elif path.startswith("modules/gateway/frontend/"):
            # Superplane borrows the gateway frontend toolchain and shared UI.
            selected.add("ui")
            if path in GATEWAY_DEPENDENCIES:
                selected.add("gateway")
        elif path in PERSONA_METADATA:
            selected.add("persona")
        elif path in SHARED_INTEGRATION:
            selected.add("lifecycle")
        elif path.startswith(MODULE + "src/superplane-api/"):
            selected.add("api")
        elif path.startswith(MODULE + "src/superplane-controller/"):
            selected.add("controller")
        elif path.startswith(MODULE + "src/superplane-platform-monitor/"):
            selected.add("monitor")
        elif path.startswith(
            (MODULE + "contracts/", MODULE + "releases/", MODULE + "src/")
        ):
            # Shared wire contracts, release inputs and unknown transferred
            # components retain full consumer coverage until mapped explicitly.
            selected.update(COMPONENTS)
            selected.add("persona")
        elif path == MODULE + "pyproject.toml" or path.startswith(
            (
                MODULE + "installation/",
                MODULE + "images/",
                MODULE + "events/",
                MODULE + "integrations/",
            )
        ):
            selected.update({"domain", "api", "controller", "worker"})
        elif path.startswith(("libs/python/adp-common/", MODULE + "auth/")):
            selected.update({"domain", "api", "gateway"})
        elif path.startswith((MODULE + "executor/", "modules/harness/jobs/")):
            selected.update({"domain", "api", "controller", "worker"})
        elif path.startswith(
            (
                MODULE + "infra/account-factory/",
                MODULE + "infra/account-provisioning/",
                MODULE + "workspace_bootstrap/",
                MODULE + "workspace_provisioning/",
            )
        ):
            selected.update({"domain", "api"})
        elif path.startswith(MODULE + "agent/"):
            selected.update({"worker", "persona"})
        elif path in {
            MODULE + "tests/acceptance/test_worker_image_assets.py",
            MODULE + "tests/_skypilot_task_schema_check.py",
        } or (
            path.startswith(
                (
                    "modules/agent-factory/agent-worker-image/",
                    "modules/gateway/security/stdlib/",
                )
            )
            or path in WORKER_DEPENDENCIES
        ):
            selected.add("worker")
        elif path == "modules/gateway/pyproject.toml":
            # Domain and gateway suites install this dependency set. The API
            # and Go components install their own dependencies in separate jobs.
            selected.update({"domain", "gateway"})
        elif path in GATEWAY_DEPENDENCIES or path.startswith(
            (
                "modules/gateway/src/domain_proxy/",
                "modules/gateway/src/auth/",
                "modules/gateway/src/shared/",
                "modules/gateway/tests/features/test_superplane_",
                "modules/gateway/tests/e2e/test_superplane_",
            )
        ):
            selected.add("gateway")
        elif path.startswith(MODULE):
            selected.add("domain")
    return selected


def main() -> None:
    selected = set(COMPONENTS) | {"persona"}
    if os.environ["CI_EVENT"] == "pull_request":
        base, head = os.environ["PR_BASE_SHA"], os.environ["PR_HEAD_SHA"]
        if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
            raise ValueError("Expected full PR commit SHAs")
        changed = (
            subprocess.check_output(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    f"{base}...{head}",
                    "--",
                ]
            )
            .decode()
            .split("\0")
        )
        paths = [path for path in changed if path]
        selected = classify(paths)
    matrix = sorted(selected - {"persona"}) or ["core"]
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"components={json.dumps(matrix)}\n")
        output.write(f"persona_changed={str('persona' in selected).lower()}\n")
        output.write(f"controller_changed={str('controller' in selected).lower()}\n")
    print("Superplane CI components: " + ", ".join(sorted(selected) or ["core"]))


if __name__ == "__main__":
    main()

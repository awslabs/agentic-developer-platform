"""Select offline Superplane checks by changed contracts, not trigger paths."""

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
SHARED_DEPENDENCIES = frozenset(
    {
        ".github/workflows/superplane-domain-ci.yml",
        ".github/scripts/superplane_ci_scope.py",
        ".github/scripts/tests/test_superplane_ci_scope.py",
        "modules/gateway/src/features/routes.py",
        "modules/gateway/src/app.py",
        "modules/gateway/frontend/src/services/features.ts",
        "modules/gateway/frontend/src/App.tsx",
        "modules/gateway/frontend/src/components/Navigation.tsx",
        "modules/gateway/frontend/src/components/next/journeys.ts",
        "modules/gateway/pyproject.toml",
        "modules/agent-factory/agent-worker-image/Dockerfile",
        ".github/workflows/agent-worker-image.yml",
        "modules/agent-factory/agent-worker-image/stage-personas.sh",
    }
)
FULL_PREFIXES = (
    "modules/domain-apps/superplane/",
    "modules/harness/jobs/",
    "modules/gateway/src/domain_proxy/",
    "modules/gateway/src/auth/",
    "modules/gateway/src/shared/",
    "modules/gateway/tests/features/test_superplane_",
    "modules/gateway/tests/e2e/test_superplane_",
)


def persona_only(paths: list[str]) -> bool:
    return bool(paths) and set(paths) <= PERSONA_METADATA


def classify(paths: list[str]) -> str:
    if not paths or any(
        (path.startswith(FULL_PREFIXES) and path not in SHARED_INTEGRATION)
        or path in SHARED_DEPENDENCIES
        for path in paths
    ):
        return "full"
    if persona_only(paths):
        return "persona"
    if any(path in SHARED_INTEGRATION for path in paths):
        return "integration"
    return "core"


def main() -> None:
    selected = "full"
    persona_changed = False
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
        persona_changed = bool(set(paths) & PERSONA_METADATA)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"scope={selected}\n")
        output.write(f"persona_changed={str(persona_changed).lower()}\n")
    print(f"Superplane CI scope: {selected}")


if __name__ == "__main__":
    main()

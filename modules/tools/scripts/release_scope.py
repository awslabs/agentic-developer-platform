"""Resolve reviewed tools changes to their actual release paths."""

import argparse
import json
import re
import subprocess
from pathlib import Path
from pathlib import PurePosixPath


RELEASES = (
    ("modules/tools/platform/schemas/", "platform targets", "tools-platform-deploy.yml", "dispatch apply with native approval"),
    ("modules/tools/platform/infra/", "platform targets", "tools-platform-deploy.yml", "dispatch apply with native approval"),
    ("modules/tools/platform/bootstrap/", "platform bootstrap", None, "protected bootstrap prerequisite"),
    ("platform/automation-infra/shared-tools-", "platform bootstrap", None, "protected bootstrap prerequisite"),
    ("platform/infra/shared-tools-release-rbac.tf", "platform bootstrap", None, "protected bootstrap prerequisite"),
    ("modules/tools/agentcore/", "shared Lambda and Browser", "shared-agentcore-tools-deploy.yml", "dispatch apply"),
    # Shared source is copied into provider and adapter images and installed by
    # the facade; each consumer must be released from the reviewed source.
    ("modules/tools/adp_tools/", "shared Lambda and Browser", "shared-agentcore-tools-deploy.yml", "dispatch apply"),
    ("modules/tools/adp_tools/", "facade and reconciler", "tools-facade-deploy.yml", "push apply"),
    ("modules/tools/adp_tools/", "tool-adapters", "tools-platform-deploy.yml", "dispatch release stage tool-adapters with native approval"),
    ("modules/tools/adapters/", "tool-adapters", "tools-platform-deploy.yml", "dispatch release stage tool-adapters with native approval"),
    ("modules/tools/interceptor/", "interceptor", "tools-interceptor-deploy.yml", "push apply"),
    ("modules/tools/facade/", "facade and reconciler", "tools-facade-deploy.yml", "push apply"),
    ("modules/gateway/src/", "gateway", "gateway-deploy.yml", "push apply"),
    ("modules/gateway/cli/", "gateway-installed CLI", "gateway-deploy.yml", "push apply"),
    ("modules/gateway/frontend/", "frontend client", "gateway-frontend-deploy.yml", "push apply"),
    ("modules/agent-context/", "Door", "agent-context-deploy.yml", "dispatch"),
    ("modules/agent-factory/agent-worker-image/", "worker image", "agent-worker-image.yml", "push image build"),
    ("modules/tools/task-sdk/", "Task SDK client", "agent-worker-image.yml", "push image build"),
    ("modules/tools/platform/scripts/", "platform targets", "tools-platform-deploy.yml", "dispatch apply with native approval"),
    ("modules/tools/deploy.sh", "platform targets", "tools-platform-deploy.yml", "dispatch apply with native approval"),
    (".github/workflows/shared-agentcore-tools-deploy.yml", "shared Lambda and Browser", "shared-agentcore-tools-deploy.yml", "dispatch apply"),
)

# Only the facade installs this distribution. The provider images copy shared
# source and retain their own dependency installation, so metadata is not a
# reason to publish a provider image. Match the metadata file exactly.
FILE_RELEASES = {
    "modules/tools/pyproject.toml": ("facade and reconciler", "tools-facade-deploy.yml", "push apply"),
}


def release_scope(paths):
    """Return component, existing workflow, and trigger; never treat dispatch as push."""
    selected = []
    for path in paths:
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("release paths must be repository relative")
        matches = [entry[1:] for entry in RELEASES if path.startswith(entry[0])]
        if path in FILE_RELEASES:
            matches.append(FILE_RELEASES[path])
        for component, workflow, trigger in matches:
            target = {"component": component, "workflow": workflow, "trigger": trigger}
            if target not in selected:
                selected.append(target)
    return selected


def operator_sequence(paths):
    """Describe the supported release order without authorizing or dispatching any workflow."""
    selected = release_scope(paths)
    components = {item["component"] for item in selected}
    steps = []
    if "platform bootstrap" in components or "shared Lambda and Browser" in components:
        steps.append({"component": "protected bootstrap", "workflow": None,
                      "trigger": "coordinator prepares scoped build, release role and private state bindings"})
    for item in selected:
        if item["component"] == "shared Lambda and Browser":
            steps.append(item)
    for item in selected:
        if item["component"] == "interceptor":
            steps.append(item)
    if "shared Lambda and Browser" in components and "platform targets" not in components:
        steps.append({"component": "platform targets", "workflow": "tools-platform-deploy.yml",
                      "trigger": "dispatch apply after shared runtime receipt"})
    for name in ("platform targets", "tool-adapters", "facade and reconciler", "gateway", "gateway-installed CLI",
                 "frontend client", "worker image", "Task SDK client", "Door"):
        for item in selected:
            if item["component"] != name:
                continue
            if name == "platform targets" and "shared Lambda and Browser" in components:
                item = {**item, "trigger": "dispatch apply after shared runtime receipt"}
            steps.append(item)
    return steps


def verify_shared_receipt(path, *, source, environment, account):
    """Accept only an applied provider receipt with matched independent runtime digests."""
    receipt = json.loads(Path(path).read_text())
    if not (re.fullmatch(r"[a-f0-9]{40}", source) and re.fullmatch(r"[0-9]{12}", account)
            and environment in {"dev", "prod"} and receipt.get("source_revision") == source
            and receipt.get("environment") == environment and receipt.get("account") == account
            and receipt.get("mode") == "apply"
            and type(receipt.get("run_id")) is int and receipt["run_id"] > 0
            and type(receipt.get("run_attempt")) is int and receipt["run_attempt"] > 0):
        raise ValueError("Coordinator must supply the verified protected shared apply receipt")
    images = receipt.get("images")
    if not isinstance(images, dict):
        raise ValueError("Shared runtime image evidence unavailable")
    for name in ("lambda", "browser"):
        image = images.get(name, {})
        if not (isinstance(image, dict) and isinstance(image.get("built"), str)
                and re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", image["built"])
                and image.get("runtime") == image["built"]):
            raise ValueError("Shared runtime image does not match the reviewed build")
    return receipt


def platform_push_deferred(paths):
    return any(path.startswith(("modules/tools/agentcore/", "modules/tools/adp_tools/", "modules/tools/platform/bootstrap/",
                                "platform/automation-infra/shared-tools-",
                                "platform/infra/shared-tools-release-rbac.tf")) or
               path == ".github/workflows/shared-agentcore-tools-deploy.yml" for path in paths)


def main():
    parser = argparse.ArgumentParser(description="Plan protected tools releases; no workflow is dispatched")
    parser.add_argument("--guard-platform-push", action="store_true")
    parser.add_argument("--base", required=True)
    parser.add_argument("--source", required=True)
    arguments = parser.parse_args()
    if not all(re.fullmatch(r"[a-f0-9]{40}", revision) for revision in (arguments.base, arguments.source)):
        parser.error("reviewed full source and base commits are required")
    root = Path(__file__).resolve().parents[3]
    for revision in (arguments.base, arguments.source):
        subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", revision, "origin/main"], check=True)
    paths = subprocess.check_output(["git", "-C", str(root), "diff", "--name-only", arguments.base,
                                     arguments.source], text=True).splitlines()
    if arguments.guard_platform_push and platform_push_deferred(paths):
        parser.error("Coordinator: wait for protected shared provider apply, then dispatch platform release; automatic push cannot establish provider runtime")
    print(json.dumps({"source_revision": arguments.source, "status": "plan-only",
                      "steps": operator_sequence(paths)}, sort_keys=True))


if __name__ == "__main__":
    main()

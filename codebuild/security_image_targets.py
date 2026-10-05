"""Container scan inventory, including runtime images with no local Dockerfile."""

import argparse
import json
import os
import re
from pathlib import Path

import yaml

SUPERPLANE = Path("modules/domain-apps/superplane")
DOC_API_COMPAT = "docs/security/runs/2026-09-27/admin-closure/api-compat/Dockerfile"
ORIGINAL_GAPS = {
    "docs-security-runs-2026-09-27-admin-closure-api-compat": "sanitized documentation fixture",
    "modules-agent-context-images-codegraph-context": "shared stdlib apply/check failure",
    "modules-agent-context-images-context-mcp": "shared stdlib apply/check failure",
    "modules-agent-context-images-ingestion": "pinned ingestion ECR input returned 403",
    "modules-agent-context-images-ingestion-high-security": "pinned ingestion ECR input returned 403",
    "modules-agent-context-images-litellm-proxy": "shared stdlib apply/check failure",
    "modules-agent-context-images-parser": "pinned ingestion ECR input returned 403",
}


def non_runtime_fixtures(root: Path, scope: str = "all") -> list[dict]:
    if scope != "all" or not (root / DOC_API_COMPAT).is_file():
        return []
    source = (root / DOC_API_COMPAT).read_text()
    inputs = re.findall(r"^\s*FROM\s+(\S+)", source, flags=re.IGNORECASE | re.MULTILINE)
    if (len(inputs) != 2 or any(
        not image.startswith("000000000101.dkr.ecr.us-east-1.amazonaws.com/")
        or not re.search(r"@sha256:[0-9a-f]{64}$", image)
        for image in inputs
    ) or "maintenance artifact" not in (root / DOC_API_COMPAT).with_name("README.md").read_text()):
        raise ValueError("Documentation fixture changed: review its scan classification")
    return [{
        "name": str(Path(DOC_API_COMPAT).parent).replace("/", "-"),
        "dockerfile": DOC_API_COMPAT,
        "original_status": "failed",
        "original_run": "37278531434/1",
        "reason": "Historical maintenance artifact with sanitized example-account inputs; not a maintained runtime (docs/PUBLISHING.md)",
    }]


BUILD_CONFIG = {
    # This detached-check fixture requires only POSIX sh/coreutils. Reuse the
    # reviewed scan Python base rather than leaving its required ARG empty.
    "modules/agent-factory/codex-harness/test/fixtures/detached-checks/Dockerfile": {
        "build_arg_env": {"BASE_IMAGE": "SECURITY_EXECUTOR_PYTHON_IMAGE"},
    },
    "modules/tools/agentcore/Dockerfile": {"context": "."},
    "modules/tools/validation/Dockerfile": {"context": "."},
    "platform/security/openssh-high/Dockerfile": {"context": "."},
    "platform/security/skypilot-openssh/Dockerfile": {"context": "."},
    "modules/agent-context/images/parser/Dockerfile": {"context": "modules/agent-context/images/ingestion"},
    "modules/domain-apps/cyber/tools/Dockerfile": {"context": "."},
    "modules/domain-apps/cyber/browser/Dockerfile": {"context": "."},
    "modules/domain-apps/cyber/workers/Dockerfile": {"context": "modules/domain-apps/cyber"},
    "modules/domain-apps/superplane/tests/acceptance/workloads/Dockerfile": {
        "build_arg_env": {"PYTORCH_IMAGE": "SECURITY_PYTORCH_IMAGE"},
    },
    "platform/automation-infra/Dockerfile": {
        "build_arg_env": {"RUNNER_IMAGE": "SECURITY_RUNNER_IMAGE"},
    },
    "modules/domain-apps/superplane/executor/Dockerfile": {
        "context": ".",
        "build_arg_env": {"PYTHON_IMAGE": "SECURITY_EXECUTOR_PYTHON_IMAGE"},
    },
    "modules/agent-context/images/context-mcp/Dockerfile": {
        "prepare": [
            ["copy-tree", "modules/agent-context/door", "modules/agent-context/images/context-mcp/door"],
            ["copy-tree", "modules/agent-context/personal_context", "modules/agent-context/images/context-mcp/personal_context"],
        ],
    },
    "modules/agent-context/images/ingestion/Dockerfile": {
        "prepare": [
            ["copy-tree", "modules/agent-context/pipeline", "modules/agent-context/images/ingestion/pipeline"],
            ["copy-tree", "modules/agent-context/alembic", "modules/agent-context/images/ingestion/alembic"],
            ["copy-tree", "modules/agent-context/personal_context", "modules/agent-context/images/ingestion/personal_context"],
        ],
    },
    "modules/agent-factory/agent-worker-image/Dockerfile": {"context": "."},
    "modules/agent-factory/agent/Dockerfile": {"context": "modules/agent-factory"},
    "modules/agent-factory/gateway/Dockerfile": {
        "context": "modules/agent-factory",
        "prepare": [["run", "bash", "modules/agent-factory/scripts/stage-security-bundles.sh"]],
    },
    "modules/domain-apps/superplane/src/superplane-api/Dockerfile": {
        "prepare": [["run", "bash", "modules/domain-apps/superplane/src/superplane-api/scripts/stage-domain-auth.sh"]],
    },
    "modules/gateway/Dockerfile": {
        "prepare": [["run", "bash", "modules/gateway/scripts/stage-contracts.sh"]],
    },
    "modules/research/gbrain/docker/Dockerfile": {"context": "modules/research/gbrain"},
}

# Keep scanner builds on the same canonical source bundles as release builds.
for _image in ("codegraph-context", "ingestion", "context-mcp", "litellm-proxy", "parser", "deepwiki"):
    _dockerfile = f"modules/agent-context/images/{_image}/Dockerfile"
    _build = BUILD_CONFIG.setdefault(_dockerfile, {})
    _context = _build.get("context", str(Path(_dockerfile).parent))
    _prepare = _build.setdefault("prepare", [])
    _prepare.append(["copy-tree", "modules/gateway/security/stdlib", f"{_context}/security-stdlib"])
    if _image in ("codegraph-context", "ingestion"):
        _prepare.append(["copy-tree", "modules/agent-context/images/shared", f"{_context}/security-build"])


def discover(root: Path, scope: str = "all") -> list[dict]:
    lock = yaml.safe_load((root / SUPERPLANE / "releases/superplane.lock.yaml").read_text())
    components = lock["maintained_source"]["components"]
    required = {str(SUPERPLANE / path / "Dockerfile") for path in components.values()}
    excluded = {fixture["dockerfile"] for fixture in non_runtime_fixtures(root, scope)}
    targets = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        if "Dockerfile" not in files:
            continue
        dockerfile = str((Path(directory) / "Dockerfile").relative_to(root))
        if dockerfile in excluded:
            continue
        if scope == "superplane" and not dockerfile.startswith(str(SUPERPLANE) + "/"):
            continue
        build = BUILD_CONFIG.get(dockerfile, {})
        targets.append({
            "name": str(Path(dockerfile).parent).replace("/", "-"),
            "dockerfile": dockerfile,
            "context": build.get("context", str(Path(dockerfile).parent)),
            "prepare": build.get("prepare", []),
            "build_arg_env": build.get("build_arg_env", {}),
            "image": "-",
            "required": dockerfile in required,
        })
    missing = required - {target["dockerfile"] for target in targets}
    if missing:
        raise ValueError(f"Missing maintained Superplane Dockerfiles: {sorted(missing)}")
    # Images built from maintained source are covered above; resolved external
    # runtimes (currently SkyPilot) must also be scanned, by digest, never by tag.
    for name, digest in lock["images"].items():
        source = lock["image_sources"][name]
        if source.get("source_path"):
            continue
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or digest == "sha256:" + "0" * 64:
            raise ValueError(f"Unpinned runtime image: {name}")
        targets.append({
            "name": "superplane-" + name,
            "dockerfile": "-", "context": "-",
            "prepare": [],
            "image": f"{source['registry']}/{source['repository']}@{digest}",
            "required": True,
        })
    return sorted(targets, key=lambda target: target["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("all", "superplane"), default="all")
    parser.add_argument("--format", choices=("json", "tsv", "count"), default="json")
    args = parser.parse_args()
    targets = discover(Path.cwd(), args.scope)
    if args.format == "count":
        print(len(targets))
    elif args.format == "tsv":
        for target in targets:
            print("\t".join(str(target[key]) for key in ("name", "dockerfile", "context", "image")))
    else:
        print(json.dumps(targets, indent=2))


if __name__ == "__main__":
    main()

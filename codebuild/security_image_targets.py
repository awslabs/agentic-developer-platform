"""Container scan inventory, including runtime images with no local Dockerfile."""

import argparse
import json
import os
import re
from pathlib import Path

import yaml

SUPERPLANE = Path("modules/domain-apps/superplane")
BUILD_CONFIG = {
    "modules/agent-context/images/parser/Dockerfile": {"context": "modules/agent-context/images/ingestion"},
    "modules/domain-apps/cyber/browser/Dockerfile": {"context": "modules/domain-apps/cyber"},
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
    targets = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        if "Dockerfile" not in files:
            continue
        dockerfile = str((Path(directory) / "Dockerfile").relative_to(root))
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

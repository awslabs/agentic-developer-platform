"""Reproduce the FinOps design audit without contacting a provider."""

import argparse
import ast
import importlib.util
import json
import subprocess
from pathlib import Path

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]
BASELINE = "388584bdd80a4a5ff142abe3234e7d1eac1cfd64"
SPEC = importlib.util.spec_from_file_location("shared_inventory", HOME.parent / "audit_inventory.py")
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)
ORIGINAL_CLASSIFY = SHARED.classify
ORIGINAL_ROLES = SHARED.roles


def classify(path):
    if path.startswith("modules/L5-services/"):
        return "l5-runtime", "reuse shared core; extend FinOps via coordinated FO01-FO07 changes"
    if path.startswith("docs/l5-services/"):
        return "l5-design", "reference shared and sibling contracts; FinOps owns its subtree only"
    if path == ".github/workflows/l5-services-ci.yml":
        return "l5-validation", "extend checks; CI is not a worker deployment"
    if path.startswith(("deploy.sh", "undeploy.sh")):
        return "platform-release", "audit launcher compatibility; no deployment in design assignment"
    component, disposition = ORIGINAL_CLASSIFY(path)
    return component, disposition


def roles(path, text, executable):
    found = ORIGINAL_ROLES(path, text, executable).split(",")
    if path.endswith(".py") and ("lambda_handler" in text or "[project.scripts]" in text):
        found.append("handler-entry")
    if path.endswith((".json", ".toml", ".yaml", ".yml")) and any(
        marker in text for marker in ("[project.scripts]", '"scripts":', '"bin":', "entry_points")
    ):
        found.append("package-entry")
    return ",".join(sorted(set(found) - {"supporting-artifact"})) or "supporting-artifact"


def inventories():
    SHARED.REVISION = BASELINE
    SHARED.classify = classify
    SHARED.roles = roles
    outputs = SHARED.inventories()
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASELINE], cwd=ROOT, text=True).splitlines()
    migrations = {}
    for path in paths:
        if not path.startswith("modules/L5-services/src/l5_services/migrations/versions/") or not path.endswith(".py"):
            continue
        text = subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=ROOT, text=True)
        values = {}
        for node in ast.parse(text).body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                        values[target.id] = ast.literal_eval(node.value)
        if "revision" in values:
            migrations[values["revision"]] = (path, values.get("down_revision"))
    rows = [["path", "revision", "parents"]]
    pending, seen, parents = dict(migrations), set(), set()
    while pending:
        ready = []
        for revision, (path, parent) in pending.items():
            dependencies = set(parent if isinstance(parent, (tuple, list)) else [parent] if parent else [])
            if dependencies <= seen:
                ready.append(revision)
        if not ready:
            raise ValueError("Unresolved L5 migration graph: " + str(sorted(pending)))
        for revision in sorted(ready):
            path, parent = pending.pop(revision)
            parents.update(parent if isinstance(parent, (tuple, list)) else [parent] if parent else [])
            rows.append([path, revision, json.dumps(parent)])
            seen.add(revision)
    outputs["l5-migrations.tsv"] = SHARED.table(rows)
    summary = json.loads(outputs["inventory-summary.json"])
    summary["l5_migration_count"] = len(migrations)
    summary["l5_migration_heads"] = sorted(seen - parents)
    summary["semantic_limits"].extend([
        "Path-based component assignment is exhaustive, not a semantic certification of every artifact",
        "External actions, mutable provider UI, live IAM and deployment state are not expanded or verified",
        "Open PR overlays and historical POCs are reviewed separately in audit.md; not counted as main artifacts",
    ])
    outputs["inventory-summary.json"] = json.dumps(summary, indent=2) + "\n"
    return outputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=HOME)
    args = parser.parse_args()
    for name, content in inventories().items():
        path = args.output / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Inventory differs: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    print("FinOps complete pinned-tree inventory " + ("verified" if args.check else "generated"))


if __name__ == "__main__":
    main()

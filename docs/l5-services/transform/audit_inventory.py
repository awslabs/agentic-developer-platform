"""Reproduce the Transform design's full tracked-tree inventory without cloud access."""

import argparse
import ast
import importlib.util
import json
from collections import Counter
from pathlib import Path

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]
REVISION = "388584bdd80a4a5ff142abe3234e7d1eac1cfd64"
RELATED = {
    "9080": "35e00861a273b4a6492e05c57917c0272947255d",
    "8932": "1477aed6761d4f7e09be64344fe3c27ddeb605fd",
    "9081": "25e62f04bf750a7bcb36663e94105edfab587218",
    "7991": "cebebe4c4bb6ed13cfbd5b9c68749b144cec79f9",
    "8020": "398cf85c6017cc5492e246eae440fd4ccf241341",
}
HISTORICAL_BASES = {
    "7991": "3c3f6dff03901a7c6243ec5dd22a2cf04b4305b7",
    "8020": "59bb20db944c5c86a6a049a178416cfb038319ca",
}
spec = importlib.util.spec_from_file_location("shared_l5_audit", HOME.parent / "audit_inventory.py")
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
original_classify = shared.classify
original_roles = shared.roles


def classify(path):
    if path.startswith("modules/L5-services/"):
        return "l5", "reuse implemented shared core; Transform extension proposed, not deployed"
    if path.startswith(".github/workflows/l5-"):
        return "l5-release", "coordinate with SA15 and TF07; source presence is not live delivery"
    if path.startswith("modules/tools/validation/") or (path.startswith("modules/agent-factory/agent-worker-image/") and "validation" in path):
        return "isolated-executor", "reuse Kubernetes validation isolation; TF01 qualifies L5 non-blocking bridge"
    if path.startswith("modules/domain-apps/superplane/executor/"):
        return "superplane-executor", "excluded: infrastructure-operation executor, not selected for repository builds"
    if path.startswith("docs/l5-services/"):
        return "l5-design", "reference shared contracts; edit only Transform-owned documents and index link"
    return original_classify(path)


def roles(path, text, executable):
    result = original_roles(path, text, executable)
    extra = []
    if Path(path).suffix in {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".hcl"}:
        extra.append("configuration-candidate")
    if "def lambda_handler(" in text or "def handler(" in text:
        extra.append("handler-candidate")
    if "[project.scripts]" in text or '"scripts":' in text or "[project.entry-points" in text:
        extra.append("declared-command-container")
    if extra and result == "supporting-artifact":
        return ",".join(extra)
    return ",".join([result, *extra])


def literal_assignments(tree):
    values = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision", "depends_on"}:
                    values[target.id] = ast.literal_eval(node.value)
    return values


def products():
    shared.REVISION = REVISION
    shared.classify = classify
    shared.roles = roles
    records = shared.inventories()
    models = [["path", "line", "class", "table", "columns"]]
    migrations = [["path", "revision", "parents", "dependencies"]]
    paths = shared.git("ls-tree", "-r", "--name-only", REVISION, "modules/L5-services/src/l5_services").decode().splitlines()
    for path in paths:
        if not path.endswith(".py") or not any(part in path for part in ("/models/", "/migrations/versions/")):
            continue
        tree = ast.parse(shared.git("show", f"{REVISION}:{path}").decode())
        if "/migrations/versions/" in path:
            values = literal_assignments(tree)
            if "revision" in values:
                migrations.append([path, values["revision"], json.dumps(values.get("down_revision")), json.dumps(values.get("depends_on"))])
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            table = None
            columns = []
            for member in node.body:
                if isinstance(member, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__tablename__" for target in member.targets):
                    table = ast.literal_eval(member.value)
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    columns.append(member.target.id)
            if table:
                models.append([path, node.lineno, node.name, table, ",".join(columns)])
    related = [["pr", "head_revision", "merge_base", "path", "blob_or_deleted", "component", "disposition"]]
    for number, revision in RELATED.items():
        base = HISTORICAL_BASES.get(number) or shared.git("merge-base", REVISION, revision).decode().strip()
        for path in shared.git("diff", "--name-only", base, revision).decode().splitlines():
            entry = shared.git("ls-tree", revision, "--", path).decode().strip()
            related.append([number, revision, base, path, entry.split()[2] if entry else "deleted", *classify(path)])
    records["l5-models.tsv"] = shared.table(models)
    records["l5-migrations.tsv"] = shared.table(migrations)
    records["related-pr-artifacts.tsv"] = shared.table(related)
    summary = json.loads(records["inventory-summary.json"])
    summary.update({
        "l5_model_tables": len(models) - 1,
        "l5_migrations": len(migrations) - 1,
        "related_pr_paths": dict(Counter(row[0] for row in related[1:])),
        "related_heads": RELATED,
        "semantic_limits": [
            "Every baseline tracked path is mapped; entry detection conservatively includes all structured configuration candidates.",
            "External actions, downloaded binaries, runtime-generated commands and live resources are not expanded or executed.",
            "Related PR paths are complete merge-base-to-pinned-head diffs, not assertions that these PRs have merged.",
            "SQL models and migration sources are inventoried; deployed database state and raw SQL effects are not introspected.",
            "Kubernetes validation executor exists; an L5 workload bridge, durable paid-session recovery and live isolation remain unqualified.",
        ],
    })
    records["inventory-summary.json"] = json.dumps(summary, indent=2) + "\n"
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--patch", action="store_true")
    arguments = parser.parse_args()
    records = products()
    if arguments.patch:
        print("*** Begin Patch")
        for name, content in records.items():
            relative = (HOME / name).relative_to(ROOT)
            if (HOME / name).exists():
                print(f"*** Delete File: {relative}")
            print(f"*** Add File: {relative}")
            print("".join("+" + line + "\n" for line in content.splitlines()), end="")
        print("*** End Patch")
        return
    if not arguments.check:
        parser.error("select --check or --patch")
    for name, content in records.items():
        if not (HOME / name).exists() or (HOME / name).read_text() != content:
            raise SystemExit(f"Inventory differs: {name}")
    print(f"Verified all {len(records)} inventory artifacts at {REVISION}")


if __name__ == "__main__":
    main()

"""Reproduce the Well-Architected design's complete, source-only repository audit."""

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HOME = Path(__file__).resolve().parent
REVISION = "388584bdd80a4a5ff142abe3234e7d1eac1cfd64"
FOLLOWUP_MAIN = "eba7f5b95a0ea2bbc70a655201099e727d4479f7"
OVERLAYS = {
    "9080": "35e00861a273b4a6492e05c57917c0272947255d",
    "8932": "1477aed6761d4f7e09be64344fe3c27ddeb605fd",
    "9081": "25e62f04bf750a7bcb36663e94105edfab587218",
    "8018": "b442cf8904ad003d119659520bced807a7d8131c",
}
OVERLAY_BASES = {
    "9080": "32880295c85595f4d11fb77c2053088ce1a4b7ca",
    "8932": "a3fcf57a3f815e569553679b8ff67b706dc3ebad",
    "9081": "388584bdd80a4a5ff142abe3234e7d1eac1cfd64",
    "8018": "59bb20db944c5c86a6a049a178416cfb038319ca",
}
SPEC = importlib.util.spec_from_file_location("shared_inventory", HOME.parent / "audit_inventory.py")
SHARED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARED)


def classify(path):
    if path.startswith("modules/L5-services/"):
        return "l5-core", "reuse shared runtime; extend only WA-owned or coordinated contracts"
    if path.startswith("docs/l5-services/"):
        return "l5-design", "reference shared contracts; preserve sibling designs and audits"
    component, disposition = SHARED.classify(path)
    return component, disposition.replace("design-only baseline", "source-inspected; deployment unverified")


def generate():
    files = [["path", "blob", "component", "disposition", "roles"]]
    workflows = [["path", "events", "jobs", "path_filters", "local_dependencies"]]
    resources = [["path", "line", "kind", "logical_name"]]
    migrations = {}
    declarations = [["path", "line", "kind", "name"]]
    gaps = []
    process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for entry in SHARED.git("ls-tree", "-r", REVISION).decode().splitlines():
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if kind != "blob":
            files.append([path, blob, *classify(path), "external-gitlink"])
            gaps.append({"path": path, "reason": "external gitlink content not expanded"})
            continue
        process.stdin.write((blob + "\n").encode())
        process.stdin.flush()
        size = int(process.stdout.readline().split()[2])
        body = process.stdout.read(size)
        process.stdout.read(1)
        text = "" if b"\0" in body else body.decode(errors="replace")
        roles = SHARED.roles(path, text, mode == "100755")
        additions = []
        if re.search(r"(?m)^(?:async )?def (?:lambda_handler|handler|main)\(", text):
            additions.append("runtime-handler")
        if Path(path).name.lower() in {"justfile", "taskfile.yml", "cdk.json", "serverless.yml", "terragrunt.hcl"}:
            additions.append("build-deploy-config")
        if additions:
            roles = ",".join(([roles] if roles != "supporting-artifact" else []) + additions)
        files.append([path, blob, *classify(path), roles])
        if "workflow" in roles:
            data = SHARED.yaml.safe_load(text)
            events = data.get("on", data.get(True, {}))
            filters = {name: {key: value for key, value in settings.items()
                             if key in {"paths", "paths-ignore", "branches", "branches-ignore"}}
                       for name, settings in events.items() if isinstance(settings, dict)} if isinstance(events, dict) else {}
            refs = sorted(set(re.findall(r"(?:uses:\s*|(?:python3?|bash|sh)\s+)(\./[^\s\"']+|(?:\.github|scripts|modules|platform|tests|codebuild)/[^\s\"']+)", text)))
            workflows.append([path, ",".join(events) if isinstance(events, (dict, list)) else str(events),
                              ",".join(data.get("jobs", {})), json.dumps(filters, sort_keys=True), ",".join(refs)])
        if path.endswith(".tf"):
            for match in re.finditer(r'(?m)^\s*(resource|data|module)\s+"([^\"]+)"(?:\s+"([^\"]+)")?', text):
                resources.append([path, text[:match.start()].count("\n") + 1, match[1] + ":" + match[2], match[3] or "-"])
        for match in re.finditer(r"(?m)^(?:async )?def (lambda_handler|handler|main)\(", text):
            declarations.append([path, text[:match.start()].count("\n") + 1, "python-entry", match[1]])
        if Path(path).name == "package.json":
            data = json.loads(text)
            for category in ("scripts", "bin"):
                values = data.get(category, {})
                for name in sorted(values if isinstance(values, dict) else [category]):
                    declarations.append([path, 1, "package-" + category, name])
        if Path(path).name == "pyproject.toml":
            project = tomllib.loads(text).get("project", {})
            for category in ("scripts", "gui-scripts", "entry-points"):
                for name in sorted(project.get(category, {})):
                    declarations.append([path, 1, "python-package-" + category, name])
        if path.endswith(".py") and ("/alembic/versions/" in path or "/migrations/versions/" in path):
            values = {}
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision", "depends_on"}:
                            values[target.id] = ast.literal_eval(node.value)
            if "revision" in values:
                revision = values["revision"]
                if revision in migrations:
                    raise ValueError("Duplicate migration revision: " + revision)
                migrations[revision] = (path, values.get("down_revision"), values.get("depends_on"))
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("git cat-file failed")
    ordered = [["path", "revision", "parents", "dependencies"]]
    pending, seen, parents = dict(migrations), set(), set()
    while pending:
        ready = []
        for revision, (_, parent, dependency) in pending.items():
            dependencies = set()
            for value in (parent, dependency):
                dependencies.update(value if isinstance(value, (tuple, list)) else [value] if value else [])
            if dependencies <= seen:
                ready.append(revision)
        if not ready:
            raise ValueError("Unresolved migration graph: " + str(sorted(pending)))
        for revision in sorted(ready):
            path, parent, dependency = pending.pop(revision)
            parents.update(parent if isinstance(parent, (tuple, list)) else [parent] if parent else [])
            seen.add(revision)
            ordered.append([path, revision, json.dumps(parent), json.dumps(dependency)])
    entries = [files[0]] + [row for row in files[1:] if row[-1] != "supporting-artifact"]
    summary = {"revision": REVISION, "tracked_artifacts": len(files) - 1,
               "entry_artifacts": len(entries) - 1, "workflows": len(workflows) - 1,
               "terraform_declarations": len(resources) - 1, "migration_count": len(migrations),
               "migration_heads": sorted(seen - parents), "unmapped_paths": [], "structural_gaps": gaps,
               "components": dict(sorted(Counter(row[2] for row in files[1:]).items())),
               "limits": ["static entry detection does not expand dynamic commands or external actions",
                          "SQL migration graph is not live schema or exhaustive SQL effect interpretation",
                          "excluded modules are enumerated, not semantically reviewed",
                          "provider, catalog, board access, live IAM and deployment state are not established"]}
    overlays = [["pr", "revision", "status", "path", "blob", "component", "disposition", "roles"]]
    for number, revision in OVERLAYS.items():
        base = OVERLAY_BASES[number]
        for change in SHARED.git("diff", "--name-status", "--no-renames", base, revision).decode().splitlines():
            status, path = change.split("\t", 1)
            if status == "D":
                overlays.append([number, revision, status, path, "-", *classify(path), "deleted-in-overlay"])
                continue
            metadata = SHARED.git("ls-tree", revision, "--", path).decode().split()
            content = SHARED.git("show", revision + ":" + path).decode(errors="replace")
            overlays.append([number, revision, status, path, metadata[2], *classify(path),
                             SHARED.roles(path, content, metadata[0] == "100755")])
    summary["overlay_changed_paths"] = len(overlays) - 1
    summary["overlay_revisions"] = OVERLAYS
    summary["overlay_bases"] = OVERLAY_BASES
    delta = [["revision", "status", "path", "blob", "component", "disposition", "roles"]]
    for change in SHARED.git("diff", "--name-status", "--no-renames", REVISION, FOLLOWUP_MAIN).decode().splitlines():
        status, path = change.split("\t", 1)
        if status == "D":
            delta.append([FOLLOWUP_MAIN, status, path, "-", *classify(path), "deleted-in-followup"])
            continue
        metadata = SHARED.git("ls-tree", FOLLOWUP_MAIN, "--", path).decode().split()
        content = SHARED.git("show", FOLLOWUP_MAIN + ":" + path).decode(errors="replace")
        delta.append([FOLLOWUP_MAIN, status, path, metadata[2], *classify(path),
                      SHARED.roles(path, content, metadata[0] == "100755")])
    summary["followup_main"] = FOLLOWUP_MAIN
    summary["followup_changed_paths"] = len(delta) - 1
    summary["followup_tracked_artifacts"] = len(SHARED.git("ls-tree", "-r", "--name-only", FOLLOWUP_MAIN).decode().splitlines())
    return {"main-delta.tsv": SHARED.table(delta), "reviewed-overlays.tsv": SHARED.table(overlays), "tracked-artifacts.tsv": SHARED.table(files), "entry-points.tsv": SHARED.table(entries),
            "workflows.tsv": SHARED.table(workflows), "resources.tsv": SHARED.table(resources),
            "migrations.tsv": SHARED.table(ordered), "entry-declarations.tsv": SHARED.table(declarations),
            "inventory-summary.json": json.dumps(summary, indent=2) + "\n"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = generate()
    if args.check:
        for name, text in outputs.items():
            if not (HOME / name).exists() or (HOME / name).read_text() != text:
                raise SystemExit("Inventory differs: " + name)
    else:
        for name, text in outputs.items():
            (HOME / name).write_text(text)
    print("Pinned inventory " + ("verified" if args.check else "generated"))


if __name__ == "__main__":
    main()

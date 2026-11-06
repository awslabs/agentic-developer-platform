"""Reproduce the #8394 source inventory without running application/deployment code."""

import argparse
import ast
import csv
import io
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REVISION = "d8e8a2a5c5e8e540f243e4aee77dcf54b3689ecc"
GLOBAL = re.compile(r"BG_ORCH_DISPATCH_REPO|orchestration_dispatch_repo|orch_dispatch_repo|config\.repo|configured_repo|resolve_installation_id")
IDENTITY = re.compile(r"repository_identity|provider_repository_id|repo_scope|source_ref|repository-evaluation/v1|orchestration-evaluation/v1")


def component(path):
    if path.startswith("modules/gateway/src/orchestration/"):
        return "engine", "review", "MR-01..12; graph, lifecycle and evidence consumers"
    if path.startswith(("modules/gateway/src/agentauth/", "modules/gateway/src/admin/connections/", "modules/gateway/src/admin/installations/", "modules/gateway/src/knowledge/github", "modules/gateway/src/auth/", "modules/gateway/src/shared/models/")):
        return "identity-access", "support", "MR-02..06; existing authority and tenant boundaries"
    if path.startswith("modules/gateway/alembic/"):
        return "schema", "review", "MR-01,06,12; full migration lineage; preserve unrelated schema"
    if path.startswith("modules/gateway/frontend/"):
        return "frontend", "support", "MR-08; orchestration views change; other views regression-only"
    if path.startswith(("modules/gateway/cli/", "modules/gateway/src/cli_download/")):
        return "cli", "support", "MR-07; flow authoring and packaged CLI; other commands unchanged"
    if path.startswith(("modules/gateway/infra/", "modules/gateway/k8s/", "modules/gateway/scripts/")) or path in {"modules/gateway/Dockerfile", "modules/gateway/pyproject.toml"}:
        return "gateway-release", "review", "MR-09,12; image, migrations, tick and configuration delivery"
    if path.startswith(("modules/gateway/tests/orchestration/", "modules/gateway/tests/agentauth/", "modules/gateway/tests/cli/")):
        return "gateway-tests", "review", "MR-01..12; regression and integration suites"
    if path.startswith("contracts/"):
        return "contracts", "support", "MR-03,04,09..11; shared contracts; unrelated versions unchanged"
    if path.startswith("tests/e2e/orchestration/"):
        return "qualification", "review", "MR-13,14; fixture lifecycle and scenario runner"
    if path.startswith("modules/agent-factory/"):
        return "worker-ingress", "support", "MR-03..06,09,12; broker/envelope/runtime packaging; personas unchanged"
    if path.startswith((".github/", "codebuild/", "platform/", "config/release-defaults/", "environments/", "scripts/")) or path == "deploy.sh":
        return "shared-release", "support", "MR-09,12,13; entry-point audit; unrelated deployments not changed"
    if path.startswith(("docs/", "aidlc-docs/")) or path.endswith((".md", ".rst")):
        return "documentation", "reference", "Historical/design guidance is evidence, not runtime authority"
    if path.startswith("modules/gateway/"):
        return "gateway-other", "excluded", "Proxy, metering and unrelated product behavior unchanged"
    if path.startswith("modules/"):
        return "module-" + path.split("/")[1], "excluded", "No new domain/context/harness topology; build links enumerated"
    if path.startswith(("tests/", "test/")):
        return "other-tests", "excluded", "Non-orchestration test behavior unchanged"
    return "repository-tooling", "excluded", "Root tooling/assets are not story routing; entry points enumerated"


def entry_kind(path, content):
    name = Path(path).name
    kinds = []
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        kinds.append("workflow")
    if "codebuild/" in path or "buildspec" in name:
        kinds.append("buildspec-or-build-support")
    if "Dockerfile" in name or name in {"Makefile", "Justfile", "Taskfile.yml", "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "Chart.yaml"}:
        kinds.append("build-or-package")
    if path.endswith(".tf"):
        kinds.append("terraform")
    if path.endswith((".yaml", ".yml")) and any(part in path for part in ("k8s/", "helm/", "manifests/", "docker-compose", "charts/")):
        kinds.append("runtime-manifest")
    if path.endswith((".sh", ".bash", ".ps1")) or (path.endswith((".py", ".js", ".ts", ".mjs", ".cjs")) and ("/scripts/" in f"/{path}" or "__main__" in content or "process.argv" in content)):
        kinds.append("script-or-cli")
    if re.search(r"^\s*(?:async )?def (?:lambda_handler|handler)\(", content, re.M):
        kinds.append("lambda-handler-candidate")
    if path.startswith(".github/actions/") and name in {"action.yml", "action.yaml"}:
        kinds.append("composite-action")
    return ";".join(kinds)


def trigger(content):
    match = re.search(r"^on:\s*\n(.*?)(?=^\S|\Z)", content, re.M | re.S)
    if not match:
        return "see source (inline/quoted/non-workflow entry)"
    block = match.group(1)
    events = re.findall(r"^  ([A-Za-z_]+):", block, re.M)
    filters = [name for name in ("paths", "paths-ignore", "branches", "branches-ignore", "tags", "schedule") if re.search(rf"\b{name}:", block)]
    return ",".join(events) + ("; filters=" + ",".join(filters) if filters else "")


def blobs(revision):
    listing = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", revision], cwd=ROOT)
    records = []
    for entry in listing.split(b"\0"):
        if entry:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            records.append((raw_path.decode(), oid, kind))
    records.sort()
    objects = [oid for _, oid, kind in records if kind == "blob"]
    output = subprocess.check_output(["git", "cat-file", "--batch"], input=("\n".join(objects) + "\n").encode(), cwd=ROOT)
    stream = io.BytesIO(output)
    for path, oid, kind in records:
        if kind != "blob":
            yield path, oid, "", kind
            continue
        header = stream.readline().decode().split()
        data = stream.read(int(header[2]))
        stream.read(1)
        if header[0] != oid:
            raise ValueError("Git object order changed")
        yield path, oid, data.decode("utf-8", errors="replace"), kind


def write_table(output, name, headings, rows):
    with (output / name).open("w", newline="") as target:
        writer = csv.writer(target, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(headings)
        writer.writerows(rows)


def generate(revision, output):
    output.mkdir(parents=True, exist_ok=True)
    inventory, entries, hits, migrations = [], [], [], []
    counts = {}
    for path, oid, content, kind in blobs(revision):
        owner, scope, rationale = component(path)
        entry = entry_kind(path, content)
        global_lines = [str(number) for number, line in enumerate(content.splitlines(), 1) if GLOBAL.search(line)]
        identity_lines = [str(number) for number, line in enumerate(content.splitlines(), 1) if IDENTITY.search(line)]
        inventory.append([path, oid, owner, scope, rationale, entry, ",".join(global_lines), ",".join(identity_lines)])
        counts[owner] = counts.get(owner, 0) + 1
        if entry:
            entries.append([path, owner, scope, entry, trigger(content) if "workflow" in entry else "see pinned source; not invoked by audit"])
        hits.extend([path, line, owner, scope, "global-config-or-resolver-consumer"] for line in global_lines)
        if path.startswith("modules/gateway/alembic/versions/") and path.endswith(".py"):
            tree = ast.parse(content)
            values = {}
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
            operations = sorted(set(re.findall(r'op\.(create_table|add_column|drop_column|drop_table|create_index|alter_column)\(\s*["\']([^"\']+)', content)))
            migrations.append([path, values.get("revision"), json.dumps(values.get("down_revision")), ";".join(f"{operation}:{table}" for operation, table in operations)])
    write_table(output, "artifacts.tsv", ["path", "git_blob", "component", "scope", "disposition", "entry_kind", "global_reference_lines", "identity_reference_lines"], inventory)
    write_table(output, "entry-points.tsv", ["path", "component", "scope", "kind", "trigger_summary"], entries)
    write_table(output, "global-readers.tsv", ["path", "line", "component", "scope", "evidence"], hits)
    write_table(output, "migrations.tsv", ["path", "revision", "down_revision", "literal_operations"], migrations)
    revisions = {row[1] for row in migrations if row[1]}
    parents = set()
    for row in migrations:
        parent = json.loads(row[2])
        parents.update(parent if isinstance(parent, list) else [parent] if parent else [])
    summary = {"source_revision": revision, "artifacts": len(inventory), "entry_points": len(entries), "global_references": len(hits), "global_files": len({row[0] for row in hits}), "migration_files": len(migrations), "migration_heads": sorted(revisions - parents), "missing_migration_parents": sorted(parents - revisions), "components": dict(sorted(counts.items()))}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.revision, arguments.output)

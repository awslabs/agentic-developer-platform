"""Reproduce the design's tracked-file, entry-point and migration inventories."""

import ast
import csv
import io
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import yaml


REVISION = "56a8450aa41cc661c52ad90a1e421b8a16aca7f8"
ROOT = Path(__file__).resolve().parents[3]
HOME = Path(__file__).resolve().parent


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def component(path):
    if path.startswith(".github/workflows/"):
        name = Path(path).name
        workflow_groups = (
            (("gateway-frontend-",), "gateway-ui", "extend-publisher"),
            (("gateway-infra-",), "gateway-infrastructure", "reuse-manual-infra-gates"),
            (("gateway-", "run-gateway-", "pricing-", "postgres-schema-", "github-auth-broker-"), "gateway-backend", "reuse-applicable-pipeline"),
            (("agent-context-", "gbrain-"), "knowledge-layer", "excluded-not-product-context-pipeline"),
            (("agent-", "chat-agent-", "arc-runner-", "webhook-", "codex-harness-", "aidlc-", "skill-agent"), "agent-factory", "reuse-where-producer-or-consumer-changes"),
            (("cyber-", "superplane-", "malware-", "yara-", "fixture-edge-"), "domain-apps", "excluded-separate-domain-deployment"),
            (("tools-", "shared-agentcore-"), "tools", "reuse-existing-credential-tool-dependencies"),
            (("insights-",), "insights", "peer-owned-no-analytics-implementation"),
            (("platform-", "adp-", "spawn-deploy-", "undeploy"), "shared-platform", "reuse-applicable-release-and-evaluation"),
            (("eval-", "e2e-", "nightly-cli-", "credential-", "gitlab-integration-", "orchestration-"), "cross-component-tests", "reuse-applicable-evaluations"),
            (("dev-box-", "harness-", "gitlab-infra-"), "adjacent-runtime", "excluded-no-profile-deployment-change"),
        )
        for prefixes, owner, disposition in workflow_groups:
            if name.startswith(prefixes):
                return owner, disposition
        return "repository-automation", "excluded-independent-maintenance-or-reuse-checks"
    special = (
        ("modules/gateway/frontend/", "gateway-ui", "extend"),
        ("modules/gateway/cli/", "gateway-cli", "extend"),
        ("modules/gateway/alembic/", "gateway-database", "extend"),
        ("modules/gateway/infra/", "gateway-infrastructure", "reuse"),
        ("modules/gateway/k8s/", "gateway-runtime", "reuse"),
        ("modules/gateway/lambda/", "gateway-identity-budget-lambdas", "reuse-no-profile-storage"),
        ("modules/gateway/", "gateway-backend", "inspect-reuse-extend"),
        ("modules/agent-factory/", "agent-factory", "reuse-adapter-no-new-executor"),
        ("modules/agent-context/", "knowledge-layer", "excluded-not-governed-project-registry"),
        ("modules/insights/", "insights", "peer-owned-no-analytics-implementation"),
        ("modules/engineering-insights/", "insights", "peer-owned-no-analytics-implementation"),
        ("modules/tools/", "tools", "reuse-credential-connector-contracts"),
        ("modules/source-control/", "source-control", "reuse-provider-identity"),
        ("modules/harness/", "harness", "reuse-authenticated-agent-transport"),
        ("modules/platform-deploy-mgmt/", "platform-deploy-management", "reuse-release-path-only"),
        ("modules/user-services/", "user-services", "excluded-personal-services-not-product-storage"),
        ("modules/research/", "research", "excluded-experimental-not-product-runtime"),
        ("modules/domain-apps/", "domain-apps", "excluded-domain-execution-not-onboarding"),
        ("platform/", "shared-platform", "reuse-no-new-cloud-resources"),
        ("codebuild/", "shared-builds", "reuse-component-builds"),
        (".github/", "repository-automation", "map-entry-points"),
        ("tests/", "cross-component-tests", "extend-applicable-evaluations"),
        ("scripts/", "repository-tooling", "reuse-applicable-checks"),
        ("contracts/", "shared-contracts", "reuse-lineage-and-auth"),
        ("libs/", "shared-libraries", "reuse-no-new-runtime"),
        ("docs/product-onboarding/", "product-onboarding-design", "requirements-and-design"),
        ("docs/product-context/", "product-context-design", "reuse-typed-contract-not-runtime"),
        ("docs/", "documentation", "reference-not-runtime"),
        ("doc/", "documentation", "reference-not-runtime"),
        ("aidlc/", "planning", "reference-not-runtime"),
        ("aidlc-docs/", "planning", "reference-not-runtime"),
        ("agent_learning/", "historical-notes", "excluded-not-runtime-or-current-evidence"),
        ("experiments/", "experiments", "excluded-not-supported-product-runtime"),
        ("demos/", "demonstrations", "excluded-not-production-entry-point"),
        ("examples/", "examples", "excluded-not-production-entry-point"),
        ("environments/", "deployment-configuration", "private-config-no-values-copied"),
        ("config/", "deployment-configuration", "private-config-no-values-copied"),
        ("data/", "data-artifacts", "excluded-no-new-profile-data-store"),
        ("apps/", "apps", "excluded-separate-applications"),
        ("cli/", "legacy-root-cli", "excluded-use-gateway-installed-cli"),
    )
    for prefix, owner, disposition in special:
        if path.startswith(prefix):
            return owner, disposition
    if "/" not in path or path.startswith((".adp/", ".adp-rules/", ".claude/", ".kiro/")):
        return "repository-root", "reference-or-launcher-no-profile-runtime"
    raise ValueError(f"Unclassified tracked path: {path}")


def roles(path, text):
    name = Path(path).name
    result = []
    if path.startswith(".github/workflows/"):
        result.append("workflow")
    if name in {"action.yml", "action.yaml"}:
        result.append("action")
    if "Dockerfile" in name or name in {"Makefile", "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "Chart.yaml"} or "buildspec" in name or path.startswith("codebuild/"):
        result.append("build-package")
    template = path.endswith((".yaml", ".yml", ".json", ".template")) and ("AWSTemplateFormatVersion" in text or re.search(r"^kind:\s*(Deployment|Job|CronJob|Service|Namespace|ConfigMap|Secret|ClusterRole|Role|Ingress|ScaledJob)\b", text, re.MULTILINE))
    if path.endswith((".tf", ".tfvars")) or "/k8s/" in path or "/helm/" in path or "compose" in name or "kustomization" in name or template:
        result.append("deployment-definition")
    script_directory = set(Path(path).parts) & {"scripts", "script", "cli", "bin"}
    if path.endswith((".sh", ".bash", ".ps1")) or text.startswith("#!") or re.search(r"if __name__\s*==\s*['\"]__main__", text) or (script_directory and path.endswith((".py", ".js", ".mjs", ".cjs", ".ts"))):
        result.append("executable-entry")
    if "/alembic/versions/" in path or "/migrations/" in path:
        result.append("migration")
    if "test" in path.lower() or "/fixtures/" in path:
        result.append("test-or-fixture")
    if path.endswith((".md", ".rst")):
        result.append("documentation")
    return ",".join(result) or "supporting-artifact"


def table(rows):
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerows([[value if value != "" else "-" for value in row] for row in rows])
    return output.getvalue()


def inventories():
    paths = git("ls-tree", "-r", "--name-only", REVISION).decode().splitlines()
    blobs = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    files = [["path", "component", "disposition", "roles"]]
    entries = [files[0]]
    workflows = [["path", "events", "jobs", "local_dependencies"]]
    migrations = {}
    for path in paths:
        blobs.stdin.write(f"{REVISION}:{path}\n".encode())
        blobs.stdin.flush()
        header = blobs.stdout.readline().decode().split()
        body = blobs.stdout.read(int(header[2]))
        blobs.stdout.read(1)
        text = body.decode(errors="replace")
        role = roles(path, text)
        row = [path, *component(path), role]
        files.append(row)
        if set(role.split(",")) & {"workflow", "action", "build-package", "deployment-definition", "executable-entry", "migration"}:
            entries.append(row)
        if "workflow" in role:
            data = yaml.safe_load(text)
            events = data.get("on", data.get(True, {}))
            event_names = sorted(events) if isinstance(events, dict) else events if isinstance(events, list) else [events]
            refs = sorted(set(re.findall(r"(?:uses:\s*|(?:python3?|bash|sh)\s+)(\./[^\s\"']+|(?:\.github|scripts|modules|platform|tests|codebuild)/[^\s\"']+)", text)))
            workflows.append([path, ",".join(str(value) for value in event_names), ",".join(data.get("jobs", {})), ",".join(refs)])
        if path.startswith("modules/gateway/alembic/versions/") and path.endswith(".py"):
            tree = ast.parse(text)
            metadata = {}
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision", "depends_on"}:
                            metadata[target.id] = ast.literal_eval(node.value)
            revision = metadata.get("revision")
            if not revision:
                raise ValueError(f"Migration metadata missing: {path}")
            operations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "op":
                    target = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else "dynamic-see-source"
                    operations.append(f"{node.lineno}:{node.func.attr}:{target}" if node.func.attr != "execute" else f"{node.lineno}:execute:SQL-see-source")
            migrations[revision] = dict(path=path, parents=metadata["down_revision"], dependencies=metadata.get("depends_on"), operations=operations)
    blobs.stdin.close()
    blobs.wait()
    ordered = []
    pending = dict(migrations)
    seen = set()
    parents_seen = set()
    while pending:
        ready = []
        for revision, value in sorted(pending.items()):
            parents = value["parents"]
            parents = [] if parents is None else [parents] if isinstance(parents, str) else list(parents)
            dependencies = value["dependencies"]
            dependencies = [] if dependencies is None else [dependencies] if isinstance(dependencies, str) else list(dependencies)
            if set(parents + dependencies) <= seen:
                ready.append(revision)
                parents_seen.update(parents)
                ordered.append([value["path"], revision, ",".join(parents), ",".join(dependencies), ";".join(value["operations"])])
        if not ready:
            raise ValueError(f"Unresolved migration graph: {sorted(pending)}")
        for revision in ready:
            seen.add(revision)
            del pending[revision]
    summary = dict(revision=REVISION, tracked_files=len(paths), entry_points=len(entries)-1, workflows=len(workflows)-1, migrations=len(migrations), migration_heads=sorted(seen-parents_seen), components=dict(sorted(Counter(row[1] for row in files[1:]).items())), unclassified_paths=[])
    return {
        "tracked-artifacts.tsv": table(files),
        "entry-points.tsv": table(entries),
        "workflows.tsv": table(workflows),
        "migrations.tsv": table([["path", "revision", "parents", "dependencies", "operations_including_downgrade"]] + ordered),
        "inventory-summary.json": json.dumps(summary, indent=2) + "\n",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", action="store_true", help="Emit apply_patch input for initial artifact creation")
    parser.add_argument("--write", action="store_true", help="Regenerate the mechanical audit artifacts")
    args = parser.parse_args()
    output = inventories()
    if args.write:
        for name, content in output.items():
            (HOME / name).write_text(content)
        print(output["inventory-summary.json"])
    elif args.patch:
        print("*** Begin Patch")
        for name, content in output.items():
            print(f"*** Add File: docs/product-onboarding/design/{name}")
            print("\n".join("+" + line for line in content.splitlines()))
        print("*** End Patch")
    else:
        for name, content in output.items():
            if (HOME / name).read_text() != content:
                raise SystemExit(f"Inventory differs: {name}")
        print(json.dumps(json.loads(output["inventory-summary.json"]), indent=2))

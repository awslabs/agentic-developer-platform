"""Enumerate the complete pinned tree; retain names and source locations, not secrets."""

import argparse
import ast
import csv
import io
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HOME = Path(__file__).resolve().parent
REVISION = "31148f8edfda23f162cf4b4705b7820710621a66"
SPIKES = {
    "7984": "499eade9a8e1376a3f5bcfdcd61c16aa0bbf835d",
    "8022": "24338ad0eb533613df3354bf65e342bd5c12d2b9",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def classify(path):
    if path.startswith(".github/workflows/"):
        name = Path(path).name
        families = [
            (("gateway-frontend-",), "host-ui", "separate frontend publisher"),
            (("gateway-", "run-gateway-", "postgres-schema-", "github-auth-broker-"), "gateway", "reuse or extend named host integration; preserve other gateway jobs"),
            (("security-agent-nightly", "security-s14-"), "nightly-security", "excluded from modification: preserve nightly/spike ownership"),
            (("agent-context-", "gbrain-"), "context-runtime", "peer-owned; no L5 worker publication here"),
            (("agent-", "webhook-", "chat-agent-", "arc-", "codex-", "aidlc-", "skill-agent"), "agent-factory", "event/tool integration only; not L5 worker publisher"),
            (("tools-", "shared-agentcore-"), "tools", "reuse existing catalogue/transport publisher where changed"),
            (("adp-", "platform-", "spawn-deploy-", "undeploy"), "platform-release", "extend release manifest and launcher integration where named"),
            (("insights-",), "insights", "excluded: peer analytics delivery"),
            (("cyber-", "superplane-", "malware-", "yara-", "dev-box-", "gitlab-infra-"), "other-domain-runtime", "excluded: independent domain deployment"),
            (("_", "eval-", "e2e-", "credential-", "orchestration-", "nightly-cli-"), "shared-checks", "reuse applicable checks; no new L5 runtime owner"),
        ]
        for prefixes, component, disposition in families:
            if name.startswith(prefixes):
                return component, disposition
        return "repository-automation", "excluded: independent maintenance/check pipeline; not L5 publisher"
    groups = [
        ("modules/L5-services/", "l5", "design-only baseline"),
        ("modules/gateway/frontend/", "host-ui", "extend Product workspace"),
        ("modules/gateway/cli/", "host-cli", "extend installed client"),
        ("modules/gateway/alembic/", "database", "reuse migration runner; add L5 version location"),
        ("modules/gateway/infra/", "host-infra", "integrate L5-owned Terraform module"),
        ("modules/gateway/", "gateway", "reuse auth/API/artifact host; see architecture boundaries"),
        ("modules/tools/", "tools", "reuse broker/catalogue transport; no second L5 backend"),
        ("modules/harness/", "harness", "reuse authenticated tool/Task lineage"),
        ("modules/agent-factory/", "agent-factory", "reuse event and Task integration; no worker replacement"),
        ("modules/agent-context/", "context-runtime", "peer-owned; existing Door is not Product contribution API"),
        ("modules/insights/", "insights", "peer-owned; no generic finding ingestion"),
        ("modules/source-control/", "source-control", "reuse authorized source identity"),
        ("modules/platform-deploy-mgmt/", "release-management", "extend applicable release inventory only"),
        ("modules/", path.split("/")[1] if path.startswith("modules/") else "other-modules", "excluded: separate service/domain runtime"),
        (".github/workflows/", "workflow", "entry-by-entry metadata; see deployment change matrix"),
        (".github/scripts/code_review", "nightly-security", "reuse protocol; preserve nightly ownership"),
        (".github/scripts/normalize_security", "nightly-security", "adapt normalization without nightly configuration"),
        (".github/scripts/dedup_security", "nightly-security", "adapt fingerprint with tenant/scope fencing"),
        (".github/", "repository-automation", "excluded unless named host/release integration"),
        ("platform/", "platform", "reuse infrastructure/release orchestration; no new workflow engine"),
        ("codebuild/", "build", "inspect all build definitions; extend gateway packaging"),
        ("contracts/", "platform-contracts", "reuse authority/lineage; L5 schemas remain module-owned"),
        ("libs/", "shared-libraries", "reuse only named interfaces; no broad refactor"),
        ("scripts/", "repository-tooling", "entry inventory; preserve independent tools/spikes"),
        ("tests/", "cross-component-tests", "reuse applicable checks; exclude unrelated suites"),
        ("docs/", "documentation", "reference only; historical reports not current authority"),
        ("doc/", "documentation", "excluded: legacy documentation"),
        ("environments/", "private-deployment-inputs", "excluded values; operator supplies authorized configuration"),
    ]
    for prefix, component, disposition in groups:
        if path.startswith(prefix):
            return component, disposition
    if "/" not in path:
        return "repository-root", "entry inventory or repository metadata; named launchers integrated"
    return path.split("/")[0], "excluded: planning/example/data/editor artifact, not L5 runtime"


def roles(path, text, executable):
    name = Path(path).name
    result = []
    if path.startswith(".github/workflows/"):
        result.append("workflow")
    if name in {"action.yml", "action.yaml"}:
        result.append("action")
    if any(term in name.lower() for term in ("dockerfile", "buildspec", "makefile")) or name in {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "Chart.yaml"
    } or path.startswith("codebuild/"):
        result.append("build-package")
    if path.endswith((".tf", ".tfvars", ".tfvars.json", ".template")) or any(
        term in path for term in ("/k8s/", "/helm/", "compose", "kustomization")
    ) or "AWSTemplateFormatVersion" in text or re.search(
        r"^kind:\s*(Deployment|Job|CronJob|Service|Ingress|ScaledJob|StatefulSet|DaemonSet)\b", text, re.M
    ):
        result.append("deployment")
    if executable or text.startswith("#!") or path.endswith((".sh", ".bash", ".ps1")) or re.search(
        r"if __name__\s*==\s*['\"]__main__", text
    ) or (set(Path(path).parts) & {"scripts", "script", "cli", "bin"} and path.endswith((".py", ".js", ".ts", ".mjs", ".cjs"))):
        result.append("executable")
    if "/migrations/" in path or "/alembic/versions/" in path:
        result.append("migration")
    return ",".join(result) or "supporting-artifact"


def table(rows):
    output = io.StringIO()
    csv.writer(output, delimiter="\t", lineterminator="\n").writerows([[value if value != "" else "-" for value in row] for row in rows])
    return output.getvalue()


def inventories():
    entries = git("ls-tree", "-r", REVISION).decode().splitlines()
    files = [["path", "blob", "component", "disposition", "roles"]]
    workflows = [["path", "events", "jobs", "path_filters", "local_dependencies"]]
    resources = [["path", "line", "kind", "logical_name"]]
    migrations = {}
    findings = []
    process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for entry in entries:
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if kind != "blob":
            findings.append({"path": path, "gap": "external gitlink content not expanded"})
            files.append([path, blob, *classify(path), "external-gitlink"])
            continue
        process.stdin.write((blob + "\n").encode())
        process.stdin.flush()
        size = int(process.stdout.readline().split()[2])
        body = process.stdout.read(size)
        process.stdout.read(1)
        text = "" if b"\0" in body else body.decode(errors="replace")
        role = roles(path, text, mode == "100755")
        files.append([path, blob, *classify(path), role])
        if "workflow" in role:
            data = yaml.safe_load(text)
            events = data.get("on", data.get(True, {}))
            filters = {}
            if isinstance(events, dict):
                filters = {name: {key: value for key, value in (settings or {}).items() if key in {"paths", "paths-ignore", "branches"}}
                           for name, settings in events.items() if isinstance(settings, dict)}
            references = sorted(set(re.findall(r"(?:uses:\s*|(?:python3?|bash|sh)\s+)(\./[^\s\"']+|(?:\.github|scripts|modules|platform|tests|codebuild)/[^\s\"']+)", text)))
            workflows.append([path, ",".join(events) if isinstance(events, (dict, list)) else str(events), ",".join(data.get("jobs", {})), json.dumps(filters, sort_keys=True), ",".join(references)])
        if path.endswith(".tf"):
            for match in re.finditer(r'(?m)^\s*(resource|data|module)\s+"([^\"]+)"(?:\s+"([^\"]+)")?', text):
                resources.append([path, text[:match.start()].count("\n") + 1, match[1] + ":" + match[2], match[3] or "-"])
        if path.startswith("modules/gateway/alembic/versions/") and path.endswith(".py"):
            tree = ast.parse(text)
            values = {}
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision", "depends_on"}:
                            values[target.id] = ast.literal_eval(node.value)
            operations = []
            for function in tree.body:
                if isinstance(function, ast.FunctionDef) and function.name == "upgrade":
                    for node in ast.walk(function):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "op":
                            names = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
                            if node.func.attr == "execute":
                                names = ["SQL: inspect source"]
                            operations.append(f"{node.lineno}:{node.func.attr}:{','.join(names)}")
            migrations[values["revision"]] = [path, values.get("down_revision"), values.get("depends_on"), ";".join(operations)]
    process.stdin.close()
    process.wait()
    ordered = [["path", "revision", "parents", "dependencies", "upgrade_operations"]]
    pending, seen, parents = dict(migrations), set(), set()
    while pending:
        ready = []
        for revision, (_, parent, dependency, _) in pending.items():
            dependencies = set()
            for value in (parent, dependency):
                dependencies.update(value if isinstance(value, (tuple, list)) else [value] if value else [])
            if dependencies <= seen:
                ready.append(revision)
        if not ready:
            raise ValueError("Unresolved migration DAG: " + str(sorted(pending)))
        for revision in sorted(ready):
            path, parent, dependency, operations = pending.pop(revision)
            parents.update(parent if isinstance(parent, (tuple, list)) else [parent] if parent else [])
            ordered.append([path, revision, json.dumps(parent), json.dumps(dependency), operations])
            seen.add(revision)
    summary = {"revision": REVISION, "tracked_files": len(files) - 1, "entry_points": sum(row[-1] != "supporting-artifact" for row in files[1:]),
               "workflows": len(workflows) - 1, "migration_count": len(migrations), "migration_heads": sorted(seen - parents),
               "terraform_declarations": len(resources) - 1, "components": dict(sorted(Counter(row[2] for row in files[1:]).items())),
               "unmapped_paths": [], "structural_gaps": findings,
               "semantic_limits": ["dynamic shell dispatch and external actions are not expanded", "raw migration SQL requires source review", "registered live catalog and deployed/provider state not queried"]}
    return {"tracked-artifacts.tsv": table(files), "entry-points.tsv": table([files[0]] + [row for row in files[1:] if row[-1] != "supporting-artifact"]),
            "workflows.tsv": table(workflows), "resources.tsv": table(resources), "migrations.tsv": table(ordered),
            "inventory-summary.json": json.dumps(summary, indent=2) + "\n"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in inventories().items():
        path = HOME / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit(f"Inventory differs: {path}")
        else:
            path.write_text(content)
    print("Complete pinned-tree inventory " + ("verified" if args.check else "generated"))


if __name__ == "__main__":
    main()

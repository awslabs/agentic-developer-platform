"""Reproduce the read-only source inventory for the DevLake architecture review."""

import argparse
import ast
import csv
import io
import re
import subprocess
from collections import Counter
from pathlib import Path


ADP_REVISION = "aeba89070ec685b41a4902a4441bfe47449e48ba"
UPSTREAM_REVISION = "6502e549f58eb40c40ccbcd39aab9bc8eea6616f"
SIGNALS = {
    "container-build": r"\b(?:docker|podman|buildah|buildx|kaniko)\b",
    "cluster-or-infra": r"\b(?:terraform|kubectl|helm|cloudformation|aws_cloudformation)\b",
    "artifact-publication": r"\b(?:codebuild|buildspec|ecr|publish-shared-image)\b",
    "program-entry": r"__name__\s*==\s*['\"]__main__|^func main\(|^#!",
    "database-client": r"sql\.Open\(|gorm\.Open\(|NewGormDb\(|MakeDbConnection\(|create_engine\(|create_async_engine\(|async_engine_from_config\(|psycopg|pgxpool|generate_db_auth_token|POSTGRES_PASSWORD|DB_URL",
}


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def classify(path, upstream=False):
    if upstream:
        if path.startswith(("backend/plugins/github/", "backend/plugins/jira/")):
            return "selected-provider", "evaluate", "actual Go provider code and native fixtures"
        if path.startswith(("backend/core/", "backend/helpers/", "backend/models/")):
            return "upstream-core", "evaluate", "extraction/DAL/model dependency; not wholesale reuse"
        if path.startswith(("grafana/", "config-ui/", "backend/python/")):
            return "excluded-runtime", "exclude", "not deployed; separate DB-client qualification if added"
        if path.startswith("backend/plugins/"):
            return "other-provider", "exclude", "outside selected two-tool pilot"
        return "upstream-build-and-reference", "audit", "source/build closure or reference; not deployment authority"
    if path.startswith("modules/insights/") or path.startswith("docs/engineering-insights/"):
        return "insights", "reuse-or-extend", "S03-S08/S12-S13 and shared Insights ownership"
    if path.startswith("modules/connectors/devlake/"):
        return "connector", "extend", "generic provider library and tenant discovery"
    if path.startswith("modules/gateway/"):
        if any(part in path for part in ("/products/", "/product_onboarding/", "/catalog/", "/insights_results/", "adp-product", "adp-onboard", "adp-insights", "Product", "Onboarding", "product", "onboarding")):
            return "profile-insights-integration", "reuse-or-extend", "Profile transactions/source binding or bounded read integration"
        if any(part in path for part in ("/alembic/", "/shared/", "/auth/", "/internal/credential", "/admin/org_connections/", "/infra/", "/k8s/", "/scripts/", "/Dockerfile")):
            return "gateway-shared-authority", "audit", "existing identity/credential/database/build boundary; no ownership transfer"
        return "gateway-other", "exclude", "not proposed POC application behavior; release dependency indexed"
    if path.startswith(("platform/", "codebuild/", ".github/")) or path == "deploy.sh":
        return "platform-build-deployment", "audit", "shared build/infra/release boundary; only selected entry points change"
    if path.startswith(("docs/product-onboarding/", "docs/product-context/", "docs/adp-platform-deployment/", "docs/user-identity")):
        return "shared-design", "reuse", "Profile/identity/deployment contract; no competing baseline"
    if path.startswith(("tests/e2e/product_onboarding/", "tests/regression/", "contracts/")):
        return "contracts-and-acceptance", "audit", "existing contract/catalog and integration evidence boundary"
    if path.startswith("modules/"):
        return "/".join(path.split("/")[:2]), "exclude", "other module; no POC behavior change or borrowed deployment authority"
    if path.startswith(("docs/", "aidlc/", ".adp/", "plans/")):
        return "other-planning-record", "exclude", "outside POC normative design; history remains unchanged"
    if path.startswith(("scripts/", "tests/", "apps/")):
        return path.split("/")[0], "audit", "repository utility/test/application; candidate entry points enumerated"
    return "repository-support", "exclude", "root tooling/assets/config; no POC application ownership"


def tsv(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(tuple("-" if value == "" else value for value in row) for row in rows)
    return stream.getvalue()


def inventory(root, revision, upstream=False):
    tree = git(root, "ls-tree", "-r", "-z", revision).decode().split("\0")
    existing = {row.split("\t", 1)[1] for row in tree if row}
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    artifacts, entries, migrations, clients = [], [], [], []
    for item in sorted(filter(None, tree), key=lambda value: value.split("\t", 1)[1]):
        metadata, path = item.split("\t", 1)
        mode, kind, object_id = metadata.split()
        component, disposition, reason = classify(path, upstream)
        text = ""
        if kind == "blob":
            process.stdin.write((object_id + "\n").encode())
            process.stdin.flush()
            size = int(process.stdout.readline().split()[2])
            payload = process.stdout.read(size)
            process.stdout.read(1)
            if b"\0" not in payload:
                text = payload.decode("utf-8", errors="replace")
        artifacts.append((path, object_id, mode, component, disposition, reason))
        suffix = Path(path).suffix.lower()
        name = Path(path).name.lower()
        prose = suffix in {".md", ".rst", ".txt", ".svg", ".csv", ".tsv", ".lock"}
        categories = []
        if mode == "100755" or suffix in {".sh", ".bash", ".ps1", ".tf", ".tfvars", ".yaml", ".yml"}:
            categories.append("executable-or-declarative:1")
        if any(token in name for token in ("dockerfile", "makefile", "buildspec", "compose")) or name in {"package.json", "pyproject.toml", "go.mod", "cargo.toml", "justfile", "procfile", "chart.yaml", "taskfile.yml"}:
            categories.append("build-or-package-manifest:1")
        if not prose:
            for label, pattern in SIGNALS.items():
                match = re.search(pattern, text, re.M | re.I)
                if match:
                    line = text.count("\n", 0, match.start()) + 1
                    categories.append(f"{label}:{line}")
                    if label == "database-client":
                        clients.append((path, line, object_id, component, disposition, reason))
        if categories:
            references = sorted(set(re.findall(r"(?:modules|platform|scripts|codebuild|\.github)/[\w./-]+", text)))
            resolved = [reference for reference in references if reference.rstrip("./") in existing]
            entries.append((path, object_id, component, disposition, ";".join(categories), ";".join(resolved)))
        if not upstream and path.startswith("modules/gateway/alembic/versions/") and suffix == ".py":
            module = ast.parse(text)
            values = {}
            for node in module.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
            operations = []
            for function in module.body:
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name == "upgrade":
                    for node in ast.walk(function):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "op":
                            table = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and node.func.attr != "execute" else "dynamic-or-SQL"
                            operations.append(f"{node.lineno}:{node.func.attr}:{table}")
            parents = values.get("down_revision")
            if isinstance(parents, str):
                parents = [parents]
            migrations.append((path, values.get("revision", ""), ";".join(parents or []), ";".join(operations), object_id))
    process.stdin.close()
    process.wait()
    prefix = "upstream-" if upstream else ""
    result = {
        prefix + "tracked-artifacts.tsv": tsv(("path", "git_blob", "mode", "component", "disposition", "reason"), artifacts),
        prefix + "entrypoints.tsv": tsv(("path", "git_blob", "component", "disposition", "static_signals_and_lines", "literal_existing_file_references"), entries),
        prefix + "database-clients.tsv": tsv(("path", "signal_line", "git_blob", "component", "disposition", "reason"), clients),
    }
    if not upstream:
        if any(not row[1] for row in migrations) or len({row[1] for row in migrations}) != len(migrations):
            raise ValueError("missing or duplicate migration revision")
        pending = {row[1]: set(filter(None, row[2].split(";"))) for row in migrations}
        missing = set().union(*pending.values()) - pending.keys()
        if missing:
            raise ValueError(f"missing migration parents: {sorted(missing)}")
        ordered = []
        while pending:
            ready = sorted(current_revision for current_revision, parents in pending.items() if not parents.intersection(pending))
            if not ready:
                raise ValueError("migration cycle")
            ordered.extend(ready)
            for current_revision in ready:
                del pending[current_revision]
        migrations.sort(key=lambda row: ordered.index(row[1]))
        result["migrations.tsv"] = tsv(("path", "revision", "parents", "upgrade_operations", "git_blob"), migrations)
    counts = Counter(row[3] for row in artifacts)
    print(f"{revision}: {len(artifacts)} artifacts, {len(entries)} entry candidates, {len(clients)} DB candidates, {len(migrations)} migrations; components={dict(sorted(counts.items()))}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    root = directory.parents[2]
    outputs = inventory(root, ADP_REVISION)
    if args.upstream_root:
        outputs.update(inventory(args.upstream_root, UPSTREAM_REVISION, upstream=True))
    for name, content in outputs.items():
        path = directory / name
        if args.check:
            if path.read_text() != content:
                raise SystemExit(f"inventory differs: {name}")
        else:
            path.write_text(content)


if __name__ == "__main__":
    main()

"""Reproduce the design inventory without executing repository code."""

import argparse
import ast
import csv
import io
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

BASELINE = "fc5fe6f21100df5d49c29f4c3a882907ff8e659e"
ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent
GATEWAY_REFERENCE = re.compile(
    r"modules/gateway|bedrockgateway|bs-gateway-build|gateway-rollout|"
    r"gateway-deploy|budget-usage-tracker|tests/performance/gateway"
)
EXECUTION = re.compile(
    r"kubectl\s|terraform\s|docker\s+(?:build|push)|buildx\s|"
    r"aws\s+(?:codebuild|lambda|eks|cloudformation)\s|alembic\s|"
    r"publish-shared-image|deploy-all\.sh|workflow_dispatch|workflow_call|"
    r"helm\s|cdk\s|pulumi\s|tofu\s|npm\s+run\s+build|pnpm\s+build|"
    r"uv\s+build|python\S*\s+-m\s+build|cargo\s+build|dotnet\s+publish"
)


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def component(path):
    parts = path.split("/")
    if path.startswith("modules/gateway/"):
        if len(parts) > 3 and parts[2] in {"src", "tests", "infra", "lambda"}:
            return "gateway/" + "/".join(parts[2:4])
        return "gateway/" + parts[2]
    if path.startswith("tests/performance/gateway/"):
        return "performance-harness"
    if path.startswith("platform/infra/"):
        return "shared-platform-infra"
    if path.startswith("platform/scripts/") or path in {"deploy.sh", "teardown.sh"}:
        return "platform-launcher"
    if path.startswith(".github/"):
        return "ci/" + (parts[1] if len(parts) > 1 else "root")
    if path.startswith("codebuild/"):
        return "image-build"
    if path.startswith("modules/"):
        return "/".join(parts[:2])
    return parts[0] if len(parts) > 1 else "repository-root"


def signals(path, text):
    found = []
    filename = Path(path).name.lower()
    if path.startswith(".github/workflows/"):
        found.append("workflow")
    if filename in {"action.yml", "action.yaml"}:
        found.append("action")
    if path.endswith((".sh", ".bash", ".ps1")):
        found.append("shell-entry")
    if "dockerfile" in filename or "buildspec" in path or path.startswith("codebuild/"):
        found.append("build")
    if filename in {
        "package.json",
        "pyproject.toml",
        "makefile",
        "justfile",
        "taskfile.yml",
    }:
        found.append("package-entry")
    if path.endswith((".tf", ".tfvars", ".tfvars.example", ".hcl")):
        found.append("infra")
    if "/k8s/" in path or "docker-compose" in filename or filename == "chart.yaml":
        found.append("runtime-manifest")
    if any(
        word in filename
        for word in (
            "deploy",
            "rollout",
            "release",
            "migration",
            "bootstrap",
            "teardown",
        )
    ):
        found.append("lifecycle-name")
    if EXECUTION.search(text):
        found.append("execution-reference")
    if GATEWAY_REFERENCE.search(text):
        found.append("gateway-reference")
    return found


def migration(path, text):
    tree = ast.parse(text)
    values = {}
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in {"revision", "down_revision"}
        ):
            values[node.target.id] = ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    values[target.id] = ast.literal_eval(node.value)
    parents = values.get("down_revision")
    parents = (
        list(parents) if isinstance(parents, tuple) else ([parents] if parents else [])
    )
    tables = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "create_table",
                "drop_table",
                "add_column",
                "drop_column",
                "alter_column",
                "rename_table",
            }
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            tables.add(node.args[0].value)
    return [
        path,
        values["revision"],
        ",".join(parents),
        ",".join(sorted(tables)) or "inspect-source",
    ]


def render_table(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def generate():
    entries = git("ls-tree", "-r", "-z", BASELINE).split(b"\0")
    files = []
    for entry in entries:
        if entry:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split()
            files.append((raw_path.decode(), mode, kind, object_id))
    object_ids = [entry[3] for entry in files if entry[2] == "blob"]
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input=("\n".join(object_ids) + "\n").encode(),
        capture_output=True,
        check=True,
    ).stdout
    contents = io.BytesIO(batch)
    rows = []
    migrations = []
    for path, mode, kind, object_id in files:
        text = ""
        if kind == "blob":
            header = contents.readline().decode().split()
            assert header[0] == object_id
            data = contents.read(int(header[2]))
            assert contents.read(1) == b"\n"
            if b"\0" not in data:
                text = data.decode("utf-8", errors="replace")
        found = signals(path, text)
        if mode == "100755":
            found.append("executable")
        owner = component(path)
        if path.startswith(("modules/gateway/", "tests/performance/gateway/")):
            scope = "component-audit"
        elif "gateway-reference" in found:
            scope = "integration-reference"
        elif found:
            scope = "entrypoint-audit"
        else:
            scope = "excluded-unrelated"
        rows.append([path, owner, scope, ",".join(found) or "none", mode])
        if (
            path.startswith("modules/gateway/alembic/versions/")
            and path.endswith(".py")
            and "down_revision" in text
        ):
            migrations.append(migration(path, text))
    revisions = {row[1] for row in migrations}
    assert len(revisions) == len(migrations)
    ordered = []
    seen = set()
    while len(ordered) < len(migrations):
        ready = [
            row
            for row in migrations
            if row[1] not in seen and set(filter(None, row[2].split(","))) <= seen
        ]
        if not ready:
            raise ValueError("Migration graph has missing parent or cycle")
        for row in sorted(ready):
            ordered.append(row)
            seen.add(row[1])
    parents = {parent for row in migrations for parent in row[2].split(",") if parent}
    summary = {
        "baseline": BASELINE,
        "tracked_artifacts": len(rows),
        "scopes": dict(sorted(Counter(row[2] for row in rows).items())),
        "signals": dict(
            sorted(
                Counter(
                    signal
                    for row in rows
                    for signal in row[3].split(",")
                    if signal != "none"
                ).items()
            )
        ),
        "migrations": len(migrations),
        "migration_heads": sorted(revisions - parents),
        "non_blob_entries": [
            path for path, mode, kind, object_id in files if kind != "blob"
        ],
        "unmapped_artifacts": 0,
    }
    return {
        "inventory.tsv": render_table(
            ["path", "component", "scope", "signals", "git_mode"], rows
        ),
        "migrations.tsv": render_table(
            ["path", "revision", "parents", "literal_table_operations"], ordered
        ),
        "inventory-summary.json": json.dumps(summary, indent=2) + "\n",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    for name, text in generate().items():
        destination = OUTPUT / name
        if arguments.check:
            if not destination.exists() or destination.read_text() != text:
                raise SystemExit(f"Inventory differs: {name}")
        else:
            destination.write_text(text)
    print("Inventory verified" if arguments.check else "Inventory generated")


if __name__ == "__main__":
    main()

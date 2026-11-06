"""Reproduce the design's static inventory without executing repository code."""

import argparse
import ast
import collections
import fnmatch
import io
from pathlib import Path
import re
import subprocess
import tarfile


BASE = "849f6163e59855c1554bc263e9ad96695730e9bd"
ROOT = Path(__file__).resolve().parents[3]
DESTINATION = Path(__file__).resolve().parent
MANIFEST = "modules/gateway/src/orchestration/manifests/orchestration-deployments.yaml"
PATH_REFERENCE = re.compile(r"(?:\./)?(?:modules|platform|codebuild|scripts|environments|contracts|\.github)/(?:[A-Za-z0-9_.*/-]+)")
BUILD_COMMAND = re.compile(r"\b(?:terraform|tofu|kubectl|helm|docker|podman|buildah|codebuild)\b|\baws\s+(?:lambda|cloudformation|s3|eks|ecr)\b")


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments])


def owner(path):
    if path in {"deploy.sh", "remove.sh", "status.sh"}:
        return "platform-lifecycle"
    special = (
        ("modules/gateway/frontend/", "gateway-frontend"),
        ("modules/gateway/alembic/", "gateway-migrations"),
        ("modules/gateway/cli/", "gateway-cli"),
        ("modules/gateway/infra/", "gateway-infra"),
        ("modules/gateway/lambda/", "gateway-lambdas"),
        ("modules/agent-factory/codex-", "agent-worker"),
        ("modules/agent-factory/agent-worker-image/", "agent-worker"),
        ("modules/agent-factory/agent/", "agent-worker-and-chat"),
        ("modules/agent-factory/rules/", "agent-worker-policy-assets"),
        ("modules/agent-factory/skills/", "agent-worker-skill-assets"),
        ("modules/agent-factory/webhook-ingress/", "agent-webhook"),
        ("modules/tools/task-sdk/", "tools-task-sdk-and-worker"),
        ("modules/tools/", "tools/"),
        ("platform/automation-infra/", "automation-infra"),
        ("platform/infra/", "shared-platform-infra"),
        ("platform/scripts/", "platform-lifecycle"),
        (".github/", "repository-automation"),
        ("codebuild/", "build-system"),
        ("environments/", "deployment-configuration"),
        ("contracts/", "shared-contracts"),
    )
    for prefix, component in special:
        if path.startswith(prefix):
            if component.endswith("/"):
                parts = path.split("/")
                return component + parts[2] if len(parts) > 3 else "tools-platform" if parts[-1] in {"deploy.sh", "orchestrate.sh"} else "tools-common"
            return component
    parts = path.split("/")
    if parts[0] == "modules":
        return "/".join(parts[:3]) if parts[1] in {"domain-apps", "harness", "user-services"} else parts[1]
    return "repository-root" if len(parts) == 1 else parts[0]


def disposition(path):
    if path.startswith(("docs/", "doc/", "aidlc/", "aidlc-docs/", "agent_learning/", ".adp/")):
        return "excluded-design-or-record;not-runtime-authority"
    if "/tests/" in path or path.startswith("tests/") or "/test/" in path or ".test." in path or path.endswith(".tftest.hcl"):
        return "validation;not-a-release-target"
    if Path(path).name in {"README.md", "AGENTS.md", "CLAUDE.md", "LICENSE", "NOTICE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"}:
        return "excluded-contributor-documentation"
    return "component-owned;release-selection-review-required"


def surface(path, text):
    name = Path(path).name
    kinds = []
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        kinds.append("workflow")
    if path.startswith(".github/actions/") and name in {"action.yml", "action.yaml"}:
        kinds.append("composite-action")
    if "Dockerfile" in name or name.startswith("Containerfile"):
        kinds.append("container-build")
    if path.startswith("codebuild/") or "buildspec" in name:
        kinds.append("buildspec")
    if name in {"package.json", "pyproject.toml", "Makefile", "makefile", "Justfile", "Taskfile.yml", "Cargo.toml", "go.mod", "setup.py", "setup.cfg"}:
        kinds.append("package-or-build-entry")
    if path.endswith(".tf"):
        kinds.append("terraform-definition")
    if path.endswith((".yml", ".yaml")) and re.search(r"^kind:\s*\S", text, re.M):
        kinds.append("kubernetes-or-manifest")
    if path.endswith((".sh", ".ps1", ".bash")) or (text.startswith("#!") and not path.endswith(".md")):
        kinds.append("script-entry")
    if not path.endswith((".md", ".json", ".lock")) and BUILD_COMMAND.search(text):
        kinds.append("build-deploy-command-reference")
    return ",".join(kinds)


def table(header, rows):
    return "\n".join("\t".join(str(value).replace("\t", " ").replace("\n", " ").strip() or "-" for value in row) for row in [header, *rows]) + "\n"


def inventory():
    tracked = {}
    for row in git("ls-tree", "-r", BASE).decode().splitlines():
        metadata, path = row.split("\t", 1)
        mode, kind, digest = metadata.split()
        tracked[path] = (mode, kind, digest)
    source = git("show", f"{BASE}:{MANIFEST}").decode()
    rules = re.findall(r'pattern: "([^"]+)", component: ([\w-]+)', source)
    assert len(rules) == len(re.findall(r"^  - \{ pattern:", source, re.M))
    records, entrypoints, references, migrations = [], [], [], []
    reference_matches = {}
    dependency_components = collections.defaultdict(set)
    callers = collections.defaultdict(set)
    with tarfile.open(fileobj=io.BytesIO(git("archive", BASE)), mode="r:") as archive:
        texts = {}
        for member in archive:
            if member.isfile():
                raw = archive.extractfile(member).read()
                if b"\0" not in raw:
                    texts[member.name] = raw.decode("utf-8", errors="replace")
    for path, (mode, kind, digest) in sorted(tracked.items()):
        text = texts.get(path, "")
        matched = [(pattern, component) for pattern, component in rules if fnmatch.fnmatchcase(path, pattern)]
        pattern, component = matched[0] if matched else ("NONE", "UNMAPPED")
        classification = disposition(path)
        kinds = surface(path, text)
        records.append((path, digest, mode, owner(path), classification, component, pattern, kinds or "source-or-data"))
        if kinds:
            events = sorted(set(re.findall(r"^  (push|pull_request|pull_request_target|workflow_dispatch|workflow_call|schedule|workflow_run|repository_dispatch|issues|issue_comment|release):", text, re.M)))
            entrypoints.append((path, owner(path), kinds, ",".join(events) or "inspect-entry", component, classification))
            for line_number, line in enumerate(text.splitlines(), 1):
                for reference in sorted(set(PATH_REFERENCE.findall(line))):
                    normalized = reference.removeprefix("./").rstrip("/")
                    if normalized not in reference_matches:
                        reference_matches[normalized] = [candidate for candidate in tracked if candidate == normalized or candidate.startswith(normalized + "/") or fnmatch.fnmatchcase(candidate, normalized)]
                    matches = reference_matches[normalized]
                    dependency_components[path].update(owner(candidate) for candidate in matches)
                    for candidate in matches:
                        callers[candidate].add(path)
                    references.append((path, line_number, normalized, "tracked" if matches else "dynamic-generated-or-unresolved", len(matches)))
        if path.startswith("modules/gateway/alembic/versions/") and path.endswith(".py"):
            values = {}
            for statement in ast.parse(text).body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(statement.value)
                elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id in {"revision", "down_revision"}:
                    values[statement.target.id] = ast.literal_eval(statement.value)
            migrations.append((path, values.get("revision", ""), repr(values.get("down_revision")), ",".join(sorted(set(re.findall(r'(?:create_table|add_column|drop_column)\(\s*[\"\x27]([^\"\x27]+)', text))))))
    assert len(records) == len(tracked) == len({row[0] for row in records})
    counts = collections.Counter(row[5] for row in records)
    owners = collections.Counter(row[3] for row in records)
    summary = ["# Static audit results", "", f"Source revision: `{BASE}`. Tracked artifacts: **{len(records)}**; enumerated build/deployment/validation surfaces: **{len(entrypoints)}**; local source-reference occurrences: **{len(references)}**; gateway migrations: **{len(migrations)}**.", "", "Generated by [audit.py](audit.py). See [coverage interpretation](inventory.md) before treating a path match as deployment coverage.", "", "## Existing manifest classification", "", "| Classification | Tracked files |", "|---|---:|"]
    summary.extend(f"| {component} | {count} |" for component, count in sorted(counts.items()))
    summary.extend(["", "## Audit ownership", "", "These are inventory groupings, not new platform module IDs or authorization grants.", "", "| Component or excluded record family | Tracked files |", "|---|---:|"])
    summary.extend(f"| {component} | {count} |" for component, count in sorted(owners.items()))
    summary.extend(["", "## Limits", "", "Static enumeration is complete for the pinned Git tree, including non-runtime records. Entrypoint detection deliberately over-includes tests and command references. Dynamic/generated references and external build definitions are not asserted to be resolved. Runtime state, external workflows, AWS resources and private environment settings were not inspected by this script.", ""])
    return {
        "tracked-artifacts.tsv": table(("path", "git_blob", "mode", "audit_component", "disposition", "existing_component", "existing_rule", "surface"), records),
        "entrypoints.tsv": table(("path", "audit_component", "surface", "workflow_events", "existing_component", "disposition", "referenced_components", "static_callers"), [(*row, ",".join(sorted(dependency_components[row[0]])), ",".join(sorted(callers[row[0]]))) for row in entrypoints]),
        "source-references.tsv": table(("source", "line", "reference", "resolution", "tracked_matches"), references),
        "gateway-migrations.tsv": table(("path", "revision", "down_revision", "created_or_altered_tables"), migrations),
        "audit-results.md": "\n".join(summary),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", action="store_true")
    arguments = parser.parse_args()
    artifacts = inventory()
    if arguments.patch:
        print("*** Begin Patch")
        for name, content in artifacts.items():
            destination = DESTINATION / name
            if destination.exists():
                print(f"*** Delete File: {destination.relative_to(ROOT)}")
            print(f"*** Add File: {destination.relative_to(ROOT)}")
            print("\n".join("+" + line for line in content.splitlines()))
        print("*** End Patch")
    else:
        for name, expected in artifacts.items():
            assert (DESTINATION / name).read_text() == expected, f"Inventory drift: {name}"
        print(f"PASS: {len(artifacts)} inventory artifacts match {BASE}")


if __name__ == "__main__":
    main()

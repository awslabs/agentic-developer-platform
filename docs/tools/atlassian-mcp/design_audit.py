"""Reproduce the Atlassian design inventory without changing or deploying runtime code."""

import argparse
import ast
import csv
import io
import re
import subprocess
from collections import Counter
from pathlib import Path

BASE = "0ebf98389d609aa553679497250572b8a13bdb07"
DIRECTORY = Path("docs/tools/atlassian-mcp")
TERMS = {
    "vendor": r"atlassian|confluence|\bjira\b",
    "tools": r"adp-tools|tools_facade|tools-platform|ADP_TOOLS_|tools_authorize",
    "oauth": r"oauth_connection|oauth_setup|oauth_providers|OAuthConnection",
}
WORKFLOWS = {
    "gateway-deploy.yml",
    "gateway-frontend-deploy.yml",
    "gateway-infra-apply.yml",
    "tools-platform-deploy.yml",
    "tools-facade-deploy.yml",
    "tools-interceptor-deploy.yml",
    "tools-live-evaluation.yml",
    "tools-ci.yml",
    "gateway-ci.yml",
    "gateway-frontend-ci.yml",
    "adp-release.yml",
    "adp-integration-clean-install.yml",
    "gateway-infra-plan.yml",
    "run-gateway-migrations.yml",
    "tools-platform-ci.yml",
}
CONDITIONAL_WORKFLOWS = {
    "agent-worker-image.yml",
    "agent-context-deploy.yml",
    "shared-agentcore-tools-deploy.yml",
}
RELEASE_SUPPORT = set()


def git(*arguments):
    return subprocess.check_output(["git", *arguments])


def entry_types(path, mode, executable_source):
    name = Path(path).name
    kinds = []
    if path.startswith(".github/workflows/"):
        kinds.append("workflow")
    if name in {"action.yml", "action.yaml"}:
        kinds.append("action")
    if "dockerfile" in name.lower() or "buildspec" in name.lower():
        kinds.append("image-build")
    if name in {
        "Makefile",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Cargo.toml",
        "go.mod",
        "justfile",
    }:
        kinds.append("package-build")
    if path.endswith((".tf", ".tf.json", ".hcl", ".tfvars", ".tfvars.json")):
        kinds.append("infra")
    if any(
        part in Path(path).parts for part in ("k8s", "helm", "charts")
    ) and path.endswith((".yaml", ".yml", ".tpl")):
        kinds.append("runtime-manifest")
    if (
        executable_source
        or mode == "100755"
        or path.endswith((".sh", ".ps1"))
        or any(part in Path(path).parts for part in ("scripts", "bin"))
    ):
        kinds.append("script-candidate")
    return ",".join(kinds) or "-"


def classification(path, hits):
    if path in RELEASE_SUPPORT:
        return "release", "include", "transitive-local-workflow-or-action"
    if path.startswith(".github/workflows/"):
        if Path(path).name in WORKFLOWS:
            return "release", "include", "existing-release-or-validation-boundary"
        return (
            ("release-adjacent", "conditional", "shared-consumer-or-regression")
            if hits or Path(path).name in CONDITIONAL_WORKFLOWS
            else ("other-workflow", "exclude", "unrelated-workflow")
        )
    if path.startswith("modules/tools/"):
        part = path.split("/")[2]
        if (
            part in {"platform", "facade", "interceptor", "scripts"}
            or len(path.split("/")) == 3
        ):
            return "tools-" + part, "include", "target-authorization-release-recovery"
        return (
            "tools-" + part,
            "conditional",
            "shared-tools-regression-no-provider-runtime-change",
        )
    if path.startswith("modules/gateway/"):
        if path.startswith(
            ("modules/gateway/src/proxy/", "modules/gateway/tests/proxy/")
        ):
            return (
                "client-model-protocol",
                "include",
                "schema-loading-through-adp-proxy-qualification",
            )
        if path.startswith("modules/gateway/cli/"):
            return "clients", "include", "installed-cli-and-client-configuration"
        if path.startswith("modules/gateway/alembic/"):
            return "schema", "include", "complete-migration-dependency-audit"
        if any(
            path.startswith("modules/gateway/" + part)
            for part in ("infra/", "k8s/", "scripts/", "deployment_evidence/")
        ) or Path(path).name in {
            "Dockerfile",
            "pyproject.toml",
            "requirements.txt",
            "buildspec.yml",
        }:
            return "gateway-release", "include", "shared-runtime-build-config-release"
        if hits:
            return (
                "connections-ui" if "/frontend/" in path else "gateway-authority",
                "include",
                "connector-or-shared-authority-reference",
            )
        if any(
            part in path
            for part in (
                "/auth/",
                "/identity/",
                "/services/secrets",
                "/models/vault",
                "/internal/",
                "/tests/cli/",
            )
        ):
            return (
                "gateway-authority",
                "include",
                "transitive-auth-vault-internal-route-boundary",
            )
        return "gateway-other", "exclude", "other-gateway-feature-no-selected-reference"
    if path.startswith(("tests/e2e/tools/", "tests/cli/")):
        return "acceptance", "include", "client-and-live-tools-verification"
    if path.startswith("platform/"):
        return (
            "platform-release",
            "conditional",
            "shared-install-iam-build-no-core-redesign",
        )
    if path.startswith(
        ("docs/tools/", "docs/adp-platform-deployment/", "docs/regression-testing/")
    ):
        return "documentation", "include", "architecture-deployment-scenario-lineage"
    if (
        path.startswith(("docs/", "data/", "aidlc-docs/"))
        or "/aidlc-docs/" in path
        or "/docs/" in path
    ):
        return (
            "historical-or-adjacent-docs",
            "exclude",
            "context-not-executable-contract",
        )
    if path.startswith(
        (
            "modules/agent-factory/",
            "modules/harness/",
            "modules/agent-context/",
            "modules/domain-apps/",
            "modules/user-services/",
        )
    ):
        return (
            path.split("/")[1],
            "conditional" if hits else "exclude",
            "shared-consumer-no-direct-atlassian-token"
            if hits
            else "independent-product-or-agent-runtime",
        )
    return (
        ("cross-cutting", "include", "selected-reference")
        if hits
        else (
            "repository-other",
            "exclude",
            "no-selected-reference-or-deployment-ownership",
        )
    )


def inventory():
    records = [
        record
        for record in git("ls-tree", "-r", "-z", BASE).decode().split("\0")
        if record
    ]
    paths = {record.split("\t", 1)[1] for record in records}
    pending = [
        ".github/workflows/" + name
        for name in WORKFLOWS
        if ".github/workflows/" + name in paths
    ]
    while pending:
        path = pending.pop()
        if path in RELEASE_SUPPORT:
            continue
        RELEASE_SUPPORT.add(path)
        source = git("show", f"{BASE}:{path}").decode()
        for reference in re.findall(
            r"uses:\s*[\"']?(\./\.github/(?:workflows|actions)/[A-Za-z0-9_/.-]+)",
            source,
        ):
            candidate = reference.removeprefix("./")
            candidates = (
                [candidate]
                if candidate.endswith((".yml", ".yaml"))
                else [candidate + "/action.yml", candidate + "/action.yaml"]
            )
            pending.extend(candidate for candidate in candidates if candidate in paths)
    hits = {}
    executable_paths = {
        name.removeprefix(BASE + ":")
        for name in git(
            "grep", "-Il", "-E", r"^#!|__name__.*__main__|import.meta.main", BASE, "--"
        )
        .decode()
        .splitlines()
    }
    for label, expression in TERMS.items():
        result = subprocess.run(
            ["git", "grep", "-Il", "-i", "-E", expression, BASE, "--"],
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.decode())
        for name in result.stdout.decode().splitlines():
            hits.setdefault(name.removeprefix(BASE + ":"), []).append(label)
    rows = []
    for record in records:
        metadata, path = record.split("\t", 1)
        mode, object_type, blob = metadata.split()
        matches = hits.get(path, [])
        component, disposition, reason = classification(path, matches)
        rows.append(
            [
                path,
                blob,
                object_type,
                component,
                disposition,
                reason,
                entry_types(path, mode, path in executable_paths),
                ",".join(matches) or "-",
            ]
        )
    return sorted(rows)


def migration_rows(rows):
    migrations = {}
    for row in rows:
        path = row[0]
        if not path.startswith(
            "modules/gateway/alembic/versions/"
        ) or not path.endswith(".py"):
            continue
        values = {}
        for node in ast.parse(git("show", f"{BASE}:{path}").decode()).body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "revision",
                        "down_revision",
                    }:
                        values[target.id] = ast.literal_eval(node.value)
        if "revision" in values:
            parents = values.get("down_revision")
            migrations[values["revision"]] = (
                path,
                (parents,) if isinstance(parents, str) else tuple(parents or ()),
            )
    result, visited = [], set()

    def visit(revision):
        if revision in visited:
            return
        path, parents = migrations[revision]
        for parent in parents:
            visit(parent)
        visited.add(revision)
        result.append([str(len(result) + 1), revision, ",".join(parents) or "-", path])

    for revision in sorted(migrations):
        visit(revision)
    return result, sorted(
        set(migrations)
        - {parent for _, parents in migrations.values() for parent in parents}
    )


def tsv(header, rows):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-patch", action="store_true")
    arguments = parser.parse_args()
    rows = inventory()
    migrations, heads = migration_rows(rows)
    header = [
        "path",
        "git_blob",
        "object_type",
        "component",
        "disposition",
        "reason",
        "entry_type",
        "search_hits",
    ]
    artifacts = {
        "inventory.tsv": tsv(header, rows),
        "entrypoints.tsv": tsv(header, [row for row in rows if row[6] != "-"]),
        "migrations.tsv": tsv(
            ["topological_order", "revision", "parents", "path"], migrations
        ),
    }
    if arguments.emit_patch:
        print("*** Begin Patch")
        for name, content in artifacts.items():
            print(f"*** Add File: {DIRECTORY / name}")
            print("".join("+" + line + "\n" for line in content.splitlines()), end="")
        print("*** End Patch")
        return
    for name, content in artifacts.items():
        if (DIRECTORY / name).read_text() != content:
            raise SystemExit(f"Inventory mismatch: {name}")
    print(
        f"Verified {len(rows)} tracked artifacts; {sum(row[6] != '-' for row in rows)} entry candidates; {len(migrations)} migrations; heads={heads}"
    )
    print("Dispositions:", dict(sorted(Counter(row[4] for row in rows).items())))
    print(
        "Selected-reference exclusions:",
        sum(row[7] != "-" and row[4] == "exclude" for row in rows),
    )


if __name__ == "__main__":
    main()

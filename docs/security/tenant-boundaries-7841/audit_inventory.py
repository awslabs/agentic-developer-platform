"""Reproduce the source-only inventory for issue 7841; no app imports."""

import argparse
import ast
import csv
import io
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys


REVISION = "c958000f0829b1462c2f4ce23bfb743676b97306"
ROOT = Path(__file__).resolve().parents[3]
DIRECT_ROOTS = (
    "modules/gateway/src/internal/",
    "modules/gateway/src/agentauth/",
    "modules/gateway/src/auth/",
    "modules/gateway/src/shared/identity/",
    "modules/gateway/src/shared/models/",
    "modules/gateway/infra/modules/cognito/",
    "modules/gateway/alembic/versions/",
    "modules/gateway/tests/internal/",
    "modules/gateway/tests/auth/",
    "modules/domain-apps/superplane/src/superplane-api/",
    "modules/domain-apps/superplane/auth/",
    "modules/domain-apps/superplane/installation/",
    "modules/agent-context/door/",
    "modules/agent-context/tests/",
    "modules/agent-context/alembic/",
)
SIGNALS = (
    "credential-evidence|credential-revocation-state|read_credential_evidence|"
    "verify_internal_or_irsa|service_tenant|pre_token_generation|"
    "SSOConfigRequest|create_or_update_saml_provider|create_or_update_oidc_provider|"
    "handle_identify|secure_backend|ADP_DOMAIN_OPERATION_BINDINGS|"
    "internal:cross-tenant|domain:operation-producer|current_human_identity"
)


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments]).decode()


def entrypoint(path):
    name = Path(path).name
    return (
        path.startswith((".github/workflows/", ".github/actions/", "codebuild/", "scripts/"))
        or any(part in path for part in ("/scripts/", "/releases/", "/codebuild/"))
        or path.endswith((".tf", ".tfvars", ".hcl", ".sh", ".yml", ".yaml"))
        or "Dockerfile" in name
        or name in {"Makefile", "package.json", "pyproject.toml", "setup.py", "__main__.py"}
        or bool(re.search(r"(?:^|/)(?:build|deploy|release|bootstrap|install|migrat)[^/]*\.(?:py|js|ts)$", path))
    )


def component(path):
    for prefix, label in (
        ("modules/gateway/", "gateway"),
        ("modules/domain-apps/superplane/", "superplane"),
        ("modules/agent-context/", "agent-context"),
        ("modules/agent-factory/", "agent-factory-caller"),
        ("modules/harness/", "harness-caller"),
        ("modules/tools/", "tools-caller"),
    ):
        if path.startswith(prefix):
            return label
    if path.startswith((".github/", "codebuild/", "platform/", "environments/", "scripts/")) or path == "deploy.sh":
        return "shared-delivery"
    return "other"


def inventory(paths, references):
    rows = []
    for path in paths:
        direct = path.startswith(DIRECT_ROOTS)
        referenced = path in references
        entry = entrypoint(path)
        if not (direct or referenced or entry):
            continue
        owner = component(path)
        if path.startswith(("docs/", "aidlc-docs/")) or path.endswith(".md"):
            disposition = "reference-only"
        elif owner == "other" or (not direct and not referenced and owner in {"agent-factory-caller", "harness-caller", "tools-caller"}):
            disposition = "excluded-no-boundary-reference"
        elif entry and not direct and not referenced:
            disposition = "delivery-or-config-surface"
        else:
            disposition = "boundary-or-dependency"
        reasons = ";".join(label for flag, label in (
            (direct, "bounded-component-root"), (referenced, "symbol-reference"), (entry, "entrypoint-or-config")
        ) if flag)
        rows.append((path, owner, disposition, reasons))
    return rows


def routes(paths):
    rows = []
    for path in paths:
        if not path.startswith("modules/gateway/src/") or not path.endswith(".py"):
            continue
        source = git("show", f"{REVISION}:{path}")
        if "/internal" not in source and "/internal/" not in path:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr not in {"get", "post", "put", "patch", "delete", "api_route"}:
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                    continue
                rows.append((path, node.lineno, decorator.func.attr.upper(), decorator.args[0].value, node.name))
    return sorted(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    paths = git("ls-tree", "-r", "--name-only", REVISION).splitlines()
    references = {line.split(":", 1)[1] for line in git("grep", "-Il", "-E", SIGNALS, REVISION, "--").splitlines()}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    if arguments.routes:
        writer.writerow(("path", "line", "method", "local_route", "handler"))
        rows = routes(paths)
        target = "internal-route-candidates.csv"
    else:
        writer.writerow(("path", "component", "disposition", "selection"))
        rows = inventory(paths, references)
        target = "artifact-inventory.csv"
    writer.writerows(rows)
    print(f"revision={REVISION} tracked={len(paths)} selected={len(rows)}", file=sys.stderr)
    if not arguments.routes:
        print(dict(sorted(Counter(row[2] for row in rows).items())), file=sys.stderr)
    if arguments.check:
        expected = Path(__file__).with_name(target).read_text()
        if expected != output.getvalue():
            raise SystemExit(f"Inventory differs: {target}")
        print(f"Verified {target}")
    else:
        sys.stdout.write(output.getvalue())


if __name__ == "__main__":
    main()

"""Reproduce the design's tracked-artifact and deployment-entry inventory."""

import argparse
import collections
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess


BASE = "a3ffb462b29e6b6c88e32b0619ae04a3a2b5df78"
ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
REFERENCE = re.compile(
    r"gitlab|arc.runner|deploy.runner.pool|SecretProviderClass|secrets-store\.csi|"
    r"ApplicationNetworkPolicy|NodeClass|NodePool|enable.network.policy.controller",
    re.IGNORECASE,
)
ENTRY = re.compile(
    r"terraform\s+(?:init|plan|apply|destroy)|helm\s+(?:install|upgrade|uninstall)|"
    r"kubectl\s+(?:apply|delete)|docker\s+(?:build|buildx)|"
    r"start-build|workflow_dispatch|workflow_call|buildspec|"
    r"npm\s+run\s+build|make\s+(?:build|deploy)|deploy-all\.sh|undeploy\.sh"
)
ARTIFACT = re.compile(
    r"(?:\.(?:tf|tfvars|hcl|ya?ml|sh|ps1|bicep|pkr\.json)$|"
    r"(?:^|/)(?:Dockerfile[^/]*|Containerfile[^/]*|Makefile|GNUmakefile|"
    r"Jenkinsfile|Procfile|Justfile|Tiltfile|Taskfile[^/]*|Chart\.lock|"
    r"package\.json|pyproject\.toml|Cargo\.toml|go\.mod|"
    r"docker-compose[^/]*|buildspec[^/]*|action\.ya?ml))$",
    re.IGNORECASE,
)
ARTIFACT_TREE = re.compile(
    r"(?:^|/)(?:infra|k8s|manifests|releases|image|images|scripts)/|"
    r"^(?:codebuild/|\.github/actions/|environments/)"
)


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def component(path, relevant, entry):
    if path.startswith("modules/source-control/gitlab/"):
        return "instance", "inspect/change only registration, CA and additive discovery; other instance resources retained"
    if path.startswith(("modules/gateway/src/gitlab/", "modules/gateway/tests/gitlab/", "modules/gateway/tests/products/test_gitlab", "modules/gateway/alembic/versions/150_")):
        return "identity", "reuse isolated administrator and tenant-group mapping; no new database schema"
    if path.startswith("environments/") and "gitlab" in path:
        return "instance-config", "existing private target inputs; runner backend must be separate"
    if path.startswith(".github/") and "gitlab" in path:
        return "gitlab-ci", "existing GitLab apply/destroy/evaluation; add sibling runner paths, do not inherit logging"
    if path.startswith(("platform/infra/", "platform/scripts/", "platform/arc-runner/", "platform/deploy-management/")) or path == "deploy.sh":
        return "platform", "shared cluster/network/IAM/build/launcher dependency; no implicit platform apply"
    if path.startswith("modules/agent-factory/infra/"):
        return "runner-precedent", "ARC and deploy-pool precedent only; do not copy authority or state-held secrets"
    if path.startswith((".github/actions/", ".github/workflows/", ".github/scripts/")):
        return "delivery", "enumerated build/deploy/test entry; unrelated module payload excluded from runner changes"
    if path.startswith(("codebuild/", "scripts/")):
        return "build-support", "enumerated build/scan/publish helper or fixture; no existing GitLab runner image recipe"
    if path.startswith(("docs/", "agent_learning/", "aidlc", "probe scan/", ".adp/")):
        return "reference-only", "historical/design/procedure or tracking artifact, not an active runner deployment"
    if path.startswith("modules/user-services/dev-box/"):
        return "denied-target", "Dev Box is a negative connectivity target, not runner infrastructure"
    if path.startswith("modules/gateway/"):
        return "gateway-boundary", "identity/CloudFront context or denied gateway/database target; no runner-to-gateway trust"
    if path.startswith("modules/agent-factory/"):
        return "agent-boundary", "existing GitLab webhook/client or agent runtime, not GitLab CI execution"
    if path.startswith("modules/"):
        return "other-module-excluded", "enumerated unrelated module artifact; no deployment or build change for this epic"
    if path.startswith(("experiments/", "tests/", "test/", "fixtures/")):
        return "fixture-excluded", "test/experiment is not the production runner path"
    if relevant or entry:
        return "repository-context", "repository-level reference/build/config; inspect entry linkage, no runner payload yet"
    return "other-artifact-excluded", "enumerated non-runner configuration/build artifact"


def inventory():
    entries = []
    for record in git("ls-tree", "-rz", "--full-tree", BASE).split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        entries.append((path.decode(), kind, oid))
    objects = [oid for _, kind, oid in entries if kind == "blob"]
    result = subprocess.run(
        ["git", "cat-file", "--batch"], input=("\n".join(objects) + "\n").encode(),
        stdout=subprocess.PIPE, check=True, cwd=ROOT,
    ).stdout
    blobs = {}
    stream = io.BytesIO(result)
    for oid in objects:
        header = stream.readline().decode().split()
        size = int(header[2])
        blobs[oid] = stream.read(size)
        assert stream.read(1) == b"\n"
    rows = []
    omitted = collections.Counter()
    for path, kind, oid in sorted(entries):
        content = blobs.get(oid, b"")
        text = "" if b"\0" in content else content.decode("utf-8", errors="replace")
        reference_lines = [str(number) for number, line in enumerate(text.splitlines(), 1) if REFERENCE.search(line)]
        entry_lines = [str(number) for number, line in enumerate(text.splitlines(), 1) if ENTRY.search(line)]
        selected = ARTIFACT.search(path) or ARTIFACT_TREE.search(path) or REFERENCE.search(path) or reference_lines or entry_lines or kind != "blob"
        if not selected:
            omitted[path.split("/")[0]] += 1
            continue
        owner, disposition = component(path, bool(reference_lines), bool(entry_lines))
        rows.append({
            "path": path, "component": owner, "disposition": disposition,
            "artifact_name_match": str(bool(ARTIFACT.search(path))).lower(),
            "reference_lines": ",".join(reference_lines), "entry_lines": ",".join(entry_lines),
            "git_object": oid, "sha256": hashlib.sha256(content).hexdigest(),
        })
    summary = {
        "revision": BASE, "tracked_entries": len(entries), "inventoried": len(rows),
        "entry_content_matches": sum(bool(row["entry_lines"]) for row in rows),
        "github_workflows": sum(row["path"].startswith(".github/workflows/") for row in rows),
        "components": dict(sorted(collections.Counter(row["component"] for row in rows).items())),
        "omitted_non_candidate_entries_by_root": dict(sorted(omitted.items())),
        "unclassified_candidates": 0,
    }
    assert len(rows) + sum(omitted.values()) == len(entries)
    return rows, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    rows, summary = inventory()
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    outputs = {
        HERE / "inventory.tsv": buffer.getvalue(),
        HERE / "inventory-summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
    }
    for path, content in outputs.items():
        if arguments.check:
            if path.read_text() != content:
                raise SystemExit(f"Inventory differs: {path.name}")
        else:
            patch = "*** Begin Patch\n*** Add File: " + str(path) + "\n"
            patch += "".join("+" + line + "\n" for line in content.splitlines())
            patch += "*** End Patch\n"
            subprocess.run(["apply_patch"], input=patch.encode(), check=True, cwd=ROOT)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()

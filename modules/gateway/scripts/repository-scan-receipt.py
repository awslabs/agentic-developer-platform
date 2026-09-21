"""Export observed one-off scan identity and results; never manufacture a verdict."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SECRET = re.compile(r"password|token|secret|private.?key|access.?key|authorization", re.I)


def load_document(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate evidence field")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def context(env, aws_read, git_head):
    source, revision = env["CONTEXT_SOURCE"], env["CONTEXT_REVISION"]
    workflow, repository = env["CONTEXT_WORKFLOW"], env["GITHUB_REPOSITORY"]
    if not SHA.fullmatch(source) or not SHA.fullmatch(revision) or source != git_head or revision != env["GITHUB_SHA"]:
        raise ValueError("scan checkout/source or workflow revision changed")
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml", workflow):
        raise ValueError("scan workflow path invalid")
    if env["GITHUB_EVENT_NAME"] != "workflow_dispatch" or env["GITHUB_WORKFLOW_REF"].split("@", 1)[0] != repository + "/" + workflow:
        raise ValueError("scan producer must be the bound one-off workflow")
    account = aws_read(["sts", "get-caller-identity"])["Account"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != env["SCAN_EXPECTED_ACCOUNT"]:
        raise ValueError("scan credential account differs from accepted target")
    region = env["AWS_REGION"]
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+", region):
        raise ValueError("scan region invalid")
    correlation = env["CONTEXT_CORRELATION"]
    if not re.fullmatch(r"[0-9a-f]{64}", correlation):
        raise ValueError("scan correlation missing")
    inputs = load_document(env["SCAN_INPUTS_JSON"])
    if (
        not isinstance(inputs, dict)
        or len(inputs) > 16
        or any(not isinstance(key, str) or SECRET.search(key) or not isinstance(value, str) or len(value) > 1024 for key, value in inputs.items())
    ):
        raise ValueError("scan inputs must be bounded non-secret strings")
    if inputs.get("expected_account_id") != account or inputs.get("region") != region:
        raise ValueError("scan inputs differ from the observed target")
    ids = [int(env[name]) for name in ("CONTEXT_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")]
    if any(number <= 0 for number in ids):
        raise ValueError("provider identifiers must be positive")
    return dict(
        schema_version=1,
        repository_id=ids[0],
        run_id=ids[1],
        run_attempt=ids[2],
        workflow_path=workflow,
        workflow_revision=revision,
        source_revision=source,
        account_id=account,
        region=region,
        resource_kind="repository_scan",
        resource_id=repository,
        inputs=inputs,
        correlation=correlation,
    )


def scan_receipt(identity, observed, provenance_dir):
    if not isinstance(observed, dict) or set(observed) != {"source_revision", "coverage_complete", "cleanup_complete", "images"}:
        raise ValueError("scanner must supply the exact observed result contract")
    if observed["source_revision"] != identity["source_revision"] or any(
        type(observed[name]) is not bool for name in ("coverage_complete", "cleanup_complete")
    ):
        raise ValueError("scanner source/status is not verified")
    images = observed["images"]
    if not isinstance(images, dict) or not 1 <= len(images) <= 64:
        raise ValueError("scanner image inventory missing")
    root = Path(provenance_dir).resolve()
    output = {}
    for name, item in images.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 256 or not isinstance(item, dict) or set(item) != {"digest", "provenance_path"}:
            raise ValueError("scanner image result invalid")
        if not isinstance(item["digest"], str) or not DIGEST.fullmatch(item["digest"]):
            raise ValueError("scanner must report the actual immutable image digest")
        relative = Path(item["provenance_path"])
        path = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(root)
            or not path.is_file()
            or not 0 < path.stat().st_size <= 1024 * 1024
        ):
            raise ValueError("observed image provenance file missing or outside the evidence directory")
        output[name] = dict(digest=item["digest"], provenance_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return dict(
        evidence_schema="repository-scan-receipt/v1",
        source_revision=identity["source_revision"],
        correlation=identity["correlation"],
        target={name: identity[name] for name in ("account_id", "region", "resource_kind", "resource_id")},
        images=output,
        coverage_complete=observed["coverage_complete"],
        cleanup_complete=observed["cleanup_complete"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--provenance-dir", type=Path)
    args = parser.parse_args()
    if (args.results is None) != (args.provenance_dir is None):
        parser.error("--results and --provenance-dir must be supplied together")

    def aws_read(command):
        return json.loads(subprocess.check_output(["aws", *command, "--output", "json"], timeout=20))

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=10).strip()
    identity = context(os.environ, aws_read, head)
    directory = Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-context"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deployment-context.json").write_text(json.dumps(identity, sort_keys=True) + "\n")
    if args.results is not None:
        if not 0 < args.results.stat().st_size <= 1024 * 1024:
            raise ValueError("scanner result exceeds the evidence bound")
        result = scan_receipt(identity, load_document(args.results.read_bytes()), args.provenance_dir)
        directory = Path(os.environ["RUNNER_TEMP"]) / "adp-repository-scan"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "scan-receipt.json").write_text(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

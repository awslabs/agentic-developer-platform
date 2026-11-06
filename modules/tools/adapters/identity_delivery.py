"""Read back private Identity evidence before emitting generic delivery v2 proof."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

CHECKS = ("8448.AC-01", "8448.AC-02", "8448.AC-03", "8448.AC-04", "identity-fixtures-clean")


def write_private(path, value):
    """Write one new private evidence file; never overwrite a prior result.

    This reporter is the only adapter script the release workflow runs with the
    host interpreter instead of the adapter runtime prepared by deploy.sh, so it
    stays stdlib-only. Importing deploy.write_private also loaded deploy's
    module graph (adp_tools, pydantic), which the host interpreter cannot
    resolve. These are deploy.write_private's O_EXCL/0600 semantics.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def require(condition):
    if not condition:
        raise ValueError("identity_delivery_unverified")


def readback(summary_bytes, receipt, env):
    require(set(receipt) == {"bucket", "key", "version_id", "sha256"})
    require(receipt["bucket"] == f"adp-dev-deployment-evidence-{env['ACCOUNT_ID']}")
    require(re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]))
    require(receipt["key"].endswith("/repository-artifact/atlassian-identity.json"))

    def read_object(key, version):
        with tempfile.TemporaryDirectory(prefix="adp-identity-readback-") as name:
            destination = Path(name) / "evidence"
            response = json.loads(
                subprocess.check_output(
                    [
                        "aws",
                        "s3api",
                        "get-object",
                        "--bucket",
                        receipt["bucket"],
                        "--key",
                        key,
                        "--version-id",
                        version,
                        "--expected-bucket-owner",
                        env["ACCOUNT_ID"],
                        "--region",
                        env["AWS_REGION"],
                        "--output",
                        "json",
                        str(destination),
                    ],
                    timeout=40,
                )
            )
            content = destination.read_bytes()
        require(response.get("VersionId") == version and response.get("ServerSideEncryption") == "AES256")
        require(0 < len(content) <= 4 * 1024 * 1024)
        return content

    envelope = json.loads(read_object(receipt["key"], receipt["version_id"]))
    require(set(envelope) == {"transport", "payload", "signature"} and isinstance(envelope["signature"], str))
    payload = base64.b64decode(envelope["payload"], validate=True)
    require(hashlib.sha256(payload).hexdigest() == receipt["sha256"])
    descriptor = json.loads(payload)
    require(descriptor["component"] == "atlassian-identity" and descriptor["bucket"] == receipt["bucket"])
    require(descriptor["source_revision"] == env["ADP_SOURCE_REVISION"])
    require(descriptor["workflow_revision"] == env["ADP_DEFINITION_REVISION"])
    require(descriptor["run_id"] == int(env["GITHUB_RUN_ID"]) and descriptor["run_attempt"] == 1)
    archive = read_object(descriptor["key"], descriptor["version_id"])
    require(len(archive) == descriptor["size"] and hashlib.sha256(archive).hexdigest() == descriptor["sha256"])
    with zipfile.ZipFile(io.BytesIO(archive)) as packed:
        require(packed.namelist() == ["atlassian-stage-summary.json"])
        require(packed.read("atlassian-stage-summary.json") == summary_bytes)
    return "repository-artifact:atlassian-identity:sha256:" + descriptor["sha256"]


def delivery(report, env, evidence_ref):
    require(re.fullmatch(r"[0-9a-f]{64}", env["ADP_IDENTITY_CONTRACT_DIGEST"]))
    require(re.fullmatch(r"[0-9a-f]{40}", env["ADP_TOOLS_FOUNDATION_GATEWAY_REVISION"]))
    require(report["stage_id"] == "identity" and report["issue_number"] == 8448)
    require(report["source_revision"] == env["ADP_SOURCE_REVISION"])
    require(report["gateway_revision"] == env["ADP_TOOLS_FOUNDATION_GATEWAY_REVISION"] == env["ADP_SOURCE_REVISION"])
    require(report["workflow_revision"] == env["ADP_DEFINITION_REVISION"])
    require(report["run_id"] == env["GITHUB_RUN_ID"] and report["run_attempt"] == env["GITHUB_RUN_ATTEMPT"] == "1")
    require(report["correlation"] == env["ADP_CORRELATION"])
    require(
        report["criteria"]
        == [
            {"criterion_id": f"8448.AC-{index:02}", "phase_status": "pass", "evidence_verified": True}
            for index in range(1, 5)
        ]
    )
    require(report["runtime_verified"] is True and report["cleanup_status"] == "confirmed")
    require(report["independent_verification"] == "confirmed" and report["unresolved_operations"] == [])
    require(report["test_runtime_artifacts_match"] is True and report["provider_live_attestation"] is False)
    require(report["verification_level"] == "controlled_integration" and report["fixture_source"] == "generated")
    require(re.fullmatch(r"[0-9a-f]{64}", report["junit_sha256"]))
    require(re.fullmatch(r"[0-9a-f]{64}", report["adapter_junit_sha256"]))
    require(report["measurements"]["gateway"] and report["measurements"]["adapter"])
    resource_id = env["ADP_TOOLS_EKS_CLUSTER_NAME"] + "/" + env["ADP_TOOLS_ADAPTER_NAMESPACE"]
    require(report["resource_kind"] == "eks-namespace" and report["resource_id"] == resource_id)
    require(re.fullmatch(r"[0-9]{12}", env["ACCOUNT_ID"]))
    require(re.fullmatch(r"[1-9][0-9]*", env["GITHUB_REPOSITORY_ID"]))
    require(bool(evidence_ref) and len(evidence_ref) <= 512)
    return {
        "schema_version": 2,
        "contract_digest": env["ADP_IDENTITY_CONTRACT_DIGEST"],
        "correlation": env["ADP_CORRELATION"],
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": 1,
        "workflow_path": ".github/workflows/tools-platform-deploy.yml",
        "workflow_revision": env["ADP_DEFINITION_REVISION"],
        "source_revision": env["ADP_SOURCE_REVISION"],
        "actual_revision": report["source_revision"],
        "account_id": env["ACCOUNT_ID"],
        "region": env["AWS_REGION"],
        "resource_kind": "eks-namespace",
        "resource_id": resource_id,
        "component": "tool-adapters",
        "checks": [{"id": check, "outcome": "passed", "evidence_ref": evidence_ref} for check in CHECKS],
        "cleanup": "passed",
        "produced_at": datetime.now(UTC).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.summary.read_bytes()
    require(0 < len(source) <= 2 * 1024 * 1024)
    receipt = json.loads(args.receipt.read_text())
    reference = readback(source, receipt, os.environ)
    write_private(args.output, delivery(json.loads(source), os.environ, reference))


if __name__ == "__main__":
    main()

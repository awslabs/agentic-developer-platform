"""Produce and independently read back controlled Atlassian proof evidence."""

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

from tests.e2e.tools.atlassian_contract import CONTRACT_PATH
from tests.e2e.tools.atlassian_handoff import DESIGN_REVISION, MATRIX_PATH
from tests.e2e.tools.evidence_reader import (
    VERSION,
    archived_files,
    checked_object,
    fetch_s3,
    require,
)


CASES = {
    "SCN-01": "test_proof_auth_lifecycle_is_generated_only_and_denies_after_cleanup",
    "SCN-02": "test_generated_proof_pins_full_catalogue_and_redacts_site",
    "SCN-03": "test_proof_handoff_review_requires_both_bound_generated_procedures",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
CLEANUP_CASE = "test_owned_cleanup_is_provider_first_and_read_back_twice"


def phase_report(junit, *, source, definition, gateway, run_id, attempt):
    require(
        all(
            isinstance(value, str) and SHA.fullmatch(value)
            for value in (source, definition, gateway)
        )
    )
    require(
        source == gateway
        and all(
            isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value)
            for value in (run_id, attempt)
        )
    )
    require(isinstance(junit, bytes) and 0 < len(junit) < 2 * 1024 * 1024)
    try:
        root = ElementTree.fromstring(junit)
    except ElementTree.ParseError:
        raise ValueError("controlled proof JUnit is invalid") from None
    require(root.tag in {"testsuite", "testsuites"})
    suites = list(root.iter("testsuite"))
    require(
        bool(suites)
        and all(
            suite.get(field, "0") == "0"
            for suite in suites
            for field in ("errors", "failures", "skipped")
        )
    )
    tests = list(root.iter("testcase"))
    names = [case.get("name") for case in tests]
    require(
        len(tests) >= len(CASES) + 1
        and all(names.count(name) == 1 for name in (*CASES.values(), CLEANUP_CASE))
    )
    require(
        not any(
            case.find(tag) is not None
            for case in tests
            for tag in ("failure", "error", "skipped")
        )
    )
    cleanup = next(case for case in tests if case.get("name") == CLEANUP_CASE)
    properties = [item for item in cleanup.findall("properties/property")
                  if item.get("name") == "adp_generated_cleanup"]
    require(len(properties) == 1)
    try:
        observation = json.loads(properties[0].get("value", ""))
    except (TypeError, ValueError):
        raise ValueError("generated cleanup observation is invalid") from None
    require(
        isinstance(observation, dict)
        and observation.get("phase") == "verified"
        and observation.get("uncertain") is False
        and observation.get("remaining_resources") == []
    )
    return {
        "schema_version": 1,
        "issue_number": 8446,
        "phase": "stage.identity-proof",
        "generated_cleanup": "verified",
        "cleanup_observation": observation,
        "unresolved_operations": observation["remaining_resources"],
        "evidence_kind": "generated-fixture",
        "controlled_status": "candidate",
        "live_status": "unproven",
        "source_revision": source,
        "definition_revision": definition,
        "gateway_revision": gateway,
        "design_revision": DESIGN_REVISION,
        "run_id": run_id,
        "run_attempt": attempt,
        "scenarios": {scenario: "pass" for scenario in CASES},
        "junit_sha256": hashlib.sha256(junit).hexdigest(),
        "contract_sha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        "readiness_sha256": hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest(),
    }


def write_report(destination, junit_path, **pins):
    path = Path(destination)
    require(
        path.name == "evidence.json"
        and path.parent == Path(junit_path).parent
        and not path.is_symlink()
    )
    report = phase_report(Path(junit_path).read_bytes(), **pins)
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return report


def readback(
    uri,
    version,
    *,
    repository,
    account,
    run_id,
    attempt,
    source,
    definition,
    gateway,
    fetch=fetch_s3,
):
    require(
        isinstance(version, str) and version != "null" and VERSION.fullmatch(version)
    )
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account))
    require(
        isinstance(repository, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    )
    require(
        all(
            isinstance(value, str) and SHA.fullmatch(value)
            for value in (source, definition, gateway)
        )
    )
    require(
        source == gateway
        and all(
            isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value)
            for value in (run_id, attempt)
        )
    )
    bucket = f"adp-dev-ci-evidence-{account}"
    name = f"atlassian-identity-proof-{run_id}-{attempt}"
    prefix = f"artifacts/14/{repository}/{run_id}/{attempt}/identity-proof/{name}/"
    require(
        isinstance(uri, str)
        and re.fullmatch(
            re.escape(f"s3://{bucket}/{prefix}") + r"[0-9a-f]{64}\.manifest\.json", uri
        )
    )
    key = uri.removeprefix(f"s3://{bucket}/")
    manifest_sha = key.rsplit("/", 1)[-1].split(".", 1)[0]
    data, metadata = fetch(bucket, key)
    require(
        isinstance(data, bytes)
        and len(data) <= 2 * 1024 * 1024
        and isinstance(metadata, dict)
    )
    require(
        metadata.get("VersionId") == version
        and metadata.get("ContentLength") == len(data)
        and metadata.get("ChecksumSHA256")
        == base64.b64encode(hashlib.sha256(data).digest()).decode()
        and hashlib.sha256(data).hexdigest() == manifest_sha
    )
    manifest = json.loads(data)
    require(
        isinstance(manifest, dict)
        and manifest.get("schema_version") == 1
        and manifest.get("repository") == repository
        and manifest.get("run_id") == run_id
        and manifest.get("run_attempt") == attempt
        and manifest.get("job") == "identity-proof"
        and manifest.get("name") == name
        and manifest.get("retention_days") == 14
        and not manifest.get("missing_path_count")
        and manifest.get("checked_out_revision") == definition
        and manifest.get("workflow_revision") == definition
    )
    artifact = manifest.get("artifact", {})
    require(
        isinstance(artifact, dict)
        and artifact.get("bucket") == bucket
        and isinstance(artifact.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        and artifact.get("key") == f"{prefix}{artifact['sha256']}.tar.gz"
    )
    archive = checked_object(
        bucket,
        artifact["key"],
        artifact.get("version_id"),
        artifact["sha256"],
        artifact.get("size"),
        fetch,
    )
    files = archived_files(archive, manifest.get("files"))
    require(set(files) == {"evidence.json", "junit.xml"})
    expected = phase_report(
        files["junit.xml"],
        source=source,
        definition=definition,
        gateway=gateway,
        run_id=run_id,
        attempt=attempt,
    )
    require(json.loads(files["evidence.json"]) == expected)
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("report", "readback"))
    parser.add_argument("--output")
    args = parser.parse_args()
    env = os.environ
    pins = dict(
        source=env["SOURCE_REVISION"],
        definition=env["DEFINITION_REVISION"],
        gateway=env["GATEWAY_REVISION"],
        run_id=env["GITHUB_RUN_ID"],
        attempt=env["GITHUB_RUN_ATTEMPT"],
    )
    if args.action == "report":
        require(bool(args.output))
        write_report(
            Path(args.output) / "evidence.json", Path(args.output) / "junit.xml", **pins
        )
    else:
        readback(
            env["CI_EVIDENCE_MANIFEST_URI"],
            env["CI_EVIDENCE_MANIFEST_VERSION"],
            repository=env["GITHUB_REPOSITORY"],
            account=env["EXPECTED_ACCOUNT"],
            **pins,
        )


if __name__ == "__main__":
    main()

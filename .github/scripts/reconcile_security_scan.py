#!/usr/bin/env python3
"""Validate one-off scan outputs and export observed receipt inputs."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "codebuild"))
TERMINAL_BUILD_STATES = {"SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
EXPECTED_CFN_TEMPLATES = {
    "full-admin.cfn.yaml",
    "readonly.cfn.yaml",
    "scoped-write.cfn.yaml",
}
EXPECTED_NPM_REPORTS = {
    "npm-audit-modules-gateway-frontend.json",
    "npm-audit-modules-agent-factory-agent.json",
}


def load_json(path: Path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r} in {path}")
            result[key] = value
        return result

    if not path.is_file() or not 0 < path.stat().st_size <= 50 * 1024 * 1024:
        raise ValueError(f"missing, empty, or oversized result: {path}")
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON result {path}: {exc}") from exc


def require_sarif(path: Path):
    report = load_json(path)
    runs = report.get("runs") if isinstance(report, dict) else None
    if report.get("version") != "2.1.0" or not isinstance(runs, list) or not runs or any(not isinstance(run, dict) for run in runs):
        raise ValueError(f"{path} is not a SARIF report with a non-empty runs list")


def require_cyclonedx(path: Path):
    report = load_json(path)
    if not isinstance(report, dict) or report.get("bomFormat") != "CycloneDX":
        raise ValueError(f"{path} is not a CycloneDX SBOM")


def only_files(directory: Path, pattern: str) -> set[str]:
    return {path.name for path in directory.glob(pattern) if path.is_file()}


def validate_findings(root: Path, expected_images: set[str]):
    # Pinned Checkov emits results_sarif.sarif inside its output directory;
    # the publisher flattens that directory. Also accept the historical name,
    # but never choose between competing reports or silently skip an extra one.
    checkov_files = only_files(root / "checkov", "*.sarif")
    if len(checkov_files) != 1 or not checkov_files <= {"checkov-results.sarif", "results_sarif.sarif"}:
        raise ValueError("Checkov must supply exactly one recognized SARIF report")
    require_sarif(root / "checkov" / next(iter(checkov_files)))
    sarif_reports = {
        "semgrep": "semgrep-results.sarif",
        "bandit": "bandit-results.sarif",
    }
    for tool, filename in sarif_reports.items():
        require_sarif(root / tool / filename)

    secret_scan = load_json(root / "detect-secrets" / "detect-secrets-results.json")
    if not isinstance(secret_scan, dict) or not isinstance(secret_scan.get("results"), dict) or not secret_scan.get("plugins_used"):
        raise ValueError("detect-secrets scan lacks results or plugins_used")
    secret_audit = load_json(root / "detect-secrets" / "detect-secrets-audit.json")
    if not isinstance(secret_audit, dict) or not isinstance(secret_audit.get("results"), list):
        raise ValueError("detect-secrets audit lacks a results list")

    if (secret_scan.get("repository_matcher_policy")
            or secret_audit.get("schema_version") == "adp.detect-secrets.audit/v2"):
        from run_detect_secrets import validate_audit_coverage
        validate_audit_coverage(secret_scan, secret_audit)

    cfn = load_json(root / "cfn-nag" / "cfn-nag-results.json")
    if not isinstance(cfn, list):
        raise ValueError("cfn-nag output must be a list")
    scanned = set()
    for entry in cfn:
        if not isinstance(entry, dict):
            raise ValueError("cfn-nag output contains a non-object record")
        scanned.add(Path(entry.get("filename", "")).name)
        file_results = entry.get("file_results")
        violations = file_results.get("violations") if isinstance(file_results, dict) else None
        if not isinstance(violations, list) or any(not isinstance(item, dict) or item.get("id") == "FATAL" for item in violations):
            raise ValueError("cfn-nag output is invalid or contains a FATAL record")
    if scanned != EXPECTED_CFN_TEMPLATES:
        raise ValueError(f"cfn-nag template coverage mismatch: {sorted(scanned)}")

    npm_dir = root / "npm-audit"
    npm_files = only_files(npm_dir, "*.json")
    if npm_files != EXPECTED_NPM_REPORTS:
        raise ValueError(f"npm audit matrix coverage mismatch: {sorted(npm_files)}")
    for filename in npm_files:
        report = load_json(npm_dir / filename)
        if not isinstance(report, dict) or not isinstance(report.get("auditReportVersion"), int) or not isinstance(report.get("vulnerabilities"), dict):
            raise ValueError(f"invalid npm audit report: {filename}")

    grype_dir = root / "grype"
    grype_files = only_files(grype_dir, "*.sarif")
    expected_grype = {name + ".sarif" for name in expected_images}
    if grype_files != expected_grype:
        raise ValueError(f"Grype image coverage mismatch: expected {sorted(expected_grype)}, got {sorted(grype_files)}")
    for filename in grype_files:
        require_sarif(grype_dir / filename)

    syft_dir = root / "syft"
    syft_files = only_files(syft_dir, "*.cdx.json")
    expected_syft = {name + ".cdx.json" for name in expected_images}
    if syft_files != expected_syft:
        raise ValueError(f"Syft image coverage mismatch: expected {sorted(expected_syft)}, got {sorted(syft_files)}")
    for filename in syft_files:
        require_cyclonedx(syft_dir / filename)


def validate_coverage(report, tool: str, source_revision: str, expected_images: set[str]):
    if not isinstance(report, dict) or report.get("tool") != tool or report.get("commit") != source_revision:
        raise ValueError(f"{tool} coverage has the wrong tool or source revision")
    targets = report.get("targets")
    if not isinstance(targets, list) or report.get("expected") != len(expected_images) or report.get("succeeded") != len(expected_images):
        raise ValueError(f"{tool} coverage is incomplete")
    by_name = {item.get("name"): item for item in targets if isinstance(item, dict)}
    if len(targets) != len(expected_images) or set(by_name) != expected_images or any(item.get("status") != "succeeded" for item in by_name.values()):
        raise ValueError(f"{tool} coverage target inventory is incomplete")
    if report.get("scope") == "all" or "excluded_non_runtime" in report:
        from security_image_targets import DOC_API_COMPAT, ORIGINAL_COVERAGE, non_runtime_fixtures

        expected_exclusion = non_runtime_fixtures(Path(__file__).resolve().parents[2])
        if (len(expected_exclusion) != 1
                or expected_exclusion[0]["dockerfile"] != DOC_API_COMPAT
                or report.get("excluded_non_runtime") != expected_exclusion
                or report.get("original_gap") != ORIGINAL_COVERAGE
                or report.get("discovered") != len(expected_images) + len(expected_exclusion)):
            raise ValueError(f"{tool} inventory correction is missing or inconsistent")
    return by_name


def observed_results(evidence_root: Path, provenance_output: Path, source_revision: str, expected_images: set[str], cleanup_complete: bool):
    if not SHA.fullmatch(source_revision):
        raise ValueError("source revision is not a full SHA")
    reports = {
        tool: load_json(evidence_root / tool / "coverage.json") for tool in ("grype", "syft")
    }
    coverage = {
        tool: validate_coverage(report, tool, source_revision, expected_images)
        for tool, report in reports.items()
    }
    correction_fields = ("scope", "excluded_non_runtime", "original_gap", "discovered")
    if any(reports["grype"].get(field) != reports["syft"].get(field) for field in correction_fields):
        raise ValueError("Grype and Syft inventory corrections differ")
    provenance_output.mkdir(parents=True, exist_ok=True)
    images = {}
    for name in sorted(expected_images):
        digests = {}
        for tool in coverage:
            digest = coverage[tool][name].get("digest")
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError(f"invalid {tool} image digest for {name}")
            digests[tool] = digest
        for tool, digest in digests.items():
            path = evidence_root / tool / "provenance" / f"{name}.json"
            provenance = load_json(path)
            item = coverage[tool][name]
            expected = {
                "artifact_sha256": item.get("artifact_sha256"),
                "digest": digest,
                "name": name,
                "source_revision": source_revision,
                "tool": tool,
            }
            evidence_suffixes = {
                "raw_artifact_sha256": ".raw.sarif",
                "suppression_summary_sha256": ".suppression-summary.json",
                "scanner_metadata_sha256": ".scanner-metadata.json",
            }
            extended = {"build_args", *evidence_suffixes}
            if not isinstance(provenance, dict):
                raise ValueError(f"invalid {tool} provenance for {name}")
            # Preserve legacy receipts, but require the complete extended schema
            # whenever either coverage or provenance claims the newer evidence.
            if extended.intersection(provenance) or extended.intersection(item):
                build_args = item.get("build_args")
                if not isinstance(build_args, dict) or any(
                    not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key)
                    or not isinstance(value, str)
                    or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", value)
                    or value.endswith("sha256:" + "0" * 64)
                    for key, value in build_args.items()
                ):
                    raise ValueError(f"invalid {tool} build inputs for {name}")
                expected["build_args"] = build_args
                for field, suffix in evidence_suffixes.items():
                    checksum = item.get(field)
                    expected[field] = checksum
                    if tool == "syft":
                        if checksum is not None:
                            raise ValueError(f"unexpected Syft {field} for {name}")
                        continue
                    artifact_path = evidence_root / tool / "artifacts" / f"{name}{suffix}"
                    if (
                        not isinstance(checksum, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                        or not artifact_path.is_file()
                        or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != checksum
                    ):
                        raise ValueError(f"invalid {tool} {field} evidence for {name}")
            if provenance != expected or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("artifact_sha256", ""))):
                raise ValueError(f"invalid {tool} provenance for {name}")
            suffix = ".sarif" if tool == "grype" else ".cdx.json"
            artifact = evidence_root / tool / "artifacts" / f"{name}{suffix}"
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != provenance["artifact_sha256"]:
                raise ValueError(f"{tool} artifact bytes do not match provenance for {name}")
            shutil.copyfile(path, provenance_output / f"{tool}-{name}.json")
        images[name] = {
            tool: {
                "digest": digests[tool],
                "provenance_path": f"{tool}-{name}.json",
            }
            for tool in ("grype", "syft")
        }
    return {
        "source_revision": source_revision,
        "coverage_complete": True,
        "cleanup_complete": cleanup_complete,
        "images": images,
    }


def sanitize_summary(summary_path: Path, output: Path, source_revision: str, correlation: str):
    summary = load_json(summary_path)
    if not isinstance(summary, dict):
        raise ValueError("security summary must be an object")
    tools = {}
    for tool, result in sorted(summary.items()):
        if not isinstance(result, dict):
            raise ValueError(f"summary result for {tool} must be an object")
        tools[tool] = {
            key: result.get(key, 0)
            for key in ("new_count", "resolved_count", "new_unrated_count")
        }
        if "legacy_partial_identity_count" in result:
            tools[tool]["legacy_partial_identity_count"] = result["legacy_partial_identity_count"]
        if any(type(value) is not int or value < 0 for value in tools[tool].values()):
            raise ValueError(f"summary counts for {tool} are invalid")
    output.write_text(json.dumps({
        "evidence_schema": "security-reconciliation/v1",
        "source_revision": source_revision,
        "correlation": correlation,
        "tools": tools,
    }, sort_keys=True) + "\n")


def aws_json(arguments):
    output = subprocess.check_output(["aws", *arguments, "--output", "json"], timeout=30)
    return json.loads(output) if output.strip() else {}


def cleanup_children(state_dir: Path, expected_children: set[str], region: str, state_bucket: str, aws=aws_json, sleep=time.sleep):
    result = {"cleanup_complete": False, "children": {}, "errors": []}
    files = only_files(state_dir, "*.json") if state_dir.is_dir() else set()
    expected_files = {name + ".json" for name in expected_children}
    if files != expected_files:
        result["errors"].append(f"child state coverage mismatch: expected {sorted(expected_files)}, got {sorted(files)}")
    for name in sorted(expected_children):
        state_path = state_dir / f"{name}.json"
        if not state_path.is_file():
            continue
        try:
            state = load_json(state_path)
            build_id = state.get("build_id")
            source_key = state.get("source_key")
            if not isinstance(source_key, str) or not re.fullmatch(r"codebuild/src/adp-[A-Za-z0-9_-]+-(?:grype|syft)-scan/[0-9a-f]{40}-[0-9]+-[0-9]+-[A-Za-z0-9_-]+\.zip", source_key):
                raise ValueError("state lacks an invocation-owned source key")
            if not isinstance(build_id, str) or not build_id:
                aws(["s3api", "delete-object", "--bucket", state_bucket, "--key", source_key, "--region", region])
                result["children"][name] = {"build_id": None, "terminal_status": "UNPROVEN", "source_removed": True}
                result["errors"].append(f"{name}: build start was not proven and no build ID was recorded")
                continue
            if build_id.split(":", 1)[0] != source_key.split("/")[2]:
                raise ValueError("child project does not own the recorded source")
            status = None
            for _ in range(21):
                response = aws(["codebuild", "batch-get-builds", "--ids", build_id, "--region", region])
                builds = response.get("builds", [])
                if len(builds) != 1 or builds[0].get("id") != build_id:
                    raise ValueError("CodeBuild did not return the recorded child")
                status = builds[0].get("buildStatus")
                if status in TERMINAL_BUILD_STATES:
                    break
                aws(["codebuild", "stop-build", "--id", build_id, "--region", region])
                sleep(3)
            if status not in TERMINAL_BUILD_STATES:
                raise ValueError(f"child did not reach a terminal state: {status}")
            aws(["s3api", "delete-object", "--bucket", state_bucket, "--key", source_key, "--region", region])
            result["children"][name] = {"build_id": build_id, "terminal_status": status, "source_removed": True}
        except (KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {exc}")
    result["cleanup_complete"] = not result["errors"] and set(result["children"]) == expected_children
    return result


def validate_cleanup_receipt(path: Path, expected_complete: bool):
    receipt = load_json(path)
    if (not isinstance(receipt, dict)
            or set(receipt) != {"cleanup_complete", "children", "errors"}
            or type(receipt["cleanup_complete"]) is not bool
            or receipt["cleanup_complete"] != expected_complete
            or not isinstance(receipt["children"], dict)
            or not isinstance(receipt["errors"], list)):
        raise ValueError("cleanup receipt does not match the observed cleanup state")
    if expected_complete:
        if receipt["errors"] or set(receipt["children"]) != {"grype", "syft"}:
            raise ValueError("cleanup receipt does not prove both children finished")
        for tool, child in receipt["children"].items():
            if (not isinstance(child, dict)
                    or set(child) != {"build_id", "terminal_status", "source_removed"}
                    or not isinstance(child["build_id"], str)
                    or not child["build_id"].split(":", 1)[0].endswith(f"-{tool}-scan")
                    or child["terminal_status"] not in TERMINAL_BUILD_STATES
                    or child["source_removed"] is not True):
                raise ValueError(f"cleanup receipt lacks terminal child and source removal: {tool}")
    return receipt


def expected_image_names(path: Path) -> set[str]:
    targets = load_json(path)
    names = {item.get("name") for item in targets if isinstance(item, dict)} if isinstance(targets, list) else set()
    if not names or None in names or len(names) != len(targets):
        raise ValueError("expected image inventory is invalid")
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-findings")
    validate.add_argument("--findings-dir", type=Path, required=True)
    validate.add_argument("--inventory", type=Path, required=True)
    observe = subparsers.add_parser("observed-results")
    observe.add_argument("--evidence-dir", type=Path, required=True)
    observe.add_argument("--provenance-output", type=Path, required=True)
    observe.add_argument("--inventory", type=Path, required=True)
    observe.add_argument("--source-revision", required=True)
    observe.add_argument("--cleanup-complete", choices=("true", "false"), required=True)
    observe.add_argument("--output", type=Path, required=True)
    sanitize = subparsers.add_parser("sanitize-summary")
    sanitize.add_argument("--summary", type=Path, required=True)
    sanitize.add_argument("--output", type=Path, required=True)
    sanitize.add_argument("--source-revision", required=True)
    sanitize.add_argument("--correlation", required=True)
    cleanup = subparsers.add_parser("cleanup-children")
    cleanup.add_argument("--state-dir", type=Path, required=True)
    cleanup.add_argument("--region", required=True)
    cleanup.add_argument("--state-bucket", required=True)
    cleanup.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "validate-findings":
        validate_findings(args.findings_dir, expected_image_names(args.inventory))
    elif args.command == "observed-results":
        cleanup_complete = args.cleanup_complete == "true"
        validate_cleanup_receipt(args.evidence_dir / "cleanup.json", cleanup_complete)
        result = observed_results(args.evidence_dir, args.provenance_output, args.source_revision, expected_image_names(args.inventory), cleanup_complete)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
    elif args.command == "sanitize-summary":
        sanitize_summary(args.summary, args.output, args.source_revision, args.correlation)
    else:
        result = cleanup_children(args.state_dir, {"grype", "syft"}, args.region, args.state_bucket)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
        if not result["cleanup_complete"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

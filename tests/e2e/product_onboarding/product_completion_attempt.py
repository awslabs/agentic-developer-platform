"""Execute two fresh live passes and assemble the consolidated Product evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from tests.e2e.new_ui.product_suite_cleanup import validate_cleanup
from tests.e2e.product_onboarding.product_completion_runner import (
    _pytest_results, required_cases, run_inventory, verify_fixture_providers,
)
from tests.e2e.product_onboarding.product_v02_record import private_json

BASELINE_SELECTORS = tuple(
    "tests/e2e/new_ui/test_product_profile.py::" + name
    for name in ("test_product_create", "test_product_conflict_denial", "test_product_evidence")
)


def owned_file(root, path, *, limit=1024 * 1024, allow_empty=False):
    file = Path(path)
    if (not file.is_absolute() or file.is_symlink() or not file.is_file()
            or not file.resolve().is_relative_to(root)
            or file.stat().st_uid != os.getuid() or file.stat().st_mode & 0o777 != 0o600
            or not (0 if allow_empty else 1) <= file.stat().st_size <= limit):
        raise ValueError("Regression evidence must be private, bounded and owned by this attempt")
    return file


def load(root, path):
    return json.loads(owned_file(root, path).read_text(), parse_constant=lambda _: (
        _ for _ in ()).throw(ValueError("Non-finite regression evidence")))


def reference(root, path):
    return "evidence/" + Path(path).relative_to(root).as_posix()


def run_baseline(root, prefix):
    directory = root / "baseline"
    directory.mkdir(mode=0o700)
    return _pytest_results(
        BASELINE_SELECTORS, execute=None, evidence_dir=directory, working_directory=directory,
        environment_overrides={"PRODUCT_B03_MODE": "false", "PRODUCT_V11_MODE": "false",
                               "PRODUCT_SUITE_ID": prefix + "-baseline"},
    )


def verify_results(root, pass_root, results, selectors, environment, cleanups):
    if (not isinstance(results, list) or any(not isinstance(item, dict) for item in results)
            or [item.get("selector") for item in results] != list(selectors)):
        raise ValueError("Regression produced missing or unexpected selectors")
    verified = []
    for item in results:
        if item.get("status") != "passed":
            raise ValueError("Regression selector failed or did not execute")
        report = owned_file(pass_root, item.get("report_path", ""))
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        if digest != item.get("report_sha256"):
            raise ValueError("Regression report changed after execution")
        if item["selector"].startswith("case:"):
            if item.get("run_id") != f"product-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}":
                raise ValueError("CLI report belongs to another workflow attempt")
        elif type(item.get("tests")) is not int or item["tests"] < 1 or not item.get("command"):
            raise ValueError("Regression selector has no executed command")
        suite_id = item.get("suite_id")
        if not isinstance(suite_id, str) or not suite_id.startswith(pass_root.name + "-"):
            raise ValueError("Regression fixture belongs to another pass")
        cleanup_file = owned_file(pass_root, item.get("cleanup_path", ""))
        cleanup = validate_cleanup(load(pass_root, cleanup_file), {**environment, "PRODUCT_SUITE_ID": suite_id})
        previous = cleanups.setdefault(cleanup_file, cleanup)
        if previous != cleanup:
            raise ValueError("Suite cleanup changed during evidence assembly")
        verified.append({"selector": item["selector"], "status": "passed",
                         "report": reference(root, report), "sha256": digest,
                         "tests": item.get("tests"), "command": item.get("command"),
                         "cleanup": reference(root, cleanup_file)})
    return verified


def execute_attempt(root, environment):
    """Missing producers fail before fixtures; only completed observations become checks."""
    from tests.e2e.product_onboarding.product_completion_regression import select_manifest

    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o777 != 0o700):
        raise ValueError("Regression needs a private attempt directory")
    cases = required_cases()
    verify_fixture_providers(cases)
    selected = load(root, root / "required-checks.json")
    manifest = select_manifest(
        root / "release-manifest.json", release_id=selected["release_id"],
        manifest_sha256=selected["manifest_sha256"], environment=environment,
    )
    identity = {"release_id": manifest["release_id"], "manifest_sha256": selected["manifest_sha256"],
                "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
                "workflow_run_id": int(environment["GITHUB_RUN_ID"]),
                "workflow_attempt": int(environment["GITHUB_RUN_ATTEMPT"])}
    passes, all_cleanups, owned_products = {}, {}, []
    for prefix in ("pass-1", "pass-2"):
        pass_root = root / prefix
        pass_root.mkdir(mode=0o700)
        results = run_inventory(pass_root, namespace_prefix=prefix)
        if set(results) != set(cases):
            raise ValueError("Regression pass has missing or unexpected cases")
        cleanups, observed = {}, {}
        for check_id, spec in cases.items():
            selectors = (*spec.producers, *((spec.validator,) if spec.validator else ()))
            observed[check_id] = verify_results(root, pass_root, results[check_id], selectors, environment, cleanups)
        observed["baseline-regression"] = verify_results(
            root, pass_root, run_baseline(pass_root, prefix), BASELINE_SELECTORS, environment, cleanups,
        )
        product_ids, project_ids = set(), set()
        for cleanup in cleanups.values():
            for receipt in cleanup["cases"].values():
                if receipt["product_id"] in product_ids or receipt["project_id"] in project_ids:
                    raise ValueError("Different suites reused owned fixture data")
                product_ids.add(receipt["product_id"])
                project_ids.add(receipt["project_id"])
        if owned_products and (product_ids & owned_products[0][0] or project_ids & owned_products[0][1]):
            raise ValueError("Repeatability requires fresh Product and Project fixtures")
        owned_products.append((product_ids, project_ids))
        if hashlib.sha256((root / "release-manifest.json").read_bytes()).hexdigest() != identity["manifest_sha256"]:
            raise ValueError("Selected release changed between regression passes")
        passes[prefix] = observed
        all_cleanups[prefix] = [reference(root, path) for path in cleanups]
        private_json(pass_root / "result.json", {**identity, "pass": prefix, "cases": observed})
    signatures = [
        {case: [(item["selector"], item["status"], item["tests"]) for item in result]
         for case, result in passes[prefix].items()}
        for prefix in ("pass-1", "pass-2")
    ]
    if signatures[0] != signatures[1]:
        raise ValueError("Repeat passes did not execute the same successful assertions")
    checks = []
    for check_id in (*cases, "baseline-regression"):
        path = root / (check_id + ".json")
        private_json(path, {**identity, "check_id": check_id,
                           "expected": "Selected assertions and owned cleanup pass in both runs",
                           "actual": {prefix: observed[check_id] for prefix, observed in passes.items()}})
        checks.append({"id": check_id, "outcome": "passed", "evidence_ref": reference(root, path)})
    for check_id, actual in (
        ("repeatability", {"passes": ["pass-1", "pass-2"], "same_assertions": signatures[0],
                           "fresh_fixtures": [{"products": sorted(ids), "projects": sorted(projects)}
                                              for ids, projects in owned_products]}),
        ("cleanup", all_cleanups),
    ):
        path = root / (check_id + ".json")
        private_json(path, {**identity, "check_id": check_id,
                           "expected": "Two fresh passes retain verified ownership and the same successful assertions",
                           "actual": actual})
        checks.append({"id": check_id, "outcome": "passed", "evidence_ref": reference(root, path)})
    files = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError("Regression archive contains a symlink")
        if not file.is_file():
            continue
        file = owned_file(root, file, limit=64 * 1024 * 1024, allow_empty=True)
        files.append({"path": reference(root, file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                      "bytes": file.stat().st_size})
        if len(files) > 4096:
            raise ValueError("Regression evidence inventory exceeded its bound")
    index = root / "evidence-index.json"
    private_json(index, {**identity, "check_id": "evidence-index", "checks": checks, "files": files})
    checks.append({"id": "evidence-index", "outcome": "passed", "evidence_ref": reference(root, index)})
    return checks


def publish_standalone(root, environment):
    from tests.e2e.product_onboarding.product_completion_regression import guard_standalone, required_checks

    guard_standalone(environment)
    checks = execute_attempt(root, environment)
    if {check["id"] for check in checks} != set(required_checks()):
        raise ValueError("Standalone regression has missing or unexpected checks")
    return private_json(Path(root) / "standalone-results.json", {
        "mode": "standalone", "run_id": int(environment["GITHUB_RUN_ID"]),
        "run_attempt": int(environment["GITHUB_RUN_ATTEMPT"]),
        "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
        "checks": checks, "cleanup": "passed",
    })


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Execute the standalone live Product regression")
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    print(publish_standalone(args.attempt, os.environ))

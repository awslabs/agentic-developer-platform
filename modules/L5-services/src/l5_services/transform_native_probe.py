"""Independently read native Transform outputs and rerun detached checks."""

import re
from hashlib import sha256

from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
from l5_services.adapters.transform.provenance import (
    capture_original_patch, compare_source_trees, verify_original_patch,
)
from l5_services.adapters.transform.verification import verify_detached_checks


async def probe_native_result(driver, *, run_id, scope, source_revision, capability):
    if (not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", source_revision)
            or capability not in {"refactor", "dependency_upgrade"}):
        raise ValueError("native proof needs a pinned source and supported goal")
    run = await driver.read_run(run_id)
    result = await driver.read_result(run_id)
    if (not isinstance(run, dict) or not isinstance(result, dict)
            or run.get("run_id") != run_id or result.get("run_id") != run_id
            or run.get("scope") != scope or result.get("scope") != scope
            or run.get("state") != "completed" or run.get("provider_state") != "completed"
            or result.get("verification_status") != "succeeded"):
        raise ValueError("native run or result is not independently readable and complete")
    source = run.get("input", {}).get("sources", [])
    goal = run.get("input", {}).get("transform_input", {}).get("goal", {})
    if (len(source) != 1 or source != result.get("source_ref")
            or source[0].get("record_revision") != source_revision
            or source[0].get("tenant_id") != scope["tenant_id"]
            or source[0].get("project_id") != scope["project_id"]
            or goal.get("kind") != capability or run.get("native_job_ref") != result.get("native_job_ref")):
        raise ValueError("native result differs from sealed goal, scope or source revision")
    native = run["native_job_ref"]
    observed = await driver.read_native(native)
    if (not isinstance(native, dict) or not isinstance(observed, dict)
            or observed.get("native_id") != native.get("native_id")
            or observed.get("executor_uid") != native.get("executor_uid")
            or observed.get("attempt_token") != native.get("attempt_token")
            or observed.get("input_hash") != native.get("input_hash")
            or observed.get("state") != "completed"):
        raise ValueError("native session readback is missing or different")
    trees = await driver.read_trees(source[0], native)
    base = trees["base_files"]
    candidate = trees["candidate_files"]
    base_modes = trees["base_modes"]
    candidate_modes = trees["candidate_modes"]
    comparison = compare_source_trees(base, candidate, base_modes=base_modes, candidate_modes=candidate_modes)
    if not comparison["changed_files"] or result.get("source_trees") != comparison:
        raise ValueError("independently read candidate differs from reported trees")
    patch_ref = result.get("retained_artifacts", {}).get("original_patch", {})
    patch_bytes = await driver.read_artifact(patch_ref)
    patch = result.get("original_patch", {})
    if (not isinstance(patch_bytes, bytes) or not patch_bytes
            or sha256(patch_bytes).hexdigest() != patch_ref.get("sha256")
            or len(patch_bytes) != patch_ref.get("bytes")
            or patch.get("sha256") != patch_ref.get("sha256")
            or patch.get("bytes") != patch_ref.get("bytes")
            or patch.get("native_job_ref") != native
            or patch.get("provider_pins") != result.get("provider_attribution")):
        raise ValueError("original provider patch is not retained with native attribution")
    original = capture_original_patch(patch_bytes, native, result["provider_attribution"],
                                      {"source_snapshot_ids": run["admission"]["source_snapshots"]})
    verify_original_patch(base, comparison["candidate_tree"], original, base_modes)
    expected_dependency = {"status": "not_applicable"}
    if capability == "dependency_upgrade":
        expected_dependency = {"status": "passed", **compare_dependency_upgrade(
            base, candidate, goal["target"], run["admission"]["transform_artifact_hashes"])}
    if result.get("dependency_comparison") != expected_dependency:
        raise ValueError("dependency upgrade differs from independent source comparison")
    policy = run["admission"]["transform_test_policy"]
    detached = await verify_detached_checks(driver.detached_runner, policy, base, candidate,
                                            driver.authority, base_modes=base_modes,
                                            candidate_modes=candidate_modes)
    for stage in ("tests_before", "tests_after"):
        reported = result.get(stage)
        verified = detached[stage]
        if (not isinstance(reported, list) or len(reported) != len(policy["commands"])
                or any(not isinstance(entry, dict) or entry.get("status") != "passed"
                       or not isinstance(entry.get("receipt"), dict)
                       or entry["receipt"].get("stage") != ("before" if stage == "tests_before" else "after")
                       or entry["receipt"].get("source_tree") != comparison[
                           "base_tree" if stage == "tests_before" else "candidate_tree"]
                       or entry["receipt"].get("image_digest") != policy["image_digest"]
                       or entry["receipt"].get("bundle_sha256") != policy["bundle_sha256"]
                       or entry["receipt"].get("lock_sha256") != policy["lock_sha256"]
                       or entry["receipt"].get("oracle_version") != policy["oracle_version"]
                       or entry["receipt"].get("argv") != command["argv"]
                       or entry["receipt"].get("cwd") != command["cwd"]
                       or entry["receipt"].get("exit_code") != 0
                       or entry["receipt"].get("timed_out") is not False
                       or type(entry["receipt"].get("test_count")) is not int
                       or entry["receipt"]["test_count"] < command["minimum_test_count"]
                       for entry, command in zip(reported, policy["commands"]))
                or any(entry.get("status") != "passed" for entry in verified)):
            raise ValueError("reported or independently rerun detached tests failed")
    return {"phase": "native-observations", "live_qualified": False,
            "source_revision": source_revision, "base_tree": comparison["base_tree"],
            "candidate_tree": comparison["candidate_tree"], "native_id": native["native_id"],
            "original_patch_sha256": original.sha256, "checks": detached}

"""Detached pre-submit checks bound to immutable source and policy revisions."""

import asyncio
import json
from hashlib import sha256

from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.adapters.transform.verification import _check_policy, _timestamp, verify_detached_checks


def pinned_test_policy(admission):
    settings = admission.get("transform_settings") or {}
    policy = admission.get("transform_test_policy")
    if (not isinstance(policy, dict) or not isinstance(settings.get("test_policy_ref"), dict)
            or policy.get("test_policy_ref") != settings["test_policy_ref"]):
        raise ValueError("resolved Transform test policy differs from pinned reference")
    _check_policy(policy)
    if any(command["timeout_seconds"] > 120 for command in policy["commands"]):
        raise ValueError("selected Transform executor supports at most 120 seconds per check")
    digest = sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return policy, digest


def manifest_digest(manifest):
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def verify_pre_submit_baseline(run, bridge, runner, authority, *, clock):
    policy, policy_sha256 = pinned_test_policy(run.admission)
    manifest = run.manifest
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run.run_id
            or manifest.get("sources") != run.input["sources"]
            or manifest.get("source_snapshot_ids") != [
                snapshot["snapshot_id"] for snapshot in run.admission["source_snapshots"]]
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise ValueError("detached baseline requires sealed source files")
    files = await asyncio.wait_for(bridge.read_sealed_source(manifest=manifest, authority=authority), timeout=5)
    if (not isinstance(files, dict) or set(files) != {entry["path"] for entry in manifest["files"]}
            or any(not isinstance(files[entry["path"]], bytes)
                   or len(files[entry["path"]]) != entry["bytes"]
                   or sha256(files[entry["path"]]).hexdigest() != entry["sha256"]
                   for entry in manifest["files"])):
        raise ValueError("detached baseline differs from sealed source files")
    modes = {entry["path"]: entry.get("mode", "100644") for entry in manifest["files"]}
    tree = compare_source_trees(files, files, base_modes=modes, candidate_modes=modes)["base_tree"]
    verified = await verify_detached_checks(runner, policy, files, None, authority, base_modes=modes)
    completed_at = clock()
    before = verified["tests_before"]
    passed = bool(before) and all(
        check["status"] == "passed" and _timestamp(check["receipt"].get("ended_at")) <= completed_at
        for check in before)
    return {"kind": "transform_pre_submit_baseline", "schema_version": "1.0",
            "manifest_sha256": manifest_digest(manifest), "policy_sha256": policy_sha256,
            "source_tree": tree, "status": "passed" if passed else "failed",
            "checks": before}

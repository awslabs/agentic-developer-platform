"""Retain independently readable Transform evidence before scratch cleanup."""

import asyncio
from datetime import UTC, datetime
from hashlib import sha256


async def retain_transform_artifacts(run, result, bridge, runner, store, authority, *, now=None):
    """Copy native patch and detached logs into immutable, scoped storage."""
    now = now or datetime.now(UTC)
    original = result.get("original_patch")
    if not isinstance(original, dict):
        raise ValueError("original patch is unavailable for retention")
    items = [("original_patch", original, bridge.read_original_patch)]
    for stage in ("tests_before", "tests_after"):
        for index, check in enumerate(result.get(stage, [])):
            receipt = check.get("receipt")
            if not isinstance(receipt, dict):
                continue
            if runner is None:
                raise ValueError("detached log reader is unavailable")
            for stream in ("stdout", "stderr"):
                reference = receipt.get(stream)
                if not isinstance(reference, dict):
                    raise ValueError("detached log reference is unavailable")
                items.append((f"{stage}-{index}-{stream}", reference, runner.read_log))

    retained = {}
    for name, source, reader in items:
        digest = source.get("sha256")
        length = source.get("bytes")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(length) is not int or length < 0
                or not isinstance(source.get("artifact_id"), str)
                or not isinstance(source.get("version"), str) or not source["version"]):
            raise ValueError("evidence lacks a versioned digest")
        contents = await asyncio.wait_for(reader(reference=source, authority=authority), timeout=5)
        if not isinstance(contents, bytes) or len(contents) != length or sha256(contents).hexdigest() != digest:
            raise ValueError("source evidence changed before retention")
        reference = await asyncio.wait_for(store.put(
            tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.run_id,
            name=name, contents=contents, media_type=("text/x-diff" if name == "original_patch"
                                                   else "text/plain"), authority=authority), timeout=5)
        if (not isinstance(reference, dict) or reference.get("tenant_id") != run.tenant_id
                or reference.get("project_id") != run.project_id
                or not isinstance(reference.get("artifact_id"), str) or not reference["artifact_id"]
                or not isinstance(reference.get("version"), str) or not reference["version"]
                or reference.get("sha256") != digest or reference.get("bytes") != length
                or reference.get("media_type") != ("text/x-diff" if name == "original_patch" else "text/plain")):
            raise ValueError("retained artifact reference differs from source evidence")
        try:
            expiration = datetime.fromisoformat(reference["expires_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            raise ValueError("retained artifact lacks expiration") from None
        if expiration.tzinfo is None or expiration <= now or reference.get("revoked_at"):
            raise ValueError("retained artifact lacks current retention")
        readback = await asyncio.wait_for(store.read(reference=reference, authority=authority), timeout=5)
        if readback != contents:
            raise ValueError("retained artifact readback differs from original bytes")
        retained[name] = reference
    return retained


def normalize_transform_result(run, result_id, version, collected, now):
    """Project only verified Transform fields into the existing versioned result rows."""
    from datetime import UTC

    from l5_services.models.result import ResultDetail

    patch = collected.get("original_patch")
    retained = collected.get("retained_artifacts")
    native = patch.get("native_job_ref") if isinstance(patch, dict) else None
    pinned_fields = ("tenant_id", "project_id", "native_id", "executor_uid",
                     "attempt_token", "input_hash")
    if (not isinstance(patch, dict) or not isinstance(retained, dict)
            or not retained or retained.get("original_patch", {}).get("sha256") != patch.get("sha256")
            or not isinstance(native, dict) or not isinstance(run.native_job_ref, dict)
            or any(not isinstance(native.get(field), str) or not native[field]
                   or native[field] != run.native_job_ref.get(field) for field in pinned_fields)):
        raise ValueError("Transform result lacks a retained original patch and native identity")
    checks = collected.get("tests_before", []) + collected.get("tests_after", [])
    passed = (collected.get("scope_status") == "passed"
              and collected.get("dependency_comparison", {}).get("status") in {"passed", "not_applicable"}
              and bool(collected.get("tests_before")) and bool(collected.get("tests_after"))
              and all(item.get("status") == "passed" for item in checks))
    projection = {
        "schema_version": "1.0", "service_id": "transform", "result_id": result_id,
        "run_id": run.run_id, "revision": version, "scope": run.admission["scope"],
        "observed_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "source_ref": run.input["sources"], "configuration_revision": run.configuration_revision,
        "test_policy_ref": run.admission["transform_settings"]["test_policy_ref"],
        "provider_attribution": patch["provider_pins"], "native_job_ref": run.native_job_ref,
        "original_patch": patch, "source_trees": collected.get("source_trees"),
        "dependency_comparison": collected.get("dependency_comparison", {"status": "inconclusive"}),
        "scope_status": collected.get("scope_status", "inconclusive"),
        "tests_before": collected.get("tests_before", []),
        "tests_after": collected.get("tests_after", []),
        "verification_status": "succeeded" if passed else "partial",
        "limitations": collected.get("limitations", []), "cost_status": "unknown",
        "retained_artifacts": retained,
        "retention_expires_at": min(
            datetime.fromisoformat(reference["expires_at"].replace("Z", "+00:00"))
            for reference in retained.values()
        ).astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    return ResultDetail(tenant_id=run.tenant_id, result_id=result_id, payload=projection)

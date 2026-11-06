"""Read back retained Transform evidence and receipt-owned cleanup without deleting anything."""

from datetime import datetime
from hashlib import sha256


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _current(reference, now):
    try:
        expiration = datetime.fromisoformat(reference["expires_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        raise ValueError("retained artifact expiration is unavailable") from None
    return expiration.tzinfo is not None and expiration > now and not reference.get("revoked_at")


async def probe_cleanup(driver, *, run_id, scope, owned, adopted, now):
    _require(isinstance(scope, dict) and bool(scope.get("tenant_id"))
             and isinstance(owned, list) and bool(owned)
             and isinstance(adopted, list) and bool(adopted)
             and all(isinstance(item, dict) and item.get("tenant_id") == scope["tenant_id"]
                     and item.get("project_id") == scope.get("project_id")
                     and isinstance(item.get("record_id"), str) and bool(item["record_id"])
                     for item in owned + adopted)
             and len({item["record_id"] for item in owned + adopted}) == len(owned + adopted)
             and isinstance(now, datetime) and now.tzinfo is not None,
             "cleanup selection must have disjoint, scoped owned and adopted resources")
    result = await driver.read_result(run_id)
    run = await driver.read_run(run_id)
    _require(isinstance(result, dict) and isinstance(run, dict)
             and result.get("run_id") == run_id and run.get("run_id") == run_id
             and result.get("scope") == scope and run.get("scope") == scope,
             "retained result differs from selected scope")
    native = run.get("native_job_ref")
    _require(isinstance(native, dict) and isinstance(native.get("executor_uid"), str)
             and bool(native["executor_uid"]), "executor identity was not retained")
    termination = await driver.read_termination(native)
    _require(isinstance(termination, dict)
             and termination.get("pod_uid") == native["executor_uid"]
             and termination.get("observedTermination") is True
             and isinstance(termination.get("evidence_ref"), str) and bool(termination["evidence_ref"]),
             "executor termination has not been observed")
    retained = result.get("retained_artifacts")
    before = result.get("tests_before")
    after = result.get("tests_after")
    _require(isinstance(retained, dict) and isinstance(before, list) and bool(before)
             and isinstance(after, list) and bool(after),
             "patch or before/after test evidence was not retained")
    required = {"original_patch"} | {f"{stage}-{index}-{stream}"
                                    for stage, checks in (("tests_before", before), ("tests_after", after))
                                    for index in range(len(checks)) for stream in ("stdout", "stderr")}
    _require(set(retained) == required and all(isinstance(item, dict)
             and isinstance(item.get("receipt"), dict)
             for item in before + after), "retained test log inventory is incomplete")
    hashes = {}
    for name in sorted(required):
        reference = retained[name]
        _require(isinstance(reference, dict) and reference.get("tenant_id") == scope["tenant_id"]
                 and reference.get("project_id") == scope["project_id"]
                 and isinstance(reference.get("artifact_id"), str) and bool(reference["artifact_id"])
                 and isinstance(reference.get("version"), str) and bool(reference["version"])
                 and isinstance(reference.get("sha256"), str) and len(reference["sha256"]) == 64
                 and type(reference.get("bytes")) is int and reference["bytes"] >= 0
                 and _current(reference, now), "retained artifact is unscoped, expired or incomplete")
        data = await driver.read_artifact(reference)
        _require(isinstance(data, bytes) and len(data) == reference["bytes"]
                 and sha256(data).hexdigest() == reference["sha256"],
                 "retained artifact bytes differ after cleanup")
        if name == "original_patch":
            patch = result.get("original_patch", {})
            _require(data and patch.get("sha256") == reference["sha256"]
                     and patch.get("native_job_ref") == native,
                     "original patch or native attribution was lost")
        else:
            stage, index, stream = name.rsplit("-", 2)
            receipt = result[stage][int(index)]["receipt"].get(stream)
            _require(isinstance(receipt, dict)
                     and receipt.get("sha256") == reference["sha256"]
                     and receipt.get("bytes") == reference["bytes"],
                     "reported detached log differs from retained bytes")
        hashes[name] = reference["sha256"]
    identity = await driver.read_identity()
    _require(isinstance(identity, dict)
             and all(isinstance(identity.get(key), str) and bool(identity[key])
                     for key in ("account_id", "role_arn", "region")),
             "selected cleanup identity is unavailable")
    for reference in owned:
        _require(reference.get("record_type") == "transform_definition",
                 "owned cleanup is not a Transform definition")
        receipt = await driver.read_cleanup_receipt(reference)
        inventory = await driver.read_native_resource(reference)
        creation = receipt.get("creation_receipt") if isinstance(receipt, dict) else None
        absence = receipt.get("absence_evidence_ref") if isinstance(receipt, dict) else None
        _require(isinstance(receipt, dict) and receipt.get("absent") is True
                 and receipt.get("native_resource_ref") == reference
                 and isinstance(creation, dict) and creation.get("native_resource_ref") == reference
                 and bool(creation.get("operation_id")) and bool(creation.get("intent_hash"))
                 and receipt.get("provider_identity") == creation.get("provider_identity") == identity
                 and isinstance(absence, dict) and absence.get("tenant_id") == scope["tenant_id"]
                 and absence.get("project_id") == scope["project_id"]
                 and absence.get("record_type") == "transform_absence"
                 and absence.get("record_id") == reference["record_id"]
                 and bool(absence.get("record_revision"))
                 and inventory == {"native_resource_ref": reference, "absent": True,
                                   "absence_evidence_ref": absence},
                 "owned resource lacks a matching creation receipt and independent absence")
    for reference in adopted:
        receipt = await driver.read_cleanup_receipt(reference)
        inventory = await driver.read_native_resource(reference)
        _require(receipt is None and isinstance(inventory, dict)
                 and inventory.get("native_resource_ref") == reference
                 and inventory.get("present") is True and inventory.get("ownership") == "customer_linked"
                 and inventory.get("creation_receipt") is None,
                 "adopted resource is missing or was claimed as owned")
    scm = await driver.read_scm(run_id)
    _require(isinstance(scm, dict) and isinstance(scm.get("before"), dict)
             and scm.get("before") == scm.get("after")
             and bool(scm["before"].get("revision"))
             and isinstance(scm.get("evidence_ref"), str) and bool(scm["evidence_ref"]),
             "run cleanup or publication changed SCM without separate authorization")
    return {"phase": "cleanup-observations", "live_qualified": False,
            "artifact_hashes": hashes, "owned_absent": len(owned),
            "adopted_retained": len(adopted), "termination_ref": termination["evidence_ref"],
            "scm_ref": scm["evidence_ref"]}

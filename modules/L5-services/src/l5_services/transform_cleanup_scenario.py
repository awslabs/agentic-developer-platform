"""Compare offline retained artifacts and receipt-owned cleanup with separate storage readback."""

from hashlib import sha256

from l5_services.transform_cleanup_probe import probe_cleanup


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def verify_ec05(driver, *, run_id, scope, owned, adopted, now):
    report = await probe_cleanup(driver, run_id=run_id, scope=scope,
                                 owned=owned, adopted=adopted, now=now)
    oracle = await driver.read_cleanup_oracle(run_id)
    _require(isinstance(oracle, dict) and oracle.get("source") == "native-inventory"
             and oracle.get("run_id") == run_id and oracle.get("scope") == scope
             and isinstance(oracle.get("evidence_ref"), str) and bool(oracle["evidence_ref"])
             and oracle["evidence_ref"] not in {report["termination_ref"], report["scm_ref"]}
             and oracle.get("termination_ref") == report["termination_ref"]
             and isinstance(oracle.get("artifacts"), dict)
             and set(oracle["artifacts"]) == set(report["artifact_hashes"]),
             "independent cleanup and retention oracle is missing or unscoped")
    for name, expected_hash in report["artifact_hashes"].items():
        contents = oracle["artifacts"][name]
        _require(isinstance(contents, bytes) and sha256(contents).hexdigest() == expected_hash,
                 "retained bytes differ from independent storage readback")
    before = {reference["record_id"]: {"native_resource_ref": reference, "present": True}
              for reference in owned + adopted}
    after = {reference["record_id"]: {"native_resource_ref": reference,
                                     "present": reference in adopted}
             for reference in owned + adopted}
    _require(oracle.get("before") == before and oracle.get("after") == after
             and isinstance(oracle.get("creation_receipts"), dict)
             and set(oracle["creation_receipts"]) == {reference["record_id"] for reference in owned},
             "owned absence or adopted preservation differs from independent inventory")
    for reference in owned:
        receipt = await driver.read_cleanup_receipt(reference)
        _require(isinstance(receipt, dict)
                 and oracle["creation_receipts"][reference["record_id"]] == receipt.get("creation_receipt"),
                 "creation receipt does not bind independent owned-resource inventory")
    return {"phase": "offline-cleanup-retention", "live_qualified": False,
            "artifact_hashes": report["artifact_hashes"],
            "owned_absent": report["owned_absent"],
            "adopted_retained": report["adopted_retained"],
            "termination_ref": report["termination_ref"],
            "inventory_ref": oracle["evidence_ref"], "scm_ref": report["scm_ref"]}

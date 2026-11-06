"""Cross-check two fictional fixtures against an independent release and resource snapshot."""

from hashlib import sha256

from l5_services.transform_portability_report import RELEASE_DIGESTS, build_portability_report


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def verify_ec08(driver, *, fixture_ids):
    report = await build_portability_report(driver, fixture_ids=fixture_ids)
    oracle = await driver.read_portability_oracle()
    release = report["fixtures"][0]["release"]
    expected_release = {field: release[field] for field in ("source_revision", *RELEASE_DIGESTS)}
    _require(isinstance(oracle, dict) and oracle.get("source") == "release-inventory-ledger"
             and isinstance(oracle.get("evidence_ref"), str) and bool(oracle["evidence_ref"])
             and oracle.get("deployment_history") == report["deployment_history"]
             and oracle.get("release") == expected_release
             and isinstance(oracle.get("fixtures"), dict)
             and set(oracle["fixtures"]) == set(fixture_ids),
             "independent release and fixture ledger is missing or inconsistent")
    references = [oracle["evidence_ref"], report["deployment_history"]["evidence_ref"]]
    artifacts = {}
    for entry in report["fixtures"]:
        manifest = entry["fixture"]
        fixture_id = manifest["fixture_id"]
        snapshot = oracle["fixtures"][fixture_id]
        _require(isinstance(snapshot, dict)
                 and isinstance(snapshot.get("evidence_ref"), str)
                 and bool(snapshot["evidence_ref"])
                 and snapshot.get("manifest") == manifest
                 and snapshot.get("run_id") == entry["result"]["run_id"]
                 and snapshot.get("native_id") == entry["result"]["native_id"]
                 and snapshot.get("cost") == entry["cost"],
                 "fixture, native work or cost differs from the independent ledger")
        contents = snapshot.get("retained_patch_bytes")
        _require(isinstance(contents, bytes)
                 and sha256(contents).hexdigest() == entry["result"]["original_patch_sha256"]
                 and sha256(contents).hexdigest() == entry["cleanup"]["retained_patch_sha256"],
                 "independent retained patch bytes do not match both reported hashes")
        expected_inventory = {
            item["reference"]: {"present": False, "receipt_ref": item["receipt_ref"]}
            for item in entry["cleanup"]["owned_inventory"]
        }
        expected_inventory.update({
            item["reference"]: {"present": True, "receipt_ref": None}
            for item in entry["cleanup"]["adopted_inventory"]
        })
        _require(snapshot.get("inventory") == expected_inventory,
                 "independent inventory does not prove owned absence and adopted retention")
        references.extend((snapshot["evidence_ref"], manifest["evidence_ref"],
                           entry["release"]["evidence_ref"], entry["result"]["evidence_ref"],
                           entry["cleanup"]["evidence_ref"], entry["cost"]["evidence_ref"]))
        artifacts[fixture_id] = sha256(contents).hexdigest()
    _require(len(set(references)) == len(references),
             "release, fixture and cost evidence references are not independent")
    return {"phase": "offline-two-fixture-portability", "live_qualified": False,
            "acceptance_complete": False, "release_digests": expected_release,
            "fixture_ids": list(fixture_ids), "retained_patch_hashes": artifacts,
            "ledger_ref": oracle["evidence_ref"],
            "cost_statuses": {entry["fixture"]["fixture_id"]: entry["cost"]["status"]
                              for entry in report["fixtures"]}}

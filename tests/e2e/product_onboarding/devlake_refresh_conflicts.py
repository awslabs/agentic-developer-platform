"""Deterministic SCN-02 readback and cleanup contract for source enrichment."""

import hashlib
import re

from .devlake_profile_setup import FIELDS


def assert_refresh_conflicts(fixture, observations):
    before = observations["before"]
    after_initial = observations["after_initial"]
    after_edit = observations["after_edit"]
    native_before = observations["native_before"]
    native_after = observations["native_after"]
    assert all(view["tenant_id"] == fixture["tenant_id"] and
               (view["product_id"], view["project_id"]) == (fixture["product_id"], fixture["project_id"])
               for view in (before, after_initial, after_edit))
    assert before["revision"] == fixture["revision"]
    assert after_initial["revision"] == before["revision"] + 1
    assert after_edit["revision"] == after_initial["revision"] + 1
    assert after_edit["product"]["purpose"] != after_initial["product"]["purpose"]
    assert fixture["source_before"] == native_before["revision"]
    assert fixture["source_after"] == native_after["revision"] != native_before["revision"]
    assert native_after["binding_id"] == native_before["binding_id"] == fixture["binding_id"]
    assert native_after["generation"] == native_before["generation"] == fixture["binding_generation"]
    assert native_after["observed_at"] == fixture["observed_at"]
    for source in (native_before, native_after):
        digest = hashlib.sha256("\x00".join(("repository", source["namespace"], source["native_id"], source["name"])).encode()).hexdigest()
        assert source["revision"] == "observation-sha256:" + digest

    manual = before["configuration"]["basic"]["repositories"]
    assert len(manual) == 1
    assert before["configuration"]["basic"].get("trackers", []) == []
    assert any(proof["paths"] == ["/configuration/basic/repositories"] and proof["state"] == "accepted"
               and proof["method"] == "manual" for proof in before["provenance"])
    assert after_initial["configuration"]["basic"]["repositories"] == manual
    assert after_initial["provenance"][:len(before["provenance"])] == before["provenance"]
    assert after_edit["configuration"] == after_initial["configuration"]
    assert after_edit["provenance"] == after_initial["provenance"]
    assert observations["after_stale"] == observations["after_old"] == observations["after_fresh"] == after_edit

    initial = observations["initial"]
    assert initial["status"] == 200 and initial["revision"] == after_initial["revision"]
    assert initial["coverage"] == "complete" and initial["suggestions"] == []
    assert initial["conflicts"] == [{"field_path": "/configuration/basic/repositories", "reason": "confirmed_or_changed"}]
    assert len(initial["applied"]) == 1
    tracker = after_initial["configuration"]["basic"]["trackers"]
    assert len(tracker) == 1 and tracker[0]["record_type"] == FIELDS["jira"][0]
    assert tracker[0]["tenant_id"] == fixture["tenant_id"] and tracker[0]["project_id"] == fixture["project_id"]
    assert tracker[0]["record_revision"] == fixture["tracker_revision"]
    assert initial["applied"][0] in after_initial["provenance"]
    proof = initial["applied"][0]
    assert proof["paths"] == ["/configuration/basic/trackers"] and proof["source_refs"] == tracker
    assert proof["state"] == "accepted" and proof["method"] == "connector"
    assert proof["field_map_version"] == "devlake-profile-v1" and proof["revision_basis"] == "observation_only"
    assert proof["binding_id"] == fixture["tracker_binding_id"] and proof["binding_generation"] == fixture["tracker_generation"]
    assert proof["coverage"] == "complete" and proof["observed_at"] == fixture["observed_at"]

    assert observations["stale"] == {"status": 412, "code": "stale_revision"}
    assert observations["old"] == {"status": 409, "code": "source_changed"}
    fresh = observations["fresh"]
    assert fresh["status"] == 200 and fresh["revision"] == after_edit["revision"]
    assert fresh["coverage"] == "complete" and fresh["applied"] == [] and fresh["suggestions"] == []
    assert {item["field_path"] for item in fresh["conflicts"]} == {
        "/configuration/basic/repositories", "/configuration/basic/trackers"}
    assert all(item["reason"] == "confirmed_or_changed" for item in fresh["conflicts"])
    for response in (initial, observations["stale"], observations["old"], fresh):
        assert all(manual[0][field] not in str(response) for field in ("record_id", "source_connection_id"))
    cleanup = observations["cleanup"]
    assert cleanup["unlinked_binding_ids"] == {fixture["binding_id"], fixture["tracker_binding_id"]}
    assert cleanup["remaining_owned_binding_ids"] == [] and cleanup["shared_connections_retained"] is True
    assert observations["native_after_cleanup"] == native_after
    assert observations["connections_after"] == observations["connections_before"]
    assert {item["id"] for item in observations["bindings_after"]} == cleanup["unlinked_binding_ids"]
    assert all(item["status"] == "unlinked" for item in observations["bindings_after"])


def run_refresh_conflicts(fixture, ports):
    """Fence the concurrent edit and changed observation separately, then retire only owned bindings."""
    required = ("verify_assets", "read_profile", "read_source", "read_bindings", "read_connections",
                "apply", "concurrent_edit", "refresh_source", "unlink_owned")
    if any(not callable(ports.get(name)) for name in required):
        raise ValueError("SCN-02 needs served assets, independent source/Profile reads and owned cleanup ports")
    assert set(fixture["keys"]) == {"initial", "stale", "old", "fresh"}
    assert len(set(fixture["keys"].values())) == 4
    assert all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) for key in fixture["keys"].values())
    ports["verify_assets"]()
    before = ports["read_profile"]()
    assert before["revision"] == fixture["revision"]
    native_before = ports["read_source"]()
    assert native_before["revision"] == fixture["source_before"]
    observed_bindings = ports["read_bindings"]()
    assert {item["id"] for item in observed_bindings if item["status"] == "healthy"} == {
        fixture["binding_id"], fixture["tracker_binding_id"]}
    observations = {"before": before, "native_before": native_before,
                    "connections_before": ports["read_connections"]()}
    try:
        observations["initial"] = ports["apply"](before["revision"], native_before["revision"], fixture["keys"]["initial"])
        observations["after_initial"] = ports["read_profile"]()
        ports["concurrent_edit"](observations["after_initial"]["revision"])
        observations["after_edit"] = ports["read_profile"]()
        observations["stale"] = ports["apply"](observations["after_initial"]["revision"], native_before["revision"], fixture["keys"]["stale"])
        observations["after_stale"] = ports["read_profile"]()
        observations["native_after"] = ports["refresh_source"]()
        observations["old"] = ports["apply"](observations["after_edit"]["revision"], native_before["revision"], fixture["keys"]["old"])
        observations["after_old"] = ports["read_profile"]()
        observations["fresh"] = ports["apply"](observations["after_edit"]["revision"], observations["native_after"]["revision"], fixture["keys"]["fresh"])
        observations["after_fresh"] = ports["read_profile"]()
    finally:
        observations["cleanup"] = ports["unlink_owned"]({fixture["binding_id"], fixture["tracker_binding_id"]})
        observations["bindings_after"] = ports["read_bindings"]()
        observations["connections_after"] = ports["read_connections"]()
        observations["native_after_cleanup"] = ports["read_source"]()
    assert_refresh_conflicts(fixture, observations)
    return observations

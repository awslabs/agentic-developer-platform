"""Offline-verifiable SCN-01 contract for an owned two-provider browser run."""

import hashlib
import re

FIELDS = {"github": ("repository", "repositories"), "jira": ("tracker", "trackers")}


def assert_profile_setup(fixture, browser, native, bindings, profile, cleanup):
    assert isinstance(fixture.get("source_revision"), str) and re.fullmatch(r"[0-9a-f]{40}", fixture["source_revision"])
    assert profile["tenant_id"] == fixture["tenant_id"]
    assert (profile["product_id"], profile["project_id"]) == (fixture["product_id"], fixture["project_id"])
    assert profile["revision"] > fixture["revision"]
    assert browser["session_id"] and browser["product_id"] == fixture["product_id"]
    assert browser["project_id"] == fixture["project_id"]
    assert browser["credential_entries"] == 0 and browser["second_setup_prompts"] == 0
    assert browser["complete"] is True and browser["coverage"] == {"github": "complete", "jira": "complete"}
    assert set(browser["selected"]) == set(browser["operations"]) == set(native) == set(FIELDS)
    assert all(browser["operations"][provider] for provider in FIELDS)
    assert isinstance(bindings, list) and len(bindings) == 2
    assert len({binding["id"] for binding in bindings}) == 2
    assert cleanup["unlinked_binding_ids"] == {binding["id"] for binding in bindings}
    assert cleanup["remaining_owned_binding_ids"] == []
    assert cleanup["shared_connections_retained"] is True
    for provider, (kind, field) in FIELDS.items():
        oracle = native[provider]
        binding = next(binding for binding in bindings if binding["provider"] == provider)
        selection = binding["selection"]
        assert binding["project_id"] == fixture["project_id"] and binding["status"] == "healthy"
        assert binding["generation"] >= 1
        assert selection["kind"] == kind and selection["provider"] == provider
        assert selection["namespace"] == oracle["namespace"]
        assert selection["native_id"] == str(oracle["id"])
        assert browser["selected"][provider] == binding["id"]
        assert oracle["name"] and isinstance(oracle["name"], str)
        digest = hashlib.sha256("\x00".join((kind, oracle["namespace"], str(oracle["id"]), oracle["name"])).encode()).hexdigest()
        expected = {
            "tenant_id": fixture["tenant_id"], "project_id": fixture["project_id"],
            "source_connection_id": binding["connection_ref"]["id"], "record_type": kind,
            "record_id": str(oracle["id"]), "record_revision": "observation-sha256:" + digest,
        }
        path = "/configuration/basic/" + field
        assert profile["configuration"]["basic"][field] == [expected]
        proofs = [proof for proof in profile["provenance"] if proof.get("paths") == [path]]
        assert len(proofs) == 1
        proof = proofs[0]
        assert proof["state"] == "accepted" and proof["method"] == "connector"
        assert proof["source_refs"] == [expected]
        assert proof["binding_id"] == binding["id"] and proof["binding_generation"] == binding["generation"]
        assert proof["field_map_version"] == "devlake-profile-v1"
        assert proof["coverage"] == "complete" and proof["revision_basis"] == "observation_only"
        assert proof["observed_at"]


def run_profile_setup(fixture, ports):
    """Compare browser writes with independently read Profile and native sources; retire owned bindings even on failure."""
    required = ("verify_assets", "read_profile", "read_bindings", "read_native", "read_connections", "browser_setup", "unlink_owned")
    if any(not callable(ports.get(name)) for name in required):
        raise ValueError("SCN-01 needs served assets, browser, native oracle, API readback and owned cleanup ports")
    ports["verify_assets"]()
    before = ports["read_profile"]()
    assert before["product_id"] == fixture["product_id"] and before["project_id"] == fixture["project_id"]
    assert before["tenant_id"] == fixture["tenant_id"] and before["revision"] == fixture["revision"]
    baseline = {item["id"] for item in ports["read_bindings"]()}
    assert not baseline
    connections_before = ports["read_connections"]()
    native_before = ports["read_native"]()
    assert set(native_before) == set(FIELDS)
    browser = profile = bindings = cleanup = None
    try:
        browser = ports["browser_setup"](fixture)
        profile = ports["read_profile"]()
        bindings = ports["read_bindings"]()
    finally:
        owned = {item["id"] for item in ports["read_bindings"]()} - baseline
        cleanup = ports["unlink_owned"](owned)
        assert cleanup["unlinked_binding_ids"] == owned and cleanup["remaining_owned_binding_ids"] == []
        assert all(item["status"] == "unlinked" for item in ports["read_bindings"]() if item["id"] in owned)
        assert ports["read_native"]() == native_before
        assert ports["read_connections"]() == connections_before
    assert_profile_setup(fixture, browser, native_before, bindings, profile, cleanup)
    return {"profile_revision": profile["revision"], "session_id": browser["session_id"], "cleanup": cleanup}

"""SCN-04 optional connector contract; live ports require a compatible served target."""

from .devlake_profile_setup import assert_profile_setup


def run_optional_connectors(fixture, ports):
    required = ("verify_assets", "read_profile", "read_native", "read_bindings", "read_connections",
                "read_collection", "select_config", "read_features", "read_clients", "edit_profile",
                "discover", "unlink_owned")
    if any(not callable(ports.get(name)) for name in required):
        raise ValueError("SCN-04 needs served UI/CLI, feature, native, collection and cleanup ports")
    ports["verify_assets"]()
    before = ports["read_profile"]()
    native_before = ports["read_native"]()
    connections_before = ports["read_connections"]()
    baseline = {item["id"] for item in ports["read_bindings"]()}
    assert before["tenant_id"] == fixture["tenant_id"]
    assert (before["product_id"], before["project_id"]) == (fixture["product_id"], fixture["project_id"])
    assert before["revision"] == fixture["revision"]
    assert before["product"]["purpose"] == fixture["confirmed_purpose"]
    assert before["configuration"]["basic"]["owners"] == fixture["confirmed_owners"]
    assert ports["read_collection"]() == []
    observations = {}
    cleanup = None
    try:
        for mode in ("missing", "off"):
            ports["select_config"](mode)
            features = ports["read_features"]()
            assert features.get("devlake_connectors") is (None if mode == "missing" else False)
            clients = ports["read_clients"]()
            for surface in ("browser", "installed_cli"):
                assert clients[surface]["connector_available"] is False
                assert clients[surface]["native_available"] is True
                assert clients[surface]["profile_edit_available"] is True
            assert ports["read_profile"]() == before
            assert ports["read_native"]() == native_before
            observations[mode] = clients
        edited = ports["edit_profile"](before["revision"])
        assert edited["revision"] == before["revision"] + 1
        assert edited["product"]["purpose"] == fixture["confirmed_purpose"]
        assert edited["product"]["name"] == fixture["new_name"]
        assert ports["read_profile"]() == edited
        ports["select_config"]("enabled")
        assert ports["read_features"]()["devlake_connectors"] is True
        clients = ports["read_clients"]()
        for surface in ("browser", "installed_cli"):
            assert clients[surface]["connector_available"] is True
            assert clients[surface]["native_available"] is True
            assert clients[surface]["profile_edit_available"] is True
        assert clients["capability_ready"] is True and clients["permission_granted"] is True
        browser = ports["discover"](fixture)
        profile = ports["read_profile"]()
        native = ports["read_native"]()
        bindings = ports["read_bindings"]()
        assert profile["product"] == edited["product"]
        assert profile["configuration"]["basic"]["owners"] == fixture["confirmed_owners"]
        assert profile["revision"] > edited["revision"]
        assert ports["read_collection"]() == []
        observations["enabled"] = clients
    finally:
        owned = {item["id"] for item in ports["read_bindings"]()} - baseline
        cleanup = ports["unlink_owned"](owned)
        assert cleanup["unlinked_binding_ids"] == owned
        assert cleanup["remaining_owned_binding_ids"] == []
        assert cleanup["shared_connections_retained"] is True
        assert ports["read_native"]() == native_before
        assert ports["read_connections"]() == connections_before
        assert ports["read_collection"]() == []
    assert_profile_setup(fixture, browser, native, bindings, profile, cleanup)
    assert {item["id"] for item in bindings} - baseline == cleanup["unlinked_binding_ids"]
    return {"profile_revision": profile["revision"], "cleanup": cleanup, "clients": observations}

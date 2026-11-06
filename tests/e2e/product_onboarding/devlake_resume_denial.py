"""SCN-03 scoped discovery recovery and denial oracle."""

import re

SURFACES = ("api", "browser", "installed_cli")


def scoped_views(ports, fixture, expected_coverage):
    views = {surface: ports["read_session"](surface) for surface in SURFACES}
    for surface, view in views.items():
        assert (view["tenant_id"], view["product_id"], view["project_id"], view["id"]) == (
            fixture["tenant_id"], fixture["product_id"], fixture["project_id"], fixture["session_id"])
        discovery = next(item for item in view["discoveries"] if item["id"] == fixture["operation_id"])
        if expected_coverage == "partial":
            assert discovery["coverage"] == "partial" and discovery["state"] == "succeeded"
            assert discovery["complete"] is False and discovery["resource_count"] == 1
            assert discovery["cursor"] == fixture["operation_id"]
            assert discovery["binding_ids"] == [fixture["binding_id"]]
        else:
            assert discovery["coverage"] == "denied" and discovery["state"] == "cancelled"
            assert discovery["complete"] is False and discovery["resource_count"] == 0
            assert not {"cursor", "binding_ids"} & discovery.keys() and not discovery.get("resources")
            assert all(item["coverage"] == "denied" and item["state"] == "cancelled"
                       and item["complete"] is False and item["resource_count"] == 0
                       and not {"cursor", "binding_ids"} & item.keys() and not item.get("resources")
                       for item in view["discoveries"])
            if surface != "api":
                assert view["retry_available"] is False
    return views


def run_resume_denial(fixture, ports):
    required = ("verify_assets", "read_session", "read_operation", "read_bindings", "read_source",
                "read_connections", "retry", "revoke", "unlink", "save_profile", "read_profile", "cleanup_owned")
    if any(not callable(ports.get(name)) for name in required):
        raise ValueError("SCN-03 needs independent API/UI/installed-CLI reads, run-owned mutations and cleanup ports")
    assert all(isinstance(fixture.get(name), str) and fixture[name] for name in (
        "tenant_id", "product_id", "project_id", "session_id", "binding_id", "shared_binding_id", "operation_id"))
    assert fixture["binding_id"] != fixture["shared_binding_id"]
    assert fixture["generation"] >= 1
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", fixture[name]) for name in ("retry_key", "unlink_key"))
    assert fixture["retry_key"] != fixture["unlink_key"]
    ports["verify_assets"]()
    native = ports["read_source"]()
    connections = ports["read_connections"]()
    before = ports["read_bindings"]()
    assert {item["id"] for item in before} == {fixture["binding_id"], fixture["shared_binding_id"]}
    assert all(item["status"] == "healthy" for item in before)
    assert next(item for item in before if item["id"] == fixture["binding_id"])["generation"] == fixture["generation"]
    observations = {}
    try:
        observations["partial"] = scoped_views(ports, fixture, "partial")
        operation = ports["read_operation"](fixture["operation_id"])
        assert operation["coverage"] == "partial" and operation["cursor"] == fixture["operation_id"]
        assert operation["state"] == "succeeded" and len(operation["resources"]) == 1
        try:
            ports["retry"](fixture["operation_id"], fixture["retry_key"], fixture["revision"])
        except ConnectionError:
            pass
        retried = ports["retry"](fixture["operation_id"], fixture["retry_key"], fixture["revision"])
        observations["retry"] = retried
        assert retried["coverage"] == "complete" and retried["state"] == "succeeded"
        assert retried["id"] != fixture["operation_id"]
        operations = ports["read_session"]("api")["discoveries"]
        assert len(operations) == 2 and {item["id"] for item in operations} == {fixture["operation_id"], retried["id"]}
        assert ports["read_bindings"]() == before
        ports["revoke"]()
        observations["denied"] = scoped_views(ports, fixture, "denied")
        denied = ports["read_operation"](fixture["operation_id"])
        assert denied["coverage"] == "denied" and denied["state"] == "cancelled"
        assert not denied.get("resources") and "cursor" not in denied
        assert ports["read_source"]() == native
        try:
            ports["unlink"](fixture["binding_id"], fixture["unlink_key"], fixture["revision"])
        except ConnectionError:
            pass
        unlinked = ports["unlink"](fixture["binding_id"], fixture["unlink_key"], fixture["revision"])
        observations["unlink"] = unlinked
        assert unlinked["status"] == "unlinked" and unlinked["id"] == fixture["binding_id"]
        assert unlinked["generation"] == fixture["generation"] + 1
        remaining = {item["id"]: item for item in ports["read_bindings"]()}
        assert set(remaining) == {fixture["binding_id"], fixture["shared_binding_id"]}
        assert remaining[fixture["binding_id"]]["status"] == "unlinked"
        assert remaining[fixture["shared_binding_id"]] == next(
            item for item in before if item["id"] == fixture["shared_binding_id"])
        assert ports["read_connections"]() == connections
        saved = ports["save_profile"](fixture["revision"] + 1)
        assert saved["revision"] == fixture["revision"] + 2
        assert ports["read_profile"]()["revision"] == saved["revision"]
        observations["saved"] = saved
    finally:
        cleanup = ports["cleanup_owned"]({fixture["binding_id"]})
        assert cleanup["unlinked_binding_ids"] == {fixture["binding_id"]}
        assert cleanup["remaining_owned_binding_ids"] == [] and cleanup["shared_connections_retained"] is True
        assert ports["read_connections"]() == connections and ports["read_source"]() == native
        observations["cleanup"] = cleanup
    return observations

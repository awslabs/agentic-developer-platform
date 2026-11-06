"""Independent scope and ownership assertions for live fault cleanup."""


def verify_scope_denials(probes):
    """Require each foreign Product, tenant and revoked-access probe to deny."""
    required = {"foreign_product", "foreign_tenant", "revoked_access"}
    if {probe["case"] for probe in probes} != required or len(probes) != len(required):
        raise AssertionError("missing independent isolation counterexample")
    for probe in probes:
        if probe["status"] not in {403, 404} or probe["disclosed_resources"]:
            raise AssertionError(f"{probe['case']} disclosed private evidence")
    return {"denied_cases": sorted(required)}


def verify_owned_cleanup(owned, linked, readbacks):
    """Only owned fixtures may be absent; linked resources must remain."""
    if not owned or set(owned) & set(linked) or set(readbacks) != set(owned) | set(linked):
        raise AssertionError("owned fixture inventory or cleanup readback incomplete")
    if any(readbacks[resource] != "absent" for resource in owned):
        raise AssertionError("owned fixture still exists in native readback")
    if any(readbacks[resource] != "retained" for resource in linked):
        raise AssertionError("linked resource was removed during cleanup")
    return {"owned_absent": len(owned), "linked_retained": len(linked)}

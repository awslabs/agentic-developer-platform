"""Check bounded recovery without mistaking local unlink for provider deletion."""


def assert_resumed_discovery(first, resumed, repeated):
    assert first["coverage"] == "partial" and first["resource_cursor"] == "2"
    assert resumed["coverage"] == "complete" and "resource_cursor" not in resumed
    assert repeated["coverage"] == resumed["coverage"] and repeated.get("resource_cursor") == resumed.get("resource_cursor")
    assert [(item["namespace"], item["native_id"], item["source_revision"]) for item in repeated["resources"]] == [
        (item["namespace"], item["native_id"], item["source_revision"]) for item in resumed["resources"]
    ]
    first_ids = {(item["namespace"], item["native_id"]) for item in first["resources"]}
    resumed_ids = {(item["namespace"], item["native_id"]) for item in resumed["resources"]}
    assert len(first_ids) == len(first["resources"]) == 1
    assert len(resumed_ids) == len(resumed["resources"]) == 1
    assert first_ids.isdisjoint(resumed_ids)


def assert_owned_unlink(before, after, owned_id, preserved_id, provider_before, provider_after, calls):
    assert before["product"] == after["product"]
    assert before["project"] == after["project"]
    assert provider_after == provider_before
    assert calls and all(call[0].startswith("GET") for call in calls)
    previous_bindings = {binding["id"]: binding for binding in before["bindings"]}
    bindings = {binding["id"]: binding for binding in after["bindings"]}
    assert bindings[owned_id]["status"] == "unlinked"
    assert previous_bindings[preserved_id]["status"] == "healthy"
    assert bindings[preserved_id] == previous_bindings[preserved_id]

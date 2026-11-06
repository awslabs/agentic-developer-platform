"""Installed setup uses Product routes, waits for workers, and retains scope."""

import json
from copy import deepcopy

import httpx
import pytest

from installed_setup import prepare_binding, validate_setup
from journey_driver import FirstPartyDriver
from test_selector import selector_case as selector_case

__all__ = ["selector_case"]


@pytest.mark.parametrize("action", ["create", "link", "check"])
@pytest.mark.parametrize("boundary", [None, "paused", "foreign_binding", "wrong_configuration", "unlinked", "changed_handoff"])
def test_first_install_uses_bounded_ordinary_product_setup(selector_case, action, boundary):
    manifest, _, directory, _ = selector_case
    manifest = deepcopy(manifest)
    driver = manifest["journey_driver"]
    scope = {"tenant_id": "example-tenant", "product_id": driver["scope"]["product_id"], "project_id": driver["scope"]["project_id"]}
    pending = "selected-binding" if action == "check" else "pending-setup"
    driver["scope"]["binding_id"] = pending
    for body in driver["inputs"].values():
        body["binding_id"] = pending
    provider = driver["provider_readback"]
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": provider["source_connection_id"], "record_type": "agent_space",
                 "record_id": "native-space", "record_revision": "1"}
    request = {"action": action, "configuration_revision": 1}
    if action == "link":
        request["native_resource_ref"] = reference
    elif action == "check":
        request["binding_id"] = pending
    manifest["installed_setup"] = {"tenant_id": scope["tenant_id"], "request": request,
                                   "receipt": "example-setup-receipt-00001", "max_requests": 20, "deadline_seconds": 30,
                                   **({"if_match": '"1"'} if action == "check" else {})}
    configuration = {"configuration_id": "selected-configuration", "revision": 1, "scope": scope,
                     "hash": "a" * 64, "handoff_state": "linked", "settings": {
                         "enabled": True, "devops": {"trigger_policies": [{"binding_id": pending, "capability": "investigation"}]}}}
    binding = {"binding_id": "selected-binding", "configuration_id": configuration["configuration_id"],
               "scope": scope, "state": "ready", "gaps": [], "native_resource_ref": reference,
               "provider_identity": {field: provider[field] for field in ("account_id", "role_arn", "region")}, "revision": 1}
    calls, operations, polls = [], {}, {}

    def respond(http_request):
        calls.append((http_request.method, http_request.url.path))
        path = http_request.url.path
        if http_request.method == "GET" and path.endswith("/configuration"):
            return httpx.Response(200, json={**configuration, **({"revision": 2} if boundary == "wrong_configuration" else {}),
                                            **({"handoff_state": "pending"} if boundary == "unlinked" else {})})
        if http_request.method == "POST" and path.endswith("/binding-operations"):
            body = json.loads(http_request.content)
            if body["action"] == "check":
                assert http_request.headers["if-match"] == '"1"'
            operation_id = "operation-" + str(len(operations))
            operations[operation_id] = {"operation_id": operation_id, "action": body["action"], "state": "accepted"}
            polls[operation_id] = 0
            return httpx.Response(202, json=operations[operation_id])
        if http_request.method == "GET" and "/binding-operations/" in path:
            operation_id = path.rsplit("/", 1)[1]
            polls[operation_id] += 1
            if polls[operation_id] == 1 or boundary == "paused":
                return httpx.Response(200, json=operations[operation_id])
            return httpx.Response(200, json={**operations[operation_id], "state": "succeeded", "binding_id": "selected-binding"})
        if http_request.method == "GET" and "/bindings/" in path:
            return httpx.Response(200, json={**binding, **({"scope": {**scope, "product_id": "foreign"}} if boundary == "foreign_binding" else {})})
        if http_request.method == "PUT" and path.endswith("/configuration"):
            assert http_request.headers["if-match"] == '"' + configuration["hash"] + '"'
            settings = json.loads(http_request.content)
            assert settings["devops"]["trigger_policies"][0]["binding_id"] == "selected-binding"
            return httpx.Response(200, json={**configuration, "revision": 2, "settings": settings,
                                            "handoff_state": "pending" if boundary == "changed_handoff" else "linked"})
        raise AssertionError("unexpected Product route")

    def client(*args, **kwargs):
        selected = FirstPartyDriver(*args, **kwargs)
        selected.client.close()
        selected.client = httpx.Client(transport=httpx.MockTransport(respond))
        return selected

    assert validate_setup(manifest)["request"] == request
    assert not calls
    if boundary and not (boundary == "changed_handoff" and action == "check"):
        with pytest.raises(ValueError):
            prepare_binding(manifest, directory, driver_factory=client, sleep=lambda _: None)
        assert sum(method == "POST" for method, _ in calls) <= 1
    else:
        hydrated = prepare_binding(manifest, directory, driver_factory=client, sleep=lambda _: None)
        assert "installed_setup" not in hydrated
        assert hydrated["journey_driver"]["scope"]["binding_id"] == "selected-binding"
        assert {body["configuration_revision"] for body in hydrated["journey_driver"]["inputs"].values()} == {1 if action == "check" else 2}
        assert sum(method == "POST" for method, _ in calls) == (1 if action == "check" else 2)
        assert manifest["journey_driver"]["scope"]["binding_id"] == pending
        entries = [json.loads(line) for line in next(directory.glob("*-journey-actions.jsonl")).read_text().splitlines()]
        acknowledged = [entry for entry in entries if entry["entry_point"] == "setup" and entry["status"] == "acknowledged"]
        assert {entry["operation_id"] for entry in acknowledged} == set(operations)
        assert all(entry["run_id"] is None for entry in acknowledged)

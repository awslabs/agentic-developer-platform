"""Offline fixtures for independent Security setup readback."""

from copy import deepcopy

import httpx
import pytest

from clients import api_reader, verify_setup


@pytest.fixture
def setup_records():
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example"}
    selection = {"scope": scope, "configuration_id": "config-example", "binding_id": "binding-example",
                 "run_id": "run-example", "connection_ref": {"kind": "credential", "id": "account-example"},
                 "binding_state": "active", "readiness": [],
                 "provider_identity": {"account_id": "000000000000", "region": "region-example"},
                 "sources": [{"record_id": "source-example", "record_revision": "revision-example"}]}
    selection["max_attempts"] = 3
    selection["effects"] = {"create": "config-example", "edit": "config-example", "submit": "run-example"}
    configuration = {"scope": scope, "configuration_id": selection["configuration_id"], "revision": 2, "settings": {"visibility": "private"}}
    binding = {"scope": scope, "configuration_id": selection["configuration_id"],
               "binding_id": selection["binding_id"], "revision": 3, "state": "active",
               "readiness": [], "connection_ref": selection["connection_ref"],
               "provider_identity": selection["provider_identity"]}
    run = {"scope": scope, "run_id": selection["run_id"], "revision": 4,
           "input": {"binding_id": selection["binding_id"], "sources": selection["sources"]},
           "admission": {"configuration_id": selection["configuration_id"], "configuration_revision": 2}}
    records = {"configuration": configuration, "binding": binding, "run": run}
    return selection, records, {surface: deepcopy(records) for surface in ("cli", "ui", "mcp")}


def verify(selection, records, clients):
    paths = []
    attempts = [{"surface": "cli", "operation": "create", "effect_id": "config-example"},
                {"surface": "ui", "operation": "edit", "effect_id": "config-example"},
                {"surface": "mcp", "operation": "submit", "effect_id": "run-example"}]

    def read_api(path):
        paths.append(path)
        return records[next(name for name, suffix in (
            ("configuration", "/configuration"), ("binding", "/bindings/binding-example"),
            ("run", "/runs/run-example")) if path.endswith(suffix))]

    return verify_setup(selection, clients, attempts, read_api), paths


def test_readback_uses_scoped_paths_and_pinned_selection(setup_records):
    selection, records, clients = setup_records
    result, paths = verify(selection, records, clients)
    assert result == {"configuration_revision": 2, "binding_revision": 3,
                      "run_revision": 4, "run_id": "run-example", "attempt_count": 3}
    assert all("/products/product-example/projects/project-example/services/security/" in path for path in paths)
    assert len(paths) == 3


@pytest.mark.parametrize("surface,name,field,value", [
    ("cli", "configuration", "revision", 1),
    ("ui", "configuration", "scope", {"tenant_id": "other"}),
    ("mcp", "binding", "binding_id", "other"),
    ("cli", "run", "run_id", "other"),
    ("ui", "binding", "readiness", ["gap"]),
    ("mcp", "run", "input", {"binding_id": "binding-example", "sources": []}),
])
def test_client_mismatch_rejected(setup_records, surface, name, field, value):
    selection, records, clients = setup_records
    clients[surface][name][field] = value
    with pytest.raises(AssertionError, match=f"{surface} {name}"):
        verify(selection, records, clients)


@pytest.mark.parametrize("name,field,value", [
    ("configuration", "scope", {"tenant_id": "other"}),
    ("binding", "connection_ref", {"kind": "credential", "id": "other"}),
    ("binding", "readiness", ["gap"]),
    ("binding", "provider_identity", {"account_id": "different"}),
    ("run", "input", {"binding_id": "binding-example", "sources": []}),
    ("run", "admission", {"configuration_id": "config-example", "configuration_revision": 1}),
])
def test_independent_api_mismatch_rejected(setup_records, name, field, value):
    selection, records, clients = setup_records
    records[name][field] = value
    with pytest.raises(AssertionError):
        verify(selection, records, clients)


def test_attempt_limit_rejects_unbounded_operation(setup_records):
    selection, records, clients = setup_records
    selection["max_attempts"] = 2
    with pytest.raises(AssertionError, match="bound"):
        verify(selection, records, clients)


def test_operation_rejects_duplicate_effects(setup_records):
    selection, records, clients = setup_records
    attempts = [{"surface": "cli", "operation": "create", "effect_id": "config-example"},
                {"surface": "ui", "operation": "edit", "effect_id": "other-config"},
                {"surface": "mcp", "operation": "submit", "effect_id": "run-example"}]
    with pytest.raises(AssertionError, match="multiple effects"):
        verify_setup(selection, clients, attempts, lambda path: records["run"])


def test_unselected_operation_rejected(setup_records):
    selection, records, clients = setup_records
    attempts = [{"surface": "cli", "operation": "create", "effect_id": "config-example"},
                {"surface": "ui", "operation": "edit", "effect_id": "config-example"},
                {"surface": "mcp", "operation": "unknown", "effect_id": "run-example"}]
    with pytest.raises(AssertionError, match="submit missing"):
        verify_setup(selection, clients, attempts, lambda path: records["run"])


def test_separate_http_readback_uses_scoped_api_and_independent_authority(setup_records):
    selection, records, clients = setup_records
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-only"
        assert request.headers["Cache-Control"] == "no-cache"
        for name, suffix in (("configuration", "/configuration"),
                             ("binding", "/bindings/binding-example"), ("run", "/runs/run-example")):
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=records[name])
        return httpx.Response(404)

    with httpx.Client(base_url="https://security.example", transport=httpx.MockTransport(respond),
                      headers={"Authorization": "Bearer test-only"}) as independent:
        result = verify_setup(selection, clients, [
            {"surface": "cli", "operation": "create", "effect_id": "config-example"},
            {"surface": "ui", "operation": "edit", "effect_id": "config-example"},
            {"surface": "mcp", "operation": "submit", "effect_id": "run-example"},
        ], api_reader(independent))
    assert result["attempt_count"] == 3
    assert len(requests) == 3
    assert all("/products/product-example/projects/project-example/services/security/" in request.url.path
               for request in requests)

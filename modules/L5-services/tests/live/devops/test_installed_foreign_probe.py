"""Exercise installed foreign probes with only Product HTTP boundaries mocked."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import Client

from context_selector import authorize_channels
from installed_foreign_probe import validate_installed_foreign_probe
import test_installed_operations as operation_fixtures

operations = operation_fixtures.operations
installed_access = operation_fixtures.installed_access
portable_selection = operation_fixtures.portable_selection
selector_case = operation_fixtures.selector_case


@pytest.fixture
def probe(operations):
    case = operations
    case.manifest["portability"]["installed_foreign_probe"] = True
    case.manifest["portability"]["action_channels"].pop("probe_foreign")
    case.requests = []
    case.respond = None

    def respond(request):
        assert request.method == "GET" and request.url.host == "gateway.example.org"
        selected = next(entry for entry in case.entries if request.url.path == (
            f"/api/v1/products/{entry['scope']['product_id']}/projects/{entry['scope']['project_id']}"
            f"/services/devops/bindings/{entry['binding_id']}"))
        index = case.entries.index(selected)
        case.requests.append((selected["binding_id"], request.headers["Authorization"]))
        if case.respond:
            response = case.respond(request, len(case.requests))
            if response is not None:
                return response
        allowed = {f"Bearer private-operation-submitter-{index + 1}",
                   f"Bearer independent-portable-reader-{index + 1}"}
        if request.headers["Authorization"] not in allowed:
            return httpx.Response(404, json={"detail": {"code": "not_found"}})
        return httpx.Response(200, json=deepcopy(case.bindings[selected["binding_id"]]))

    case.target.client.close()
    case.target.client = Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    return case


def execute(case, index=0):
    return case.target.probe_foreign(case.entries[index]["scope"], case.entries[1 - index]["scope"])


def test_both_products_probe_installed_bindings_without_custom_actions(probe):
    case = probe
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")
    for index in range(2):
        observed = execute(case, index)
        assert observed["status"] == 404 and observed["disclosed_resources"] == []
        assert observed["readback"]["scope"] == case.entries[index]["scope"]
        assert observed["readback"]["foreign_scope"] == case.entries[1 - index]["scope"]
        assert "provider_session_calls" not in observed and "native_session" not in observed
        assert observed["readback"]["own_binding"] == case.bindings[f"binding-{index + 1}"]
        assert "private-operation-submitter" not in json.dumps(observed)
    assert case.target.calls == 10 and case.calls == [] and case.posts == []
    assert case.requests[:5] == [
        ("binding-1", "Bearer private-operation-submitter-1"),
        ("binding-2", "Bearer independent-portable-reader-2"),
        ("binding-2", "Bearer private-operation-submitter-1"),
        ("binding-1", "Bearer private-operation-submitter-1"),
        ("binding-2", "Bearer independent-portable-reader-2"),
    ]
    assert operation_fixtures.journal(case) == []


@pytest.mark.parametrize("status", [200, 202, 302, 401, 403, 429, 503])
def test_unexpected_foreign_access_or_errors_are_not_denial_evidence(probe, status):
    probe.respond = lambda request, count: httpx.Response(
        status, json={"detail": {"code": "not_found"}}, headers={"Location": "https://foreign.example.org"}
    ) if count == 3 else None
    with pytest.raises(ValueError, match="expected Product access"):
        execute(probe)
    assert len(probe.requests) == 3


@pytest.mark.parametrize("body", [
    {"detail": {"code": "session_expired"}}, {"detail": {"code": "not_found", "binding_id": "private"}},
    {"detail": {"code": "not_found"}, "native_session": "private"}, [], None,
])
def test_denial_must_be_the_nondisclosing_product_response(probe, body):
    probe.respond = lambda request, count: httpx.Response(404, content=json.dumps(body)) if count == 3 else None
    with pytest.raises(ValueError, match="disclosed data"):
        execute(probe)


@pytest.mark.parametrize("step", [1, 2, 4, 5])
@pytest.mark.parametrize("status", [302, 401, 404, 503])
def test_both_bindings_must_remain_authorized_for_their_own_actors(probe, step, status):
    probe.respond = lambda request, count: httpx.Response(status) if count == step else None
    with pytest.raises(ValueError, match="expected Product access"):
        execute(probe)
    assert len(probe.requests) == step


@pytest.mark.parametrize("mutate", [
    lambda binding: binding.update(revision=4),
    lambda binding: binding.update(state="revoked"),
    lambda binding: binding.update(gaps=["revoked"]),
    lambda binding: binding["scope"].update(tenant_id="foreign"),
    lambda binding: binding["scope"].update(product_id="foreign"),
    lambda binding: binding["scope"].update(project_id="foreign"),
    lambda binding: binding["provider_identity"].update(role_arn="foreign"),
    lambda binding: binding["native_resource_ref"].update(record_id="foreign"),
    lambda binding: binding["native_resource_ref"].update(source_connection_id="foreign"),
])
def test_stale_or_foreign_binding_stops_before_the_probe(probe, mutate):
    mutate(probe.bindings["binding-1"])
    with pytest.raises(ValueError, match="foreign, stale or not ready"):
        execute(probe)
    assert len(probe.requests) == 1


@pytest.mark.parametrize("binding_id", ["binding-1", "binding-2"])
def test_changes_during_denial_are_not_accepted(probe, binding_id):
    def respond(request, count):
        if count == 3:
            probe.bindings[binding_id]["generation"] += 1
    probe.respond = respond
    with pytest.raises(ValueError, match="authority changed"):
        execute(probe)


@pytest.mark.parametrize("phase", [1, 3, 5])
@pytest.mark.parametrize("fault", ["oversized", "malformed", "timeout"])
def test_read_failure_does_not_become_a_passing_denial(probe, phase, fault):
    def respond(request, count):
        if count != phase:
            return None
        if fault == "timeout":
            raise httpx.ReadTimeout("mock read timeout")
        return httpx.Response(404 if count == 3 else 200,
                              content=b"x" * 65537 if fault == "oversized" else b"{")
    probe.respond = respond
    with pytest.raises((ValueError, httpx.ReadTimeout)):
        execute(probe)
    assert len(probe.requests) == phase


@pytest.mark.parametrize("fault", ["expired", "deadline", "budget", "cleanup", "foreign", "same"])
def test_selection_and_bounds_deny_before_http(probe, fault):
    own, foreign = [entry["scope"] for entry in probe.entries]
    if fault == "expired":
        probe.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif fault == "deadline":
        probe.target.deadline = time.monotonic() - 1
    elif fault == "budget":
        probe.target.calls = probe.target.max_requests - 8
    elif fault == "cleanup":
        probe.target.cleanup_phase = True
    elif fault == "foreign":
        foreign = {**foreign, "product_id": "unselected"}
    else:
        foreign = own
    with pytest.raises(ValueError):
        probe.target.probe_foreign(own, foreign)
    assert probe.requests == []


def test_exact_request_limit_preserves_cleanup_capacity(probe):
    probe.target.calls = probe.target.max_requests - 9
    assert execute(probe)["status"] == 404
    assert probe.target.calls == probe.target.max_requests - 4


def test_authorization_expiring_during_readback_is_not_accepted(probe, monkeypatch):
    clock = time.monotonic()
    monkeypatch.setattr("installed_foreign_probe.time.monotonic", lambda: clock)
    def respond(request, count):
        nonlocal clock
        if count == 3:
            clock += 3600
    probe.respond = respond
    with pytest.raises(ValueError, match="expired during readback"):
        execute(probe)
    assert len(probe.requests) == 3


@pytest.mark.parametrize("change", ["disabled", "no_operations", "no_access", "shared_token"])
def test_probe_requires_installed_scope_and_separate_read_credentials(probe, monkeypatch, change):
    selected = probe.manifest["portability"]
    if change == "disabled":
        selected["installed_foreign_probe"] = False
    elif change == "no_operations":
        selected.pop("installed_operations")
    elif change == "no_access":
        selected.pop("installed_access")
    else:
        monkeypatch.setenv(probe.operations[0]["submit_token_env"], "independent-portable-reader-2")
    with pytest.raises(ValueError):
        validate_installed_foreign_probe(probe.manifest)


def test_independent_provider_session_readback_is_still_required(probe):
    probe.manifest["portability"]["readback_channels"].pop("foreign_denial")
    with pytest.raises(ValueError, match="independent readbacks"):
        authorize_channels(probe.manifest, probe.manifest["release"]["source_sha"], probe.directory, "portability")


async def test_probe_accepts_actual_sql_binding_serialization(probe, tmp_path):
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.api.bindings import BindingStore, view_binding
    from l5_services.models.base import Base
    from l5_services.models.binding import Binding

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bindings.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for entry in probe.entries:
                scope = entry["scope"]
                session.add(Binding(tenant_id=scope["tenant_id"], binding_id=entry["binding_id"],
                    product_id=scope["product_id"], project_id=scope["project_id"], service_id="devops",
                    configuration_id="configuration-" + entry["binding_id"], revision=entry["binding_revision"],
                    generation=2, state="ready", ownership="adopted", scope=deepcopy(scope),
                    connection_ref={"record_id": entry["provider"]["source_connection_id"]},
                    connection_key=entry["provider"]["source_connection_id"], native_key=entry["agent_space_id"],
                    native_resource_ref=deepcopy(probe.bindings[entry["binding_id"]]["native_resource_ref"]),
                    provider_identity=deepcopy(probe.bindings[entry["binding_id"]]["provider_identity"]),
                    readiness=[], gaps=[]))
            await session.commit()
            for entry in probe.entries:
                scope = entry["scope"]
                store = BindingStore(session, scope["tenant_id"], scope["product_id"], scope["project_id"], "devops")
                probe.bindings[entry["binding_id"]] = jsonable_encoder(view_binding(await store.binding(entry["binding_id"])))
        assert execute(probe)["status"] == 404
    finally:
        await engine.dispose()

"""Verify installed configuration joins against Product and binding API shapes."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import Client

from context_selector import authorize_channels
from installed_configuration import validate_installed_configuration
import test_installed_operations as operation_fixtures

operations = operation_fixtures.operations
installed_access = operation_fixtures.installed_access
portable_selection = operation_fixtures.portable_selection
selector_case = operation_fixtures.selector_case


@pytest.fixture
def configured(operations):
    case = operations
    selection = case.manifest["portability"]
    selection["installed_configuration"] = True
    selection["action_channels"].pop("configure")
    selection["readback_channels"].pop("configuration")
    case.profiles, case.requests = {}, []
    case.respond = None
    case.check = {"ready": True, "gaps": [], "provider_contacted": True,
                  "integrations": [{"status": "ready"}]}
    for project, entry in zip(case.manifest["projects"], case.entries, strict=True):
        scope = entry["scope"]
        def reference(kind, identifier):
            return {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                    "source_connection_id": entry["provider"]["source_connection_id"],
                    "record_type": kind, "record_id": identifier, "record_revision": "1"}
        connection = reference("connection", entry["provider"]["source_connection_id"])
        repository = reference("repository", project["repository_ref"])
        environment = reference("environment", project["environment_ref"])
        case.bindings[entry["binding_id"]]["connection_ref"] = connection
        configuration = case.configurations[scope["product_id"]]
        configuration["hash"] = "configuration-hash"
        configuration["settings"]["visibility"] = configuration["scope"]["visibility"]
        configuration["settings"]["service_id"] = "devops"
        configuration["settings"].update(source_refs=[environment], devops={
            "hosting_connection_ref": connection, "region": entry["provider"]["region"],
            "access_policy_ref": reference("policy", project["policy_ref"]),
            "integrations": [{"kind": "github", "connection_ref": connection, "repository_refs": [repository]}],
            "accounts": [{"connection_ref": connection, "environments": [{
                "environment_ref": environment, "workload_kind": project["workload_kind"]}]}]})
        case.profiles[scope["product_id"]] = {
            **scope, "revision": 7, "product": {"team_id": project["team_ref"]},
            "project": {"id": scope["project_id"], "tenant_id": scope["tenant_id"], "state": "registered"},
            "configuration": {"selected_capabilities": [{"capability": "devops", "ownership_mode": "integrated"}],
                              "basic": {"repositories": [repository]}, "advanced": {"environments": [environment]}}}

    def respond(request):
        assert request.url.host == "gateway.example.org"
        entry = next(entry for entry in case.entries if f"/products/{entry['scope']['product_id']}/" in request.url.path)
        index = case.entries.index(entry)
        prefix = f"/api/v1/products/{entry['scope']['product_id']}/projects/{entry['scope']['project_id']}"
        suffix = request.url.path.removeprefix(prefix)
        assert request.headers["Authorization"] == "Bearer " + (
            f"private-operation-submitter-{index + 1}" if request.method == "POST"
            else f"independent-portable-reader-{index + 1}")
        case.requests.append((request.method, suffix))
        if case.respond:
            result = case.respond(request, len(case.requests))
            if result is not None:
                return result
        if request.method == "POST":
            assert suffix == "/services/devops/configuration/check"
            assert request.headers["Idempotency-Key"] == case.manifest["projects"][index]["configuration_receipt"]
            return httpx.Response(200, json=deepcopy(case.check))
        assert request.method == "GET"
        if suffix == "":
            return httpx.Response(200, json=deepcopy(case.profiles[entry["scope"]["product_id"]]))
        if suffix == "/services/devops/configuration":
            return httpx.Response(200, json=deepcopy(case.configurations[entry["scope"]["product_id"]]))
        assert suffix == "/services/devops/bindings/" + entry["binding_id"]
        return httpx.Response(200, json=deepcopy(case.bindings[entry["binding_id"]]))

    case.target.client.close()
    case.target.client = Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    return case


def read(case, index=0):
    return case.target.read_configuration(case.entries[index]["scope"])


def confirm(case, index=0):
    return case.target.configure(case.manifest["projects"][index])


def test_configuration_joins_two_products_without_custom_channels(configured):
    case = configured
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")
    for index, project in enumerate(case.manifest["projects"]):
        observed = read(case, index)
        for field in ("scope", "repository_ref", "team_ref", "policy_ref", "environment_ref",
                      "workload_kind", "account_ref", "role_ref"):
            assert observed[field] == project[field]
        assert observed["readback"]["configuration"] == case.configurations[project["scope"]["product_id"]]
        assert "private-" not in json.dumps(observed)
    assert case.target.calls == 10 and case.calls == [] and case.posts == []


def test_prepared_configuration_check_uses_submit_session_and_independent_readback(configured):
    case = configured
    original = deepcopy(case.configurations)
    for index in range(2):
        case.target.cleanup_phase = True
        assert confirm(case, index) is None
        assert case.target.cleanup_phase is False
    assert case.target.calls == 22 and case.configurations == original
    assert [row for row in case.requests if row[0] == "POST"] == [("POST", "/services/devops/configuration/check")] * 2
    assert [row["phase"] for row in operation_fixtures.journal(case)] == ["prepared", "acknowledged"] * 2
    assert case.calls == [] and case.posts == []


@pytest.mark.parametrize("record,mutation", [
    ("profile", lambda value: value.update(tenant_id="foreign")),
    ("profile", lambda value: value["product"].update(team_id="other")),
    ("profile", lambda value: value.update(archived_at="2026-01-01T00:00:00Z")),
    ("profile", lambda value: value["configuration"]["basic"].update(repositories=[])),
    ("profile", lambda value: value["configuration"]["advanced"].update(environments=[])),
    ("profile", lambda value: value["configuration"].update(selected_capabilities=[])),
    ("profile", lambda value: value["configuration"]["selected_capabilities"][0].update(ownership_mode="not_applicable")),
    ("configuration", lambda value: value.update(revision=2)),
    ("configuration", lambda value: value.update(state="needs_input")),
    ("configuration", lambda value: value.update(handoff_state="pending")),
    ("configuration", lambda value: value["scope"].update(project_id="foreign")),
    ("configuration", lambda value: value["settings"].update(enabled=False)),
    ("configuration", lambda value: value["settings"].update(service_id="security")),
    ("configuration", lambda value: value["settings"].update(source_refs=[])),
    ("configuration", lambda value: value["settings"]["devops"]["access_policy_ref"].update(project_id="foreign")),
    ("configuration", lambda value: value["settings"]["devops"]["access_policy_ref"].update(record_id="other")),
    ("configuration", lambda value: value["settings"]["devops"]["integrations"][0]["repository_refs"][0].update(record_revision="latest")),
    ("configuration", lambda value: value["settings"]["devops"]["accounts"][0]["environments"][0].update(workload_kind="other")),
    ("binding", lambda value: value.update(revision=4)),
    ("binding", lambda value: value.update(configuration_id="foreign")),
    ("binding", lambda value: value["provider_identity"].update(role_arn="foreign")),
    ("binding", lambda value: value["native_resource_ref"].update(record_id="foreign")),
])
def test_scope_source_and_binding_mismatch_prevent_configuration_checks(configured, record, mutation):
    case = configured
    product_id = case.entries[0]["scope"]["product_id"]
    selected = {"profile": case.profiles[product_id], "configuration": case.configurations[product_id],
                "binding": case.bindings["binding-1"]}[record]
    mutation(selected)
    with pytest.raises(ValueError):
        confirm(case)
    assert all(method == "GET" for method, path in case.requests)
    assert operation_fixtures.journal(case) == []


@pytest.mark.parametrize("record", ["profile", "configuration"])
def test_changed_authority_during_join_is_rejected(configured, record):
    case = configured
    def respond(request, count):
        if count == 3:
            product_id = case.entries[0]["scope"]["product_id"]
            if record == "profile":
                case.profiles[product_id]["revision"] += 1
            else:
                case.configurations[product_id]["hash"] = "changed"
    case.respond = respond
    with pytest.raises(ValueError, match="authority changed"):
        read(case)


@pytest.mark.parametrize("change", ["not_ready", "missing_provider", "missing_integration", "blocked_integration", "unsupported_events"])
def test_readiness_gaps_remain_prerequisites_not_success(configured, change):
    if change == "not_ready":
        configured.check["ready"] = False
    elif change == "missing_provider":
        configured.check["provider_contacted"] = False
    elif change == "missing_integration":
        configured.check["integrations"] = []
    elif change == "blocked_integration":
        configured.check["integrations"][0]["status"] = "blocked"
    else:
        configured.check["integrations"][0]["events"] = {"requested_mode_unavailable": True}
    with pytest.raises(ValueError, match="prerequisites are not ready"):
        confirm(configured)
    assert [row["phase"] for row in operation_fixtures.journal(configured)] == ["prepared", "unverified"]


def test_readiness_check_cannot_change_retained_configuration(configured):
    def respond(request, count):
        if request.method == "POST":
            configured.configurations[configured.entries[0]["scope"]["product_id"]]["hash"] = "changed"
    configured.respond = respond
    with pytest.raises(ValueError, match="changed during its readiness check"):
        confirm(configured)


@pytest.mark.parametrize("status", [302, 401, 403, 404, 503])
@pytest.mark.parametrize("step", [1, 3, 6, 10])
def test_denial_and_redirects_do_not_become_configuration_evidence(configured, status, step):
    configured.respond = lambda request, count: httpx.Response(status, headers={"Location": "https://other.example.org"}) if count == step else None
    with pytest.raises(ValueError, match="unavailable or redirected"):
        confirm(configured)
    assert len(configured.requests) == step


@pytest.mark.parametrize("fault", ["oversized", "malformed", "timeout"])
def test_incomplete_check_response_never_acknowledges_configuration(configured, fault):
    def respond(request, count):
        if request.method != "POST":
            return None
        if fault == "timeout":
            raise httpx.ReadTimeout("mock lost check response")
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1) if fault == "oversized" else b"{")
    configured.respond = respond
    with pytest.raises((ValueError, httpx.ReadTimeout)):
        confirm(configured)
    assert operation_fixtures.journal(configured)[-1]["phase"] == "unverified"


@pytest.mark.parametrize("fault", ["expired", "deadline", "budget", "foreign"])
def test_bounds_and_scope_stop_before_requests(configured, fault):
    scope = configured.entries[0]["scope"]
    if fault == "expired":
        configured.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif fault == "deadline":
        configured.target.deadline = time.monotonic() - 1
    elif fault == "budget":
        configured.target.calls = configured.target.max_requests - 8
    else:
        scope = {**scope, "product_id": "foreign"}
    with pytest.raises(ValueError):
        configured.target.read_configuration(scope)
    assert configured.requests == []


def test_exact_check_budget_preserves_cleanup_capacity(configured):
    configured.target.calls = configured.target.max_requests - 15
    confirm(configured)
    assert configured.target.calls == configured.target.max_requests - 4


def test_authority_expiring_during_check_is_not_acknowledged(configured, monkeypatch):
    clock = time.monotonic()
    monkeypatch.setattr("installed_configuration.time.monotonic", lambda: clock)
    def respond(request, count):
        nonlocal clock
        if request.method == "POST":
            clock += 3600
    configured.respond = respond
    with pytest.raises(ValueError, match="invalid or expired"):
        confirm(configured)
    assert operation_fixtures.journal(configured)[-1]["phase"] == "unverified"


@pytest.mark.parametrize("change", ["false", "missing_operations", "duplicate_receipt"])
def test_explicit_installed_configuration_selection_is_required(configured, change):
    if change == "false":
        configured.manifest["portability"]["installed_configuration"] = False
    elif change == "missing_operations":
        configured.manifest["portability"].pop("installed_operations")
    else:
        configured.manifest["projects"][1]["configuration_receipt"] = configured.manifest["projects"][0]["configuration_receipt"]
    with pytest.raises(ValueError):
        validate_installed_configuration(configured.manifest)


async def test_sql_configuration_and_product_profile_use_real_serializers(configured, tmp_path, monkeypatch):
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "offline-configuration-test-not-a-secret")
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.api.configuration import ConfigurationStore, view
    from l5_services.models.base import Base
    from l5_services.models.configuration import Configuration, ConfigurationVersion
    from src.products.models import Product, Project
    from src.products.routes import profile

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'configuration.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for index, entry in enumerate(configured.entries, start=1):
                scope = entry["scope"]
                current = configured.configurations[scope["product_id"]]
                current["scope"]["relationship_revision"] = "1"
                configured.bindings[entry["binding_id"]]["scope"]["relationship_revision"] = "1"
                session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id=current["configuration_id"],
                    product_id=scope["product_id"], project_id=scope["project_id"], service_id="devops",
                    relationship_revision="1", owner_id=current["scope"]["owner_id"], current_revision=1, handoff_state="linked"))
                session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id=current["configuration_id"],
                    revision=1, settings=deepcopy(current["settings"]), inherited_fields=[], gaps=[], state="ready", hash=current["hash"]))
                selected_profile = configured.profiles[scope["product_id"]]
                product = Product(tenant_id=scope["tenant_id"], id=scope["product_id"], revision=7,
                    name=f"Product {index}", purpose="Test configuration", intended_users=["operators"],
                    owner_user_id=current["scope"]["owner_id"], team_id=selected_profile["product"]["team_id"],
                    starting_point="new", lifecycle_status="draft", goals=[])
                project = Project(tenant_id=scope["tenant_id"], id=scope["project_id"], name=f"Project {index}",
                    owner_user_id=current["scope"]["owner_id"], state="registered",
                    configuration=deepcopy(selected_profile["configuration"]), provenance=[])
                configured.profiles[scope["product_id"]] = jsonable_encoder(profile(product, project))
            await session.commit()
            for entry in configured.entries:
                scope = entry["scope"]
                current = configured.configurations[scope["product_id"]]
                store = ConfigurationStore(session, scope["tenant_id"], current["scope"]["owner_id"])
                saved, version = await store.current(scope["product_id"], scope["project_id"], "devops")
                configured.configurations[scope["product_id"]] = jsonable_encoder(view(saved, version))
        confirm(configured)
        assert read(configured, 1)["scope"] == configured.entries[1]["scope"]
    finally:
        await engine.dispose()

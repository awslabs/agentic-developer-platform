"""Installed portability submissions with only the HTTP boundary mocked."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import Client

from context_selector import authorize_channels
from installed_operations import validate_installed_operations
import test_installed_portability as access_fixtures

installed_access = access_fixtures.installed_access
portable_selection = access_fixtures.portable_selection
selector_case = access_fixtures.selector_case


@pytest.fixture
def operations(installed_access, monkeypatch):
    case = installed_access
    case.manifest["portability"]["action_channels"].pop("run_operation")
    case.operations, case.configurations, case.runs, case.posts, case.http = [], {}, {}, [], []
    case.fault = None
    for index, access in enumerate(case.entries, start=1):
        scope = access["scope"]
        body = {"service_id": "devops", "configuration_revision": 1, "binding_id": access["binding_id"],
                "capability": "investigation", "devops_input": {"kind": "investigation", "question": "What failed?",
                    "resource_refs": [{"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                        "source_connection_id": access["provider"]["source_connection_id"],
                        "record_type": "resource", "record_id": f"resource-{index}", "record_revision": "1"}],
                    "telemetry_refs": []}}
        token = f"DEVOPS_PORTABLE_SUBMIT_{index}"
        monkeypatch.setenv(token, f"private-operation-submitter-{index}")
        case.operations.append({"scope": deepcopy(scope), "submit_token_env": token, "input": body})
        binding = case.bindings[access["binding_id"]]
        binding["configuration_id"] = f"configuration-{index}"
        case.configurations[scope["product_id"]] = {
            "configuration_id": binding["configuration_id"], "revision": 1,
            "scope": deepcopy(binding["scope"]), "state": "ready", "gaps": [], "handoff_state": "linked",
            "settings": {"enabled": True, "capabilities": ["investigation"], "limits": {
                "max_cost": "0.25", "currency": "USD", "deadline_seconds": 120, "concurrency": 1,
                "max_provider_attempts": 1, "max_polls": 4}}}
    case.manifest["portability"]["installed_operations"] = {"projects": case.operations}

    def respond(request):
        assert request.url.host == "gateway.example.org"
        entry = next(entry for entry in case.operations if f"/products/{entry['scope']['product_id']}/" in request.url.path)
        index = case.operations.index(entry)
        access = case.entries[index]
        prefix = f"/api/v1/products/{entry['scope']['product_id']}/projects/{entry['scope']['project_id']}/services/devops"
        assert request.url.path.startswith(prefix)
        assert request.headers["Authorization"] == "Bearer " + (
            f"private-operation-submitter-{index + 1}" if request.method == "POST"
            else f"independent-portable-reader-{index + 1}")
        suffix = request.url.path.removeprefix(prefix)
        case.http.append((request.method, suffix))
        configuration = case.configurations[entry["scope"]["product_id"]]
        if suffix == "/configuration":
            return httpx.Response(200, json=deepcopy(configuration))
        if suffix.startswith("/bindings/"):
            return httpx.Response(200, json=deepcopy(case.bindings[access["binding_id"]]))
        if request.method == "POST":
            assert suffix == "/runs" and json.loads(request.content) == entry["input"]
            receipt = request.headers["Idempotency-Key"]
            assert receipt == case.manifest["projects"][index]["operation_receipt"]
            case.posts.append(receipt)
            if case.fault in {"denied", "redirected", "oversized"}:
                return (httpx.Response(403 if case.fault == "denied" else 302, headers={"Location": "https://foreign.example.org"})
                        if case.fault != "oversized" else httpx.Response(202, content=b"x" * (1024 * 1024 + 1)))
            case.runs.setdefault(receipt, {"run_id": f"run-{index + 1}", "input": deepcopy(entry["input"]),
                "scope": deepcopy(configuration["scope"]), "origin": "adp_submitted", "state": "admitted",
                "admission": {"scope": deepcopy(configuration["scope"]), "service_id": "devops",
                    "native_space_ref": deepcopy(case.bindings[access["binding_id"]]["native_resource_ref"]),
                    "provider_identity": deepcopy(case.bindings[access["binding_id"]]["provider_identity"]),
                    "limits": deepcopy(configuration["settings"]["limits"])}})
            if case.fault == "lost_response":
                raise httpx.ReadTimeout("response lost after admission")
            return httpx.Response(202, json=deepcopy(case.runs[receipt]))
        run = deepcopy(next(run for run in case.runs.values() if suffix == "/runs/" + run["run_id"]))
        if case.fault == "foreign_readback":
            run["scope"]["product_id"] = "foreign"
        if case.fault == "changed_input":
            run["input"]["devops_input"]["question"] = "Different work"
        if case.fault == "revoked_readback":
            return httpx.Response(403)
        return httpx.Response(200, json=run)

    target = access_fixtures.target_for(case)
    target.client.close()
    target.client = Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    case.target = target
    yield case
    target.close()


def submit(case, index=0):
    project = case.manifest["projects"][index]
    return case.target.run_operation(project["scope"], project["capability"], project["limits"]["operation"])


def journal(case):
    path = case.directory / f"devops-{case.manifest['authorization']['run_id']}-portability.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_two_products_submit_through_installed_run_api(operations):
    case = operations
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")
    assert submit(case) == {"run_id": "run-1"}
    assert submit(case, 1) == {"run_id": "run-2"}
    assert case.target.calls == 8 and len(case.runs) == 2
    assert case.http == [("GET", "/configuration"), ("GET", "/bindings/binding-1"),
                         ("POST", "/runs"), ("GET", "/runs/run-1"),
                         ("GET", "/configuration"), ("GET", "/bindings/binding-2"),
                         ("POST", "/runs"), ("GET", "/runs/run-2")]
    assert [row["operation_id"] for row in journal(case) if row["phase"] == "acknowledged"] == ["run-1", "run-2"]
    assert "private-operation-submitter" not in json.dumps(journal(case))


@pytest.mark.parametrize("changed_input", [None, "binding_id", "configuration_revision"])
async def test_sql_admitted_run_readback_uses_public_input(operations, tmp_path, changed_input):
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.admission import admit_run
    from l5_services.models.base import Base
    from l5_services.models.configuration import Configuration, ConfigurationVersion
    from l5_services.models.run import view_run
    from l5_services.run_access import RunStore

    case = operations
    body = case.operations[0]["input"]
    binding = case.bindings[body["binding_id"]]
    configuration = case.configurations[case.operations[0]["scope"]["product_id"]]
    scope = configuration["scope"]
    scope["relationship_revision"] = "1"
    binding["scope"] = deepcopy(scope)
    resource = body["devops_input"]["resource_refs"][0]
    connection = {**resource, "record_type": "connection", "record_id": resource["source_connection_id"]}
    receipt = case.manifest["projects"][0]["operation_receipt"]
    verified = {
        "scope": scope, "service_id": "devops", "actor_id": scope["owner_id"], "actor_type": "human",
        "configuration_id": configuration["configuration_id"], "configuration_revision": 1,
        "configuration_hash": "f" * 64, "configuration_enabled": True,
        "binding_id": body["binding_id"], "binding_generation": binding["generation"], "binding_state": "ready",
        "connection_ref": connection, "binding_connection_ref": connection,
        "native_space_ref": binding["native_resource_ref"], "provider_identity": binding["provider_identity"],
        "delegation_id": "delegation-one", "delegation_version": 1, "delegation_active": True,
        "profile_ref": {**resource, "record_type": "product", "record_id": scope["product_id"]},
        "trigger": {"trigger_id": "manual-one", "kind": "manual", "enabled": True,
                    "capability": "investigation", "binding_id": body["binding_id"],
                    "resource_refs": [resource], "telemetry_refs": []},
        "adapter_revision": "devops-v1", "visibility": scope["visibility"], "classification": "internal",
        "causal_origin": "manual", "exclusions": [], "max_bytes": 1024, "max_files": 10,
        "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
        "limits": configuration["settings"]["limits"],
    }

    async def budget_guard(session, limits, requested_scope, now, replay):
        assert limits == verified["limits"] and requested_scope == scope
        return await replay()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admitted.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Configuration(
                tenant_id=scope["tenant_id"], configuration_id=configuration["configuration_id"],
                product_id=scope["product_id"], project_id=scope["project_id"], service_id="devops",
                relationship_revision="1", owner_id=scope["owner_id"], current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(
                tenant_id=scope["tenant_id"], configuration_id=configuration["configuration_id"], revision=1,
                settings=configuration["settings"], inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
            await session.commit()
            admitted = await admit_run(session, None, body, verified, receipt, budget_guard=budget_guard)
        async with sessions() as independent:
            store = RunStore(independent, scope["tenant_id"], scope["product_id"], scope["project_id"],
                             "devops", scope["owner_id"], "1")
            wire = jsonable_encoder(view_run(await store.get(admitted.run_id)))
        assert "binding_id" not in wire["admission"] and "configuration_revision" not in wire["admission"]
        assert wire["input"] == body
        case.runs[receipt] = wire
        if changed_input is None:
            assert submit(case) == {"run_id": admitted.run_id}
            assert [record["phase"] for record in journal(case)] == ["prepared", "acknowledged"]
        else:
            wire["input"][changed_input] = "foreign-binding" if changed_input == "binding_id" else 2
            with pytest.raises(ValueError, match="readback differs"):
                submit(case)
            assert journal(case)[-1]["phase"] == "uncertain"
        assert len(case.runs) == 1 and len(case.posts) == 1 and case.target.calls == 4
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", [
    lambda case: case.operations[0]["scope"].update(product_id="foreign"),
    lambda case: case.operations[0]["input"].update(binding_id="foreign"),
    lambda case: case.operations[0]["input"]["devops_input"]["resource_refs"][0].update(tenant_id="foreign"),
    lambda case: case.operations[0]["input"].update(allow_duplicate_effects=True),
    lambda case: case.operations[0].update(submit_token_env=case.entries[0]["product_token_env"]),
    lambda case: case.operations[1].update(submit_token_env=case.operations[0]["submit_token_env"]),
    lambda case: case.manifest["projects"][1].update(operation_receipt=case.manifest["projects"][0]["operation_receipt"]),
])
def test_invalid_private_selection_prevents_all_http_requests(operations, change):
    change(operations)
    with pytest.raises(ValueError):
        validate_installed_operations(operations.manifest)
    assert operations.http == []


@pytest.mark.parametrize("change", [
    lambda configuration: configuration.update(revision=2),
    lambda configuration: configuration["scope"].update(product_id="foreign"),
    lambda configuration: configuration.update(state="blocked"),
    lambda configuration: configuration["settings"].update(enabled=False),
    lambda configuration: configuration["settings"]["limits"].update(max_cost="0.26"),
    lambda configuration: configuration["settings"]["limits"].update(max_cost="0"),
    lambda configuration: configuration["settings"]["limits"].update(max_cost="NaN"),
    lambda configuration: configuration["settings"]["limits"].update(currency="EUR"),
    lambda configuration: configuration["settings"]["limits"].update(deadline_seconds=121),
    lambda configuration: configuration["settings"]["limits"].update(concurrency=6),
    lambda configuration: configuration["settings"]["limits"].update(max_polls=5),
])
def test_stale_foreign_or_unbounded_configuration_prevents_submission(operations, change):
    change(next(iter(operations.configurations.values())))
    with pytest.raises(ValueError):
        submit(operations)
    assert operations.posts == [] and journal(operations) == []


@pytest.mark.parametrize("change", [
    lambda binding: binding.update(revision=4),
    lambda binding: binding.update(state="revoked"),
    lambda binding: binding["provider_identity"].update(account_id="999999999999"),
    lambda binding: binding["native_resource_ref"].update(record_id="foreign"),
])
def test_changed_binding_prevents_submission(operations, change):
    change(operations.bindings["binding-1"])
    with pytest.raises(ValueError, match="binding"):
        submit(operations)
    assert operations.posts == []


@pytest.mark.parametrize("fault", ["denied", "redirected", "oversized", "foreign_readback", "changed_input", "revoked_readback"])
def test_rejected_or_inconclusive_submission_keeps_original_receipt(operations, fault):
    operations.fault = fault
    with pytest.raises(ValueError):
        submit(operations)
    assert len(operations.posts) == 1
    records = journal(operations)
    assert records[0]["phase"] == "prepared" and records[-1]["phase"] == "uncertain"
    assert all(row["receipt"] == operations.posts[0] for row in records)
    assert all("foreign.example.org" not in str(item) for item in operations.http)


def test_timeout_recovery_reuses_receipt_and_original_durable_run(operations):
    operations.fault = "lost_response"
    with pytest.raises(TimeoutError, match="original receipt"):
        submit(operations)
    assert len(operations.runs) == 1
    operations.fault = None
    assert submit(operations) == submit(operations) == {"run_id": "run-1"}
    assert len(operations.runs) == 1 and len(set(operations.posts)) == 1


@pytest.mark.parametrize("boundary", ["expiry", "deadline", "requests", "cleanup", "limits"])
def test_authorized_bounds_prevent_submission(operations, boundary):
    case = operations
    if boundary == "expiry":
        case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif boundary == "deadline":
        case.target.deadline = time.monotonic() - 1
    elif boundary == "requests":
        case.target.calls = case.target.max_requests - 7
    elif boundary == "cleanup":
        case.target.cleanup_phase = True
    project = case.manifest["projects"][0]
    with pytest.raises(ValueError):
        case.target.run_operation(project["scope"], project["capability"],
            {**project["limits"]["operation"], "max_spend_usd": "999"} if boundary == "limits"
            else project["limits"]["operation"])
    assert case.http == []

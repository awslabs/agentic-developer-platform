"""Installed native readbacks; only external Product/provider transports are mocked."""

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from botocore.session import Session
from botocore.validate import validate_parameters
from httpx import Client

from context_selector import authorize_channels
from installed_native import validate_installed_native
from portability import verify_portability
from provider_task import read_task
from test_portability import FakePlatform, FakeReader
from test_installed_results import result_fixture
import test_installed_operations as operation_fixtures

operations = operation_fixtures.operations
installed_access = operation_fixtures.installed_access
portable_selection = operation_fixtures.portable_selection
selector_case = operation_fixtures.selector_case


@pytest.fixture
def native_case(operations, monkeypatch):
    case = operations
    case.manifest["portability"]["installed_native"] = True
    case.manifest["portability"]["readback_channels"].pop("native")
    case.results, case.contents, case.tasks = {}, {}, {}
    for index, project in enumerate(case.manifest["projects"]):
        project["limits"]["operation"]["max_requests"] = 20
        operation_fixtures.submit(case, index)
        run = case.runs[project["operation_receipt"]]
        entry = case.entries[index]
        run["state"] = "succeeded"
        run["admission"].update(binding_generation=case.bindings[entry["binding_id"]]["generation"],
                                connection_ref={"source_connection_id": entry["provider"]["source_connection_id"]})
        run["native_job_ref"] = {key: project["scope"][key] for key in ("tenant_id", "project_id")}
        run["native_job_ref"].update(source_connection_id=entry["provider"]["source_connection_id"],
                                   record_type="native_job", record_id=f"execution-{index + 1}", record_revision=f"task-{index + 1}")
        result, content, task = result_fixture(run)
        task.update(createdAt=datetime.now(UTC) - timedelta(seconds=10), updatedAt=datetime.now(UTC) - timedelta(seconds=1))
        case.results[project["scope"]["product_id"]] = result
        case.contents[project["scope"]["product_id"]] = content
        case.tasks[project["scope"]["product_id"]] = task
    case.target.calls = 0
    case.native_calls, case.reads, case.posts = [], [], []
    case.after_native, case.response_body = None, None
    case.pending, case.response_status = [], 200
    case.run_for = lambda index=0: case.runs[case.manifest["projects"][index]["operation_receipt"]]

    def transport(request):
        assert request.method == "GET" and request.url.host == "gateway.example.org"
        entry = next(entry for entry in case.entries if f"/products/{entry['scope']['product_id']}/" in request.url.path)
        product = entry["scope"]["product_id"]
        index = case.entries.index(entry)
        assert request.headers["Authorization"] == f"Bearer independent-portable-reader-{index + 1}"
        prefix = f"/api/v1/products/{product}/projects/{entry['scope']['project_id']}/services/devops"
        suffix = request.url.path.removeprefix(prefix)
        case.reads.append((product, suffix))
        if case.response_body is not None:
            return httpx.Response(case.response_status, content=case.response_body)
        if case.response_status != 200:
            return httpx.Response(case.response_status, headers={"Location": "https://foreign.example.org"})
        if suffix == "/bindings/" + entry["binding_id"]:
            body = deepcopy(case.bindings[entry["binding_id"]])
        elif suffix == "/runs/" + case.run_for(index)["run_id"]:
            body = deepcopy(case.run_for(index))
            if case.pending:
                body["state"] = case.pending.pop(0)
        elif suffix == "/results":
            body = {"items": [deepcopy(case.results[product])]}
        elif suffix == "/results/" + case.results[product]["result_id"]:
            body = deepcopy(case.results[product])
        else:
            assert suffix == "/artifacts/" + case.results[product]["evidence_ref"]["artifact_id"] + "/content"
            return httpx.Response(200, content=case.contents[product])
        return httpx.Response(200, json=body)

    model = Session().get_service_model("devops-agent")

    def provider(selected, space_id, task_id, *, deadline):
        assert time.monotonic() < deadline
        entry = next(entry for entry in case.entries if entry["provider"] == selected)

        class Boundary:
            def get_caller_identity(self):
                case.native_calls.append("sts")
                return case.identities[selected["account_id"]]

            def get_backlog_task(self, **kwargs):
                validate_parameters(kwargs, model.operation_model("GetBacklogTask").input_shape)
                assert kwargs == {"agentSpaceId": space_id, "taskId": task_id}
                case.native_calls.append("task")
                if case.after_native:
                    case.after_native()
                return {"task": deepcopy(case.tasks[entry["scope"]["product_id"]])}

        def factory(service, **kwargs):
            assert service in {"sts", "devops-agent"} and kwargs["region_name"] == selected["region"]
            assert kwargs["config"].retries["total_max_attempts"] == 1
            return Boundary()

        return read_task(selected["account_id"], selected["role_arn"], selected["region"], space_id, task_id,
                         client_factory=factory)

    case.target.client.close()
    case.target.client = Client(transport=httpx.MockTransport(transport), follow_redirects=False)
    monkeypatch.setattr("installed_native.read_provider_task", provider)
    monkeypatch.setattr("installed_native.time.sleep", lambda seconds: None)
    return case


def read(case, index=0):
    return case.target.read_native(case.entries[index]["scope"], case.run_for(index)["run_id"])


def test_two_product_native_reads_do_not_invent_grounding_or_task_cost(native_case):
    case = native_case
    authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")
    for index, entry in enumerate(case.entries):
        observed = read(case, index)
        assert observed["scope"] == entry["scope"] and observed["native_job_id"] == f"execution-{index + 1}"
        assert observed["source_sha"] is None and observed["deployment_id"] is None
        assert set(observed["coverage"].values()) == {"unavailable"}
        assert observed["usage"]["cost_coverage"] == "unavailable" and "spend_usd" not in observed["usage"]
        assert observed["usage"]["requests"] == 10 and observed["usage"]["elapsed_seconds"] in {9, 10}
    assert case.target.calls == 20 and case.native_calls == ["sts", "task"] * 2 and case.posts == []


@pytest.mark.parametrize("field,value", [("state", "blocked"), ("revision", 4), ("generation", 3), ("gaps", ["revoked"])])
def test_current_binding_rejection_precedes_provider_calls(native_case, field, value):
    case = native_case
    case.bindings[case.entries[0]["binding_id"]][field] = value
    with pytest.raises(ValueError, match="binding|authority"):
        read(case)
    assert case.native_calls == []


@pytest.mark.parametrize("field,value", [("state", "failed"), ("origin", "native"), ("input", {}), ("admission", None),
                                        ("scope", {}), ("native_job_ref", {"record_id": "foreign"})])
def test_unmatched_run_precedes_provider_calls(native_case, field, value):
    case = native_case
    case.run_for()[field] = value
    with pytest.raises(ValueError, match="native"):
        read(case)
    assert case.native_calls == []


@pytest.mark.parametrize("field,value", [("agentSpaceId", "foreign"), ("taskId", "foreign"), ("executionId", "foreign"),
                                        ("taskType", "RELEASE_TESTING"), ("status", "FAILED"), ("createdAt", None),
                                        ("updatedAt", datetime.now(UTC) + timedelta(hours=1))])
def test_independent_task_must_match_original_identity(native_case, field, value):
    case = native_case
    case.tasks[case.entries[0]["scope"]["product_id"]][field] = value
    with pytest.raises(ValueError):
        read(case)
    assert not any("/results" in path for _, path in case.reads)


def test_wrong_selected_role_never_reads_native_task(native_case):
    case = native_case
    case.identities[case.entries[0]["provider"]["account_id"]]["Arn"] = "arn:aws:sts::123456789012:assumed-role/other/test"
    with pytest.raises(ValueError, match="identity"):
        read(case)
    assert case.native_calls == ["sts"]


@pytest.mark.parametrize("change", ["scope", "digest", "content", "run", "binding", "revoked"])
def test_changed_result_or_authority_is_not_accepted(native_case, change):
    case = native_case
    product = case.entries[0]["scope"]["product_id"]
    if change == "scope":
        case.results[product]["scope"] = {}
    elif change == "digest":
        case.results[product]["evidence_ref"]["sha256"] = "0" * 64
    elif change == "content":
        case.contents[product] = b"{}"
    elif change == "run":
        case.after_native = lambda: case.run_for().update(revision=2)
    elif change == "binding":
        case.after_native = lambda: case.bindings[case.entries[0]["binding_id"]].update(generation=3)
    else:
        case.after_native = lambda: setattr(case, "response_status", 404)
    with pytest.raises(ValueError):
        read(case)


@pytest.mark.parametrize("status", [302, 401, 403, 404, 503])
def test_http_failure_does_not_follow_redirect_or_reach_provider(native_case, status):
    native_case.response_status = status
    with pytest.raises(ValueError, match="unavailable or redirected"):
        read(native_case)
    assert len(native_case.reads) == 1 and native_case.native_calls == []


@pytest.mark.parametrize("body", [b"[]", b"x" * 65537])
def test_invalid_or_oversized_product_readback(native_case, body):
    native_case.response_body = body
    with pytest.raises(ValueError):
        read(native_case)
    assert native_case.native_calls == []


@pytest.mark.parametrize("limit", ["expired", "deadline", "requests", "operation", "cleanup"])
def test_no_effect_when_authorized_bounds_cannot_cover_readback(native_case, limit):
    case = native_case
    if limit == "expired":
        case.manifest["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif limit == "deadline":
        case.target.deadline = time.monotonic() - 1
    elif limit == "requests":
        case.target.calls = case.target.max_requests - case.target.cleanup_reserve - 8
    elif limit == "operation":
        case.manifest["projects"][0]["limits"]["operation"]["max_requests"] = 8
    else:
        case.target.cleanup_phase = True
    with pytest.raises(ValueError):
        read(case)
    assert case.reads == [] and case.native_calls == []


def test_pending_and_uncertain_work_polls_original_run_without_resubmission(native_case):
    case = native_case
    case.pending = ["admitted", "queued", "outcome_unknown", "running"]
    observed = read(case)
    assert {path for _, path in case.reads if "/runs/" in path} == {"/runs/" + observed["run_id"]}
    assert observed["usage"]["requests"] == 14 and case.posts == []


def test_pending_work_retains_cleanup_reservation(native_case):
    case = native_case
    case.target.calls = case.target.max_requests - case.target.cleanup_reserve - 10
    case.pending = ["queued"] * 20
    with pytest.raises(ValueError, match="bound"):
        read(case)
    assert case.native_calls == [] and case.posts == []
    assert case.target.calls <= case.target.max_requests - case.target.cleanup_reserve


@pytest.mark.parametrize("selection", [False, {}, "yes"])
def test_installed_reader_requires_explicit_selection(native_case, selection):
    native_case.manifest["portability"]["installed_native"] = selection
    with pytest.raises(ValueError, match="explicitly selected"):
        validate_installed_native(native_case.manifest)


def test_missing_custom_channel_is_replaced_only_when_selected(native_case):
    case = native_case
    case.manifest["portability"].pop("installed_native")
    with pytest.raises(ValueError, match="bounded actions and independent readbacks"):
        authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "portability")


def test_binding_revoked_while_polling_prevents_native_effects(native_case, monkeypatch):
    case = native_case
    case.pending = ["queued"]
    monkeypatch.setattr("installed_native.time.sleep", lambda seconds: case.bindings[case.entries[0]["binding_id"]].update(state="blocked"))
    with pytest.raises(ValueError, match="binding"):
        read(case)
    assert case.native_calls == []


def test_source_and_deployment_claims_are_not_invented_to_pass_portability(native_case):
    case = native_case
    platform = FakePlatform(case.manifest)
    reader = FakeReader(platform)
    reader.read_native = lambda scope, run_id: read(case)
    platform.run_operation = lambda scope, capability, limits: {"run_id": case.run_for()["run_id"]}
    with pytest.raises(ValueError, match="independently observed native result"):
        verify_portability(case.manifest, platform, reader)
    assert not any(action[0] == "context" for action in platform.actions)
    assert any(action[0] == "cleanup" for action in platform.actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, "binding_id", "configuration_revision"])
async def test_sql_admitted_run_and_result_are_compatible(native_case, tmp_path, changed):
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.admission import admit_run
    from l5_services.devops.results import evidence_bytes, normalize_investigation_result
    from l5_services.models.base import Base
    from l5_services.models.configuration import Configuration, ConfigurationVersion
    from l5_services.models.result import ResultDetail
    from l5_services.models.run import Run, view_run

    case = native_case
    body = case.operations[0]["input"]
    binding = case.bindings[body["binding_id"]]
    product = case.entries[0]["scope"]["product_id"]
    configuration = case.configurations[product]
    scope = binding["scope"]
    scope["relationship_revision"] = "1"
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
        "profile_ref": {**resource, "record_type": "product", "record_id": product},
        "trigger": {"trigger_id": "manual-one", "kind": "manual", "enabled": True,
                    "capability": "investigation", "binding_id": body["binding_id"], "resource_refs": [resource], "telemetry_refs": []},
        "adapter_revision": "devops-v1", "visibility": scope["visibility"], "classification": "internal",
        "causal_origin": "manual", "exclusions": [], "max_bytes": 1024, "max_files": 10,
        "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
        "limits": configuration["settings"]["limits"],
    }

    async def budget_guard(session, limits, requested_scope, now, replay):
        return await replay()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Configuration(
                tenant_id=scope["tenant_id"], configuration_id=configuration["configuration_id"],
                product_id=product, project_id=scope["project_id"], service_id="devops",
                relationship_revision="1", owner_id=scope["owner_id"], current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(
                tenant_id=scope["tenant_id"], configuration_id=configuration["configuration_id"], revision=1,
                settings=configuration["settings"], inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
            await session.commit()
            admitted = await admit_run(session, None, body, verified, receipt, budget_guard=budget_guard)
            admitted.state, admitted.native_job_ref = "succeeded", deepcopy(case.run_for()["native_job_ref"])
            collected = json.loads(case.contents[product])
            detail, _ = normalize_investigation_result(admitted, "result-sql", 1, collected, datetime.now(UTC))
            session.add(detail)
            await session.commit()
            run_id = admitted.run_id
        async with sessions() as independent:
            wire = jsonable_encoder(view_run(await independent.get(Run, (scope["tenant_id"], run_id))))
            detail = await independent.get(ResultDetail, (scope["tenant_id"], "result-sql"))
            case.results[product] = jsonable_encoder(detail.payload)
        case.runs[receipt] = wire
        case.contents[product] = evidence_bytes(collected)
        assert "binding_id" not in wire["admission"] and "configuration_revision" not in wire["admission"]
        if changed:
            wire["input"][changed] = "foreign" if changed == "binding_id" else 2
            with pytest.raises(ValueError, match="current authority"):
                read(case)
            assert case.native_calls == []
        else:
            assert read(case)["result_ref"] == "result-sql"
    finally:
        await engine.dispose()

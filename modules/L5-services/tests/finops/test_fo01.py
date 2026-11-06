import json
from pathlib import Path

import pytest


QUALIFICATION = Path(__file__).resolve().parents[4] / "docs/l5-services/finops/qualification.json"
REQUIRED_FACTS = {
    "provider_version", "supported_surface", "native_iam_resource_scope",
    "native_console_policy", "automation_terms", "eligibility",
    "regional_availability", "limits_pricing", "agent_identity", "conversation_identity",
    "turn_identity", "read_only_observation", "restart_session_expiry",
    "data_role_actions", "billing_view_isolation",
}


def test_qualification_requires_live_evidence_before_native_ready():
    record = json.loads(QUALIFICATION.read_text())
    assert record["schema_version"] == 1
    assert record["recorded_at"]
    assert record["design_revision"]
    assert set(record["facts"]) == REQUIRED_FACTS
    for fact in record["facts"].values():
        assert fact["status"] in {"documented", "unverified", "unsupported", "qualified"}
        assert all(fact[key] for key in ("source", "finding", "owner"))
        if fact["status"] == "qualified":
            assert fact["tested_revision"] and fact["private_evidence_ref"]
        else:
            assert fact["tested_revision"] is None
            assert fact["private_evidence_ref"] is None
    assert not record["native_ready"] or all(
        fact["status"] == "qualified" for fact in record["facts"].values()
    )
    assert record["native_ready"] is False


def test_native_recovery_procedure_preserves_live_evidence_gate():
    backlog = (QUALIFICATION.parent / "backlog.md").read_text()
    procedure = backlog.split("### FO01 SCN-01", 1)[1].split("### FO01 SCN-02", 1)[0]
    steps = (
        "1. **Authorize and pin:**",
        "2. **Identify before effect:**",
        "3. **Interrupt and observe:**",
        "4. **Expire and reauthorize:**",
        "5. **Reconcile and retain:**",
    )
    assert [procedure.index(step) for step in steps] == sorted(procedure.index(step) for step in steps)
    for invariant in (
        "not executed", "stop before any paid turn", "documented and authorized",
        "read-only", "outcome_unknown", "do not resubmit", "current delegation",
        "paid turn count", "private store", "native_ready` false", "unknown",
    ):
        assert invariant in procedure
    record = json.loads(QUALIFICATION.read_text())
    assert record["native_ready"] is False
    for name in ("agent_identity", "conversation_identity", "turn_identity",
                 "read_only_observation", "restart_session_expiry", "limits_pricing"):
        assert record["facts"][name]["status"] != "qualified"
        assert record["facts"][name]["private_evidence_ref"] is None


def test_scoped_finops_contract_definitions():
    from jsonschema import Draft202012Validator, FormatChecker
    from l5_services.run_input import FinopsRunInput

    repository = QUALIFICATION.parents[3]
    schema = json.loads((repository / "docs/l5-services/contracts/v1/schemas.json").read_text())
    draft = json.loads((repository / "docs/l5-services/finops/contract.json").read_text())
    for name, definition in draft["$defs"].items():
        if name != "RecordRef":
            assert schema["$defs"][name] == definition
    query = {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
             "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}
    request = {"service_id": "finops", "capability": "cost_inquiry",
               "configuration_revision": 1, "binding_id": "binding-example",
               "question": "What changed in January?", "query": query}
    request_validator = Draft202012Validator(
        {**schema, "$ref": "#/$defs/FinopsRunInput"}, format_checker=FormatChecker())
    assert request_validator.is_valid(request)
    assert FinopsRunInput.model_validate(request).sources == []
    assert not request_validator.is_valid({**request, "query": {**query, "metric": "Forecast"}})
    measure = {"amount": "12.34", "currency": "USD", "estimated": False,
               "source_refreshed_at": None, "freshness_reason": None, "coverage": "complete"}
    artifact = {"artifact_id": "artifact-example", "version": "version-1", "sha256": "0" * 64}
    result = {"kind": "finops_cost_inquiry", "run_id": "run-example", "query": query,
              "scope_digest": "1" * 64, "binding_generation": 1, "attribution": "unknown",
              "attribution_evidence": [],
              "native": {"status": "answered", "conversation_id": "conversation-example",
                         "turn_id": "turn-example", "answer_artifact": artifact,
                         "observed_at": "2026-02-02T00:00:00Z", "measure": measure},
              "oracle": {"collection_id": "collection-example", "query_hash": "2" * 64,
                         "observed_at": "2026-02-02T00:00:00Z", "pages": [artifact], "measure": measure},
              "comparison": {"state": "exact", "delta": "0", "display_quantum": "0.01", "reason": None},
              "billing_scope": {"account_ids": ["123456789012"], "view_ref": "view-example",
                                "snapshot_artifact": artifact}}
    result_validator = Draft202012Validator(
        {**schema, "$ref": "#/$defs/FinopsCostResult"}, format_checker=FormatChecker())
    assert result_validator.is_valid(result)
    assert not result_validator.is_valid({**result, "provider_session": "not-public"})


def finops_request():
    return {"service_id": "finops", "capability": "cost_inquiry",
            "configuration_revision": 1, "binding_id": "binding-example",
            "question": "How much in January?",
            "query": {"start": "2026-01-01", "end": "2026-02-01",
                      "metric": "UnblendedCost", "granularity": "MONTHLY",
                      "group_by": ["SERVICE"], "currency": "USD"}}


@pytest.mark.parametrize("service_id", ["security", "devops", "finops"])
async def test_contract_variants(monkeypatch, service_id):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker

    from l5_services import work_store
    from l5_services.api.router import create_router
    from l5_services.run_input import validate_run_input
    from l5_services.workers import execution

    repository = QUALIFICATION.parents[3]
    examples = json.loads((repository / "docs/l5-services/contracts/v1/examples.json").read_text())
    schema = json.loads((repository / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/RunInput"}, format_checker=FormatChecker())
    cases = [entry for entry in examples if entry["schema"] == "RunInput" and
             entry["value"].get("service_id", "security") == service_id]
    assert len(cases) == 2
    assert {case["valid"] for case in cases} == {True, False}

    submitted = []

    class RunReply(dict):
        revision = 1

    class Backend:
        async def submit(self, product_id, project_id, service, value, key):
            submitted.append((service, value))
            if service == "finops":
                raise HTTPException(409, detail={"code": "native_unqualified"})
            return RunReply(run_id="run-example", revision=1)

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        for case in cases:
            value = case["value"]
            assert validator.is_valid(value) == case["valid"]
            if case["valid"]:
                validate_run_input(value)
            else:
                with pytest.raises(HTTPException) as error:
                    validate_run_input(value)
                assert error.value.status_code == 422
            response = await client.post(
                f"/v1/products/product-example/projects/project-example/services/{service_id}/runs",
                json=value, headers={"Idempotency-Key": "contract-variant-example-0001"})
            expected = 409 if service_id == "finops" and case["valid"] else 202 if case["valid"] else 422
            assert response.status_code == expected, response.text
            if not case["valid"]:
                assert response.json() == {"detail": {"code": "validation_error"}}
    assert submitted == [(service_id, next(case["value"] for case in cases if case["valid"]))]

    if service_id == "finops":
        claim = SimpleNamespace(tenant_id="tenant-example", run_id="run-example", run_revision=1, kind="submit")
        run = SimpleNamespace(revision=1, service_id="finops")

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def scalar(self, statement):
                return None

            async def get(self, model, key):
                return run

        async def claim_work(*args, **kwargs):
            return claim

        async def finish_work(*args, **kwargs):
            assert kwargs["state"] == "blocked"
            assert kwargs["provider_state"] == "not_started"
            return "completed"

        monkeypatch.setattr(execution, "claim_work", claim_work)
        monkeypatch.setattr(execution, "finish_work", finish_work)
        prepare = AsyncMock()
        monkeypatch.setattr(work_store, "prepare_submit_attempt", prepare)
        adapter = AsyncMock()
        authorize = AsyncMock()
        worker = execution.ExecutionService(lambda: Session(), None, adapter, None, authorize)
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        prepare.assert_not_called()
        adapter.submit.assert_not_called()
        authorize.assert_not_called()


async def test_finops_route_validates_without_native_admission():
    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker
    from l5_services.api.router import create_router

    schema = json.loads((QUALIFICATION.parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/RunInput"}, format_checker=FormatChecker())
    request = finops_request()
    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "connection-example", "record_type": "document",
              "record_id": "document-example", "record_revision": "version-1"}
    security = {"configuration_revision": 1, "binding_id": "binding-example",
                "capability": "threat_model", "sources": [source]}
    devops = {"service_id": "devops", "configuration_revision": 1,
              "binding_id": "binding-example", "capability": "investigation",
              "devops_input": {"kind": "investigation", "question": "What changed?",
                               "resource_refs": [{**source, "record_type": "resource"}],
                               "telemetry_refs": []}}
    for value in (request, security, devops):
        assert validator.is_valid(value), value

    calls = []

    class RunReply(dict):
        revision = 1

    class Backend:
        async def submit(self, product_id, project_id, service_id, run_input, key):
            calls.append((product_id, project_id, service_id, run_input, key))
            if service_id == "finops":
                raise HTTPException(409, detail={"code": "native_unqualified"})
            return RunReply(run_id="run-example", revision=1)

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        for service, value, expected in (("finops", request, 409), ("security", security, 202),
                                         ("devops", devops, 202)):
            response = await client.post(
                f"/v1/products/product-example/projects/project-example/services/{service}/runs",
                json=value, headers={"Idempotency-Key": "manual-inquiry-example-00001"})
            assert response.status_code == expected, response.text
            if service == "finops":
                assert response.json() == {"detail": {"code": "native_unqualified"}}
    assert [call[2] for call in calls] == ["finops", "security", "devops"]


async def test_unqualified_finops_work_blocks_before_provider_or_attempt(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from l5_services import work_store
    from l5_services.workers import execution

    claim = SimpleNamespace(tenant_id="tenant-example", run_id="run-example",
                            run_revision=1, kind="submit")
    run = SimpleNamespace(revision=1, service_id="finops")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, statement):
            return None

        async def get(self, model, key):
            return run

    async def claim_work(*args, **kwargs):
        return claim

    async def finish_work(*args, **kwargs):
        assert kwargs["state"] == "blocked"
        assert kwargs["provider_state"] == "not_started"
        return "completed"

    monkeypatch.setattr(execution, "claim_work", claim_work)
    monkeypatch.setattr(execution, "finish_work", finish_work)
    prepare = AsyncMock()
    monkeypatch.setattr(work_store, "prepare_submit_attempt", prepare)
    adapter = AsyncMock()
    authorize = AsyncMock()
    service = execution.ExecutionService(lambda: Session(), None, adapter, None, authorize)
    assert await service.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
    adapter.submit.assert_not_called()
    authorize.assert_not_called()
    prepare.assert_not_called()


@pytest.mark.parametrize("location", ["request", "query", "source"])
async def test_finops_unknown_fields_fail_schema_runtime_and_route(location):
    from copy import deepcopy

    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker
    from pydantic import ValidationError

    from l5_services.api.router import create_router
    from l5_services.run_input import FinopsRunInput, validate_run_input

    value = deepcopy(finops_request())
    if location == "request":
        value["executor_options"] = {"retry": True}
    elif location == "query":
        value["query"]["forecast"] = True
    else:
        value["sources"] = [{"tenant_id": "tenant-example", "project_id": "project-example",
                             "source_connection_id": "connection-example", "record_type": "document",
                             "record_id": "document-example", "record_revision": "version-1",
                             "credential_ref": "not-allowed"}]

    schema = json.loads((QUALIFICATION.parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/FinopsRunInput"},
                                     format_checker=FormatChecker())
    assert any(error.validator == "additionalProperties" for error in validator.iter_errors(value))
    with pytest.raises(ValidationError):
        FinopsRunInput.model_validate(value)
    with pytest.raises(HTTPException) as failure:
        validate_run_input(value)
    assert failure.value.status_code == 422
    assert failure.value.detail == {"code": "validation_error"}

    class Backend:
        async def submit(self, *args):
            raise AssertionError("invalid input reached submission")

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(
            "/v1/products/product-example/projects/project-example/services/finops/runs",
            json=value, headers={"Idempotency-Key": "invalid-inquiry-example-00001"})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "validation_error"}}


@pytest.mark.parametrize("field", ["view_ref", "data_role_ref"])
@pytest.mark.parametrize("location", ["request", "query"])
async def test_caller_view_and_role_never_reach_admission(field, location):
    from copy import deepcopy

    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker
    from pydantic import ValidationError

    from l5_services.api.router import create_router
    from l5_services.run_input import FinopsRunInput, validate_run_input

    request = deepcopy(finops_request())
    selected = request if location == "request" else request["query"]
    selected[field] = "caller-selected-example"
    schema = json.loads((QUALIFICATION.parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/FinopsRunInput"},
                                     format_checker=FormatChecker())
    assert any(error.validator == "additionalProperties" for error in validator.iter_errors(request))
    with pytest.raises(ValidationError):
        FinopsRunInput.model_validate(request)
    with pytest.raises(HTTPException) as failure:
        validate_run_input(request)
    assert failure.value.status_code == 422
    assert failure.value.detail == {"code": "validation_error"}

    class Backend:
        async def submit(self, *args):
            raise AssertionError("caller scope reached admission")

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(
            "/v1/products/product-example/projects/project-example/services/finops/runs",
            json=request, headers={"Idempotency-Key": "invalid-inquiry-example-00002"})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "validation_error"}}


@pytest.mark.parametrize("change,schema_valid", [
    ({"start": "2026-02-30"}, False),
    ({"end": "2026-02-01T00:00:00Z"}, False),
    ({"start": "2026-02-01", "end": "2026-01-01"}, True),
    ({"start": "2026-02-01", "end": "2026-02-01"}, True),
    ({"metric": "AmortizedCost"}, False),
    ({"granularity": "HOURLY"}, False),
    ({"group_by": ["REGION"]}, False),
])
async def test_invalid_cost_period_and_metrics_fail_before_admission(change, schema_valid):
    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker
    from pydantic import ValidationError

    from l5_services.api.router import create_router
    from l5_services.run_input import FinopsRunInput, validate_run_input

    request = finops_request()
    request["query"] = {**request["query"], **change}
    schema = json.loads((QUALIFICATION.parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/FinopsRunInput"},
                                     format_checker=FormatChecker())
    assert validator.is_valid(request) is schema_valid
    with pytest.raises(ValidationError):
        FinopsRunInput.model_validate(request)
    with pytest.raises(HTTPException) as failure:
        validate_run_input(request)
    assert failure.value.status_code == 422
    assert failure.value.detail == {"code": "validation_error"}

    class Backend:
        async def submit(self, *args):
            raise AssertionError("invalid cost query reached admission")

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(
            "/v1/products/product-example/projects/project-example/services/finops/runs",
            json=request, headers={"Idempotency-Key": "invalid-period-example-00001"})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "validation_error"}}


def test_billing_boundary_requires_native_read_and_denial_evidence():
    record = json.loads(QUALIFICATION.read_text())
    boundary = record["billing_boundary"]
    assert boundary["selection"] == {
        "connection": "explicit_only", "billing_view": "explicit_only",
        "data_role": "explicit_only", "primary_view_fallback": False,
        "ambient_role_fallback": False,
    }
    checks = boundary["checks"]
    assert set(checks) == {
        "selected_view_contents", "provider_view_attachment", "data_role_permissions",
        "data_role_trust", "owned_view_read", "foreign_view_denial",
        "foreign_tenant_denial", "other_project_denial",
    }
    assert checks["owned_view_read"]["route"] == "native_data_role"
    assert checks["foreign_view_denial"]["route"] == "native_data_role"
    for check in checks.values():
        assert check["status"] in {"not_run", "blocked", "failed", "passed"}
        assert all(check[field] for field in ("expected", "source", "route", "owner"))
        if check["status"] == "passed":
            assert check["tested_revision"] and check["private_evidence_ref"]
        else:
            assert check["tested_revision"] is None
            assert check["private_evidence_ref"] is None
    if any(check["status"] != "passed" for check in checks.values()):
        assert record["native_ready"] is False


def test_billing_isolation_procedure_preserves_native_denial_gate():
    backlog = (QUALIFICATION.parent / "backlog.md").read_text()
    procedure = backlog.split("### FO01 SCN-03", 1)[1].split("### FO01 SCN-04", 1)[0]
    steps = (
        "1. **Authorize and pin scope:**",
        "2. **Read the boundary:**",
        "3. **Prove owned access:**",
        "4. **Prove foreign denial:**",
        "5. **Compare and retain:**",
    )
    assert [procedure.index(step) for step in steps] == sorted(procedure.index(step) for step in steps)
    checks = json.loads(QUALIFICATION.read_text())["billing_boundary"]["checks"]
    for check in checks:
        assert f"`{check}`" in procedure
    for invariant in (
        "not executed", "selected data role", "native/provider", "independent",
        "controlled foreign", "IAM policy simulation", "CE filter", "No selected-view fallback",
        "scope generation", "private evidence reference", "otherwise unknown", "owned-only",
    ):
        assert invariant in procedure
    for name in ("owned_view_read", "foreign_view_denial"):
        assert checks[name]["status"] != "passed"
        assert checks[name]["tested_revision"] is None
        assert checks[name]["private_evidence_ref"] is None


async def test_readiness_exposes_unresolved_catalog_and_relevant_options():
    import httpx
    from fastapi import FastAPI
    from jsonschema import Draft202012Validator

    from l5_services.api.router import create_router
    from l5_services.configuration_population import configuration_options

    profile = {"tenant_id": "tenant-example", "product_id": "product-example",
               "project_id": "project-example", "revision": 1,
               "configuration": {"selected_capabilities": [
                   {"capability": "finops", "ownership_mode": "managed"}]}}
    connection = {"tenant_id": "tenant-example", "project_id": "project-example",
                  "source_connection_id": "connection-example", "record_type": "connection",
                  "record_id": "connection-example", "record_revision": "version-1"}
    available = {"coverage": "complete", "next_cursor": None, "items": [
        {"connection_ref": {"id": "connection-example"}, "record_ref": connection,
         "resources": []}]}
    options = configuration_options(profile, available, "finops")
    record = json.loads(QUALIFICATION.read_text())
    assert options["catalog_mapping"] == {
        key: record["catalog_mapping"][key]
        for key in ("status", "module_id", "feature_id", "owner")
    }
    assert record["catalog_mapping"]["tested_revision"] is None
    assert options["catalog_mapping"]["module_id"] is None
    assert options["catalog_mapping"]["feature_id"] is None
    assert options["capabilities"] == ["cost_inquiry"]
    assert options["connections"] == [connection]
    assert options["sources"] == []
    assert options["readiness"] == {"capability": "cost_inquiry", "status": "blocked",
                                    "gaps": options["unavailable_reasons"]}
    assert "native_unqualified" in options["unavailable_reasons"]
    assert "source_options_unavailable" not in options["unavailable_reasons"]
    assert "catalog_mapping_unresolved" not in options["unavailable_reasons"]
    schema = json.loads((QUALIFICATION.parents[3] / "docs/l5-services/contracts/v1/schemas.json").read_text())
    assert Draft202012Validator({**schema, "$ref": "#/$defs/Options"}).is_valid(options)

    class Backend:
        async def options(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == ("product-example", "project-example", "finops")
            return options

        async def get(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == ("product-example", "project-example", "finops")
            return {"hash": "version-example"}

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        path = "/v1/products/product-example/projects/project-example/services/finops/configuration"
        response = await client.get(path + "/options")
        closed = await client.get(path)
    assert response.status_code == 200 and response.json() == options
    assert closed.status_code == 200 and closed.headers["ETag"] == '"version-example"'

    unselected = configuration_options({**profile, "configuration": {}}, available, "finops")
    assert unselected["capabilities"] == []
    assert "capability_not_selected" in unselected["unavailable_reasons"]
    foreign = configuration_options(profile, {**available, "items": [
        {**available["items"][0], "record_ref": {**connection, "project_id": "foreign"}}]}, "finops")
    assert foreign["connections"] == []
    assert "connection_revision_unavailable" in foreign["unavailable_reasons"]


def test_unrelated_profile_fields_and_proposals_do_not_change_inquiry_gaps():
    from l5_services.configuration_population import configuration_options

    profile = {"tenant_id": "tenant-example", "product_id": "product-example",
               "project_id": "project-example", "revision": 1,
               "configuration": {"selected_capabilities": [
                   {"capability": "finops", "ownership_mode": "managed"}]}}
    available = {"coverage": "complete", "next_cursor": None, "items": []}
    baseline = configuration_options(profile, available, "finops")
    enriched = {**profile, "product": {"purpose": "Unrelated review"},
                "configuration": {**profile["configuration"],
                                  "basic": {"repositories": []},
                                  "governance": {"data_classification": "internal"},
                                  "financials": {"allocations": [{"approval_status": "proposed",
                                                                  "amount": {"amount": "999999", "currency": "USD"}}]}}}
    assert configuration_options(enriched, available, "finops") == baseline
    assert "source_options_unavailable" not in baseline["unavailable_reasons"]
    assert baseline["provider_contacted"] is False
    assert baseline["readiness"]["status"] == "blocked"


@pytest.mark.parametrize("observed_at,end,valid", [
    ("2026-10-26T23:59:00Z", "2026-10-27", False),
    ("2026-10-27T00:01:00Z", "2026-10-27", True),
    ("2026-10-27T00:01:00Z", "2026-10-28", False),
    ("2026-10-27T23:59:00Z", "2026-11-01", False),
])
async def test_utc_closed_period_at_runtime_and_host(monkeypatch, observed_at, end, valid):
    from datetime import UTC, date, datetime

    import httpx
    from fastapi import FastAPI, HTTPException
    from pydantic import ValidationError

    from l5_services.api.router import create_router
    from l5_services import run_input

    fixed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

    class FrozenClock:
        @staticmethod
        def now(timezone):
            assert timezone is UTC
            return fixed_time

    monkeypatch.setattr(run_input, "datetime", FrozenClock)
    request = finops_request()
    request["query"] = {**request["query"], "start": "2026-10-25", "end": end}
    if valid:
        assert run_input.FinopsRunInput.model_validate(request, context={"today": date(2026, 10, 27)})
        run_input.validate_run_input(request)
    else:
        with pytest.raises(ValidationError):
            run_input.FinopsRunInput.model_validate(request, context={"today": fixed_time.date()})
        with pytest.raises(HTTPException) as failure:
            run_input.validate_run_input(request)
        assert failure.value.status_code == 422

    calls = []

    class Backend:
        async def submit(self, *args):
            calls.append(args)
            raise HTTPException(409, detail={"code": "native_unqualified"})

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(
            "/v1/products/product-example/projects/project-example/services/finops/runs",
            json=request, headers={"Idempotency-Key": "closed-period-example-00001"})
    assert response.status_code == (409 if valid else 422)
    assert response.json()["detail"]["code"] == ("native_unqualified" if valid else "validation_error")
    assert len(calls) == int(valid)


@pytest.mark.parametrize("query_change", (
    {"metric": "Forecast"},
    {"start": "2026-02-01", "end": "2026-01-01"},
    {"group_by": ["SERVICE", "SERVICE"]},
    {"end": "9999-12-31"},
))
async def test_invalid_finops_cost_query_returns_422_without_submission(query_change):
    import httpx
    from fastapi import FastAPI
    from l5_services.api.router import create_router

    calls = []

    class Backend:
        async def submit(self, *args):
            calls.append(args)
            raise AssertionError("invalid cost query reached admission")

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    request = finops_request()
    request["query"] = {**request["query"], **query_change}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(
            "/v1/products/product-example/projects/project-example/services/finops/runs",
            json=request, headers={"Idempotency-Key": "invalid-query-example-0001"})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "validation_error"}}
    assert calls == []

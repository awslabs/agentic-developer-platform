"""Security and DevOps publishers never fall back to another service's queue."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request

from src import l5_integration


@pytest.fixture(params=[(), ("security",), ("devops",), ("security", "devops")])
def queues(request, monkeypatch):
    configured = {}
    for service_id, variable in (("security", "L5_WORK_QUEUE_URL"), ("devops", "L5_DEVOPS_WORK_QUEUE_URL")):
        if service_id in request.param:
            configured[service_id] = f"https://sqs.example.test/{service_id}-work"
            monkeypatch.setenv(variable, configured[service_id])
        else:
            monkeypatch.delenv(variable, raising=False)
    return configured


@pytest.mark.parametrize("service_id", ["security", "devops", "unknown"])
async def test_service_uses_only_its_configured_queue(service_id, queues, monkeypatch):
    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(l5_integration.boto3, "client", client_factory)
    actor = SimpleNamespace(org_id="tenant-a", user_id="owner-a")
    selected = l5_integration.backend(
        service_id=service_id,
        request=Request({"type": "http", "app": FastAPI()}),
        db=SimpleNamespace(bind=None),
        actor=actor,
        x_l5_delegation_id="delegation-a",
    )
    assert selected.actor is actor
    assert selected.delegation_id == "delegation-a"
    if service_id not in queues:
        assert selected.publish is None
        client_factory.assert_not_called()
        return

    envelope = {"schema_version": "1.0", "work_id": "work-a", "generation": 3}
    await selected.publish(envelope, 37)
    client_factory.assert_called_once_with("sqs")
    client.send_message.assert_called_once_with(QueueUrl=queues[service_id], MessageBody=json.dumps(envelope, separators=(",", ":")), DelaySeconds=37)


def test_incident_backend_keeps_devops_queue_and_receipt_scope(queues):
    selected = l5_integration.incident_work_backend()(SimpleNamespace(bind=None), SimpleNamespace(tenant_id="tenant-b"))
    assert selected.actor.org_id == "tenant-b"
    assert selected.actor.user_id is None
    assert selected.publish is (l5_integration.publish_l5_work if "devops" in queues else None)


async def test_unqualified_finops_submission_never_selects_queue_or_admits(queues, monkeypatch):
    from fastapi import HTTPException

    client_factory = MagicMock()
    monkeypatch.setattr(l5_integration.boto3, "client", client_factory)
    selected = l5_integration.backend(
        service_id="finops",
        request=Request({"type": "http", "app": FastAPI()}),
        db=SimpleNamespace(bind=None),
        actor=SimpleNamespace(org_id="tenant-example", user_id="owner-example"),
        x_l5_delegation_id="delegation-example",
    )
    assert selected.publish is None
    with pytest.raises(HTTPException) as failure:
        await selected.submit("product-example", "project-example", "finops", {}, "request-example-00001")
    assert failure.value.status_code == 409
    assert failure.value.detail == {"code": "native_unqualified"}
    client_factory.assert_not_called()


async def test_finops_rollback_disables_new_submissions_but_keeps_worker_transport(queues, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("L5_FINOPS_WORK_QUEUE_URL", "https://sqs.example.test/finops-work")
    app = FastAPI()
    app.state.l5_finops_authority = object()
    selected = l5_integration.backend(
        "finops",
        Request({"type": "http", "app": app}),
        SimpleNamespace(bind=None),
        SimpleNamespace(org_id="tenant-example", user_id="owner-example"),
        None,
    )
    assert selected.publish is not None
    assert selected.finops_authority is app.state.l5_finops_authority
    for setting in (None, "false", "TRUE"):
        if setting is None:
            monkeypatch.delenv("L5_FINOPS_ADMISSION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", setting)
        with pytest.raises(HTTPException) as failure:
            await selected.submit("product-example", "project-example", "finops", {}, "request-example-00001")
        assert failure.value.detail == {"code": "finops_admissions_disabled"}
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    selected.admission_authority = AsyncMock(return_value=({"service_id": "finops"}, object()))
    dispatched = AsyncMock(return_value="run-example")
    monkeypatch.setattr(l5_integration, "submit_and_dispatch", dispatched)
    assert await selected.submit("product-example", "project-example", "finops", {}, "request-example-00001") == "run-example"
    dispatched.assert_awaited_once()


async def test_finops_host_receives_only_server_selected_authority_and_own_queue(queues, monkeypatch):
    app = FastAPI()
    authority = object()
    app.state.l5_finops_authority = authority
    request = Request({"type": "http", "app": app})
    client = MagicMock()
    monkeypatch.setattr(l5_integration.boto3, "client", MagicMock(return_value=client))
    actor = SimpleNamespace(org_id="tenant-example", user_id="owner-example")

    selected = l5_integration.backend("finops", request, SimpleNamespace(bind=None), actor, None)

    assert selected.finops_authority is authority
    assert selected.publish is None
    client.send_message.assert_not_called()
    monkeypatch.setenv("L5_FINOPS_WORK_QUEUE_URL", "https://sqs.example.test/finops-work")
    selected = l5_integration.backend("finops", request, SimpleNamespace(bind=None), actor, None)
    await selected.publish({"work_id": "example-work"}, 0)
    assert client.send_message.call_args.kwargs["QueueUrl"] == "https://sqs.example.test/finops-work"

    other = l5_integration.backend("devops", request, SimpleNamespace(bind=None), actor, None)
    assert other.finops_authority is None


async def test_finops_options_use_profile_handoff_without_native_work():
    from unittest.mock import AsyncMock

    profile = {
        "tenant_id": "tenant-example",
        "product_id": "product-example",
        "project_id": "project-example",
        "revision": 1,
        "configuration": {"selected_capabilities": [{"capability": "finops", "ownership_mode": "managed"}]},
    }
    available = {"coverage": "complete", "next_cursor": None, "items": []}
    selected = object.__new__(l5_integration.GatewayConfigurationBackend)
    selected.profile = AsyncMock(return_value=profile)
    selected.profile_inputs = AsyncMock(return_value=(None, None, available))
    result = await selected.options("product-example", "project-example", "finops")
    assert result["service_id"] == "finops"
    assert result["capabilities"] == ["cost_inquiry"]
    assert result["readiness"]["status"] == "blocked"
    assert result["catalog_mapping"]["status"] == "unresolved"
    assert result["connections"] == []
    assert result["provider_contacted"] is False
    selected.profile.assert_awaited_once_with("product-example", "project-example")
    selected.profile_inputs.assert_awaited_once_with(profile, "product-example", "project-example", "finops")


@pytest.fixture
def finops_readiness_cases():
    from src.products.configuration import ProjectConfiguration

    reference = {
        "tenant_id": "tenant-example",
        "project_id": "project-example",
        "source_connection_id": "connection-example",
        "record_type": "connection",
        "record_id": "connection-example",
        "record_revision": "version-1",
    }
    profile = {
        "tenant_id": "tenant-example",
        "product_id": "product-example",
        "project_id": "project-example",
        "revision": 1,
        "configuration": ProjectConfiguration.model_validate(
            {
                "selected_capabilities": [{"capability": "finops", "ownership_mode": "managed"}],
            }
        ).model_dump(exclude_none=True),
    }
    sources = {
        "coverage": "complete",
        "next_cursor": None,
        "items": [{"connection_ref": {"id": "connection-example"}, "record_ref": reference, "resources": []}],
    }
    allocation_target = {**reference, "record_type": "product", "record_id": "product-example"}
    proposed = ProjectConfiguration.model_validate(
        {
            "selected_capabilities": [{"capability": "finops", "ownership_mode": "managed"}],
            "financials": {
                "allocations": [
                    {
                        "id": "allocation-example",
                        "name": "Proposed cloud budget",
                        "target_ref": allocation_target,
                        "amount": {"amount": "999999", "currency": "USD"},
                        "category": "cloud",
                        "period_start": "2026-01-01",
                        "period_end": "2026-12-31",
                        "approval_status": "proposed",
                        "owner_ref": {"kind": "user", "id": "owner-example"},
                    }
                ]
            },
        }
    ).model_dump(exclude_none=True)
    return {
        "selected_owned": (
            profile,
            sources,
            ["region_selection_required", "billing_scope_unverified", "native_unqualified"],
            [reference],
            ["cost_inquiry"],
        ),
        "unselected": (
            {**profile, "configuration": {}},
            sources,
            ["capability_not_selected", "region_selection_required", "billing_scope_unverified", "native_unqualified"],
            [reference],
            [],
        ),
        "foreign_only": (
            profile,
            {**sources, "items": [{**sources["items"][0], "record_ref": {**reference, "project_id": "foreign-project"}}]},
            ["connection_revision_unavailable", "region_selection_required", "billing_scope_unverified", "native_unqualified"],
            [],
            ["cost_inquiry"],
        ),
        "incomplete_page": (
            profile,
            {**sources, "coverage": "partial", "next_cursor": "cursor-example"},
            [
                "connection_options_incomplete",
                "connection_revision_unavailable",
                "region_selection_required",
                "billing_scope_unverified",
                "native_unqualified",
            ],
            [],
            ["cost_inquiry"],
        ),
        "proposed_allocation": (
            {**profile, "configuration": proposed},
            sources,
            ["region_selection_required", "billing_scope_unverified", "native_unqualified"],
            [reference],
            ["cost_inquiry"],
        ),
    }


async def test_readiness(finops_readiness_cases):
    from pathlib import Path
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI

    repository = Path(__file__).resolve().parents[4]
    record = json.loads((repository / "docs/l5-services/finops/qualification.json").read_text())
    responses = {}
    for case_name, (profile, sources, expected_gaps, connections, capabilities) in finops_readiness_cases.items():
        backend = object.__new__(l5_integration.GatewayConfigurationBackend)
        backend.profile = AsyncMock(return_value=profile)
        backend.profile_inputs = AsyncMock(return_value=(None, None, sources))
        app = FastAPI()
        app.include_router(l5_integration.create_router(lambda: backend))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            prefix = "/v1/products/product-example/projects/project-example/services/finops"
            response = await client.get(prefix + "/configuration/options")
            request = {
                "service_id": "finops",
                "capability": "cost_inquiry",
                "configuration_revision": 1,
                "binding_id": "binding-example",
                "question": "How much did January cost?",
                "query": {
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                    "metric": "UnblendedCost",
                    "granularity": "MONTHLY",
                    "group_by": [],
                    "currency": "USD",
                },
            }
            submission = await client.post(prefix + "/runs", json=request, headers={"Idempotency-Key": "readiness-example-00001"})
        assert response.status_code == 200, (case_name, response.text)
        options = response.json()
        assert options["readiness"] == {"capability": "cost_inquiry", "status": "blocked", "gaps": expected_gaps}, case_name
        assert options["unavailable_reasons"] == expected_gaps, case_name
        assert options["capabilities"] == capabilities, case_name
        assert options["connections"] == connections, case_name
        assert options["sources"] == [] and options["provider_contacted"] is False
        assert options["catalog_mapping"] == {key: record["catalog_mapping"][key] for key in ("status", "module_id", "feature_id", "owner")}
        assert options["catalog_mapping"]["status"] == "unresolved"
        assert options["catalog_mapping"]["module_id"] is None
        assert options["catalog_mapping"]["feature_id"] is None
        assert submission.status_code == 409
        assert submission.json() == {"detail": {"code": "native_unqualified"}}
        backend.profile.assert_awaited_once_with("product-example", "project-example")
        backend.profile_inputs.assert_awaited_once_with(profile, "product-example", "project-example", "finops")
        responses[case_name] = options
    assert responses["proposed_allocation"] == responses["selected_owned"]


async def test_proposed_product_allocation_cannot_authorize_finops_submission():
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from src.products.configuration import ProjectConfiguration

    reference = {
        "tenant_id": "tenant-example",
        "project_id": "project-example",
        "source_connection_id": "connection-example",
        "record_type": "product",
        "record_id": "product-example",
        "record_revision": "version-1",
    }
    configuration = ProjectConfiguration.model_validate(
        {
            "selected_capabilities": [{"capability": "finops", "ownership_mode": "managed"}],
            "financials": {
                "allocations": [
                    {
                        "id": "allocation-example",
                        "name": "Proposed cloud budget",
                        "target_ref": reference,
                        "amount": {"amount": "999999", "currency": "USD"},
                        "category": "cloud",
                        "period_start": "2026-01-01",
                        "period_end": "2026-12-31",
                        "approval_status": "proposed",
                        "owner_ref": {"kind": "user", "id": "owner-example"},
                    }
                ]
            },
        }
    ).model_dump(exclude_none=True)
    profile = {
        "tenant_id": "tenant-example",
        "product_id": "product-example",
        "project_id": "project-example",
        "revision": 1,
        "configuration": configuration,
    }
    selected = object.__new__(l5_integration.GatewayConfigurationBackend)
    selected.profile = AsyncMock(return_value=profile)
    selected.profile_inputs = AsyncMock(return_value=(None, None, {"coverage": "complete", "next_cursor": None, "items": []}))
    options = await selected.options("product-example", "project-example", "finops")
    assert options["readiness"]["status"] == "blocked"
    assert "native_unqualified" in options["unavailable_reasons"]
    assert "source_options_unavailable" not in options["unavailable_reasons"]
    with pytest.raises(HTTPException) as failure:
        await selected.submit("product-example", "project-example", "finops", {}, "allocation-key-example-01")
    assert failure.value.status_code == 409
    assert failure.value.detail == {"code": "native_unqualified"}

"""Actual registration route/store, mocking only authority and provider boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.pr_binding_routes import router
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.workload import WorkloadRefusedError
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationPullRequestBinding
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.pr_identity import PrIdentityError
from src.shared.models.organization import Organization

REPO = "aws-e/adp"
ORG = "registration-tenant"
INSTALLATION = 4242
IDENTITY = PullRequestIdentity(987654321, "PR_provider", REPO, 5293, "a" * 40)
URL = "/internal/v1/agent/self/pull-request"
HEADERS = {"X-Adp-Run-Credential": "verified-run-credential", "X-Adp-Workload-Token": "verified-pod-token"}


async def _story(session, issue):
    flow = OrchestrationFlow(org_id=ORG, slug=f"flow-{issue}", title="Delivery", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        org_id=ORG,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref=f"story-{issue}",
        kind="story",
        state="running",
        title="Implement",
        issue_ref=str(issue),
        attempts=1,
    )
    session.add(node)
    await session.flush()
    run = attempt_run_id(node.id, 1)
    session.add(
        OrchestrationDecision(
            org_id=ORG,
            flow_id=flow.id,
            node_id=node.id,
            kind="node_dispatched",
            actor_id="engine",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps({"run_id": run, "attempt": 1, "repo": REPO, "issue": issue, "pr_binding_required": True}),
        )
    )
    await session.commit()
    return node, run


def _authenticate_story(runtime, node, run):
    caller = SimpleNamespace(invocation_id=run, tenant_id=ORG, principal=f"{run}#1", persona="developer")
    record = SimpleNamespace(invocation_id=run, tenant_id=ORG, repo=REPO, flow_id=node.flow_id)
    grant = SimpleNamespace(authority=SimpleNamespace(kind="gate_decision"), repo_scope={REPO}, flow_id=node.flow_id)
    runtime.authenticate.return_value = (SimpleNamespace(uid="verified-pod"), caller, record, grant)
    runtime.store._read.return_value = {
        "provider_repository_id": {"N": str(IDENTITY.provider_repository_id)},
        "installation_id": {"N": str(INSTALLATION)},
        "persona": {"S": "developer"},
        "repo": {"S": REPO},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": "1"},
    }


@pytest.fixture
async def registration(db_session_factory, monkeypatch):
    async with db_session_factory() as session:
        session.add(Organization(id=ORG, name=ORG, github_installation_ids=[str(INSTALLATION)]))
        node, run = await _story(session, 5049)
    runtime = MagicMock()
    runtime.validate_flow = AsyncMock()
    _authenticate_story(runtime, node, run)
    provider = AsyncMock(return_value=IDENTITY)
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_pr_identity", provider)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    app.dependency_overrides[require_agent_transport] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield SimpleNamespace(client=client, runtime=runtime, provider=provider, sessions=db_session_factory, node=node)


def _body(**changes):
    return {**IDENTITY.__dict__, **changes}


async def _rows(registration):
    async with registration.sessions() as session:
        return list((await session.scalars(select(OrchestrationPullRequestBinding))).all())


@pytest.mark.parametrize("persona", ["developer", "agent-codex-developer"])
async def test_registration_uses_provider_head_and_is_idempotent(registration, persona):
    reg = registration
    reg.runtime.store._read.return_value["persona"] = {"S": persona}
    first = await reg.client.post(URL, headers=HEADERS, json=_body(head_sha="b" * 40))
    retry = await reg.client.post(URL, headers=HEADERS, json=_body())
    assert first.status_code == 201, first.text
    assert retry.status_code == 200, retry.text
    assert first.json()["head_sha"] == IDENTITY.head_sha
    assert retry.json()["created"] is False
    assert len(await _rows(reg)) == 1
    reg.runtime.authenticate.assert_called_with(HEADERS["X-Adp-Run-Credential"], HEADERS["X-Adp-Workload-Token"])
    reg.runtime.validate_flow.assert_awaited()
    reg.provider.assert_awaited_with(org_id=ORG, installation_id=INSTALLATION, repo=REPO, pr_number=IDENTITY.pr_number)


@pytest.mark.parametrize("change", [{"provider_pr_node_id": "PR_forged"}, {"provider_repository_id": 123}, {"repo": "foreign/repo"}])
async def test_forged_provider_identity_cannot_create_binding(registration, change):
    response = await registration.client.post(URL, headers=HEADERS, json=_body(**change))
    assert response.status_code == 409
    assert await _rows(registration) == []


async def test_same_real_pr_cannot_be_registered_to_second_story(registration):
    reg = registration
    assert (await reg.client.post(URL, headers=HEADERS, json=_body())).status_code == 201
    async with reg.sessions() as session:
        second, run = await _story(session, 5050)
    _authenticate_story(reg.runtime, second, run)
    forged = await reg.client.post(URL, headers=HEADERS, json=_body(provider_pr_node_id="PR_forged"))
    actual = await reg.client.post(URL, headers=HEADERS, json=_body())
    assert forged.status_code == actual.status_code == 409
    assert actual.json()["detail"] == "already_bound_elsewhere"
    assert len(await _rows(reg)) == 1


async def test_reused_repo_name_cannot_replace_authorized_repository(registration):
    registration.provider.return_value = replace(IDENTITY, provider_repository_id=123)
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 409
    assert await _rows(registration) == []


@pytest.mark.parametrize("persona", ["reviewer", "agent-codex-reviewer"])
async def test_reviewer_role_is_derived_from_protected_execution(registration, persona):
    # Even a credential's advisory persona / an omitted downgrade cannot promote
    # the persona on the protected execution row.
    registration.runtime.store._read.return_value["persona"] = {"S": persona}
    response = await registration.client.post(URL, headers=HEADERS, json=_body(reviewer_artifact=False))
    assert response.status_code == 201
    assert response.json()["role"] == "reviewer_artifact"
    assert (await _rows(registration))[0].role == "reviewer_artifact"


@pytest.mark.parametrize("error", [CredentialError("invalid"), WorkloadRefusedError("wrong pod"), ExecutionStateError("superseded")])
async def test_invalid_run_or_workload_proof_cannot_register(registration, error):
    registration.runtime.authenticate.side_effect = error
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 404
    registration.provider.assert_not_awaited()
    assert await _rows(registration) == []


async def test_shared_worker_identity_and_run_id_are_not_sufficient(registration):
    response = await registration.client.post(URL, headers={"X-Caller-Identity": "shared-worker", "X-Agent-RunId": "forged"}, json=_body())
    assert response.status_code == 404
    registration.runtime.authenticate.assert_not_called()


async def test_revoked_flow_authority_cannot_register(registration):
    registration.runtime.validate_flow.side_effect = BootstrapRefusedError("flow halted")
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 404
    registration.provider.assert_not_awaited()
    assert await _rows(registration) == []


@pytest.mark.parametrize("field", ["provider_repository_id", "installation_id", "persona", "orchestration_node_id"])
async def test_missing_protected_context_cannot_register(registration, field):
    del registration.runtime.store._read.return_value[field]
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 404
    registration.provider.assert_not_awaited()


async def test_provider_outage_is_visible_and_writes_nothing(registration):
    registration.provider.side_effect = PrIdentityError("unavailable")
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 503
    assert await _rows(registration) == []


async def test_worker_accepts_actual_created_route_response(registration, monkeypatch):
    """The real worker HTTP parser must accept the real route's success status."""
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 201
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "agent-factory" / "agent-worker-image"))
    from botocore.credentials import Credentials
    from lib import pr_binding, status_gateway_client

    monkeypatch.setenv("ADP_PR_BINDING_REQUIRED", "true")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_AGENT_CONTROL_ENDPOINT", "https://gateway.test/internal/v1/agent")
    monkeypatch.setattr(pr_binding, "_pr_identity", lambda *args: _body())
    monkeypatch.setattr(status_gateway_client, "read_workload_token", lambda: "workload-token")
    monkeypatch.setattr(status_gateway_client, "_read_credential", lambda: "adpr1.run.signature")
    monkeypatch.setattr(status_gateway_client.botocore.session, "get_session", MagicMock())
    monkeypatch.setattr("adp_trigger.transport_identity.worker_credentials", lambda _: Credentials("platform-key", "secret", "token"))
    monkeypatch.setattr("adp_trigger.transport_identity.gateway_signing_region", lambda _: "us-east-1")
    wire_response = MagicMock(status_code=response.status_code)
    wire_response.__enter__.return_value = wire_response
    wire_response.raw.read.side_effect = lambda *_args, **_kwargs: BytesIO(response.content).read()
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = wire_response
    monkeypatch.setattr(status_gateway_client.requests, "Session", lambda: http)
    note = pr_binding.binding_note(repo=REPO, pr_number=IDENTITY.pr_number)
    assert "Registered PR" in note
    assert "failed" not in note
    headers = http.post.call_args.kwargs["headers"]
    assert headers["X-Adp-Run-Credential"] == "adpr1.run.signature"
    assert headers["X-Adp-Workload-Token"] == "workload-token"
    assert "Credential=platform-key/" in headers["Authorization"]


@pytest.mark.parametrize("persona", ["architect", "operations", "agent-codex-architect", "unregistered-developer"])
async def test_non_delivery_persona_cannot_register_even_with_developer_credential_hint(registration, persona):
    registration.runtime.store._read.return_value["persona"] = {"S": persona}
    response = await registration.client.post(URL, headers=HEADERS, json=_body())
    assert response.status_code == 404
    assert await _rows(registration) == []
    registration.provider.assert_not_awaited()

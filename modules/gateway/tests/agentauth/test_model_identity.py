"""The actual ASGI boundary preserves bytes and rejects identity substitution."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.agentauth.store import AuthorityStoreError
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.run_binding import RunBindingError
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def context():
    return TokenContext(
        user_id="iam-agent:authority-worker",
        agent_registry_id="authority-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def runtime(monkeypatch):
    caller = SimpleNamespace(tenant_id="tenant", invocation_id="run")
    grant = SimpleNamespace(flow_id="flow", authority=SimpleNamespace(kind="gate_decision", human_id="sub:human"))

    def authenticate(credential, workload):
        if (credential, workload) != ("credential", "pod"):
            raise BootstrapRefusedError("invalid worker")
        return None, caller, None, grant

    runtime = SimpleNamespace(authenticate=authenticate, validate_flow=AsyncMock(), store=SimpleNamespace(_read=lambda *_: {}))

    class SessionContext:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.orchestration.work_admission.worker_checkpoint", AsyncMock())
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: SessionContext)
    monkeypatch.setattr("src.shared.identity.resolver.resolve_root_user_entity_id", AsyncMock(return_value="canonical-human"))
    from src.orchestration.execution_policy import Decision
    from src.orchestration.policy_admission import AdmissionInputs

    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(None, 0)))
    monkeypatch.setattr("src.orchestration.runtime_policy.authorize_worker_credential", AsyncMock(return_value=Decision.permit()))
    return runtime


async def call(context, headers, *, path="/v1/messages"):
    body = b'{  "message": "literal \\n bytes"  }'
    frames = [{"type": "http.request", "body": body[:12], "more_body": True}, {"type": "http.request", "body": body[12:], "more_body": False}]
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "state": {"token_context": context}}
    sent, consumed = [], []

    async def receive():
        frame = frames.pop(0)
        consumed.append(frame)
        return frame

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        assert consumed == []
        assert await Request(scope, receive).body() == body
        await Response(body)(scope, receive, send)

    await AgentModelIdentityMiddleware(app)(scope, receive, send)
    return sent, consumed


PROOF = [(b"x-adp-run-credential", b"credential"), (b"x-adp-workload-token", b"pod")]


async def test_identity_and_flow_derive_from_protected_run_and_preserve_raw_body(context, runtime):
    sent, consumed = await call(context, PROOF)
    assert sent[0]["status"] == 200
    assert len(consumed) == 2
    binding = context._protected_run_binding
    assert (binding.run_id, binding.flow_id, binding.tenant_id, binding.root_human_id) == ("run", "flow", "tenant", "canonical-human")
    assert context.org_id == "__platform__"
    assert context.attributed_org_id == "tenant"
    assert "_protected_run_binding" not in context.model_dump()


@pytest.mark.parametrize(
    "headers", [[], [(b"x-adp-run-credential", b"credential")], PROOF + [(b"x-agent-runid", b"sibling")], PROOF + [(b"x-agent-orgid", b"other")]]
)
async def test_missing_proof_and_forged_attribution_never_reach_model(context, runtime, headers):
    sent, consumed = await call(context, headers)
    assert sent[0]["status"] == 403
    assert not consumed
    assert context._protected_run_binding is None


async def test_unavailable_authority_has_no_legacy_fallback(context, runtime):
    runtime.validate_flow.side_effect = AuthorityStoreError("unavailable")
    sent, consumed = await call(context, PROOF)
    assert sent[0]["status"] == 503
    assert not consumed


@pytest.mark.parametrize("reason", ["stale_policy_version", "schema_unsupported"])
async def test_authoring_policy_refusal_precedes_body_consumption(context, runtime, monkeypatch, reason):
    from src.orchestration.execution_policy import Decision, DenyReason
    from src.orchestration.policy_admission import AdmissionInputs

    runtime.authenticate("credential", "pod")[3].authority.kind = "replan_request"
    monkeypatch.setattr(
        "src.orchestration.policy_admission.load_in_force_policy",
        AsyncMock(return_value=AdmissionInputs(None, 2, Decision.block(DenyReason(reason), "accepted policy unavailable"))),
    )
    sent, consumed = await call(context, PROOF)
    assert sent[0]["status"] == 403
    assert not consumed


async def test_legacy_worker_and_control_routes_keep_existing_auth(context, runtime):
    context.user_id = "iam-agent:scaledjob-worker"
    context.agent_registry_id = "scaledjob-worker"
    assert (await call(context, []))[0][0]["status"] == 200
    context.user_id = "iam-agent:authority-worker"
    context.agent_registry_id = "authority-worker"
    assert (await call(context, [], path="/internal/v1/agent/status"))[0][0]["status"] == 200
    runtime.validate_flow.assert_not_awaited()


@pytest.mark.parametrize("enabled,mode", [(False, "shadow"), (True, "shadow"), (True, "enforce")])
async def test_budget_cannot_replace_protected_binding_in_any_legacy_mode(context, runtime, monkeypatch, enabled, mode):
    from src.budget.config import budget_config

    monkeypatch.setattr(budget_config, "budget_run_cap_enabled", enabled)
    monkeypatch.setattr(budget_config, "budget_run_binding_mode", mode)
    service = BudgetEnforcementService()
    with pytest.raises(RunBindingError):
        await service._resolve_run_scope(context, "guessed")
    await call(context, PROOF)
    assert await service._resolve_run_scope(context, None) is context._protected_run_binding
    with pytest.raises(RunBindingError):
        await service._resolve_run_scope(context, "sibling")


async def test_paid_domain_worker_has_no_model_authority(context, runtime):
    runtime.authenticate("credential", "pod")[3].authority.kind = "paid_domain_operation"
    sent, consumed = await call(context, PROOF)
    assert sent[0]["status"] == 403
    assert consumed == []
    assert context._protected_run_binding is None

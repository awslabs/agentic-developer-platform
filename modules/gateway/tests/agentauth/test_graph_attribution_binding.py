"""Graph attribution is attached only from verified server state (#4898).

Companion to `test_model_identity.py`, which owns the identity/authorization
boundary. These tests cover what the middleware does with the graph assignment
`validate_engine_authority` returns: it must reach the usage writer for a genuinely
assigned node, must be absent for everything else, and must never be selectable or
influenceable by the caller.

The distinction that matters throughout: attribution is REPORTING. It must never
change an authorization outcome — a request that would have been served must still
be served when attribution is missing, mismatched or malformed, just with a NULL
address.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.orchestration.dispatch import GraphAttribution
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService

ADDRESS = "delivery-loop/epic-1/wave-2/story-3"
PROOF = [(b"x-adp-run-credential", b"credential"), (b"x-adp-workload-token", b"pod")]


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
    """The protected worker runtime, authenticating as tenant `tenant` / run `run`."""
    caller = SimpleNamespace(tenant_id="tenant", invocation_id="run")
    grant = SimpleNamespace(flow_id="flow", authority=SimpleNamespace(kind="gate_decision", human_id="sub:human"))

    def authenticate(credential, workload):
        if (credential, workload) != ("credential", "pod"):
            raise BootstrapRefusedError("invalid worker")
        return None, caller, None, grant

    runtime = SimpleNamespace(authenticate=authenticate, validate_flow=AsyncMock(return_value=None), store=SimpleNamespace(_read=lambda *_: {}))

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


async def call(context, headers=PROOF, *, path="/v1/messages"):
    body = b'{"message": "hello"}'
    frames = [{"type": "http.request", "body": body, "more_body": False}]
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "state": {"token_context": context}}
    sent = []

    async def receive():
        return frames.pop(0)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        await Request(scope, receive).body()
        await Response(b"ok")(scope, receive, send)

    await AgentModelIdentityMiddleware(app)(scope, receive, send)
    return sent


def _assignment(**overrides) -> GraphAttribution:
    fields = dict(org_id="tenant", flow_id="flow", node_id="node", node_attempt=1, address=ADDRESS, run_id="run")
    fields.update(overrides)
    return GraphAttribution(**fields)


async def test_assigned_node_reaches_the_usage_writer(context, runtime):
    """A developer/reviewer bound to a node bills to that node."""
    runtime.validate_flow.return_value = _assignment()
    assert (await call(context))[0]["status"] == 200
    assert context._graph_attribution == _assignment()
    assert UsageService._graph_address_for(context, "run") == ADDRESS


async def test_unassigned_coordinator_is_not_charged_to_a_child_node(context, runtime):
    """`validate_engine_authority` returns None for a coordinator, and it stays None.

    A flow-level or wave coordinator owns no single graph node. Charging it to one
    of its children would invent a number, so it must remain unattributed — while
    the call itself proceeds and is still metered.
    """
    runtime.validate_flow.return_value = None
    assert (await call(context))[0]["status"] == 200
    assert context._graph_attribution is None
    assert UsageService._graph_address_for(context, "run") is None


@pytest.mark.parametrize(
    "header",
    [
        (b"x-graph-address", b"victim-flow/epic-1/wave-1/node-1"),
        (b"x-agent-graph-address", b"victim-flow/epic-1/wave-1/node-1"),
        (b"x-adp-graph-address", b"victim-flow/epic-1/wave-1/node-1"),
    ],
)
async def test_a_caller_supplied_address_header_is_ignored(context, runtime, header):
    """No header can select the persisted address — it is not read at all.

    The address comes from server state only. These headers do not exist in the
    implementation, and this test is what keeps it that way: if someone later
    wired one in, the asserted address would change.
    """
    runtime.validate_flow.return_value = _assignment()
    assert (await call(context, PROOF + [header]))[0]["status"] == 200
    assert UsageService._graph_address_for(context, "run") == ADDRESS


@pytest.mark.parametrize("headers", [PROOF + [(b"x-agent-runid", b"sibling")], PROOF + [(b"x-agent-orgid", b"other")], []])
async def test_forged_or_missing_identity_yields_no_attribution(context, runtime, headers):
    """Existing refusals are unchanged, and a refused call attributes nothing.

    Asserting both together matters: a refusal path that still wrote attribution
    would let a rejected caller pollute another node's cost.
    """
    runtime.validate_flow.return_value = _assignment()
    assert (await call(context, headers))[0]["status"] == 403
    assert context._graph_attribution is None


@pytest.mark.parametrize(
    "assignment",
    [
        _assignment(org_id="other-tenant"),
        _assignment(run_id="another-run"),
        _assignment(address=""),
    ],
)
async def test_assignment_disagreeing_with_the_caller_is_declined(context, runtime, assignment):
    """Cross-tenant, cross-run and empty assignments are not attached.

    The middleware requires the assignment to agree with the authenticated caller.
    Agreement is expected — both come from the same grant — so this is a binding
    assertion that keeps any future path from cross-attributing spend. It declines
    attribution WITHOUT denying the call: attribution must not be able to break a
    model request.
    """
    runtime.validate_flow.return_value = assignment
    assert (await call(context))[0]["status"] == 200
    assert context._graph_attribution is None
    assert UsageService._graph_address_for(context, "run") is None


async def test_ordinary_traffic_is_never_attributed(context, runtime):
    """Legacy-worker and control-plane paths keep their existing auth and stay NULL.

    Human, CLI and chat calls never enter this middleware's protected branch at
    all, so their address is NULL by construction rather than by a policy a caller
    could ignore.
    """
    context.user_id = "iam-agent:scaledjob-worker"
    context.agent_registry_id = "scaledjob-worker"
    assert (await call(context, []))[0]["status"] == 200
    assert context._graph_attribution is None
    runtime.validate_flow.assert_not_awaited()


async def test_attribution_never_appears_in_a_serialized_context(context, runtime):
    """No API response, log line or serialized context changes shape."""
    runtime.validate_flow.return_value = _assignment()
    await call(context)
    dumped = context.model_dump()
    assert "_graph_attribution" not in dumped
    assert ADDRESS not in str(dumped)

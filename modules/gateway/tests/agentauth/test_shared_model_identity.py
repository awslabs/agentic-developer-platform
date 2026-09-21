"""Shared-run report authentication feeds the existing quote/reservation path."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from starlette.requests import Request

from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.budget.config import budget_config
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.run_binding import RunBindingError
from src.orchestration.flow_meter import read_flow_meter
from src.orchestration.models import OrchestrationAcceptedPlan
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import prepare_run_report
from src.shared.schemas.auth import TokenContext
from tests.orchestration import test_policy_model_request as fixtures

engine, session, assignment = fixtures.engine, fixtures.session, fixtures.assignment
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers
model_path = fixtures.model_path


@pytest.fixture
async def shared(session, assignment, model_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "test-report-key")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    assignment.flow.state = "running"
    envelope = {
        "actor": {"user_id": model_path.policy.principal_id, "org_id": assignment.node.org_id},
        "message_id": "shared-worker",
        "tenant_id": assignment.node.org_id,
        "persona": "developer",
        "orchestration": {"node_id": assignment.node.id, "flow_id": assignment.flow.id, "attempt": 1},
        "source_ref": {"repo": "aws-e/adp", "installation_id": 1, "provider_repository_id": 42},
    }
    row = await prepare_run_report(session, envelope)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.plan_document = {**plan.plan_document, "execution_continuation": {"contract_version": 1, "mode": "shared_worker_role"}}
    await session.flush()

    async def authorize(db, report):
        if report.terminal_receipt:
            raise CycleBlockedError("run_already_finished")
        if "execution_continuation" not in plan.plan_document:
            raise CycleBlockedError("shared_worker_continuation_not_accepted")
        inputs = await load_in_force_policy(db, org_id=report.org_id, flow_id=report.flow_id)
        if inputs.refusal or not inputs.policy:
            raise CycleBlockedError("policy_unavailable")
        return inputs.policy, inputs.policy.principal_id, assignment.node, assignment.flow

    authorizer = AsyncMock(side_effect=authorize)
    monkeypatch.setattr("src.orchestration.shared_policy.authorize_shared_model", authorizer)
    return SimpleNamespace(row=row, credential=envelope["run_report"]["credential"], plan=plan, authorizer=authorizer)


def context():
    return TokenContext(
        user_id="scaledjob-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def invoke(model_path, shared, *, headers=None, during_upload=None, enforce=True, usage_known=True):
    raw = b'{ "model": "anthropic.claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role":"user","content":"hello"}] }'
    token = context()
    frames = [{"type": "http.request", "body": raw[:13], "more_body": True}, {"type": "http.request", "body": raw[13:], "more_body": False}]
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("gateway.test", 443),
        "query_string": b"",
        "path": "/v1/messages",
        "state": {"token_context": token},
        "headers": [(b"content-type", b"application/json")]
        + ([(b"x-adp-report-credential", shared.credential.encode())] if headers is None else headers),
    }
    sent, consumed = [], []

    async def receive():
        if during_upload:
            await during_upload()
        frame = frames.pop(0)
        consumed.append(frame)
        return frame

    async def send(frame):
        sent.append(frame)

    async def provider(scope, receive, send):
        assert b"x-adp-report-credential" not in dict(scope["headers"])
        assert shared.credential not in str(token.model_dump())
        model_path.calls += 1
        model_path.bodies.append(await Request(scope, receive).body())
        model_path.request_ids.append(token._policy_request_id)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"stream-one", "more_body": True})
        if enforce:
            await model_path.service.reconcile_reservation(
                token,
                token._policy_request_id,
                "anthropic.claude-sonnet-4-6",
                5,
                1,
                actual_cost_usd=Decimal("0.01"),
                usage_known=usage_known,
            )
        await send({"type": "http.response.body", "body": b"stream-two", "more_body": False})

    downstream = BudgetEnforcementMiddleware(provider, model_path.service) if enforce else provider
    await AgentModelIdentityMiddleware(downstream)(scope, receive, send)
    return SimpleNamespace(sent=sent, token=token, raw=raw, consumed=consumed)


async def test_shared_request_uses_real_quote_budget_and_exact_body_and_stream(model_path, shared, assignment):
    result = await invoke(model_path, shared)
    assert result.sent[0]["status"] == 200
    assert model_path.bodies == [result.raw]
    assert [item["body"] for item in result.sent[1:]] == [b"stream-one", b"stream-two"]
    assert result.token.org_id == "__platform__" and result.token.attributed_org_id == assignment.node.org_id
    assert result.token._protected_run_binding.run_id == shared.row.run_id
    assert result.token._graph_attribution.node_id == assignment.node.id
    snapshot = await read_flow_meter(org_id=assignment.node.org_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert snapshot.total_usd == Decimal("0.01") and not snapshot.has_pending
    assert shared.authorizer.await_count == 2


@pytest.mark.parametrize("invalid", ["garbage", "adprpt1." + "a" * 43, "", "expired", "stale"])
async def test_invalid_missing_expired_and_stale_proof_never_falls_back(model_path, shared, session, invalid):
    credential = shared.credential
    if invalid == "expired":
        shared.row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "stale":
        from src.orchestration.models import OrchestrationNode

        (await session.get(OrchestrationNode, shared.row.node_id)).attempts = 2
    elif invalid != "":
        credential = invalid
    await session.flush()
    headers = [(b"x-agent-runid", shared.row.run_id.encode())]
    if invalid != "":
        headers.append((b"x-adp-report-credential", credential.encode()))
    result = await invoke(model_path, shared, headers=headers)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and result.consumed == []


@pytest.mark.parametrize("name,value", [(b"x-agent-runid", b"other-run"), (b"x-agent-orgid", b"other-org")])
async def test_asserted_run_and_tenant_cannot_replace_authenticated_assignment(model_path, shared, name, value):
    result = await invoke(model_path, shared, headers=[(b"x-adp-report-credential", shared.credential.encode()), (name, value)])
    assert result.sent[0]["status"] == 403 and model_path.calls == 0


async def test_unrelated_tokenless_legacy_traffic_remains_legacy(model_path, shared):
    for headers in ([], [(b"x-agent-runid", b"unrelated-legacy-run")]):
        result = await invoke(model_path, shared, headers=headers, enforce=False)
        assert result.sent[0]["status"] == 200 and result.token._protected_run_binding is None
    assert shared.authorizer.await_count == 0


@pytest.mark.parametrize("malformed", [False, True])
async def test_tokenless_known_policy_assignment_without_shared_marker_is_denied(model_path, shared, session, malformed):
    shared.plan.plan_document = {key: value for key, value in shared.plan.plan_document.items() if key != "execution_continuation"}
    if malformed:
        shared.plan.plan_document = {**shared.plan.plan_document, "execution_policy": {"unrecognized": True}}
    await session.flush()
    result = await invoke(model_path, shared, headers=[(b"x-agent-runid", shared.row.run_id.encode())], enforce=False)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and shared.authorizer.await_count == 0


async def test_tokenless_known_policyless_legacy_assignment_stays_legacy(model_path, shared, session):
    shared.plan.plan_document = {
        key: value for key, value in shared.plan.plan_document.items() if key not in {"execution_policy", "execution_continuation"}
    }
    await session.flush()
    result = await invoke(model_path, shared, headers=[(b"x-agent-runid", shared.row.run_id.encode())], enforce=False)
    assert result.sent[0]["status"] == 200 and shared.authorizer.await_count == 0


@pytest.mark.parametrize("with_credential", [False, True])
@pytest.mark.parametrize("version", [None, 1, 99])
async def test_shared_marker_without_policy_never_downgrades_to_legacy(model_path, shared, session, with_credential, version):
    shared.plan.plan_document = {
        **{key: value for key, value in shared.plan.plan_document.items() if key != "execution_policy"},
        "execution_continuation": {"mode": "shared_worker_role", "contract_version": version},
    }
    await session.flush()
    headers = [(b"x-agent-runid", shared.row.run_id.encode())]
    if with_credential:
        headers.append((b"x-adp-report-credential", shared.credential.encode()))
    result = await invoke(model_path, shared, headers=headers, enforce=False)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0


@pytest.mark.parametrize("change", ["terminal", "policy", "budget-disabled"])
async def test_upload_cannot_keep_old_assignment_or_policy_authority(model_path, shared, session, monkeypatch, change):
    async def mutate():
        if change == "terminal":
            shared.row.terminal_receipt = {"outcome": "complete"}
        elif change == "policy":
            policy = dict(shared.plan.plan_document["execution_policy"])
            policy["policy_id"] = "changed-policy"
            shared.plan.plan_document = {**shared.plan.plan_document, "execution_policy": policy}
        else:
            monkeypatch.setenv("BUDGET_ENFORCEMENT_ENABLED", "false")
        await session.flush()

    result = await invoke(model_path, shared, during_upload=mutate)
    assert result.sent[0]["status"] in {403, 503} and model_path.calls == 0
    assert result.token._policy_flow_target is None


async def test_same_flow_budget_covers_retries_with_legacy_caps_disabled(model_path, shared, assignment, monkeypatch):
    monkeypatch.setattr(budget_config, "budget_run_cap_enabled", False)
    monkeypatch.setattr(budget_config, "budget_run_binding_mode", "shadow")
    first = await invoke(model_path, shared)
    assert first.sent[0]["status"] == 200
    target = first.token._policy_flow_target
    assert (
        await model_path.service._reservations.reserve("prior-spend", model_path.policy.limits.max_spend_usd - Decimal("0.011"), [target])
    ).admitted
    pending = await invoke(model_path, shared)
    assert pending.sent[0]["status"] == 429 and model_path.calls == 1
    await model_path.service._reservations.reconcile("prior-spend", model_path.policy.limits.max_spend_usd - Decimal("0.011"), [target])
    denied = await invoke(model_path, shared)
    assert denied.sent[0]["status"] == 402 and model_path.calls == 1
    assert denied.token._policy_request_id != first.token._policy_request_id
    assert await model_path.service._resolve_run_scope(first.token, None) is first.token._protected_run_binding
    with pytest.raises(RunBindingError):
        await model_path.service._resolve_run_scope(first.token, "different-run")


async def test_missing_usage_keeps_flow_spend_unknown(model_path, shared):
    assert (await invoke(model_path, shared, usage_known=False)).sent[0]["status"] == 200
    assert (await invoke(model_path, shared)).sent[0]["status"] == 503
    assert model_path.calls == 1


async def test_valid_legacy_report_keeps_models_and_run_attribution_without_flow_policy(model_path, shared, session):
    shared.plan.plan_document = {
        key: value for key, value in shared.plan.plan_document.items() if key not in {"execution_policy", "execution_continuation"}
    }
    await session.flush()
    result = await invoke(model_path, shared, enforce=False)
    assert result.sent[0]["status"] == 200 and model_path.bodies == [result.raw]
    assert result.token._protected_run_binding.run_id == shared.row.run_id
    assert result.token._protected_run_binding.user_id == shared.row.dispatch_metadata["actor"]["user_id"]
    assert result.token._policy_flow_target is None and result.token._policy_quote is None
    assert shared.authorizer.await_count == 0


@pytest.mark.parametrize("terminal", [True, False])
async def test_legacy_report_never_authorizes_models_after_worker_or_story_finishes(model_path, shared, assignment, session, terminal):
    shared.plan.plan_document = {
        key: value for key, value in shared.plan.plan_document.items() if key not in {"execution_policy", "execution_continuation"}
    }
    if terminal:
        shared.row.terminal_receipt = {"outcome": "complete"}
    else:
        assignment.node.state = "passed"
    await session.flush()
    result = await invoke(model_path, shared, enforce=False)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and not result.consumed


async def test_present_policy_without_shared_acceptance_cannot_downgrade_to_legacy(model_path, shared, session):
    shared.plan.plan_document = {key: value for key, value in shared.plan.plan_document.items() if key != "execution_continuation"}
    await session.flush()
    result = await invoke(model_path, shared)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and shared.authorizer.await_count == 1


async def test_quote_revalidation_failure_does_not_reserve_or_reach_provider(model_path, shared, monkeypatch):
    from src.orchestration.provider_quotes import Capability, QuoteReason, QuoteRefusal, QuoteRefusedError

    monkeypatch.setattr(
        "src.agentauth.model_identity.revalidate_quote",
        AsyncMock(
            side_effect=QuoteRefusedError(
                QuoteRefusal(reason=QuoteReason.REQUEST_CHANGED, capability=Capability.TEXT),
            )
        ),
    )
    result = await invoke(model_path, shared)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0
    assert result.token._policy_flow_target is None and result.token._protected_run_binding is None


async def test_malformed_policy_never_becomes_legacy_model_traffic(model_path, shared, session):
    shared.plan.plan_document = {**shared.plan.plan_document, "execution_policy": {"unrecognized": True}}
    await session.flush()
    result = await invoke(model_path, shared)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and shared.authorizer.await_count == 1


async def test_missing_proof_with_assignment_lookup_outage_does_not_fall_back(model_path, shared, monkeypatch):
    monkeypatch.setattr("src.agentauth.model_identity._known_shared_model_run", AsyncMock(side_effect=RuntimeError("database unavailable")))
    result = await invoke(model_path, shared, headers=[(b"x-agent-runid", shared.row.run_id.encode())], enforce=False)
    assert result.sent[0]["status"] == 503 and model_path.calls == 0 and not result.consumed


async def test_explicit_empty_proof_is_invalid_even_without_a_run_assertion(model_path, shared):
    result = await invoke(model_path, shared, headers=[(b"x-adp-report-credential", b"")], enforce=False)
    assert result.sent[0]["status"] == 403 and model_path.calls == 0 and not result.consumed


@pytest.mark.parametrize("tenant_limit", [None, "0.01"])
@pytest.mark.parametrize("during_upload", [False, True])
async def test_platform_budget_increase_reaches_model_path_and_preserves_tenant_limit(
    model_path, shared, session, assignment, tenant_limit, during_upload
):
    import json

    from src.orchestration.continuation import digest
    from src.orchestration.models import OrchestrationDecision
    from src.orchestration.shared_budget import CONTRACT
    from src.shared.models.budget import BudgetConfig as TenantBudget

    shared.plan.plan_hash = digest(shared.plan.plan_document)
    await session.flush()
    if tenant_limit is not None:
        session.add(
            TenantBudget(org_id=assignment.node.org_id, entity_type="run", entity_id="*", period_type="run", budget_amount_usd=Decimal(tenant_limit))
        )
        await session.flush()
    original_document = json.loads(json.dumps(shared.plan.plan_document))
    # Existing settled usage is retained across the new financial approval.
    first = await invoke(model_path, shared)
    if tenant_limit is None:
        assert first.sent[0]["status"] == 200
        from src.orchestration.flow_meter import meter_target

        targets = [meter_target(org_id=assignment.node.org_id, flow_id=assignment.flow.id, policy=model_path.policy)]
        targets.extend(model_path.service._scope_targets(first.token._protected_run_binding, Decimal("100"), Decimal("1000")))
        await model_path.service._reservations.reserve("historical-call", Decimal("25"), targets)
        await model_path.service._reservations.reconcile("historical-call", Decimal("25"), targets)
        assert (await invoke(model_path, shared)).sent[0]["status"] == 402
    else:
        assert first.sent[0]["status"] == 402
    added = False

    async def increase():
        nonlocal added
        if added:
            return
        added = True
        session.add(
            OrchestrationDecision(
                org_id=shared.plan.org_id,
                flow_id=shared.plan.flow_id,
                kind="budget_increased",
                actor_id="platform-approver",
                actor_kind="human",
                actor_role="platform_admin",
                reason=json.dumps(
                    {
                        "contract": CONTRACT,
                        "plan_version": shared.plan.version,
                        "plan_hash": shared.plan.plan_hash,
                        "original_policy_hash": model_path.policy.policy_hash,
                        "principal_id": model_path.policy.principal_id,
                        "limits": {"max_spend_usd": "1000", "max_run_spend_usd": "100", "max_chain_spend_usd": "1000"},
                    }
                ),
            )
        )
        await session.flush()

    if not during_upload:
        await increase()
    result = await invoke(model_path, shared, during_upload=increase if during_upload else None)
    assert result.sent[0]["status"] == (200 if tenant_limit is None else 402)
    assert result.token._policy_scope_caps == (Decimal("100"), Decimal("1000"))
    assert shared.plan.plan_document == original_document
    meter = await read_flow_meter(org_id=assignment.node.org_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == (Decimal("25.02") if tenant_limit is None else Decimal("0"))
    assert not meter.has_pending

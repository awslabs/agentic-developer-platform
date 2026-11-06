"""Materialized AI-DLC handoffs through real HTTP, SQL and protected DDB."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select, update

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.grants import TargetRelationship
from src.agentauth.waves import load_issue_wave, wave_key
from src.agentauth.workload import WORKLOAD_HEADER, VerifiedPod
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from tests.agentauth.test_graph_dispatch import (  # noqa: F401
    child_dispatch,  # noqa: F401
    engine,
    enroll,
    messages,
    session,
    session_factory,
    store,
)
from tests.agentauth.test_graph_dispatch import (
    graph_context as graph_context_fixture,
)
from tests.orchestration.test_dispatch_pass import _make_approval, _make_node

graph_context = graph_context_fixture


@pytest.fixture
async def wave_context(graph_context, monkeypatch):
    ctx = graph_context
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(epic_ref="epic-4191", wave_ref="wave-1"))
        evaluation = await _make_node(sql, ctx.flow, node_ref="eval", kind="eval", issue_ref=None, state="pending")
        evaluation.epic_ref, evaluation.wave_ref = "epic-4191", "wave-1"
        await sql.commit()
    ctx.evaluation_id = evaluation.id
    ctx.binding = {"repo": "org/repo", "epic_ref": "epic-4191", "wave_ref": "wave-1", "orchestrator_issue": 44, "evaluation_issue": 45}
    ctx.verify_issues = AsyncMock(return_value=987)
    monkeypatch.setattr("src.agentauth.waves.verify_materialized_issues", ctx.verify_issues)
    await enroll(ctx)
    return ctx


@pytest.fixture
async def legacy_wave_context(wave_context):
    ctx = wave_context
    async with ctx.session_factory() as sql:
        node = await sql.get(OrchestrationNode, ctx.node.id)
        node.repository_identity = None
        node.provider_repository_id = None
        node.repository_connection = None
        plan = await sql.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == ctx.flow.id))
        plan.plan_document = {**plan.plan_document, "nodes": []}
        await sql.commit()
    ctx.verify_issues.return_value = None
    with patch("src.agentauth.waves.wave_repository", new=AsyncMock(return_value=(None, 123))):
        assert (await bind(ctx)).status_code == 200
    return ctx


async def bind(ctx, **changes):
    return await ctx.client.post("/internal/v1/agent/waves", json={**ctx.binding, **changes}, headers=ctx.headers)


async def dispatch(ctx, persona="operations", issue=44, request_id="wave-start", headers=None):
    return await ctx.client.post(
        "/internal/v1/agent/dispatch",
        headers=headers or ctx.headers,
        json={
            "persona": persona,
            "target": {"repo": "org/repo", "issue": issue},
            "request_id": request_id,
        },
    )


async def bootstrap_child(ctx, invocation):
    row = ctx.store._read("TENANT#tenant", f"EXEC#{invocation}")
    original = ctx.runtime.workloads.verify
    proof = "proof-" + invocation
    ctx.runtime.workloads.verify = lambda token: (
        VerifiedPod(proof, "worker", "adp-agents", "agent-scaledjob-sa", "10.0.1.3") if token == proof else original(token)
    )
    headers = {"X-Caller-Identity": "shared-role", WORKLOAD_HEADER: proof}
    response = await ctx.client.post(
        "/internal/v1/agent/bootstrap",
        headers=headers,
        json={
            "invocation_id": invocation,
            "envelope_digest": row["envelope_digest"]["S"],
        },
    )
    if response.status_code == 200:
        headers["X-Adp-Run-Credential"] = response.json()["credential"]
    return response, headers


async def start_wave(ctx):
    assert (await bind(ctx)).status_code == 200
    launch = await dispatch(ctx)
    assert launch.status_code == 202, launch.text
    boot, headers = await bootstrap_child(ctx, launch.json()["invocation_id"])
    assert boot.status_code == 200, boot.text
    return launch, headers


@pytest.fixture
async def qualified_wave_context(wave_context):
    ctx = wave_context
    async with ctx.session_factory() as sql:
        first = await sql.get(OrchestrationNode, ctx.node.id)
        second = await _make_node(sql, ctx.flow, node_ref="story-b", issue_ref="43")
        second.epic_ref, second.wave_ref = "epic-4191", "wave-2"
        evaluation = await _make_node(sql, ctx.flow, node_ref="eval-b", kind="eval", issue_ref=None, state="pending")
        evaluation.epic_ref, evaluation.wave_ref = "epic-4191", "wave-2"
        third = await _make_node(sql, ctx.flow, node_ref="story-a-later", issue_ref="47")
        third.epic_ref, third.wave_ref = "epic-4191", "wave-3"
        last_evaluation = await _make_node(sql, ctx.flow, node_ref="eval-a-later", kind="eval", issue_ref=None, state="pending")
        last_evaluation.epic_ref, last_evaluation.wave_ref = "epic-4191", "wave-3"
        accepted = []
        for node, name, repository_id, installation_id in (
            (first, "example/a", 1001, 123),
            (second, "example/b", 1002, 124),
            (third, "example/a", 1001, 123),
        ):
            node.repository_identity = name
            node.provider_repository_id = repository_id
            node.repository_connection = {"provider": "github", "installation_id": installation_id}
            accepted.append(
                {
                    "address": f"{ctx.flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}",
                    "kind": "story",
                    "title": node.title,
                    "issue_ref": node.issue_ref,
                    "repository_identity": name,
                    "provider_repository_id": repository_id,
                    "repository_connection": node.repository_connection,
                }
            )
        plan = await sql.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == ctx.flow.id, OrchestrationAcceptedPlan.superseded_at.is_(None)
            )
        )
        plan.plan_document = {**plan.plan_document, "nodes": accepted}
        await sql.commit()
    ctx.store.client.update_item(
        TableName=ctx.store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{ctx.child.invocation}#1"}},
        UpdateExpression="SET repo_scope = :scope",
        ExpressionAttributeValues={":scope": {"SS": ["org/repo", "example/a", "example/b"]}},
    )
    ctx.verify_issues.side_effect = lambda **kwargs: {"example/a": 1001, "example/b": 1002}[kwargs["body"].repo]
    return ctx


async def test_repository_qualified_wave_aliases_coexist_and_resolve(qualified_wave_context):
    ctx = qualified_wave_context
    first = await bind(ctx, repo="example/a")
    second = await bind(ctx, repo="example/b", wave_ref="wave-2")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    async with ctx.session_factory() as sql:
        for name, repository_id, wave_ref in (("example/a", 1001, "wave-1"), ("example/b", 1002, "wave-2")):
            for issue in (44, 45):
                mapping, role, nodes, evaluation = await load_issue_wave(
                    service=ctx.child.service,
                    session=sql,
                    tenant_id="tenant",
                    flow_id=ctx.flow.id,
                    repo=name,
                    issue=issue,
                )
                assert mapping["repo"] == name
                assert mapping["provider_repository_id"] == repository_id
                assert mapping["installation_id"] == (123 if repository_id == 1001 else 124)
                assert mapping["wave_ref"] == wave_ref
                assert mapping[role + "_issue"] == issue
                assert len(nodes) == 2
                assert evaluation.id == mapping["evaluation_node_id"]
                assert ctx.store._read("TENANT#tenant", f"WAVEISSUE#{repository_id}#{issue}") is not None
    assert ctx.verify_issues.await_count == 2


async def test_unbound_wave_refuses_even_when_global_repository_is_present(wave_context):
    ctx = wave_context
    ctx.runtime.env["BG_ORCH_DISPATCH_REPO"] = "org/repo"
    async with ctx.session_factory() as sql:
        node = await sql.get(OrchestrationNode, ctx.node.id)
        node.repository_identity = None
        node.provider_repository_id = None
        node.repository_connection = None
        await sql.commit()
    assert (await bind(ctx)).status_code == 404
    assert ctx.store._read("TENANT#tenant", wave_key(ctx.flow.id, "epic-4191", "wave-1")) is None


async def test_qualified_wave_launch_and_bootstrap_use_control_repository(qualified_wave_context):
    ctx = qualified_wave_context
    assert (await bind(ctx, repo="example/a")).status_code == 200
    assert (await bind(ctx, repo="example/b", wave_ref="wave-2")).status_code == 200

    for index, (name, repository_id, installation_id, wave_ref) in enumerate(
        (("example/a", 1001, 123, "wave-1"), ("example/b", 1002, 124, "wave-2"))
    ):
        body = {
            "persona": "operations",
            "target": {"repo": name, "issue": 44},
            "request_id": f"qualified-control-{index}",
        }
        response = await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)
        assert response.status_code == 202, response.text
        invocation_id = response.json()["invocation_id"]
        execution = ctx.store._read("TENANT#tenant", f"EXEC#{invocation_id}")
        assert execution["repo"] == {"S": name}
        assert execution["provider_repository_id"] == {"N": str(repository_id)}
        assert execution["installation_id"] == {"N": str(installation_id)}
        assert execution["wave_key"] == {"S": wave_key(ctx.flow.id, "epic-4191", wave_ref)}
        child_grant = ctx.store._read("TENANT#tenant", f"GRANT#{invocation_id}#1")
        assert child_grant["repo_scope"]["SS"] == [name]
        assert child_grant["flow_id"] == {"S": ctx.flow.id}
        assert (await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)).json() == response.json()
        boot, _ = await bootstrap_child(ctx, invocation_id)
        assert boot.status_code == 200, boot.text


async def test_cross_repository_story_predecessor_gate_and_replay(qualified_wave_context):
    ctx = qualified_wave_context
    async with ctx.session_factory() as sql:
        predecessor = await sql.get(OrchestrationNode, ctx.node.id)
        successor = await sql.scalar(select(OrchestrationNode).where(OrchestrationNode.repository_identity == "example/b"))
        gate = await _make_node(sql, ctx.flow, node_ref="cross-repo-gate", kind="gate", issue_ref=None, state="awaiting_gate")
        gate.epic_ref, gate.wave_ref = "epic-4191", "wave-2"
        sql.add_all(
            [
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=predecessor.id, to_node_id=successor.id),
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=gate.id, to_node_id=successor.id),
            ]
        )
        await sql.commit()

    body = {"persona": "developer", "target": {"repo": "example/b", "issue": 43}, "request_id": "cross-repo-successor"}
    reservation = envelope_digest({"principal": f"{ctx.child.invocation}#1", "request_id": body["request_id"]})
    for release_predecessor in (True, False):
        response = await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)
        assert response.status_code == 404, response.text
        assert messages(ctx) == []
        assert ctx.store._read("TENANT#tenant", f"DISPATCH#{reservation}") is None
        async with ctx.session_factory() as sql:
            successor = await sql.get(OrchestrationNode, successor.id)
            assert (successor.state, successor.attempts) == ("ready", 0)
            if release_predecessor:
                await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == predecessor.id).values(state="passed"))
                await sql.commit()

    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="passed"))
        await sql.commit()
    result = await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)
    assert result.status_code == 202, result.text
    assert (await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)).json() == result.json()
    assert len(messages(ctx)) == 1
    assert (
        await ctx.client.post("/internal/v1/agent/dispatch", json={**body, "request_id": "new-replay-attempt"}, headers=ctx.headers)
    ).status_code == 404
    assert messages(ctx) == []


async def test_cross_repository_gate_change_after_commit_keeps_original_child_pending(qualified_wave_context, monkeypatch):
    from src.agentauth import graph_dispatch

    ctx = qualified_wave_context
    async with ctx.session_factory() as sql:
        successor = await sql.scalar(select(OrchestrationNode).where(OrchestrationNode.repository_identity == "example/b"))
        gate = await _make_node(sql, ctx.flow, node_ref="late-gate", kind="gate", issue_ref=None, state="passed")
        sql.add(OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=gate.id, to_node_id=successor.id))
        await sql.commit()

    original_dispatch = graph_dispatch.dispatch_node

    async def hold_gate_after_transition(sql, node_id, genesis):
        outcome = await original_dispatch(sql, node_id, genesis)
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="awaiting_gate"))
        return outcome

    monkeypatch.setattr(graph_dispatch, "dispatch_node", hold_gate_after_transition)
    body = {"persona": "developer", "target": {"repo": "example/b", "issue": 43}, "request_id": "late-gate-request"}
    refused = await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)
    assert refused.status_code == 404, refused.text
    assert messages(ctx) == []
    async with ctx.session_factory() as sql:
        successor = await sql.get(OrchestrationNode, successor.id)
        assert (successor.state, successor.attempts) == ("running", 1)
        receipts = list(
            (await sql.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.AGENT_DISPATCHED.value))).all()
        )
        assert len(receipts) == 1
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="passed"))
        await sql.commit()

    recovered = await ctx.client.post("/internal/v1/agent/dispatch", json=body, headers=ctx.headers)
    assert recovered.status_code == 202, recovered.text
    assert recovered.json()["invocation_id"] == json.loads(receipts[0].reason)["invocation_id"]
    assert len(messages(ctx)) == 1
    child = ctx.store._read("TENANT#tenant", f"EXEC#{recovered.json()['invocation_id']}")
    assert child["provider_repository_id"] == {"N": "1002"}
    assert child["repo"] == {"S": "example/b"}


@pytest.fixture
async def cross_repository_blocked_successor(qualified_wave_context):
    ctx = qualified_wave_context
    async with ctx.session_factory() as sql:
        gate = await _make_node(sql, ctx.flow, node_ref="boundary-gate", kind="gate", issue_ref=None, state="awaiting_gate")
        gate.epic_ref, gate.wave_ref = "epic-4191", "wave-2"
        successor = await sql.scalar(select(OrchestrationNode).where(OrchestrationNode.repository_identity == "example/b"))
        sql.add_all(
            [
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=ctx.evaluation_id, to_node_id=gate.id),
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=gate.id, to_node_id=successor.id),
            ]
        )
        await sql.commit()
    assert (await bind(ctx, repo="example/a")).status_code == 200
    assert (await bind(ctx, repo="example/b", wave_ref="wave-2")).status_code == 200
    return ctx, gate


async def test_scn_04_cross_repository_wave_predecessor_gate_and_replay(cross_repository_blocked_successor):
    ctx, gate = cross_repository_blocked_successor
    first = {"persona": "operations", "target": {"repo": "example/a", "issue": 44}, "request_id": "first-wave"}
    successor_body = {"persona": "operations", "target": {"repo": "example/b", "issue": 44}, "request_id": "successor-wave"}
    first_result = await ctx.client.post("/internal/v1/agent/dispatch", json=first, headers=ctx.headers)
    assert first_result.status_code == 202, first_result.text
    assert (await ctx.client.post("/internal/v1/agent/dispatch", json=successor_body, headers=ctx.headers)).status_code == 404
    reservation = envelope_digest({"principal": f"{ctx.child.invocation}#1", "request_id": successor_body["request_id"]})
    assert ctx.store._read("TENANT#tenant", f"DISPATCH#{reservation}") is None
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="passed"))
        await sql.commit()
    assert (await ctx.client.post("/internal/v1/agent/dispatch", json=successor_body, headers=ctx.headers)).status_code == 404
    assert ctx.store._read("TENANT#tenant", f"DISPATCH#{reservation}") is None
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="passed"))
        await sql.commit()
    outcome = await ctx.client.post("/internal/v1/agent/dispatch", json=successor_body, headers=ctx.headers)
    assert outcome.status_code == 202, outcome.text
    assert (await ctx.client.post("/internal/v1/agent/dispatch", json=successor_body, headers=ctx.headers)).json() == outcome.json()
    invocation_id = outcome.json()["invocation_id"]
    child = ctx.store._read("TENANT#tenant", f"EXEC#{invocation_id}")
    assert child["repo"] == {"S": "example/b"}
    assert child["provider_repository_id"] == {"N": "1002"}
    grant = ctx.store._read("TENANT#tenant", f"GRANT#{invocation_id}#1")
    assert grant["repo_scope"]["SS"] == ["example/b"]
    bootstrap, _ = await bootstrap_child(ctx, invocation_id)
    assert bootstrap.status_code == 200, bootstrap.text
    assert (
        await ctx.client.post("/internal/v1/agent/dispatch", json={**successor_body, "request_id": "competing-wave-replay"}, headers=ctx.headers)
    ).status_code == 409
    assert len(messages(ctx)) == 2
    async with ctx.session_factory() as sql:
        receipts = list(
            (
                await sql.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.WAVE_COORDINATOR_DISPATCHED.value))
            ).all()
        )
        assert len(receipts) == 2


async def test_repository_qualified_wave_refuses_true_conflict_and_changed_alias(qualified_wave_context):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = qualified_wave_context
    assert (await bind(ctx, repo="example/a")).status_code == 200
    collision = await bind(ctx, repo="example/a", wave_ref="wave-3", evaluation_issue=46)
    assert collision.status_code == 409, collision.text
    assert ctx.store._read("TENANT#tenant", wave_key(ctx.flow.id, "epic-4191", "wave-3")) is None

    canonical = ctx.store._read("TENANT#tenant", "WAVEISSUE#1001#44")
    ctx.store.client.put_item(TableName=ctx.store.table, Item={**canonical, "digest": {"S": "changed"}})
    async with ctx.session_factory() as sql:
        with pytest.raises(BootstrapRefusedError, match="wave assignment is unavailable"):
            await load_issue_wave(service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="example/a", issue=44)


async def test_legacy_wave_read_remains_compatible_but_refuses_ambiguous_rebinding(wave_context):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = wave_context
    assert (await bind(ctx)).status_code == 200
    async with ctx.session_factory() as sql:
        mapping, role, _, _ = await load_issue_wave(
            service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="org/repo", issue=44
        )
        assert role == "orchestrator"
        assert mapping.get("provider_repository_id") == 987
        node = await sql.get(OrchestrationNode, ctx.node.id)
        node.provider_repository_id = 1001
        node.repository_identity = "example/a"
        await sql.commit()
    async with ctx.session_factory() as sql:
        with pytest.raises(BootstrapRefusedError, match="wave assignment is unavailable"):
            await load_issue_wave(service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="org/repo", issue=44)


@pytest.mark.parametrize("interrupted", [False, True], ids=["committed", "missing-sql-receipt"])
async def test_legacy_wave_registration_replay_preserves_original_reservation(legacy_wave_context, interrupted):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = legacy_wave_context
    original_response = await bind(ctx)
    assert original_response.status_code == 200
    key = wave_key(ctx.flow.id, "epic-4191", "wave-1")
    original = ctx.store._read("TENANT#tenant", key)
    mapping = json.loads(original["mapping_json"]["S"])
    aliases = {issue: ctx.store._read("TENANT#tenant", f"WAVEISSUE#org/repo#{issue}") for issue in (44, 45)}
    assert "provider_repository_id" not in mapping
    assert "installation_id" not in mapping
    assert all(len(node) == 6 for node in mapping["node_snapshot"])
    if interrupted:
        async with ctx.session_factory() as sql:
            receipt = await sql.get(OrchestrationDecision, mapping["receipt_id"])
            await sql.delete(receipt)
            await sql.commit()
        async with ctx.session_factory() as sql:
            with pytest.raises(BootstrapRefusedError, match="wave assignment is unavailable"):
                await load_issue_wave(service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="org/repo", issue=44)

    ctx.verify_issues.return_value = 1001
    for _ in range(2):
        response = await bind(ctx)
        assert response.status_code == 200, response.text
        assert response.json() == original_response.json()
    assert ctx.store._read("TENANT#tenant", key) == original
    for issue in (44, 45):
        assert ctx.store._read("TENANT#tenant", f"WAVEISSUE#org/repo#{issue}") == aliases[issue]
        assert ctx.store._read("TENANT#tenant", f"WAVEISSUE#1001#{issue}") is None
    async with ctx.session_factory() as sql:
        receipts = list(await sql.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.WAVE_MATERIALIZED.value)))
        assert len(receipts) == 1
        assert receipts[0].id == mapping["receipt_id"]
        assert receipts[0].reason == json.dumps({"mapping_digest": original["digest"]["S"]})
        loaded, role, _, _ = await load_issue_wave(
            service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="org/repo", issue=44
        )
        assert loaded == mapping
        assert role == "orchestrator"
    assert (await dispatch(ctx)).status_code == 404
    assert messages(ctx) == []


@pytest.mark.parametrize("change", ["request", "snapshot", "repository_binding", "alias", "authority"])
async def test_legacy_wave_registration_recovery_refuses_changed_assignment(legacy_wave_context, change):
    ctx = legacy_wave_context
    assert (await bind(ctx)).status_code == 200
    key = wave_key(ctx.flow.id, "epic-4191", "wave-1")
    original = ctx.store._read("TENANT#tenant", key)
    mapping = json.loads(original["mapping_json"]["S"])
    async with ctx.session_factory() as sql:
        receipt = await sql.get(OrchestrationDecision, mapping["receipt_id"])
        await sql.delete(receipt)
        if change == "snapshot":
            node = await sql.get(OrchestrationNode, ctx.node.id)
            node.node_ref = "changed-story"
        elif change == "repository_binding":
            evaluation = await sql.get(OrchestrationNode, ctx.evaluation_id)
            evaluation.provider_repository_id = 1001
        await sql.commit()
    if change == "repository_binding":
        parent = ctx.store._read("TENANT#tenant", f"EXEC#{ctx.child.invocation}")
        parent["provider_repository_id"] = {"N": "1001"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=parent)
    elif change == "alias":
        alias = ctx.store._read("TENANT#tenant", "WAVEISSUE#org/repo#44")
        alias["digest"] = {"S": "changed"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=alias)
    elif change == "authority":
        grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
        grant["revoked"] = {"BOOL": True}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
    ctx.verify_issues.return_value = 1001
    response = await bind(ctx, **({"orchestrator_issue": 46} if change == "request" else {}))
    assert response.status_code == (404 if change in {"repository_binding", "authority"} else 409), response.text
    assert ctx.store._read("TENANT#tenant", key) == original
    assert ctx.store._read("TENANT#tenant", "WAVEISSUE#1001#44") is None
    async with ctx.session_factory() as sql:
        assert await sql.get(OrchestrationDecision, mapping["receipt_id"]) is None
    assert messages(ctx) == []


async def test_new_wave_registration_requires_accepted_repository_identity(wave_context):
    ctx = wave_context
    ctx.verify_issues.return_value = 1001
    assert (await bind(ctx)).status_code == 404
    assert ctx.store._read("TENANT#tenant", wave_key(ctx.flow.id, "epic-4191", "wave-1")) is None
    ctx.verify_issues.return_value = 987
    response = await bind(ctx)
    assert response.status_code == 200, response.text
    key = wave_key(ctx.flow.id, "epic-4191", "wave-1")
    original = ctx.store._read("TENANT#tenant", key)
    mapping = json.loads(original["mapping_json"]["S"])
    assert mapping["provider_repository_id"] == 987
    assert mapping["installation_id"] == 123
    assert all(len(node) == 9 for node in mapping["node_snapshot"])
    assert (await bind(ctx)).status_code == 200
    assert ctx.store._read("TENANT#tenant", key) == original
    for issue in (44, 45):
        alias = ctx.store._read("TENANT#tenant", f"WAVEISSUE#org/repo#{issue}")
        assert alias["provider_repository_id"] == {"N": "987"}
        assert ctx.store._read("TENANT#tenant", f"WAVEISSUE#987#{issue}") == {**alias, "sk": {"S": f"WAVEISSUE#987#{issue}"}}
    async with ctx.session_factory() as sql:
        loaded, _, _, _ = await load_issue_wave(
            service=ctx.child.service, session=sql, tenant_id="tenant", flow_id=ctx.flow.id, repo="org/repo", issue=44
        )
        assert loaded == mapping


async def test_emitter_coordinator_developer_reviewer_and_evaluation(wave_context):
    ctx = wave_context
    assert "BG_ORCH_DISPATCH_REPO" not in ctx.runtime.env
    launch, headers = await start_wave(ctx)
    assert (await bind(ctx)).status_code == 200
    assert (await dispatch(ctx)).json() == launch.json()
    assert (await dispatch(ctx, request_id="duplicate-wave")).status_code == 409
    row = ctx.store._read("TENANT#tenant", f"GRANT#{launch.json()['invocation_id']}#1")
    assert row["target_relationships"]["SS"] == ["descendant", "self"]
    assert TargetRelationship.FLOW_NODE.value not in row["target_relationships"]["SS"]
    # A coordinator launch does not execute its EVAL anchor.
    async with ctx.session_factory() as sql:
        evaluation = await sql.get(OrchestrationNode, ctx.evaluation_id)
        assert (evaluation.state, evaluation.attempts) == ("pending", 0)
    assert (await dispatch(ctx, issue=45, request_id="early-eval", headers=headers)).status_code == 404
    developer = await dispatch(ctx, persona="developer", issue=43, request_id="story", headers=headers)
    assert developer.status_code == 202, developer.text
    boot, developer_headers = await bootstrap_child(ctx, developer.json()["invocation_id"])
    assert boot.status_code == 200, boot.text
    review = await dispatch(ctx, persona="reviewer", issue=43, request_id="review", headers=developer_headers)
    assert review.status_code == 202, review.text
    assert (await bootstrap_child(ctx, review.json()["invocation_id"]))[0].status_code == 200
    status = await ctx.client.get("/internal/v1/agent/status", params={"run": review.json()["invocation_id"]}, headers=headers)
    assert status.status_code == 200
    # The existing engine releases eval after dependencies pass. This test is
    # about dispatch consuming that state, not bypassing the engine's gates.
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(state="passed"))
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="ready"))
        await sql.commit()
    evaluation = await dispatch(ctx, issue=45, request_id="eval", headers=headers)
    assert evaluation.status_code == 202, evaluation.text
    assert (await bootstrap_child(ctx, evaluation.json()["invocation_id"]))[0].status_code == 200
    async with ctx.session_factory() as sql:
        node = await sql.get(OrchestrationNode, ctx.evaluation_id)
        assert (node.state, node.attempts) == ("running", 1)
        receipts = list(
            (
                await sql.execute(select(OrchestrationDecision).where(OrchestrationDecision.kind == DecisionKind.WAVE_COORDINATOR_DISPATCHED.value))
            ).scalars()
        )
        assert len(receipts) == 1 and receipts[0].actor_kind == "service"


async def test_wave_cannot_dispatch_other_wave_or_bind_more_work(wave_context):
    ctx = wave_context
    _, headers = await start_wave(ctx)
    async with ctx.session_factory() as sql:
        other = await _make_node(sql, ctx.flow, issue_ref="90", node_ref="other")
        other.wave_ref = "wave-2"
        await sql.commit()
    assert (await dispatch(ctx, persona="developer", issue=90, request_id="cross-wave", headers=headers)).status_code == 404
    assert (await ctx.client.post("/internal/v1/agent/waves", json=ctx.binding, headers=headers)).status_code == 404
    assert (await dispatch(ctx, issue=44, request_id="self-launch", headers=headers)).status_code == 404


@pytest.mark.parametrize("change", ["new_node", "changed_issue", "amended_plan", "cancelled", "missing_receipt", "bad_alias"])
async def test_changed_or_uncommitted_wave_cannot_bootstrap(wave_context, change):
    ctx = wave_context
    assert (await bind(ctx)).status_code == 200
    launch = await dispatch(ctx)
    assert launch.status_code == 202, launch.text
    if change == "missing_receipt":
        execution = ctx.store._read("TENANT#tenant", f"EXEC#{launch.json()['invocation_id']}")
        execution["orchestration_dispatch_receipt"] = {"S": "missing"}
        ctx.store.client.put_item(TableName=ctx.store.table, Item=execution)
    elif change == "bad_alias":
        ctx.store.client.put_item(
            TableName=ctx.store.table, Item={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "WAVEISSUE#org/repo#44"}, "digest": {"S": "bad"}}
        )
    else:
        async with ctx.session_factory() as sql:
            if change == "new_node":
                node = await _make_node(sql, ctx.flow, node_ref="added", issue_ref="55")
                node.epic_ref, node.wave_ref = "epic-4191", "wave-1"
            elif change == "changed_issue":
                await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.node.id).values(issue_ref="55"))
            elif change == "amended_plan":
                await _make_approval(sql, ctx.flow, kind=DecisionKind.PLAN_AMENDED.value)
            else:
                from src.orchestration.models import OrchestrationFlow

                await sql.execute(update(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).values(state="halted"))
            await sql.commit()
    assert (await bootstrap_child(ctx, launch.json()["invocation_id"]))[0].status_code == 404


async def test_binding_recovers_lost_ddb_reply_and_refuses_conflicts(wave_context, monkeypatch):
    ctx = wave_context
    original = ctx.store.client.transact_write_items

    def lost_reply(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://dynamodb.test")

    monkeypatch.setattr(ctx.store.client, "transact_write_items", lost_reply)
    assert (await bind(ctx)).status_code == 200
    assert (await bind(ctx)).status_code == 200
    assert (await bind(ctx, orchestrator_issue=99)).status_code == 409
    key = wave_key(ctx.flow.id, "epic-4191", "wave-1")
    mapping = json.loads(ctx.store._read("TENANT#tenant", key)["mapping_json"]["S"])
    assert mapping["orchestrator_issue"] == 44


async def test_native_parent_is_verified_and_failure_has_no_binding(wave_context):
    from src.agentauth.bootstrap import BootstrapRefusedError

    ctx = wave_context
    ctx.verify_issues.side_effect = BootstrapRefusedError("wrong epic")
    assert (await bind(ctx)).status_code == 404
    assert ctx.store._read("TENANT#tenant", wave_key(ctx.flow.id, "epic-4191", "wave-1")) is None
    assert messages(ctx) == []


@pytest.mark.parametrize("passed_gate", [False, True])
async def test_next_wave_requires_passed_evaluation_and_human_gate(wave_context, passed_gate):
    from src.orchestration.models import OrchestrationEdge

    ctx = wave_context
    launch, headers = await start_wave(ctx)
    async with ctx.session_factory() as sql:
        next_story = await _make_node(sql, ctx.flow, node_ref="next-story", issue_ref="90")
        next_story.repository_identity = "org/repo"
        next_story.provider_repository_id = 987
        next_story.repository_connection = {"provider": "github", "installation_id": 123}
        next_eval = await _make_node(sql, ctx.flow, node_ref="next-eval", issue_ref=None, kind="eval", state="pending")
        gate = await _make_node(sql, ctx.flow, node_ref="boundary-gate", issue_ref=None, kind="gate", state="awaiting_gate")
        for node in (next_story, next_eval, gate):
            node.epic_ref, node.wave_ref = "epic-4191", "wave-2"
        plan = await sql.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == ctx.flow.id, OrchestrationAcceptedPlan.superseded_at.is_(None)
            )
        )
        plan.plan_document = {
            **plan.plan_document,
            "nodes": [
                *plan.plan_document["nodes"],
                {
                    "address": f"{ctx.flow.slug}/{next_story.epic_ref}/{next_story.wave_ref}/{next_story.node_ref}",
                    "kind": next_story.kind,
                    "title": next_story.title,
                    "issue_ref": next_story.issue_ref,
                    "repository_identity": next_story.repository_identity,
                    "provider_repository_id": next_story.provider_repository_id,
                    "repository_connection": next_story.repository_connection,
                },
            ],
        }
        sql.add_all(
            [
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=ctx.evaluation_id, to_node_id=gate.id),
                OrchestrationEdge(org_id="tenant", flow_id=ctx.flow.id, from_node_id=gate.id, to_node_id=next_story.id),
            ]
        )
        await sql.commit()
    assert (await bind(ctx, wave_ref="wave-2", orchestrator_issue=91, evaluation_issue=92)).status_code == 200
    assert (await dispatch(ctx, issue=91, request_id="next-wave", headers=headers)).status_code == 404
    async with ctx.session_factory() as sql:
        await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == ctx.evaluation_id).values(state="passed"))
        if passed_gate:
            await sql.execute(update(OrchestrationNode).where(OrchestrationNode.id == gate.id).values(state="passed"))
        await sql.commit()
    response = await dispatch(ctx, issue=91, request_id="next-wave", headers=headers)
    assert response.status_code == (202 if passed_gate else 404), response.text
    if passed_gate:
        assert (await bootstrap_child(ctx, response.json()["invocation_id"]))[0].status_code == 200


@pytest.mark.skipif(not os.environ.get("ADP_GRAPH_TEST_DATABASE_URL"), reason="requires PostgreSQL row locks")
async def test_concurrent_materialization_and_wave_launch_are_unique(wave_context):
    ctx = wave_context
    bound = await asyncio.gather(bind(ctx), bind(ctx))
    assert [response.status_code for response in bound] == [200, 200]
    launched = await asyncio.gather(dispatch(ctx), dispatch(ctx, request_id="competing-wave"))
    assert sorted(response.status_code for response in launched) == [202, 409]
    assert len(messages(ctx)) == 1


async def test_revocation_after_mapping_commit_never_creates_sql_receipt(wave_context, monkeypatch):
    ctx = wave_context
    original = ctx.store.client.transact_write_items

    def revoke_after_mapping(**kwargs):
        result = original(**kwargs)
        if any(action.get("Put", {}).get("Item", {}).get("sk", {}).get("S", "").startswith("WAVE#") for action in kwargs["TransactItems"]):
            grant = ctx.store._read("TENANT#tenant", f"GRANT#{ctx.child.invocation}#1")
            grant["revoked"] = {"BOOL": True}
            ctx.store.client.put_item(TableName=ctx.store.table, Item=grant)
            raise EndpointConnectionError(endpoint_url="https://dynamodb.test")
        return result

    monkeypatch.setattr(ctx.store.client, "transact_write_items", revoke_after_mapping)
    assert (await bind(ctx)).status_code == 404
    async with ctx.session_factory() as sql:
        assert (
            await sql.execute(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == DecisionKind.WAVE_MATERIALIZED.value))
        ).first() is None

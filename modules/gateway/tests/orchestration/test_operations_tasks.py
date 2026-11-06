"""Operations tasks survive new sessions and link only observed provider receipts."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.agentauth.github_operations import OperationRefusedError
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import OrchestrationAction, OrchestrationExecution
from src.orchestration.operations_tasks import read_operations_tasks, record_operations_task, record_repair_pr
from tests.orchestration.test_report_dispatch import engine, prepare, session, session_factory  # noqa: F401


def assignment(node, *, attempt=1, claim_generation=1):
    return SimpleNamespace(
        tenant_id=node.org_id,
        node_id=node.id,
        invocation_id=attempt_run_id(node.id, attempt),
        attempt=attempt,
        accepted_plan_version=1,
        claim_generation=claim_generation,
        repository="example/catalog",
    )


async def cycle(database, node, number):
    node.attempts = number
    execution = OrchestrationExecution(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        cycle=number,
        phase="delivering",
        status="active",
        accepted_plan_version=1,
        claim_id="claim",
        claim_generation=1,
    )
    database.add(execution)
    await database.flush()
    return execution


async def test_restart_reuses_current_task_and_retains_previous_cycle_with_evidence(session, session_factory):  # noqa: F811
    node = await prepare(session)
    first = await cycle(session, node, 1)
    current = assignment(node)
    task_id = await record_operations_task(session, current, stage="diagnose", state="done", diagnosis_code="runtime_failed")
    await record_repair_pr(session, current, {"pull_number": 42, "head": "a" * 40, "base": "b" * 40, "checks_verified": False})
    await record_repair_pr(session, current, {"pull_number": 42, "head": "a" * 40, "base": "b" * 40, "checks_verified": True})
    session.add(
        OrchestrationAction(
            org_id=node.org_id,
            execution_id=first.id,
            operation_key="operations-repair:42:" + "a" * 40,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=1,
            detail={"pull_number": 42, "head": "a" * 40, "merge_sha": "c" * 40},
            receipt_ref="github/aws-e/adp/pull/42",
        )
    )
    await session.commit()

    async with session_factory() as recovered:
        assert await record_operations_task(recovered, current, stage="diagnose", state="done", diagnosis_code="runtime_failed") == task_id
        snapshot = await read_operations_tasks(recovered, current)
        assert snapshot["current_cycle"] == 1
        assert snapshot["cycles"][0]["tasks"][0]["diagnosis_code"] == "runtime_failed"
        assert snapshot["cycles"][0]["tasks"][1]["state"] == "open"
        assert [(row["kind"], row.get("head"), row.get("merge_sha")) for row in snapshot["cycles"][0]["evidence"]] == [
            ("operations_repair_pr", "a" * 40, None),
            ("operations_repair_merge", "a" * 40, "c" * 40),
        ]
        assert snapshot["cycles"][0]["evidence"][0]["checks_verified"] is True
        assert (
            len(
                (
                    await recovered.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == first.id,
                            OrchestrationAction.kind == "operations_repair_pr",
                        )
                    )
                ).all()
            )
            == 1
        )
        await recovered.commit()

    await cycle(session, node, 2)
    await session.commit()
    async with session_factory() as recovered:
        resumed = await read_operations_tasks(recovered, assignment(node, attempt=2))
        assert [item["cycle"] for item in resumed["cycles"]] == [1, 2]
        assert resumed["cycles"][1]["tasks"][0]["state"] == "open"
        assert resumed["node_id"] == node.id


async def test_claim_and_task_transition_fences(session):  # noqa: F811
    node = await prepare(session)
    await cycle(session, node, 1)
    current = assignment(node)
    await record_operations_task(session, current, stage="repair", state="done")
    with pytest.raises(OperationRefusedError, match="stale"):
        await record_operations_task(session, current, stage="repair", state="in_progress")
    with pytest.raises(OperationRefusedError, match="diagnosis"):
        await record_operations_task(session, current, stage="deploy", state="done", diagnosis_code="unsanitized")
    with pytest.raises(OperationRefusedError, match="ownership"):
        await read_operations_tasks(session, assignment(node, claim_generation=2))
    with pytest.raises(OperationRefusedError, match="ownership"):
        await record_repair_pr(session, assignment(node, attempt=2), {"pull_number": 42, "head": "a" * 40, "base": "b" * 40})


async def test_amended_plan_keeps_prior_repair_visible_without_advancing_current_work(session, session_factory):  # noqa: F811
    node = await prepare(session)
    previous = await cycle(session, node, 1)
    await record_operations_task(session, assignment(node), stage="diagnose", state="done", diagnosis_code="release_failed")
    await record_repair_pr(session, assignment(node), {"pull_number": 42, "head": "a" * 40, "base": "b" * 40})
    previous.status = previous.phase = "concluded"
    current_execution = await cycle(session, node, 2)
    current_execution.accepted_plan_version = 3
    current = assignment(node, attempt=2)
    current.accepted_plan_version = 3
    await session.commit()

    async with session_factory() as recovered:
        board = await read_operations_tasks(recovered, current)
        assert [(item["cycle"], item["accepted_plan_version"]) for item in board["cycles"]] == [(1, 1), (2, 3)]
        earlier, active = board["cycles"]
        assert earlier["tasks"][0]["diagnosis_code"] == "release_failed"
        assert [(item["kind"], item["pull_number"], item["head"]) for item in earlier["evidence"]] == [("operations_repair_pr", 42, "a" * 40)]
        assert all(task["state"] == "open" for task in active["tasks"])
        assert active["evidence"] == []
        with pytest.raises(OperationRefusedError, match="ownership"):
            await record_operations_task(recovered, assignment(node), stage="repair", state="done")


@pytest.mark.parametrize("foreign_field", ["org_id", "flow_id", "node_id", "accepted_plan_version", "cycle"])
async def test_history_excludes_foreign_scope_and_future_versions_or_attempts(session, foreign_field):  # noqa: F811
    node = await prepare(session)
    previous = await cycle(session, node, 1)
    await record_repair_pr(session, assignment(node), {"pull_number": 42, "head": "a" * 40, "base": "b" * 40})
    previous.status = previous.phase = "concluded"
    await cycle(session, node, 2)
    setattr(previous, foreign_field, 3 if foreign_field in {"accepted_plan_version", "cycle"} else "foreign")
    await session.flush()

    board = await read_operations_tasks(session, assignment(node, attempt=2))
    assert [item["cycle"] for item in board["cycles"]] == [2]
    assert board["cycles"][0]["evidence"] == []


@pytest.mark.parametrize("repository_scope", [False, True])
async def test_task_operation_uses_no_github_token(session, session_factory, monkeypatch, repository_scope):  # noqa: F811
    from src.agentauth.github_operation_routes import OperationRequest, _perform

    node = await prepare(session)
    await cycle(session, node, 1)
    await session.commit()
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session_factory)
    monkeypatch.setattr("src.agentauth.github_operation_routes.installation_token", lambda **_: pytest.fail("no provider token for progress"))
    current = assignment(node)
    policy = SimpleNamespace(repair_paths=[] if repository_scope else ["docs/rollout.md"], repair_scope="repository" if repository_scope else None)
    authorized = SimpleNamespace(operations_repair=True, assignment=current, policy=policy)

    async def authorize():
        return authorized

    result = await _perform(
        OperationRequest(operation="record_repair_task", task_stage="diagnose", task_state="in_progress"), authorized, authorize, now=None
    )
    assert result["operations_tasks"]["cycles"][0]["tasks"][0]["state"] == "in_progress"
    assert result["operations_tasks"]["repair_paths"] == policy.repair_paths
    assert result["operations_tasks"].get("repair_scope") == policy.repair_scope
    result = await _perform(OperationRequest(operation="read_repair_tasks"), authorized, authorize, now=None)
    assert result["operations_tasks"]["cycles"][0]["tasks"][0]["state"] == "in_progress"
    assert result["operations_tasks"]["repair_paths"] == policy.repair_paths
    assert result["operations_tasks"].get("repair_scope") == policy.repair_scope
    with pytest.raises(OperationRefusedError, match="operations executor"):
        await _perform(
            OperationRequest(operation="read_repair_tasks"), SimpleNamespace(operations_repair=False, assignment=current), authorize, now=None
        )


async def test_multiple_repairs_in_one_dispatch_keep_separate_tasks_and_receipts(session, session_factory):  # noqa: F811
    node = await prepare(session)
    execution = await cycle(session, node, 1)
    current = assignment(node)
    await record_operations_task(session, current, stage="diagnose", state="done", diagnosis_code="test_failure", repair_cycle=1)
    await record_repair_pr(session, current, {"pull_number": 42, "head": "a" * 40, "base": "b" * 40, "checks_verified": True})
    await record_operations_task(session, current, stage="diagnose", state="in_progress", diagnosis_code="runtime_failure", repair_cycle=2)
    await record_operations_task(session, current, stage="diagnose", state="done", repair_cycle=2)
    await record_repair_pr(session, current, {"pull_number": 43, "head": "d" * 40, "base": "c" * 40, "checks_verified": True})
    session.add(
        OrchestrationAction(
            org_id=node.org_id,
            execution_id=execution.id,
            operation_key="operations-repair:43:" + "d" * 40,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=1,
            detail={"pull_number": 43, "head": "d" * 40, "merge_sha": "e" * 40},
            receipt_ref="github/aws-e/adp/pull/43",
        )
    )
    session.add(
        OrchestrationAction(
            org_id=node.org_id,
            execution_id=execution.id,
            operation_key="operations-runtime:88:1",
            kind="deployment_verification",
            status="succeeded",
            attempt=1,
            detail={"deployment_receipt": {"source_revision": "e" * 40, "workflow_operation_keys": ["operations-workflow:88:1"]}},
            receipt_ref="operations-runtime:88:1",
        )
    )
    await session.commit()
    async with session_factory() as recovered:
        await record_repair_pr(recovered, current, {"pull_number": 42, "head": "a" * 40, "base": "b" * 40, "checks_verified": True})
        snapshot = await read_operations_tasks(recovered, current)
        repairs = snapshot["cycles"][0]["repair_cycles"]
        assert [item["repair_cycle"] for item in repairs] == [1, 2]
        assert [item["tasks"][0]["diagnosis_code"] for item in repairs] == ["test_failure", "runtime_failure"]
        assert [item["kind"] for item in repairs[0]["evidence"]] == ["operations_repair_pr"]
        assert [item["kind"] for item in repairs[1]["evidence"]] == [
            "operations_repair_pr",
            "operations_repair_merge",
            "deployment_verification",
        ]
        assert repairs[1]["evidence"][-1]["source_revision"] == "e" * 40
        assert (
            len(
                (
                    await recovered.scalars(
                        select(OrchestrationAction).where(
                            OrchestrationAction.execution_id == execution.id,
                            OrchestrationAction.kind == "operations_repair_pr",
                        )
                    )
                ).all()
            )
            == 2
        )


async def test_admission_tasks_are_persisted_once_before_worker_progress(session):  # noqa: F811
    from src.orchestration.operations_tasks import seed_operations_tasks

    node = await prepare(session)
    execution = await cycle(session, node, 1)
    await seed_operations_tasks(session, execution)
    await seed_operations_tasks(session, execution)
    tasks = (
        await session.scalars(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.kind == "operations_task",
            )
        )
    ).all()
    assert len(tasks) == 6
    assert all(task.detail["state"] == "open" for task in tasks)
    assert (await read_operations_tasks(session, assignment(node)))["title"] == node.title


@pytest.mark.parametrize("case", ["valid", "newer", "foreign", "oversized", "duplicate"])
async def test_task_context_uses_only_assigned_tenant_plan_and_bounded_metadata(session, case):  # noqa: F811
    from src.orchestration.compile import address_of
    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow

    node = await prepare(session)
    await cycle(session, node, 1)
    flow = await session.get(OrchestrationFlow, node.flow_id)
    document = {
        "address": address_of(flow.slug, node),
        "kind": "story",
        "title": "Accepted rollout",
        "criteria": ["Write docs/rollout.md, then verify real deployment."],
        "required_inputs": [
            {
                "name": "Operator dispatch",
                "source": "deployment_workflow:gateway-deploy.yml",
                "description": "Operator dispatches after verified repair merge.",
            }
        ],
        "evidence_expectations": ["Runtime receipt and real smoke evidence"],
    }
    if case == "oversized":
        document["criteria"] = ["x" * 17000]
    session.add(
        OrchestrationAcceptedPlan(
            org_id=node.org_id if case != "foreign" else "another-tenant",
            flow_id=node.flow_id,
            version=1,
            plan_hash="a" * 64,
            plan_document={"nodes": [document, document] if case == "duplicate" else [document]},
        )
    )
    if case == "newer":
        session.add(
            OrchestrationAcceptedPlan(
                org_id=node.org_id,
                flow_id=node.flow_id,
                version=2,
                plan_hash="b" * 64,
                plan_document={"nodes": [{**document, "criteria": ["Different future work"]}]},
            )
        )
    await session.flush()
    result = (await read_operations_tasks(session, assignment(node)))["task_context"]
    if case in {"foreign", "oversized", "duplicate"}:
        assert result["available"] is False
        assert "criteria" not in result
    else:
        assert result["available"] is True
        assert result["criteria"] == document["criteria"]
        assert result["required_inputs"] == document["required_inputs"]
        assert result["evidence_expectations"] == document["evidence_expectations"]
        assert result["plan_version"] == 1 and result["plan_hash"] == "a" * 64


@pytest.mark.parametrize(
    "case",
    ["valid", "unlinked", "foreign_tenant", "foreign_flow", "foreign_node", "rejected", "service", "wrong_transition", "wrong_attempt", "oversized"],
)
async def test_recovery_note_comes_from_this_attempts_linked_resume(session, case):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from src.orchestration.models import OrchestrationDecision
    from src.orchestration.run_reports import OrchestrationRunReport

    node = await prepare(session)
    await cycle(session, node, 2)
    current = assignment(node, attempt=2)
    note = "The catalog fixture is repaired. Reuse its release and verify the deployed search API."
    if case == "oversized":
        note = "x" * 2001
    decision = OrchestrationDecision(
        org_id=node.org_id if case != "foreign_tenant" else "another-tenant",
        flow_id=node.flow_id if case != "foreign_flow" else "another-flow",
        node_id=node.id if case != "foreign_node" else "another-node",
        kind="node_stalled",
        from_state="failed" if case != "wrong_transition" else "ready",
        to_state="ready",
        actor_kind="human" if case != "service" else "service",
        actor_id="operator",
        actor_role="admin",
        reason=note,
        rejection_reason="refused" if case == "rejected" else None,
    )
    session.add(decision)
    await session.flush()
    session.add(
        OrchestrationRunReport(
            run_id=current.invocation_id,
            credential_hash="a" * 64,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            attempt=2 if case != "wrong_attempt" else 1,
            persona="operations",
            repo=current.repository,
            installation_id=42,
            dispatch_metadata={"orchestration": {"operations_recovery": {} if case == "unlinked" else {"resume_decision_id": decision.id}}},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    # A newer note must never replace the one used to dispatch this attempt.
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="node_stalled",
            from_state="failed",
            to_state="ready",
            actor_kind="human",
            actor_id="operator",
            actor_role="admin",
            reason="Unrelated later instruction",
        )
    )
    await session.flush()
    result = await read_operations_tasks(session, current)
    if case in {"valid", "oversized"}:
        assert result["recovery_note"] == {"decision_id": decision.id, "text": note[:2000], "truncated": case == "oversized"}
    else:
        assert result["recovery_note"] is None
    assert node.attempts == 2

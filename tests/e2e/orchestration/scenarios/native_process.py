"""Disposable K2 process entrypoint; executed only by the opt-in live harness.

This file is streamed unchanged to a verified gateway revision. It runs in its
OWN Python process, never kills a pod/controller, changes a feature flag, calls
run_tick over all tenants, or writes a table directly. All effects still pass
the production K1/K2 claim, policy, revision and provider-authority checks.
"""

from datetime import UTC, datetime
import asyncio
import json
import os
import re
import sys

MODES = frozenset(
    {
        "read",
        "once",
        "crash-after-intent",
        "timeout-after-effect",
        "duplicate-events",
        "out-of-order",
    }
)
KEYS = frozenset(
    {
        "mode",
        "org_id",
        "flow_id",
        "execution_id",
        "qualification_id",
        "definition_hash",
        "plan_hash",
        "plan_version",
    }
)


def validate_request(request):
    if (
        not isinstance(request, dict)
        or set(request) != KEYS
        or request["mode"] not in MODES
    ):
        raise ValueError("unsupported isolated operation")
    for key in ("org_id", "flow_id", "execution_id"):
        if not isinstance(request[key], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", request[key]
        ):
            raise ValueError("invalid exact scope")
    if not re.fullmatch(r"q-[a-z0-9-]{8,50}", request["qualification_id"]):
        raise ValueError("not a qualification namespace")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", request[k])
        for k in ("definition_hash", "plan_hash")
    ):
        raise ValueError("unverifiable accepted definition")
    if type(request["plan_version"]) is not int or request["plan_version"] < 1:
        raise ValueError("unverifiable accepted version")
    return request


def emit(value):
    print("ADP_Q2_RESULT:" + json.dumps(value, sort_keys=True), flush=True)


async def scoped_state(factory, request):
    from sqlalchemy import select
    from src.orchestration.models import (
        OrchestrationFlow,
        OrchestrationAcceptedPlan,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationWorkClaim,
        OrchestrationDecision,
    )
    from src.orchestration.execution_store import _to_record

    async with factory() as session:
        flow = await session.scalar(
            select(OrchestrationFlow).where(
                OrchestrationFlow.org_id == request["org_id"],
                OrchestrationFlow.id == request["flow_id"],
            )
        )
        if flow is None or flow.slug != request["qualification_id"]:
            raise ValueError("foreign or non-fixture flow")
        plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.org_id == request["org_id"],
                OrchestrationAcceptedPlan.flow_id == flow.id,
                OrchestrationAcceptedPlan.version == request["plan_version"],
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        if (
            plan is None
            or plan.plan_hash != request["plan_hash"]
            or plan.plan_document.get("spec_revision") != request["definition_hash"]
        ):
            raise ValueError("accepted plan changed")
        execution = await session.scalar(
            select(OrchestrationExecution).where(
                OrchestrationExecution.org_id == request["org_id"],
                OrchestrationExecution.flow_id == flow.id,
                OrchestrationExecution.id == request["execution_id"],
            )
        )
        if execution is None or execution.accepted_plan_version != plan.version:
            raise ValueError("foreign or stale execution")
        claim = await session.scalar(
            select(OrchestrationWorkClaim).where(
                OrchestrationWorkClaim.org_id == request["org_id"],
                OrchestrationWorkClaim.id == execution.claim_id,
            )
        )
        if claim is None or claim.generation != execution.claim_generation:
            raise ValueError("unverifiable current claim")
        actions = list(
            (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == request["org_id"],
                        OrchestrationAction.execution_id == execution.id,
                    )
                    .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
                )
            ).all()
        )
        if len(actions) > 200:
            raise ValueError("action evidence overflow")
        decisions = list(
            (
                await session.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.org_id == request["org_id"],
                        OrchestrationDecision.flow_id == flow.id,
                    )
                )
            ).all()
        )
        policy = plan.plan_document.get("execution_policy") or {}
        value = dict(
            flow_id=flow.id,
            execution_id=execution.id,
            node_id=execution.node_id,
            policy_id=policy.get("policy_id"),
            policy_hash=policy.get("policy_hash"),
            plan_version=plan.version,
            claim_generation=claim.generation,
            owner_kind=claim.owner_kind,
            owner_ref=claim.owner_ref,
            mutating_owner_count=int(
                claim.state == "held" and claim.active_run_id is not None
            ),
            coordinator_retriggers=sum(d.kind == "node_resumed" for d in decisions),
            decision_ids=[d.id for d in decisions],
            effect_ids=[a.id for a in actions],
            actions=[
                dict(
                    id=a.id,
                    operation_key=a.operation_key,
                    kind=a.kind,
                    status=a.status,
                    receipt_ref=a.receipt_ref,
                    attempt=a.attempt,
                    observed_at=a.observed_at.isoformat() if a.observed_at else None,
                )
                for a in actions
            ],
            pending_operation=execution.pending_action_key,
            progress_revision=execution.revision,
            phase=execution.phase,
            status=execution.status,
            terminal=execution.status == "concluded",
            next_check_at=execution.next_check_at.isoformat()
            if execution.next_check_at
            else None,
            explicit_block=execution.block_code,
            observed_at=datetime.now(UTC).isoformat(),
            process_id=os.getpid(),
        )
        return _to_record(execution), actions, value


async def execute(request):
    from src.shared.database import get_session_factory
    from src.orchestration import execution_runner as runner
    from src.orchestration.execution_state import Observation, ObservedOutcome
    from src.orchestration.execution_store import record_observation

    factory = get_session_factory()
    initial, actions, before = await scoped_state(factory, request)
    mode = request["mode"]
    if mode == "read":
        return {"before": before, "after": before, "live": True}
    config = runner.RunnerConfig.from_env()
    if not config.enabled:
        raise ValueError("runner feature is disabled; harness cannot activate it")
    if mode in {"duplicate-events", "out-of-order"}:
        # Replay actual, already-settled native observations. No fabricated
        # provider receipt, webhook signature, expected success or new event id.
        rows = [
            a for a in actions if a.status in {"succeeded", "failed"} and a.receipt_ref
        ]
        if not rows or (mode == "out-of-order" and len(rows) < 2):
            raise ValueError("no captured native observations available to replay")
        selected = (
            [rows[-1], rows[-1]] if mode == "duplicate-events" else [rows[-1], rows[0]]
        )
        delivery_ids = []
        for action in selected:
            async with factory() as session:
                outcome = await record_observation(
                    session,
                    identity=runner._identity(initial),
                    observation=Observation(
                        operation_key=action.operation_key,
                        outcome=ObservedOutcome(action.status),
                        receipt_ref=action.receipt_ref,
                    ),
                )
                await session.commit()
                delivery_ids.append(
                    {"action_id": action.id, "outcome": outcome.kind.value}
                )
        _, _, after = await scoped_state(factory, request)
        return {
            "live": True,
            "before": before,
            "after": after,
            "injection": {
                "delivery_ids": delivery_ids,
                "boundary": "K1.record_observation",
            },
        }
    now = datetime.now(UTC)
    if initial.next_check_at is None or initial.next_check_at > now:
        raise ValueError("execution is not due; harness cannot accelerate its schedule")
    from src.orchestration.review_cycle import handlers as review
    from src.orchestration.merge_controller import handlers as merge
    from src.orchestration.deployment_workflows import handlers as deploy
    from src.orchestration.deployment_controller import handlers as runtime
    from src.orchestration.evaluation_controller import handlers as evaluate

    handlers = {
        **review(factory),
        **merge(factory),
        **deploy(factory),
        **runtime(factory),
        **evaluate(factory),
        **runner.registered_execution_handlers(),
    }
    handler = handlers.get(initial.phase)
    if handler is None:
        raise ValueError("no deployed phase handler")
    injected = {}

    async def checkpoint(name, context):
        if mode == "crash-after-intent" and name == "after_intent":
            _, _, durable = await scoped_state(factory, request)
            emit(
                {
                    "live": True,
                    "before": before,
                    "after": durable,
                    "injection": {
                        "checkpoint": name,
                        "isolated": True,
                        "before_process_id": os.getpid(),
                    },
                }
            )
            # Only this newly created interpreter. Never signal an existing PID.
            os._exit(75)
        if mode == "timeout-after-effect" and name == "after_effect":
            injected.update(transport_outcome="timeout", checkpoint=name)
            raise TimeoutError(
                "qualification response-loss injection after actual provider effect"
            )

    report = runner.RunnerReport(enabled=True)
    try:
        await runner._process_one(
            factory,
            initial=initial,
            handler=handler,
            config=config,
            clock=runner.SystemClock(),
            authority_verifier=runner.verify_live_authority,
            notifier=runner.notify,
            report=report,
            checkpoint=checkpoint,
        )
    except TimeoutError:
        if not injected:
            raise
    _, _, after = await scoped_state(factory, request)
    return {
        "live": True,
        "before": before,
        "after": after,
        "injection": injected,
        "runner": {"observed": report.observed, "errors": report.errors},
    }


if __name__ == "__main__":
    if not __debug__:
        sys.exit(2)
    try:
        request = validate_request(json.loads(sys.argv[1]))

        async def bounded():
            return await asyncio.wait_for(execute(request), timeout=50)

        emit(asyncio.run(bounded()))
    except Exception as exc:
        # Do not print exception strings: driver/DB failures can contain DSNs.
        emit({"live": True, "status": "NOT_RUN", "reason": type(exc).__name__})
        sys.exit(3)

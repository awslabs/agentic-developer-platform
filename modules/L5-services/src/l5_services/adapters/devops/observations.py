"""Import provider-observed work only after matching an explicit scoped binding."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.devops_observation import NativeRunIdentity
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.models.run import Run
from l5_services.adapters.devops.prevention import PreventionAdapter
from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.devops.prevention import PreventionPolicy, prevention_readiness


async def import_native_task(session, binding: Binding, scope: dict, task: dict, *, now: datetime,
                             source="provider_readback", prevention_goal_id=None,
                             per_run_limits=None):
    configuration = await session.get(Configuration, (scope["tenant_id"], binding.configuration_id))
    if (binding.tenant_id != scope["tenant_id"] or binding.product_id != scope["product_id"]
            or binding.project_id != scope["project_id"] or binding.service_id != "devops"
            or binding.scope != scope or binding.state != "ready" or configuration is None
            or configuration.service_id != "devops" or configuration.product_id != scope["product_id"]
            or configuration.project_id != scope["project_id"]
            or configuration.current_revision != binding.revision
            or configuration.relationship_revision != scope.get("relationship_revision")
            or (binding.native_resource_ref or {}).get("record_type") != "agent_space"):
        raise HTTPException(404, detail={"code": "not_found"})
    space_id = binding.native_resource_ref["record_id"]
    connection_id = binding.connection_ref.get("source_connection_id")
    execution_id = task.get("executionId")
    if (task.get("agentSpaceId") != space_id or task.get("taskType") not in (
            {"INVESTIGATION", "MITIGATION"} if prevention_goal_id is None else {"EVALUATION"})
            or source not in {"provider_readback", "native_event"}
            or (prevention_goal_id is not None and (source != "provider_readback"
                or not isinstance(task.get("taskId"), str) or not task["taskId"]))
            or not isinstance(connection_id, str) or not connection_id
            or not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 128):
        raise HTTPException(404, detail={"code": "not_found"})
    key = (scope["tenant_id"], connection_id, space_id, execution_id)
    existing = await session.get(NativeRunIdentity, key)
    if existing is not None:
        run = await session.get(Run, (scope["tenant_id"], existing.run_id))
        if prevention_goal_id is not None and (run.input.get("native_goal_id") != prevention_goal_id
                                               or run.input.get("native_task_id") != task["taskId"]):
            raise ValueError("native prevention occurrence changed attribution")
        return run
    if prevention_goal_id is not None:
        prior = (await session.scalars(select(Run).where(
            Run.tenant_id == scope["tenant_id"], Run.binding_id == binding.binding_id,
            Run.service_id == "devops", Run.origin == "native",
            Run.input["native_task_id"].as_string() == task["taskId"],
        ))).all()
        if prior:
            raise ValueError("native prevention task changed execution identity")
    submitted = (await session.scalars(select(Run).where(
        Run.tenant_id == scope["tenant_id"], Run.product_id == scope["product_id"],
        Run.project_id == scope["project_id"], Run.binding_id == binding.binding_id,
        Run.service_id == "devops", Run.origin == "adp_submitted",
        Run.native_job_ref["record_id"].as_string() == execution_id,
    ))).all()
    if len(submitted) > 1:
        raise ValueError("provider identity resolves to multiple admitted runs")
    if submitted and (submitted[0].native_job_ref.get("source_connection_id") == connection_id
                      and (submitted[0].admission or {}).get("native_space_ref") == binding.native_resource_ref):
        return submitted[0]
    run = Run(
        tenant_id=scope["tenant_id"], run_id=str(uuid4()), product_id=scope["product_id"],
        project_id=scope["project_id"], service_id="devops", configuration_id=binding.configuration_id,
        configuration_revision=binding.revision, binding_id=binding.binding_id,
        binding_generation=binding.generation, trigger_id="native", origin="native", scope=scope,
        input={"service_id": "devops", "capability": "prevention" if prevention_goal_id else task["taskType"].lower(),
               "source": source, "native_task_id": task.get("taskId"),
               **({"native_goal_id": prevention_goal_id} if prevention_goal_id else {})}, admission=None,
        capture_limits=per_run_limits if prevention_goal_id else None,
        state="running", provider_state="unknown", deadline_at=now + timedelta(days=1),
        next_check_at=now + timedelta(minutes=1),
        native_job_ref={"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                        "source_connection_id": connection_id, "record_type": "native_job",
                        "record_id": execution_id, "record_revision": execution_id},
        monitoring={"state": "current" if source == "provider_readback" else "not_started",
                    "progress_state": "unknown", "poll_attempts": 0,
                    "consecutive_poll_errors": 0, "next_poll_at": (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    **({"last_successful_poll_at": now.isoformat().replace("+00:00", "Z")}
                       if source == "provider_readback" else {}), "attribution": task.get("createdBy"),
                    "native_status": task.get("status", "unknown")},
    )
    session.add(run)
    session.add(NativeRunIdentity(tenant_id=scope["tenant_id"], connection_id=connection_id,
                                  space_id=space_id, execution_id=execution_id, run_id=run.run_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(NativeRunIdentity, key)
        if existing is None:
            raise
        return await session.get(Run, (scope["tenant_id"], existing.run_id))
    return run


async def readback_native_tasks(session, binding: Binding, scope: dict, provider_session, *, now: datetime):
    """Bounded provider listing for an explicitly selected, verified space."""
    identity = binding.provider_identity or {}
    if not identity or any(getattr(provider_session, field, None) != identity.get(field)
                           for field in ("account_id", "region", "role_arn")):
        raise HTTPException(404, detail={"code": "not_found"})
    space_id = (binding.native_resource_ref or {}).get("record_id")
    if not space_id or binding.scope != scope or binding.state != "ready":
        raise HTTPException(404, detail={"code": "not_found"})
    client = provider_session.client("devops-agent")
    observed = await asyncio.to_thread(client.get_agent_space, agentSpaceId=space_id)
    if (observed.get("agentSpace") or {}).get("agentSpaceId") != space_id:
        raise HTTPException(404, detail={"code": "not_found"})
    token = None
    seen = set()
    imports = []
    for _ in range(10):
        page = await asyncio.to_thread(client.list_backlog_tasks, agentSpaceId=space_id, limit=100,
                                       **({"nextToken": token} if token else {}))
        if not isinstance(page.get("tasks"), list) or len(page["tasks"]) > 100:
            raise ValueError("provider readback is not a bounded task page")
        for task in page["tasks"]:
            if task.get("agentSpaceId") != space_id:
                raise HTTPException(404, detail={"code": "not_found"})
            if task.get("taskType") in {"INVESTIGATION", "MITIGATION"} and task.get("executionId"):
                imports.append(await import_native_task(session, binding, scope, task, now=now))
        token = page.get("nextToken")
        if not token:
            return imports
        if token in seen:
            raise ValueError("provider readback repeated a page cursor")
        seen.add(token)
    raise ValueError("provider readback exceeded the page limit")


async def readback_prevention(session, binding: Binding, scope: dict, provider_session, *, now: datetime):
    """Import only the provider's latest observed task for a configured native goal."""
    identity = binding.provider_identity or {}
    if binding.state != "ready" or binding.scope != scope or not identity or any(
        getattr(provider_session, field, None) != identity.get(field)
        for field in ("account_id", "region", "role_arn")
    ):
        raise HTTPException(404, detail={"code": "not_found"})
    receipts = (await session.scalars(select(DevopsSpaceIntent).where(
        DevopsSpaceIntent.tenant_id == binding.tenant_id,
        DevopsSpaceIntent.configuration_revision == binding.revision,
        DevopsSpaceIntent.selection["binding_id"].as_string() == binding.binding_id,
    ).limit(2))).all()
    if len(receipts) != 1:
        return []
    receipt = receipts[0]
    selection = SpaceSelection(**receipt.selection)
    if (selection.tenant_id, selection.product_id, selection.project_id,
        selection.account_id, selection.region) != (
            scope["tenant_id"], scope["product_id"], scope["project_id"],
            identity["account_id"], identity["region"]
    ) or receipt.native_space_id != (binding.native_resource_ref or {}).get("record_id"):
        raise HTTPException(404, detail={"code": "not_found"})
    policy_record = receipt.prevention_policy or {}
    if policy_record.get("state") != "verified" or not policy_record.get("goal_id"):
        return []
    policy = PreventionPolicy(**{key: policy_record[key] for key in (
        "enabled", "goal_id", "expression", "timezone", "missed_run_policy")})
    client = provider_session.client("devops-agent")
    adapter = PreventionAdapter(client, account_id=selection.account_id, region=selection.region)
    observed = await asyncio.to_thread(adapter.read, selection, receipt.native_space_id,
                                       receipt.configuration_revision)
    status = prevention_readiness(policy, observed, configuration_revision=binding.revision,
                                  scope_fingerprint=selection.fingerprint())
    if status["status"] != "ready":
        raise ValueError("native prevention schedule changed since verified policy")
    receipt.prevention_observation = observed
    await session.commit()
    goal = next(goal for goal in observed["native_schedule"]["goals"]
                if goal["goal_id"] == policy.goal_id)
    task_id = goal["last_task_id"]
    if not task_id:
        return []
    response = await asyncio.to_thread(client.get_backlog_task,
                                       agentSpaceId=receipt.native_space_id, taskId=task_id)
    task = response.get("task") or {}
    if task.get("taskId") != task_id:
        raise ValueError("native prevention task differs from goal readback")
    return [await import_native_task(session, binding, scope, task, now=now,
                                     prevention_goal_id=policy.goal_id,
                                     per_run_limits=policy_record.get("per_run_limits"))]

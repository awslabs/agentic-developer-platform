"""Original command dependencies; these are application references, never EC2 ARNs."""

import asyncio
import json

from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.store import _record
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)

from .node_command import invocation, ssm_client, validate_receipt
from .node_command_plan import KINDS, canonical, digest, reference


async def rows(provider, operation):
    lease = operation.grant.lease
    source = operation.request.parameters.get(
        "controller_source_operation_id", lease.operation_id
    )
    allocation = operation.request.parameters["allocation_id"]
    async with provider.domain_pool.acquire() as c:
        found = await c.fetch(
            """SELECT * FROM controller_node_commands
 WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 AND allocation_id=$4
 ORDER BY reference LIMIT 33""",
            source,
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
    if len(found) > 32:
        raise OperationRefused("original node command inventory exceeds bound")
    async with provider.execution_pool.acquire() as c:
        original = await c.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1",
            source,
        )
        intents = await c.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 AND operation_kind=ANY($4::text[]) LIMIT 3",
            source,
            lease.org_id,
            lease.workspace_id,
            list(KINDS),
        )
    if (
        original is None
        or original["org_id"] != lease.org_id
        or original["workspace_id"] != lease.workspace_id
        or len(intents) > 2
    ):
        raise OperationRefused("original command operation is unavailable")
    from harness_jobs.identity import decode_payload

    request = decode_payload(original["request_payload"])
    if request.parameters["allocation_id"] != allocation:
        raise OperationRefused("original command allocation differs")
    record = _record(original)
    approved = {step_key(record, step): step for step in admitted_steps(record)}
    by_key = {intent["idempotency_key"]: intent for intent in intents}
    for row in found:
        expected = reference(
            source,
            lease.org_id,
            lease.workspace_id,
            allocation,
            row["instance_id"],
            row["purpose"],
        )
        intent = by_key.get(row["step_key"])
        step = approved.get(row["step_key"])
        contract = json.loads(row["contract"])
        if (
            canonical(contract) != row["contract"]
            or digest(contract) != row["contract_sha256"]
            or any(
                contract.get(key) != row[key]
                for key in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "allocation_id",
                    "purpose",
                    "instance_id",
                    "region",
                    "account_id",
                )
            )
        ):
            raise OperationRefused("original node command contract binding differs")
        if (
            row["reference"] != expected
            or row["plan_digest"] != original["plan_digest"]
            or intent is None
            or step is None
            or (intent["provider"], intent["operation_kind"], intent["target"])
            != (step.provider, step.operation_kind, step.target)
            or intent["provider"] != "aws"
            or intent["allocation_id"] != allocation
            or KINDS[intent["operation_kind"]] != row["purpose"]
        ):
            raise OperationRefused("original node command inventory binding differs")
    return found


async def discover(provider, operation):
    return {
        row["reference"]: AllocationResource(
            row["reference"],
            "aws",
            row["reference"],
            "node_command",
            frozenset({row["step_key"]}),
        )
        for row in await rows(provider, operation)
    }


async def authorize(provider, operation):
    callback = getattr(provider, "node_observation_authorize", None)
    if callback is not None:
        await callback()
    else:
        current, _, _, _ = await provider.registry.verify(
            operation.grant.lease.operation_id, require_active=True
        )
        a, b = current.grant.lease, operation.grant.lease
        if (a.operation_id, a.attempt_id, a.holder, a.fence_token) != (
            b.operation_id,
            b.attempt_id,
            b.holder,
            b.fence_token,
        ):
            raise OperationRefused("node command observation authority changed")


async def terminated(provider, operation, plan, row):
    session, _ = await provider.session_for(operation, plan)
    await authorize(provider, operation)
    response = await asyncio.to_thread(
        session.client("ec2", region_name=row["region"]).describe_instances,
        InstanceIds=[row["instance_id"]],
    )
    await authorize(provider, operation)
    instances = [
        i
        for reservation in response.get("Reservations", [])
        for i in reservation.get("Instances", [])
    ]
    # Empty/denied/missing response is not termination proof.
    return (
        len(instances) == 1
        and instances[0].get("InstanceId") == row["instance_id"]
        and instances[0].get("State", {}).get("Name") == "terminated"
    )


async def observe(provider, operation, plan, resource):
    ref = resource.provider_reference
    found = [row for row in await rows(provider, operation) if row["reference"] == ref]
    if len(found) != 1:
        return ResourceObservation(
            ResourcePresence.UNKNOWN, ref, detail="original command journal unavailable"
        )
    row = found[0]
    try:
        if await terminated(provider, operation, plan, row):
            return ResourceObservation(ResourcePresence.ABSENT, ref)
        # Bootstrap can enqueue systemd work even after SSM exits. Keep the
        # original compute dependency present until exact termination is proven.
        return ResourceObservation(
            ResourcePresence.PRESENT, ref, "original command compute remains"
        )
    except Exception:
        return ResourceObservation(
            ResourcePresence.UNKNOWN, ref, detail="original command instance unobserved"
        )


async def require_completed(provider, operation, plan, callback):
    if plan.node_bootstrap is None:
        return
    await callback()
    found = await rows(provider, operation)
    instances = await provider.instances(operation, plan)
    from .node_command import contract_for, original_instances

    await original_instances(provider, operation, plan, instances)
    expected = {
        (i["InstanceId"], purpose) for i in instances for purpose in KINDS.values()
    }
    if {(r["instance_id"], r["purpose"]) for r in found} != expected:
        raise OperationRefused("original native command receipts are incomplete")
    by_instance = {instance["InstanceId"]: instance for instance in instances}
    for row in found:
        if json.loads(row["contract"]) != contract_for(
            operation, plan, by_instance[row["instance_id"]], row["purpose"]
        ):
            raise OperationRefused("completed node command differs from approved plan")
        if row["state"] != "succeeded" or not row["result"]:
            raise OperationRefused("original native command remains unresolved")
        validate_receipt(row["result"], json.loads(row["contract"]))
    await callback()


async def recover(provider, operation, plan, kind, request_id):
    if kind not in KINDS or request_id != "native-command:" + kind:
        raise OperationRefused("original native request identity unavailable")
    found = [
        row for row in await rows(provider, operation) if row["purpose"] == KINDS[kind]
    ]
    if len(found) != plan.data["node_count"]:
        return "unknown", None
    # Absence after exact original termination settles risk without pretending
    # bootstrap succeeded or scheduling another attempt.
    if all([await terminated(provider, operation, plan, row) for row in found]):
        return "absent", None
    session, _ = await provider.session_for(operation, plan)
    for row in found:
        if not row["command_id"]:
            return "unknown", None
        try:
            result = await invocation(
                ssm_client(session, row["region"]),
                row,
                lambda: authorize(provider, operation),
            )
        except Exception:
            return "unknown", None
        if result is None or row["state"] != "succeeded" or not row["result"]:
            return "unknown", None
        if validate_receipt(row["result"], json.loads(row["contract"])) != result:
            return "unknown", None
    return "succeeded", found[0]["reference"]

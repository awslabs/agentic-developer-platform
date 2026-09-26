"""Capture original identity evidence under its source finalizer's live fence.

Snapshots authorize no execution or release. Later readers verify the retained
original report, not the allocation's mutable current enumeration generation.
"""

import hashlib
import json

from harness_jobs.execution_plan import (
    PlanProgress,
    admitted_steps,
    confirmed_plan_progress,
    step_key,
)
from harness_jobs.identity import OperationRefused, decode_payload, payload_digest
from harness_jobs.leases import lock_lease
from harness_jobs.store import _record, stored_outcome

from .cleanup_graph import canonical, digest, header
from .cleanup_recipes import network_recipes
from .node_inventory import decode as node_identity


def document(operation, target, plan, resources, calls, network, revision):
    """Build only from original durable membership and validated source calls."""
    lease = operation.grant.lease
    creates = {c["idempotency_key"]: c["operation_kind"] for c in calls}
    members, nodes, roots, compute = [], [], [], []
    for row in sorted(resources, key=lambda r: r["provider_reference"]):
        keys = sorted(row["operation_keys"])
        if (
            row["provider"] != "aws"
            or row["operation_id"] != lease.operation_id
            or not keys
            or not set(keys) <= creates.keys()
        ):
            raise OperationRefused("snapshot member lacks original source provenance")
        kind, ref = row["kind"], row["provider_reference"]
        members.append(
            {
                "resource_id": row["resource_id"],
                "provider": "aws",
                "kind": kind,
                "reference": ref,
                "operation_keys": keys,
            }
        )
        if kind == "instance":
            if not any(creates[k] == "launch" for k in keys):
                raise OperationRefused("snapshot compute is not from original launch")
            compute.append(ref)
        elif kind == "kubernetes_node":
            identity = node_identity(ref)
            if identity["cluster_id"] != target["cluster_id"] or not any(
                creates[k] in {"launch", "run-node-bootstrap"} for k in keys
            ):
                raise OperationRefused("snapshot Node source or cluster differs")
            nodes.append(ref)
        elif kind == "workspace_object":
            parts = ref.split(":")
            if (
                len(parts) != 5
                or parts[0] != "kubernetes"
                or parts[1] not in {"Job", "Deployment", "Service"}
                or parts[2] != target["namespace"]
                or not all(parts[3:])
                or not any(creates[k] == "deploy" for k in keys)
            ):
                raise OperationRefused(
                    "snapshot root lacks original POST UID provenance"
                )
            roots.append(ref)
    if not compute:
        raise OperationRefused("snapshot requires original compute membership")
    for ref in nodes:
        identity = node_identity(ref)
        instance = identity["provider_id"].split("/")[-1]
        if (
            plan.resource_reference("instance", instance, identity["region"])
            not in compute
        ):
            raise OperationRefused("snapshot Node has no original compute identity")
    for row in network:
        if (
            any(
                row[k] != value
                for k, value in {
                    "org_id": lease.org_id,
                    "workspace_id": lease.workspace_id,
                    "allocation_id": operation.request.parameters["allocation_id"],
                    "cluster_id": target["cluster_id"],
                    "source_operation_id": lease.operation_id,
                    "source_plan_digest": operation.plan_digest,
                }.items()
            )
            or row["state"] != "present"
            or row["released_at"] is not None
        ):
            raise OperationRefused("snapshot network source evidence is incomplete")
        if (
            row["membership_generation"]
            != plan.network["cluster"]["membership_generation"]
        ):
            raise OperationRefused("snapshot network membership generation differs")
    recipes = network_recipes(plan, network)
    network_refs = {
        "network:" + operation.request.parameters["allocation_id"] + ":" + r["key"]
        for r in recipes
    }
    if network_refs != {
        r["reference"] for r in members if r["kind"] == "network_dependency"
    }:
        raise OperationRefused(
            "snapshot network membership differs from sealed inventory"
        )
    value = {
        "version": 1,
        "source_operation_id": lease.operation_id,
        "source_plan_digest": operation.plan_digest,
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "deployment_id": operation.request.parameters["controller_deployment_id"],
        "allocation_id": operation.request.parameters["allocation_id"],
        "cluster_id": target["cluster_id"],
        "cluster_name": plan.cluster_name,
        "sealed_revision": revision,
        "resources": members,
        "compute": compute,
        "nodes": nodes,
        "roots": roots,
        "network": recipes,
    }
    if len(canonical(value).encode()) > 131072:
        raise OperationRefused("cleanup snapshot exceeds its finite bound")
    return value


async def capture(finalizer, operation, target, plan, assessment):
    """Called after real source inventory attestation, never by preview."""
    if (
        operation.request.action != "provision"
        or "controller_deployment_id" not in operation.request.parameters
    ):
        return None
    finalizer.provider.workspace.require_dedicated_node_authority(target)
    inventory = assessment.inventory
    lease = operation.grant.lease
    allocation = operation.request.parameters["allocation_id"]
    if (
        not inventory
        or not inventory.complete
        or (
            inventory.org_id,
            inventory.workspace,
            inventory.allocation_id,
            inventory.executor_id,
        )
        != (lease.org_id, lease.workspace_id, allocation, lease.holder)
    ):
        raise OperationRefused(
            "source cleanup snapshot requires verified complete inventory"
        )
    async with finalizer.provider.execution_pool.acquire() as c, c.transaction():
        if not await lock_lease(c, lease):
            raise OperationRefused("source snapshot lease expired")
        if (
            await confirmed_plan_progress(c, lease.operation_id)
            is not PlanProgress.COMPLETE
        ):
            raise OperationRefused("source snapshot cannot capture an incomplete plan")
        source = await c.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
        if (
            source is None
            or source["plan_digest"] != operation.plan_digest
            or decode_payload(source["request_payload"]) != operation.request
        ):
            raise OperationRefused("source snapshot original plan changed")
        record = _record(source)
        approved = {step_key(record, step): step for step in admitted_steps(record)}
        calls = await c.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
            lease.operation_id,
        )
        if len(calls) != len(approved) or any(
            row["idempotency_key"] not in approved
            or stored_outcome(row["outcome"]) != "succeeded"
            or (row["provider"], row["operation_kind"], row["target"])
            != (
                approved[row["idempotency_key"]].provider,
                approved[row["idempotency_key"]].operation_kind,
                approved[row["idempotency_key"]].target,
            )
            for row in calls
        ):
            raise OperationRefused("source snapshot creation is not terminal")
        report = await c.fetchrow(
            "SELECT * FROM harness_provider_report WHERE report_digest=$1 AND operation_id=$2 AND org_id=$3 AND workspace_id=$4 AND attempt_id=$5 AND executor_id=$6 AND fence_token=$7 AND allocation_id=$8 AND sealed_revision=$9",
            inventory.attested_report_digest,
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            lease.attempt_id,
            lease.holder,
            lease.fence_token,
            allocation,
            inventory.revision,
        )
        if (
            report is None
            or hashlib.sha256(report["observations"].encode()).hexdigest()
            != report["report_digest"]
        ):
            raise OperationRefused("original source attestation unavailable")
        sealed = await c.fetchval(
            "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
        if sealed != report["sealed_revision"]:
            raise OperationRefused(
                "source attestation does not bind original sealed membership"
            )
        rows = await c.fetch(
            "SELECT * FROM harness_allocation_resource WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 ORDER BY provider_reference",
            lease.org_id,
            lease.workspace_id,
            allocation,
        )
        expected = {
            (
                r.resource_id,
                r.provider,
                r.provider_reference,
                r.kind,
                frozenset(r.operation_keys),
            )
            for r in inventory.resources
        }
        if expected != {
            (
                r["resource_id"],
                r["provider"],
                r["provider_reference"],
                r["kind"],
                frozenset(r["operation_keys"]),
            )
            for r in rows
        }:
            raise OperationRefused("source membership changed after attestation")
        # Only bounded SQL while source authority is locked; no callbacks/provider I/O.
        async with finalizer.provider.domain_pool.acquire() as db, db.transaction():
            network = (
                await db.fetch(
                    "SELECT r.*,m.workspace_id,m.allocation_id,m.cluster_id,m.membership_generation,m.source_operation_id,m.source_plan_digest,m.released_at FROM controller_network_resources r JOIN controller_network_members m USING(resource_key) WHERE r.org_id=$1 AND m.org_id=$1 AND m.workspace_id=$2 AND m.allocation_id=$3 ORDER BY r.resource_key LIMIT 501",
                    lease.org_id,
                    lease.workspace_id,
                    allocation,
                )
                if plan.network is not None
                else []
            )
            value = document(
                operation, target, plan, rows, calls, network, inventory.revision
            )
            stored = await db.fetchrow(
                "SELECT * FROM controller_cleanup_snapshots WHERE source_operation_id=$1",
                lease.operation_id,
            )
            if stored is not None:
                original = await retained(c, source, stored)
                if identity(original) != identity(value):
                    raise OperationRefused("original cleanup snapshot identity changed")
                if not await lock_lease(c, lease):
                    raise OperationRefused("source snapshot lost its original fence")
                return header(original)
            # Oversized graphs remain on the existing aggregate path. Do not make
            # completing a valid provision depend on fitting a new optional graph.
            try:
                graph = header(value)
            except (ValueError, OperationRefused):
                return None
            await db.execute(
                "INSERT INTO controller_cleanup_snapshots(snapshot_id,source_operation_id,org_id,workspace_id,allocation_id,source_plan_digest,body,body_sha256,sealed_revision,report_digest,enumeration_binding,report_observations,attempt_id,executor_id,fence_token) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT DO NOTHING",
                graph["snapshot_id"],
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                allocation,
                operation.plan_digest,
                canonical(value),
                digest(value),
                inventory.revision,
                report["report_digest"],
                report["enumeration_binding"],
                report["observations"],
                lease.attempt_id,
                lease.holder,
                lease.fence_token,
            )
            stored = await db.fetchrow(
                "SELECT * FROM controller_cleanup_snapshots WHERE source_operation_id=$1",
                lease.operation_id,
            )
            if stored is None:
                raise OperationRefused("original cleanup snapshot is unavailable")
            original = await retained(c, source, stored)
            if identity(original) != identity(value):
                raise OperationRefused("original cleanup snapshot identity changed")
            if not await lock_lease(c, lease):
                raise OperationRefused("source snapshot lost its original fence")
    return header(original)


def identity(document):
    # The historical seal/report remains immutable. A fresh finalizer may attest
    # unchanged original identities under a later observation or recovery claim.
    return {key: value for key, value in document.items() if key != "sealed_revision"}


async def retained(connection, source, snapshot):
    """Validate historical identity proof without borrowing current release proof."""
    value = json.loads(snapshot["body"])
    original = decode_payload(source["request_payload"])
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "source_operation_id",
            "source_plan_digest",
            "org_id",
            "workspace_id",
            "deployment_id",
            "allocation_id",
            "cluster_id",
            "cluster_name",
            "sealed_revision",
            "resources",
            "compute",
            "nodes",
            "roots",
            "network",
        }
        or type(value["version"]) is not int
        or value["version"] != 1
        or original.action != "provision"
        or value["allocation_id"] != original.parameters["allocation_id"]
        or value["deployment_id"] != original.parameters["controller_deployment_id"]
        or snapshot["body"] != canonical(value)
        or snapshot["body_sha256"] != digest(value)
        or snapshot["snapshot_id"] != header(value)["snapshot_id"]
        or any(
            snapshot[k] != source[s]
            for k, s in {
                "source_operation_id": "operation_id",
                "source_plan_digest": "plan_digest",
                "org_id": "org_id",
                "workspace_id": "workspace_id",
            }.items()
        )
        or payload_digest(decode_payload(source["request_payload"]))
        != source["plan_digest"]
        or any(
            value[k] != snapshot[k]
            for k in (
                "source_operation_id",
                "source_plan_digest",
                "org_id",
                "workspace_id",
                "allocation_id",
                "sealed_revision",
            )
        )
    ):
        raise OperationRefused("retained cleanup snapshot binding differs")
    report = await connection.fetchrow(
        "SELECT observations,sealed_revision,enumeration_binding,allocation_id FROM harness_provider_report WHERE report_digest=$1 AND operation_id=$2 AND org_id=$3 AND workspace_id=$4 AND attempt_id=$5 AND executor_id=$6 AND fence_token=$7",
        snapshot["report_digest"],
        snapshot["source_operation_id"],
        snapshot["org_id"],
        snapshot["workspace_id"],
        snapshot["attempt_id"],
        snapshot["executor_id"],
        snapshot["fence_token"],
    )
    if (
        report is None
        or any(
            report[k] != snapshot[s]
            for k, s in {
                "observations": "report_observations",
                "sealed_revision": "sealed_revision",
                "enumeration_binding": "enumeration_binding",
                "allocation_id": "allocation_id",
            }.items()
        )
        or hashlib.sha256(report["observations"].encode()).hexdigest()
        != snapshot["report_digest"]
    ):
        raise OperationRefused("retained original source report unavailable")
    # Current provider enumeration and query generations intentionally not read:
    # cleanup must advance them. This snapshot never authorizes allocation release.
    return value


async def select(connection, source, graph=None):
    """Read-only selection of source-owned evidence; no capture or fencing."""
    snapshot = await connection.fetchrow(
        "SELECT * FROM controller_cleanup_snapshots WHERE source_operation_id=$1 AND org_id=$2 AND workspace_id=$3",
        source["operation_id"],
        source["org_id"],
        source["workspace_id"],
    )
    if snapshot is None:
        raise OperationRefused(
            "staged cleanup requires a retained original source snapshot"
        )
    value = await retained(connection, source, snapshot)
    expected = header(value)
    if graph is not None and graph != expected:
        raise OperationRefused("cleanup graph differs from its original snapshot")
    return expected, value

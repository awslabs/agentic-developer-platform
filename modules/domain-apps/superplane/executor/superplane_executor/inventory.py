"""Provider-backed allocation membership and exact domain accounting decisions.

Financial balances remain with C. This adapter persists the shared service's
per-call/per-resource dispositions and exposure assessment without doing billing
arithmetic or treating a successful stop request as proof of complete absence.
"""

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import quote

from botocore.exceptions import ClientError
from harness_jobs.execution import disposition_for, read_call
from harness_jobs.execution_plan import PlanProgress, confirmed_plan_progress
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease
from harness_jobs.inventory import (
    AllocationResource,
    InventoryAuthority,
    ResourceObservation,
    ResourcePresence,
)
from harness_jobs.store import OperationStore

from .plan import Plan


class Finalizer:
    def __init__(self, provider, registry):
        self.provider, self.registry = provider, registry
        self.authority = InventoryAuthority(
            connect=provider.execution_pool.acquire,
            authenticate=registry.authenticate,
            query_provider=self.query,
        )

    async def context(self, operation_id):
        operation, target, _, _ = await self.registry.verify(
            operation_id, require_active=True
        )
        return operation, target, Plan.read(operation, target)

    async def known(self, lease, allocation):
        async with self.provider.execution_pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT resource_id,provider,provider_reference,kind,operation_keys
                  FROM harness_allocation_resource WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3
                """,
                lease.org_id,
                lease.workspace_id,
                allocation,
            )
        return {
            row["provider_reference"]: AllocationResource(
                row["resource_id"],
                row["provider"],
                row["provider_reference"],
                row["kind"],
                frozenset(row["operation_keys"]),
            )
            for row in rows
        }

    async def require_workspace_provenance(self, operation, resources):
        if "controller_deployment_id" not in operation.request.parameters:
            return
        lease = operation.grant.lease
        source = (
            operation.request.parameters.get("controller_source_operation_id")
            or lease.operation_id
        )
        async with self.provider.execution_pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT provider_reference FROM harness_allocation_resource "
                "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 "
                "AND operation_id=$4 AND provider='aws' AND kind='workspace_object'",
                lease.org_id,
                lease.workspace_id,
                operation.request.parameters["allocation_id"],
                source,
            )
        originals = {row["provider_reference"] for row in rows}
        if any(
            resource.kind == "workspace_object" and reference not in originals
            for reference, resource in resources.items()
        ):
            # Keep allocation-wide membership for exposure, but do not let a
            # different operation's row become this deployment's original UID.
            raise OperationRefused("original workload UID provenance unavailable")

    async def discover(self, operation, target, plan, calls):
        lease = operation.grant.lease
        resources = await self.known(
            lease, operation.request.parameters["allocation_id"]
        )
        await self.require_workspace_provenance(operation, resources)
        creating = {
            call["operation_kind"]: call["idempotency_key"]
            for call in calls
            if call["operation_kind"]
            in ("launch", "deploy", "run-node-bootstrap", "run-node-probe")
        }

        def add(reference, kind, operation_kind, region=None):
            if region is not None and plan.data["version"] == 4:
                reference = plan.resource_reference(kind, reference, region)
            keys = (
                frozenset({creating[operation_kind]})
                if operation_kind in creating
                else frozenset()
            )
            if reference not in resources:
                resources[reference] = AllocationResource(
                    reference, "aws", reference, kind, keys
                )

        instances = await self.provider.instances(
            operation, plan, include_terminated=True
        )
        for instance in instances:
            add(
                instance["InstanceId"],
                "instance",
                "launch",
                instance["SuperplaneRegion"],
            )
            for block in instance.get("BlockDeviceMappings", []):
                if "Ebs" in block:
                    add(
                        block["Ebs"]["VolumeId"],
                        "volume",
                        "launch",
                        instance["SuperplaneRegion"],
                    )
            for interface in instance.get("NetworkInterfaces", []):
                add(
                    interface["NetworkInterfaceId"],
                    "network_interface",
                    "launch",
                    instance["SuperplaneRegion"],
                )
                if allocation := interface.get("Association", {}).get("AllocationId"):
                    add(allocation, "address", "launch", instance["SuperplaneRegion"])
        from .node_inventory import discover as discover_nodes

        resources.update(
            await discover_nodes(
                self.provider, operation, target, plan, resources, instances
            )
        )
        for obj in self.provider.workspace.objects(operation, target, plan):
            response = await self.provider.workspace.request(
                operation,
                target,
                "GET",
                self.provider.workspace.path(
                    target, obj["kind"], obj["metadata"]["name"]
                ),
            )
            if response.status_code == 404:
                continue
            if (
                response.status_code != 200
                or response.json()
                .get("metadata", {})
                .get("labels", {})
                .get("superplane.ai/capacity")
                != plan.cluster_name
            ):
                raise OperationRefused("workspace resource enumeration unavailable")
            reference = self.provider.workspace.reference(obj["kind"], response.json())
            if (
                "controller_deployment_id" in operation.request.parameters
                and reference not in resources
            ):
                # Only the successful POST response may establish a V2 object's
                # identity. A matching name/capacity label in a later GET cannot
                # recover a lost acknowledgement or adopt a replacement UID.
                raise OperationRefused("original workload UID evidence unavailable")
            add(reference, "workspace_object", "deploy")

        # Tag enumeration also catches newly detached or leaked volumes/interfaces
        # returned by AWS. Observations then query every persisted identity, including
        # resources that no longer appear on an instance's attachments.
        session, _ = await self.provider.session_for(operation, plan)

        def tagged(region):
            ec2 = session.client("ec2", region_name=region)
            filters = [
                {"Name": "tag:superplane-capacity", "Values": [plan.cluster_name]}
            ]
            found = []
            for method, result, key, kind in (
                ("describe_volumes", "Volumes", "VolumeId", "volume"),
                (
                    "describe_network_interfaces",
                    "NetworkInterfaces",
                    "NetworkInterfaceId",
                    "network_interface",
                ),
            ):
                for page in ec2.get_paginator(method).paginate(Filters=filters):
                    found.extend((item[key], kind) for item in page[result])
            for item in ec2.describe_addresses(Filters=filters)["Addresses"]:
                found.append((item["AllocationId"], "address"))
            return found

        # Every approved region is scanned, not only the one the plan's flat
        # fields might otherwise suggest. A lost launch reply must be
        # discoverable no matter which approved region SkyPilot actually used.
        for binding in plan.region_bindings:
            for reference, kind in await asyncio.to_thread(tagged, binding["region"]):
                add(reference, kind, "launch", binding["region"])
        if plan.network is not None:
            from .network_inventory import discover

            network = await discover(
                self.provider,
                operation,
                frozenset({creating["launch"]})
                if "launch" in creating
                else frozenset(),
            )
            for reference, resource in network.items():
                resources.setdefault(reference, resource)
        if plan.node_bootstrap is not None:
            from .node_command_inventory import discover

            resources.update(await discover(self.provider, operation))
        return resources

    async def observe(self, operation, target, plan, resource):
        if resource.kind == "kubernetes_node":
            from .node_inventory import observe

            return await observe(self.provider, operation, target, plan, resource)
        if resource.kind == "node_command":
            from .node_command_inventory import observe

            return await observe(self.provider, operation, plan, resource)
        if resource.kind == "network_dependency":
            from .network_inventory import observe

            return await observe(self.provider, operation, plan, resource)
        ref = resource.provider_reference
        if resource.kind == "workspace_object":
            try:
                _, kind, namespace, name, uid = ref.split(":")
                if namespace != target["namespace"]:
                    raise ValueError("namespace mismatch")
                response = await self.provider.workspace.request(
                    operation,
                    target,
                    "GET",
                    self.provider.workspace.path(target, kind, name),
                )
                if response.status_code == 200:
                    if response.json()["metadata"]["uid"] != uid:
                        return ResourceObservation(
                            ResourcePresence.UNKNOWN,
                            ref,
                            detail="workspace identity replaced",
                        )
                    return ResourceObservation(ResourcePresence.PRESENT, ref, "present")
                if response.status_code != 404:
                    raise ValueError("workspace read refused")
                # Foreground root deletion must also finish its dependent pods.
                selector = quote("superplane.ai/capacity=" + plan.cluster_name, safe="")
                pods = await self.provider.workspace.request(
                    operation,
                    target,
                    "GET",
                    self.provider.workspace.path(target, "Pod")
                    + "?labelSelector="
                    + selector
                    + "&limit=257",
                )
                if pods.status_code != 200:
                    raise ValueError("dependent pod read unavailable")
                listing = pods.json()
                if (
                    not isinstance(listing, dict)
                    or not isinstance(listing.get("metadata", {}), dict)
                    or listing.get("metadata", {}).get("continue")
                    or not isinstance(listing.get("items"), list)
                    or len(listing["items"]) > 256
                    or any(not isinstance(pod, dict) for pod in listing["items"])
                ):
                    raise ValueError("complete bounded dependent pod listing required")
                if listing["items"]:
                    return ResourceObservation(
                        ResourcePresence.PRESENT, ref, "dependent_pods_present"
                    )
                return ResourceObservation(ResourcePresence.ABSENT, ref)
            except Exception:
                return ResourceObservation(
                    ResourcePresence.UNKNOWN,
                    ref,
                    detail="workspace observation unavailable",
                )

        session, _ = await self.provider.session_for(operation, plan)

        raw_ref = ref
        regions = [binding["region"] for binding in plan.region_bindings]
        if ref.startswith("arn:"):
            parts = ref.split(":", 5)
            if (
                len(parts) != 6
                or parts[:3] != ["arn", "aws", "ec2"]
                or parts[3] not in regions
                or parts[4] != plan.data["provider_account_id"]
                or not parts[5].startswith(
                    (
                        resource.kind.replace("_", "-")
                        if resource.kind != "address"
                        else "elastic-ip"
                    )
                    + "/"
                )
            ):
                return ResourceObservation(
                    ResourcePresence.UNKNOWN, ref, detail="foreign regional identity"
                )
            regions = [parts[3]]
            raw_ref = parts[5].split("/", 1)[1]
        elif plan.data["version"] == 4:
            # Never adopt ambiguous historical bare IDs into a regional allocation.
            return ResourceObservation(
                ResourcePresence.UNKNOWN, ref, detail="regional identity missing"
            )

        def cloud(region):
            ec2 = session.client("ec2", region_name=region)
            methods = {
                "instance": (
                    "describe_instances",
                    "InstanceIds",
                    "Reservations",
                    "InvalidInstanceID.NotFound",
                ),
                "volume": (
                    "describe_volumes",
                    "VolumeIds",
                    "Volumes",
                    "InvalidVolume.NotFound",
                ),
                "network_interface": (
                    "describe_network_interfaces",
                    "NetworkInterfaceIds",
                    "NetworkInterfaces",
                    "InvalidNetworkInterfaceID.NotFound",
                ),
                "address": (
                    "describe_addresses",
                    "AllocationIds",
                    "Addresses",
                    "InvalidAllocationID.NotFound",
                ),
            }
            method, argument, result, missing = methods[resource.kind]
            try:
                items = getattr(ec2, method)(**{argument: [raw_ref]})[result]
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == missing:
                    return ResourceObservation(ResourcePresence.ABSENT, ref)
                raise
            if resource.kind == "instance":
                items = [
                    instance
                    for reservation in items
                    for instance in reservation["Instances"]
                ]
            identity_key = {
                "instance": "InstanceId",
                "volume": "VolumeId",
                "network_interface": "NetworkInterfaceId",
                "address": "AllocationId",
            }[resource.kind]
            if len(items) != 1 or items[0].get(identity_key) != raw_ref:
                return ResourceObservation(
                    ResourcePresence.UNKNOWN,
                    ref,
                    detail="exact provider identity unavailable",
                )
            if resource.kind == "instance":
                if items[0]["State"]["Name"] == "terminated":
                    return ResourceObservation(ResourcePresence.ABSENT, ref)
            state = items[0].get("State", items[0].get("Status", "present"))
            if isinstance(state, dict):
                state = state["Name"]
            return ResourceObservation(ResourcePresence.PRESENT, ref, str(state))

        # v4 ARNs select the original account-qualified region. Legacy bare
        # IDs use the legacy plan binding. Failed reads remain UNKNOWN so a
        # missing observation cannot authorize cleanup completion.
        try:
            observations = [
                await asyncio.to_thread(cloud, region) for region in regions
            ]
        except Exception:
            return ResourceObservation(
                ResourcePresence.UNKNOWN, ref, detail="provider observation unavailable"
            )
        present = next(
            (o for o in observations if o.presence is ResourcePresence.PRESENT), None
        )
        if present is not None:
            return present
        if any(o.presence is ResourcePresence.UNKNOWN for o in observations):
            return ResourceObservation(
                ResourcePresence.UNKNOWN, ref, detail="provider observation unavailable"
            )
        return ResourceObservation(ResourcePresence.ABSENT, ref)

    async def query(self, lease, resources, query_id):
        operation, target, plan = await self.context(lease.operation_id)
        if (
            operation.grant.lease.attempt_id,
            operation.grant.lease.fence_token,
            operation.grant.lease.holder,
        ) != (lease.attempt_id, lease.fence_token, lease.holder):
            raise OperationRefused("inventory query authority changed")
        return {
            resource.resource_id: await self.observe(operation, target, plan, resource)
            for resource in resources
        }

    def accounting(self, operation, calls, assessment=None):
        # Record the exact dispositions returned by the shared implementation.
        # Source observations are re-read from the shared store, never from Go.
        payload = {
            "checked_at": datetime.now(UTC).isoformat(),
            "operation_id": operation.grant.lease.operation_id,
            "allocation_id": operation.request.parameters["allocation_id"],
            "call_dispositions": {
                call.idempotency_key: disposition_for(call).value for call in calls
            },
            "resource_dispositions": dict(
                (key, value.value) for key, value in assessment.dispositions
            )
            if assessment
            else {},
            "exposure": assessment.exposure.value if assessment else "unresolved",
            "reason": assessment.reason
            if assessment
            else "admitted plan accounting pending",
            "release_permitted": assessment.may_return_reservation_unused
            if assessment
            else False,
            "inventory_complete": bool(
                assessment and assessment.inventory and assessment.inventory.complete
            ),
            "may_mark_released": bool(assessment and assessment.may_mark_released),
            "unresolved_resources": list(assessment.unresolved_resources)
            if assessment
            else [],
        }
        return payload

    async def accounting_with_costs(self, operation, target, calls, assessment=None):
        from .cost_evidence import build_cost_evidence

        payload = self.accounting(operation, calls, assessment)
        resources = await self.known(
            operation.grant.lease, operation.request.parameters["allocation_id"]
        )
        payload["cost_evidence"] = build_cost_evidence(
            operation, Plan.read(operation, target), resources, payload
        )
        return payload

    async def persist(self, operation, target, calls, assessment=None):
        payload = await self.accounting_with_costs(operation, target, calls, assessment)
        # Retiring capacity and publishing a release observation both require
        # the original live fence, including after a slow provider listing.
        async with (
            self.provider.execution_pool.acquire() as execution,
            execution.transaction(),
        ):
            if not await lock_lease(execution, operation.grant.lease):
                raise OperationRefused("accounting authority expired")
            await self._persist_observation(operation, target, payload, assessment)

    async def _persist_observation(self, operation, target, payload, assessment):
        async with (
            self.provider.domain_pool.acquire() as connection,
            connection.transaction(),
        ):
            await connection.execute(
                """
                INSERT INTO controller_execution_accounting(operation_id,org_id,workspace_id,observation)
                VALUES ($1,$2::text::uuid,$3::text::uuid,$4::json)
                ON CONFLICT(operation_id) DO UPDATE SET observation=EXCLUDED.observation
                """,
                operation.grant.lease.operation_id,
                target["domain_org_id"],
                operation.grant.lease.workspace_id,
                json.dumps(payload),
            )
            if assessment and assessment.may_mark_released:
                await connection.execute(
                    "UPDATE controller_capacity SET state='retired' WHERE org_id=$1 AND workspace_id=$2 AND cluster_name=$3",
                    operation.grant.lease.org_id,
                    operation.grant.lease.workspace_id,
                    Plan.read(operation, target).cluster_name,
                )
                if "controller_deployment_id" in operation.request.parameters:
                    # Model quota is released only at the same verified complete
                    # owned-absence boundary as physical capacity retirement.
                    # A successful teardown RPC or operation state is insufficient.
                    await connection.execute(
                        "UPDATE deployments d SET status='Deleted',actual_replicas=0 "
                        "FROM controller_deployment_operations r WHERE r.operation_id=$1 "
                        "AND r.org_id=$2 AND r.workspace_id=$3 AND r.plan_digest=$4 "
                        "AND r.allocation_id=$5 AND d.id::text=r.deployment_id "
                        "AND d.org_id::text=r.org_id AND d.workspace_id::text=r.workspace_id",
                        operation.grant.lease.operation_id,
                        operation.grant.lease.org_id,
                        operation.grant.lease.workspace_id,
                        operation.plan_digest,
                        operation.request.parameters["allocation_id"],
                    )

    async def __call__(self, grant, result):
        operation, target, plan = await self.context(grant.lease.operation_id)
        lease = operation.grant.lease
        if any(
            getattr(lease, field) != getattr(grant.lease, field)
            for field in (
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            )
        ):
            raise OperationRefused("bookkeeping execution binding changed")
        async with self.provider.execution_pool.acquire() as connection:
            record = await OperationStore().get(
                connection, grant.principal, lease.operation_id
            )
            rows = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                lease.operation_id,
            )
            complete = (
                await confirmed_plan_progress(connection, lease.operation_id)
                == PlanProgress.COMPLETE
            )
            calls = [
                await read_call(connection, idempotency_key=row["idempotency_key"])
                for row in rows
            ]
        await self.persist(operation, target, calls)
        resources = await self.discover(operation, target, plan, rows)
        if not resources:
            raise OperationRefused("provider resource inventory is not established")
        async with self.provider.execution_pool.acquire() as connection:
            # Membership is additive. Preserve a seal during retirement; inserting
            # a newly discovered member into a sealed allocation must fail closed.
            sealed = await connection.fetchval(
                "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
                lease.org_id,
                lease.workspace_id,
                record.admitted_request().parameters["allocation_id"],
            )
            if not sealed:
                await self.authority.enumerate_resources(
                    connection, lease, resources=tuple(resources.values())
                )
            if not complete:
                return
            attempt = await self.authority.begin_provider_enumeration(
                connection, lease, provider="aws"
            )
        listing_returned = False
        try:
            # A fresh listing after begin, not a reused pre-intent snapshot.
            fresh = await self.discover(operation, target, plan, rows)
            present = set()
            for reference, resource in fresh.items():
                observation = await self.observe(operation, target, plan, resource)
                if observation.presence is ResourcePresence.UNKNOWN:
                    raise OperationRefused("provider listing incomplete")
                if observation.presence is ResourcePresence.PRESENT:
                    present.add(reference)
            listing_returned = True
            async with self.provider.execution_pool.acquire() as connection:
                await self.authority.record_provider_enumeration(
                    connection,
                    lease,
                    provider="aws",
                    provider_references=frozenset(present),
                    attempt=attempt,
                )
                await self.authority.seal_allocation(connection, lease)
                observations = await self.authority.observe_report(connection, lease)
                await self.authority.publish_report(
                    connection, lease, observations=observations
                )
            token = self.token_for(operation)
            assessment = await self.authority.assess_cleanup(
                executor_id=lease.holder,
                workspace_id=lease.workspace_id,
                allocation_id=operation.request.parameters["allocation_id"],
                operation_authority=token,
                observations=observations,
            )
            await self.persist(operation, target, calls, assessment)
            if not assessment.inventory or not assessment.inventory.complete:
                raise OperationRefused("complete allocation accounting unavailable")
            return assessment
        except Exception:
            if not listing_returned:
                # This coroutine's provider reads have returned unsuccessfully.
                # Cancellation is a BaseException and deliberately leaves the
                # durable in-progress marker for the successor to recover.
                async with self.provider.execution_pool.acquire() as connection:
                    await self.authority.fail_provider_enumeration(
                        connection, lease, attempt=attempt
                    )
            # No release is published on an uncertain listing. Shared receipts,
            # capacity records and prior resource handles remain durable.
            raise OperationRefused(
                "allocation reconciliation requires recovery"
            ) from None

    def token_for(self, operation):
        return next(
            value[0]
            for value in self.registry.tokens.values()
            if value[1]["operation_id"] == operation.grant.lease.operation_id
        )

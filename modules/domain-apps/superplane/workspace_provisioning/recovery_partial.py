"""Classify interrupted mutations from maintained journals without replay or cleanup."""

import json
import hashlib
import re

from harness_jobs.execution import CallOutcome, read_call
from harness_jobs.execution_plan import step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from harness_jobs.store import OperationStore
from superplane_executor.recovery_authority import same_recovery_claim

from .artifacts import digest
from .effects import LifecycleEffects
from .runtime import validate_phase

PARTIAL_PHASES = frozenset(
    {"apply-infrastructure", "bootstrap-workspace", "bootstrap-account"}
)


class PartialLifecycleRecovery:
    def __init__(self, context):
        self.context = context

    async def verified(self, lease, key):
        operation = await self.context.authority.resolve_recovery(lease)
        if not isinstance(operation.grant, RecoveryGrant) or not same_recovery_claim(
            operation.grant.lease, lease
        ):
            raise OperationRefused("partial lifecycle recovery claim changed")
        config, request, authorization, source, step = await validate_phase(
            operation, self.context, require_fresh=False
        )
        if step.step_id not in PARTIAL_PHASES or source is None:
            raise OperationRefused(
                "partial recovery is outside an admitted mutation phase"
            )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, operation.grant):
                raise OperationRefused("partial lifecycle recovery authority expired")
            record = await OperationStore().get(
                connection, operation.grant.principal, lease.operation_id
            )
            call = await read_call(connection, idempotency_key=key)
            keys = await connection.fetch(
                "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
                lease.operation_id,
            )
            if (
                record is None
                or call is None
                or [item["idempotency_key"] for item in keys] != [key]
                or (record.job_id, record.plan_digest, record.request_payload)
                != (operation.job_id, operation.plan_digest, operation.request_payload)
                or key != step_key(record, step)
                or call.fence_token >= lease.fence_token
                or (
                    call.operation_id,
                    call.org_id,
                    call.workspace_id,
                    call.job_id,
                    call.provider,
                    call.operation_kind,
                    call.target,
                )
                != (
                    lease.operation_id,
                    lease.org_id,
                    lease.workspace_id,
                    operation.job_id,
                    step.provider,
                    step.operation_kind,
                    step.target,
                )
            ):
                raise OperationRefused("partial recovery original intent changed")
            if not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_execution_audit WHERE operation_id=$1 "
                "AND org_id=$2 AND workspace_id=$3 AND attempt_id=$4 AND fence_token=$5 "
                "AND event='record_intent' AND allowed)",
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                call.attempt_id,
                call.fence_token,
            ):
                raise OperationRefused("partial recovery has no original intent audit")
        inventory = await self.inventory(
            operation, config, request, authorization, source, step.step_id
        )
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, operation.grant):
                raise OperationRefused("partial recovery expired during journal read")
            if await read_call(connection, idempotency_key=key) != call:
                raise OperationRefused(
                    "partial recovery original call changed during read"
                )
        return operation, call, inventory

    async def inventory(self, operation, config, request, authorization, source, phase):
        metadata = json.loads(source["artifact_metadata_json"])
        recipe = membership = None
        lease = operation.grant.lease
        if phase == "bootstrap-account":
            from .account_runtime import bootstrap_recipe

            _, _, recipe = bootstrap_recipe(
                config,
                request,
                authorization,
                source["account_id"],
                metadata["creation_source_parent_id"],
            )
        elif phase == "bootstrap-workspace":
            from .shared_membership import approved_membership

            outputs = {
                key: value["value"] for key, value in metadata["outputs"].items()
            }
            # validate_phase already validated the immutable request. Select its
            # placement before constructing any dedicated cluster-network recipe.
            membership = approved_membership(
                operation.request.parameters,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
            )
            if membership is None:
                from .network import network_recipe

                recipe = network_recipe(operation, config, outputs)
            elif (
                outputs.get("cluster_arn") != membership.cluster_arn
                or outputs.get("cluster_endpoint") != membership.endpoint
            ):
                raise OperationRefused("partial shared discovery target changed")
        grouped, authority = {}, []
        shared = None
        async with self.context.domain_connect() as connection:
            if recipe is not None:
                journal = LifecycleEffects(
                    operation, self.context, phase=phase, recipe=recipe
                )
                grouped = journal.verify_rows(await journal.rows(connection))
            if membership is not None:
                async with connection.transaction(
                    isolation="repeatable_read", readonly=True
                ):
                    authority, shared = await self.shared_inventory(
                        connection, lease, membership
                    )
            elif phase == "bootstrap-workspace":
                rows = await connection.fetch(
                    "SELECT generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked "
                    "FROM workspace_bootstrap_authority WHERE workspace_id=$1 AND operation_id=$2 LIMIT 129",
                    lease.workspace_id,
                    lease.operation_id,
                )
                if len(rows) > 128:
                    raise OperationRefused(
                        "partial bootstrap authority inventory exceeds bound"
                    )
                for row in rows:
                    if (
                        row["org_id"] != lease.org_id
                        or row["cluster_arn"] != outputs["cluster_arn"]
                    ):
                        raise OperationRefused(
                            "partial bootstrap journal target changed"
                        )
                    authority.append(
                        {
                            "generation": row["generation"],
                            "claim": row["claim"],
                            "plan_sha256": digest(row["plan_json"]),
                            "progress_sha256": digest(row["progress_json"]),
                            "revoked": row["revoked"],
                        }
                    )
        inventory = {
            "phase": phase,
            "source_artifact_id": source["artifact_id"],
            "confirmed_effect_keys": sorted(
                key
                for key, rows in grouped.items()
                if any(row["event"] == "confirmed" for row in rows)
            ),
            "uncertain_effect_keys": sorted(
                key
                for key, rows in grouped.items()
                if all(row["event"] != "confirmed" for row in rows)
            ),
            "authority_generations": sorted(
                authority, key=lambda row: row["generation"]
            ),
            "workflow_complete": False,
            "provider_absence_verified": False,
            "cleanup_authorized": False,
        }
        if shared is not None:
            inventory["shared_membership"] = shared
        return inventory

    async def shared_inventory(self, connection, lease, membership):
        """Classify historical member journals; this grants no provider capability.

        Credential rows alone do not identify a bootstrap operation: the installed
        renewal controller shares that journal. Only revisions referenced by an
        original bootstrap claim enter this inventory.
        """
        from superplane_bootstrap.component_journal import component_key
        from superplane_bootstrap.errors import BootstrapRefused
        from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION
        from superplane_bootstrap.membership import registration_membership
        from superplane_bootstrap.state import claim_fingerprint

        member = await connection.fetchrow(
            "SELECT id,org_id::text,cluster_id::text,operation_id::text,namespace,namespace_uid,state "
            "FROM cluster_memberships WHERE workspace_id::text=$1 AND generation=$2",
            lease.workspace_id,
            membership.generation,
        )
        if member is None or any(
            member[key] != expected
            for key, expected in {
                "org_id": lease.org_id,
                "cluster_id": membership.cluster_id,
                "operation_id": membership.request_id,
                "namespace": membership.namespace,
            }.items()
        ):
            raise OperationRefused("partial shared membership scope changed")
        rows = await connection.fetch(
            "SELECT generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked "
            "FROM workspace_bootstrap_authority WHERE workspace_id=$1 AND operation_id=$2 LIMIT 129",
            lease.workspace_id,
            lease.operation_id,
        )
        if len(rows) > 128:
            raise OperationRefused("partial shared authority inventory exceeds bound")
        reservation = await connection.fetchrow(
            "SELECT identity_json,attempt_token FROM workspace_bootstrap_reservations WHERE workspace_id=$1",
            lease.workspace_id,
        )
        authority, revisions = [], set()
        try:
            for row in rows:
                plan, progress = (
                    json.loads(row["plan_json"]),
                    json.loads(row["progress_json"]),
                )
                if (
                    row["org_id"] != lease.org_id
                    or row["operation_id"] != lease.operation_id
                    or row["cluster_arn"] != membership.cluster_arn
                    or not re.fullmatch(r"[a-f0-9]{64}", row["claim"])
                    or row["generation"]
                    != hashlib.sha256(
                        (lease.operation_id + ":" + row["claim"]).encode()
                    ).hexdigest()
                    or plan["mode"] != "shared-namespace"
                    or plan["membership"] != membership.encode()
                    or len(plan["grants"]) != 1
                ):
                    raise ValueError()
                # A live claim must still be the exact original reservation.
                # Revoked historical claims remain readable after release.
                if not row["revoked"] and (
                    reservation is None
                    or claim_fingerprint(reservation["attempt_token"]) != row["claim"]
                    or registration_membership(json.loads(reservation["identity_json"]))
                    != membership
                ):
                    raise ValueError()
                spec = plan["grants"][0]
                body = spec["body"]
                if (
                    spec["key"] != "workspace-namespace"
                    or spec["kind"] != "kubernetes"
                    or spec["cluster_arn"] != membership.cluster_arn
                    or spec["generation"] != membership.generation
                    or body["kind"] != "Namespace"
                    or body["metadata"]["name"] != membership.namespace
                    or body["metadata"]["annotations"][GENERATION_ANNOTATION]
                    != membership.generation
                ):
                    raise ValueError()
                namespace = progress.get("workspace-namespace", {})
                namespace_uid = namespace.get("identity", {}).get("uid")
                if (
                    member["namespace_uid"]
                    and namespace_uid
                    and (namespace_uid != member["namespace_uid"])
                ):
                    raise ValueError()
                components = progress.get("components", {})
                if len(components) > 128:
                    raise ValueError()
                for key, component in components.items():
                    desired = component["desired"]
                    if (
                        desired["kind"] not in {"ServiceAccount", "Role", "RoleBinding"}
                        or desired["metadata"]["namespace"] != membership.namespace
                        or component_key(desired) != key
                    ):
                        raise ValueError()
                credentials = []
                for scope, revision in progress.get("member_credentials", {}).items():
                    if (
                        scope not in {"reader", "mutator"}
                        or type(revision) is not int
                        or revision < 1
                        or not namespace_uid
                        or (scope, revision) in revisions
                    ):
                        raise ValueError()
                    revisions.add((scope, revision))
                    credential = await connection.fetchrow(
                        "SELECT revision,scope,namespace_uid,state,service_account_uid,projection_uid,content_digest "
                        "FROM membership_credentials WHERE membership_id=$1 AND scope=$2 AND revision=$3",
                        member["id"],
                        scope,
                        revision,
                    )
                    if (
                        credential is None
                        or credential["namespace_uid"] != namespace_uid
                    ):
                        raise ValueError()
                    credentials.append(dict(credential))
                authority.append(
                    {
                        "generation": row["generation"],
                        "claim": row["claim"],
                        "plan_sha256": digest(row["plan_json"]),
                        "progress_sha256": digest(row["progress_json"]),
                        "revoked": row["revoked"],
                        "namespace_uid": namespace_uid,
                        "namespace_phase": namespace.get("phase"),
                        "component_keys": sorted(components),
                        "credentials": sorted(
                            credentials,
                            key=lambda item: (item["scope"], item["revision"]),
                        ),
                    }
                )
        except (KeyError, TypeError, ValueError, AttributeError, BootstrapRefused):
            raise OperationRefused(
                "partial shared bootstrap journal scope changed"
            ) from None
        return authority, {
            "generation": membership.generation,
            "cluster_id": membership.cluster_id,
            "namespace": membership.namespace,
            "state": member["state"],
            "cluster_resources_included": False,
        }

    async def observe(self, lease, key, provider, kind, target):
        _, call, _ = await self.verified(lease, key)
        if (provider, kind, target) != (
            call.provider,
            call.operation_kind,
            call.target,
        ):
            raise OperationRefused("partial recovery descriptor changed")
        return (
            CallOutcome.UNKNOWN,
            "partial mutation requires separately governed cleanup",
            call.provider_ref,
        )

    async def accounting(self, lease, calls):
        if len(calls) != 1 or calls[0].outcome is CallOutcome.SUCCEEDED:
            raise OperationRefused("partial recovery requires its original call")
        operation, call, inventory = await self.verified(
            lease, calls[0].idempotency_key
        )
        if call != calls[0]:
            raise OperationRefused("partial recovery call changed before accounting")
        return {
            "allocation_id": operation.request.parameters["allocation_id"],
            "inventory_complete": False,
            "release_permitted": False,
            "may_mark_released": False,
            "resource_dispositions": {},
            "unresolved_resources": [],
            "exposure": "unresolved",
            "reason": "partial lifecycle mutation retained; separately approved cleanup required",
            "partial_lifecycle": inventory,
        }

"""Publish an owner lease only after the current host has been verified ready."""

import asyncio
import ipaddress
import re
from datetime import UTC, datetime

from botocore.exceptions import ClientError
from sqlalchemy import select

from src.agentauth.chat_crew_run import CrewRunConflictError, CrewRunStore, current_crew_store
from src.cloud_desktop.host_identity import current_host
from src.crew.inventory import CrewLeaseFencedError
from src.crew.targets import CrewLease
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxInstance


def _mirror_key(owner) -> dict:
    return {"PK": f"crew#{owner.tenant_id}#{owner.user_id}", "SK": "executor"}


def clear_host_lease_mirror(table, owner, box_id: str) -> bool:
    """Drop the owner's published lease for ``box_id`` when its host authority is fenced.

    ``fence_host`` revokes the run and terminates the access session in the control plane,
    but the DynamoDB mirror (``crew#<tenant>#<user>`` / ``executor``) kept the pre-stop lease
    with its original expiry. ``publish_host_lease`` then refused every same-generation restart
    as a conflicting live lease (#8223, cause 39). The mirror is a projection of the control
    DB, so the fence clears it: state ``fenced`` (**not** ``revoked``, which is terminal for the
    owner) and no ``crewLease``. Only a lease for this very box is touched; a pod lease or a
    revoked mirror is left alone. Returns whether a lease was cleared.
    """
    if table is None:
        return False
    # PutItem, not UpdateItem: the gateway role's CrewHostLeaseWrite grant on ``crew#*`` keys is
    # PutItem + DeleteItem only (gateway-intake-access.tf), and dev 2026-10-21 showed an UpdateItem
    # here is AccessDenied, which left the stale lease in place (#8223 cause 43). The full item is
    # rewritten without ``crewLease``; the condition still names the exact lease being retracted.
    try:
        table.put_item(
            Item={**_mirror_key(owner), "tenantId": owner.tenant_id, "ownerUserId": owner.user_id, "crewLeaseState": "fenced"},
            ConditionExpression=(
                "attribute_exists(PK) AND tenantId = :tenant AND ownerUserId = :user AND crewLeaseState <> :revoked "
                "AND crewLease.target_kind = :kind AND crewLease.target_ref = :box"
            ),
            ExpressionAttributeValues={
                ":tenant": owner.tenant_id,
                ":user": owner.user_id,
                ":revoked": "revoked",
                ":kind": "ec2-instance",
                ":box": box_id,
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _old_lease_replaceable(store, prior: dict, old_lease, box, run_id: str, now: int) -> bool:
    """Whether the mirrored lease may be overwritten by this host's new lease.

    A lease that is not a mapping, has expired, or was cleared by a fence is replaceable, as
    before. A lease this same box published earlier is replaceable when the run it stood for is
    gone: its generation marker is absent, names this very run, or names a run that is no longer
    active or provisioning (a stop/replace revoked it but an older controller left the mirror).
    A lease for another target (a pod, another box) or for a run that is still live stays a fence.
    """
    if not isinstance(old_lease, dict) or int(old_lease.get("expires_at", 0)) <= now:
        return True
    if prior.get("crewLeaseState") == "fenced":
        return True
    if old_lease.get("target_kind") != "ec2-instance" or old_lease.get("target_ref") != box.id:
        return False
    generation = old_lease.get("generation")
    if generation is None:
        return False
    marker_key = {"event_id": f"crew-lease#{box.org_id}#{box.id}#{int(generation)}", "arrived_at": "crew"}
    marker = store.table.get_item(Key=marker_key, ConsistentRead=True).get("Item")
    if marker is None or marker.get("run_id") == run_id:
        return True
    old_run = store.read_run(marker["run_id"]) if marker.get("run_id") else None
    return old_run is None or old_run.get("status") not in ("active", "provisioning")


async def publish_host_lease(session_factory, table, owner, deployment_id: str, now: int) -> None:
    async with session_factory.begin() as session:
        box = await session.scalar(
            select(DevBox)
            .where(
                DevBox.user_id == owner.user_id,
                DevBox.org_id == owner.tenant_id,
                DevBox.deployment_id == deployment_id,
                DevBox.profile == "crew",
                DevBox.status == "running",
                DevBox.desired_state == "running",
                DevBox.observed_state == "running",
                DevBox.auth_readiness == "ready",
            )
            .with_for_update()
        )
        if box is None:
            raise CrewLeaseFencedError("lease_fenced")
        executor = await session.scalar(
            select(CrewExecutor)
            .where(
                CrewExecutor.tenant_id == owner.tenant_id,
                CrewExecutor.user_id == owner.user_id,
                CrewExecutor.target_kind == "ec2-instance",
                CrewExecutor.dev_box_id == box.id,
                CrewExecutor.generation == box.machine_generation,
                CrewExecutor.state.in_(("provisioning", "ready")),
            )
            .with_for_update()
        )
        instance = await session.scalar(
            select(DevBoxInstance).where(
                DevBoxInstance.box_id == box.id,
                DevBoxInstance.machine_generation == box.machine_generation,
                DevBoxInstance.observed_state == "running",
                DevBoxInstance.readiness_class == "crew_ready",
            )
        )
        access = await session.scalar(
            select(DevBoxAccessSession).where(
                DevBoxAccessSession.box_id == box.id,
                DevBoxAccessSession.user_id == owner.user_id,
                DevBoxAccessSession.org_id == owner.tenant_id,
                DevBoxAccessSession.deployment_id == deployment_id,
                DevBoxAccessSession.machine_generation == box.machine_generation,
                DevBoxAccessSession.terminated_at.is_(None),
                DevBoxAccessSession.expires_at > datetime.fromtimestamp(now, UTC),
            )
        )
        if (
            executor is None
            or instance is None
            or access is None
            or not access.provider_session_ref
            or not instance.provider_instance_id
            or not isinstance(instance.certificate_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", instance.certificate_fingerprint)
            or await current_host(
                session,
                box_id=box.id,
                generation=box.machine_generation,
                provider_instance_id=instance.provider_instance_id,
                user_id=owner.user_id,
                org_id=owner.tenant_id,
                deployment_id=deployment_id,
                run_id=access.provider_session_ref,
            )
            is None
        ):
            raise CrewLeaseFencedError("lease_fenced")
        try:
            address = ipaddress.IPv4Address(instance.private_address)
        except ipaddress.AddressValueError:
            raise CrewLeaseFencedError("lease_fenced") from None
        if not any(
            address in network
            for network in (
                ipaddress.IPv4Network("10.0.0.0/8"),
                ipaddress.IPv4Network("172.16.0.0/12"),
                ipaddress.IPv4Network("192.168.0.0/16"),
            )
        ):
            raise CrewLeaseFencedError("lease_fenced")
        key = _mirror_key(owner)
        prior = await asyncio.to_thread(table.get_item, Key=key, ConsistentRead=True)
        prior = prior.get("Item")
        if prior is not None and (
            prior.get("tenantId") != owner.tenant_id or prior.get("ownerUserId") != owner.user_id or prior.get("crewLeaseState") == "revoked"
        ):
            raise CrewLeaseFencedError("lease_fenced")
        store = current_crew_store()
        run = await asyncio.to_thread(store.read_run, access.provider_session_ref)
        expiry = min(
            int(access.expires_at.replace(tzinfo=access.expires_at.tzinfo or UTC).timestamp()), int(run.get("lease_expires_at", 0)) if run else 0
        )
        if (
            run is None
            or run.get("status") not in ("active", "provisioning")
            or run.get("tenant_id") != owner.tenant_id
            or run.get("user_id") != owner.user_id
            or run.get("root_human_id") != owner.user_id
            or run.get("executor_scope") != "session"
            or run.get("session_id") != box.id
            or run.get("lease_generation") != box.machine_generation
            or run.get("target_kind") != "ec2-instance"
            or run.get("target_ref") != instance.provider_instance_id
            or run.get("deployment_id") != deployment_id
            or not run.get("host_role_arn")
            or expiry <= now
            or (executor.state == "ready" and executor.lease_generation != box.machine_generation)
        ):
            raise CrewLeaseFencedError("lease_fenced")
        lease = CrewLease(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            target_kind="ec2-instance",
            target_ref=box.id,
            generation=box.machine_generation,
            expires_at=expiry,
            endpoint=f"https://{address}:8443",
        ).model_dump()
        old_lease = prior.get("crewLease") if prior else None
        replaceable = bool(prior) and old_lease != lease
        if replaceable and not await asyncio.to_thread(_old_lease_replaceable, store, prior, old_lease, box, access.provider_session_ref, now):
            raise CrewLeaseFencedError("lease_fenced")
        if run["status"] == "provisioning":
            host_store = CrewRunStore(store.table, host_role_arn=run["host_role_arn"])
            try:
                await asyncio.to_thread(
                    host_store.activate_for_host,
                    run_id=access.provider_session_ref,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    session_id=box.id,
                    lease_generation=box.machine_generation,
                    target_ref=instance.provider_instance_id,
                    deployment_id=deployment_id,
                    now=now,
                )
            except CrewRunConflictError:
                raise CrewLeaseFencedError("lease_fenced") from None
        if prior is None or prior.get("crewLeaseState") != "ready" or old_lease != lease:
            try:
                await asyncio.to_thread(
                    table.put_item,
                    Item={**key, "tenantId": owner.tenant_id, "ownerUserId": owner.user_id, "crewLeaseState": "ready", "crewLease": lease},
                    # The read above decided the old lease is replaceable; the write still demands that
                    # nothing changed the mirror in between (same old lease, or none after a fence).
                    ConditionExpression=(
                        "attribute_not_exists(PK) OR (tenantId = :tenant AND ownerUserId = :user "
                        "AND (crewLease = :old OR attribute_not_exists(crewLease)))"
                        if replaceable
                        else "attribute_not_exists(PK) OR (tenantId = :tenant AND ownerUserId = :user "
                        "AND crewLease = :old AND (crewLease.expires_at <= :now OR "
                        "crewLeaseState = :provisioning))"
                    ),
                    # DynamoDB rejects a value the expression does not use (ValidationException), so
                    # each branch carries exactly its own values (#8223 cause 43: every GET /crew/target
                    # was a 500 once the replaceable branch dropped :now/:provisioning).
                    ExpressionAttributeValues={
                        ":tenant": owner.tenant_id,
                        ":user": owner.user_id,
                        ":old": old_lease or {},
                        **({} if replaceable else {":now": now, ":provisioning": "provisioning"}),
                    },
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise CrewLeaseFencedError("lease_fenced") from None
                raise
        executor.state = "ready"
        executor.target_ref = box.id
        executor.lease_generation = box.machine_generation
        executor.lease_expires_at = expiry


async def retire_pod_lease(pod: CrewExecutor) -> None:
    from src.orchestration.intake_wiring import _get_context_table

    table = _get_context_table()
    if table is None:
        raise CrewLeaseFencedError("pod lease authority unavailable")
    now = int(datetime.now(UTC).timestamp())
    owner_key = {"event_id": f"crew-owner#{pod.tenant_id}#{pod.user_id}", "arrived_at": "crew"}
    lease_key = {"PK": f"crew#{pod.tenant_id}#{pod.user_id}", "SK": "executor"}
    store = current_crew_store()
    owner = await asyncio.to_thread(store.table.get_item, Key=owner_key, ConsistentRead=True)
    owner = owner.get("Item")
    lease = await asyncio.to_thread(table.get_item, Key=lease_key, ConsistentRead=True)
    lease = lease.get("Item")
    if lease is not None:
        record = lease.get("crewLease")
        if (
            lease.get("tenantId") != pod.tenant_id
            or lease.get("ownerUserId") != pod.user_id
            or not isinstance(record, dict)
            or record.get("target_kind") != "eks-pod"
            or record.get("tenant_id") != pod.tenant_id
            or record.get("user_id") != pod.user_id
            or record.get("generation") != pod.generation
            or (pod.state == "ready" and record.get("target_ref") != pod.target_ref)
        ):
            raise CrewLeaseFencedError("pod owner lease changed")
    if owner is None and pod.lease_generation is not None and (pod.lease_expires_at is None or pod.lease_expires_at > now):
        raise CrewLeaseFencedError("pod run cannot be proven fenced")
    if owner is not None:
        if (
            owner.get("generation") != pod.generation
            or owner.get("executor_scope") != "user"
            or owner.get("target_kind") != "eks-pod"
            or owner.get("session_count") != 1
            or (pod.state == "ready" and owner.get("pod_uid") != pod.target_ref)
            or not isinstance(owner.get("run_id"), str)
        ):
            raise CrewLeaseFencedError("pod run cannot be fenced")
        row = await asyncio.to_thread(store.read_run, owner["run_id"])
        if (
            row is None
            or row.get("tenant_id") != pod.tenant_id
            or row.get("user_id") != pod.user_id
            or row.get("root_human_id") != pod.user_id
            or row.get("target_kind") != "eks-pod"
            or row.get("executor_scope") != "user"
            or row.get("lease_generation") != pod.generation
            or row.get("status") not in ("active", "provisioning")
            or (pod.state == "ready" and row.get("pod_uid") != pod.target_ref)
            or row.get("target_ref") != owner.get("target_ref")
        ):
            raise CrewLeaseFencedError("pod run changed")
        try:
            revoked = await asyncio.to_thread(
                store.revoke_for_lease,
                tenant_id=pod.tenant_id,
                session_id=row["session_id"],
                lease_generation=pod.generation,
                expected_run_id=owner["run_id"],
            )
        except CrewRunConflictError:
            raise CrewLeaseFencedError("pod run changed during revocation") from None
        if not revoked or await asyncio.to_thread(store.read_run, owner["run_id"]) is not None:
            raise CrewLeaseFencedError("pod run could not be fenced")
        remaining = await asyncio.to_thread(store.table.get_item, Key=owner_key, ConsistentRead=True)
        if remaining.get("Item") is not None:
            raise CrewLeaseFencedError("pod owner run could not be fenced")
    if lease is not None:
        try:
            await asyncio.to_thread(
                table.delete_item,
                Key=lease_key,
                ConditionExpression="tenantId = :tenant AND ownerUserId = :user AND crewLease = :lease",
                ExpressionAttributeValues={":tenant": pod.tenant_id, ":user": pod.user_id, ":lease": lease["crewLease"]},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise CrewLeaseFencedError("pod lease changed during revocation") from None
            raise

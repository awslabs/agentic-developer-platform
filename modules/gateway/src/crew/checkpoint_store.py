"""Owner-scoped checkpoint admission and immutable helper candidate binding."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from src.crew.checkpoint_identity import CheckpointHelperIdentity
from src.crew.checkpoint_objects import COMPATIBLE_CREW_VERSION, HEX_SHA256, InvalidCheckpointObjectsError
from src.crew.conversation_index import OWNER_SHELL_PREFIXES
from src.crew.inventory import CrewExecutorInventory
from src.crew.targets import CrewOwner, CrewRunBinding
from src.shared.models.base import utcnow
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_conversation import CrewConversation
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxInstance, DevBoxPolicy, DevBoxTemplate


class CheckpointConflictError(Exception):
    pass


class CheckpointMissingError(Exception):
    pass


class CrewCheckpointStore:
    def __init__(self, session_factory, *, retention_seconds: int | None = None):
        if retention_seconds is not None and (type(retention_seconds) is not int or retention_seconds <= 0):
            raise ValueError("positive checkpoint retention required")
        self.session_factory = session_factory
        self.retention_period = timedelta(seconds=retention_seconds) if retention_seconds is not None else None

    async def admit(self, owner: CrewOwner, key: str, *, expected_predecessor_id: str | None, now: int | None = None, with_creation: bool = False):
        now = int(time.time()) if now is None else now
        if not key or len(key) > 128:
            raise CheckpointConflictError("invalid idempotency key")
        async with self.session_factory.begin() as session:
            executor = await session.scalar(
                select(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.state == "ready",
                )
                .with_for_update()
            )
            if (
                executor is None
                or not executor.owner_key
                or executor.lease_generation != executor.generation
                or (executor.lease_expires_at is None or executor.lease_expires_at <= now)
            ):
                raise CheckpointConflictError("executor unavailable")
            prior = await session.scalar(
                select(CrewCheckpoint).where(
                    CrewCheckpoint.tenant_id == owner.tenant_id,
                    CrewCheckpoint.user_id == owner.user_id,
                    CrewCheckpoint.generation == executor.generation,
                    CrewCheckpoint.idempotency_key == key,
                )
            )
            if prior is not None:
                return (prior, False) if with_creation else prior
            if self.retention_period is None:
                raise CheckpointConflictError("checkpoint retention policy unavailable")
            if executor.latest_verified_checkpoint_id != expected_predecessor_id:
                raise CheckpointConflictError("checkpoint predecessor changed")
            sequence = await session.scalar(
                select(func.coalesce(func.max(CrewCheckpoint.sequence), 0) + 1).where(
                    CrewCheckpoint.tenant_id == owner.tenant_id,
                    CrewCheckpoint.user_id == owner.user_id,
                )
            )
            checkpoint = CrewCheckpoint(
                executor_id=executor.id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                owner_key=executor.owner_key,
                generation=executor.generation,
                target_kind=executor.target_kind,
                sequence=sequence,
                idempotency_key=key,
                predecessor_id=executor.latest_verified_checkpoint_id,
            )
            session.add(checkpoint)
            await session.flush()
            return (checkpoint, True) if with_creation else checkpoint

    async def list_for_owner(self, owner: CrewOwner):
        async with self.session_factory() as session:
            records = (
                await session.scalars(
                    select(CrewCheckpoint)
                    .where(
                        CrewCheckpoint.tenant_id == owner.tenant_id,
                        CrewCheckpoint.user_id == owner.user_id,
                    )
                    .order_by(CrewCheckpoint.sequence.desc())
                    .limit(100)
                )
            ).all()
            latest = await session.scalar(
                select(CrewExecutor.latest_verified_checkpoint_id).where(
                    CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted"
                )
            )
            return [self._public_record(record, latest) for record in records]

    async def coverage_for_owner(self, owner: CrewOwner):
        async with self.session_factory() as session:
            executor = await session.scalar(
                select(CrewExecutor).where(
                    CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted"
                )
            )
            verified = None
            if executor is not None and executor.latest_verified_checkpoint_id:
                verified = await session.scalar(
                    select(CrewCheckpoint).where(
                        CrewCheckpoint.id == executor.latest_verified_checkpoint_id,
                        CrewCheckpoint.tenant_id == owner.tenant_id,
                        CrewCheckpoint.user_id == owner.user_id,
                        CrewCheckpoint.status == "verified",
                    )
                )
            candidate = (
                await session.scalar(
                    select(CrewCheckpoint)
                    .where(
                        CrewCheckpoint.tenant_id == owner.tenant_id,
                        CrewCheckpoint.user_id == owner.user_id,
                        CrewCheckpoint.executor_id == executor.id,
                        CrewCheckpoint.generation == executor.generation,
                        CrewCheckpoint.status.in_(("uploading", "failed")),
                        CrewCheckpoint.sequence > (verified.sequence if verified is not None else 0),
                    )
                    .order_by(CrewCheckpoint.sequence.desc())
                    .limit(1)
                )
                if executor is not None
                else None
            )
            captured_at = verified.captured_at if verified is not None else None
            verified_at = verified.verified_at if verified is not None else None
            if captured_at is not None and captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=UTC)
            if verified_at is not None and verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=UTC)
            return {
                "coverage": "last_verified_capture" if captured_at is not None else "none",
                "last_verified_checkpoint_id": verified.id if verified is not None else None,
                "covered_through": captured_at.isoformat() if captured_at is not None else None,
                "verified_at": verified_at.isoformat() if verified_at is not None else None,
                "pending_checkpoint_id": candidate.id if candidate is not None else None,
                "pending_status": candidate.status if candidate is not None else None,
                "unsynced_writes_durable": False,
                "local_state_examined": False,
            }

    async def selection_for_owner(self, owner: CrewOwner):
        async with self.session_factory() as session:
            executor = await session.scalar(
                select(CrewExecutor).where(
                    CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted"
                )
            )
            record = None
            if executor is not None and executor.latest_verified_checkpoint_id:
                record = await session.scalar(
                    select(CrewCheckpoint).where(
                        CrewCheckpoint.id == executor.latest_verified_checkpoint_id,
                        CrewCheckpoint.tenant_id == owner.tenant_id,
                        CrewCheckpoint.user_id == owner.user_id,
                        CrewCheckpoint.status == "verified",
                    )
                )
            if record is not None and (
                record.crew_version != COMPATIBLE_CREW_VERSION
                or record.schema_version != 1
                or any(
                    not getattr(record, f"{label}_{field}")
                    for label in ("archive", "manifest")
                    for field in ("bucket", "key", "version_id", "sha256", "size")
                )
                or any(getattr(record, f"{label}_version_id") == "null" for label in ("archive", "manifest"))
            ):
                record = None
            captured_at = record.captured_at if record is not None else None
            verified_at = record.verified_at if record is not None else None
            if captured_at is not None and captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=UTC)
            if verified_at is not None and verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=UTC)
            return {
                "checkpoint_status": "verified" if record is not None else "none",
                "checkpoint_id": record.id if record is not None else None,
                "captured_at": captured_at.isoformat() if captured_at is not None else None,
                "verified_at": verified_at.isoformat() if verified_at is not None else None,
                "sequence": record.sequence if record is not None else None,
                "source_generation": record.generation if record is not None else None,
                "source_target_kind": record.target_kind if record is not None else None,
                "current_generation": executor.generation if executor is not None else None,
                "current_target_kind": executor.target_kind if executor is not None else None,
                "executor_state": executor.state if executor is not None else None,
                "crew_version": record.crew_version if record is not None else None,
                "schema_version": record.schema_version if record is not None else None,
                "activation_verified": False,
            }

    async def status_for_owner(self, owner: CrewOwner, checkpoint_id: str):
        async with self.session_factory() as session:
            record = await session.scalar(
                select(CrewCheckpoint).where(
                    CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id
                )
            )
            if record is None:
                raise CheckpointMissingError("checkpoint unavailable")
            latest = await session.scalar(
                select(CrewExecutor.latest_verified_checkpoint_id).where(
                    CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted"
                )
            )
            return self._public_record(record, latest)

    @staticmethod
    def _public_record(record, latest):
        return {
            "id": record.id,
            "status": record.status,
            "sequence": record.sequence,
            "generation": record.generation,
            "target_kind": record.target_kind,
            "captured_at": record.captured_at.isoformat() if record.captured_at else None,
            "verified_at": record.verified_at.isoformat() if record.verified_at else None,
            "archive_size": record.archive_size,
            "failure_class": record.failure_class if record.failure_class == "verification_failed" else None,
            "is_latest_verified": record.status == "verified" and record.id == latest,
        }

    async def restore_owner(self, checkpoint_id: str, identity: CheckpointHelperIdentity, *, require_selected: bool = True):
        if checkpoint_id != identity.checkpoint_id or hashlib.sha256(checkpoint_id.encode()).hexdigest() != identity.checkpoint_hash:
            raise CheckpointMissingError("checkpoint unavailable")
        async with self.session_factory() as session:
            predicate = [CrewExecutor.owner_key == identity.owner_key, CrewExecutor.state != "deleted"]
            if require_selected:
                predicate.append(CrewExecutor.latest_verified_checkpoint_id == checkpoint_id)
            executor = await session.scalar(select(CrewExecutor).where(*predicate))
            if executor is None:
                raise CheckpointMissingError("checkpoint unavailable")
            return CrewOwner(executor.tenant_id, executor.user_id)

    async def start_host_restore(self, owner: CrewOwner, checkpoint_id: str, *, box_id: str, generation: int, deployment_id: str):
        if not box_id or not deployment_id or type(generation) is not int or generation < 1:
            raise CheckpointConflictError("host restore scope unavailable")
        token = secrets.token_urlsafe(32)
        async with self.session_factory.begin() as session:
            from src.cloud_desktop.host_identity import ec2_crew_allowed

            permitted = await ec2_crew_allowed(session, owner.tenant_id)
            policy = await session.scalar(
                select(DevBoxPolicy).where(
                    DevBoxPolicy.org_id == owner.tenant_id,
                    DevBoxPolicy.deployment_id == deployment_id,
                    DevBoxPolicy.enabled.is_(True),
                )
            )
            executor = await session.scalar(
                select(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.state != "deleted",
                )
                .with_for_update()
            )
            box = await session.scalar(
                select(DevBox)
                .where(
                    DevBox.id == box_id,
                    DevBox.user_id == owner.user_id,
                    DevBox.org_id == owner.tenant_id,
                    DevBox.deployment_id == deployment_id,
                    DevBox.profile == "crew",
                    DevBox.status != "released",
                    DevBox.desired_state == "running",
                    DevBox.observed_state == "running",
                    DevBox.machine_generation == generation,
                )
                .with_for_update()
            )
            template = await session.get(DevBoxTemplate, box.template_id) if box is not None else None
            instance = await session.scalar(
                select(DevBoxInstance).where(
                    DevBoxInstance.box_id == box_id,
                    DevBoxInstance.user_id == owner.user_id,
                    DevBoxInstance.org_id == owner.tenant_id,
                    DevBoxInstance.deployment_id == deployment_id,
                    DevBoxInstance.machine_generation == generation,
                    DevBoxInstance.observed_state == "running",
                )
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(
                    CrewCheckpoint.id == checkpoint_id,
                    CrewCheckpoint.tenant_id == owner.tenant_id,
                    CrewCheckpoint.user_id == owner.user_id,
                    CrewCheckpoint.status == "verified",
                )
                .with_for_update()
            )
            if (
                not permitted
                or policy is None
                or template is None
                or not template.approved
                or template.profile != "crew"
                or (box is not None and box.template_revision != template.revision)
                or executor is None
                or executor.target_kind != "ec2-instance"
                or executor.dev_box_id != box_id
                or executor.generation != generation
                or not executor.owner_key
                or executor.latest_verified_checkpoint_id != checkpoint_id
                or box is None
                or instance is None
                or not instance.provider_instance_id
                or instance.template_revision != box.template_revision
                or record is None
                or record.restore_pins
                or record.crew_version != COMPATIBLE_CREW_VERSION
                or record.schema_version != 1
                or any(
                    getattr(record, f"{label}_bucket") is None
                    or getattr(record, f"{label}_key") != f"crew-checkpoints/{record.owner_key}/{record.id}/{label}"
                    or getattr(record, f"{label}_version_id") in (None, "null")
                    or not HEX_SHA256.fullmatch(getattr(record, f"{label}_sha256") or "")
                    or not getattr(record, f"{label}_size")
                    for label in ("archive", "manifest")
                )
            ):
                raise CheckpointConflictError("host restore unavailable")
            record.restore_pins = 1
            record.restore_box_id = box_id
            record.restore_machine_generation = generation
            record.restore_token_hash = hashlib.sha256(token.encode()).hexdigest()
            return record, token

    async def end_host_restore(self, owner: CrewOwner, checkpoint_id: str, *, box_id: str, generation: int, token: str):
        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(
                    CrewCheckpoint.id == checkpoint_id,
                    CrewCheckpoint.tenant_id == owner.tenant_id,
                    CrewCheckpoint.user_id == owner.user_id,
                )
                .with_for_update()
            )
            if (
                record is None
                or record.restore_pins != 1
                or record.restore_box_id != box_id
                or record.restore_machine_generation != generation
                or not record.restore_token_hash
                or not isinstance(token, str)
                or not secrets.compare_digest(record.restore_token_hash, hashlib.sha256(token.encode()).hexdigest())
            ):
                raise CheckpointMissingError("host restore pin unavailable")
            record.restore_pins = 0
            record.restore_box_id = None
            record.restore_machine_generation = None
            record.restore_token_hash = None

    async def start_restore(self, owner: CrewOwner, checkpoint_id: str, *, owner_key: str, pod_uid="", pod_name="", pod_is_gone=None):
        token = secrets.token_urlsafe(32)
        async with self.session_factory.begin() as session:
            executor = await session.scalar(
                select(CrewExecutor)
                .where(CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted")
                .with_for_update()
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id)
                .with_for_update()
            )
            if record is None:
                raise CheckpointMissingError("checkpoint unavailable")
            if record.restore_pins:
                if (
                    not record.restore_pod_uid
                    or not record.restore_pod_name
                    or pod_is_gone is None
                    or not await asyncio.to_thread(pod_is_gone, record.restore_pod_name, record.restore_pod_uid)
                ):
                    raise CheckpointConflictError("restore pin remains active")
            if (
                executor is None
                or executor.owner_key != owner_key
                or executor.latest_verified_checkpoint_id != record.id
                or record.status != "verified"
                or record.crew_version != COMPATIBLE_CREW_VERSION
                or record.schema_version != 1
                or any(
                    not getattr(record, f"{label}_{field}")
                    for label in ("archive", "manifest")
                    for field in ("bucket", "key", "version_id", "sha256", "size")
                )
            ):
                raise CheckpointConflictError("checkpoint cannot be restored")
            record.restore_pins = 1
            record.restore_pod_uid = pod_uid or None
            record.restore_pod_name = pod_name or None
            record.restore_token_hash = hashlib.sha256(token.encode()).hexdigest()
            return record, token

    async def end_restore(self, owner: CrewOwner, checkpoint_id: str, token: str):
        async with self.session_factory.begin() as session:
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id)
                .with_for_update()
            )
            if (
                record is None
                or not record.restore_pins
                or record.restore_box_id is not None
                or not record.restore_token_hash
                or not secrets.compare_digest(record.restore_token_hash, hashlib.sha256(token.encode()).hexdigest())
            ):
                raise CheckpointMissingError("restore pin unavailable")
            record.restore_pins = 0
            record.restore_token_hash = None
            record.restore_pod_uid = None
            record.restore_pod_name = None
            record.restore_box_id = None
            record.restore_machine_generation = None

    async def request_delete(self, owner: CrewOwner, checkpoint_id: str, *, now: datetime | None = None):
        now = now or datetime.now(UTC)
        async with self.session_factory.begin() as session:
            executor = await session.scalar(
                select(CrewExecutor)
                .where(CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted")
                .with_for_update()
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id)
                .with_for_update()
            )
            if record is None:
                raise CheckpointMissingError("checkpoint unavailable")
            if record.status in ("deleting", "deleted"):
                return record.status
            retention_until = record.retention_until
            if retention_until is not None and retention_until.tzinfo is None:
                retention_until = retention_until.replace(tzinfo=UTC)
            if (
                executor is None
                or record.status != "verified"
                or executor.latest_verified_checkpoint_id == checkpoint_id
                or record.restore_pins
                or retention_until is None
                or retention_until > now
            ):
                raise CheckpointConflictError("checkpoint protected by retention")
            record.status = "deleting"
            return record.status

    async def reconcile_delete(self, owner: CrewOwner, checkpoint_id: str, storage):
        async with self.session_factory.begin() as session:
            executor = await session.scalar(
                select(CrewExecutor)
                .where(CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted")
                .with_for_update()
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id)
                .with_for_update()
            )
            if record is None:
                raise CheckpointMissingError("checkpoint unavailable")
            if record.status == "deleted":
                return "deleted"
            if record.status != "deleting" or executor is None or executor.latest_verified_checkpoint_id == checkpoint_id or record.restore_pins:
                raise CheckpointConflictError("checkpoint deletion fenced")
        await asyncio.to_thread(storage.delete_recorded, record)
        async with self.session_factory.begin() as session:
            executor = await session.scalar(
                select(CrewExecutor)
                .where(CrewExecutor.tenant_id == owner.tenant_id, CrewExecutor.user_id == owner.user_id, CrewExecutor.state != "deleted")
                .with_for_update()
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.tenant_id == owner.tenant_id, CrewCheckpoint.user_id == owner.user_id)
                .with_for_update()
            )
            if (
                record is None
                or record.status != "deleting"
                or executor is None
                or executor.latest_verified_checkpoint_id == checkpoint_id
                or record.restore_pins
            ):
                raise CheckpointConflictError("checkpoint deletion fenced")
            record.status = "deleted"
            return record.status

    async def helper_candidate(self, checkpoint_id: str, identity: CheckpointHelperIdentity, *, now: int | None = None):
        now = int(time.time()) if now is None else now
        if checkpoint_id != identity.checkpoint_id or hashlib.sha256(checkpoint_id.encode()).hexdigest() != identity.checkpoint_hash:
            raise CheckpointMissingError("checkpoint unavailable")
        async with self.session_factory.begin() as session:
            checkpoint = await session.scalar(
                select(CrewCheckpoint)
                .where(
                    CrewCheckpoint.id == checkpoint_id,
                    CrewCheckpoint.owner_key == identity.owner_key,
                    CrewCheckpoint.status.in_(("uploading", "failed")),
                )
                .with_for_update()
            )
            if checkpoint is None:
                raise CheckpointMissingError("checkpoint unavailable")
            executor = await session.scalar(
                select(CrewExecutor).where(
                    CrewExecutor.id == checkpoint.executor_id,
                    CrewExecutor.tenant_id == checkpoint.tenant_id,
                    CrewExecutor.user_id == checkpoint.user_id,
                    CrewExecutor.owner_key == identity.owner_key,
                    CrewExecutor.generation == checkpoint.generation,
                    CrewExecutor.state == "ready",
                    CrewExecutor.lease_generation == checkpoint.generation,
                    CrewExecutor.lease_expires_at > now,
                )
            )
            if executor is None:
                raise CheckpointConflictError("executor generation fenced")
            return checkpoint

    async def grant(self, checkpoint_id, identity, objects, storage, *, now=None):
        now = int(time.time()) if now is None else now
        candidate = await self.helper_candidate(checkpoint_id, identity, now=now)
        grant = storage.grant(candidate, objects)
        async with self.session_factory.begin() as session:
            current = await CrewExecutorInventory(self.session_factory).lock_current_generation(
                session,
                CrewRunBinding(
                    tenant_id=candidate.tenant_id, user_id=candidate.user_id, generation=candidate.generation, target_kind=candidate.target_kind
                ),
                now=now,
            )
            candidate = await session.scalar(
                select(CrewCheckpoint)
                .where(
                    CrewCheckpoint.id == checkpoint_id,
                    CrewCheckpoint.owner_key == identity.owner_key,
                    CrewCheckpoint.status.in_(("uploading", "failed")),
                )
                .with_for_update()
            )
            if candidate is None:
                raise CheckpointMissingError("checkpoint unavailable")
            if (
                candidate.executor_id != current.id
                or candidate.owner_key != current.owner_key
                or candidate.predecessor_id != current.latest_verified_checkpoint_id
            ):
                raise CheckpointConflictError("checkpoint executor or predecessor changed")
            for label in ("archive", "manifest"):
                recorded = getattr(candidate, f"{label}_sha256")
                if recorded is not None and (recorded != objects[label]["sha256"] or getattr(candidate, f"{label}_size") != objects[label]["size"]):
                    raise CheckpointConflictError("changed upload candidate")
                setattr(candidate, f"{label}_bucket", grant[label]["bucket"])
                setattr(candidate, f"{label}_key", grant[label]["key"])
                setattr(candidate, f"{label}_sha256", objects[label]["sha256"])
                setattr(candidate, f"{label}_size", objects[label]["size"])
            candidate.crew_version = COMPATIBLE_CREW_VERSION
            if candidate.captured_at is None:
                candidate.captured_at = utcnow()
            candidate.status = "uploading"
        return grant

    async def complete(self, checkpoint_id, identity, objects, storage, *, now=None):
        now = int(time.time()) if now is None else now
        try:
            candidate = await self.helper_candidate(checkpoint_id, identity, now=now)
        except CheckpointMissingError:
            if checkpoint_id != identity.checkpoint_id or hashlib.sha256(checkpoint_id.encode()).hexdigest() != identity.checkpoint_hash:
                raise
            async with self.session_factory() as session:
                accepted = await session.scalar(
                    select(CrewCheckpoint).where(
                        CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.owner_key == identity.owner_key, CrewCheckpoint.status == "verified"
                    )
                )
                if accepted is None:
                    raise
                current = await session.scalar(
                    select(CrewExecutor).where(
                        CrewExecutor.id == accepted.executor_id,
                        CrewExecutor.owner_key == identity.owner_key,
                        CrewExecutor.generation == accepted.generation,
                        CrewExecutor.state == "ready",
                        CrewExecutor.lease_generation == accepted.generation,
                        CrewExecutor.lease_expires_at > now,
                    )
                )
                if current is None:
                    raise CheckpointConflictError("stale helper") from None
                for label in ("archive", "manifest"):
                    if any(
                        objects[label].get(field) != getattr(accepted, f"{label}_{field}")
                        for field in ("bucket", "key", "version_id", "sha256", "size")
                    ):
                        raise CheckpointConflictError("changed completion receipt") from None
                return accepted
        if candidate.archive_sha256 is None or candidate.manifest_sha256 is None:
            raise InvalidCheckpointObjectsError("upload grant missing")
        if self.retention_period is None:
            raise CheckpointConflictError("checkpoint retention policy unavailable")
        if candidate.status != "uploading":
            raise CheckpointConflictError("checkpoint requires a new upload grant")
        try:
            storage.verify(candidate, objects)
        except InvalidCheckpointObjectsError:
            async with self.session_factory.begin() as session:
                record = await session.scalar(
                    select(CrewCheckpoint)
                    .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.owner_key == identity.owner_key, CrewCheckpoint.status == "uploading")
                    .with_for_update()
                )
                if record is not None:
                    record.status = "failed"
                    record.failure_class = "verification_failed"
                    record.retry_count += 1
            raise
        try:
            headers = storage.conversation_headers(objects)
        except Exception:
            headers = []
        async with self.session_factory.begin() as session:
            current = await CrewExecutorInventory(self.session_factory).lock_current_generation(
                session,
                CrewRunBinding(
                    tenant_id=candidate.tenant_id, user_id=candidate.user_id, generation=candidate.generation, target_kind=candidate.target_kind
                ),
                now=now,
            )
            record = await session.scalar(
                select(CrewCheckpoint)
                .where(CrewCheckpoint.id == checkpoint_id, CrewCheckpoint.owner_key == identity.owner_key, CrewCheckpoint.status == "uploading")
                .with_for_update()
            )
            if (
                record is None
                or current.id != record.executor_id
                or current.owner_key != record.owner_key
                or (current.latest_verified_checkpoint_id != record.predecessor_id)
            ):
                raise CheckpointConflictError("checkpoint publication fenced")
            for label in ("archive", "manifest"):
                if objects[label]["sha256"] != getattr(record, f"{label}_sha256") or objects[label]["key"] != getattr(record, f"{label}_key"):
                    raise CheckpointConflictError("checkpoint receipt changed")
                setattr(record, f"{label}_version_id", objects[label]["version_id"])
            record.status = "verified"
            record.failure_class = None
            record.verified_at = utcnow()
            record.retention_until = record.verified_at + self.retention_period
            for header in headers:
                session.add(
                    CrewConversation(
                        tenant_id=record.tenant_id,
                        user_id=record.user_id,
                        checkpoint_id=record.id,
                        source=header["source"],
                        conversation_id=header["conversation_id"],
                        archive_path=header["archive_path"],
                        title=header["title"],
                        started_at=header["started_at"],
                        last_activity_at=header["last_activity_at"],
                        turn_count=header["turn_count"],
                        unreadable=header["unreadable"],
                    )
                )
            current.latest_verified_checkpoint_id = record.id
            return record

    async def conversation_for_owner(self, owner: CrewOwner, source: str, conversation_id: str):
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(CrewConversation, CrewCheckpoint)
                    .join(CrewCheckpoint, CrewConversation.checkpoint_id == CrewCheckpoint.id)
                    .where(
                        CrewConversation.tenant_id == owner.tenant_id,
                        CrewConversation.user_id == owner.user_id,
                        CrewCheckpoint.tenant_id == owner.tenant_id,
                        CrewCheckpoint.user_id == owner.user_id,
                        CrewCheckpoint.status == "verified",
                        CrewConversation.source == source,
                        CrewConversation.conversation_id == conversation_id,
                    )
                    .order_by(CrewCheckpoint.sequence.desc())
                    .limit(1)
                )
            ).first()

    async def list_conversations(
        self,
        owner: CrewOwner,
        *,
        source: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
        owner_shell: bool = False,
    ):
        """Newest activity first; ``owner_shell`` keeps the owner's own conversations (Kiro chats, their Claude Code and
        Codex sessions over SSH) and drops Kiro's internal Claude Code runs (#9069). ``before`` pages by last activity."""
        ranked = (
            select(
                CrewConversation.id.label("conversation_row_id"),
                func.row_number()
                .over(
                    partition_by=(CrewConversation.source, CrewConversation.conversation_id),
                    order_by=CrewCheckpoint.sequence.desc(),
                )
                .label("position"),
            )
            .join(CrewCheckpoint, CrewConversation.checkpoint_id == CrewCheckpoint.id)
            .where(
                CrewConversation.tenant_id == owner.tenant_id,
                CrewConversation.user_id == owner.user_id,
                CrewCheckpoint.tenant_id == owner.tenant_id,
                CrewCheckpoint.user_id == owner.user_id,
                CrewCheckpoint.status == "verified",
            )
        )
        if source:
            ranked = ranked.where(CrewConversation.source == source)
        if owner_shell:
            ranked = ranked.where(
                or_(
                    CrewConversation.source == "kiro_crew",
                    *[CrewConversation.archive_path.like(f"{prefix}%") for prefix in OWNER_SHELL_PREFIXES],
                )
            )
        ranked = ranked.subquery()
        query = (
            select(CrewConversation)
            .join(ranked, CrewConversation.id == ranked.c.conversation_row_id)
            .where(ranked.c.position == 1)
            .order_by(CrewConversation.last_activity_at.desc().nulls_last(), CrewConversation.id.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(CrewConversation.last_activity_at < before)
        async with self.session_factory() as session:
            rows = (await session.scalars(query)).all()
        return [
            {
                "source": row.source,
                "conversation_id": row.conversation_id,
                "title": row.title,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "last_activity_at": row.last_activity_at.isoformat() if row.last_activity_at else None,
                "turn_count": row.turn_count,
                "checkpoint_id": row.checkpoint_id,
                "unreadable": row.unreadable,
                "archive_path": row.archive_path,
            }
            for row in rows
        ]

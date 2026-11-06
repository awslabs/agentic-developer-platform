"""Owner-scoped Crew target contract; registration does not launch compute."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, update

from src.cloud_desktop.host_identity import current_host
from src.crew.generation import advance_owner_generation, lock_owner_generation
from src.crew.inventory import CrewExecutorInventory, CrewLeaseFencedError
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxDevice, DevBoxInstance
from src.shared.models.organization import Organization

TargetKind = Literal["eks-pod", "ec2-instance"]


class CrewTargetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_targets: frozenset[TargetKind] = frozenset({"eks-pod"})

    @classmethod
    def from_tenant_settings(cls, settings: dict) -> CrewTargetPolicy:
        return cls.model_validate(settings.get("crew", {}))


class CrewLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    target_kind: TargetKind = "eks-pod"
    target_ref: str = Field(min_length=1)
    generation: int = Field(strict=True, ge=1)
    expires_at: int = Field(strict=True, ge=1)
    endpoint: str = Field(pattern=r"^https?://", min_length=8)

    @field_validator("generation", "expires_at", mode="before")
    @classmethod
    def stored_integer(cls, value):
        if isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value:
            raise ValueError("lease requires integral numeric values")
        return int(value)


class CrewRunBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    generation: int = Field(strict=True, ge=1)
    target_kind: TargetKind = "eks-pod"


@dataclass(frozen=True)
class CrewOwner:
    tenant_id: str
    user_id: str


class CrewTargetMissingError(CrewLeaseFencedError):
    """The signed-in owner has no active Crew lease."""


@dataclass(frozen=True)
class ResolvedCrewTarget:
    endpoint: str
    lease: CrewLease
    health: str
    host_run_id: str | None = None
    host_instance_ref: str | None = None
    host_fingerprint: str | None = None


class CrewTargetResolver(Protocol):
    async def resolve(self, owner: CrewOwner, *, now: int) -> ResolvedCrewTarget: ...

    async def ensure(self, owner: CrewOwner) -> CrewExecutor: ...

    async def end(self, owner: CrewOwner, *, generation: int) -> bool: ...


class EksPodTarget:
    def __init__(self, inventory: CrewExecutorInventory, lease_table, *, policy: CrewTargetPolicy | None = None):
        self.inventory = inventory
        self.lease_table = lease_table
        self.policy = policy or CrewTargetPolicy()

    async def ensure(self, owner: CrewOwner) -> CrewExecutor:
        if "eks-pod" not in self.policy.allowed_targets:
            raise CrewLeaseFencedError("target_not_allowed")
        return await self.inventory.ensure(tenant_id=owner.tenant_id, user_id=owner.user_id)

    async def _current_lease(self, owner: CrewOwner, *, now: int) -> CrewLease:
        if "eks-pod" not in self.policy.allowed_targets:
            raise CrewLeaseFencedError("target_not_allowed")
        if not owner.tenant_id or not owner.user_id:
            raise CrewLeaseFencedError("lease_fenced")
        item = await asyncio.to_thread(
            self.lease_table.get_item,
            Key={"PK": f"crew#{owner.tenant_id}#{owner.user_id}", "SK": "executor"},
            ConsistentRead=True,
        )
        row = item.get("Item")
        if not row:
            raise CrewTargetMissingError("crew_target_missing")
        if row.get("tenantId") != owner.tenant_id or row.get("ownerUserId") != owner.user_id or row.get("crewLeaseState", "ready") != "ready":
            raise CrewLeaseFencedError("lease_fenced")
        try:
            lease = CrewLease.model_validate(row.get("crewLease"))
        except ValidationError:
            raise CrewLeaseFencedError("lease_fenced") from None
        if lease.tenant_id != owner.tenant_id or lease.user_id != owner.user_id or lease.target_kind != "eks-pod" or lease.expires_at <= now:
            raise CrewLeaseFencedError("lease_fenced")
        return lease

    async def record_ready(self, owner: CrewOwner, *, generation: int, pod_uid: str, now: int) -> None:
        lease = await self._current_lease(owner, now=now)
        if not pod_uid or lease.generation != generation or lease.target_ref != pod_uid:
            raise CrewLeaseFencedError("lease_fenced")
        async with self.inventory.session_factory.begin() as session:
            result = await session.execute(
                update(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.generation == generation,
                    CrewExecutor.target_kind == "eks-pod",
                    CrewExecutor.state.in_(["provisioning", "stopped"]),
                )
                .values(
                    state="ready",
                    target_ref=pod_uid,
                    lease_generation=lease.generation,
                    lease_expires_at=lease.expires_at,
                    last_started_at=datetime.fromtimestamp(now, UTC),
                )
            )
            if result.rowcount != 1:
                raise CrewLeaseFencedError("lease_fenced")

    async def resolve(self, owner: CrewOwner, *, now: int) -> ResolvedCrewTarget:
        lease = await self._current_lease(owner, now=now)
        async with self.inventory.session_factory() as session:
            executor = await session.scalar(
                select(CrewExecutor).where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.state == "ready",
                )
            )
        if (
            executor is None
            or executor.target_kind != lease.target_kind
            or executor.target_ref != lease.target_ref
            or executor.generation != lease.generation
            or executor.lease_generation != lease.generation
            or executor.lease_expires_at is None
            or executor.lease_expires_at <= now
        ):
            raise CrewLeaseFencedError("lease_fenced")
        return ResolvedCrewTarget(endpoint=lease.endpoint, lease=lease, health="ready")

    async def end(self, owner: CrewOwner, *, generation: int) -> bool:
        async with self.inventory.session_factory.begin() as session:
            cursor = await lock_owner_generation(session, owner.tenant_id, owner.user_id)
            result = await session.execute(
                update(CrewExecutor)
                .where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.generation == generation,
                    CrewExecutor.state.in_(["provisioning", "ready", "stopped"]),
                )
                .values(state="ending", lease_generation=None, lease_expires_at=None)
            )
            if result.rowcount == 1:
                advance_owner_generation(cursor)
            return result.rowcount == 1


class Ec2InstanceTarget:
    def __init__(self, inventory: CrewExecutorInventory, lease_table, *, deployment_id: str, answers=None):
        self.inventory = inventory
        self.lease_table = lease_table
        self.deployment_id = deployment_id
        # Issue #9619: whether the box's Crew answers through its ingress yet. Resolved
        # late so tests (and the module default) can swap the probe; see host_readiness.
        self._answers = answers

    async def resolve(self, owner: CrewOwner, *, now: int) -> ResolvedCrewTarget:
        if not owner.tenant_id or not owner.user_id or not self.deployment_id:
            raise CrewLeaseFencedError("lease_fenced")
        item = await asyncio.to_thread(
            self.lease_table.get_item,
            Key={"PK": f"crew#{owner.tenant_id}#{owner.user_id}", "SK": "executor"},
            ConsistentRead=True,
        )
        row = item.get("Item")
        if not row:
            raise CrewTargetMissingError("crew_target_missing")
        if row.get("tenantId") != owner.tenant_id or row.get("ownerUserId") != owner.user_id or row.get("crewLeaseState") != "ready":
            raise CrewLeaseFencedError("lease_fenced")
        try:
            lease = CrewLease.model_validate(row.get("crewLease"))
        except ValidationError:
            raise CrewLeaseFencedError("lease_fenced") from None
        if lease.tenant_id != owner.tenant_id or lease.user_id != owner.user_id or lease.target_kind != "ec2-instance" or lease.expires_at <= now:
            raise CrewLeaseFencedError("lease_fenced")
        async with self.inventory.session_factory() as session:
            settings = await session.scalar(select(Organization.settings).where(Organization.id == owner.tenant_id))
            if not isinstance(settings, dict):
                raise CrewLeaseFencedError("lease_fenced")
            try:
                policy = CrewTargetPolicy.from_tenant_settings(settings)
            except (TypeError, ValidationError):
                raise CrewLeaseFencedError("lease_fenced") from None
            if "ec2-instance" not in policy.allowed_targets:
                raise CrewLeaseFencedError("lease_fenced")
            executor = await session.scalar(
                select(CrewExecutor).where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.target_kind == "ec2-instance",
                    CrewExecutor.state == "ready",
                    CrewExecutor.generation == lease.generation,
                    CrewExecutor.lease_generation == lease.generation,
                    CrewExecutor.lease_expires_at > now,
                )
            )
            if not executor or not executor.dev_box_id or lease.target_ref != executor.dev_box_id:
                raise CrewLeaseFencedError("lease_fenced")
            box = await session.scalar(
                select(DevBox).where(
                    DevBox.id == executor.dev_box_id,
                    DevBox.user_id == owner.user_id,
                    DevBox.org_id == owner.tenant_id,
                    DevBox.deployment_id == self.deployment_id,
                    DevBox.profile == "crew",
                    DevBox.status == "running",
                    DevBox.desired_state == "running",
                    DevBox.observed_state == "running",
                    DevBox.auth_readiness == "ready",
                    DevBox.machine_generation == executor.generation,
                )
            )
            if not box:
                raise CrewLeaseFencedError("lease_fenced")
            instance = await session.scalar(
                select(DevBoxInstance).where(
                    DevBoxInstance.box_id == box.id,
                    DevBoxInstance.user_id == owner.user_id,
                    DevBoxInstance.org_id == owner.tenant_id,
                    DevBoxInstance.deployment_id == self.deployment_id,
                    DevBoxInstance.machine_generation == box.machine_generation,
                    DevBoxInstance.observed_state == "running",
                    DevBoxInstance.readiness_class == "crew_ready",
                )
            )
            active = await session.scalar(
                select(DevBoxAccessSession)
                .join(DevBoxDevice, DevBoxDevice.id == DevBoxAccessSession.device_id)
                .where(
                    DevBoxDevice.revoked_at.is_(None),
                    DevBoxDevice.machine_generation == box.machine_generation,
                    DevBoxAccessSession.provider_session_ref.is_not(None),
                    DevBoxAccessSession.box_id == box.id,
                    DevBoxAccessSession.user_id == owner.user_id,
                    DevBoxAccessSession.org_id == owner.tenant_id,
                    DevBoxAccessSession.deployment_id == self.deployment_id,
                    DevBoxAccessSession.machine_generation == box.machine_generation,
                    DevBoxAccessSession.terminated_at.is_(None),
                    DevBoxAccessSession.expires_at > datetime.fromtimestamp(now, UTC),
                )
            )
            if not instance or not instance.provider_instance_id or not active:
                raise CrewLeaseFencedError("lease_fenced")
            host = await current_host(
                session,
                box_id=box.id,
                generation=box.machine_generation,
                provider_instance_id=instance.provider_instance_id,
                user_id=owner.user_id,
                org_id=owner.tenant_id,
                deployment_id=self.deployment_id,
                run_id=active.provider_session_ref,
            )
            if (
                host is None
                or host.id != instance.id
                or not isinstance(host.certificate_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", host.certificate_fingerprint)
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
        endpoint = f"https://{address}:8443"
        if lease.endpoint != endpoint:
            raise CrewLeaseFencedError("lease_fenced")
        target = ResolvedCrewTarget(
            endpoint=endpoint,
            lease=lease,
            health="ready",
            host_run_id=active.provider_session_ref,
            host_instance_ref=instance.provider_instance_id,
            host_fingerprint=instance.certificate_fingerprint,
        )
        # The controller's activation proves the ingress and enrollment; Kiro Crew behind
        # it warms up for another 20-30 s. Until it answers, the target is "starting" and
        # the open flow keeps polling instead of handing off into a mint the box refuses.
        answers = self._answers
        if answers is None:
            from src.crew import host_readiness

            answers = host_readiness.crew_host_answers
        if not await answers(target):
            return replace(target, health="starting")
        return target


class OwnerTargetResolver:
    def __init__(self, inventory: CrewExecutorInventory, lease_table, *, deployment_id: str):
        self.inventory = inventory
        self.pod = EksPodTarget(inventory, lease_table)
        self.host = Ec2InstanceTarget(inventory, lease_table, deployment_id=deployment_id)

    async def _target_kind(self, owner: CrewOwner) -> str | None:
        async with self.inventory.session_factory() as session:
            return await session.scalar(
                select(CrewExecutor.target_kind).where(
                    CrewExecutor.tenant_id == owner.tenant_id,
                    CrewExecutor.user_id == owner.user_id,
                    CrewExecutor.state != "deleted",
                )
            )

    async def resolve(self, owner: CrewOwner, *, now: int) -> ResolvedCrewTarget:
        kind = await self._target_kind(owner)
        if kind == "ec2-instance":
            try:
                return await self.host.resolve(owner, now=now)
            except CrewLeaseFencedError:
                pass
            from src.crew.host_lease import publish_host_lease

            await publish_host_lease(self.inventory.session_factory, self.host.lease_table, owner, self.host.deployment_id, now)
            return await self.host.resolve(owner, now=now)
        if kind == "eks-pod":
            return await self.pod.resolve(owner, now=now)
        raise CrewTargetMissingError("crew_target_missing")

    async def ensure(self, owner: CrewOwner) -> CrewExecutor:
        if await self._target_kind(owner) == "ec2-instance":
            raise CrewLeaseFencedError("target_not_allowed")
        return await self.pod.ensure(owner)

    async def end(self, owner: CrewOwner, *, generation: int) -> bool:
        if await self._target_kind(owner) == "ec2-instance":
            raise CrewLeaseFencedError("target_not_allowed")
        return await self.pod.end(owner, generation=generation)

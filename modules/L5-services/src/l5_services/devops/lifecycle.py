"""Save DevOps create intent before effects and recover uncertain outcomes."""

import asyncio
import json
import hashlib
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, update

from l5_services.adapters.devops.spaces import SpaceSelection
from l5_services.adapters.devops.prevention import PreventionAdapter
from l5_services.adapters.devops.aws_access import AccessDenied
from l5_services.adapters.devops.integrations import IntegrationAccessDenied
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.devops.prevention import PreventionPolicy, prevention_readiness, require_native_budget


class SpaceLifecycle:
    def __init__(self, sessions, adapter, *, clock=None):
        self.sessions = sessions
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare_create(self, selection: SpaceSelection, operation_id: str,
                             configuration_revision: int) -> None:
        fingerprint = selection.fingerprint()
        if not operation_id or not isinstance(configuration_revision, int) or configuration_revision < 1:
            raise ValueError("durable operation ID and pinned configuration revision required")
        intent_hash = hashlib.sha256(f"{fingerprint}:{configuration_revision}".encode()).hexdigest()
        async with self.sessions() as session:
            existing = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            if existing:
                if existing.intent_hash != intent_hash:
                    raise ValueError("operation ID reused with changed creation intent")
                return
            session.add(DevopsSpaceIntent(tenant_id=selection.tenant_id, operation_id=operation_id,
                                         selection=asdict(selection), configuration_revision=configuration_revision,
                                         intent_hash=intent_hash, state="accepted",
                                         prevention_policy={"enabled": False}))
            await session.commit()

    async def execute_create(self, tenant_id: str, operation_id: str):
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == tenant_id, DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == "accepted").values(state="effect_started"))
            await session.commit()
            if claimed.rowcount != 1:
                return await self.reconcile_create(tenant_id, operation_id)
            receipt = await session.get(DevopsSpaceIntent, (tenant_id, operation_id))
            selection = SpaceSelection(**receipt.selection)
        try:
            readback = self.adapter.create(selection, operation_id)
        except Exception:
            async with self.sessions() as session:
                await session.execute(update(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == tenant_id, DevopsSpaceIntent.operation_id == operation_id,
                    DevopsSpaceIntent.state == "effect_started").values(state="outcome_unknown"))
                await session.commit()
            raise
        await self._record(tenant_id, operation_id, readback)
        return readback

    async def reconcile_create(self, tenant_id: str, operation_id: str):
        async with self.sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (tenant_id, operation_id))
            if receipt is None:
                raise ValueError("unknown durable setup intent")
            selection = SpaceSelection(**receipt.selection)
            if receipt.state not in {"effect_started", "outcome_unknown", "succeeded"}:
                raise ValueError("creation effect has not started")
            previously_verified_id = receipt.native_space_id
        readback = self.adapter.reconcile_create(selection)
        if readback is None:
            return None
        if previously_verified_id and previously_verified_id != readback.agent_space_id:
            raise ValueError("native space changed since verified receipt")
        await self._record(tenant_id, operation_id, readback)
        return readback

    async def _record(self, tenant_id, operation_id, readback):
        async with self.sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (tenant_id, operation_id))
            selection = SpaceSelection(**receipt.selection)
            try:
                observation = PreventionAdapter(
                    self.adapter.client, account_id=self.adapter.account_id,
                    region=self.adapter.region).read(selection, readback.agent_space_id,
                                              receipt.configuration_revision)
            except (AttributeError, ValueError, BotoCoreError, ClientError, ConnectionError, TimeoutError):
                observation = {"scope_fingerprint": selection.fingerprint(),
                               "configuration_revision": receipt.configuration_revision,
                               "native_space_id": readback.agent_space_id, "native_schedule": None}
            await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == tenant_id, DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state.in_(("effect_started", "outcome_unknown"))).values(
                    state="succeeded", native_space_id=readback.agent_space_id,
                    ownership=readback.ownership, scope_fingerprint=readback.scope_fingerprint,
                    prevention_observation=observation))
            await session.commit()

    async def prevention_status(self, selection: SpaceSelection, operation_id: str, settings: dict) -> dict:
        receipt = await self._binding_receipt(selection, operation_id)
        selected = PreventionPolicy.from_configuration(settings)
        saved = receipt.prevention_policy or {}
        if saved.get("goal_id") and (selected.goal_id, selected.expression) != (
            saved["goal_id"], saved["expression"]
        ):
            raise ValueError("selected prevention policy changed since observation")
        policy = PreventionPolicy(**{key: saved[key] for key in (
            "enabled", "goal_id", "expression", "timezone", "missed_run_policy")}) if saved.get("goal_id") else selected
        desired = ({**policy.as_dict(), "per_run_limits": saved.get("per_run_limits"),
                    "cost_control": saved.get("cost_control")}
                   if saved.get("goal_id") else {"enabled": policy.enabled})
        try:
            observed = PreventionAdapter(self.adapter.client, account_id=self.adapter.account_id,
                                         region=self.adapter.region).read(
                selection, receipt.native_space_id, receipt.configuration_revision)
        except (AttributeError, ValueError, BotoCoreError, ClientError, ConnectionError, TimeoutError):
            status = prevention_readiness(policy, receipt.prevention_observation,
                                          configuration_revision=receipt.configuration_revision,
                                          scope_fingerprint=selection.fingerprint())
            status.update(desired=desired, status="blocked", reason="native_schedule_readback_unavailable")
            return status
        if observed != receipt.prevention_observation:
            async with self.sessions() as session:
                changed = await session.execute(update(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == selection.tenant_id,
                    DevopsSpaceIntent.operation_id == operation_id,
                    DevopsSpaceIntent.prevention_operation_id == receipt.prevention_operation_id,
                ).values(prevention_observation=observed))
                await session.commit()
                if changed.rowcount != 1:
                    raise ValueError("prevention policy changed during readback")
        status = prevention_readiness(policy, observed,
                                      configuration_revision=receipt.configuration_revision,
                                      scope_fingerprint=selection.fingerprint())
        status["desired"] = desired
        if (receipt.prevention_policy or {}).get("state") == "pending":
            status.update(status="blocked", reason="prevention_change_pending")
        return status

    async def configure_prevention(self, selection: SpaceSelection, operation_id: str,
                                   policy: PreventionPolicy, *, action_id: str, settings: dict,
                                   budget: dict | None = None) -> dict:
        """Prepare native schedule changes durably, then verify without replaying unknown effects."""
        receipt = await self._binding_receipt(selection, operation_id)
        if (receipt.state not in {"succeeded", "ready"} or not receipt.native_space_id
                or not isinstance(action_id, str) or not action_id):
            raise ValueError("verified space and durable prevention action required")
        selected = PreventionPolicy.from_configuration(settings)
        if (selected.goal_id, selected.expression, selected.timezone,
            selected.missed_run_policy) != (policy.goal_id, policy.expression, policy.timezone,
                                          policy.missed_run_policy):
            raise ValueError("native prevention target differs from selected project policy")
        if policy.enabled:
            require_native_budget(settings, budget, receipt.selection)
        desired = {**policy.as_dict(),
                   "per_run_limits": settings.get("limits") if policy.enabled else
                   (receipt.prevention_policy or {}).get("per_run_limits"),
                   "cost_control": "observation_only"}
        previous = receipt.prevention_policy or {"enabled": False}
        if previous.get("state") == "pending":
            if receipt.prevention_operation_id != action_id or any(
                previous.get(key) != value for key, value in desired.items()
            ):
                raise ValueError("another prevention change requires readback recovery")
            return await self.reconcile_prevention(selection, operation_id, policy)
        if receipt.prevention_operation_id == action_id:
            if any(previous.get(key) != value for key, value in desired.items()):
                raise ValueError("prevention action ID reused with different policy")
            return receipt.prevention_observation
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == receipt.state,
                DevopsSpaceIntent.prevention_operation_id == receipt.prevention_operation_id,
            ).values(prevention_policy={**desired, "state": "pending"},
                     prevention_operation_id=action_id))
            await session.commit()
            if claimed.rowcount != 1:
                raise ValueError("concurrent prevention change")
        adapter = PreventionAdapter(self.adapter.client, account_id=self.adapter.account_id,
                                    region=self.adapter.region)
        observation = adapter.set_enabled(selection, receipt.native_space_id, receipt.configuration_revision,
                                          goal_id=policy.goal_id, expression=policy.expression,
                                          enabled=policy.enabled, operation_id=action_id)
        return await self._finish_prevention(selection, operation_id, policy, observation)

    async def reconcile_prevention(self, selection: SpaceSelection, operation_id: str,
                                   policy: PreventionPolicy) -> dict:
        receipt = await self._binding_receipt(selection, operation_id)
        if (receipt.prevention_policy or {}).get("state") != "pending" or any(
            receipt.prevention_policy.get(key) != value for key, value in policy.as_dict().items()
        ):
            raise ValueError("no matching prevention intent to reconcile")
        adapter = PreventionAdapter(self.adapter.client, account_id=self.adapter.account_id,
                                    region=self.adapter.region)
        observation = adapter.read(selection, receipt.native_space_id, receipt.configuration_revision)
        status = prevention_readiness(policy, observation,
                                      configuration_revision=receipt.configuration_revision,
                                      scope_fingerprint=selection.fingerprint())
        if status["status"] != "ready":
            raise ValueError("prevention outcome unverified; retain intent for recovery")
        return await self._finish_prevention(selection, operation_id, policy, observation)

    async def _finish_prevention(self, selection, operation_id, policy, observation):
        receipt = await self._binding_receipt(selection, operation_id)
        status = prevention_readiness(policy, observation,
                                      configuration_revision=receipt.configuration_revision,
                                      scope_fingerprint=selection.fingerprint())
        if status["status"] != "ready":
            raise ValueError("native prevention readback differs from requested policy")
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.prevention_operation_id == receipt.prevention_operation_id,
            ).values(prevention_observation=observation,
                     prevention_policy={**receipt.prevention_policy, "state": "verified"}))
            await session.commit()
            if claimed.rowcount != 1:
                raise ValueError("prevention intent changed during readback")
        return observation

    async def _binding_receipt(self, selection: SpaceSelection, operation_id: str):
        async with self.sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            if receipt is None or receipt.selection != asdict(selection):
                raise ValueError("binding scope does not match original creation intent")
            return receipt

    async def verify_integration_associations(self, selection: SpaceSelection, operation_id: str,
                                              integrations: list[dict], integration_adapter, native):
        receipt = await self._binding_receipt(selection, operation_id)
        if receipt.state not in {"succeeded", "ready"} or not receipt.native_space_id:
            raise ValueError("space binding must be verified before integration setup")
        if not integrations:
            raise ValueError("selected integrations required")
        selection_hash = hashlib.sha256(json.dumps(integrations, sort_keys=True).encode()).hexdigest()
        previous = receipt.integration_observation or {}
        if previous and (previous.get("selection_hash") != selection_hash
                         or previous.get("configuration_revision") != receipt.configuration_revision):
            raise ValueError("integration selection changed since durable setup intent")
        verification_id = uuid.uuid4().hex
        pending = {"status": "verifying", "reason": "association_outcome_unknown",
                   "selection_hash": selection_hash, "configuration_revision": receipt.configuration_revision,
                   "verification_id": verification_id,
                   **({"associations": previous["associations"]} if previous.get("associations") else {})}
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == receipt.state,
                DevopsSpaceIntent.configuration_revision == receipt.configuration_revision,
                DevopsSpaceIntent.integration_observation == receipt.integration_observation,
            ).values(integration_observation=pending))
            await session.commit()
            if claimed.rowcount != 1:
                raise ValueError("integration setup already in progress")
        missing_registration = (previous.get("reason") == "native_registration_requires_consent"
                                and not previous.get("associations"))
        try:
            associations = await integration_adapter.reconcile(
                selection.tenant_id, selection.product_id, selection.project_id, integrations,
                receipt.native_space_id, operation_id, native, reconcile_only=bool(previous) and not missing_registration)
        except IntegrationAccessDenied as error:
            if str(error) != "native_registration_requires_consent":
                raise
            blocked = {**pending, "status": "blocked", "reason": str(error)}
            async with self.sessions() as session:
                await session.execute(update(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == selection.tenant_id,
                    DevopsSpaceIntent.operation_id == operation_id,
                    DevopsSpaceIntent.integration_observation["verification_id"].as_string() == verification_id,
                ).values(integration_observation=blocked))
                await session.commit()
            raise
        if previous.get("associations"):
            for old, observed in zip(previous["associations"], associations, strict=True):
                if any(old[field] != observed[field] for field in (
                        "registration_id", "association_id", "configuration")):
                    raise IntegrationAccessDenied("native_association_changed")
            associations = previous["associations"]
        observation = {**pending, "status": "ready", "reason": None, "associations": associations}
        async with self.sessions() as session:
            updated = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == receipt.state,
                DevopsSpaceIntent.configuration_revision == receipt.configuration_revision,
                DevopsSpaceIntent.integration_observation["verification_id"].as_string() == verification_id,
            ).values(integration_observation=observation))
            await session.commit()
            if updated.rowcount != 1:
                raise ValueError("binding changed while verifying integrations")
        return observation

    async def require_current_integration_access(self, selection: SpaceSelection, operation_id: str,
                                                 integrations: list[dict], integration_adapter, native):
        receipt = await self._binding_receipt(selection, operation_id)
        observation = receipt.integration_observation or {}
        selection_hash = hashlib.sha256(json.dumps(integrations, sort_keys=True).encode()).hexdigest()
        if (receipt.state not in {"succeeded", "ready"} or not receipt.native_space_id
                or observation.get("status") != "ready"
                or observation.get("selection_hash") != selection_hash
                or observation.get("configuration_revision") != receipt.configuration_revision):
            raise IntegrationAccessDenied("integration_access_unverified")
        evidence = await integration_adapter.read(selection.tenant_id, selection.product_id,
                                                  selection.project_id, integrations)
        associations = await integration_adapter.reconcile(
            selection.tenant_id, selection.product_id, selection.project_id, integrations,
            receipt.native_space_id, operation_id, native, reconcile_only=True)
        for old, observed in zip(observation["associations"], associations, strict=True):
            if any(old[field] != observed[field] for field in (
                    "registration_id", "association_id", "configuration")):
                raise IntegrationAccessDenied("native_association_changed")
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            if (current is None or current.state != receipt.state
                    or current.configuration_revision != receipt.configuration_revision
                    or current.integration_observation != observation):
                raise IntegrationAccessDenied("integration_access_changed")
        return evidence

    async def verify_resource_access(self, selection: SpaceSelection, operation_id: str,
                                     resource_selection, access_adapter, *, delegation_id: str,
                                     delegation_revision: int, attempt_id: str):
        receipt = await self._binding_receipt(selection, operation_id)
        if receipt.state not in {"succeeded", "ready"} or not receipt.native_space_id:
            raise ValueError("space binding must be verified before resource access")
        if (resource_selection.connection_ref["tenant_id"], resource_selection.connection_ref["project_id"]) != (
            selection.tenant_id, selection.project_id
        ):
            raise ValueError("resource connection is outside selected binding")
        scope_fingerprint = resource_selection.fingerprint()
        verification_id = uuid.uuid4().hex
        pending = {"status": "verifying", "reason": "rechecking_current_access",
                   "scope_fingerprint": scope_fingerprint, "verification_id": verification_id}
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == receipt.state,
                DevopsSpaceIntent.configuration_revision == receipt.configuration_revision,
            ).values(resource_observation=pending))
            await session.commit()
            if claimed.rowcount != 1:
                raise ValueError("binding changed before resource verification")
        try:
            try:
                native = await asyncio.to_thread(self.adapter.client.get_agent_space,
                                                 agentSpaceId=receipt.native_space_id)
            except (ClientError, BotoCoreError, ConnectionError, TimeoutError) as exc:
                raise AccessDenied("native_space_readback_unavailable") from exc
            space = native.get("agentSpace") or {}
            if space.get("agentSpaceId") != receipt.native_space_id:
                raise AccessDenied("native_space_identity_unverified")
            space_arn = (f"arn:aws:aidevops:{selection.region}:{selection.account_id}:"
                         f"agentspace/{receipt.native_space_id}")
            observation = await access_adapter.verify(
                resource_selection, delegation_id=delegation_id,
                delegation_revision=delegation_revision, operation_id=operation_id, attempt_id=attempt_id,
                host_account=selection.account_id, space_arn=space_arn,
                host_client=self.adapter.client, space_id=receipt.native_space_id,
                reconcile_only=(receipt.resource_observation or {}).get("reason") in {
                    "association_outcome_unknown", "rechecking_current_access", "association_readback_denied",
                    "association_readback_unavailable", "association_discovery_denied",
                    "association_discovery_unavailable", "association_discovery_incomplete"},
            )
            observation = {**observation, "resource_selection": asdict(resource_selection),
                           "native_space_id": receipt.native_space_id,
                           "space_fingerprint": selection.fingerprint()}
        except AccessDenied as exc:
            observation = {"status": "blocked", "reason": str(exc),
                           "scope_fingerprint": scope_fingerprint, "checked_at": self.clock().isoformat()}
        async with self.sessions() as session:
            updated = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == operation_id,
                DevopsSpaceIntent.state == receipt.state,
                DevopsSpaceIntent.configuration_revision == receipt.configuration_revision,
                DevopsSpaceIntent.resource_observation["verification_id"].as_string() == verification_id,
            ).values(resource_observation=observation))
            await session.commit()
            if updated.rowcount != 1:
                raise ValueError("binding changed while verifying resource access")
        return observation

    async def require_current_resource_access(self, selection: SpaceSelection, operation_id: str,
                                              resource_selection, access_adapter=None, *, delegation_id=None,
                                              delegation_revision=None, attempt_id=None):
        receipt = await self._binding_receipt(selection, operation_id)
        observation = receipt.resource_observation or {}
        reason = None
        if observation.get("scope_fingerprint") != resource_selection.fingerprint():
            reason = "resource_selection_changed"
        elif observation.get("status") != "ready":
            raise AccessDenied(observation.get("reason") or "resource_access_unverified")
        else:
            try:
                expiry = datetime.fromisoformat(observation["expires_at"])
                if expiry.tzinfo is None or expiry <= self.clock():
                    reason = "resource_access_expired"
            except (KeyError, ValueError, TypeError):
                reason = "resource_access_expired"
        if reason:
            async with self.sessions() as session:
                await session.execute(update(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == selection.tenant_id,
                    DevopsSpaceIntent.operation_id == operation_id,
                    DevopsSpaceIntent.resource_observation["scope_fingerprint"].as_string()
                    == observation.get("scope_fingerprint"),
                    DevopsSpaceIntent.resource_observation["status"].as_string()
                    == observation.get("status"),
                    DevopsSpaceIntent.resource_observation["expires_at"].as_string()
                    == observation.get("expires_at"),
                ).values(resource_observation={"status": "blocked", "reason": reason,
                                               "scope_fingerprint": resource_selection.fingerprint()}))
                await session.commit()
            raise AccessDenied(reason)
        if access_adapter is None or not all((delegation_id, delegation_revision, attempt_id)):
            raise AccessDenied("current_broker_verification_required")
        refreshed = await self.verify_resource_access(
            selection, operation_id, resource_selection, access_adapter,
            delegation_id=delegation_id, delegation_revision=delegation_revision, attempt_id=attempt_id,
        )
        if refreshed.get("status") != "ready":
            raise AccessDenied(refreshed.get("reason") or "resource_access_unverified")
        return refreshed

    async def suspend(self, selection: SpaceSelection, operation_id: str, scheduler):
        if scheduler is None:
            raise ValueError("native managed automation verifier is required")
        receipt = await self._binding_receipt(selection, operation_id)
        if receipt.ownership != "adp_created" or receipt.scope_fingerprint != selection.fingerprint():
            raise ValueError("cannot suspend automation without original creation ownership")
        if receipt.state not in {"succeeded", "ready", "suspending", "suspended"}:
            raise ValueError("binding cannot be suspended from current state")
        agent_space_id = receipt.native_space_id
        owned_trigger_ids = receipt.owned_trigger_ids
        states = self.adapter._triggers(selection, agent_space_id)
        if any(status not in {"ENABLED", "DISABLED"} for status in states.values()):
            raise ValueError("unknown native trigger status")
        if any(status == "ENABLED" and trigger_id not in owned_trigger_ids
               for trigger_id, status in states.items()):
            raise ValueError("unowned native automation remains enabled")
        if any(trigger_id not in states for trigger_id in owned_trigger_ids):
            raise ValueError("owned native trigger missing")
        resume_ids = (receipt.resume_trigger_ids if receipt.state in {"suspending", "suspended"}
                      else [trigger_id for trigger_id in owned_trigger_ids if states[trigger_id] == "ENABLED"])
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.state = "suspending"
            current.resume_trigger_ids = resume_ids
            await session.commit()
        self.adapter.pause_owned_triggers(selection, agent_space_id, owned_trigger_ids)
        scheduler.pause(selection, agent_space_id)
        if not scheduler.is_paused(selection, agent_space_id):
            raise ValueError("native managed automation pause is not verified")
        active = self.adapter.observe_work(selection, agent_space_id)
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.state = "suspended"
            current.active_executions = active
            await session.commit()
        return active

    async def resume(self, selection: SpaceSelection, operation_id: str, scheduler):
        receipt = await self._binding_receipt(selection, operation_id)
        if receipt.ownership != "adp_created" or receipt.scope_fingerprint != selection.fingerprint():
            raise ValueError("cannot resume automation without original creation ownership")
        if receipt.state not in {"suspended", "resuming"}:
            raise ValueError("binding is not suspended")
        self.adapter._read(selection, receipt.native_space_id, created=True, fingerprint=selection.fingerprint())
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.state = "resuming"
            await session.commit()
        scheduler.resume(selection, receipt.native_space_id)
        if scheduler.is_paused(selection, receipt.native_space_id):
            raise ValueError("native managed automation resume is not verified")
        self.adapter.restore_owned_triggers(selection, receipt.native_space_id, receipt.resume_trigger_ids)
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.state = "ready"
            current.active_executions = []
            await session.commit()

    async def retire(self, selection: SpaceSelection, operation_id: str, scheduler, *,
                     cleanup_policy_accepted: bool, owned_cleanup_confirmed: bool):
        receipt = await self._binding_receipt(selection, operation_id)
        if receipt.ownership == "customer_linked":
            async with self.sessions() as session:
                current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
                current.state = "retained"
                await session.commit()
            return "retained"
        if receipt.ownership != "adp_created" or receipt.scope_fingerprint != selection.fingerprint():
            raise ValueError("original creation ownership is unverified")
        if receipt.state != "suspended" or not scheduler.is_paused(selection, receipt.native_space_id):
            raise ValueError("native automation must be paused before retirement")
        if any(status != "DISABLED" for status in self.adapter._triggers(selection, receipt.native_space_id).values()):
            raise ValueError("native triggers remain enabled")
        active = self.adapter.observe_work(selection, receipt.native_space_id)
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.active_executions = active
            await session.commit()
        if active:
            return "draining"
        if cleanup_policy_accepted is not True or owned_cleanup_confirmed is not True:
            return "retained"
        async with self.sessions() as session:
            other_receipts = (await session.scalars(select(DevopsSpaceIntent).where(
                DevopsSpaceIntent.native_space_id == receipt.native_space_id,
                ~((DevopsSpaceIntent.tenant_id == selection.tenant_id) &
                  (DevopsSpaceIntent.operation_id == operation_id))))).all()
        if any(other.selection.get("account_id") == selection.account_id
               and other.selection.get("region") == selection.region for other in other_receipts):
            raise ValueError("native space is also linked to another binding")
        self.adapter.validate_owned_cleanup(selection, receipt.native_space_id)
        async with self.sessions() as session:
            current = await session.get(DevopsSpaceIntent, (selection.tenant_id, operation_id))
            current.state = "cleanup_pending"
            await session.commit()
        self.adapter.delete_owned(selection, receipt.native_space_id)
        return "cleanup_pending"

    async def prepare_update(self, selection: SpaceSelection, creation_operation_id: str,
                             update_operation_id: str, configuration_revision: int,
                             description: str) -> None:
        receipt = await self._binding_receipt(selection, creation_operation_id)
        if receipt.ownership != "adp_created" or receipt.scope_fingerprint != selection.fingerprint():
            raise ValueError("native update requires original creation ownership")
        if (not update_operation_id or not isinstance(configuration_revision, int)
                or configuration_revision < 1 or not isinstance(description, str)
                or not 1 <= len(description) <= 1000 or not description.strip()):
            raise ValueError("update requires an operation, pinned revision and valid description")
        if receipt.update_operation_id == update_operation_id:
            if (receipt.update_configuration_revision != configuration_revision
                    or receipt.requested_description != description):
                raise ValueError("update operation reused with changed intent")
            return
        if receipt.state not in {"succeeded", "ready"}:
            raise ValueError("previous space operation must be reconciled before update")
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == creation_operation_id,
                DevopsSpaceIntent.state == receipt.state).values(
                    state="update_pending", update_operation_id=update_operation_id,
                    update_configuration_revision=configuration_revision,
                    requested_description=description))
            await session.commit()
            if claimed.rowcount != 1:
                raise ValueError("concurrent space operation")

    async def execute_update(self, selection: SpaceSelection, creation_operation_id: str,
                             update_operation_id: str):
        receipt = await self._binding_receipt(selection, creation_operation_id)
        if receipt.update_operation_id != update_operation_id:
            raise ValueError("update operation does not match pinned intent")
        async with self.sessions() as session:
            claimed = await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == selection.tenant_id,
                DevopsSpaceIntent.operation_id == creation_operation_id,
                DevopsSpaceIntent.update_operation_id == update_operation_id,
                DevopsSpaceIntent.state == "update_pending").values(state="update_effect_started"))
            await session.commit()
        if claimed.rowcount != 1:
            return await self.reconcile_update(selection, creation_operation_id, update_operation_id)
        try:
            readback = self.adapter.update_description(selection, receipt.native_space_id,
                                                       receipt.requested_description)
        except Exception:
            async with self.sessions() as session:
                await session.execute(update(DevopsSpaceIntent).where(
                    DevopsSpaceIntent.tenant_id == selection.tenant_id,
                    DevopsSpaceIntent.operation_id == creation_operation_id,
                    DevopsSpaceIntent.state == "update_effect_started").values(state="update_outcome_unknown"))
                await session.commit()
            raise
        await self._record_update(selection.tenant_id, creation_operation_id, update_operation_id,
                                  receipt.requested_description)
        return readback

    async def reconcile_update(self, selection: SpaceSelection, creation_operation_id: str,
                               update_operation_id: str):
        receipt = await self._binding_receipt(selection, creation_operation_id)
        if receipt.update_operation_id != update_operation_id:
            raise ValueError("update operation does not match pinned intent")
        if receipt.state not in {"update_effect_started", "update_outcome_unknown", "ready"}:
            raise ValueError("update effect has not started")
        if self.adapter.read_description(selection, receipt.native_space_id) != receipt.requested_description:
            return None
        await self._record_update(selection.tenant_id, creation_operation_id, update_operation_id,
                                  receipt.requested_description)
        return self.adapter._read(selection, receipt.native_space_id, created=True,
                                  fingerprint=selection.fingerprint())

    async def _record_update(self, tenant_id: str, creation_operation_id: str,
                             update_operation_id: str, description: str) -> None:
        async with self.sessions() as session:
            await session.execute(update(DevopsSpaceIntent).where(
                DevopsSpaceIntent.tenant_id == tenant_id,
                DevopsSpaceIntent.operation_id == creation_operation_id,
                DevopsSpaceIntent.update_operation_id == update_operation_id,
                DevopsSpaceIntent.state.in_(("update_effect_started", "update_outcome_unknown"))).values(
                    state="ready", effective_description=description))
            await session.commit()

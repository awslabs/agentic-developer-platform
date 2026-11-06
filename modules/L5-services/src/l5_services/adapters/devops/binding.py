"""Native DevOps space setup under the shared durable operation and broker lease."""

import asyncio
from hashlib import sha256

from botocore.exceptions import BotoCoreError, ClientError

from l5_services.adapters.devops.prevention import PreventionAdapter
from l5_services.devops.setup import original_setup, setup_selection


class DevopsBindingAdapter:
    def __init__(self, client, identity, intent, *, sessions=None, resource_session=None, resource_expiry=None):
        self.client = client
        self.identity = {field: identity[field] for field in ("account_id", "region", "role_arn")}
        self.intent = intent
        self.sessions = sessions
        self.resource_session = resource_session
        self.resource_expiry = resource_expiry

    def _selection(self, verified):
        if any(self.identity.get(field) != verified.get(field) or not verified.get(field)
               for field in ("account_id", "region", "role_arn")):
            raise PermissionError("DevOps setup provider identity changed")
        return setup_selection(self.intent, verified)

    async def _call(self, operation, **parameters):
        try:
            return await asyncio.to_thread(getattr(self.client, operation), **parameters)
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("DevOps native setup outcome unavailable") from error

    async def _prevention(self, selection, space_id):
        prevention = PreventionAdapter(self.client, account_id=selection.account_id, region=selection.region)
        try:
            return await asyncio.to_thread(prevention.read, selection, space_id, self.intent.request["configuration_revision"])
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("DevOps prevention readback unavailable") from error

    async def _read(self, space_id, verified, *, created):
        selection = self._selection(verified)
        fingerprint = selection.fingerprint()
        observed = await self._call("get_agent_space", agentSpaceId=space_id)
        space = observed.get("agentSpace") or {}
        if (space.get("agentSpaceId") != space_id or (created and (
                (observed.get("tags") or {}).get("adp:scope") != fingerprint
                or space.get("name") != f"adp-devops-{fingerprint[:32]}"))):
            raise PermissionError("DevOps native setup scope changed")
        observation = await self._prevention(selection, space_id)
        if created:
            for goal in (observation.get("native_schedule") or {}).get("goals", []):
                if goal["state"] == "ENABLED":
                    token = sha256(f"{self.intent.operation_id}:{goal['goal_id']}:disabled".encode()).hexdigest()
                    await self._call("update_goal", agentSpaceId=space_id, goalId=goal["goal_id"],
                                     evaluationSchedule={"state": "DISABLED"}, clientToken=token)
            observation = await self._prevention(selection, space_id)
        schedule = observation.get("native_schedule")
        gaps = ([] if isinstance(schedule, dict) and schedule.get("enabled") is False
                else ["native_prevention_unverified" if schedule is None else "native_prevention_enabled"])
        reference = {**verified["connection_ref"], "record_type": "agent_space", "record_id": space_id, "record_revision": space_id}
        native = {"native_resource_ref": reference, "provider_identity": self.identity,
                  "state": "blocked" if gaps else "ready", "gaps": gaps,
                  "readiness": [{"kind": "native_prevention", "observation": observation}],
                  "prevention_observation": observation}
        if created:
            native["creation_receipt"] = {"operation_id": self.intent.operation_id, "intent_hash": self.intent.intent_hash,
                                          "native_resource_ref": reference, "provider_identity": self.identity,
                                          "scope_fingerprint": fingerprint}
        return native

    async def create(self, intent, verified):
        if intent.operation_id != self.intent.operation_id or intent.request["action"] != "create":
            raise PermissionError("DevOps creation intent changed")
        selection = self._selection(verified)
        fingerprint = selection.fingerprint()
        response = await self._call("create_agent_space", name=f"adp-devops-{fingerprint[:32]}",
                                    clientToken=sha256(f"{fingerprint}:{intent.operation_id}".encode()).hexdigest(),
                                    tags={"adp:scope": fingerprint})
        space_id = (response.get("agentSpace") or {}).get("agentSpaceId")
        if not isinstance(space_id, str) or not space_id:
            return None
        try:
            return await self._read(space_id, verified, created=True)
        except (ConnectionError, TimeoutError, PermissionError, ValueError):
            return {"candidate_id": space_id}

    async def reconcile(self, intent, verified, candidate_id=None):
        if intent.operation_id != self.intent.operation_id or intent.request["action"] != "create":
            raise PermissionError("DevOps creation intent changed")
        if candidate_id:
            return await self._read(candidate_id, verified, created=True)
        name = "adp-devops-" + self._selection(verified).fingerprint()[:32]
        matches, visited, token = [], set(), None
        for _page in range(10):
            page = await self._call("list_agent_spaces", **({"nextToken": token} if token else {}))
            if not isinstance(page.get("agentSpaces"), list):
                raise ValueError("DevOps setup listing unavailable")
            matches.extend(space["agentSpaceId"] for space in page["agentSpaces"]
                           if space.get("name") == name and space.get("agentSpaceId"))
            token = page.get("nextToken")
            if not token:
                return await self._read(matches[0], verified, created=True) if len(matches) == 1 else None
            if not isinstance(token, str) or token in visited:
                raise ValueError("DevOps setup listing did not advance")
            visited.add(token)
        raise ValueError("DevOps setup listing exceeded bounds")

    async def read(self, reference, verified):
        if (reference.get("tenant_id"), reference.get("project_id"), reference.get("source_connection_id"), reference.get("record_type")) != (
                self.intent.tenant_id, self.intent.project_id, verified["connection_ref"]["source_connection_id"], "agent_space"):
            raise PermissionError("DevOps adopted space scope changed")
        native = await self._read(reference["record_id"], verified, created=False)
        native["native_resource_ref"] = reference
        return native

    async def verify_resource(self, binding, verified):
        from datetime import datetime, timedelta

        from l5_services.adapters.devops.aws_access import AWSAccessAdapter
        from l5_services.devops.lifecycle import SpaceLifecycle
        from l5_services.devops.resource_scope import ResourceSelection

        raw = self.intent.request.get("resource_selection")
        if (self.intent.authorization.get("purpose") != "devops_resource_verification"
                or raw != verified.get("resource_selection") or self.resource_session is None
                or self.sessions is None):
            raise PermissionError("resource_verification_consent_required")
        async with self.sessions() as session:
            setup = await original_setup(session, binding, require_setup_consent=binding.ownership == "customer_linked")
            setup_operation_id = setup.operation_id
        resource = ResourceSelection(**raw)
        native = await self.read(binding.native_resource_ref, verified)
        if native["gaps"]:
            return native
        selected = self._selection(verified)
        adapter = AWSAccessAdapter(None)
        provider, expiry = self.resource_session, self.resource_expiry

        class InstalledAccess:
            async def verify(self, selection, **target):
                if selection != resource:
                    raise PermissionError("resource_selection_changed")
                observation = await adapter.verify_session(
                    selection, session=provider, expiry=expiry, inline_only=True,
                    **{key: target[key] for key in ("host_account", "space_arn", "host_client", "space_id", "reconcile_only")})
                # Freshness of retained readbacks never extends setup credentials.
                observation["credential_expires_at"] = observation["expires_at"]
                observation["expires_at"] = (datetime.fromisoformat(observation["checked_at"]) + timedelta(minutes=15)).isoformat()
                return observation

        lifecycle = SpaceLifecycle(self.sessions, self)
        observation = await lifecycle.verify_resource_access(
            selected, setup_operation_id, resource, InstalledAccess(),
            delegation_id=self.intent.authorization["delegation_id"],
            delegation_revision=self.intent.authorization["delegation_revision"], attempt_id=self.intent.operation_id)
        native["readiness"].append({"kind": "resource_access", "observation": observation})
        if observation.get("status") != "verifying" or observation.get("reason"):
            native.update(state="blocked", gaps=[observation.get("reason") or "resource_access_unverified"])
        return native

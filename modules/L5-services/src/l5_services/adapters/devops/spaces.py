"""Native space operations with a client from the verified hosting connection."""

import hashlib
import json
from dataclasses import dataclass


class SpaceIdentityError(ValueError):
    """The observed space does not match the selected binding."""


@dataclass(frozen=True)
class SpaceSelection:
    tenant_id: str
    product_id: str
    project_id: str
    binding_id: str
    account_id: str
    region: str

    def fingerprint(self) -> str:
        values = (self.tenant_id, self.product_id, self.project_id, self.binding_id,
                  self.account_id, self.region)
        if any(not isinstance(value, str) or not value for value in values):
            raise SpaceIdentityError("incomplete space selection")
        if not (self.account_id.isascii() and self.account_id.isdigit() and len(self.account_id) == 12):
            raise SpaceIdentityError("invalid hosting account")
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SpaceReadback:
    agent_space_id: str
    account_id: str
    region: str
    ownership: str
    scope_fingerprint: str | None = None


class SpaceAdapter:
    def __init__(self, client, *, account_id: str, region: str):
        self.client = client
        self.account_id = account_id
        self.region = region

    def _check_target(self, selection: SpaceSelection) -> str:
        fingerprint = selection.fingerprint()
        if (selection.account_id, selection.region) != (self.account_id, self.region):
            raise SpaceIdentityError("hosting connection does not match selected account and Region")
        return fingerprint

    def _read(self, selection: SpaceSelection, agent_space_id: str, *, created: bool,
              fingerprint: str | None = None) -> SpaceReadback:
        if not isinstance(agent_space_id, str) or not agent_space_id:
            raise SpaceIdentityError("missing native space ID")
        response = self.client.get_agent_space(agentSpaceId=agent_space_id)
        observed = response.get("agentSpace") or {}
        if observed.get("agentSpaceId") != agent_space_id:
            raise SpaceIdentityError("provider returned a different native space ID")
        if created:
            if (response.get("tags") or {}).get("adp:scope") != fingerprint:
                raise SpaceIdentityError("native creation receipt does not match binding scope")
            if observed.get("name") != f"adp-devops-{fingerprint[:32]}":
                raise SpaceIdentityError("native space name does not match binding scope")
        return SpaceReadback(agent_space_id, selection.account_id, selection.region,
                             "adp_created" if created else "customer_linked",
                             fingerprint if created else None)

    def adopt(self, selection: SpaceSelection, agent_space_id: str) -> SpaceReadback:
        self._check_target(selection)
        return self._read(selection, agent_space_id, created=False)

    def reconcile_create(self, selection: SpaceSelection) -> SpaceReadback | None:
        fingerprint = self._check_target(selection)
        name = f"adp-devops-{fingerprint[:32]}"
        matching = []
        next_token = None
        while True:
            request = {"nextToken": next_token} if next_token else {}
            listing = self.client.list_agent_spaces(**request)
            matching.extend(space["agentSpaceId"] for space in listing.get("agentSpaces", [])
                            if space.get("name") == name)
            next_token = listing.get("nextToken")
            if not next_token:
                break
        if len(matching) > 1:
            raise SpaceIdentityError("ambiguous native spaces for binding")
        if matching:
            return self._read(selection, matching[0], created=True, fingerprint=fingerprint)
        return None

    def create(self, selection: SpaceSelection, operation_id: str) -> SpaceReadback:
        fingerprint = self._check_target(selection)
        if not isinstance(operation_id, str) or not operation_id:
            raise SpaceIdentityError("missing durable operation ID")
        existing = self.reconcile_create(selection)
        if existing is not None:
            return existing
        name = f"adp-devops-{fingerprint[:32]}"
        token = hashlib.sha256(f"{fingerprint}:{operation_id}".encode()).hexdigest()
        created = self.client.create_agent_space(name=name, clientToken=token,
                                                 tags={"adp:scope": fingerprint})
        agent_space_id = (created.get("agentSpace") or {}).get("agentSpaceId")
        return self._read(selection, agent_space_id, created=True, fingerprint=fingerprint)

    def _pages(self, method, agent_space_id: str, field: str) -> list[dict]:
        items = []
        next_token = None
        while True:
            arguments = {"agentSpaceId": agent_space_id}
            if next_token:
                arguments["nextToken"] = next_token
            page = method(**arguments)
            items.extend(page.get(field, []))
            next_token = page.get("nextToken")
            if not next_token:
                return items

    def observe_work(self, selection: SpaceSelection, agent_space_id: str) -> list[str]:
        self._read(selection, agent_space_id, created=True, fingerprint=self._check_target(selection))
        executions = self._pages(self.client.list_executions, agent_space_id, "executions")
        if any(item.get("agentSpaceId") != agent_space_id or not item.get("executionId")
               for item in executions):
            raise SpaceIdentityError("execution readback outside selected native space")
        if any(item.get("executionStatus") not in {"RUNNING", "WAITING", "FAILED", "STOPPED", "CANCELED", "TIMED_OUT"}
               for item in executions):
            raise SpaceIdentityError("unknown native execution status")
        return [item["executionId"] for item in executions
                if item.get("executionStatus") in {"RUNNING", "WAITING"}]

    def _triggers(self, selection: SpaceSelection, agent_space_id: str) -> dict[str, str]:
        self._read(selection, agent_space_id, created=True, fingerprint=self._check_target(selection))
        triggers = self._pages(self.client.list_triggers, agent_space_id, "items")
        if any(item.get("agentSpaceId") != agent_space_id or not item.get("triggerId")
               for item in triggers):
            raise SpaceIdentityError("trigger readback outside selected native space")
        states = {item["triggerId"]: item.get("status") for item in triggers}
        if len(states) != len(triggers):
            raise SpaceIdentityError("duplicate native trigger readback")
        return states

    def pause_owned_triggers(self, selection: SpaceSelection, agent_space_id: str,
                             owned_trigger_ids: list[str]) -> list[str]:
        states = self._triggers(selection, agent_space_id)
        if any(status not in {"ENABLED", "DISABLED"} for status in states.values()):
            raise SpaceIdentityError("unknown native automation status")
        if any(status == "ENABLED" and trigger_id not in owned_trigger_ids
               for trigger_id, status in states.items()):
            raise SpaceIdentityError("unowned native automation remains enabled")
        if any(trigger_id not in states for trigger_id in owned_trigger_ids):
            raise SpaceIdentityError("owned native trigger missing")
        enabled = [trigger_id for trigger_id in owned_trigger_ids if states[trigger_id] == "ENABLED"]
        for trigger_id in enabled:
            self.client.update_trigger(agentSpaceId=agent_space_id, triggerId=trigger_id, status="DISABLED")
        if any(status != "DISABLED" for status in self._triggers(selection, agent_space_id).values()):
            raise SpaceIdentityError("native automation was not paused")
        return enabled

    def restore_owned_triggers(self, selection: SpaceSelection, agent_space_id: str,
                               enabled_before_suspend: list[str]) -> None:
        states = self._triggers(selection, agent_space_id)
        if any(states.get(trigger_id) not in {"DISABLED", "ENABLED"} for trigger_id in enabled_before_suspend):
            raise SpaceIdentityError("original native triggers changed during suspension")
        for trigger_id in enabled_before_suspend:
            if states[trigger_id] == "DISABLED":
                self.client.update_trigger(agentSpaceId=agent_space_id, triggerId=trigger_id, status="ENABLED")
        states = self._triggers(selection, agent_space_id)
        if any(states.get(trigger_id) != "ENABLED" for trigger_id in enabled_before_suspend):
            raise SpaceIdentityError("native automation did not resume")

    def validate_owned_cleanup(self, selection: SpaceSelection, agent_space_id: str) -> None:
        self._read(selection, agent_space_id, created=True, fingerprint=self._check_target(selection))
        if self._pages(self.client.list_associations, agent_space_id, "associations"):
            raise SpaceIdentityError("native associations must be retained or explicitly unlinked")
        if self._pages(self.client.list_assets, agent_space_id, "items"):
            raise SpaceIdentityError("native assets must be retained or explicitly removed")

    def delete_owned(self, selection: SpaceSelection, agent_space_id: str) -> None:
        self.validate_owned_cleanup(selection, agent_space_id)
        self.client.delete_agent_space(agentSpaceId=agent_space_id)

    def read_description(self, selection: SpaceSelection, agent_space_id: str) -> str | None:
        self._read(selection, agent_space_id, created=True, fingerprint=self._check_target(selection))
        observation = self.client.get_agent_space(agentSpaceId=agent_space_id).get("agentSpace") or {}
        if observation.get("agentSpaceId") != agent_space_id:
            raise SpaceIdentityError("provider returned a different native space ID")
        return observation.get("description")

    def update_description(self, selection: SpaceSelection, agent_space_id: str,
                           description: str) -> SpaceReadback:
        if not isinstance(description, str) or not 1 <= len(description) <= 1000 or not description.strip():
            raise ValueError("native description must be a nonempty string of at most 1000 characters")
        fingerprint = self._check_target(selection)
        self._read(selection, agent_space_id, created=True, fingerprint=fingerprint)
        before = self.client.get_agent_space(agentSpaceId=agent_space_id).get("agentSpace") or {}
        if before.get("agentSpaceId") != agent_space_id:
            raise SpaceIdentityError("provider returned a different native space ID")
        before_preferences = dict(before.get("preferences", {}))
        self.client.update_agent_space(agentSpaceId=agent_space_id, description=description)
        readback = self._read(selection, agent_space_id, created=True, fingerprint=fingerprint)
        after = self.client.get_agent_space(agentSpaceId=agent_space_id).get("agentSpace") or {}
        if (after.get("agentSpaceId") != agent_space_id
                or after.get("description") != description
                or after.get("preferences", {}) != before_preferences):
            raise SpaceIdentityError("updated space description or automation preferences not verified")
        return readback

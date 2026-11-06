"""Conservative Security Agent space creation and independent readback."""

from asyncio import to_thread
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from l5_services.adapters.security.model_binding import read_model
from l5_services.security_registration import observed_registration


class SecurityBindingAdapter:
    def __init__(self, client, provider_identity, *, scope_document=None):
        if not {"account_id", "role_arn", "region", "verified_at"} <= provider_identity.keys():
            raise ValueError("verified provider identity is required")
        self.client = client
        self.provider_identity = provider_identity
        self.scope_document = scope_document

    @staticmethod
    def _name(intent):
        return f"adp-{intent.operation_id}"

    @staticmethod
    def _description(intent):
        return f"ADP setup {intent.intent_hash}"

    def _require_authority(self, intent, verified):
        if (verified["account_id"], verified["role_arn"], verified["region"]) != (
                self.provider_identity["account_id"], self.provider_identity["role_arn"],
                self.provider_identity["region"]):
            raise PermissionError("selected provider identity changed")
        if intent.request["action"] != "create":
            raise ValueError("creation requires the original create intent")

    async def _read(self, method, **parameters):
        try:
            return await to_thread(method, **parameters)
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("native_readback_unavailable") from error

    async def _readback(self, intent, agent_space_id, verified):
        response = await self._read(self.client.batch_get_agent_spaces, agentSpaceIds=[agent_space_id])
        matches = [item for item in response.get("agentSpaces", []) if item.get("agentSpaceId") == agent_space_id]
        if len(matches) != 1 or matches[0].get("name") != self._name(intent) or (
                matches[0].get("description") != self._description(intent)):
            return None
        created_at = matches[0].get("createdAt")
        if not isinstance(created_at, datetime):
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        reference = {"tenant_id": intent.tenant_id, "project_id": intent.project_id,
                     "source_connection_id": verified["connection_ref"]["source_connection_id"],
                     "record_type": "security_space", "record_id": agent_space_id,
                     "record_revision": created_at.isoformat()}
        receipt = {"operation_id": intent.operation_id, "intent_hash": intent.intent_hash,
                   "native_resource_ref": reference, "provider_identity": self.provider_identity}
        return {"native_resource_ref": reference, "provider_identity": self.provider_identity,
                "creation_receipt": receipt, "state": "blocked", "readiness": [],
                "gaps": ["source_readiness_unverified"]}

    async def create(self, intent, verified):
        self._require_authority(intent, verified)
        try:
            response = await to_thread(
                self.client.create_agent_space, name=self._name(intent),
                description=self._description(intent),
                tags={"adp-setup-operation": intent.operation_id},
            )
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("native_create_outcome_unknown") from error
        agent_space_id = response.get("agentSpaceId")
        if not isinstance(agent_space_id, str) or not agent_space_id:
            return None
        try:
            native = await self._readback(intent, agent_space_id, verified)
        except (BotoCoreError, ClientError, ConnectionError, TimeoutError):
            native = None
        return native or {"candidate_id": agent_space_id}

    async def reconcile(self, intent, verified, candidate_id=None):
        self._require_authority(intent, verified)
        if candidate_id:
            candidate = await self._readback(intent, candidate_id, verified)
            if candidate is not None:
                return candidate
        matches = []
        token = None
        visited = set()
        for _ in range(10):
            parameters = {"maxResults": 50}
            if token is not None:
                parameters["nextToken"] = token
            page = await self._read(self.client.list_agent_spaces, **parameters)
            matches.extend(item["agentSpaceId"] for item in page.get("agentSpaceSummaries", [])
                           if item.get("name") == self._name(intent) and item.get("agentSpaceId"))
            token = page.get("nextToken")
            if not token:
                if len(matches) != 1:
                    return None
                return await self._readback(intent, matches[0], verified)
            if token in visited:
                return None
            visited.add(token)
        return None

    async def read(self, reference, verified, *, binding=None, selected_model=None):
        if any(verified[field] != self.provider_identity[field] for field in ("account_id", "role_arn", "region")):
            raise PermissionError("selected provider identity changed")
        response = await self._read(self.client.batch_get_agent_spaces, agentSpaceIds=[reference["record_id"]])
        matches = [space for space in response.get("agentSpaces", [])
                   if space.get("agentSpaceId") == reference["record_id"]]
        if len(matches) != 1:
            raise ValueError("native_identity_unconfirmed")
        created_at = matches[0].get("createdAt")
        if not isinstance(created_at, datetime) or created_at.isoformat() != reference["record_revision"]:
            raise ValueError("native_scope_mismatch")
        registration = observed_registration(matches[0].get("awsResources"), verified["account_id"])
        if registration is not None:
            readiness = [{"kind": "code_review_source_registration", "registration": registration,
                          "observed_at": datetime.now(UTC).isoformat()}]
            if binding is not None:
                receipt = binding.creation_receipt or {}
                native = matches[0]
                if (binding.ownership == "adp_created" and receipt.get("native_resource_ref") == reference
                        and receipt.get("provider_identity") == binding.provider_identity
                        and all(binding.provider_identity.get(field) == self.provider_identity[field]
                                for field in ("account_id", "role_arn", "region"))
                        and receipt.get("operation_id") and receipt.get("intent_hash")
                        and native.get("name") == f'adp-{receipt["operation_id"]}'
                        and native.get("description") == f'ADP setup {receipt["intent_hash"]}'):
                    model = await read_model(self.client, reference, registration, binding, selected_model,
                                             scope_document=self.scope_document)
                    if model:
                        readiness.append(model)
            return {"native_resource_ref": reference, "provider_identity": self.provider_identity,
                    "state": "ready", "readiness": readiness, "gaps": []}
        previous_model = ([item for item in binding.readiness or []
                           if item.get("kind") in {"threat_model", "threat_model_outcome_unknown",
                                                   "threat_model_selection_required"}]
                          if binding is not None else [])
        return {"native_resource_ref": reference, "provider_identity": self.provider_identity,
                "state": "blocked", "readiness": previous_model, "gaps": ["source_readiness_unverified"]}

    async def cleanup(self, reference, verified, receipt):
        if any(verified[field] != self.provider_identity[field] for field in ("account_id", "role_arn", "region")):
            raise PermissionError("selected provider identity changed")
        if (receipt.get("native_resource_ref") != reference
                or not isinstance(receipt.get("provider_identity"), dict)
                or any(receipt["provider_identity"].get(field) != self.provider_identity[field]
                       for field in ("account_id", "role_arn", "region"))):
            raise ValueError("ownership_required")
        response = await self._read(self.client.batch_get_agent_spaces, agentSpaceIds=[reference["record_id"]])
        matches = [space for space in response.get("agentSpaces", [])
                   if space.get("agentSpaceId") == reference["record_id"]]
        if len(matches) != 1:
            return False
        native = matches[0]
        created_at = native.get("createdAt")
        if (not isinstance(created_at, datetime) or created_at.isoformat() != reference["record_revision"]
                or native.get("name") != f'adp-{receipt["operation_id"]}'
                or native.get("description") != f'ADP setup {receipt["intent_hash"]}'):
            raise ValueError("ownership_required")
        try:
            await to_thread(self.client.delete_agent_space, agentSpaceId=reference["record_id"])
        except (BotoCoreError, ClientError) as error:
            raise ConnectionError("native_cleanup_outcome_unknown") from error
        readback = await self._read(self.client.batch_get_agent_spaces, agentSpaceIds=[reference["record_id"]])
        if readback.get("agentSpaces") or readback.get("notFound") != [reference["record_id"]]:
            return False
        token = None
        visited = set()
        for _ in range(10):
            parameters = {"maxResults": 50}
            if token is not None:
                parameters["nextToken"] = token
            page = await self._read(self.client.list_agent_spaces, **parameters)
            if any(space.get("agentSpaceId") == reference["record_id"]
                   for space in page.get("agentSpaceSummaries", [])):
                return False
            token = page.get("nextToken")
            if not token:
                return True
            if token in visited:
                return False
            visited.add(token)
        return False

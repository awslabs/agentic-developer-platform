"""Installed knowledge assembly and per-call scoped provider boundary."""

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json

from botocore.config import Config

from l5_services.devops.knowledge_admission import digest
from l5_services.devops.knowledge_contract import native_contract
from l5_services.devops.knowledge_native import KnowledgeNativeDriver
from l5_services.devops.knowledge_sync import source_key
from l5_services.devops.knowledge_worker import KnowledgeSetupWorker
from l5_services.workers.execution import AuthorityRevoked


# Admission is blocked until the owner records separate native consent and
# explicitly resumes the same retained work. Native-use acceptance is a later
# readback gate, so the first controlled publication can qualify this path.


async def knowledge_worker(
    sessions, sources, transport, provider_factory, intent, claim
):
    selected = intent.authorization["selection"]
    admission = intent.authorization["knowledge_admission"]
    authority = intent.authorization
    request = {
        "delegation_id": authority["delegation_id"],
        "delegation_revision": authority["delegation_revision"],
        "operation_id": intent.operation_id,
        "attempt_id": claim.work_id,
        "lease_owner": claim.owner,
        "lease_fence": claim.fence,
        "connection_ref": {**selected["connection_ref"], "record_type": "credential"},
    }
    identity = selected["provider_identity"]
    pinned_native = None

    async def verify(*_):
        nonlocal pinned_native
        response = await transport.post(
            "/internal/v1/l5/knowledge-publication-session", json=request
        )
        if response.status_code in {401, 403, 404, 409, 410}:
            raise AuthorityRevoked("knowledge_native_authority_denied")
        response.raise_for_status()
        result = response.json()
        verified = result.get("setup_verified") or {}
        scope = verified.get("scope") or {}
        observed_identity = result.get("provider_identity") or {}
        current_native = (
            result.get("native_delegation_id"),
            result.get("native_delegation_revision"),
        )
        if (
            result.get("source_delegation_id") != authority["delegation_id"]
            or result.get("source_delegation_revision")
            != authority["delegation_revision"]
            or result.get("operation_id") != intent.operation_id
            or result.get("attempt_id") != claim.work_id
            or verified.get("knowledge_native") is not True
            or verified.get("intent_hash") != intent.intent_hash
            or scope.get("purpose") != "devops_knowledge_native_publication"
            or scope.get("admission_hash") != digest(admission)
            or scope.get("snapshot_ids")
            != [item["snapshot_id"] for item in admission["source_snapshots"]]
            or scope.get("source_delegation_id") != authority["delegation_id"]
            or scope.get("source_delegation_revision")
            != authority["delegation_revision"]
            or verified.get("binding_id") != intent.request["binding_id"]
            or verified.get("binding_generation") != intent.binding_generation
            or verified.get("native_space_ref") != selected["native_space_ref"]
            or verified.get("provider_identity") != identity
            or any(
                observed_identity.get(key) != value for key, value in identity.items()
            )
            or not current_native[0]
            or type(current_native[1]) is not int
            or current_native[1] < 1
            or pinned_native is not None
            and current_native != pinned_native
        ):
            raise AuthorityRevoked("knowledge_native_scope_changed")
        try:
            expiry = datetime.fromisoformat(result["expires_at"])
            if expiry.tzinfo is None or not datetime.now(
                UTC
            ) < expiry <= claim.lease_until.replace(
                tzinfo=claim.lease_until.tzinfo or UTC
            ):
                raise ValueError("invalid deadline")
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityRevoked("knowledge_native_session_expired") from error
        pinned_native = current_native
        return result, expiry

    initial, deadline = await verify()
    provider = provider_factory(initial, identity["region"])
    client = provider.client(
        "devops-agent",
        region_name=identity["region"],
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 0}),
    )
    scoped = KnowledgeScopedClient(
        client, intent, claim, verify, deadline, asyncio.get_running_loop()
    )
    return KnowledgeSetupWorker(
        sessions, sources, KnowledgeNativeDriver(scoped), verify
    )


class KnowledgeScopedClient:
    """Only accepted sources and exact selected-space asset calls can escape."""

    METHODS = {
        "list_assets",
        "get_asset",
        "get_asset_content",
        "create_asset",
        "update_asset",
    }

    def __init__(self, client, intent, claim, verify, deadline, loop):
        self.client, self.intent, self.claim = client, intent, claim
        self.verify, self.deadline, self.loop = verify, deadline, loop
        admission = intent.authorization["knowledge_admission"]
        self.space_id = intent.authorization["selection"]["native_space_ref"][
            "record_id"
        ]
        self.selected, self.known = {}, {}
        for publication, snapshot in zip(
            admission["publications"], admission["source_snapshots"], strict=True
        ):
            kind, path, native, limit = native_contract(
                publication["kind"],
                publication["scope"],
                publication["active"],
                admission["visibility"],
                publication["native_metadata"],
            )
            key = source_key(publication["source_ref"])
            metadata = {
                **native,
                "adp_source_ref": publication["source_ref"],
                "adp_source_hash": snapshot["content"]["sha256"],
                "adp_mapping_version": admission["mapping_version"],
                "adp_scope": publication["scope"],
                "adp_visibility": admission["visibility"],
                "adp_active": publication["active"],
                "adp_source_key": key,
                "adp_publication_operation": intent.operation_id,
            }
            token = sha256(
                json.dumps(
                    [intent.operation_id, key, metadata], sort_keys=True
                ).encode()
            ).hexdigest()
            self.selected[key] = {
                "metadata": metadata,
                "kind": kind,
                "path": path,
                "limit": limit,
                "token": token,
            }

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AuthorityRevoked("knowledge_native_method_outside_scope")

        def call(**request):
            self._request(name, request)
            if datetime.now(UTC) >= self.deadline:
                raise AuthorityRevoked("knowledge_native_session_expired")
            pending = asyncio.run_coroutine_threadsafe(self.verify(), self.loop)
            try:
                pending.result(timeout=12)
            except Exception as error:
                pending.cancel()
                raise AuthorityRevoked("knowledge_native_authority_denied") from error
            result = getattr(self.client, name)(**request)
            if name == "list_assets":
                for item in result.get("items", []):
                    if (
                        isinstance(item, dict)
                        and (item.get("metadata") or {}).get("adp_source_key")
                        in self.selected
                    ):
                        self.known[item.get("assetId")] = item
            elif name in {"get_asset", "create_asset", "update_asset"}:
                item = result.get("asset") or {}
                if (item.get("metadata") or {}).get("adp_source_key") in self.selected:
                    self.known[item.get("assetId")] = item
            return result

        return call

    def _request(self, name, request):
        fields = {
            "list_assets": {"agentSpaceId", "nextToken"},
            "get_asset": {"agentSpaceId", "assetId"},
            "get_asset_content": {"agentSpaceId", "assetId", "assetVersion"},
            "create_asset": {
                "agentSpaceId",
                "assetType",
                "metadata",
                "content",
                "clientToken",
            },
            "update_asset": {
                "agentSpaceId",
                "assetId",
                "metadata",
                "content",
                "clientToken",
            },
        }
        if request.get("agentSpaceId") != self.space_id or set(request) - fields[name]:
            raise AuthorityRevoked("knowledge_native_request_outside_scope")
        if name in {"get_asset", "get_asset_content", "update_asset"}:
            current = self.known.get(request.get("assetId"))
            if current is None or (
                name == "get_asset_content"
                and request.get("assetVersion") != current.get("version")
            ):
                raise AuthorityRevoked("knowledge_native_asset_outside_scope")
        if name in {"create_asset", "update_asset"}:
            metadata = request.get("metadata") or {}
            selected = self.selected.get(metadata.get("adp_source_key"))
            content = (request.get("content") or {}).get("file") or {}
            text = (content.get("body") or {}).get("text")
            if (
                selected is None
                or metadata != selected["metadata"]
                or request.get("clientToken") != selected["token"]
                or content.get("path") != selected["path"]
                or not isinstance(text, str)
                or len(text.encode()) > selected["limit"]
                or sha256(text.encode()).hexdigest() != metadata["adp_source_hash"]
                or name == "create_asset"
                and request.get("assetType") != selected["kind"]
                or name == "update_asset"
                and (current.get("metadata") or {}).get("adp_source_key")
                != metadata["adp_source_key"]
            ):
                raise AuthorityRevoked("knowledge_native_publication_outside_scope")

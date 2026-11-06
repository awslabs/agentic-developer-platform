"""Bounded native knowledge plans with strictly read-only uncertainty recovery."""

from hashlib import sha256
import json

from l5_services.adapters.devops.assets import NativeKnowledgeAssets
from l5_services.devops.knowledge import KnowledgeAccessDenied
from l5_services.devops.knowledge_contract import native_contract
from l5_services.devops.knowledge_sync import source_key


class KnowledgeNativeDriver(NativeKnowledgeAssets):
    """Provider client must enforce exact space, current source rights and claim."""

    MAX_PAGES = 20
    MAX_ITEMS = 2000

    def _list_assets(self, space_id):
        items, visited, token = [], set(), None
        for _ in range(self.MAX_PAGES):
            result = self.client.list_assets(
                agentSpaceId=space_id, **({"nextToken": token} if token else {})
            )
            batch = result.get("items")
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise KnowledgeAccessDenied("knowledge_native_listing_incomplete")
            items.extend(batch)
            if len(items) > self.MAX_ITEMS:
                raise KnowledgeAccessDenied("knowledge_native_listing_limit")
            token = result.get("nextToken")
            if not token:
                return items
            if not isinstance(token, str) or token in visited:
                raise KnowledgeAccessDenied("knowledge_native_listing_incomplete")
            visited.add(token)
        raise KnowledgeAccessDenied("knowledge_native_listing_limit")

    def prepare(self, space_id, asset, operation_id, ownership):
        native_type, path, metadata, limit = native_contract(
            asset.kind,
            asset.scope,
            asset.active,
            asset.visibility,
            asset.native_metadata,
        )
        source = source_key(asset.source_ref)
        metadata = {
            **metadata,
            "adp_source_ref": asset.source_ref,
            "adp_source_hash": asset.source_hash,
            "adp_mapping_version": asset.mapping_version,
            "adp_scope": asset.scope,
            "adp_visibility": asset.visibility,
            "adp_active": asset.active,
            "adp_source_key": source,
            "adp_publication_operation": operation_id,
        }
        listing = self._list_assets(space_id)
        matches = [
            item
            for item in listing
            if (item.get("metadata") or {}).get("adp_source_key") == source
        ]
        expected_id = ownership.get("asset_id") if ownership else None
        if len(matches) > 1 or (matches and matches[0].get("assetId") != expected_id):
            # Native metadata alone never proves that ADP owns an existing asset.
            raise KnowledgeAccessDenied("knowledge_native_ownership_unverified")
        if native_type == "agents_md" and any(
            item.get("assetType") == native_type
            and (item.get("metadata") or {}).get("agent_type") == metadata["agent_type"]
            and item.get("assetId") != expected_id
            for item in listing
        ):
            raise KnowledgeAccessDenied("knowledge_instruction_target_occupied")
        prior_version, prior_metadata = None, None
        if expected_id:
            if not matches:
                raise KnowledgeAccessDenied("knowledge_owned_asset_missing")
            observed = (
                self.client.get_asset(agentSpaceId=space_id, assetId=expected_id).get(
                    "asset"
                )
                or {}
            )
            prior_version, prior_metadata = (
                ownership.get("version"),
                ownership.get("metadata"),
            )
            if (
                observed.get("assetId") != expected_id
                or observed.get("assetType") != native_type
                or observed.get("version") != prior_version
                or observed.get("metadata") != prior_metadata
            ):
                raise KnowledgeAccessDenied("knowledge_owned_asset_changed")
            if set(prior_metadata or {}) != set(metadata):
                raise KnowledgeAccessDenied("knowledge_native_metadata_changed")
        token = sha256(
            json.dumps([operation_id, source, metadata], sort_keys=True).encode()
        ).hexdigest()
        return {
            "space_id": space_id,
            "source_key": source,
            "source_ref": asset.source_ref,
            "source_hash": asset.source_hash,
            "native_type": native_type,
            "path": path,
            "limit": limit,
            "metadata": metadata,
            "asset_id": expected_id,
            "prior_version": prior_version,
            "prior_metadata": prior_metadata,
            "client_token": token,
            "action": "read"
            if prior_metadata == metadata
            else "update"
            if expected_id
            else "create",
        }

    def mutate(self, plan, asset):
        """Exactly one native mutation after a durable started event."""
        self._asset_matches(plan, asset)
        if plan["action"] == "read":
            return self.reconcile(plan, asset)
        content = {"file": {"path": plan["path"], "body": {"text": asset.content}}}
        if plan["action"] == "update":
            current = (
                self.client.get_asset(
                    agentSpaceId=plan["space_id"], assetId=plan["asset_id"]
                ).get("asset")
                or {}
            )
            if (
                current.get("assetId") != plan["asset_id"]
                or current.get("version") != plan["prior_version"]
                or current.get("metadata") != plan["prior_metadata"]
            ):
                raise KnowledgeAccessDenied("knowledge_owned_asset_changed")
            result = (
                self.client.update_asset(
                    agentSpaceId=plan["space_id"],
                    assetId=plan["asset_id"],
                    metadata=plan["metadata"],
                    content=content,
                    clientToken=plan["client_token"],
                ).get("asset")
                or {}
            )
        elif plan["action"] == "create":
            result = (
                self.client.create_asset(
                    agentSpaceId=plan["space_id"],
                    assetType=plan["native_type"],
                    metadata=plan["metadata"],
                    content=content,
                    clientToken=plan["client_token"],
                ).get("asset")
                or {}
            )
        else:
            raise KnowledgeAccessDenied("knowledge_native_plan_invalid")
        asset_id, version = result.get("assetId"), result.get("version")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or type(version) is not int
            or version < 1
            or plan["asset_id"] is not None
            and asset_id != plan["asset_id"]
            or plan["prior_version"] is not None
            and version != plan["prior_version"] + 1
        ):
            raise KnowledgeAccessDenied("knowledge_native_outcome_unknown")
        return self._receipt(plan, asset, asset_id, version)

    def reconcile(self, plan, asset):
        """Read only. Missing/changed assets never authorize repeating a mutation."""
        self._asset_matches(plan, asset)
        matches = [
            item
            for item in self._list_assets(plan["space_id"])
            if (item.get("metadata") or {}).get("adp_source_key") == plan["source_key"]
        ]
        if (
            len(matches) != 1
            or matches[0].get("metadata") != plan["metadata"]
            or plan["asset_id"] is not None
            and matches[0].get("assetId") != plan["asset_id"]
        ):
            raise KnowledgeAccessDenied("knowledge_native_outcome_unknown")
        expected = (
            plan["prior_version"]
            if plan["action"] == "read"
            else (
                plan["prior_version"] + 1 if plan["prior_version"] is not None else None
            )
        )
        return self._receipt(plan, asset, matches[0].get("assetId"), expected)

    @staticmethod
    def _asset_matches(plan, asset):
        if (
            asset.source_ref != plan["source_ref"]
            or asset.source_hash != plan["source_hash"]
            or sha256(asset.content.encode()).hexdigest() != plan["source_hash"]
            or len(asset.content.encode()) > plan["limit"]
        ):
            raise KnowledgeAccessDenied("knowledge_native_plan_changed")

    def _receipt(self, plan, asset, asset_id, version):
        observed = self._verify(
            plan["space_id"],
            asset_id,
            asset,
            plan["native_type"],
            plan["metadata"],
            plan["path"],
            plan["limit"],
            version=version,
        )
        return {
            "asset_id": observed["assetId"],
            "version": observed["version"],
            "metadata": observed["metadata"],
            "source_ref": plan["source_ref"],
            "source_hash": plan["source_hash"],
            "client_token": plan["client_token"],
            "content_written": True,
            "native_use_qualified": False,
        }

"""Publish scoped Product knowledge without changing unrelated native assets."""

from hashlib import sha256
from io import BytesIO
import json
from zipfile import BadZipFile, ZipFile

from l5_services.devops.knowledge import KnowledgeAccessDenied
from l5_services.devops.knowledge_contract import MAPPING_VERSION, native_contract


class NativeKnowledgeAssets:
    def __init__(self, client):
        self.client = client

    def _list_assets(self, space_id):
        items = []
        token = None
        visited = set()
        while True:
            response = self.client.list_assets(agentSpaceId=space_id,
                                               **({"nextToken": token} if token else {}))
            if not isinstance(response.get("items"), list):
                raise KnowledgeAccessDenied("native asset listing incomplete")
            items.extend(response["items"])
            token = response.get("nextToken")
            if not token:
                return items
            if token in visited:
                raise KnowledgeAccessDenied("native asset listing incomplete")
            visited.add(token)

    def _verify(self, space_id, asset_id, asset, native_type, metadata, path, limit, version=None):
        observed = (self.client.get_asset(agentSpaceId=space_id, assetId=asset_id)
                    .get("asset") or {})
        if (observed.get("assetId") != asset_id
                or observed.get("assetType") != native_type
                or observed.get("metadata") != metadata
                or type(observed.get("version")) is not int or observed["version"] < 1
                or (version is not None and observed["version"] != version)):
            raise KnowledgeAccessDenied("native asset readback mismatch")
        readback = self.client.get_asset_content(agentSpaceId=space_id, assetId=asset_id,
                                                 assetVersion=observed["version"])
        try:
            packed = readback["content"]["zipFile"]
            if not isinstance(packed, bytes) or len(packed) > limit + 65536:
                raise KnowledgeAccessDenied("native asset archive exceeds source bound")
            with ZipFile(BytesIO(packed)) as archive:
                if (readback.get("version") != observed["version"]
                        or archive.namelist() != [path]
                        or archive.getinfo(path).file_size > limit
                        or archive.getinfo(path).flag_bits & 1):
                    raise KnowledgeAccessDenied("native asset content readback mismatch")
                with archive.open(path) as contents:
                    if contents.read(limit + 1) != asset.content.encode("utf-8"):
                        raise KnowledgeAccessDenied("native asset content readback mismatch")
        except (KeyError, TypeError, BadZipFile) as error:
            raise KnowledgeAccessDenied("native asset content readback incomplete") from error
        return observed

    def publish(self, space_id: str, assets: list, *, operation_id: str,
                expected_asset_id: str | None = None, reconcile_only: bool = False) -> list[dict]:
        if not space_id or not operation_id:
            raise KnowledgeAccessDenied("native space and durable operation required")
        contracts = []
        instruction_targets = set()
        for asset in assets:
            try:
                contract = native_contract(asset.kind, asset.scope, asset.active, asset.visibility,
                                           asset.native_metadata)
            except ValueError as error:
                raise KnowledgeAccessDenied(str(error)) from error
            if (asset.mapping_version != MAPPING_VERSION or len(asset.content.encode("utf-8")) > contract[3]
                    or sha256(asset.content.encode("utf-8")).hexdigest() != asset.source_hash):
                raise KnowledgeAccessDenied("native knowledge mapping or content differs")
            if asset.kind == "instruction":
                target = contract[2]["agent_type"]
                if target in instruction_targets:
                    raise KnowledgeAccessDenied("one owned instruction source per native agent type required")
                instruction_targets.add(target)
            contracts.append(contract)
        listing = self._list_assets(space_id)
        results = []
        for asset, (native_type, path, native_metadata, limit) in zip(assets, contracts, strict=True):
            reference = asset.source_ref
            identity = {key: value for key, value in reference.items() if key != "record_revision"}
            source_key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            metadata = {**native_metadata, "adp_source_ref": reference, "adp_source_hash": asset.source_hash,
                        "adp_mapping_version": asset.mapping_version, "adp_scope": asset.scope,
                        "adp_visibility": asset.visibility, "adp_active": asset.active,
                        "adp_source_key": source_key}
            if native_type == "agents_md" and any(
                    item.get("assetType") == "agents_md"
                    and (item.get("metadata") or {}).get("agent_type") == native_metadata["agent_type"]
                    and (item.get("metadata") or {}).get("adp_source_key") != source_key for item in listing):
                raise KnowledgeAccessDenied("native instruction target already belongs to another source")
            matches = [item for item in listing if (item.get("metadata") or {}).get("adp_source_key") == source_key]
            if len(matches) > 1:
                raise KnowledgeAccessDenied("duplicate owned native assets")
            existing = matches[0] if matches else None
            if (expected_asset_id is not None and (not existing or existing.get("assetId") != expected_asset_id)
                    or reconcile_only and not existing):
                raise KnowledgeAccessDenied("uncertain native asset not reconciled")
            content = {"file": {"path": path, "body": {"text": asset.content}}}
            token = sha256(json.dumps([operation_id, source_key, metadata], sort_keys=True).encode()).hexdigest()
            if existing:
                asset_id = existing.get("assetId")
                old_metadata = existing.get("metadata") or {}
                old_reference = old_metadata.get("adp_source_ref") or {}
                if (not asset_id or existing.get("assetType") != native_type
                        or {key: value for key, value in old_reference.items() if key != "record_revision"} != identity):
                    raise KnowledgeAccessDenied("native asset ownership mismatch")
                if set(old_metadata) != set(metadata):
                    raise KnowledgeAccessDenied("native metadata changed outside the selected publication")
                if old_metadata == metadata:
                    observed = self._verify(space_id, asset_id, asset, native_type, metadata, path, limit)
                else:
                    written = self.client.update_asset(agentSpaceId=space_id, assetId=asset_id,
                                                       metadata=metadata, content=content,
                                                       clientToken=token).get("asset") or {}
                    if (written.get("assetId") != asset_id or type(written.get("version")) is not int
                            or written["version"] < 1):
                        raise KnowledgeAccessDenied("native asset update did not return identity")
                    observed = self._verify(space_id, asset_id, asset, native_type, metadata, path, limit,
                                            version=written["version"])
            else:
                written = self.client.create_asset(agentSpaceId=space_id, assetType=native_type,
                                                   metadata=metadata, content=content,
                                                   clientToken=token).get("asset") or {}
                asset_id = written.get("assetId")
                if not asset_id or type(written.get("version")) is not int or written["version"] < 1:
                    raise KnowledgeAccessDenied("native asset write did not return identity")
                observed = self._verify(space_id, asset_id, asset, native_type, metadata, path, limit,
                                        version=written["version"])
            results.append({"asset_id": observed["assetId"], "version": observed["version"],
                            "source_ref": reference, "source_hash": asset.source_hash,
                            "mapping_version": asset.mapping_version, "scope": asset.scope,
                            "visibility": asset.visibility, "active": asset.active})
        return results

    def revoke(self, space_id: str, source_key: str, asset_id: str | None, intent: dict) -> None:
        try:
            native_type, _, native_metadata, _ = native_contract(
                intent["kind"], intent["scope"], intent["active"], intent["visibility"], intent.get("native_metadata"))
        except ValueError as error:
            raise KnowledgeAccessDenied(str(error)) from error
        matches = [item for item in self._list_assets(space_id)
                   if (item.get("metadata") or {}).get("adp_source_key") == source_key]
        if len(matches) > 1 or (matches and asset_id is not None
                                 and matches[0].get("assetId") != asset_id):
            raise KnowledgeAccessDenied("native asset ownership mismatch on revocation")
        if matches:
            observed = matches[0]
            metadata = observed.get("metadata") or {}
            if (not observed.get("assetId") or observed.get("assetType") != native_type
                    or metadata.get("adp_source_ref") != intent["source_ref"]
                    or metadata.get("adp_source_hash") != intent["source_hash"]
                    or metadata.get("adp_scope") != intent["scope"]
                    or any(metadata.get(key) != value for key, value in native_metadata.items())):
                raise KnowledgeAccessDenied("native asset ownership mismatch on revocation")
            self.client.delete_asset(agentSpaceId=space_id, assetId=observed["assetId"])
        if any(item.get("assetId") == (asset_id or (matches[0]["assetId"] if matches else None))
               for item in self._list_assets(space_id)):
            raise KnowledgeAccessDenied("native asset cleanup unverified")

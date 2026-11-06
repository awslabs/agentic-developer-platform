"""Resolve explicitly selected, entitled Product knowledge for native publication."""

from dataclasses import dataclass
from hashlib import sha256
import json

from l5_services.devops.knowledge_contract import MAPPING_VERSION, native_contract


class KnowledgeAccessDenied(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeAsset:
    source_ref: dict
    source_hash: str
    mapping_version: int
    kind: str
    scope: str
    active: bool
    visibility: str
    content: str
    native_metadata: dict


async def resolve_knowledge(product, tenant_id: str, product_id: str, project_id: str,
                            policy: dict, publications: list[dict], *, delegation_id: str,
                            delegation_revision: int, operation_id: str, attempt_id: str,
                            lease_owner: str, lease_fence: int, retain_until: str) -> list[KnowledgeAsset]:
    """Verify each selected immutable source through the delegated Product snapshot API."""
    allowed = policy.get("source_refs")
    if (not isinstance(allowed, list) or not isinstance(publications, list)
            or policy.get("visibility") not in {"only_me", "product_team"}
            or not isinstance(lease_owner, str) or not 1 <= len(lease_owner) <= 255
            or not all((tenant_id, product_id, project_id, delegation_id, operation_id,
                        attempt_id, retain_until)) or delegation_revision < 1 or lease_fence < 1):
        raise KnowledgeAccessDenied("incomplete knowledge selection")
    discovered = await product.list_sources(product_id, project_id)
    if (discovered.get("coverage") != "complete" or discovered.get("next_cursor") is not None
            or not isinstance(discovered.get("items"), list)):
        raise KnowledgeAccessDenied("product source discovery incomplete")
    selected = []
    seen = set()
    for publication in publications:
        reference = publication.get("source_ref") or {}
        identity = tuple(reference.get(key) for key in (
            "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"))
        kind, scope, active = publication.get("kind"), publication.get("scope"), publication.get("active")
        if (any(not isinstance(part, str) or not part for part in identity)
                or identity in seen or reference not in allowed
                or identity[0] != tenant_id or identity[1] != project_id
                or identity[3] not in {"document", "runbook", "skill"}
                or kind not in {"instruction", "skill", "context"}
                or scope not in {"global", "agent"} or not isinstance(active, bool)
                or (kind == "context" and scope != "agent")
                or not any(item.get("connection_ref", {}).get("id") == identity[2]
                           and reference in item.get("resources", []) for item in discovered["items"])):
            raise KnowledgeAccessDenied("source outside selected project or publication scope")
        seen.add(identity)
        try:
            _, _, metadata, limit = native_contract(kind, scope, active, policy["visibility"],
                                                     publication.get("native_metadata"))
        except ValueError as error:
            raise KnowledgeAccessDenied(str(error)) from error
        selected.append((reference, kind, scope, active, metadata, limit))

    assets = []
    for reference, kind, scope, active, metadata, limit in selected:
        pin = {
            "delegation_id": delegation_id, "delegation_revision": delegation_revision,
            "source_ref": reference, "exclusions": [], "max_bytes": limit,
            "max_files": 1, "retain_until": retain_until,
        }
        key = sha256(json.dumps([tenant_id, product_id, project_id, operation_id, reference],
                                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        snapshot = await product.pin_source(product_id, project_id, pin, idempotency_key=key)
        if snapshot.get("source_ref") != reference or not snapshot.get("snapshot_id"):
            raise KnowledgeAccessDenied("source snapshot identity mismatch")
        read_request = {
            "delegation_id": delegation_id, "delegation_revision": delegation_revision,
            "operation_id": operation_id, "attempt_id": attempt_id, "lease_fence": lease_fence,
            "lease_owner": lease_owner,
        }
        receipt = await product.read_source(product_id, project_id, snapshot["snapshot_id"], read_request)
        if receipt != snapshot:
            raise KnowledgeAccessDenied("source receipt changed")
        content = bytearray()
        async for chunk in product.stream_source(product_id, project_id, snapshot["snapshot_id"], read_request):
            content.extend(chunk)
            if len(content) > pin["max_bytes"]:
                raise KnowledgeAccessDenied("knowledge source too large")
        artifact = snapshot.get("content") or {}
        digest = sha256(content).hexdigest()
        if artifact.get("bytes") != len(content) or artifact.get("sha256") != digest:
            raise KnowledgeAccessDenied("source content integrity mismatch")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KnowledgeAccessDenied("knowledge source is not UTF-8 text") from error
        assets.append(KnowledgeAsset(reference, digest, MAPPING_VERSION, kind, scope, active,
                                     policy["visibility"], text, metadata))
    return assets


async def synchronize_selected_knowledge(product, synchronizer, selection, binding_operation_id: str,
                                         policy: dict, publications: list[dict], *, delegation_id: str,
                                         delegation_revision: int, operation_id: str,
                                         attempt_id: str, lease_owner: str, lease_fence: int,
                                         retain_until: str) -> list[dict]:
    """Resolve the selected project's current Product sources before native effects."""
    if not isinstance(lease_owner, str) or not 1 <= len(lease_owner) <= 255:
        raise KnowledgeAccessDenied("current knowledge work lease owner required")
    discovered = await product.list_sources(selection.product_id, selection.project_id)
    if (discovered.get("coverage") != "complete" or discovered.get("next_cursor") is not None
            or not isinstance(discovered.get("items"), list)):
        raise KnowledgeAccessDenied("Product source discovery incomplete")
    available = [reference for item in discovered["items"] for reference in item.get("resources", [])
                 if item.get("connection_ref", {}).get("id") == reference.get("source_connection_id")]
    owned = await synchronizer.observations(selection, binding_operation_id)
    selected = []
    for publication in publications:
        reference = publication.get("source_ref")
        if reference not in (policy.get("source_refs") or []):
            raise KnowledgeAccessDenied("source outside selected knowledge policy")
        if reference in available:
            selected.append(publication)
        elif not any(row["source_ref"] == reference and row["state"] != "revoked" for row in owned):
            raise KnowledgeAccessDenied("unentitled knowledge was never published")
    assets = await resolve_knowledge(product, selection.tenant_id, selection.product_id,
                                     selection.project_id, policy, selected,
                                     delegation_id=delegation_id,
                                     delegation_revision=delegation_revision,
                                     operation_id=operation_id, attempt_id=attempt_id,
                                     lease_owner=lease_owner, lease_fence=lease_fence,
                                     retain_until=retain_until)
    current = await product.list_sources(selection.product_id, selection.project_id)
    if (current.get("coverage") != "complete" or current.get("next_cursor") is not None
            or not isinstance(current.get("items"), list)
            or any(not any(item.get("connection_ref", {}).get("id") == asset.source_ref["source_connection_id"]
                               and asset.source_ref in item.get("resources", [])
                               for item in current["items"]) for asset in assets)):
        raise KnowledgeAccessDenied("selected knowledge changed before native publication")
    return await synchronizer.sync(selection, binding_operation_id, assets,
                                   operation_id=operation_id,
                                   selected_refs=[asset.source_ref for asset in assets])

"""Durably reconcile source-owned native assets and revoke outdated publications."""

from dataclasses import asdict
from hashlib import sha256
import json

from sqlalchemy import select

from l5_services.devops.knowledge import KnowledgeAccessDenied
from l5_services.models import DevopsKnowledgePublication, DevopsSpaceIntent


def source_key(reference):
    identity = {key: value for key, value in reference.items() if key != "record_revision"}
    return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class KnowledgeSynchronizer:
    def __init__(self, sessions, adapter):
        self.sessions = sessions
        self.adapter = adapter

    async def _binding(self, selection, binding_operation_id):
        async with self.sessions() as session:
            binding = await session.get(DevopsSpaceIntent, (selection.tenant_id, binding_operation_id))
            if (binding is None or binding.selection != asdict(selection)
                    or binding.state not in {"ready", "succeeded"} or not binding.native_space_id):
                raise KnowledgeAccessDenied("verified native binding required")
            return binding.native_space_id

    async def _record(self, key, intent, operation_id, *, state, receipt=None, error=None):
        async with self.sessions() as session:
            row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
            if row is None or row.intent != intent or row.operation_id != operation_id or row.state != "effect_started":
                raise KnowledgeAccessDenied("knowledge intent changed during publication")
            row.state = state
            row.error = error
            if receipt is not None:
                if row.asset_id is not None and row.asset_id != receipt["asset_id"]:
                    raise KnowledgeAccessDenied("native asset identity changed during publication")
                row.asset_id = receipt["asset_id"]
                row.asset_version = receipt["version"]
            await session.commit()

    async def sync(self, selection, binding_operation_id: str, assets: list, *,
                   operation_id: str, selected_refs: list[dict]) -> list[dict]:
        """Selected refs must be the current Product-authorized policy after a fresh scope check."""
        if not operation_id or not isinstance(selected_refs, list):
            raise KnowledgeAccessDenied("current selection and durable operation required")
        space_id = await self._binding(selection, binding_operation_id)
        selected = {source_key(ref) for ref in selected_refs
                    if ref.get("tenant_id") == selection.tenant_id
                    and ref.get("project_id") == selection.project_id}
        if len(selected) != len(selected_refs) or any(source_key(asset.source_ref) not in selected
                                                     or asset.source_ref not in selected_refs for asset in assets):
            raise KnowledgeAccessDenied("selection outside verified Product scope")
        receipts = []
        for asset in assets:
            key = (selection.tenant_id, space_id, source_key(asset.source_ref))
            intent = {"source_ref": asset.source_ref, "source_hash": asset.source_hash,
                      "mapping_version": asset.mapping_version, "kind": asset.kind,
                      "scope": asset.scope, "active": asset.active, "visibility": asset.visibility,
                      "native_metadata": asset.native_metadata}
            async with self.sessions() as session:
                row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
                reconcile_only = row is not None and row.state == "effect_started" and row.asset_id is None
                expected_asset_id = row.asset_id if row is not None else None
                if row is None:
                    row = DevopsKnowledgePublication(tenant_id=key[0], space_id=key[1],
                                                     source_key=key[2], product_id=selection.product_id,
                                                     project_id=selection.project_id, operation_id=operation_id,
                                                     intent=intent, state="effect_started")
                    session.add(row)
                else:
                    if (row.product_id, row.project_id) != (selection.product_id, selection.project_id):
                        raise KnowledgeAccessDenied("knowledge ownership mismatch")
                    if row.state != "ready" and (row.intent != intent or row.operation_id != operation_id):
                        raise KnowledgeAccessDenied("unresolved earlier knowledge intent")
                    row.intent = intent
                    row.operation_id = operation_id
                    row.state = "effect_started"
                    row.error = None
                await session.commit()
            try:
                receipt = self.adapter.publish(space_id, [asset], operation_id=operation_id,
                                               expected_asset_id=expected_asset_id,
                                               reconcile_only=reconcile_only)[0]
            except Exception:
                async with self.sessions() as session:
                    row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
                    if row is not None and row.intent == intent and row.operation_id == operation_id:
                        row.state = "effect_started"
                        row.error = "native_outcome_unknown"
                        await session.commit()
                raise
            await self._record(key, intent, operation_id, state="ready", receipt=receipt)
            receipts.append(receipt)
        async with self.sessions() as session:
            rows = (await session.execute(select(DevopsKnowledgePublication).where(
                DevopsKnowledgePublication.tenant_id == selection.tenant_id,
                DevopsKnowledgePublication.space_id == space_id,
                DevopsKnowledgePublication.product_id == selection.product_id,
                DevopsKnowledgePublication.project_id == selection.project_id))).scalars().all()
        for old in rows:
            if old.source_key in selected or old.state == "revoked":
                continue
            key = (old.tenant_id, old.space_id, old.source_key)
            async with self.sessions() as session:
                row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
                row.state = "cleanup_pending"
                row.operation_id = operation_id
                row.error = None
                await session.commit()
            try:
                self.adapter.revoke(space_id, old.source_key, old.asset_id, old.intent)
            except Exception:
                async with self.sessions() as session:
                    row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
                    row.state = "cleanup_failed"
                    row.error = "native_cleanup_unverified"
                    await session.commit()
                continue
            async with self.sessions() as session:
                row = await session.get(DevopsKnowledgePublication, key, with_for_update=True)
                row.state = "revoked"
                row.error = None
                await session.commit()
        return receipts

    async def accessible(self, selection, binding_operation_id: str, product, policy: dict) -> list[dict]:
        """Never expose pending/revoked receipts or stale Product-source selections."""
        space_id = await self._binding(selection, binding_operation_id)
        sources = await product.list_sources(selection.product_id, selection.project_id)
        if (sources.get("coverage") != "complete" or sources.get("next_cursor") is not None
                or not isinstance(sources.get("items"), list)):
            raise KnowledgeAccessDenied("Product source verification unavailable")
        available = [reference for item in sources["items"] for reference in item.get("resources", [])
                     if item.get("connection_ref", {}).get("id") == reference.get("source_connection_id")]
        allowed = policy.get("source_refs") or []
        async with self.sessions() as session:
            rows = (await session.execute(select(DevopsKnowledgePublication).where(
                DevopsKnowledgePublication.tenant_id == selection.tenant_id,
                DevopsKnowledgePublication.space_id == space_id,
                DevopsKnowledgePublication.product_id == selection.product_id,
                DevopsKnowledgePublication.project_id == selection.project_id,
                DevopsKnowledgePublication.state == "ready"))).scalars().all()
        return [{"asset_id": row.asset_id, "version": row.asset_version,
                 "source_ref": row.intent["source_ref"], "source_hash": row.intent["source_hash"]}
                for row in rows if row.intent["source_ref"] in allowed
                and row.intent["source_ref"] in available]

    async def observations(self, selection, binding_operation_id: str) -> list[dict]:
        space_id = await self._binding(selection, binding_operation_id)
        async with self.sessions() as session:
            rows = (await session.execute(select(DevopsKnowledgePublication).where(
                DevopsKnowledgePublication.tenant_id == selection.tenant_id,
                DevopsKnowledgePublication.space_id == space_id,
                DevopsKnowledgePublication.product_id == selection.product_id,
                DevopsKnowledgePublication.project_id == selection.project_id))).scalars().all()
        return [{"source_ref": row.intent["source_ref"], "state": row.state,
                 "asset_id": row.asset_id, "asset_version": row.asset_version,
                 "error": row.error} for row in rows]

"""Capture an admitted source through current Product ownership and durable preparation."""

import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException
from l5_services.models.event import EventReceipt
from l5_services.models.preparation import AdmissionPreparation
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.auth.l5_schedule_scope import schedule_product_scope
from src.l5_service_identity import service_for_worker
from src.products.models import ProductProjectBinding, ProductSourceBinding, ProductSourceSnapshot
from src.products.routes import scoped_product
from src.products.source_capture import capture_source, get_snapshot_objects
from src.products.source_routes import connection_access, service_visible_bindings, source_ref_authorized
from src.products.source_snapshots import ArtifactRef, SourceSnapshotRequest, store_snapshot, stored_receipt
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class GatewayAdmissionSourceClient:
    def __init__(self, db, actor, grant_id, verified, operation, admission_key, *, service_id, run_input=None, capture_fence=None):
        self.db = db
        self.actor = actor
        self.grant_id = grant_id
        self.verified = verified
        self.operation = operation
        self.admission_key = admission_key
        self.service_id = service_id
        self.run_input = run_input
        self.capture_fence = capture_fence
        self.retry_context = None

    @property
    def tenant_id(self):
        return self.verified["scope"]["tenant_id"] if self.actor.account_type == "service" else self.actor.org_id

    @property
    def owner_id(self):
        return self.verified["actor_id"] if self.actor.account_type == "service" else self.actor.user_id

    async def scoped_product(self, product_id, project_id, grant):
        if self.actor.account_type == "service":
            receipt = await self.db.get(
                EventReceipt,
                (
                    self.tenant_id,
                    product_id,
                    project_id,
                    self.service_id,
                    self.verified["trigger"]["trigger_id"],
                    self.verified.get("event_id"),
                ),
                populate_existing=True,
            )
            if (
                self.service_id not in {"well_architected", "transform", "security"}
                or self.verified.get("actor_type") != "service"
                or receipt is None
                or receipt.state not in {"pending", "admitted"}
                or receipt.event_kind != "schedule"
                or receipt.idempotency_key != self.admission_key
                or receipt.configuration_id != self.verified.get("configuration_id")
                or receipt.configuration_revision != self.verified.get("configuration_revision")
                or (receipt.run_input or {}).get("sources") != self.verified.get("source_refs")
                or grant.created_by != self.owner_id
                or await schedule_product_scope(self.db, self.actor, grant.id, product_id, project_id, "source:pin") is None
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            relationship = await self.db.scalar(
                select(ProductProjectBinding).where(
                    ProductProjectBinding.tenant_id == grant.tenant_id,
                    ProductProjectBinding.product_id == product_id,
                    ProductProjectBinding.project_id == project_id,
                    ProductProjectBinding.valid_to.is_(None),
                )
            )
            if relationship is None or str(relationship.relationship_revision) != self.verified["scope"]["relationship_revision"]:
                raise HTTPException(404, detail={"code": "not_found"})
        else:
            await scoped_product(self.db, self.actor, product_id, project_id)

    async def connection_access(self, binding, grant):
        if self.actor.account_type == "service":
            return any(item.id == binding.id for item in await service_visible_bindings(self.db, grant))
        return await connection_access(self.db, self.actor, binding) is not None

    async def pin_source(self, product_id, project_id, request, *, idempotency_key):
        body = SourceSnapshotRequest.model_validate(request)
        now = datetime.now(UTC)
        denied = HTTPException(404, detail={"code": "not_found"})
        if (
            body.delegation_id != self.grant_id
            or not self.admission_key
            or product_id != self.verified["scope"]["product_id"]
            or project_id != self.verified["scope"]["project_id"]
        ):
            raise denied
        grant = await self.db.get(CredentialServiceDelegation, self.grant_id, populate_existing=True)
        preparation = await self.db.get(
            AdmissionPreparation,
            (
                self.tenant_id,
                product_id,
                project_id,
                self.service_id,
                self.owner_id,
                self.operation,
                self.admission_key,
            ),
            populate_existing=True,
        )
        source = body.source_ref.model_dump()
        if (
            grant is None
            or preparation is None
            or service_for_worker(grant.service_id) != self.service_id
            or grant.tenant_id != self.tenant_id
            or grant.product_id != product_id
            or grant.project_id != project_id
            or grant.created_by != self.owner_id
            or grant.revision != body.delegation_revision
            or grant.revoked_at is not None
            or _aware(grant.expires_at) <= now
            or "source:pin" not in grant.operations
            or source not in grant.sources
            or preparation.verified != self.verified
            or source not in self.verified["trigger"]["source_refs"]
            or _aware(body.retain_until)
            != (
                _aware(datetime.fromisoformat(self.verified["source_retention"]["required_until"]))
                if self.verified.get("service_id") == "well_architected"
                else _aware(preparation.deadline_at)
            )
            or _aware(preparation.deadline_at) <= now
            or _aware(preparation.deadline_at) > _aware(grant.expires_at)
            or body.exclusions != self.verified["exclusions"]
            or body.max_bytes != self.verified["max_bytes"]
            or body.max_files != self.verified["max_files"]
        ):
            raise denied
        if not await current_l5_parent(self.db, grant):
            raise denied
        await self.scoped_product(product_id, project_id, grant)
        bindings = (
            await self.db.scalars(
                select(ProductSourceBinding)
                .where(
                    ProductSourceBinding.tenant_id == grant.tenant_id,
                    ProductSourceBinding.project_id == project_id,
                    ProductSourceBinding.unlinked_at.is_(None),
                    ProductSourceBinding.status != "revoked",
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        matching = [binding for binding in bindings if source_ref_authorized(grant, binding, source) and await self.connection_access(binding, grant)]
        if len(matching) != 1:
            raise denied
        binding = matching[0]
        previous = await self.db.scalar(
            select(ProductSourceSnapshot)
            .where(
                ProductSourceSnapshot.tenant_id == grant.tenant_id,
                ProductSourceSnapshot.delegation_id == grant.id,
                ProductSourceSnapshot.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        request_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if previous is not None:
            if previous.request_hash != request_hash:
                raise HTTPException(409, detail={"code": "idempotency_conflict"})
            if _aware(previous.expires_at) <= now:
                raise HTTPException(410, detail={"code": "snapshot_expired"})
            return stored_receipt(previous).model_dump(mode="json")
        if self.service_id == "security" and body.source_ref.record_type == "repository_commit" and self.capture_fence is None:
            from src.products.source_capture_jobs import prepare_capture

            return await prepare_capture(self, body, binding, preparation, idempotency_key)
        if self.capture_fence is not None:
            await self.capture_fence(self.db, seal=False)
        mapping = await self.db.get(ChannelTenantMap, binding.installation_id) if binding.installation_id else None
        selected_state = (binding.revision, binding.generation, binding.status, binding.desired, binding.observed)
        captured = await capture_source(binding, mapping, body, get_snapshot_objects())
        await self.db.refresh(grant)
        await self.db.refresh(preparation)
        await self.db.refresh(binding)
        await self.scoped_product(product_id, project_id, grant)
        if (
            grant.revoked_at is not None
            or _aware(grant.expires_at) <= datetime.now(UTC)
            or grant.revision != body.delegation_revision
            or source not in grant.sources
            or preparation.verified != self.verified
            or _aware(preparation.deadline_at) <= datetime.now(UTC)
            or (binding.revision, binding.generation, binding.status, binding.desired, binding.observed) != selected_state
            or not source_ref_authorized(grant, binding, source)
            or not await self.connection_access(binding, grant)
        ):
            raise denied
        if not await current_l5_parent(self.db, grant):
            raise denied
        if self.capture_fence is not None:
            await self.capture_fence(self.db, seal=True)
        receipt, _ = await store_snapshot(self.db, grant, body, captured, idempotency_key)
        return receipt.model_dump(mode="json")

    async def architecture_coverage(self, snapshots):
        coverage = {}
        for snapshot in snapshots:
            stored = await self.db.get(ProductSourceSnapshot, (self.tenant_id, snapshot["snapshot_id"]))
            if (
                stored is None
                or stored.delegation_id != self.grant_id
                or stored.receipt != snapshot
                or stored.product_id != self.verified["scope"]["product_id"]
                or stored.project_id != self.verified["scope"]["project_id"]
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            raw = await run_in_threadpool(get_snapshot_objects().get, ArtifactRef.model_validate(snapshot["file_manifest"]))
            manifest = json.loads(raw)
            entries = manifest["files"]
            if len(entries) != snapshot["file_count"] or len({entry["path"] for entry in entries}) != len(entries):
                raise HTTPException(409, detail={"code": "source_integrity_mismatch"})
            coverage[snapshot["snapshot_id"]] = {
                "files": {
                    entry["path"]: {"component": snapshot["source_ref"]["record_id"], "entry_point": False, "exclusion": None} for entry in entries
                },
                "unresolved": ["architecture_entry_points_unclassified"],
            }
        return coverage

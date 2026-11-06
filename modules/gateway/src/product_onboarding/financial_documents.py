"""Read structured financial suggestions from an authorized, connected document.

The provider remains read-only. Document values are proposals, never funding
decisions; the existing selective-confirmation route owns all profile writes.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, timedelta

import httpx
from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.products.configuration import CalendarDate, ConfigurationObject, Money, Name
from src.products.github_documents import document_location as document_location
from src.products.github_documents import read_document_bytes
from src.shared.models.base import new_uuid, utcnow


class SuggestedAllocation(ConfigurationObject):
    name: Name
    amount: Money
    category: str = Field(pattern=r"^(ai|cloud|other)$")
    period_start: CalendarDate
    period_end: CalendarDate

    @model_validator(mode="after")
    def valid_period(self):
        if self.period_start > self.period_end:
            raise ValueError("period_start must not be after period_end")
        return self


class FinancialDocument(ConfigurationObject):
    release_target_date: CalendarDate = None
    allocation: SuggestedAllocation = None

    @model_validator(mode="after")
    def has_suggestion(self):
        if self.release_target_date is None and self.allocation is None:
            raise ValueError("The document has no financial suggestions")
        return self


async def read_document(binding, connection, *, client=None):
    """Parse financial proposals after the common authorized document read."""
    content, digest = await read_document_bytes(binding, connection, client=client)
    return FinancialDocument.model_validate_json(content), digest


def proposal(document, *, binding, product, project, actor_id, target_revision, source_revision, now=None):
    now = now or utcnow()
    fields = {}
    if document.release_target_date is not None:
        fields["release_target_date"] = document.release_target_date
    if document.allocation is not None:
        fields["allocations"] = [
            {
                **document.allocation.model_dump(mode="json"),
                "id": new_uuid(),
                "approval_status": "proposed",
                "owner_ref": {"kind": "user", "id": actor_id},
                "target_ref": {
                    "tenant_id": product.tenant_id,
                    "project_id": project.id,
                    "source_connection_id": "adp.profile",
                    "record_type": "product",
                    "record_id": product.id,
                    "record_revision": target_revision,
                },
            }
        ]
    reference = {
        "tenant_id": product.tenant_id,
        "project_id": project.id,
        "source_connection_id": binding.installation_id,
        "record_type": "document",
        "record_id": binding.native_id,
        "record_revision": source_revision,
    }
    authority = {
        **reference,
        "source_connection_id": "adp.profile",
        "record_type": "source_binding",
        "record_id": binding.id,
        "record_revision": str(binding.revision),
    }
    paths = [f"/configuration/financials/{field}" for field in fields]
    current = (project.configuration or {}).get("financials") or {}
    return {
        "id": new_uuid(),
        "base_revision": product.revision,
        "product": {},
        "base_values": {path: deepcopy(current.get(field)) for field, path in zip(fields, paths)},
        "configuration": {"financials": fields},
        "remaining_paths": paths,
        "provenance": [
            {
                "paths": [path],
                "state": "missing",
                "method": "connector",
                "source_refs": [reference],
                "authority_mapping_ref": authority,
                "observed_at": now.isoformat().replace("+00:00", "Z"),
            }
            for path in paths
        ],
        "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    }


async def import_suggestions(db, actor, member, product, project, session, request, revision, key):
    from src.product_onboarding.routes import fingerprint, scoped_session, session_view
    from src.products.models import Product, ProductChange, ProductOperation, ProductSourceBinding
    from src.products.source_routes import connection_access

    ids = (product.id, project.id, session.id)
    key_hash = fingerprint({"key": key})
    request_hash = fingerprint({"scope": ids, "revision": revision, "request": request.model_dump()})
    existing = await db.scalar(
        select(ProductOperation).where(
            ProductOperation.tenant_id == actor.org_id,
            ProductOperation.actor_id == member.id,
            ProductOperation.operation == "guideOnboarding",
            ProductOperation.key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash or existing.session_id != session.id:
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        if existing.expires_at.replace(tzinfo=UTC) <= utcnow():
            raise HTTPException(410, detail={"code": "operation_expired"})
        saved = await session_view(db, session, product, project, actor)
        selected = set((existing.receipt or {}).get("proposal_ids", []))
        return {
            "revision": product.revision,
            "mode": "financial",
            "message": "Review the suggested fields before accepting them.",
            "proposals": [item for item in saved["proposals"] if item["id"] in selected],
        }
    if product.revision != revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    if session.expires_at is not None and session.expires_at.replace(tzinfo=UTC) <= utcnow():
        raise HTTPException(410, detail={"code": "session_expired"})
    rows = (
        await db.scalars(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == actor.org_id,
                ProductSourceBinding.project_id == project.id,
                ProductSourceBinding.kind == "document",
                ProductSourceBinding.unlinked_at.is_(None),
            )
            .order_by(ProductSourceBinding.id)
            .limit(11)
        )
    ).all()
    if len(rows) > 10:
        raise HTTPException(422, detail={"code": "too_many_document_sources"})
    observations = []
    deadline = asyncio.get_running_loop().time() + 20
    for binding in rows:
        connection = await connection_access(db, actor, binding)
        if connection is None or binding.status == "revoked":
            continue
        identity = (
            binding.id,
            binding.generation,
            binding.revision,
            dict(binding.desired or {}),
            connection.installation_id if binding.installation_id else None,
        )
        try:
            async with asyncio.timeout_at(deadline):
                document, digest = await read_document(binding, connection)
        except httpx.HTTPStatusError as error:
            status = 404 if error.response.status_code in (401, 403, 404) else 503
            raise HTTPException(status, detail={"code": "source_unavailable"}) from None
        except (TimeoutError, httpx.HTTPError):
            raise HTTPException(503, detail={"code": "source_unavailable"}) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(422, detail={"code": "invalid_financial_document"}) from None
        observations.append((identity, document, digest))
    if not observations:
        raise HTTPException(404, detail={"code": "financial_document_required"})

    # Provider reads may outlive a profile edit or access revocation. Reauthorize
    # and compare every binding before publishing any candidate or observation.
    db.expire_all()
    member, product, project, session = await scoped_session(db, actor, *ids)
    await db.scalar(select(Product).where(Product.tenant_id == actor.org_id, Product.id == product.id).with_for_update())
    await db.refresh(product)
    await db.refresh(session, with_for_update=True)
    if product.revision != revision:
        raise HTTPException(412, detail={"code": "stale_revision"})
    if product.archived_at is not None or session.state not in ("open", "waiting_external"):
        raise HTTPException(409, detail={"code": "session_closed"})
    if len(session.proposals or []) + len(observations) > 100:
        raise HTTPException(409, detail={"code": "proposal_capacity"})
    target = await db.scalar(
        select(ProductChange).where(
            ProductChange.tenant_id == actor.org_id,
            ProductChange.product_id == product.id,
            ProductChange.revision == product.revision,
        )
    )
    if target is None:
        raise HTTPException(409, detail={"code": "profile_history_unavailable"})
    candidates = []
    now = utcnow()
    for identity, document, digest in observations:
        binding = await db.get(ProductSourceBinding, (actor.org_id, identity[0]), populate_existing=True, with_for_update=True)
        if (
            binding is None
            or binding.unlinked_at is not None
            or binding.status == "revoked"
            or (binding.generation, binding.revision, binding.desired) != identity[1:4]
        ):
            raise HTTPException(409, detail={"code": "source_changed"})
        connection = await connection_access(db, actor, binding, lock=True)
        if connection is None or connection.installation_id != identity[4]:
            raise HTTPException(404, detail={"code": "source_unavailable"})
        candidates.append(
            proposal(
                document,
                binding=binding,
                product=product,
                project=project,
                actor_id=member.id,
                target_revision=target.id,
                source_revision=digest,
                now=now,
            )
        )
        binding.status = "healthy"
        binding.observed = {
            "observed_at": now.isoformat(),
            "stale_after": (now + timedelta(minutes=15)).isoformat(),
            "source_revision": digest,
            "native_id": binding.native_id,
            "namespace": binding.namespace,
        }
    session.proposals = [*(session.proposals or []), *candidates]
    db.add(
        ProductOperation(
            tenant_id=actor.org_id,
            product_id=product.id,
            project_id=project.id,
            session_id=session.id,
            actor_id=member.id,
            operation="guideOnboarding",
            key_hash=key_hash,
            request_hash=request_hash,
            request={"mode": "financial", "revision": revision},
            state="succeeded",
            receipt={"proposal_ids": [item["id"] for item in candidates]},
            expires_at=now + timedelta(days=1),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from None
    saved = await session_view(db, session, product, project, actor)
    selected = {item["id"] for item in candidates}
    return {
        "revision": product.revision,
        "mode": "financial",
        "message": "Review the suggested fields before accepting them.",
        "proposals": [item for item in saved["proposals"] if item["id"] in selected],
    }

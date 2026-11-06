"""Read collection work authority from the existing Product operation journal.

S03's trusted job admission owns creation/renewal of collectConnectorSource
operations. A delegation or worker assertion alone is never a live work lease.
"""

from datetime import UTC

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.l5_credential_routes import CredentialSessionRequest, L5WorkPermit, WorkAuthorizer
from src.products.models import ProductOperation, ProductSourceBinding
from src.shared.database import get_db
from src.shared.models.base import utcnow


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class CollectionWorkRequest(CredentialSessionRequest):
    cell_id: str
    grant_id: str
    binding_id: str


def get_connector_work_authorizer(db: AsyncSession = Depends(get_db)) -> WorkAuthorizer:
    async def current_work(delegation, requested):
        operation = await db.scalar(
            select(ProductOperation)
            .where(
                ProductOperation.tenant_id == delegation.tenant_id,
                ProductOperation.id == requested.operation_id,
                ProductOperation.operation == "collectConnectorSource",
            )
            .execution_options(populate_existing=True)
        )
        if (
            operation is None
            or operation.state != "running"
            or operation.product_id != delegation.product_id
            or operation.project_id != delegation.project_id
            or operation.binding_id != requested.binding_id
            or operation.binding_generation != delegation.binding_generation
            or operation.lease_owner != requested.attempt_id
            or operation.attempt_count != delegation.lease_fence
            or operation.attempt_count < 1
            or operation.lease_until is None
            or min(_aware(operation.expires_at), _aware(operation.lease_until)) <= utcnow()
            or not isinstance(operation.request, dict)
            or operation.request.get("consumer") != delegation.service_id
            or operation.request.get("cell_id") != requested.cell_id
            or operation.request.get("grant_id") != requested.grant_id
            or operation.request.get("delegation_id") != delegation.id
            or operation.request.get("delegation_revision") != requested.delegation_revision
            or operation.request.get("source_ref") != requested.connection_ref.model_dump()
        ):
            raise HTTPException(409, detail={"code": "stale_collection_work"})
        binding = await db.get(ProductSourceBinding, (delegation.tenant_id, operation.binding_id), populate_existing=True)
        if binding is None or binding.generation != operation.binding_generation:
            raise HTTPException(409, detail={"code": "stale_collection_work"})
        return L5WorkPermit(
            tenant_id=operation.tenant_id,
            product_id=operation.product_id,
            project_id=operation.project_id,
            source_connection_id=delegation.installation_id or delegation.credential_id,
            operation_id=operation.id,
            attempt_id=operation.lease_owner,
            lease_fence=operation.attempt_count,
            binding_generation=operation.binding_generation,
            expires_at=min(_aware(operation.expires_at), _aware(operation.lease_until)),
        )

    return current_work

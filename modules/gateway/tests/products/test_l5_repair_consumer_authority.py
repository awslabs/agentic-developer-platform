"""A repair registry identity may deliver retained results but cannot use credentials."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from l5_services.models.publication import Publication
from sqlalchemy import update

from src.l5_consumer_authority import current
from src.l5_service_identity import contribution_worker_id, service_for_worker
from src.shared.identity.product_scope import service_product_scope
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_l5_contribution_authority import contribution as _contribution

contribution = _contribution


async def test_repair_uses_only_original_owned_contribution_authority(contribution):
    selected = contribution
    actor = selected.actor.model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "agent_registry_id": selected.grant.service_id.removesuffix("-worker") + "-repair",
            "credential_scopes": ["l5:contribution-repair"],
        }
    )
    assert contribution_worker_id(actor) == selected.grant.service_id
    assert service_for_worker(actor.agent_registry_id) is None
    async with selected.sessions() as db:
        db.add(
            UserCredential(
                id="fixture-connection",
                user_id=selected.actor.user_id,
                org_id=selected.actor.org_id,
                service="aws",
                credential_type="api_key",
                label="fixture",
                secret_arn="fixture-secret",
            )
        )
        await db.execute(update(CredentialServiceDelegation).values(operations=["credential:session", "product:read", "source:read"]))
        await db.execute(update(Publication).values(state="pending", receipt=None))
        await db.commit()
    arguments = dict(
        tenant_id=selected.run.tenant_id,
        contribution_id=selected.publication.publication_id,
        destination="product-context",
        action="admit",
        delegation_id=selected.grant.id,
    )
    async with selected.sessions() as db:
        # General credential and Product routes still see the distinct repair ID.
        for operation in ("credential:session", "product:read", "source:read"):
            assert await service_product_scope(db, actor, selected.grant.id, selected.run.product_id, selected.run.project_id, operation) is None
        run, publication = await current(db, actor, **arguments)
        assert run.run_id == selected.run.run_id and publication.state == "pending"
        for changed in ({"credential_scopes": []}, {"agent_registry_id": "l5-security-repair"}, {"auth_source": "jwt"}, {"scope": "tenant"}):
            with pytest.raises(HTTPException):
                await current(db, actor.model_copy(update=changed), **arguments)
        await db.execute(update(CredentialServiceDelegation).values(revoked_at=datetime.now(UTC)))
        await db.execute(
            update(Publication).values(
                state="invalidation_pending", invalidation_watermark=2, invalidation={"reason": "grant_revoked", "source_refs": []}
            )
        )
        await db.commit()
        _, publication = await current(db, actor, **{**arguments, "action": "invalidate"})
        assert publication.invalidation_watermark == 2
        for action in ("admit", "read", "use"):
            with pytest.raises(HTTPException):
                await current(db, actor, **{**arguments, "action": action})

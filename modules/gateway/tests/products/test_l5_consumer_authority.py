"""The receiver callback rechecks real Product rows, never a cached authority flag."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from l5_services.models.binding import Binding
from l5_services.models.publication import Publication
from sqlalchemy import delete, update

from src.l5_consumer_authority import authorize_assertion, claims_for, current
from src.products.models import ProductProjectBinding, ProductSourceBinding
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.test_l5_contribution_authority import contribution as _contribution

contribution = _contribution


@pytest.mark.parametrize("revoke", ["membership", "relationship", "binding", "grant", "parent", "publication", "source"])
async def test_current_callback_denies_independent_revocation(contribution, revoke):
    selected = contribution
    async with selected.sessions() as db:
        await db.execute(update(CredentialServiceDelegation).values(operations=["credential:session", "product:read", "source:read"]))
        await db.commit()
    claims = claims_for(selected.actor, selected.run, selected.publication, action="read")
    assert await authorize_assertion(claims, selected.sessions)
    if revoke == "source" and selected.service != "transform":
        return
    async with selected.sessions() as db:
        if revoke == "membership":
            await db.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.actor.user_id))
        elif revoke == "relationship":
            await db.execute(update(ProductProjectBinding).values(relationship_revision=2))
        elif revoke == "binding":
            await db.execute(update(Binding).values(generation=2))
        elif revoke in {"grant", "parent"}:
            await db.execute(
                update(CredentialServiceDelegation)
                .where(CredentialServiceDelegation.id == (selected.grant.id if revoke == "grant" else selected.parent.id))
                .values(revoked_at=datetime.now(UTC))
            )
        elif revoke == "publication":
            await db.execute(update(Publication).values(state="invalidation_pending", invalidation_watermark=1))
        else:
            await db.execute(update(ProductSourceBinding).values(status="revoked"))
        await db.commit()
    assert await authorize_assertion(claims, selected.sessions) is False


async def test_registered_producer_can_admit_only_its_current_durable_publication(contribution):
    selected = contribution
    actor = selected.actor.model_copy(
        update={"account_type": "service", "auth_source": "iam", "scope": "internal", "agent_registry_id": selected.grant.service_id}
    )
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
        run, publication = await current(db, actor, **arguments)
        assert run.run_id == selected.run.run_id and publication.state == "pending"
        for foreign in (actor.model_copy(update={"agent_registry_id": "another-service"}), selected.actor):
            with pytest.raises(HTTPException) as refusal:
                await current(db, foreign, **arguments)
            assert refusal.value.status_code == 404
        with pytest.raises(HTTPException):
            await current(db, actor, **{**arguments, "contribution_id": "foreign-publication"})
        with pytest.raises(HTTPException):
            await current(db, actor, **{**arguments, "destination": "engineering-insights"})


async def test_revoked_producer_can_only_apply_exact_durable_tombstone(contribution):
    selected = contribution
    actor = selected.actor.model_copy(
        update={"account_type": "service", "auth_source": "iam", "scope": "internal", "agent_registry_id": selected.grant.service_id}
    )
    async with selected.sessions() as db:
        await db.execute(update(CredentialServiceDelegation).values(revoked_at=datetime.now(UTC)))
        await db.execute(
            update(Publication).values(
                state="invalidation_pending", invalidation_watermark=1, invalidation={"reason": "source_revoked", "source_refs": []}
            )
        )
        await db.commit()
    arguments = dict(
        tenant_id=selected.run.tenant_id,
        contribution_id=selected.publication.publication_id,
        destination="product-context",
        action="invalidate",
        delegation_id=selected.grant.id,
    )
    async with selected.sessions() as db:
        _, publication = await current(db, actor, **arguments)
        assert publication.invalidation_watermark == 1
        for changes in ({"action": "read"}, {"action": "admit"}, {"delegation_id": "another-grant"}):
            with pytest.raises(HTTPException):
                await current(db, actor, **{**arguments, **changes})
        with pytest.raises(HTTPException):
            await current(db, actor.model_copy(update={"agent_registry_id": "foreign"}), **arguments)


@pytest.mark.parametrize("archive", ["product", "project"])
async def test_archived_product_or_project_revokes_consumer_authority_but_allows_invalidation(contribution, archive):
    from src.products.models import Product, Project

    selected = contribution
    repair = selected.actor.model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "agent_registry_id": selected.grant.service_id.removesuffix("-worker") + "-repair",
            "credential_scopes": ["l5:contribution-repair"],
        }
    )
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
        await db.commit()
    arguments = dict(
        tenant_id=selected.run.tenant_id, contribution_id=selected.publication.publication_id, destination="product-context", action="read"
    )
    human_claims = claims_for(selected.actor, selected.run, selected.publication, action="read")
    repair_claims = claims_for(repair, selected.run, selected.publication, action="read", delegation_id=selected.grant.id)
    assert await authorize_assertion(human_claims, selected.sessions)
    assert await authorize_assertion(repair_claims, selected.sessions)
    # Keep a loaded parent cached while its lifecycle changes in another session.
    model, identity = (Product, selected.run.product_id) if archive == "product" else (Project, selected.run.project_id)
    async with selected.sessions() as stale_db:
        cached = await stale_db.get(model, (selected.run.tenant_id, identity))
        assert cached.archived_at is None
        async with selected.sessions() as db:
            await db.execute(
                update(model).where(model.tenant_id == selected.run.tenant_id, model.id == identity).values(archived_at=datetime.now(UTC))
            )
            await db.commit()
        assert await authorize_assertion(human_claims, selected.sessions) is False
        assert await authorize_assertion(repair_claims, selected.sessions) is False
        for actor, actions, delegation in ((selected.actor, ("read", "use"), None), (repair, ("read", "admit"), selected.grant.id)):
            for action in actions:
                with pytest.raises(HTTPException) as denied:
                    await current(stale_db, actor, **{**arguments, "action": action}, delegation_id=delegation)
                assert denied.value.status_code == 404
    async with selected.sessions() as db:
        await db.execute(
            update(Publication).values(
                state="invalidation_pending", invalidation_watermark=1, invalidation={"reason": "product_archived", "source_refs": []}
            )
        )
        await db.commit()
        _, publication = await current(db, repair, **{**arguments, "action": "invalidate"}, delegation_id=selected.grant.id)
        cleanup = claims_for(repair, selected.run, publication, action="invalidate", delegation_id=selected.grant.id)
    assert await authorize_assertion(cleanup, selected.sessions)

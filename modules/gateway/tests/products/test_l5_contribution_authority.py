"""A cached result selection cannot authorize a later private contribution read."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src import l5_integration
from src.l5_service_identity import worker_id
from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.models.organization import Organization, TeamMembership, User
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from tests.products.membership import add_team_member


@pytest.fixture(params=["finops", "transform"])
async def contribution(request, test_engine, db_session, valid_token_context, monkeypatch):
    service = request.param
    l5_owner = valid_token_context
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    product, project = str(uuid4()), str(uuid4())
    db_session.add(Organization(id=l5_owner.org_id, name="Fixture workspace"))
    db_session.add(
        User(id=l5_owner.user_id, org_id=l5_owner.org_id, team_id=l5_owner.team_id, email="consumer-reader@example.test", user_kind="human")
    )
    await add_team_member(db_session, l5_owner)
    db_session.add_all(
        [
            Product(
                tenant_id=l5_owner.org_id,
                id=product,
                name="Fixture product",
                purpose="Read retained results",
                intended_users=["engineers"],
                owner_user_id=l5_owner.user_id,
                team_id=l5_owner.team_id,
                starting_point="idea",
                created_by=l5_owner.user_id,
            ),
            Project(tenant_id=l5_owner.org_id, id=project, name="Fixture project", owner_user_id=l5_owner.user_id),
            ProductProjectBinding(
                tenant_id=l5_owner.org_id, product_id=product, project_id=project, is_base=True, relationship_revision=1, provenance={}
            ),
        ]
    )
    scope = {
        "tenant_id": l5_owner.org_id,
        "product_id": product,
        "project_id": project,
        "owner_id": l5_owner.user_id,
        "relationship_revision": "1",
        "visibility": "only_me",
    }
    connection_ref = {
        "tenant_id": l5_owner.org_id,
        "project_id": project,
        "source_connection_id": "fixture-connection",
        "record_type": "connection",
        "record_id": "fixture-connection",
        "record_revision": "1",
    }
    capability = "cost_inquiry" if service == "finops" else "refactor"
    expires = datetime.now(UTC) + timedelta(days=1)
    sources = []
    if service == "transform":
        sources = [
            {
                "tenant_id": l5_owner.org_id,
                "project_id": project,
                "source_connection_id": "fixture-source",
                "record_type": "repository_commit",
                "record_id": "example/repository",
                "record_revision": "a" * 40,
            }
        ]
        db_session.add_all(
            [
                UserCredential(
                    id="fixture-source",
                    user_id=l5_owner.user_id,
                    org_id=l5_owner.org_id,
                    service="gitlab",
                    credential_type="api_key",
                    label="fixture",
                    secret_arn="fixture-secret",
                ),
                ProductSourceBinding(
                    tenant_id=l5_owner.org_id,
                    id="fixture-source-binding",
                    project_id=project,
                    kind="repository",
                    provider="gitlab",
                    credential_id="fixture-source",
                    namespace="example",
                    native_id="example/repository",
                    desired={},
                    status="healthy",
                ),
            ]
        )
    grant_values = dict(
        tenant_id=l5_owner.org_id,
        credential_id="fixture-connection",
        service_id=worker_id(service),
        product_id=product,
        project_id=project,
        created_by=l5_owner.user_id,
        operations=["credential:session"],
        sources=[{**connection_ref, "record_type": "credential"}, *sources],
        capabilities=[capability],
        visibility="restricted",
        revision=1,
        binding_generation=1,
        expires_at=expires,
    )
    parent = CredentialServiceDelegation(id="fixture-parent", **grant_values)
    grant = CredentialServiceDelegation(id="fixture-grant", parent_delegation_id=parent.id, **grant_values)
    run = Run(
        tenant_id=l5_owner.org_id,
        run_id="fixture-run",
        product_id=product,
        project_id=project,
        service_id=service,
        configuration_id="fixture-configuration",
        configuration_revision=1,
        binding_id="fixture-binding",
        trigger_id="manual",
        binding_generation=1,
        revision=1,
        state="succeeded",
        provider_state="completed",
        deadline_at=expires,
        scope=scope,
        input={"capability": capability, "sources": sources, "transform_input": {"goal": {"summary": "Review structure"}}},
        admission={"scope": scope, "connection_ref": connection_ref, "delegation_id": grant.id, "delegation_version": 1},
    )
    detail = {"result_id": "fixture-result", "revision": 1, "scope": scope, "run_id": run.run_id}
    if service == "finops":
        detail["kind"] = "finops_cost_inquiry"
        fields = {"cost": {key: value for key, value in detail.items() if key not in {"result_id", "revision", "scope"}}, "evidence_refs": []}
    else:
        artifact = {
            "tenant_id": l5_owner.org_id,
            "project_id": project,
            "artifact_id": "fixture-patch",
            "version": "1",
            "sha256": "a" * 64,
            "bytes": 10,
            "media_type": "text/x-diff",
            "expires_at": expires.isoformat(),
        }
        detail.update(
            service_id="transform", source_ref=sources, retained_artifacts={"original_patch": artifact}, retention_expires_at=expires.isoformat()
        )
        fields = {
            "result": detail,
            "source_refs": sources,
            "goal": run.input["transform_input"]["goal"],
            "evidence_refs": [{"kind": "original_patch", "artifact": artifact}],
        }
    payload = {
        "schema_version": "1.0",
        "kind": "finops_cost_result" if service == "finops" else "transform_result",
        "contribution_id": "fixture-publication",
        "scope": scope,
        "run_id": run.run_id,
        "result_ref": {
            "tenant_id": l5_owner.org_id,
            "project_id": project,
            "source_connection_id": "l5-" + service,
            "record_type": "finops_cost_result" if service == "finops" else "transform_result",
            "record_id": "fixture-result",
            "record_revision": "1",
        },
        **fields,
    }
    publication = Publication(
        tenant_id=l5_owner.org_id,
        publication_id="fixture-publication",
        result_id="fixture-result",
        result_version=1,
        destination="product-context",
        schema_version="1.0",
        payload=payload,
        contribution_hash=sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        state="published",
        revision=1,
        invalidation_watermark=0,
        applied_watermark=0,
        next_due_at=expires,
        receipt={
            "receipt_id": "fixture-receipt",
            "contribution_id": "fixture-publication",
            "admitted_schema": "1.0",
            "consumer_revision": "1",
            "record_refs": [payload["result_ref"]],
        },
    )
    db_session.add_all(
        [
            parent,
            grant,
            run,
            publication,
            Configuration(
                tenant_id=l5_owner.org_id,
                configuration_id="fixture-configuration",
                product_id=product,
                project_id=project,
                service_id=service,
                owner_id=l5_owner.user_id,
                relationship_revision="1",
                current_revision=1,
                handoff_state="linked",
            ),
            ConfigurationVersion(
                tenant_id=l5_owner.org_id,
                configuration_id="fixture-configuration",
                revision=1,
                settings={"source_refs": sources, "visibility": "only_me", "enabled": False},
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="a" * 64,
            ),
            Binding(
                tenant_id=l5_owner.org_id,
                binding_id="fixture-binding",
                configuration_id="fixture-configuration",
                product_id=product,
                project_id=project,
                service_id=service,
                scope=scope,
                revision=1,
                generation=1,
                state="ready",
                ownership="customer_linked",
                connection_ref=connection_ref,
                connection_key="a" * 64,
                readiness=[],
                gaps=[],
            ),
            AssessmentResult(tenant_id=l5_owner.org_id, result_id="fixture-result", run_id=run.run_id, version=1, payload=detail),
            ResultDetail(tenant_id=l5_owner.org_id, result_id="fixture-result", payload=detail),
        ]
    )
    await db_session.commit()

    # The tests never call a provider. Product membership, relationship, run,
    # binding, grant, parent and publication checks use their real stored rows.
    monkeypatch.setattr(
        l5_integration,
        "authorized_connections",
        AsyncMock(return_value=[({"provider": "aws", "connection_ref": {"id": "fixture-connection"}}, None)]),
    )
    backend = l5_integration.GatewayConfigurationBackend(
        db_session,
        l5_owner,
        transform_artifact_store=SimpleNamespace(read=AsyncMock(side_effect=AssertionError("metadata cannot read artifacts"))),
        publish=AsyncMock(),
    )
    store = await backend.result_store(product, project, service)
    read = store.cost_contribution if service == "finops" else store.transform_contribution
    assert (await read("fixture-result", "product-context"))["result_ref"] == payload["result_ref"]
    return SimpleNamespace(
        service=service,
        backend=backend,
        store=store,
        read=read,
        publication=publication,
        grant=grant,
        parent=parent,
        actor=l5_owner,
        run=run,
        sessions=async_sessionmaker(test_engine, expire_on_commit=False),
    )


@pytest.mark.parametrize(
    "revocation",
    [
        "membership",
        "relationship",
        "binding",
        "grant",
        "parent",
        "capability",
        "expiry",
        "parent_expiry",
        "parent_capability",
        "publication",
        "payload",
        "receipt",
        "revision",
        "deleted",
        "run",
        "scope",
    ],
)
async def test_contribution_rechecks_independent_committed_revocation(contribution, revocation):
    selected = contribution
    async with selected.sessions() as independent:
        if revocation == "membership":
            await independent.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.actor.user_id))
        elif revocation == "relationship":
            await independent.execute(update(ProductProjectBinding).values(relationship_revision=2))
        elif revocation == "binding":
            await independent.execute(update(Binding).values(generation=2))
        elif revocation in {"grant", "parent"}:
            await independent.execute(
                update(CredentialServiceDelegation)
                .where(CredentialServiceDelegation.id == (selected.grant.id if revocation == "grant" else selected.parent.id))
                .values(revoked_at=datetime.now(UTC))
            )
        elif revocation == "capability":
            await independent.execute(
                update(CredentialServiceDelegation).where(CredentialServiceDelegation.id == selected.grant.id).values(capabilities=[])
            )
        elif revocation in {"expiry", "parent_expiry", "parent_capability"}:
            changes = {"capabilities": []} if revocation == "parent_capability" else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            await independent.execute(
                update(CredentialServiceDelegation)
                .where(CredentialServiceDelegation.id == (selected.grant.id if revocation == "expiry" else selected.parent.id))
                .values(**changes)
            )
        elif revocation == "publication":
            await independent.execute(update(Publication).values(state="invalidation_pending", invalidation_watermark=1))
        elif revocation == "payload":
            await independent.execute(update(Publication).values(payload={**selected.publication.payload, "run_id": "other-run"}))
        elif revocation in {"receipt", "revision"}:
            await independent.execute(update(Publication).values(**({"receipt": None} if revocation == "receipt" else {"revision": 2})))
        elif revocation == "deleted":
            await independent.execute(delete(Publication))
        elif revocation == "scope":
            await independent.execute(update(Run).values(scope={**selected.run.scope, "owner_id": "foreign-owner"}))
        else:
            await independent.execute(update(Run).values(input={**selected.run.input, "sources": [{"record_id": "foreign-source"}]}))
        await independent.commit()
    # Deliberately retain the old identity-map objects and the already-built store.
    assert selected.grant.revoked_at is None and selected.parent.revoked_at is None
    assert selected.publication.state == "published" and selected.publication.invalidation_watermark == 0
    with pytest.raises(HTTPException) as denied:
        await selected.read("fixture-result", "product-context")
    assert denied.value.status_code == 404 and denied.value.detail == {"code": "not_found"}
    selected.backend.publish.assert_not_awaited()


async def test_transform_contribution_rechecks_original_source_binding(contribution):
    if contribution.service != "transform":
        pytest.skip("FinOps fixture has no repository source")
    async with contribution.sessions() as independent:
        await independent.execute(update(ProductSourceBinding).values(status="revoked"))
        await independent.commit()
    with pytest.raises(HTTPException) as denied:
        await contribution.read("fixture-result", "product-context")
    assert denied.value.status_code == 404


@pytest.mark.parametrize("authority", [None, False, "allowed"])
async def test_contribution_requires_an_explicit_current_human_authority(contribution, authority):
    async def checked(*args):
        return authority

    contribution.store.contribution_authority = checked if authority is not None else None
    with pytest.raises(HTTPException) as denied:
        await contribution.read("fixture-result", "product-context")
    assert denied.value.status_code == 404


async def test_contribution_does_not_enable_service_readers(contribution):
    contribution.backend.actor = contribution.actor.model_copy(update={"account_type": "service"})
    with pytest.raises(HTTPException) as denied:
        await contribution.read("fixture-result", "product-context")
    assert denied.value.status_code == 404

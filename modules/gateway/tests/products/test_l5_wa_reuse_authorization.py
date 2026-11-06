"""Retained copies require exact new owner consent after verified owned cleanup."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from adp_platform_common.l5_contributions.contract import digest
from fastapi import HTTPException
from l5_services.api.bindings import _intent_hash
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from l5_services.models.setup import BindingOperation, SetupIntent
from l5_services.models.source_retention import SourceRetention
from l5_services.models.wa_reuse_authorization import WellArchitectedReuseAuthorization as Authorization
from l5_services.publication.contributions import create_wa_publications
from sqlalchemy import delete, select, update

from src.auth.dependencies import get_current_user
from src.l5_consumer_authority import authorize_assertion, claims_for, current
from src.l5_service_identity import worker_id
from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_contribution_authority import contribution as _contribution

contribution = _contribution

pytestmark = pytest.mark.parametrize("contribution", ["transform"], indirect=True)


@pytest.fixture
def valid_token_context(valid_token_context):
    return valid_token_context.model_copy(update={"user_id": str(uuid4()), "org_id": str(uuid4())})


@pytest.fixture
async def retained(contribution, db_session, app):
    selected = contribution
    selected.actor = selected.actor.model_copy(update={"auth_source": "jwt", "token_use": "access"})
    run = selected.run
    run.service_id = "well_architected"
    run.input = {"capability": "architecture_review", "sources": run.input["sources"]}
    run.admission = {**run.admission, "admitted_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    run.native_job_ref = {"profile_arn": "arn:aws:wellarchitected:us-east-1:123456789012:profile/fixture", "review_arn": "fixture-review"}
    run.provider_readback_ref = {"record_id": "fixture-readback", "record_revision": "1"}
    run.manifest = {"archive": {"artifact_id": "fixture-manifest", "sha256": "a" * 64}}
    config = await db_session.get(Configuration, (run.tenant_id, run.configuration_id))
    config.service_id = "well_architected"
    binding = await db_session.get(Binding, (run.tenant_id, run.binding_id))
    binding.service_id, binding.state, binding.ownership, binding.generation = "well_architected", "cleaned", "adp_created", 2
    binding.native_resource_ref = {**binding.connection_ref, "record_type": "agent_profile", "record_id": run.native_job_ref["profile_arn"]}
    binding.provider_identity = {"account_id": "123456789012", "region": "us-east-1", "role_arn": "arn:aws:iam::123456789012:role/fixture"}
    for grant in (selected.parent, selected.grant):
        grant.service_id = worker_id("well_architected")
        grant.capabilities = ["architecture_review"]
        grant.revoked_at = datetime.now(UTC)
    creation = SetupIntent(
        tenant_id=run.tenant_id,
        operation_id="fixture-create",
        product_id=run.product_id,
        project_id=run.project_id,
        service_id=run.service_id,
        configuration_id=run.configuration_id,
        actor_id=selected.actor.user_id,
        idempotency_key="fixture-create-01",
        scope=run.scope,
        request={"action": "create", "configuration_revision": 1},
        binding_generation=1,
        authorization={"actor_id": selected.actor.user_id},
        intent_hash="",
    )
    creation.intent_hash = _intent_hash(creation)
    binding.creation_receipt = {
        "operation_id": creation.operation_id,
        "intent_hash": creation.intent_hash,
        "native_resource_ref": binding.native_resource_ref,
        "provider_identity": binding.provider_identity,
    }
    cleanup = SetupIntent(
        tenant_id=run.tenant_id,
        operation_id="fixture-cleanup",
        product_id=run.product_id,
        project_id=run.project_id,
        service_id=run.service_id,
        configuration_id=run.configuration_id,
        actor_id=selected.actor.user_id,
        idempotency_key="fixture-cleanup-01",
        scope=run.scope,
        request={"action": "cleanup", "configuration_revision": 1, "binding_id": run.binding_id, "owned_cleanup_confirmed": True},
        binding_generation=2,
        authorization={"actor_id": selected.actor.user_id},
        owned_resource_receipt=binding.creation_receipt,
        intent_hash="",
    )
    cleanup.intent_hash = _intent_hash(cleanup)
    operation = BindingOperation(
        tenant_id=run.tenant_id,
        operation_id=cleanup.operation_id,
        binding_id=run.binding_id,
        action="cleanup",
        state="succeeded",
        revision=2,
        gaps=[],
        cleanup_receipt={
            "schema_version": 1,
            "source": "installed-worker",
            "absent": True,
            "operation_id": cleanup.operation_id,
            "intent_hash": cleanup.intent_hash,
            "creation_receipt": binding.creation_receipt,
            "native_resource_ref": binding.native_resource_ref,
            "provider_identity": binding.provider_identity,
        },
    )
    result = await db_session.get(AssessmentResult, (run.tenant_id, "fixture-result"))
    detail = await db_session.get(ResultDetail, (run.tenant_id, result.result_id))
    detail.payload = {
        "kind": "well_architected_result",
        "result_id": result.result_id,
        "run_id": run.run_id,
        "revision": result.version,
        "scope": run.scope,
        "native_job_ref": run.native_job_ref,
        "review_mode": "source_only",
        "manifest_ref": run.manifest["archive"],
    }
    await db_session.delete(selected.publication)
    await db_session.flush()
    publications = create_wa_publications(run, result, detail, now=datetime.now(UTC))
    for publication in publications:
        publication.state = "published"
        publication.receipt = {
            "receipt_id": publication.destination + "-receipt",
            "contribution_id": publication.publication_id,
            "admitted_schema": "1.0",
            "consumer_revision": "1",
            "record_refs": [publication.payload["result_ref"]],
        }
    db_session.add_all([creation, cleanup, operation, *publications])
    await db_session.commit()
    selected.publication = publications[0]
    selected.publications, selected.cleanup = publications, operation
    selected.path = f"/v1/products/{run.product_id}/projects/{run.project_id}/services/well_architected/results/{result.result_id}"
    selected.args = dict(tenant_id=run.tenant_id, contribution_id=publications[0].publication_id, destination="product-context", action="read")
    app.dependency_overrides[get_current_user] = lambda: selected.actor
    return selected


async def consent(client, selected, *, key="explicit_reuse_consent_01"):
    preview = await client.get(selected.path + "/reuse-scope")
    assert preview.status_code == 200, preview.text
    body = {"scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    headers = {"If-Match": preview.headers["etag"], "Idempotency-Key": key}
    granted = await client.post(selected.path + "/reuse-authorizations", json=body, headers=headers)
    assert granted.status_code == 200, granted.text
    return granted, body, headers


async def test_explicit_consent_releases_exact_copy_and_revoke_replacement_cannot_revive_assertions(retained, test_client):
    selected = retained
    async with selected.sessions() as db:
        with pytest.raises(HTTPException):
            await current(db, selected.actor, **selected.args)
    granted, body, headers = await consent(test_client, selected)
    repeated = await test_client.post(selected.path + "/reuse-authorizations", json=body, headers=headers)
    assert repeated.status_code == 200 and repeated.json() == granted.json()
    async with selected.sessions() as db:
        run, publication = await current(db, selected.actor, **selected.args)
        claims = claims_for(selected.actor, run, publication, action="read")
    assert await authorize_assertion(claims, selected.sessions)
    revoke_path = selected.path + "/reuse-authorizations/" + granted.json()["authorization_id"]
    revoked = await test_client.delete(revoke_path, headers={"If-Match": granted.headers["etag"]})
    assert revoked.status_code == 200
    assert not await authorize_assertion(claims, selected.sessions)
    restored = await test_client.post(selected.path + "/reuse-authorizations", json=body, headers=headers)
    assert restored.status_code == 409
    await consent(test_client, selected, key="explicit_replacement_01")
    assert not await authorize_assertion(claims, selected.sessions)
    async with selected.sessions() as db:
        await current(db, selected.actor, **selected.args)
        assert (await db.get(CredentialServiceDelegation, selected.grant.id)).revoked_at is not None


@pytest.mark.parametrize(
    "changed",
    [
        "membership",
        "relationship",
        "source",
        "source_retention",
        "product",
        "project",
        "owner",
        "binding",
        "cleanup",
        "native",
        "result",
        "publication",
        "receipt",
        "invalidation",
        "expiry",
    ],
)
async def test_each_release_rechecks_committed_scope_or_revocation(retained, test_client, changed):
    selected = retained
    await consent(test_client, selected)
    async with selected.sessions() as stale:
        run, publication = await current(stale, selected.actor, **selected.args)
        claims = claims_for(selected.actor, run, publication, action="read")
        assert await authorize_assertion(claims, selected.sessions)
        async with selected.sessions() as db:
            if changed == "membership":
                await db.execute(delete(TeamMembership).where(TeamMembership.user_id == selected.actor.user_id))
            elif changed == "relationship":
                await db.execute(update(ProductProjectBinding).values(relationship_revision=2))
            elif changed == "source":
                await db.execute(update(ProductSourceBinding).values(status="revoked"))
            elif changed == "source_retention":
                db.add(
                    SourceRetention(
                        tenant_id=run.tenant_id,
                        run_id=run.run_id,
                        project_id=run.project_id,
                        artifact_id="fixture-artifact",
                        artifact_ref={},
                        reason="test",
                        required_until=datetime.now(UTC),
                        policy_expires_at=datetime.now(UTC),
                        revoked_at=datetime.now(UTC),
                    )
                )
            elif changed in {"product", "project"}:
                await db.execute(update(Product if changed == "product" else Project).values(archived_at=datetime.now(UTC)))
            elif changed == "owner":
                await db.execute(update(Configuration).values(owner_id="foreign-owner"))
            elif changed == "binding":
                await db.execute(update(Binding).values(generation=3))
            elif changed == "cleanup":
                await db.execute(update(BindingOperation).values(cleanup_receipt=None))
            elif changed == "native":
                await db.execute(update(Run).values(provider_readback_ref={"record_id": "replacement"}))
            elif changed == "result":
                await db.execute(update(AssessmentResult).values(payload={"replacement": True}))
            elif changed == "publication":
                payload = deepcopy(selected.publications[1].payload)
                payload["result"]["changed"] = True
                await db.execute(
                    update(Publication)
                    .where(Publication.destination == "engineering-insights")
                    .values(payload=payload, contribution_hash=digest(payload))
                )
            elif changed == "receipt":
                receipt = {**selected.publications[1].receipt, "consumer_revision": "2"}
                await db.execute(update(Publication).where(Publication.destination == "engineering-insights").values(receipt=receipt))
            elif changed == "invalidation":
                await db.execute(update(Publication).where(Publication.destination == "engineering-insights").values(invalidation_watermark=1))
            else:
                await db.execute(update(Authorization).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            await db.commit()
        with pytest.raises(HTTPException):
            await current(stale, selected.actor, **selected.args)
        assert not await authorize_assertion(claims, selected.sessions)


@pytest.mark.parametrize(
    "change", [{"account_type": "service", "auth_source": "iam"}, {"user_id": "foreign"}, {"org_id": "foreign"}, {"token_use": "id"}]
)
async def test_reuse_cannot_authorize_another_principal_or_provider_action(retained, test_client, app, change):
    selected = retained
    await consent(test_client, selected)
    async with selected.sessions() as db:
        for action in ("admit", "use", "invalidate"):
            with pytest.raises(HTTPException):
                await current(db, selected.actor, **{**selected.args, "action": action}, task_id="unowned-task")
        with pytest.raises(HTTPException):
            await current(db, selected.actor.model_copy(update=change), **selected.args)
    app.dependency_overrides[get_current_user] = lambda: selected.actor.model_copy(update=change)
    denied = await test_client.get(selected.path + "/reuse-scope")
    assert denied.status_code in {403, 404}


async def test_no_automatic_consent_and_explicit_preconditions(retained, test_client):
    selected = retained
    listing = await test_client.get(selected.path + "/reuse-authorizations")
    assert listing.status_code == 200 and listing.json() == {"items": []}
    preview = await test_client.get(selected.path + "/reuse-scope")
    assert preview.status_code == 200
    body = {"scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    assert (await test_client.post(selected.path + "/reuse-authorizations", json=body)).status_code == 428
    headers = {"If-Match": '"' + "0" * 64 + '"', "Idempotency-Key": "explicit_consent_01"}
    assert (await test_client.post(selected.path + "/reuse-authorizations", json=body, headers=headers)).status_code == 412
    headers["If-Match"] = preview.headers["etag"]
    body["expires_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    assert (await test_client.post(selected.path + "/reuse-authorizations", json=body, headers=headers)).status_code == 422
    assert (await test_client.get(selected.path.replace("well_architected", "devops") + "/reuse-scope")).status_code == 422
    async with selected.sessions() as db:
        assert not (await db.scalars(select(Authorization))).all()


async def test_later_use_requires_current_canonical_task_and_policy(retained, test_client, monkeypatch):
    import boto3
    from boto3.dynamodb.types import TypeSerializer
    from moto import mock_aws

    from src.agentauth.task_service_policy import TaskServicePolicyStore
    from src.tasks import routes
    from src.tasks.dynamo_read_store import DynamoTaskReadStore
    from src.tasks.records import META_SORT_KEY, task_partition
    from src.tasks.store import TaskStore

    selected = retained
    await consent(test_client, selected)
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "wa-reuse-authority")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table, partition, sort in (("wa-reuse-tasks", "event_id", "arrived_at"), ("wa-reuse-authority", "pk", "sk")):
            client.create_table(
                TableName=table,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in (partition, sort)],
                KeySchema=[{"AttributeName": partition, "KeyType": "HASH"}, {"AttributeName": sort, "KeyType": "RANGE"}],
            )
        policy = {
            "status": "active",
            "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["read"],
            "model_policy_version": "1",
            "limits": {"max_duration_minutes": 5, "max_turns": 2, "max_output_tokens_per_turn": 100, "max_usd_per_task": 1},
        }
        policies = TaskServicePolicyStore(table_name="wa-reuse-authority", client=client)
        principal = "human:" + selected.actor.user_id
        policies.put(
            tenant_id=selected.run.tenant_id, canonical_principal_id=principal, expected_version=0, policy=policy, updated_by=selected.actor.user_id
        )
        now = datetime.now(UTC)
        task_id = "tsk_" + str(uuid4())
        row = {
            "event_id": task_partition(task_id),
            "arrived_at": META_SORT_KEY,
            "task_id": task_id,
            "invocation_id": str(uuid4()),
            "scope": {"tenant": selected.run.tenant_id, "canonical_principal": principal},
            "persona": "agent-task-investigator",
            "state": "completed",
            "version": 1,
            "generation": 1,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "deadline_at": (now + timedelta(minutes=5)).isoformat(),
            "product_id": selected.run.product_id,
            "project_id": selected.run.project_id,
        }
        serializer = TypeSerializer()
        client.put_item(TableName="wa-reuse-tasks", Item={key: serializer.serialize(value) for key, value in row.items()})
        store = DynamoTaskReadStore(
            TaskStore(table_name="wa-reuse-tasks", authority_table_name="wa-reuse-authority", dynamodb_client=client),
            s3_client=boto3.client("s3", region_name="us-east-1"),
            artifact_bucket="wa-reuse-artifacts",
        )
        monkeypatch.setattr(routes, "get_store", lambda: store)
        args = {**selected.args, "action": "use", "task_id": task_id}
        async with selected.sessions() as db:
            run, publication = await current(db, selected.actor, **args)
            claims = claims_for(selected.actor, run, publication, action="use", usage={"task_id": task_id})
        assert await authorize_assertion(claims, selected.sessions)
        policies.put(
            tenant_id=selected.run.tenant_id,
            canonical_principal_id=principal,
            expected_version=1,
            policy={**policy, "status": "disabled"},
            updated_by=selected.actor.user_id,
        )
        assert not await authorize_assertion(claims, selected.sessions)

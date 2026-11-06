"""Per-call asset scope and mounted broker boundaries use no live provider."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request, Response
from l5_services.devops.knowledge_native import KnowledgeNativeDriver
from l5_services.devops.knowledge_runtime import KnowledgeScopedClient
from l5_services.devops.knowledge_work import current_context, resolve_admitted
from l5_services.workers.execution import AuthorityRevoked

from src.internal import l5_credential_routes as broker
from src.shared.models.vault import UserCredential
from tests.products.test_l5_devops_knowledge_admission import knowledge_fixture as knowledge_fixture
from tests.products.test_l5_knowledge_effects import knowledge_worker as knowledge_worker
from tests.products.test_l5_knowledge_native_consent import native_consent
from tests.products.test_l5_knowledge_work import claimed_knowledge as claimed_knowledge
from tests.products.test_l5_owner_delegation import consent_fixture as consent_fixture
from tests.products.test_l5_owner_delegation import l5_owner as l5_owner

__all__ = ["knowledge_fixture", "claimed_knowledge", "consent_fixture", "l5_owner", "knowledge_worker"]


async def test_scoped_client_rechecks_each_call_and_rejects_unselected_effects(knowledge_worker):
    row, worker, native = knowledge_worker
    checked = AsyncMock()
    scoped = KnowledgeScopedClient(native, row.intent, row.claim, checked, row.claim.lease_until, asyncio.get_running_loop())
    driver = KnowledgeNativeDriver(scoped)
    async with worker.sessions() as session:
        context = await current_context(session, row.intent, row.claim)
    asset = (await resolve_admitted(context, row.claim, worker.sources, AsyncMock()))[0]
    plan = await asyncio.to_thread(driver.prepare, "native-space", asset, row.intent.operation_id, None)
    receipt = await asyncio.to_thread(driver.mutate, plan, asset)
    assert receipt["asset_id"] == "1" and checked.await_count >= 4
    calls = len(native.calls)
    with pytest.raises(AuthorityRevoked, match="method_outside_scope"):
        scoped.delete_asset
    with pytest.raises(AuthorityRevoked, match="request_outside_scope"):
        await asyncio.to_thread(scoped.list_assets, agentSpaceId="other-space")
    with pytest.raises(AuthorityRevoked, match="asset_outside_scope"):
        await asyncio.to_thread(scoped.get_asset_content, agentSpaceId="native-space", assetId="unrelated", assetVersion=1)
    with pytest.raises(AuthorityRevoked, match="publication_outside_scope"):
        await asyncio.to_thread(
            scoped.update_asset,
            agentSpaceId="native-space",
            assetId="1",
            metadata=plan["metadata"],
            content={"file": {"path": plan["path"], "body": {"text": "unselected content"}}},
            clientToken=plan["client_token"],
        )
    assert len(native.calls) == calls
    checked.side_effect = AuthorityRevoked("knowledge_source_access_revoked")
    with pytest.raises(AuthorityRevoked, match="authority_denied"):
        await asyncio.to_thread(scoped.get_asset, agentSpaceId="native-space", assetId="1")
    assert len(native.calls) == calls


async def test_native_endpoint_requires_separate_consent_and_generic_endpoint_refuses_it(
    test_client, db_session, claimed_knowledge, l5_owner, monkeypatch
):
    row = claimed_knowledge
    actor = l5_owner.model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "agent_registry_id": row.grant.service_id,
            "credential_scopes": ["l5:credential-session", "l5:knowledge-publication"],
        }
    )
    request = Request({"type": "http"})
    request.state.token_context = actor
    body = row.body.model_copy(update={"connection_ref": broker.ConnectionRef.model_validate(row.grant.sources[0])})
    assume = AsyncMock()
    monkeypatch.setattr(broker, "assume_bound_role", assume)
    with pytest.raises(HTTPException):
        await broker.knowledge_publication_session(body, request, Response(), db_session, object(), broker.get_l5_work_authorizer(db_session))
    assume.assert_not_awaited()
    _, _, _, native = await native_consent(test_client, row)
    credential = await db_session.get(UserCredential, row.grant.credential_id)
    checked_policies = []

    async def issue(*args, **kwargs):
        await kwargs["validate"](credential)
        await kwargs["validate"](credential, revalidate=True)
        checked_policies.append(kwargs["session_policy"])
        return SimpleNamespace(
            access_key_id="offline-access",
            secret_access_key="offline-secret",
            session_token="offline-session",
            expiration=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            region="us-east-1",
        )

    assume.side_effect = issue
    monkeypatch.setattr("src.agentauth.broker_identity.verify_selected_user_credential", AsyncMock())
    monkeypatch.setattr(
        broker,
        "verify_assumed_session",
        AsyncMock(return_value={**row.fixture["binding"].provider_identity, "verified_at": datetime.now(UTC).isoformat()}),
    )
    result = await broker.knowledge_publication_session(body, request, Response(), db_session, object(), broker.get_l5_work_authorizer(db_session))
    assert result["native_delegation_id"] == native["delegation_id"] and result["source_delegation_id"] == row.grant.id
    assert len(checked_policies) == 1 and "DeleteAsset" not in checked_policies[0] and "CreateBacklogTask" not in checked_policies[0]
    native_body = body.model_copy(update={"delegation_id": native["delegation_id"], "delegation_revision": native["revision"]})
    with pytest.raises(HTTPException):
        await broker.credential_session(native_body, request, Response(), db_session, object(), broker.get_l5_work_authorizer(db_session))
    assert assume.await_count == 1
    request.state.token_context = actor.model_copy(update={"credential_scopes": ["l5:credential-session"]})
    with pytest.raises(HTTPException):
        await broker.knowledge_publication_session(body, request, Response(), db_session, object(), broker.get_l5_work_authorizer(db_session))
    assert assume.await_count == 1


async def test_installed_factory_dispatches_retained_knowledge_through_scoped_runtime(test_client, db_session, knowledge_worker, monkeypatch):
    from l5_services import runtime_devops, runtime_support

    from src.auth.l5_knowledge_native_consent import native_scope
    from tests.products.test_l5_knowledge_resume import blocked, reconcile

    row, worker, native = knowledge_worker
    await blocked(db_session, row)
    _, _, _, consent = await native_consent(test_client, row)
    assert (await reconcile(test_client, row)).status_code == 202
    identity = row.intent.authorization["selection"]["provider_identity"]

    async def broker_call(path, *, json):
        assert path == "/internal/v1/l5/knowledge-publication-session"
        assert json["delegation_id"] == row.grant.id
        await db_session.refresh(row.work)
        assert json["lease_owner"] == row.work.lease_owner and json["lease_fence"] == row.work.fence
        return httpx.Response(
            200,
            json={
                "source_delegation_id": row.grant.id,
                "source_delegation_revision": row.grant.revision,
                "native_delegation_id": consent["delegation_id"],
                "native_delegation_revision": consent["revision"],
                "operation_id": row.intent.operation_id,
                "attempt_id": row.work.work_id,
                "provider_identity": identity,
                "expires_at": row.work.lease_until.replace(tzinfo=UTC).isoformat(),
                "setup_verified": {
                    "knowledge_native": True,
                    "intent_hash": row.intent.intent_hash,
                    "scope": native_scope(row.intent, row.grant),
                    "binding_id": row.intent.request["binding_id"],
                    "binding_generation": row.intent.binding_generation,
                    "native_space_ref": row.intent.authorization["selection"]["native_space_ref"],
                    "provider_identity": identity,
                },
            },
            request=httpx.Request("POST", "https://example.invalid" + path),
        )

    transport = SimpleNamespace(post=AsyncMock(side_effect=broker_call))
    provider = SimpleNamespace(client=lambda *args, **kwargs: native)
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setattr(runtime_support, "sessions", lambda: worker.sessions)
    monkeypatch.setattr(runtime_support, "gateway_transport", lambda: transport)
    monkeypatch.setattr(runtime_support, "ScopedSources", lambda _: worker.sources)
    monkeypatch.setattr(runtime_support, "archive_store", lambda _: None)
    monkeypatch.setattr(runtime_support, "queue_publisher", lambda _: AsyncMock())
    monkeypatch.setattr(runtime_support, "provider_session", lambda *args: provider)
    monkeypatch.setattr(runtime_devops, "installed_publication_client", lambda **kwargs: None)
    installed = runtime_devops.factory()
    assert await installed.execute({"schema_version": "1.0", "work_id": row.work.work_id, "generation": row.work.generation})
    assert native.calls.count("create") == 1 and "update" not in native.calls and transport.post.await_count > 4
    await db_session.refresh(row.operation)
    assert row.operation.state == "waiting" and row.operation.gaps == ["knowledge_native_use_unqualified"]

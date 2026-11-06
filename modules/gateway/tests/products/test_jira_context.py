"""Current membership, credential rotation and pagination govern shared Jira reads."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from src.auth import atlassian_system_routes as admin
from src.auth.dependencies import get_current_user
from src.products import jira_context as context
from src.products.jira_context_contract import JiraContextQuery
from src.products.models import Product, ProductProjectBinding, ProductSourceBinding, Project
from src.shared.database import get_db
from src.shared.models.atlassian_system import AtlassianSystemKey
from src.shared.models.base import utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership
from tests.auth.test_connections_routes import caller
from tests.auth.test_connections_routes import session as connection_session

session = connection_session


@pytest.fixture
async def setup(session, monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED", "true")
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-test-oauth-user"))
    session.add_all(
        [
            TenantMembership(tenant_id="org-acme", user_id="user-alice", role="member"),
            TeamMembership(org_id="org-acme", user_id="user-alice", team_id="team-eng"),
            Product(
                tenant_id="org-acme",
                id="product-a",
                name="Example",
                purpose="Test",
                intended_users=[],
                owner_user_id="user-alice",
                team_id="team-eng",
                starting_point="existing",
                created_by="user-alice",
            ),
            Project(tenant_id="org-acme", id="project-a", name="App", owner_user_id="user-alice"),
        ]
    )
    await session.flush()
    session.add(ProductProjectBinding(tenant_id="org-acme", id="binding", product_id="product-a", project_id="project-a", provenance={}))
    await session.commit()
    monkeypatch.setattr(admin, "provision_key", AsyncMock())
    monkeypatch.setattr(admin, "remove_key", AsyncMock())
    monkeypatch.setattr(admin, "adapter_read", AsyncMock(return_value={"account_id": "service-account", "schemas": {"listJiraProjects": {}}}))
    actor = caller().model_copy(update={"is_admin": True})
    body = admin.ConnectSystem(
        label="Shared",
        site_id="12345678-1234-1234-1234-123456789abc",
        api_key="secret-value",
        expires_at=utcnow() + timedelta(days=30),
        product_ids=["product-a"],
    )
    saved = await admin.save_connection(body, actor, session)
    return actor, body, saved


async def bind(session, monkeypatch, setup):
    actor, _, saved = setup
    monkeypatch.setattr(context, "adapter_read", AsyncMock(return_value={"data": {"values": [{"id": "100", "key": "APP", "name": "App"}]}}))
    source = await context.select_source(
        "product-a",
        "project-a",
        context.JiraSourceInput(connection_id=saved["id"], jira_project_id="100", jira_project_key="APP", share_with_product=True),
        actor,
        session,
    )
    return JiraContextQuery(product_id="product-a", project_id="project-a", source_id=source["id"], view="issues")


async def test_setup_stores_only_identity_reference_and_org_owner(session, setup):
    _, _, saved = setup
    credential, connection = await admin.current_system(session, saved["id"], "org-acme", "product-a")
    assert credential.owner_scope == "org" and credential.provider == "agentcore-identity"
    assert credential.secret_arn.endswith(connection.provider_name)
    assert "secret-value" not in repr(credential.__dict__)
    assert await session.scalar(select(AtlassianSystemKey.provider_name)) == connection.provider_name
    for tenant, product in [("other-tenant", "product-a"), ("org-acme", "other-product")]:
        with pytest.raises(HTTPException) as error:
            await admin.current_system(session, saved["id"], tenant, product)
        assert error.value.status_code == 404


async def test_rotation_verifies_before_switch_and_preserves_old_key_on_failure(session, setup, monkeypatch):
    actor, body, saved = setup
    replacement = body.model_copy(update={"expected_revision": saved["revision"]})
    monkeypatch.setattr(admin, "adapter_read", AsyncMock(side_effect=RuntimeError("provider failed")))
    with pytest.raises(HTTPException):
        await admin.save_connection(replacement, actor, session, saved["id"])
    _, current = await admin.current_system(session, saved["id"], "org-acme", "product-a")
    assert current.revision == saved["revision"] and current.status == "ready"
    failed_provider = admin.provision_key.call_args.args[1]
    admin.remove_key.assert_awaited_once_with(failed_provider)
    monkeypatch.setattr(admin, "adapter_read", AsyncMock(return_value={"account_id": "service-account", "schemas": {}}))
    rotated = await admin.save_connection(replacement, actor, session, saved["id"])
    assert rotated["revision"] != saved["revision"]
    assert admin.remove_key.await_count == 2
    with pytest.raises(HTTPException) as error:
        await admin.save_connection(replacement, actor, session, saved["id"])
    assert error.value.status_code == 409


async def test_sources_require_live_membership_and_do_not_follow_project_reassignment(session, setup, monkeypatch):
    query = await bind(session, monkeypatch, setup)
    actor = caller()
    assert len(await context.sources(session, actor, "product-a", "project-a")) == 1
    binding = await session.scalar(select(ProductSourceBinding))
    binding.desired = {**binding.desired, "product_id": "some-other-product"}
    await session.commit()
    assert await context.sources(session, actor, "product-a", "project-a") == []
    binding.desired = {**binding.desired, "product_id": "product-a"}
    await session.execute(delete(TeamMembership).where(TeamMembership.user_id == actor.user_id))
    await session.commit()
    context.adapter_read.reset_mock()
    with pytest.raises(HTTPException):
        await context.read_context(session, actor, query)
    context.adapter_read.assert_not_awaited()


async def test_scoped_jql_cursor_and_deadline_survive_the_roundtrip(session, setup, monkeypatch):
    query = await bind(session, monkeypatch, setup)
    reply = {
        "data": {"issues": [], "nextPageToken": "upstream-secret-cursor", "isLast": False},
        "upstream_tool": "searchJiraIssuesUsingJql",
        "retrieved_at_ms": 1,
    }
    monkeypatch.setattr(context, "adapter_read", AsyncMock(return_value=reply))
    page = await context.read_context(session, caller(), query, deadline_at_ms=12345)
    await session.commit()
    assert "upstream-secret-cursor" not in repr(page)
    assert page["coverage"] == "page" and page["next_cursor"]
    call = context.adapter_read.call_args
    assert call.args[2]["jql"] == "project IN (100) ORDER BY Rank ASC"
    assert call.kwargs["deadline_at_ms"] == 12345
    continuation = query.model_copy(update={"cursor": page["next_cursor"]})
    await context.read_context(session, caller(), continuation)
    assert context.adapter_read.call_args.args[2]["nextPageToken"] == "upstream-secret-cursor"
    with pytest.raises(HTTPException) as error:
        await context.read_context(session, caller(), continuation.model_copy(update={"text": "something else"}))
    assert error.value.status_code == 409
    _, connection = await admin.current_system(session, setup[2]["id"], "org-acme", "product-a")
    connection.revision = "rotated"
    await session.commit()
    with pytest.raises(HTTPException):
        await context.read_context(session, caller(), continuation)


async def test_unlink_and_revoke_take_effect_without_provider_calls(session, setup, monkeypatch):
    query = await bind(session, monkeypatch, setup)
    await context.unlink_source("product-a", "project-a", query.source_id, 1, caller(), session)
    assert await context.sources(session, caller(), "product-a", "project-a") == []
    await admin.disconnect_system_connection(setup[2]["id"], setup[0], session)
    with pytest.raises(HTTPException):
        await admin.current_system(session, setup[2]["id"], "org-acme", "product-a")


async def test_kanban_backlog_is_read_from_provider_but_sprints_are_unavailable(session, setup, monkeypatch):
    query = await bind(session, monkeypatch, setup)
    binding = await session.scalar(select(ProductSourceBinding))
    binding.desired = {**binding.desired, "board_id": 5, "board_type": "kanban"}
    await session.commit()
    result = {"data": {"issues": [{"key": "APP-1"}], "isLast": True}, "retrieved_at_ms": 1}
    monkeypatch.setattr(context, "adapter_read", AsyncMock(return_value=result))
    page = await context.read_context(session, caller(), query.model_copy(update={"view": "backlog"}))
    assert page["data"]["issues"] == [{"key": "APP-1"}]
    assert context.adapter_read.call_args.args[2]["dataset"] == "backlog"
    context.adapter_read.reset_mock()
    with pytest.raises(HTTPException) as error:
        await context.read_context(session, caller(), query.model_copy(update={"view": "sprints"}))
    assert error.value.status_code == 422
    context.adapter_read.assert_not_awaited()


async def test_non_admin_and_invalid_inputs_never_echo_token(session, setup):
    app = FastAPI()
    app.include_router(admin.router)

    async def db():
        yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: caller()
    client = TestClient(app)
    payload = {**setup[1].model_dump(mode="json"), "api_key": "do-not-echo", "site_id": "invalid"}
    assert client.post("/auth/connections/atlassian-mcp/system", json=payload).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: setup[0]
    response = client.post("/auth/connections/atlassian-mcp/system", json=payload)
    assert response.status_code == 422 and "do-not-echo" not in response.text


async def test_expired_or_revoked_connections_remain_visible_for_source_removal(session, setup, monkeypatch):
    query = await bind(session, monkeypatch, setup)
    await admin.disconnect_system_connection(setup[2]["id"], setup[0], session)
    listing = await context.list_sources("product-a", "project-a", caller(), session)
    assert listing["sources"][0]["id"] == query.source_id
    assert listing["sources"][0]["available"] is False
    with pytest.raises(HTTPException):
        await context.read_context(session, caller(), query)
    await context.unlink_source("product-a", "project-a", query.source_id, 1, caller(), session)
    assert (await context.list_sources("product-a", "project-a", caller(), session))["sources"] == []


async def test_failed_cleanup_is_journaled_and_can_be_retried(session, setup, monkeypatch):
    saved = setup[2]
    monkeypatch.setattr(admin, "remove_key", AsyncMock(side_effect=RuntimeError("temporarily unavailable")))
    response = await admin.disconnect_system_connection(saved["id"], setup[0], session)
    assert response["provider_cleanup_pending"] is True
    assert await session.scalar(select(AtlassianSystemKey.provider_name))
    with pytest.raises(HTTPException):
        await admin.current_system(session, saved["id"], "org-acme", "product-a")
    monkeypatch.setattr(admin, "remove_key", AsyncMock())
    assert (await admin.disconnect_system_connection(saved["id"], setup[0], session))["provider_cleanup_pending"] is False


async def test_shared_key_does_not_create_an_extra_generic_credential_choice(session, setup):
    from src.auth.vault_service import list_credentials

    assert await list_credentials(session, setup[0]) == []


async def test_admin_can_reverify_changed_schemas_without_resubmitting_key(session, setup, monkeypatch):
    old_revision = setup[2]["revision"]
    admin.provision_key.reset_mock()
    monkeypatch.setattr(admin, "adapter_read", AsyncMock(return_value={"account_id": "service-account", "schemas": {"new": {}}}))
    result = await admin.verify_system_connection(setup[2]["id"], setup[0], session)
    assert result["revision"] != old_revision and result["capabilities"] == ["new"]
    admin.provision_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_jira_source_is_visible_only_with_current_product_connection_and_pinned_selection(session, setup, monkeypatch):
    from src.products import source_routes

    query = await bind(session, monkeypatch, setup)
    binding = await session.scalar(select(ProductSourceBinding))
    _, connection = await admin.current_system(session, setup[2]["id"], "org-acme", "product-a")
    revision = binding.observed["source_revision"]
    assert len(revision) == 64 and binding.observed["connection_revision"] == connection.revision
    grant = SimpleNamespace(
        tenant_id="org-acme",
        product_id="product-a",
        project_id="project-a",
        execution_scope={},
        operations=["source:read"],
        credential_id=setup[2]["id"],
        installation_id=None,
        sources=[
            {
                "tenant_id": "org-acme",
                "project_id": "project-a",
                "source_connection_id": setup[2]["id"],
                "record_type": "tracker",
                "record_id": binding.native_id,
                "record_revision": revision,
            }
        ],
    )
    assert [item.id for item in await source_routes.service_visible_bindings(session, grant)] == [query.source_id]
    grant.credential_id = "other-credential"
    assert await source_routes.service_visible_bindings(session, grant) == []
    grant.credential_id = setup[2]["id"]
    binding.desired = {**binding.desired, "share_with_product": False}
    await session.commit()
    assert await source_routes.service_visible_bindings(session, grant) == []
    binding.desired = {**binding.desired, "share_with_product": True}
    connection.revision = "rotated"
    await session.commit()
    assert await source_routes.service_visible_bindings(session, grant) == []
    connection.revision = binding.observed["connection_revision"]
    binding.namespace = "another-site"
    await session.commit()
    assert await source_routes.service_visible_bindings(session, grant) == []
    binding.namespace = connection.site_id
    binding.observed = {**binding.observed, "stale_after": "2020-01-01T00:00:00+00:00"}
    await session.commit()
    assert await source_routes.service_visible_bindings(session, grant) == []


@pytest.mark.asyncio
async def test_shared_jira_selection_revision_changes_revoke_old_source_grants(session, setup, monkeypatch):
    from src.products.source_routes import allowed_source_ref

    query = await bind(session, monkeypatch, setup)
    binding = await session.scalar(select(ProductSourceBinding))
    previous, generation = binding.observed["source_revision"], binding.generation
    grant = SimpleNamespace(
        tenant_id="org-acme",
        project_id="project-a",
        execution_scope={},
        sources=[
            {
                "tenant_id": "org-acme",
                "project_id": "project-a",
                "source_connection_id": setup[2]["id"],
                "record_type": "tracker",
                "record_id": binding.native_id,
                "record_revision": previous,
            }
        ],
    )
    assert allowed_source_ref(grant, binding) == grant.sources[0]
    monkeypatch.setattr(
        context, "adapter_read", AsyncMock(return_value={"data": {"values": [{"id": "100", "key": "APP", "name": "Changed project"}]}})
    )
    await context.select_source(
        "product-a",
        "project-a",
        context.JiraSourceInput(
            connection_id=setup[2]["id"],
            jira_project_id="100",
            jira_project_key="APP",
            share_with_product=True,
        ),
        setup[0],
        session,
    )
    await session.refresh(binding)
    assert binding.observed["source_revision"] != previous
    assert binding.generation == generation + 1
    assert allowed_source_ref(grant, binding) is None
    assert query.source_id == binding.id


@pytest.mark.asyncio
async def test_shared_jira_source_refuses_rotation_during_provider_observation(session, setup, monkeypatch):
    _, connection = await admin.current_system(session, setup[2]["id"], "org-acme", "product-a")

    async def changed_connection(*args, **kwargs):
        connection.revision = "rotated-during-observation"
        await session.commit()
        return {"data": {"values": [{"id": "100", "key": "APP", "name": "App"}]}}

    monkeypatch.setattr(context, "adapter_read", changed_connection)
    with pytest.raises(HTTPException) as error:
        await context.select_source(
            "product-a",
            "project-a",
            context.JiraSourceInput(
                connection_id=setup[2]["id"],
                jira_project_id="100",
                jira_project_key="APP",
                share_with_product=True,
            ),
            setup[0],
            session,
        )
    assert error.value.status_code == 409
    assert await session.scalar(select(ProductSourceBinding)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection_method,location,accepted",
    [
        # Jira board details and the configuration schema/example use distinct
        # location fields. Forge returns the current board-detail location.
        ("forge_app", {"projectId": 100, "projectKey": "APP"}, True),
        ("forge_app", {"projectId": 200, "projectKey": "APP"}, False),
        ("forge_app", None, False),
        ("service_account", {"projectId": 100, "projectKey": "APP"}, True),
        ("service_account", {"type": "project", "projectKeyOrId": "100"}, True),
        ("service_account", {"type": "project", "projectKeyOrId": "APP"}, True),
        ("service_account", {"type": "project", "id": "100", "key": "APP", "name": "App"}, True),
        ("service_account", {"type": "project", "projectKeyOrId": "200"}, False),
        ("service_account", {"type": "project", "id": "200", "key": "APP"}, False),
        ("service_account", {"type": "user", "projectKeyOrId": "100"}, False),
        ("service_account", {"projectKeyOrId": "100"}, False),
        ("service_account", {"type": "project", "id": "100"}, False),
        ("service_account", {"type": "project", "projectKeyOrId": "100", "id": "200", "key": "APP"}, False),
        ("service_account", {"projectId": 100, "projectKey": "OTHER"}, False),
        ("service_account", {"projectId": True, "projectKey": "APP"}, False),
        ("service_account", None, False),
    ],
)
async def test_explicit_board_selection_requires_matching_project_location(session, setup, monkeypatch, connection_method, location, accepted):
    _, connection = await admin.current_system(session, setup[2]["id"], "org-acme", "product-a")
    connection.connection_method = connection_method
    await session.commit()
    project = {"id": "100", "key": "APP", "name": "Selected project"}
    board = {"id": 91, "type": "scrum", "name": "Board"}
    if location is not None:
        board["location"] = location
    reader = AsyncMock(side_effect=[{"data": {"values": [project]}}, {"data": board}])
    monkeypatch.setattr(context, "adapter_read", reader)
    body = context.JiraSourceInput(
        connection_id=setup[2]["id"],
        jira_project_id="100",
        jira_project_key="APP",
        board_id=91,
        share_with_product=True,
    )
    if accepted:
        selected = await context.select_source("product-a", "project-a", body, setup[0], session)
        saved = await session.get(ProductSourceBinding, (setup[0].org_id, selected["id"]))
        assert saved.native_id == "100:91" and saved.desired["jira_project_key"] == "APP"
    else:
        with pytest.raises(HTTPException) as refused:
            await context.select_source("product-a", "project-a", body, setup[0], session)
        assert refused.value.status_code == 503
        assert await session.scalar(select(ProductSourceBinding)) is None
    assert reader.await_count == 2
    assert all(call.args[0].connection_method == connection_method for call in reader.await_args_list)

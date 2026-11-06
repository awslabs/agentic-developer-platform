import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration import engine_commands, repository_binding
from src.orchestration.engine_commands import (
    EngineCommandConfig,
    EngineCommandReport,
    _handle_row,
    _resolve_target,
    flush_engine_commands,
    run_engine_command_pass,
)
from src.orchestration.models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.state import NodeState
from tests.orchestration.signed_command_rows import envelope, signed_row
from tests.orchestration.signed_command_rows import signing_keyring as _signing_keyring

signing_keyring = _signing_keyring


@pytest.fixture(autouse=True)
def _verified_event_repository(monkeypatch):
    async def verified(_session, **kwargs):
        return repository_binding.VerifiedRepository(
            "github",
            kwargs["repository_id"],
            kwargs["name"],
            kwargs["installation_id"],
            kwargs["name"],
            kwargs["issue"],
            kwargs["operation"],
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified)


async def _targets(session, *, org_id: str, installation_id: int):
    for name, repository_id in (("example/api", 1001), ("example/web", 1002)):
        flow = OrchestrationFlow(org_id=org_id, slug=name.replace("/", "-"), title=name, intent_ref="42")
        session.add(flow)
        await session.flush()
        session.add(
            OrchestrationAcceptedPlan(
                org_id=org_id,
                flow_id=flow.id,
                version=1,
                plan_hash="a" * 64,
                plan_document={
                    "nodes": [
                        dict(
                            address=f"{flow.slug}/epic/wave/story",
                            issue_ref="42",
                            repository_identity=name,
                            provider_repository_id=repository_id,
                            repository_connection={"provider": "github", "installation_id": installation_id},
                        )
                    ]
                },
            )
        )
        session.add(
            OrchestrationNode(
                org_id=org_id,
                flow_id=flow.id,
                epic_ref="epic",
                wave_ref="wave",
                node_ref="story",
                kind="story",
                title=name,
                issue_ref="42",
                repository_identity=name,
                provider_repository_id=repository_id,
                repository_connection={"provider": "github", "installation_id": installation_id},
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_signed_events_route_same_issue_number_to_distinct_repositories(db_session, monkeypatch, signing_keyring):
    base = envelope(tenant_id="tenant-commands", installation_id="77", issue_number=42, command_body="@agent-engine halt")
    await _targets(db_session, org_id=base["tenant_id"], installation_id=77)

    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(object(), None)))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_apply_command", applied)

    for repository, repository_id in (("example/api", 1001), ("example/web", 1002)):
        report = EngineCommandReport()
        await _handle_row(db_session, signed_row({**base, "repo": repository, "repo_id": repository_id}), report, access=None)
        assert report.commands_applied == 1
        target = await _resolve_target(
            db_session,
            org_id=base["tenant_id"],
            issue_number=42,
            repo=repository,
            repo_id=repository_id,
            installation_id=77,
        )
        assert target is not None
        assert target[1].repository_identity == repository
        assert applied.call_args.kwargs["flow_id"] == target[0]


@pytest.mark.asyncio
async def test_signed_colliding_events_transition_only_their_verified_flow_in_command_pass(
    db_session,
    monkeypatch,
    signing_keyring,
):
    org_id = "tenant-commands"
    await _targets(db_session, org_id=org_id, installation_id=77)
    nodes = (await db_session.scalars(select(OrchestrationNode).order_by(OrchestrationNode.repository_identity))).all()
    for node in nodes:
        node.state = NodeState.RUNNING.value
    await db_session.flush()

    context = SimpleNamespace(user_id="authorized-human")
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(context, None)))
    access = SimpleNamespace(
        get_user_role=AsyncMock(return_value=(SimpleNamespace(value="org_admin"), None)),
        check_permission=AsyncMock(),
    )
    monkeypatch.setattr(engine_commands, "AccessControl", lambda _session: access)

    class EventTable:
        def __init__(self, row):
            self.row = row

        def query(self, **_kwargs):
            return {"Items": [self.row]}

    config = EngineCommandConfig(enabled=True, table_name="events")
    for index, (repository, repository_id) in enumerate((("EXAMPLE/API", 1001), ("example/web", 1002))):
        event = envelope(
            tenant_id=org_id,
            installation_id="77",
            issue_number=42,
            repo=repository,
            repo_id=repository_id,
            event_id=f"signed-command-{index}",
            command_body="@agent-engine halt",
        )
        report = await run_engine_command_pass(db_session, config, table=EventTable(signed_row(event)))
        assert report.commands_applied == 1
        assert report.commands_refused == 0
        await db_session.flush()
        await db_session.refresh(nodes[index])
        await db_session.refresh(nodes[1 - index])
        assert nodes[index].state == NodeState.HALTED.value
        assert nodes[1 - index].state == (NodeState.RUNNING.value if index == 0 else NodeState.HALTED.value)
        assert report.pending[0].repo == repository

    decisions = (
        await db_session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.kind == DecisionKind.NODE_HALTED.value,
            )
        )
    ).all()
    assert {decision.flow_id for decision in decisions} == {node.flow_id for node in nodes}
    assert len(decisions) == 2
    assert access.check_permission.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("later_authority", ["approved", "revoked", "reused_name"])
async def test_signed_command_postcommit_ack_rechecks_the_same_repository(
    db_session,
    db_session_factory,
    monkeypatch,
    signing_keyring,
    later_authority,
):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    nodes = (await db_session.scalars(select(OrchestrationNode).order_by(OrchestrationNode.repository_identity))).all()
    for node in nodes:
        node.state = NodeState.RUNNING.value
    await db_session.flush()

    context = SimpleNamespace(user_id="authorized-human")
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(context, None)))
    access = SimpleNamespace(
        get_user_role=AsyncMock(return_value=(SimpleNamespace(value="org_admin"), None)),
        check_permission=AsyncMock(),
    )
    monkeypatch.setattr(engine_commands, "AccessControl", lambda _session: access)
    event = envelope(
        tenant_id="tenant-commands", repo="example/api", repo_id=1001, installation_id="77", issue_number=42, command_body="@agent-engine halt"
    )
    row = signed_row(event)

    class EventTable:
        consumed = False

        def query(self, **_kwargs):
            return {"Items": [row]}

        def update_item(self, **_kwargs):
            self.consumed = True

    table = EventTable()
    config = EngineCommandConfig(enabled=True, table_name="events")
    report = await run_engine_command_pass(db_session, config, table=table)
    assert report.commands_applied == 1
    await db_session.commit()
    assert nodes[0].state == NodeState.HALTED.value
    assert nodes[1].state == NodeState.RUNNING.value
    assert report.pending[0].repo_id == 1001

    async def current(_session, **kwargs):
        assert kwargs["operation"] == "command_ack"
        assert (kwargs["repository_id"], kwargs["installation_id"]) == (1001, 77)
        if later_authority == "revoked":
            raise repository_binding.RepositoryBindingError("connection_revoked")
        repository_id = 1002 if later_authority == "reused_name" else 1001
        return repository_binding.VerifiedRepository(
            "github", repository_id, kwargs["name"], 77, kwargs["name"], kwargs["issue"], kwargs["operation"]
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", current)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    credentials = AsyncMock(return_value=(123, "fixture-key"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    client = SimpleNamespace(create_issue_comment=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", lambda *_args: client)
    await flush_engine_commands(report, config, table=table)
    assert table.consumed is True
    assert report.acks_posted == (1 if later_authority == "approved" else 0)
    assert report.acks_failed == (0 if later_authority == "approved" else 1)
    if later_authority == "approved":
        assert client.create_issue_comment.await_args.kwargs["repo"] == "example/api"
        assert client.create_issue_comment.await_args.kwargs["installation_id"] == 77
        assert client.create_issue_comment.await_args.kwargs["repository_id"] == 1001
        credentials.assert_awaited_once_with("tenant-commands")
    else:
        client.create_issue_comment.assert_not_awaited()
        credentials.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name_reused", [False, True])
async def test_command_ack_transport_cannot_write_to_reused_repository_name(db_session_factory, monkeypatch, name_reused):
    from src.admin.connections.github_client import GitHubAppClient
    from src.orchestration.engine_commands import PendingEngineAck

    repository_at_name = 1001
    token_scopes = []
    writes = []

    def github(request):
        nonlocal repository_at_name
        if request.url.path == "/app/installations/77/access_tokens":
            payload = json.loads(request.content)
            token_scopes.append(payload)
            repository_at_name = 1002 if name_reused else 1001
            return httpx.Response(201, json={"token": "synthetic-scoped-token", "expires_at": "2099-01-01T00:00:00Z"})
        assert request.url.path == "/repos/example/api/issues/42/comments"
        assert request.headers["authorization"] == "token synthetic-scoped-token"
        assert json.loads(request.content) == {"body": "accepted"}
        if repository_at_name not in token_scopes[-1]["repository_ids"]:
            return httpx.Response(403, json={"message": "repository not in token scope"})
        writes.append(repository_at_name)
        return httpx.Response(201, json={"id": 123})

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    monkeypatch.setattr("src.knowledge.github_app_service._mint_app_jwt", lambda *_args: "synthetic-app-jwt")
    credentials = AsyncMock(return_value=(123, "synthetic-key"))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr(
        "src.admin.connections.github_client.GitHubAppClient",
        lambda app_id, private_key: GitHubAppClient(
            app_id, private_key, http_client=httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(github))
        ),
    )
    ack = PendingEngineAck(
        event_id="signed-event",
        arrived_at="2026-10-22T00:00:00Z",
        org_id="tenant-commands",
        repo="example/api",
        repo_id=1001,
        issue_number=42,
        installation_id=77,
        message="accepted",
    )

    if name_reused:
        with pytest.raises(httpx.HTTPStatusError) as refusal:
            await engine_commands._post_ack(ack)
        assert refusal.value.response.status_code == 403
        assert writes == []
    else:
        await engine_commands._post_ack(ack)
        assert writes == [1001]
    assert token_scopes == [{"repository_ids": [1001], "permissions": {"issues": "write", "metadata": "read"}}]
    credentials.assert_awaited_once_with("tenant-commands")


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["repository", "installation", "tampered_row"])
async def test_signed_forged_event_refused_by_production_command_pass(db_session, monkeypatch, signing_keyring, forgery):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    event = envelope(
        tenant_id="tenant-commands", repo="example/api", repo_id=1001, installation_id="77", issue_number=42, command_body="@agent-engine halt"
    )
    if forgery == "repository":
        event["repo_id"] = 1002
    elif forgery == "installation":
        event["installation_id"] = "88"
    row = signed_row(event, **({"repo": "example/web"} if forgery == "tampered_row" else {}))
    identity = AsyncMock(return_value=(object(), None))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", identity)
    monkeypatch.setattr(engine_commands, "_apply_command", applied)

    class EventTable:
        def query(self, **_kwargs):
            return {"Items": [row]}

    report = await run_engine_command_pass(db_session, EngineCommandConfig(enabled=True, table_name="events"), table=EventTable())
    assert report.commands_applied == 0
    assert report.commands_quarantined == (1 if forgery == "tampered_row" else 0)
    assert report.commands_refused == (0 if forgery == "tampered_row" else 1)
    applied.assert_not_awaited()
    assert (await db_session.scalars(select(OrchestrationDecision))).all() == []


@pytest.mark.asyncio
async def test_signed_planning_epic_selects_its_flow_with_separate_installation(db_session, monkeypatch, signing_keyring):
    base = envelope(
        tenant_id="tenant-commands", installation_id="303", issue_number=42, repo="example/planning", repo_id=3003, command_body="@agent-engine halt"
    )
    await _targets(db_session, org_id=base["tenant_id"], installation_id=77)
    flow = await db_session.scalar(select(OrchestrationFlow).where(OrchestrationFlow.slug == "example-api"))
    reference = dict(provider="github", repository_id=3003, repository="example/planning", installation_id=303, issue_number=42)
    flow.tracker_refs = [dict(scope="epic", address=f"{flow.slug}/epic", issue=reference)]
    plan = await db_session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    plan.plan_document = {**plan.plan_document, "tracker_refs": flow.tracker_refs}
    await db_session.flush()
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(object(), None)))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_apply_command", applied)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_applied == 1
    assert applied.call_args.kwargs["flow_id"] == flow.id
    assert applied.call_args.kwargs["source"] is None


@pytest.mark.asyncio
async def test_revoked_or_forged_event_refuses_before_command_mutation(db_session, monkeypatch, signing_keyring):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(object(), None)))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_apply_command", applied)
    base = envelope(
        tenant_id="tenant-commands", installation_id="77", issue_number=42, repo="example/api", repo_id=1001, command_body="@agent-engine halt"
    )

    async def revoked(_session, **kwargs):
        raise repository_binding.RepositoryBindingError("connection_revoked")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", revoked)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_refused == 1
    assert report.pending[0].installation_id is None
    applied.assert_not_awaited()

    async def wrong(_session, **kwargs):
        return repository_binding.VerifiedRepository("github", 1002, "example/web", 77, "example/api", 42, "command")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", wrong)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_refused == 1
    applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_replan_uses_c_epic_and_refuses_revocation_before_mutation(db_session, monkeypatch, signing_keyring):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    flow = await db_session.scalar(select(OrchestrationFlow).where(OrchestrationFlow.slug == "example-api"))
    reference = dict(provider="github", repository_id=3003, repository="example/planning", installation_id=303, issue_number=99)
    flow.tracker_refs = [dict(scope="epic", address=f"{flow.slug}/epic", issue=reference)]
    plan = await db_session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    plan.plan_document = {**plan.plan_document, "tracker_refs": flow.tracker_refs}
    db_session.add(
        OrchestrationDecision(
            org_id="tenant-commands",
            flow_id=flow.id,
            kind=DecisionKind.PLAN_ACCEPTED.value,
            actor_id="human",
            actor_role="org_admin",
            actor_kind="human",
        )
    )
    await db_session.flush()
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(object(), None)))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_apply_command", applied)
    base = envelope(
        tenant_id="tenant-commands",
        installation_id="77",
        issue_number=42,
        repo="example/api",
        repo_id=1001,
        command_body="@agent-engine replan: review dependencies",
    )

    async def revoked(_session, **kwargs):
        if kwargs["name"] == "example/planning":
            raise repository_binding.RepositoryBindingError("connection_revoked")
        return repository_binding.VerifiedRepository("github", 1001, "example/api", 77, "example/api", 42, "command")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", revoked)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_refused == 1
    applied.assert_not_awaited()

    async def restored(_session, **kwargs):
        return repository_binding.VerifiedRepository(
            "github", kwargs["repository_id"], kwargs["name"], kwargs["installation_id"], kwargs["name"], kwargs["issue"], kwargs["operation"]
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", restored)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_applied == 1
    assert applied.call_args.kwargs["source"] == ("example/planning", 99, 303, 3003)


@pytest.mark.asyncio
async def test_signed_planning_event_selects_single_flow_when_origin_and_tracker_match(
    db_session,
    monkeypatch,
    signing_keyring,
):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    flow = await db_session.scalar(select(OrchestrationFlow).where(OrchestrationFlow.slug == "example-api"))
    reference = dict(provider="github", repository_id=3003, repository="example/planning", installation_id=303, issue_number=99)
    flow.origin_ref = reference
    flow.tracker_refs = [dict(scope="epic", address=f"{flow.slug}/epic", issue=reference)]
    plan = await db_session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow.id))
    plan.plan_document = {**plan.plan_document, "origin_ref": reference, "tracker_refs": flow.tracker_refs}
    monkeypatch.setattr(engine_commands, "_resolve_platform_identity", AsyncMock(return_value=(object(), None)))
    applied = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_commands, "_apply_command", applied)
    base = envelope(
        tenant_id="tenant-commands", installation_id="303", issue_number=99, repo="example/planning", repo_id=3003, command_body="@agent-engine halt"
    )

    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_applied == 1
    assert applied.call_args.kwargs["flow_id"] == flow.id

    forged = envelope(
        tenant_id="tenant-commands", installation_id="77", issue_number=99, repo="example/planning", repo_id=3003, command_body="@agent-engine halt"
    )
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(forged), report, access=None)
    assert report.commands_refused == 1
    applied.assert_awaited_once()

    async def revoked(_session, **kwargs):
        raise repository_binding.RepositoryBindingError("connection_revoked")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", revoked)
    report = EngineCommandReport()
    await _handle_row(db_session, signed_row(base), report, access=None)
    assert report.commands_refused == 1
    applied.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_mismatched_and_ambiguous_bindings_refuse(db_session):
    await _targets(db_session, org_id="tenant-commands", installation_id=77)
    bound = dict(org_id="tenant-commands", issue_number=42, installation_id=77)
    assert await _resolve_target(db_session, repo="example/other", repo_id=1001, **bound) is None
    assert await _resolve_target(db_session, repo="example/api", repo_id=1002, **bound) is None
    assert await _resolve_target(db_session, repo="example/api", repo_id=1001, installation_id=88, org_id="tenant-commands", issue_number=42) is None

    duplicate = OrchestrationFlow(org_id="tenant-commands", slug="duplicate", title="Duplicate", intent_ref="42")
    db_session.add(duplicate)
    await db_session.flush()
    db_session.add(
        OrchestrationAcceptedPlan(
            org_id="tenant-commands",
            flow_id=duplicate.id,
            version=1,
            plan_hash="a" * 64,
            plan_document={
                "nodes": [
                    dict(
                        address="duplicate/epic/wave/story",
                        issue_ref="42",
                        repository_identity="example/api",
                        provider_repository_id=1001,
                        repository_connection={"provider": "github", "installation_id": 77},
                    )
                ]
            },
        )
    )
    db_session.add(
        OrchestrationNode(
            org_id="tenant-commands",
            flow_id=duplicate.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="story",
            kind="story",
            title="Duplicate",
            issue_ref="42",
            repository_identity="example/api",
            provider_repository_id=1001,
            repository_connection={"provider": "github", "installation_id": 77},
        )
    )
    await db_session.flush()
    assert await _resolve_target(db_session, repo="example/api", repo_id=1001, **bound) is None


@pytest.mark.asyncio
async def test_legacy_unbound_reference_remains_readable_but_cannot_select_signed_command(db_session):
    db_session.add(OrchestrationFlow(org_id="tenant-commands", slug="old", title="Old", intent_ref="42"))
    await db_session.flush()
    assert await _resolve_target(db_session, org_id="tenant-commands", issue_number=42) is not None
    assert await _resolve_target(db_session, org_id="tenant-commands", issue_number=42, repo="example/api", repo_id=1001, installation_id=77) is None

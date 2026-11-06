"""Retry-safe amendment issue publication (#5329) through the real binding table.

Only the provider HTTP layer is faked (a transport-level GitHub that honours `since`,
records every POST/PATCH and stamps the publishing app). Everything else — the binding
rows, the claim compare-and-swap, reconciliation — is the production path against
file-backed SQLite and PostgreSQL databases, so two sessions genuinely race.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.orchestration import amendment_issue_publisher as publisher_module
from src.orchestration.amendment_issue_publisher import (
    MAX_ISSUES_PER_REQUEST,
    AmendmentIssuePublisher,
    apply_publications,
    issue_content,
    plan_publications,
    publish_unbound_work,
)
from src.orchestration.models import OrchestrationAmendmentIssueBinding
from src.orchestration.proposal import LoopProposal, PlanningIssueReference, ProposedNode, RepositoryConnection, TrackerReference
from src.orchestration.review_cycle import CycleBlockedError
from src.shared.models.base import Base
from tests.migrations import conftest_postgres as postgres

pg_server = postgres.pg_server
pg_url = postgres.pg_url

ORG = "org-alpha"
REPO = "aws-e/adp"
APP_ID = 900
PERMITTED = frozenset({REPO})
EXISTING = {"delivery/epic/wave/existing-eval"}


def proposal(*, title="Implement cleanup checker", criteria=("cleanup-complete",), extra=0) -> LoopProposal:
    nodes = [
        ProposedNode(address="delivery/epic/wave/existing-eval", kind="eval", title="Existing eval"),
        ProposedNode(
            address="delivery/epic/wave/checker",
            kind="story",
            title=title,
            proposal_identity="delivery:cleanup-checker",
            repository_identity=REPO,
            criteria=list(criteria),
            required_inputs=[{"name": "live account", "source": "environment:prod"}],
            evidence_expectations=["verified-merge"],
        ),
    ]
    nodes.extend(
        ProposedNode(
            address=f"delivery/epic/wave/extra-{index}",
            kind="story",
            title=f"Extra {index}",
            proposal_identity=f"delivery:extra-{index}",
            repository_identity=REPO,
        )
        for index in range(extra)
    )
    return LoopProposal(flow_slug="delivery", title="Delivery", org_id=ORG, spec_revision="test", nodes=nodes, edges=[])


class Crash(BaseException):
    """A process death: no `except Exception` handler runs."""


class FakeGitHub(httpx.AsyncBaseTransport):
    """Enough of the issues API to exercise reconciliation honestly."""

    def __init__(self):
        self.issues: list[dict] = []
        self.posts = 0
        self.patches = 0
        self.link_posts = 0
        self.link_status = None
        self.timeout_after_link_post = False
        self.parents: dict[tuple[str, int], dict] = {}
        self.listings = 0
        self.post_delay = 0.0
        self.crash_after_post = False
        self.timeout_after_post = None
        self.timeout_after_patch = False
        self.write_status = None
        self.read_status = None
        self.app_receipt = {"id": APP_ID}
        self.author = {"id": 1900, "login": "fixture-app[bot]", "type": "Bot"}
        self.tokens = {}
        self.mints = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apps/fixture-app":
            return httpx.Response(200, json={"id": APP_ID, "slug": "fixture-app"})
        if path == "/users/fixture-app[bot]":
            return httpx.Response(200, json={"id": 1900, "login": "fixture-app[bot]", "type": "Bot"})
        repositories = {REPO: 99, "acme/api": 101, "acme/service": 101, "acme/web": 102, "acme/planning": 100}
        repo = next((name for name in repositories if path == f"/repos/{name}" or path.startswith(f"/repos/{name}/issues")), None)
        scope = self.tokens[request.headers["Authorization"].removeprefix("Bearer ")]["repositories"]
        if repo is not None and repo.split("/")[1] not in scope:
            return httpx.Response(404, json={"message": "Repository outside token scope"})
        if request.method in {"POST", "PATCH"} and self.write_status is not None:
            return httpx.Response(self.write_status, json={"message": "Issue write refused"})
        if request.method == "GET" and repo is not None and path.startswith(f"/repos/{repo}/issues") and self.read_status is not None:
            return httpx.Response(self.read_status, json={"message": "Issue read refused"})
        if path == f"/repos/{repo}" and repo is not None:
            return httpx.Response(200, json={"id": repositories[repo], "full_name": repo})
        if repo is not None and path.endswith("/parent") and request.method == "GET":
            number = int(path.split("/")[-2])
            parent = self.parents.get((repo, number))
            if parent and parent["repository_url"].rsplit("/", 1)[-1] not in scope:
                return httpx.Response(404, json={"message": "Private parent outside token scope"})
            return httpx.Response(200, json=parent) if parent else httpx.Response(404, json={"message": "No native parent"})
        if repo == "acme/planning" and path.endswith("/sub_issues") and request.method == "POST":
            assert path == "/repos/acme/planning/issues/42/sub_issues"
            self.link_posts += 1
            if self.link_status is not None:
                return httpx.Response(self.link_status, json={"message": "Link denied"})
            child_id = json.loads(request.content)["sub_issue_id"]
            child = next(issue for issue in self.issues if issue["id"] == child_id)
            child_repo = child["html_url"].removeprefix("https://github.com/").split("/issues/")[0]
            if child_repo.split("/")[1] not in scope:
                return httpx.Response(422, json={"message": "Child outside token scope"})
            self.parents[(child_repo, child["number"])] = {
                "id": 8000,
                "repository_url": "https://api.github.com/repos/acme/planning",
            }
            if self.timeout_after_link_post:
                raise httpx.ReadTimeout("provider linked the child but its response was lost", request=request)
            return httpx.Response(201, json=child)
        if path == f"/repos/{repo}/issues" and request.method == "GET" and repo is not None:
            self.listings += 1
            since = datetime.fromisoformat(request.url.params["since"])
            page = int(request.url.params["page"])
            rows = [
                row
                for row in self.issues
                if row["html_url"].startswith(f"https://github.com/{repo}/issues/") and datetime.fromisoformat(row["updated_at"]) >= since
            ]
            return httpx.Response(200, json=rows[(page - 1) * 100 : page * 100])
        if path == f"/repos/{repo}/issues" and request.method == "POST" and repo is not None:
            await asyncio.sleep(self.post_delay)
            self.posts += 1
            payload = json.loads(request.content)
            number = 7000 + len(self.issues)
            row = {
                "number": number,
                "id": 17000 + len(self.issues),
                "node_id": f"I_{number}",
                "title": payload["title"],
                "body": payload["body"],
                "state": "open",
                "html_url": f"https://github.com/{repo}/issues/{number}",
                "performed_via_github_app": self.app_receipt,
                "user": self.author.copy(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self.issues.append(row)
            if self.crash_after_post:
                raise Crash("process died after the provider created the issue")
            if self.posts == self.timeout_after_post:
                raise httpx.ReadTimeout("provider created the issue but its response was lost", request=request)
            return httpx.Response(201, json=row)
        if request.method == "GET" and repo is not None and path.startswith(f"/repos/{repo}/issues/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=next(row for row in self.issues if row["number"] == number))
        if request.method == "PATCH" and repo is not None:
            self.patches += 1
            number = int(path.rsplit("/", 1)[1])
            row = next(row for row in self.issues if row["number"] == number)
            payload = json.loads(request.content)
            row.update(title=payload["title"], body=payload["body"], updated_at=datetime.now(UTC).isoformat())
            if self.timeout_after_patch:
                raise httpx.ReadTimeout("provider updated the issue but its response was lost", request=request)
            return httpx.Response(200, json=row)
        raise AssertionError(f"unexpected {request.method} {request.url}")


@pytest.fixture(params=["sqlite", "postgres"])
async def factory(tmp_path, request):
    if request.param == "postgres":
        engine = create_async_engine(postgres.to_async_url(request.getfixturevalue("pg_url")))
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publish.db'}", poolclass=NullPool, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _emit_explicit_begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=55))
    monkeypatch.setattr("src.orchestration.work_admission.resolve_repository_id", AsyncMock(return_value=99))
    monkeypatch.setattr("src.orchestration.evaluation_issue_provider.resolve_tenant_app_credentials", AsyncMock(return_value=(str(APP_ID), "key")))

    async def mint(_app, _key, installation, **kwargs):
        token = f"scoped-token-{len(fake.mints)}"
        fake.mints.append({"installation": installation, **kwargs})
        fake.tokens[token] = kwargs
        return token, (datetime.now(UTC) + timedelta(minutes=10)).isoformat()

    monkeypatch.setattr("src.orchestration.evaluation_issue_provider.mint_installation_token_with_expiry", AsyncMock(side_effect=mint))
    async with httpx.AsyncClient(transport=fake) as client:
        fake.provider = AmendmentIssuePublisher(client=client)
        yield fake


async def publish(factory, github, document=None, *, permitted=PERMITTED):
    async with factory() as session:
        return await publish_unbound_work(
            session,
            org_id=ORG,
            proposal=document or proposal(),
            existing_addresses=EXISTING,
            permitted=permitted,
            publisher=github.provider,
        )


async def binding_rows(factory):
    async with factory() as session:
        return list(await session.scalars(sa.select(OrchestrationAmendmentIssueBinding)))


async def test_publication_records_a_binding_and_retries_reuse_it(factory, github):
    first = await publish(factory, github)
    second = await publish(factory, github)

    assert [plan.issue_number for plan in first] == [plan.issue_number for plan in second] == [7000]
    assert github.posts == 1
    rows = await binding_rows(factory)
    assert [(row.state, row.issue_number, row.provider_repository_id) for row in rows] == [("created", 7000, 99)]
    assert rows[0].claim_token not in github.issues[0]["body"]
    assert publisher_module.observed_content(github.issues[0])[1] == publisher_module.write_identity(rows[0].claim_token)


async def test_null_app_receipt_publishes_and_recovers_same_native_child(factory, github, monkeypatch):
    authorize_product_epic(monkeypatch)
    github.app_receipt = None
    github.crash_after_post = True
    document = product_epic_proposal()
    permitted = frozenset({"acme/api", "acme/web"})
    with pytest.raises(Crash):
        await publish(factory, github, document, permitted=permitted)
    assert (github.posts, github.link_posts) == (1, 0)
    assert (await binding_rows(factory))[0].state == "writing"
    github.crash_after_post = False
    recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
    recovered = await publish(factory, github, document, permitted=permitted)
    repeated = await publish(factory, github, document, permitted=permitted)
    assert [plan.issue_number for plan in recovered] == [plan.issue_number for plan in repeated] == [7000, 7001]
    assert (github.posts, github.patches, github.link_posts) == (2, 0, 2)
    assert all(row.state == "created" and row.link_confirmed_at for row in await binding_rows(factory))


async def test_product_stories_publish_to_their_own_verified_connections(factory, github, monkeypatch):
    from src.admin.installations.resolver import OwnerState

    async def owner(installation_id, *, db):
        assert installation_id in {22, 33} and db is not None
        return SimpleNamespace(tenant_id=ORG), OwnerState.RESOLVED

    monkeypatch.setattr(publisher_module, "resolve_installation_owner", owner)
    from src.orchestration import work_admission

    monkeypatch.setattr(
        work_admission,
        "resolve_repository_id",
        AsyncMock(
            side_effect=lambda *, org_id, installation_id, repo: {
                ("acme/api", 22): 101,
                ("acme/web", 33): 102,
            }[(repo, installation_id)]
        ),
    )
    document = proposal().model_copy(
        update={
            "product_id": "product-1",
            "nodes": [
                ProposedNode(
                    address=f"delivery/epic/wave/{name}",
                    kind="story",
                    title=f"Build {name}",
                    proposal_identity=f"delivery:{name}",
                    repository_identity=f"acme/{name}",
                    provider_repository_id=repo_id,
                    repository_connection=RepositoryConnection(provider="github", installation_id=installation),
                )
                for name, repo_id, installation in [("api", 101, 22), ("web", 102, 33)]
            ],
        }
    )

    first = await publish(factory, github, document, permitted=frozenset({"acme/api", "acme/web"}))
    second = await publish(factory, github, document, permitted=frozenset({"acme/api", "acme/web"}))

    assert [(item.repository, item.provider_repository_id, item.issue_number) for item in first] == [("acme/api", 101, 7000), ("acme/web", 102, 7001)]
    assert [(item.repository, item.issue_number) for item in second] == [("acme/api", 7000), ("acme/web", 7001)]
    assert [issue["html_url"] for issue in github.issues] == ["https://github.com/acme/api/issues/7000", "https://github.com/acme/web/issues/7001"]
    assert [(row.repository, row.provider_repository_id) for row in await binding_rows(factory)] == [("acme/api", 101), ("acme/web", 102)]
    assert document.product_id == LoopProposal.model_validate(document.model_dump()).product_id == "product-1"


async def test_product_target_identity_mismatch_refuses_before_issue_create(factory, github, monkeypatch):
    from src.admin.installations.resolver import OwnerState

    monkeypatch.setattr(publisher_module, "resolve_installation_owner", AsyncMock(return_value=(SimpleNamespace(tenant_id=ORG), OwnerState.RESOLVED)))
    document = proposal().model_copy(
        update={
            "product_id": "product-1",
            "nodes": [
                ProposedNode(
                    address="delivery/epic/wave/checker",
                    kind="story",
                    title="Checker",
                    proposal_identity="delivery:cleanup-checker",
                    repository_identity=REPO,
                    provider_repository_id=123,
                    repository_connection=RepositoryConnection(provider="github", installation_id=55),
                )
            ],
        }
    )

    with pytest.raises(CycleBlockedError, match="amendment_issue_repository_changed"):
        await publish(factory, github, document)
    assert github.posts == 0
    assert await binding_rows(factory) == []


def product_epic_proposal() -> LoopProposal:
    epic = PlanningIssueReference(provider="github", repository_id=100, repository="acme/planning", installation_id=11, issue_number=42)
    return proposal().model_copy(
        update={
            "product_id": "product-1",
            "origin_ref": epic,
            "tracker_refs": [TrackerReference(scope="epic", address="delivery/epic", issue=epic)],
            "nodes": [
                ProposedNode(
                    address=f"delivery/epic/wave/{name}",
                    kind="story",
                    title=f"Build {name}",
                    proposal_identity=f"delivery:{name}",
                    repository_identity=f"acme/{name}",
                    provider_repository_id=repo_id,
                    repository_connection=RepositoryConnection(provider="github", installation_id=installation),
                )
                for name, repo_id, installation in [("api", 101, 22), ("web", 102, 33)]
            ],
        }
    )


def authorize_product_epic(monkeypatch):
    from src.admin.installations.resolver import OwnerState
    from src.orchestration import repository_binding, work_admission

    async def owner(_installation_id, *, db):
        assert db is not None
        return SimpleNamespace(tenant_id=ORG), OwnerState.RESOLVED

    async def epic(_session, *, tenant_id, name, issue, repository_id, installation_id, operation):
        assert (tenant_id, name, issue, repository_id, installation_id) == (ORG, "acme/planning", 42, 100, 11)
        return repository_binding.VerifiedRepository("github", 100, name, 11, name, 42, operation, 8000)

    monkeypatch.setattr(publisher_module, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "resolve_story_repository", epic)
    monkeypatch.setattr(
        work_admission,
        "resolve_repository_id",
        AsyncMock(
            side_effect=lambda *, org_id, installation_id, repo: {
                ("acme/api", 22): 101,
                ("acme/web", 33): 102,
            }[(repo, installation_id)]
        ),
    )


async def test_product_epic_issues_link_to_verified_parent_before_becoming_usable(factory, github, monkeypatch):
    authorize_product_epic(monkeypatch)
    document = product_epic_proposal()
    permitted = frozenset({"acme/api", "acme/web"})

    first = await publish(factory, github, document, permitted=permitted)
    assert [(plan.repository, plan.parent_repository, plan.parent_issue_number) for plan in first] == [
        ("acme/api", "acme/planning", 42),
        ("acme/web", "acme/planning", 42),
    ]
    assert (github.posts, github.link_posts) == (2, 2)
    assert {tuple(m["repositories"]) for m in github.mints} == {("api",), ("web",), ("api", "planning"), ("planning", "web")}
    assert all(m["permissions"] in ({"issues": "read", "metadata": "read"}, {"issues": "write", "metadata": "read"}) for m in github.mints)
    assert github.parents == {
        ("acme/api", 7000): {"id": 8000, "repository_url": "https://api.github.com/repos/acme/planning"},
        ("acme/web", 7001): {"id": 8000, "repository_url": "https://api.github.com/repos/acme/planning"},
    }
    rows = await binding_rows(factory)
    assert all(row.parent_repository_id == 100 and row.parent_issue_id == 8000 and row.link_confirmed_at is not None for row in rows)
    async with factory() as session:
        plans, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=document,
            existing_addresses=EXISTING,
            permitted=permitted,
        )
    assert not violations and [plan.action for plan in plans] == ["reuse", "reuse"]
    assert [(node.repository_identity, node.issue_ref) for node in apply_publications(document, plans).nodes] == [
        ("acme/api", "7000"),
        ("acme/web", "7001"),
    ]
    assert [plan.issue_number for plan in await publish(factory, github, document, permitted=permitted)] == [7000, 7001]
    assert (github.posts, github.link_posts) == (2, 2)
    assert document.product_id == LoopProposal.model_validate(document.model_dump()).product_id


@pytest.mark.parametrize("link_pending", [False, True])
async def test_verified_repository_rename_reuses_saved_issue_and_parent_progress(factory, github, monkeypatch, link_pending):
    authorize_product_epic(monkeypatch)
    document = product_epic_proposal()
    permitted = frozenset({"acme/api", "acme/web"})
    if link_pending:
        github.link_status = 403
        with pytest.raises(CycleBlockedError, match="amendment_issue_link_unconfirmed"):
            await publish(factory, github, document, permitted=permitted)
        github.link_status = None
    else:
        await publish(factory, github, document, permitted=permitted)

    original = next(row for row in await binding_rows(factory) if row.proposal_identity == "delivery:api")
    saved_identity = (original.id, original.provider_repository_id, original.issue_number, original.repository)
    assert saved_identity[1:] == (101, 7000, "acme/api")

    node = document.nodes[0].model_copy(update={"repository_identity": "acme/service"})
    renamed = document.model_copy(update={"nodes": [node, *document.nodes[1:]]})
    github.issues[0]["html_url"] = "https://github.com/acme/service/issues/7000"
    if not link_pending:
        github.parents[("acme/service", 7000)] = github.parents.pop(("acme/api", 7000))
    from src.orchestration import repository_binding, work_admission

    original_resolver = repository_binding.resolve_story_repository

    async def verified_rename(session, *, tenant_id, name, issue, repository_id, installation_id, operation):
        if name == "acme/planning":
            return await original_resolver(
                session,
                tenant_id=tenant_id,
                name=name,
                issue=issue,
                repository_id=repository_id,
                installation_id=installation_id,
                operation=operation,
            )
        assert (tenant_id, name, issue, repository_id, installation_id) == (ORG, "acme/service", 7000, 101, 22)
        return repository_binding.VerifiedRepository("github", 101, name, 22, name, issue, operation, 17000)

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified_rename)
    monkeypatch.setattr(
        work_admission,
        "resolve_repository_id",
        AsyncMock(
            side_effect=lambda *, org_id, installation_id, repo: {
                ("acme/service", 22): 101,
                ("acme/web", 33): 102,
            }[(repo, installation_id)]
        ),
    )
    permitted = frozenset({"acme/service", "acme/web"})
    async with factory() as session:
        plans, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=renamed,
            existing_addresses=EXISTING,
            permitted=permitted,
        )
    assert not violations
    assert [(plan.action, plan.provider_repository_id, plan.issue_number) for plan in plans] == [
        ("pending_link" if link_pending else "reuse", 101, 7000),
        ("create" if link_pending else "reuse", 102, None if link_pending else 7001),
    ]
    published = await publish(factory, github, renamed, permitted=permitted)
    assert [(plan.repository, plan.issue_number) for plan in published] == [
        ("acme/service", 7000),
        ("acme/web", 7001),
    ]
    assert (github.posts, github.link_posts) == (2, 2 if not link_pending else 3)
    rows = await binding_rows(factory)
    assert len(rows) == 2
    assert all(row.parent_issue_id == 8000 and row.link_confirmed_at is not None for row in rows)
    saved = {row.proposal_identity: row for row in rows}
    api = saved["delivery:api"]
    assert (api.id, api.provider_repository_id, api.issue_number, api.repository) == saved_identity
    assert (saved["delivery:web"].provider_repository_id, saved["delivery:web"].issue_number) == (102, 7001)

    async with factory() as session:
        recovered, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=renamed,
            existing_addresses=EXISTING,
            permitted=permitted,
        )
    assert not violations
    assert [(plan.action, plan.repository, plan.provider_repository_id, plan.issue_number) for plan in recovered] == [
        ("reuse", "acme/service", 101, 7000),
        ("reuse", "acme/web", 102, 7001),
    ]
    bound = apply_publications(renamed, recovered)
    assert [(node.repository_identity, node.provider_repository_id, node.issue_ref) for node in bound.nodes] == [
        ("acme/service", 101, "7000"),
        ("acme/web", 102, "7001"),
    ]
    again = await publish(factory, github, renamed, permitted=permitted)
    assert [(plan.repository, plan.issue_number) for plan in again] == [("acme/service", 7000), ("acme/web", 7001)]
    assert (github.posts, github.link_posts) == (2, 2 if not link_pending else 3)


async def test_pending_native_link_never_binds_an_unconfirmed_issue(factory, github, monkeypatch):
    authorize_product_epic(monkeypatch)
    document = product_epic_proposal()
    github.link_status = 403
    with pytest.raises(CycleBlockedError):
        await publish(factory, github, document, permitted=frozenset({"acme/api", "acme/web"}))
    assert (github.posts, github.link_posts) == (1, 1)
    async with factory() as session:
        plans, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=document,
            existing_addresses=EXISTING,
            permitted=frozenset({"acme/api", "acme/web"}),
        )
    assert not violations
    assert plans[0].action == "pending_link" and plans[0].issue_number == 7000
    assert all(node.issue_ref is None for node in apply_publications(document, plans).nodes)


async def test_pending_link_rejects_resumed_numeric_target_change_without_a_new_issue(factory, github, monkeypatch):
    authorize_product_epic(monkeypatch)
    document = product_epic_proposal()
    permitted = frozenset({"acme/api", "acme/web"})
    github.link_status = 403
    with pytest.raises(CycleBlockedError, match="amendment_issue_link_unconfirmed"):
        await publish(factory, github, document, permitted=permitted)
    row = (await binding_rows(factory))[0]
    assert (row.provider_repository_id, row.issue_number, row.link_confirmed_at) == (101, 7000, None)
    github.link_status = None

    changed_node = document.nodes[0].model_copy(update={"provider_repository_id": 102})
    changed = document.model_copy(update={"nodes": [changed_node, *document.nodes[1:]]})
    async with factory() as session:
        plans, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=changed,
            existing_addresses=EXISTING,
            permitted=permitted,
        )
    assert any(violation.rule == "amendment_issue_repository_changed" for violation in violations)
    assert plans[0].issue_number == 7000
    with pytest.raises(CycleBlockedError, match="amendment_issue_repository_changed"):
        await publish(factory, github, changed, permitted=permitted)
    assert (github.posts, github.link_posts) == (1, 1)
    assert len(github.issues) == 1
    saved = (await binding_rows(factory))[0]
    assert (saved.id, saved.provider_repository_id, saved.issue_number, saved.link_confirmed_at) == (row.id, 101, 7000, None)

    assert [plan.issue_number for plan in await publish(factory, github, document, permitted=permitted)] == [7000, 7001]
    assert (github.posts, github.link_posts) == (2, 3)


async def test_lost_native_link_ack_reuses_created_issue_and_observes_parent(factory, github, monkeypatch):
    authorize_product_epic(monkeypatch)
    document = product_epic_proposal()
    permitted = frozenset({"acme/api", "acme/web"})
    github.timeout_after_link_post = True

    with pytest.raises(CycleBlockedError, match="amendment_issue_link_unconfirmed"):
        await publish(factory, github, document, permitted=permitted)
    assert (github.posts, github.link_posts) == (1, 1)
    assert github.parents == {
        ("acme/api", 7000): {"id": 8000, "repository_url": "https://api.github.com/repos/acme/planning"},
    }
    async with factory() as session:
        plans, violations = await plan_publications(
            session,
            org_id=ORG,
            proposal=document,
            existing_addresses=EXISTING,
            permitted=permitted,
        )
    assert not violations
    assert [(plan.action, plan.issue_number) for plan in plans] == [("pending_link", 7000), ("create", None)]
    assert all(node.issue_ref is None for node in apply_publications(document, plans).nodes)
    row = (await binding_rows(factory))[0]
    assert (row.state, row.issue_number, row.provider_repository_id, row.parent_issue_id, row.link_confirmed_at) == (
        "created",
        7000,
        101,
        8000,
        None,
    )

    github.timeout_after_link_post = False
    result = await publish(factory, github, document, permitted=permitted)
    assert [(plan.repository, plan.issue_number) for plan in result] == [("acme/api", 7000), ("acme/web", 7001)]
    assert (github.posts, github.link_posts) == (2, 2)
    assert len(github.issues) == 2
    rows = await binding_rows(factory)
    assert all(row.state == "created" and row.parent_issue_id == 8000 and row.link_confirmed_at is not None for row in rows)
    assert [(plan.repository, plan.issue_number) for plan in await publish(factory, github, document, permitted=permitted)] == [
        ("acme/api", 7000),
        ("acme/web", 7001),
    ]
    assert (github.posts, github.link_posts) == (2, 2)


async def test_content_drift_after_publication_updates_the_same_issue(factory, github):
    """The old reconciler demanded an exact title/body match and failed every retry."""
    await publish(factory, github)

    drifted = proposal(title="Implement the cleanup checker (renamed)", criteria=("cleanup-complete", "no-orphans"))
    result = await publish(factory, github, drifted)

    assert [plan.issue_number for plan in result] == [7000]
    assert (github.posts, github.patches) == (1, 1)
    assert github.issues[0]["title"] == "Implement the cleanup checker (renamed)"
    assert "no-orphans" in github.issues[0]["body"]
    await publish(factory, github, drifted)
    assert github.patches == 1, "recorded content is not rewritten again"


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("status", [400, 408, 409, 418, 500, 502, 503])
async def test_unclassified_write_failures_still_require_observed_completion(factory, github, monkeypatch, operation, status):
    if operation == "update":
        await publish(factory, github)
    document = proposal(criteria=("revised-criterion",))
    github.write_status = status
    with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
        await publish(factory, github, document)
    before = (await binding_rows(factory))[0]
    assert before.state == "writing"

    github.write_status = None
    recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
    for _attempt in range(2):
        with pytest.raises(CycleBlockedError, match="amendment_issue_write_uncertain"):
            await publish(factory, github, document)
    after = (await binding_rows(factory))[0]
    assert (after.state, after.claim_token) == ("writing", before.claim_token)
    assert (github.posts, github.patches) == (int(operation == "update"), 0)


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_rejected_recovery_read_cannot_release_an_uncertain_write(factory, github, monkeypatch, operation):
    if operation == "update":
        await publish(factory, github)
        github.timeout_after_patch = True
    else:
        github.timeout_after_post = 1
    document = proposal(criteria=("recovered-criterion",))
    with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
        await publish(factory, github, document)
    before = (await binding_rows(factory))[0]
    assert before.state == "writing"

    recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
    github.read_status = 403
    with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
        await publish(factory, github, document)
    after = (await binding_rows(factory))[0]
    assert (after.state, after.claim_token) == ("writing", before.claim_token)

    github.read_status = None
    assert [plan.issue_number for plan in await publish(factory, github, document)] == [7000]
    assert [row.state for row in await binding_rows(factory)] == ["created"]
    assert (github.posts, github.patches) == (1, int(operation == "update"))


async def test_crash_after_provider_create_reconciles_on_restart_without_a_second_issue(factory, github):
    github.crash_after_post = True
    with pytest.raises(Crash):
        await publish(factory, github)
    github.crash_after_post = False
    assert github.posts == 1
    assert [row.state for row in await binding_rows(factory)] == ["writing"]

    # Within the lease a dead claimant is indistinguishable from a slow one.
    with pytest.raises(CycleBlockedError, match="amendment_issue_publication_in_progress"):
        await publish(factory, github)
    assert github.posts == 1

    async with factory() as session:
        await session.execute(sa.update(OrchestrationAmendmentIssueBinding).values(claimed_at=datetime.now(UTC) - timedelta(minutes=10)))
        await session.commit()

    restarted = await publish(factory, github)

    assert [plan.issue_number for plan in restarted] == [7000]
    assert github.posts == 1
    assert [(row.state, row.issue_number) for row in await binding_rows(factory)] == [("created", 7000)]


async def test_concurrent_retries_create_exactly_one_issue(factory, github):
    github.post_delay = 0.2
    outcomes = await asyncio.gather(publish(factory, github), publish(factory, github), return_exceptions=True)

    assert github.posts == 1
    succeeded = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert succeeded and all(plans[0].issue_number == 7000 for plans in succeeded)
    assert all(isinstance(error, CycleBlockedError) and str(error) == "amendment_issue_publication_in_progress" for error in refused), refused
    assert len(await binding_rows(factory)) == 1
    assert [plan.issue_number for plan in await publish(factory, github)] == [7000]
    assert github.posts == 1


async def test_partial_publication_with_lost_response_recovers_without_duplicate_issues(factory, github, monkeypatch):
    document = proposal(extra=2)
    github.timeout_after_post = 2

    with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
        await publish(factory, github, document)

    rows = await binding_rows(factory)
    assert sorted(row.state for row in rows) == ["created", "writing"]
    assert github.posts == 2
    with pytest.raises(CycleBlockedError, match="amendment_issue_publication_in_progress"):
        await publish(factory, github, document)
    assert github.posts == 2

    recovered_at = datetime.now(UTC) + publisher_module.SETTLE_AFTER_FAILURE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
    recovered = await publish(factory, github, document)
    repeated = await publish(factory, github, document)

    assert [plan.issue_number for plan in recovered] == [plan.issue_number for plan in repeated] == [7000, 7001, 7002]
    assert (github.posts, github.patches) == (3, 0)
    assert len(await binding_rows(factory)) == 3


async def test_lost_update_response_retries_the_recorded_issue(factory, github, monkeypatch):
    await publish(factory, github)
    changed = proposal(title="Updated cleanup checker")
    github.timeout_after_patch = True

    with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
        await publish(factory, github, changed)

    github.timeout_after_patch = False
    with pytest.raises(CycleBlockedError, match="amendment_issue_publication_in_progress"):
        await publish(factory, github)
    recovered_at = publisher_module._now() + publisher_module.SETTLE_AFTER_FAILURE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
    result = await publish(factory, github, changed)
    await publish(factory, github, changed)

    assert [plan.issue_number for plan in result] == [7000]
    assert (github.posts, github.patches) == (1, 1)
    assert len(await binding_rows(factory)) == 1


async def test_concurrent_updates_serialize_content_and_recorded_hash(factory, github, monkeypatch):
    await publish(factory, github)
    first = proposal(criteria=("criteria-a",))
    second = proposal(criteria=("criteria-b",))
    entered, resume = asyncio.Event(), asyncio.Event()
    update_issue = github.provider.update_issue

    async def delayed_update(binding, number, content):
        issue = await update_issue(binding, number, content)
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
        return issue

    monkeypatch.setattr(github.provider, "update_issue", delayed_update)
    pending = asyncio.create_task(publish(factory, github, first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        for document in (second, first, proposal()):
            with pytest.raises(CycleBlockedError, match="amendment_issue_publication_in_progress"):
                await publish(factory, github, document)
        row = (await binding_rows(factory))[0]
        assert (row.state, row.issue_number) == ("writing", 7000)
        assert github.patches == 1
    finally:
        resume.set()
        await asyncio.wait_for(pending, timeout=10)

    for document in (second, first):
        await publish(factory, github, document)
        row = (await binding_rows(factory))[0]
        content = issue_content(document.nodes[1])
        assert row.state == "created"
        assert row.content_hash == publisher_module.content_hash(content)
        assert publisher_module.observed_content(github.issues[0])[0]["body"] == content["body"]
    assert (github.posts, github.patches, github.listings) == (1, 3, 0)


@pytest.mark.parametrize("pause_at", ["before_mutation", "after_mutation"])
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_expired_update_cannot_complete_another_claim(factory, github, monkeypatch, pause_at, operation):
    if operation == "update":
        await publish(factory, github)
    first = proposal(criteria=("criteria-a",))
    second = proposal(criteria=("criteria-b",))
    entered, resume = asyncio.Event(), asyncio.Event()
    method_name = f"{operation}_issue"
    write_issue = getattr(github.provider, method_name)

    async def delayed_write(*args, **kwargs):
        pause = not entered.is_set()
        if pause and pause_at == "before_mutation":
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
        issue = await write_issue(*args, **kwargs)
        if pause and pause_at == "after_mutation":
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
        return issue

    monkeypatch.setattr(github.provider, method_name, delayed_write)
    pending = asyncio.create_task(publish(factory, github, first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
        monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
        await publish(factory, github, second)
    finally:
        resume.set()
        with pytest.raises(CycleBlockedError, match="amendment_issue_publication_conflict"):
            await asyncio.wait_for(pending, timeout=10)
    row = (await binding_rows(factory))[0]
    content = issue_content(second.nodes[1])
    assert row.state == "created"
    assert row.content_hash == publisher_module.content_hash(content)
    assert publisher_module.observed_content(github.issues[0])[0]["body"] == content["body"]
    await publish(factory, github, second)
    assert github.posts == 1
    assert github.patches == int(operation == "update") + int(pause_at == "after_mutation")
    assert github.listings == int(operation == "create")


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_uncertain_write_cannot_be_reassigned_before_it_is_observed(factory, github, monkeypatch, operation):
    if operation == "update":
        await publish(factory, github)
    first = proposal(criteria=("criteria-a",))
    second = proposal(criteria=("criteria-b",))
    entered, resume = asyncio.Event(), asyncio.Event()
    request = github.provider.request

    async def delayed_request(binding, method, path, **kwargs):
        if method == ("POST" if operation == "create" else "PATCH") and not entered.is_set():
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
        return await request(binding, method, path, **kwargs)

    monkeypatch.setattr(github.provider, "request", delayed_request)
    pending = asyncio.create_task(publish(factory, github, first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
        monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
        for document in (second, proposal()):
            with pytest.raises(CycleBlockedError, match="amendment_issue_write_uncertain"):
                await publish(factory, github, document)
        assert (await binding_rows(factory))[0].state == "writing"
        assert (github.posts, github.patches) == (int(operation == "update"), 0)
    finally:
        resume.set()
        await asyncio.wait_for(pending, timeout=10)
    await publish(factory, github, second)
    row = (await binding_rows(factory))[0]
    assert row.state == "created"
    assert row.content_hash == publisher_module.content_hash(issue_content(second.nodes[1]))
    assert "criteria-b" in github.issues[0]["body"] and "criteria-a" not in github.issues[0]["body"]
    assert (github.posts, github.patches) == (1, 1 + int(operation == "update"))


@pytest.mark.parametrize("refusal", ["ambiguous", "listing_incomplete"])
async def test_uncertain_reconciliation_refuses_to_create_again(factory, github, monkeypatch, refusal):
    github.crash_after_post = True
    with pytest.raises(Crash):
        await publish(factory, github)
    github.crash_after_post = False
    original = github.issues[0]
    if refusal == "ambiguous":
        github.issues.append({**original, "number": 7001, "html_url": f"https://github.com/{REPO}/issues/7001"})
    else:
        monkeypatch.setattr(publisher_module, "LISTING_PAGES", 1)
        github.issues.extend({**original, "number": number, "body": "Unrelated issue"} for number in range(7100, 7199))
    recovered_at = datetime.now(UTC) + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
    monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)

    with pytest.raises(CycleBlockedError, match=f"amendment_issue_{refusal}"):
        await publish(factory, github)

    assert (github.posts, github.patches) == (1, 0)
    assert [row.state for row in await binding_rows(factory)] == ["writing"]
    github.issues = [original]
    recovered_at += publisher_module.SETTLE_AFTER_FAILURE + timedelta(seconds=1)
    result = await publish(factory, github)
    assert [plan.issue_number for plan in result] == [7000]
    assert github.posts == 1


async def test_recorded_issue_outside_any_listing_window_is_reused_without_listing(factory, github):
    await publish(factory, github)
    github.issues[0]["updated_at"] = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    github.listings = 0

    again = await publish(factory, github)

    assert [plan.issue_number for plan in again] == [7000]
    assert (github.listings, github.posts) == (0, 1)


async def test_recovery_reconciles_by_marker_and_app_identity_only(factory, github):
    """A stale pending row, an issue by another app carrying the marker, and ours."""
    created = datetime.now(UTC) - timedelta(days=30)
    async with factory() as session:
        session.add(
            OrchestrationAmendmentIssueBinding(
                org_id=ORG,
                repository=REPO,
                proposal_identity="delivery:cleanup-checker",
                state="pending",
                claim_token="dead",
                claimed_at=created,
                content_hash="0" * 64,
                created_at=created,
            )
        )
        await session.commit()
    marker = issue_content(proposal().nodes[1])["marker"]
    for number, app in ((6998, 1), (6999, APP_ID)):
        github.issues.append(
            {
                "number": number,
                "title": "A title that no longer matches",
                "body": marker + "\n\nstale body",
                "html_url": f"https://github.com/{REPO}/issues/{number}",
                "performed_via_github_app": {"id": app},
                "updated_at": (created + timedelta(minutes=1)).isoformat(),
            }
        )

    result = await publish(factory, github)

    assert [plan.issue_number for plan in result] == [6999]
    assert github.posts == 0
    assert github.patches == 1, "reconciled content is refreshed in place"


async def test_unpermitted_repository_is_refused_before_any_provider_call(factory, github):
    with pytest.raises(CycleBlockedError, match="amendment_issue_repository_not_permitted"):
        await publish(factory, github, permitted=frozenset({"aws-e/other"}))
    assert github.posts == 0
    assert await binding_rows(factory) == []


async def test_one_request_may_create_at_most_the_cap(factory, github):
    async with factory() as session:
        plans, violations = await plan_publications(
            session, org_id=ORG, proposal=proposal(extra=MAX_ISSUES_PER_REQUEST), existing_addresses=EXISTING, permitted=PERMITTED
        )
        assert len(plans) == MAX_ISSUES_PER_REQUEST + 1
        assert [violation.rule for violation in violations] == ["amendment_issue_limit_exceeded"]
        _, violations = await plan_publications(
            session, org_id=ORG, proposal=proposal(extra=MAX_ISSUES_PER_REQUEST - 1), existing_addresses=EXISTING, permitted=PERMITTED
        )
        assert violations == []
    with pytest.raises(CycleBlockedError, match="amendment_issue_limit_exceeded"):
        await publish(factory, github, proposal(extra=MAX_ISSUES_PER_REQUEST))
    assert github.posts == 0
    assert MAX_ISSUES_PER_REQUEST == 20


async def test_recorded_bindings_apply_deterministically(factory, github):
    await publish(factory, github)
    async with factory() as session:
        plans, _ = await plan_publications(session, org_id=ORG, proposal=proposal(), existing_addresses=EXISTING, permitted=PERMITTED)
    bound = apply_publications(proposal(), plans)

    assert [plan.action for plan in plans] == ["reuse"]
    assert (bound.nodes[1].issue_ref, bound.nodes[1].provider_repository_id) == ("7000", 99)
    assert apply_publications(proposal(), plans) == bound


def test_public_issue_body_omits_required_input_sources():
    content = issue_content(proposal().nodes[1])

    assert "environment:prod" not in content["body"]
    assert "credentials are intentionally omitted" in content["body"]


def test_claim_lease_exceeds_the_provider_timeout():
    assert publisher_module.CLAIM_LEASE > timedelta(seconds=15)

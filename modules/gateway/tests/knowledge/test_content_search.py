"""Content search preserves real registry ownership and current membership."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, text
from starlette.responses import Response

from src.knowledge import routes, search
from src.knowledge import search_billing as billing
from src.products.models import ProductProjectBinding, Project
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import UserIdentity
from tests.knowledge.test_product_postgres import A, B, create, pg_server, pg_url, registry  # noqa: F401
from tests.knowledge.test_product_postgres import actor as original_actor


def actor(*args, **kwargs):
    user = original_actor(*args, **kwargs)
    user.expires_at = datetime.now(UTC) + timedelta(hours=1)
    return user


@pytest.mark.parametrize("cap_kind", ["user", "person"])
async def test_search_respects_existing_login_subject_budgets_before_door(content, monkeypatch, cap_kind):
    from decimal import Decimal

    from src.budget import enforcement_service
    from src.budget.config import BudgetConfig as BudgetFeatures
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.budget.utils import get_period_start_end
    from src.shared.identity import resolve_user_entity_id
    from src.shared.models.budget import BudgetConfig, BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
    from src.shared.schemas.budget import PeriodType

    storage = content[0]
    async with storage.engine.begin() as conn:
        for model in (BudgetConfig, BudgetUsage, PersonBudgetConfig, PersonBudgetDefault):
            await conn.run_sync(lambda sync, model=model: model.__table__.create(sync))
    async with storage.sessions() as db:
        subject = await resolve_user_entity_id(db, "org-a", "member")
        assert subject == "login-member" and subject != "member"
        db.add(
            BudgetUsage(
                org_id="org-a",
                entity_type="user",
                entity_id=subject,
                period_type="monthly",
                period_start=get_period_start_end(PeriodType.MONTHLY)[0],
                total_cost_usd=Decimal("1"),
                total_tokens=1000,
                request_count=1,
            )
        )
        if cap_kind == "user":
            db.add(
                BudgetConfig(
                    org_id="org-a",
                    entity_type="user",
                    entity_id=subject,
                    period_type="monthly",
                    budget_amount_usd=Decimal("1"),
                    enforcement_mode="hard",
                )
            )
        else:
            db.add(
                PersonBudgetConfig(
                    person_anchor="users:member",
                    period_type="monthly",
                    budget_amount_usd=Decimal("1"),
                    enforcement_mode="hard",
                    authored_by_user_id="member",
                )
            )
        await db.commit()
    config = BudgetFeatures()
    object.__setattr__(config, "budget_check_enabled", True)
    monkeypatch.setattr(enforcement_service, "budget_config", config)
    monkeypatch.setattr(search, "admit_search", billing.admit_search)
    monkeypatch.setattr(billing, "get_accounting_session_factory", lambda: storage.sessions)
    monkeypatch.setattr(billing, "require_ordinary_tool_identity", AsyncMock())
    monkeypatch.setattr(
        billing, "get_rate_limit_service", lambda request: Mock(consume_tool_rate_limit=AsyncMock(return_value=SimpleNamespace(allowed=True)))
    )
    activity = Mock()
    monkeypatch.setattr(billing, "ActivityService", lambda **kwargs: activity)
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "search-test")
    async with storage.sessions() as db:
        import fakeredis.aioredis

        from src.budget.reservations import ReservationStore

        service = BudgetEnforcementService(
            db_session=db, reservations=ReservationStore(None, 120, client=fakeredis.aioredis.FakeRedis(decode_responses=True))
        )
        monkeypatch.setattr(billing, "BudgetEnforcementService", lambda: service)
        with pytest.raises(HTTPException) as error:
            await call(content)
    assert error.value.status_code == 429
    assert error.value.detail == "Context search budget is exhausted"
    content[3].assert_not_awaited()
    activity.record_tool_call.assert_not_called()


@pytest.mark.parametrize("value", ["", "not-a-url", "https://door.test/unsafe-path"])
async def test_invalid_door_configuration_creates_no_budget_hold(content, monkeypatch, value):
    monkeypatch.setenv("ADP_DOOR_SERVICE_URL", value)
    with pytest.raises(HTTPException) as error:
        await call(content)
    assert error.value.status_code == 503
    search.admit_search.assert_not_awaited()
    search.finalize_search.assert_not_awaited()
    content[3].assert_not_awaited()


@pytest.fixture
async def content(registry, monkeypatch):  # noqa: F811
    await registry.upgrade()
    async with registry.engine.begin() as conn:
        await conn.execute(text("UPDATE team_memberships SET is_primary=true"))
    async with registry.engine.begin() as conn:
        for model in (Project, ProductProjectBinding):
            await conn.run_sync(lambda sync, model=model: model.__table__.create(sync))
    async with registry.sessions() as db:
        for product_id in (A, B):
            db.add(Project(tenant_id="org-a", id="base-" + product_id, name="Base", owner_user_id="owner"))
        await db.flush()
        for product_id in (A, B):
            db.add(ProductProjectBinding(tenant_id="org-a", product_id=product_id, project_id="base-" + product_id, is_base=True, provenance={}))
        await db.commit()
    monkeypatch.setattr(search, "admit_search", AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(dispatched=False, result=None)))
    monkeypatch.setattr(search, "finalize_search", AsyncMock(return_value=True))
    async with registry.engine.begin() as conn:
        await conn.run_sync(lambda c: UserIdentity.__table__.create(c))
    assets = [
        await create(registry),
        await create(registry, scope="personal"),
        await create(registry, uid="owner", product=B),
        await create(registry, uid="peer", scope="personal"),
    ]
    hits = []
    async with registry.sessions() as db:
        for asset in assets:
            attempt = str(uuid.uuid4())
            await db.execute(
                text("UPDATE knowledge_assets SET status='complete', ingestion_attempt_id=:attempt WHERE id=:id"),
                {"attempt": attempt, "id": asset.id},
            )
            hits.append(
                {
                    "asset_id": asset.id,
                    "ingestion_attempt_id": attempt,
                    "product_id": asset.product_id or "",
                    "tenant_id": asset.tenant_id,
                    "owner_sub": asset.owner_sub or "",
                    "visibility": "product" if asset.product_id else "personal",
                    "chunk_text": "The release uses a staged deployment.",
                    "section_heading": "Deployment",
                }
            )
        await db.commit()
    signed = []
    monkeypatch.setattr(search, "sign_door_identity", lambda **kwargs: signed.append(kwargs) or "signed-test-identity")
    monkeypatch.setenv("ADP_DOOR_SERVICE_URL", "http://context-mcp:5100")
    forward = AsyncMock(return_value=Response(json.dumps({"results": hits}), media_type="application/json"))
    monkeypatch.setattr(search, "forward_door", forward)
    return registry, assets, hits, forward, signed


async def call(content, *, scope="product", product_id=A, who=None):
    async with content[0].sessions() as db, content[0].sessions() as gateway:
        return await routes.search_assets(
            search.KnowledgeSearchRequest(query="How is the service deployed?", scope=scope, product_id=product_id),
            db,
            gateway,
            who or actor(),
            Response(),
            SimpleNamespace(),
        )


async def test_product_and_personal_results_cannot_mix_even_if_reader_returns_other_scopes(content):
    _, assets, _, forward, signed = content
    result = await call(content)
    assert [hit.asset.id for hit in result.items] == [assets[0].id]
    scope = search.admit_search.await_args.args[1]._project_attribution
    assert (scope.product_id, scope.project_id) == (A, "base-" + A)
    assert result.items[0].excerpt == "The release uses a staged deployment."
    assert result.items[0].heading == "Deployment"
    assert signed[-1]["identity"]["x-product-id"] == A
    assert json.loads(signed[-1]["body"])["arguments"] == {"query": "How is the service deployed?", "scope": "docs", "product_id": A, "limit": 20}
    assert forward.await_args.args[:2] == ("POST", "http://context-mcp:5100/call")
    personal = await call(content, scope="personal", product_id=None)
    assert search.admit_search.await_args.args[1]._project_attribution is None
    assert [hit.asset.id for hit in personal.items] == [assets[1].id]
    assert "x-product-id" not in signed[-1]["identity"]
    assert json.loads(signed[-1]["body"])["arguments"]["scope"] == "personal"


async def test_nonmember_denied_before_search_and_platform_admin_remains_tenant_bound(content):
    forward = content[3]
    for user in (actor("owner"), actor("outsider"), actor("member", admin=True, org="org-b")):
        with pytest.raises(HTTPException) as error:
            await call(content, who=user)
        assert error.value.status_code == 404
    forward.assert_not_awaited()
    assert [hit.asset.id for hit in (await call(content, who=actor("outsider", admin=True))).items] == [content[1][0].id]


async def test_membership_revocation_applies_to_the_next_query(content):
    assert len((await call(content)).items) == 1
    async with content[0].sessions() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "member"))
        await db.commit()
    content[3].reset_mock()
    with pytest.raises(HTTPException) as error:
        await call(content)
    assert error.value.status_code == 404
    content[3].assert_not_awaited()


@pytest.mark.parametrize("change", ["removed", "indexing", "new_generation"])
async def test_stale_or_removed_results_are_dropped_after_the_vector_request(content, change):
    async def forward(*args):
        async with content[0].sessions() as db:
            await db.execute(
                text("UPDATE knowledge_assets SET status=:status, ingestion_attempt_id=:attempt WHERE id=:id"),
                {
                    "id": content[1][0].id,
                    "status": "complete" if change == "new_generation" else change,
                    "attempt": str(uuid.uuid4()) if change == "new_generation" else content[2][0]["ingestion_attempt_id"],
                },
            )
            await db.commit()
        return Response(json.dumps({"results": content[2]}), media_type="application/json")

    content[3].side_effect = forward
    assert (await call(content)).items == []


@pytest.mark.parametrize(
    "response",
    [
        {"error": "private endpoint internal error", "results": []},
        {"status": "not_ready"},
        {"results": [{"asset_id": "not-a-uuid"}]},
        {"results": "bad"},
    ],
)
async def test_reader_errors_are_unavailable_not_empty_success(content, response):
    content[3].return_value = Response(json.dumps(response), media_type="application/json")
    with pytest.raises(HTTPException) as error:
        await call(content)
    assert error.value.status_code == 503
    assert error.value.detail == "Content search is unavailable. Please try again."


@pytest.mark.parametrize(
    "values",
    [
        {"query": " "},
        {"query": "x" * 4097},
        {"query": "q", "limit": 21},
        {"query": "q", "limit": True},
        {"query": "q", "scope": "product"},
        {"query": "q", "product_id": A},
        {"query": "q", "scope": "tenant"},
        {"query": "q", "scope": "product", "product_id": "invalid"},
        {"query": "q", "owner_sub": "someone-else"},
        {"query": "q", "project": "legacy"},
    ],
)
def test_search_request_is_bounded_and_does_not_accept_caller_authority(values):
    with pytest.raises(ValidationError):
        search.KnowledgeSearchRequest(**values)


async def test_http_search_requires_auth_and_preserves_errors_and_no_store(content):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(routes._router)

    async def database():
        async with content[0].sessions() as db:
            yield db

    async def denied():
        raise HTTPException(401, "Authentication required")

    app.dependency_overrides[routes.get_agent_context_db] = database
    app.dependency_overrides[routes.get_db] = database
    app.dependency_overrides[routes.get_current_user] = denied
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway.test") as client:
        body = {"scope": "product", "product_id": A, "query": "release process"}
        assert (await client.post("/agent-context/assets/search", json=body)).status_code == 401
        content[3].assert_not_awaited()
        app.dependency_overrides[routes.get_current_user] = lambda: actor()
        response = await client.post("/agent-context/assets/search", json=body)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["asset"]["id"] == content[1][0].id
        assert (await client.post("/agent-context/assets/search", json={**body, "limit": 500})).status_code == 422


async def test_search_uses_the_real_signed_call_contract(content, monkeypatch):
    from src.agentauth.door_identity import sign_door_identity
    from tests.agentauth.test_knowledge_service import PUBLIC_KEYS, SIGNING_ENV, verify_identity

    subject = "11111111-1111-1111-1111-111111111111"
    async with content[0].sessions() as db:
        await db.execute(text("UPDATE users SET cognito_sub=:subject WHERE id='member'"), {"subject": subject})
        await db.commit()
    monkeypatch.setattr(search, "sign_door_identity", sign_door_identity)
    for key, value in SIGNING_ENV.items():
        monkeypatch.setenv(key, value)

    async def verify(method, url, headers, body):
        claims = verify_identity(headers["x-adp-door-identity"], PUBLIC_KEYS, method=method, path="/call", body=body)
        assert claims["tenant_id"] == "org-a"
        assert claims["product_id"] == A
        assert claims["asset_owner_id"] == "member"
        return Response(json.dumps({"results": content[2]}), media_type="application/json")

    content[3].side_effect = verify
    current = actor()
    current.user_id = subject
    assert (await call(content, who=current)).items[0].asset.id == content[1][0].id


@pytest.mark.parametrize("scope", ["personal", "product"])
async def test_partial_ingestion_with_a_live_search_generation_remains_searchable(content, scope):
    asset = content[1][0 if scope == "product" else 1]
    async with content[0].sessions() as db:
        await db.execute(text("UPDATE knowledge_assets SET status='partial' WHERE id=:id"), {"id": asset.id})
        await db.commit()
    result = await call(content, scope=scope, product_id=A if scope == "product" else None)
    assert [hit.asset.id for hit in result.items] == [asset.id]
    assert result.items[0].asset.status == "partial"


async def test_scope_limit_preserves_an_actionable_permanent_error(content):
    content[3].return_value = Response(
        json.dumps(
            {
                "error": "private implementation detail",
                "code": "knowledge_scope_limit",
                "results": [],
                "total": 0,
            }
        ),
        media_type="application/json",
    )
    with pytest.raises(HTTPException) as error:
        await call(content)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "knowledge_scope_limit"
    assert "too many indexed sources" in error.value.detail["message"]
    assert "private implementation detail" not in str(error.value.detail)

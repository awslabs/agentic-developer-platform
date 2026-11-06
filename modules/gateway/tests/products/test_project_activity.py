"""Shared activity requires attribution to the selected Project on every read."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete

from src.activity import project_routes
from src.activity.schemas import InvocationChainItem, InvocationChainResponse, InvocationItem
from src.auth.dependencies import get_current_user
from src.orchestration.models import OrchestrationFlow, OrchestrationNode
from src.orchestration.run_reports import OrchestrationRunReport
from src.products.models import ProductProjectBinding, Project, ProjectRun
from src.shared.models.organization import TeamMembership, User
from tests.products import test_project_budgets as budget_fixtures

product = budget_fixtures.product


@pytest.fixture
async def activity(app, db_session, source_member, product):
    other = Project(id="other-project", tenant_id=source_member.org_id, name="Other", owner_user_id=source_member.user_id)
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        ProductProjectBinding(tenant_id=source_member.org_id, product_id=product["product_id"], project_id=other.id, is_base=False, provenance={})
    )
    await db_session.commit()
    items = {}
    service = Mock()
    service.get_invocation.side_effect = lambda run_id, **kwargs: items.get(run_id)
    app.dependency_overrides[project_routes.get_service] = lambda: service

    async def add(run_id, *, project_id=None, day=1, **fields):
        db_session.add(
            ProjectRun(
                tenant_id=source_member.org_id,
                run_id=run_id,
                product_id=product["product_id"],
                project_id=project_id or product["project_id"],
                owner_user_id="another-owner",
                relationship_revision=1,
                created_at=datetime(2026, 10, day, tzinfo=UTC),
            )
        )
        await db_session.commit()
        items[run_id] = InvocationItem(
            invocation_id=run_id, invoked_at=f"2026-10-{day:02d}T12:00:00Z", status="complete", persona="developer", **fields
        )
        return items[run_id]

    url = f"/v1/products/{product['product_id']}/projects/{product['project_id']}/agent-invocations"
    return SimpleNamespace(add=add, items=items, service=service, url=url, product=product, other=other.id)


async def test_team_member_sees_other_owners_runs_but_no_sibling_project(test_client, activity):
    await activity.add("mine")
    await activity.add("foreign", project_id=activity.other)
    result = await test_client.get(activity.url)
    assert result.status_code == 200, result.text
    assert [item["invocation_id"] for item in result.json()["items"]] == ["mine"]
    assert result.headers["cache-control"] == "no-store"
    assert [call.args[0] for call in activity.service.get_invocation.call_args_list] == ["mine"]
    assert (await test_client.get(activity.url + "/foreign")).status_code == 404
    assert (await test_client.get(activity.url + "/foreign/transcript")).status_code == 404


async def test_detail_and_transcript_recheck_membership(test_client, app, db_session, source_member, activity, monkeypatch):
    await activity.add("run", transcript_key="retained/run.md")
    fetch = AsyncMock(return_value="Recorded implementation and results")
    monkeypatch.setattr("src.activity.routes._fetch_transcript", fetch)
    assert (await test_client.get(activity.url + "/run")).status_code == 200
    transcript = await test_client.get(activity.url + "/run/transcript")
    assert transcript.status_code == 200 and transcript.text == "Recorded implementation and results"
    fetch.assert_awaited_once_with("retained/run.md")
    teammate = source_member.model_copy(update={"user_id": "teammate"})
    db_session.add(User(id="teammate", org_id=source_member.org_id, team_id=source_member.team_id, email="team@example.test", user_kind="human"))
    await db_session.flush()
    db_session.add(TeamMembership(user_id="teammate", org_id=source_member.org_id, team_id=source_member.team_id, is_primary=True))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: teammate
    assert (await test_client.get(activity.url + "/run")).status_code == 200
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == "teammate"))
    await db_session.commit()
    for suffix in ("", "/run", "/run/transcript", "/chain/chain"):
        assert (await test_client.get(activity.url + suffix)).status_code == 404
    fetch.assert_awaited_once()


@pytest.mark.parametrize("boundary", ["product", "tenant", "service", "unbound"])
async def test_scope_boundaries_fail_closed(test_client, app, source_member, activity, boundary):
    await activity.add("run")
    url = activity.url
    if boundary == "product":
        url = url.replace(activity.product["product_id"], "wrong-product")
    elif boundary == "tenant":
        app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update={"org_id": "foreign"})
    elif boundary == "service":
        app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update={"account_type": "service"})
    else:
        activity.items["unbound"] = activity.items["run"].model_copy(update={"invocation_id": "unbound"})
        url += "/unbound"
    result = await test_client.get(url)
    assert result.status_code in (403, 404), result.text
    activity.service.get_invocation.assert_not_called()


async def test_filters_and_cursor_do_not_skip_project_runs(test_client, activity):
    for i in range(1, 6):
        await activity.add(f"run-{i}", day=i, channel="github" if i % 2 else "api")
    params = {"page_size": 2, "channel": "github"}
    first = (await test_client.get(activity.url, params=params)).json()
    assert [item["invocation_id"] for item in first["items"]] == ["run-5", "run-3"]
    assert first["last_key"]
    second = (await test_client.get(activity.url, params={**params, "last_key": first["last_key"]})).json()
    assert [item["invocation_id"] for item in second["items"]] == ["run-1"]
    assert second["last_key"] is None
    assert (await test_client.get(activity.url, params={"last_key": first["last_key"], "channel": "api"})).status_code == 422
    sibling = activity.url.replace(activity.product["project_id"], activity.other)
    assert (await test_client.get(sibling, params={**params, "last_key": first["last_key"]})).status_code == 422


@pytest.mark.parametrize("cursor", ["bad", "e30=", "bnVsbA==", "WyJ4IiwgInkiLCAieiJd"])
async def test_invalid_cursors_have_actionable_errors(test_client, activity, cursor):
    result = await test_client.get(activity.url, params={"last_key": cursor})
    assert result.status_code == 422
    assert "first page" in result.text


async def test_chain_membership_never_grants_project_access(test_client, activity):
    await activity.add("child", correlation_id="chain")
    await activity.add("sibling", project_id=activity.other, correlation_id="chain")
    activity.service.get_chain.return_value = InvocationChainResponse(
        correlation_id="chain",
        total_count=3,
        items=[
            InvocationChainItem(
                invocation_id="outside",
                invoked_at="2026-10-01T00:00:00Z",
                children=[
                    InvocationChainItem(invocation_id="child", parent_invocation_id="outside", invoked_at="2026-10-01T01:00:00Z"),
                    InvocationChainItem(invocation_id="sibling", parent_invocation_id="outside", invoked_at="2026-10-01T02:00:00Z"),
                ],
            )
        ],
    )
    result = await test_client.get(activity.url + "/chain/chain")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["total_count"] == 1 and body["items"][0]["invocation_id"] == "child"
    assert body["items"][0]["parent_invocation_id"] is None
    assert "outside" not in result.text and "sibling" not in result.text


async def test_grouped_page_and_date_filter_preserve_actual_runs(test_client, activity):
    await activity.add("one", day=1, correlation_id="chain")
    await activity.add("two", day=2, correlation_id="chain")
    grouped = await test_client.get(activity.url, params={"view": "chains"})
    assert grouped.status_code == 200, grouped.text
    chain = grouped.json()["chains"][0]
    assert chain["root"]["invocation_id"] == "one" and chain["descendant_count"] == 1
    result = (await test_client.get(activity.url, params={"since": "2026-10-02", "until": "2026-10-02"})).json()
    assert [item["invocation_id"] for item in result["items"]] == ["two"]


async def test_dispatch_before_metering_and_immutable_attribution_precedence(test_client, db_session, source_member, activity):
    flow = OrchestrationFlow(
        id="flow",
        org_id=source_member.org_id,
        slug="flow",
        title="Flow",
        product_id=activity.product["product_id"],
        project_id=activity.product["project_id"],
    )
    db_session.add(flow)
    await db_session.flush()
    db_session.add(
        OrchestrationNode(
            id="node",
            org_id=flow.org_id,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="node",
            kind="story",
            state="running",
            title="Node",
        )
    )
    await db_session.flush()
    for name in ("early-failure", "conflict"):
        db_session.add(
            OrchestrationRunReport(
                run_id=name,
                credential_hash=name,
                org_id=flow.org_id,
                flow_id=flow.id,
                node_id="node",
                attempt=1,
                persona="operations",
                repo="example/test",
                installation_id=1,
                dispatch_metadata={},
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        activity.items[name] = InvocationItem(invocation_id=name, invoked_at="2026-11-01T00:00:00Z", status="failed")
    await db_session.commit()
    await activity.add("conflict", project_id=activity.other)
    result = (await test_client.get(activity.url)).json()
    assert [item["invocation_id"] for item in result["items"]] == ["early-failure"]


async def test_cost_enrichment_never_combines_tenants(test_client, db_session, source_member, activity):
    from decimal import Decimal

    from src.shared.models.usage import UsageLog

    await activity.add("same-run", correlation_id="same-chain")
    for tenant, amount in ((source_member.org_id, "2.00"), ("other-tenant", "97.00")):
        db_session.add(
            UsageLog(
                org_id=tenant,
                agent_run_id="same-run",
                department_id="department",
                team_id="team",
                user_id="user",
                model="model",
                input_tokens=10,
                output_tokens=5,
                cost_usd=Decimal(amount),
                latency_ms=1,
                status_code=200,
            )
        )
    await db_session.commit()
    listing = (await test_client.get(activity.url)).json()
    assert listing["items"][0]["total_cost_usd"] == 2
    detail = (await test_client.get(activity.url + "/same-run")).json()
    assert detail["total_cost_usd"] == 2
    activity.service.get_chain.return_value = InvocationChainResponse(
        correlation_id="same-chain", total_count=1, items=[InvocationChainItem(invocation_id="same-run", invoked_at="2026-10-01T00:00:00Z")]
    )
    chain = (await test_client.get(activity.url + "/chain/same-chain")).json()
    assert chain["chain_total_cost_usd"] == 2


async def test_project_stream_reauthorizes_reader_without_granting_commands(test_client, db_session, source_member, activity, monkeypatch):
    from contextlib import asynccontextmanager

    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse

    from src.agentauth.bootstrap import BootstrapRefusedError

    await activity.add("run", root_human_id="another-owner")

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session)
    monkeypatch.setattr("src.activity.routes.get_control_service", lambda: object())

    async def open_stream(control, run_id, *, session, reauthorize, target_owner_id, cursor):
        assert run_id == "run"
        assert session.user_id == source_member.user_id
        assert target_owner_id == "another-owner"
        await reauthorize()
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
        await db_session.commit()
        with pytest.raises((HTTPException, BootstrapRefusedError)):
            await reauthorize()
        return PlainTextResponse("read-only stream")

    monkeypatch.setattr("src.activity.explanation_stream.open_explanation_stream", open_stream)
    result = await test_client.get(activity.url + "/run/events")
    assert result.status_code == 200, result.text
    assert result.text == "read-only stream"
    assert (await test_client.post(activity.url + "/run/abort")).status_code in (404, 405)

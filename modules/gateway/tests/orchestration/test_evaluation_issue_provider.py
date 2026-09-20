"""Provider-shaped correction correlation; no live GitHub calls."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.evaluation_issue_provider import EvaluationIssueProvider, issue_content
from src.orchestration.review_cycle import CycleBlockedError


@pytest.fixture
async def issues(monkeypatch):
    ctx = SimpleNamespace(rows=[], posts=0, lose_response=False, authorized=False)
    ctx.binding = SimpleNamespace(org_id="org-a", repo="org/repo", provider_repository_id=123, installation_id=42)
    ctx.content = issue_content(
        operation_key="operation",
        evaluation_id="evaluation",
        cycle=1,
        failed_criteria=["API-1"],
        actual_revision="a" * 40,
        evidence_ref="github/actions/runs/1/artifacts/2",
        source_issue=43,
    )
    ctx.resolve = AsyncMock(return_value=("900", "private-test-key"))
    ctx.mint = AsyncMock(return_value=("scoped-token", (datetime.now(UTC) + timedelta(minutes=10)).isoformat()))
    monkeypatch.setattr("src.orchestration.evaluation_issue_provider.resolve_tenant_app_credentials", ctx.resolve)
    monkeypatch.setattr("src.orchestration.evaluation_issue_provider.mint_installation_token_with_expiry", ctx.mint)

    def response(request):
        assert request.headers["Authorization"] == "Bearer scoped-token"
        if request.url.path == "/repos/org/repo":
            return httpx.Response(200, json={"id": 123, "full_name": "org/repo"})
        if request.method == "POST":
            assert ctx.authorized
            ctx.posts += 1
            row = dict(
                number=71,
                node_id="I_correction",
                title=ctx.content["title"],
                body=ctx.content["body"],
                state="open",
                html_url="https://github.com/org/repo/issues/71",
                performed_via_github_app={"id": 900},
            )
            ctx.rows.append(row)
            if ctx.lose_response:
                raise httpx.ReadTimeout("response lost after remote creation")
            return httpx.Response(201, json=row)
        if request.url.path == "/repos/org/repo/issues/71":
            return httpx.Response(200, json=ctx.rows[0])
        if request.url.path == "/repos/org/repo/issues":
            return httpx.Response(200, json=ctx.rows)
        raise AssertionError(request.url)

    async def authorize():
        ctx.authorized = True

    ctx.authorize = AsyncMock(side_effect=authorize)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        ctx.provider = EvaluationIssueProvider(client=client)
        yield ctx


async def test_create_uses_only_scoped_issue_permission_after_authorization(issues):
    ctx = issues
    issue = await ctx.provider.create(ctx.binding, ctx.content, reauthorize=ctx.authorize)
    assert issue.number == 71 and issue.repository_id == 123
    ctx.authorize.assert_awaited_once()
    assert ctx.mint.await_args.kwargs["permissions"] == {"issues": "write", "metadata": "read"}
    assert ctx.mint.await_args.kwargs["repositories"] == ["repo"]
    assert "claim" not in ctx.content["body"]


async def test_lost_creation_response_is_observed_by_exact_correlation(issues):
    ctx = issues
    ctx.lose_response = True
    with pytest.raises(httpx.ReadTimeout):
        await ctx.provider.create(ctx.binding, ctx.content, reauthorize=ctx.authorize)
    found = await ctx.provider.find(ctx.binding, ctx.content, since=datetime.now(UTC))
    assert found.number == 71 and ctx.posts == 1
    assert ctx.mint.await_args.kwargs["permissions"] == {"issues": "read", "metadata": "read"}


@pytest.mark.parametrize("fault", ["app", "body", "title", "number", "url", "pr", "duplicate"])
async def test_foreign_or_edited_correlation_cannot_adopt_a_correction(issues, fault):
    ctx = issues
    await ctx.provider.create(ctx.binding, ctx.content, reauthorize=ctx.authorize)
    row = ctx.rows[0]
    if fault == "app":
        row["performed_via_github_app"] = {"id": 901}
    elif fault == "body":
        row["body"] += "\nchanged scope"
    elif fault == "title":
        row["title"] = "Unrelated work"
    elif fault == "number":
        row["number"] = True
    elif fault == "url":
        row["html_url"] = "https://github.com/foreign/repo/issues/71"
    elif fault == "pr":
        row["pull_request"] = {"url": "pull/71"}
    else:
        ctx.rows.append(dict(row, number=72, html_url="https://github.com/org/repo/issues/72"))
    with pytest.raises(CycleBlockedError):
        await ctx.provider.find(ctx.binding, ctx.content, since=datetime.now(UTC))


async def test_unrelated_issues_never_supply_a_receipt(issues):
    issues.rows = [{"number": 1, "body": "not this correction"}]
    assert await issues.provider.find(issues.binding, issues.content, since=datetime.now(UTC)) is None


async def test_unbounded_history_refuses_instead_of_assuming_absence(issues):
    issues.rows = [{"number": index, "body": "unrelated"} for index in range(100)]
    with pytest.raises(CycleBlockedError, match="history_limit"):
        await issues.provider.find(issues.binding, issues.content, since=datetime.now(UTC))


async def test_reauthorization_refusal_prevents_issue_creation(issues):
    issues.authorize.side_effect = CycleBlockedError("revoked")
    with pytest.raises(CycleBlockedError, match="revoked"):
        await issues.provider.create(issues.binding, issues.content, reauthorize=issues.authorize)
    assert issues.posts == 0


async def test_expired_credential_cannot_fall_back_to_platform_token(issues):
    issues.mint.return_value = ("scoped-token", (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(CycleBlockedError, match="credential_unavailable"):
        await issues.provider.find(issues.binding, issues.content, since=datetime.now(UTC))
    assert issues.posts == 0


def test_provider_timeout_waits_without_losing_the_current_phase():
    from datetime import UTC, datetime

    from src.orchestration.evaluation_controller import EvaluationController
    from src.orchestration.execution_runner import DecisionKind, HandlerObservation, ObservationKind

    controller = EvaluationController(None, services=SimpleNamespace())
    result = controller.decide(SimpleNamespace(now=datetime.now(UTC)), HandlerObservation(ObservationKind.UNCERTAIN, detail="provider timeout"))
    assert result.kind is DecisionKind.WAIT and result.next_check_at is not None

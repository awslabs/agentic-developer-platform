"""A report credential grants reviewer identity only for its current REVIEW action."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.agentauth.shared_review_identity import verify_shared_review_worker
from src.orchestration.models import OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_review_cycle import review, tick
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def review_identity(shared, monkeypatch):  # noqa: F811
    assert (await tick(shared)).effects_succeeded == 1
    envelope = shared.calls[-1]
    monkeypatch.setattr("src.agentauth.shared_review_identity.get_session_factory", lambda: shared.factory)
    monkeypatch.setattr("src.agentauth.routes.require_agent_transport", AsyncMock())
    async with shared.factory() as db:
        row = await db.get(OrchestrationRunReport, envelope["message_id"])
        row.worker_receipt = {"recorded_at": "2026-01-01T00:00:00Z"}
        await db.commit()
    return shared


def request(ctx, **overrides):
    envelope = ctx.calls[-1]
    owner, repo = ctx.binding.repo.split("/")
    body = dict(identity="review", invocation_id=envelope["message_id"], installation_id=42, repo_owner=owner, repo_name=repo)
    body.update(overrides)

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/v1/github-installation-token",
            "scheme": "https",
            "server": ("test", 443),
            "query_string": b"",
            "headers": [(b"x-adp-report-credential", envelope["run_report"]["credential"].encode())],
        },
        receive,
    )


async def test_authenticated_review_gets_read_only_code_and_formal_review_permission(review_identity):
    req = request(review_identity)
    await verify_shared_review_worker(req)
    assert req.state.agent_authorized_action.value == "review"
    assert req.state.agent_installation_binding.tenant_id == review_identity.node.org_id
    assert req.state.agent_github_permissions == {"contents": "read", "pull_requests": "write", "metadata": "read"}


@pytest.mark.parametrize(
    "field,value", [("invocation_id", "other"), ("installation_id", 999), ("repo_owner", "foreign"), ("repo_name", "other"), ("identity", "default")]
)
async def test_requested_body_cannot_redirect_authenticated_review(review_identity, field, value):
    with pytest.raises(HTTPException):
        await verify_shared_review_worker(request(review_identity, **{field: value}))


@pytest.mark.parametrize("change", ["claim", "terminal", "unstarted"])
async def test_stale_or_unstarted_report_cannot_mint_reviewer_identity(review_identity, change):
    ctx = review_identity
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, ctx.calls[-1]["message_id"])
        if change == "claim":
            (await db.get(OrchestrationWorkClaim, ctx.claim.id)).active_run_id = "other"
        elif change == "terminal":
            row.terminal_receipt = {"outcome": "complete"}
        else:
            row.worker_receipt = None
        await db.commit()
    with pytest.raises(HTTPException):
        await verify_shared_review_worker(request(ctx))


async def test_repair_action_cannot_assume_review_identity(review_identity):
    ctx = review_identity
    await review(ctx, findings=[{"summary": "fix", "finding_id": "F1", "evidence_refs": []}])
    assert (await tick(ctx)).effects_succeeded == 1
    assert ctx.calls[-1]["review_cycle_input"]["action"] == "repair"
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, ctx.calls[-1]["message_id"])
        row.worker_receipt = {"started": True}
        await db.commit()
    with pytest.raises(HTTPException):
        await verify_shared_review_worker(request(ctx))


@pytest.mark.parametrize("displace_during_mint", [False, True])
async def test_shared_identity_route_revalidates_and_revokes_after_claim_change(review_identity, monkeypatch, displace_during_mint):
    from datetime import UTC, datetime, timedelta

    from fastapi import Response

    from src.internal import routes
    from src.internal.routes import GithubInstallationTokenRequest

    ctx = review_identity
    req = request(ctx)
    monkeypatch.setattr(routes, "assert_installation_owned_by", AsyncMock())
    monkeypatch.setattr(routes, "resolve_reviewer_app_credentials", AsyncMock(return_value=("review-app", "test-key", 900)))
    monkeypatch.setattr(routes, "_write_audit", AsyncMock())
    revoke = AsyncMock()
    monkeypatch.setattr(routes, "_revoke_undelivered_github_token", revoke)

    async def mint(*args, **kwargs):
        assert args[2] == 900
        assert kwargs["repositories"] == [ctx.binding.repo.split("/")[1]]
        assert kwargs["permissions"] == {"contents": "read", "pull_requests": "write", "metadata": "read"}
        if displace_during_mint:
            async with ctx.factory() as db:
                (await db.get(OrchestrationWorkClaim, ctx.claim.id)).active_run_id = "new-owner"
                await db.commit()
        return "test-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    monkeypatch.setattr(routes, "mint_installation_token_with_expiry", mint)
    async with ctx.factory() as db:
        body = GithubInstallationTokenRequest(**await req.json())
        if displace_during_mint:
            with pytest.raises(HTTPException):
                await routes.github_installation_token(body, req, Response(), db, None)
            revoke.assert_awaited_once_with("test-token")
        else:
            result = await routes.github_installation_token(body, req, Response(), db, None)
            assert result.identity == "review"
            revoke.assert_not_awaited()

"""Fresh review recovery remains an attributed human control, never agent self-approval."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.admin.config import Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.orchestration import controls
from src.orchestration.review_recovery import ReviewRecoveryRequest


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("failure", ["permission", "service"])
async def test_recovery_requires_human_plan_approver_before_any_work(monkeypatch, accept, failure):
    access = SimpleNamespace(check_permission=AsyncMock(side_effect=HTTPException(403, "denied") if failure == "permission" else None))
    human = AsyncMock(side_effect=BootstrapRefusedError("service identity"))
    monkeypatch.setattr("src.agentauth.human_control.authorize_human_session", human)
    recover = AsyncMock()
    monkeypatch.setattr(controls, "request_review_recovery", recover)
    user = SimpleNamespace(org_id="org")
    endpoint = controls.accept_review_recovery if accept else controls.preview_review_recovery
    with pytest.raises(HTTPException) as exc:
        await endpoint(node_id="node", body=None, current_user=user, access=access, db=None)
    assert exc.value.status_code == 403
    access.check_permission.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    recover.assert_not_awaited()
    if failure == "permission":
        human.assert_not_awaited()


@pytest.mark.parametrize("accept", [False, True])
async def test_recovery_uses_authenticated_tenant_actor_and_preview_writes_nothing(monkeypatch, accept):
    access = SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"),)))
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session", AsyncMock(return_value=SimpleNamespace(tenant_id="org", user_id="human"))
    )
    recover = AsyncMock(return_value={"snapshot": "a" * 64})
    monkeypatch.setattr(controls, "request_review_recovery", recover)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    endpoint = controls.accept_review_recovery if accept else controls.preview_review_recovery
    body = SimpleNamespace()
    await endpoint(node_id="node", body=body, current_user=SimpleNamespace(org_id="org"), access=access, db=db)
    assert recover.call_args.kwargs == dict(org_id="org", node_id="node", actor_id="human", actor_role="owner", request=body, accept=accept)
    assert db.commit.await_count == int(accept)
    assert db.rollback.await_count == int(not accept)


def test_recovery_body_cannot_assert_actor_or_approval():
    with pytest.raises(ValidationError):
        ReviewRecoveryRequest(
            expected_attempt=1,
            expected_plan_version=1,
            expected_run_id="run",
            expected_head_sha="a" * 40,
            reason="Recover the exited reviewer",
            actor_kind="human",
            approved=True,
        )

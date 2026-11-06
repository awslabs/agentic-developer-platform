"""Exercise the installed executor boundary without contacting paid providers."""

from contextlib import asynccontextmanager
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from l5_services.adapters.finops.console_executor import ConsoleFinOpsExecutor
from l5_services.adapters.finops import executor_host
from l5_services.adapters.finops.executor_host import build_executor, dispatch, verify_executor_work
from l5_services.models.run import Run
from l5_services.models.work import Work


@pytest.mark.parametrize("failure_at", ("login", "observation", "close"))
def test_host_browser_errors_do_not_expose_signin_material(monkeypatch, failure_at):
    import sys
    import traceback
    from l5_services.adapters.finops.console_executor import PlaywrightFinOpsBrowser

    secret = "synthetic-signin-token-never-log"
    page = SimpleNamespace(goto=AsyncMock())
    context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    if failure_at == "login":
        page.goto.side_effect = TimeoutError(f"page.goto: https://signin.aws.amazon.com/?SigninToken={secret}")
    if failure_at == "close":
        browser.close.side_effect = RuntimeError(f"browser context failed: {secret}")

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=playwright))
    selected = PlaywrightFinOpsBrowser()
    monkeypatch.setattr(selected, "_signin_token", lambda url: secret)

    async def observe(**kwargs):
        async with selected.session({"access_key_id": "example", "secret_access_key": "example",
                                     "session_token": "example", "native_agent_id": "agent-example"}, {}):
            if failure_at == "observation":
                raise RuntimeError(f"navigation call log contains cookie {secret}")

    monkeypatch.setattr(executor_host, "build_executor", lambda: SimpleNamespace(observe=observe))
    event = {"schema_version": 1, "action": "observe", "intent": None, "native_job_ref": {},
             "authority": {"work_kind": "poll", "operation_id": "run-example", "attempt_id": "work-example"}}
    with pytest.raises(ConnectionError, match="private FinOps console operation unavailable") as failure:
        executor_host.lambda_handler(event, None)
    visible = "".join(traceback.format_exception(failure.value))
    assert secret not in visible and "SigninToken" not in visible
    assert failure.value.__suppress_context__ is True
    browser.close.assert_awaited_once()


def fixture():
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    connection = {"record_type": "credential", "record_id": "credential-example"}
    authority = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example",
                 "operation_id": "00000000-0000-0000-0000-000000000001", "attempt_id": "work-example",
                 "lease_owner": "owner-example", "lease_fence": 2, "work_generation": 3, "work_kind": "reconcile", "run_revision": 4,
                 "delegation_id": "delegation-example", "delegation_revision": 5,
                 "connection_ref": connection, "provider_identity": identity}
    work = SimpleNamespace(run_id=authority["operation_id"], generation=3, fence=2, kind="reconcile",
                           lease_owner="owner-example", completed_at=None, blocked_at=None,
                           lease_until=datetime.now(UTC) + timedelta(minutes=2))
    run = SimpleNamespace(run_id=authority["operation_id"], service_id="finops", revision=4,
                          tenant_id="tenant-example", product_id="product-example", project_id="project-example",
                          admission={"native_agent_ref": {"record_type": "finops_agent", "record_id": "agent-example",
                                          "tenant_id": "tenant-example", "project_id": "project-example"},
                                     "delegation_id": "delegation-example", "delegation_version": 5,
                                     "connection_ref": {"record_id": "credential-example"},
                                     "provider_identity": identity})
    class Session:
        async def get(self, model, key):
            assert key[0] == authority["tenant_id"]
            return {Work: work, Run: run}[model]

    @asynccontextmanager
    async def sessions():
        yield Session()

    result = {"operation_id": authority["operation_id"], "attempt_id": authority["attempt_id"],
              "delegation_revision": 5, "provider_identity": identity,
              "access_key_id": "not-a-real-key", "secret_access_key": "not-a-real-secret",
              "session_token": "not-a-real-token", "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}
    transport = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: result)))
    return authority, run, work, sessions, transport, result


async def test_current_work_rechecked_against_database_and_broker():
    authority, run, work, sessions, transport, result = fixture()
    verified = await verify_executor_work(authority, sessions, transport)
    assert verified["provider_identity"] == authority["provider_identity"]
    assert verified["lease_fence"] == 2 and verified["native_agent_id"] == "agent-example"
    assert transport.post.await_args.args == ("/internal/v1/l5/credential-session",)
    for attribute, replacement in (("fence", 3), ("lease_until", datetime.now(UTC) - timedelta(seconds=1)),
                                   ("completed_at", datetime.now(UTC))):
        original = getattr(work, attribute)
        setattr(work, attribute, replacement)
        with pytest.raises(PermissionError, match="current scope"):
            await verify_executor_work(authority, sessions, transport)
        setattr(work, attribute, original)
    run.admission["delegation_version"] = 6
    with pytest.raises(PermissionError, match="current scope"):
        await verify_executor_work(authority, sessions, transport)
    run.admission["delegation_version"] = 5
    result["provider_identity"] = {**authority["provider_identity"], "account_id": "000000000001"}
    with pytest.raises(PermissionError, match="another scope"):
        await verify_executor_work(authority, sessions, transport)
    assert transport.post.await_count == 2
    run.admission["native_agent_ref"]["record_id"] = "another.example.invalid"
    with pytest.raises(PermissionError, match="native agent identity"):
        await verify_executor_work(authority, sessions, transport)
    run.admission["native_agent_ref"]["record_id"] = "agent-example"
    transport.post.return_value.status_code = 403
    with pytest.raises(PermissionError, match="broker authority"):
        await verify_executor_work(authority, sessions, transport)
    transport.post.return_value.status_code = 503
    with pytest.raises(ConnectionError, match="broker response"):
        await verify_executor_work(authority, sessions, transport)


async def test_dispatch_rejects_other_actions_and_payloads():
    authority, _, _, _, _, _ = fixture()
    executor = SimpleNamespace(read_submit=AsyncMock(return_value=None))
    event = {"schema_version": 1, "action": "read_submit", "authority": authority,
             "intent": {"run_id": authority["operation_id"]}, "native_job_ref": None}
    assert (await dispatch(event, executor))["result"] is None
    executor.read_submit.assert_awaited_once()
    with pytest.raises(PermissionError, match="current work"):
        await dispatch({**event, "action": "submit"}, executor)
    for invalid in ({**event, "action": "create_agent"}, {**event, "native_job_ref": {}},
                    {**event, "schema_version": "1"}):
        with pytest.raises(ValueError, match="invalid"):
            await dispatch(invalid, executor)
    executor.read_submit.assert_awaited_once()


def test_lambda_denial_has_no_session_material(monkeypatch):
    authority, _, _, _, _, _ = fixture()
    authority["session_token"] = "not-a-real-token"
    executor = SimpleNamespace(read_submit=AsyncMock(side_effect=PermissionError("selected credential denied")))
    monkeypatch.setattr(executor_host, "build_executor", lambda: executor)
    event = {"schema_version": 1, "action": "read_submit", "authority": authority,
             "intent": {"run_id": authority["operation_id"]}, "native_job_ref": None}
    response = executor_host.lambda_handler(event, None)
    assert response == {"action": "read_submit", "operation_id": authority["operation_id"],
                        "attempt_id": authority["attempt_id"], "error": "authority_revoked"}
    assert "session_token" not in str(response)


def test_host_requires_installed_private_surface(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "example-l5-finops-executor")
    monkeypatch.delenv("L5_FINOPS_SURFACE", raising=False)
    with pytest.raises(ValueError, match="qualified FinOps surface"):
        build_executor()


async def test_host_factory_mounts_current_work_and_broker_without_provider_calls(monkeypatch):
    authority, _, _, sessions, transport, _ = fixture()
    surface = {"agent_label": "Example FinOps Agent", "agent_identity_selector": "[data-agent-id]",
               "conversation_selector": "[data-conversation-id]", "turn_selector": "[data-turn-receipt]",
               "question_selector": "[data-question]", "turn_receipt_attribute": "data-turn-receipt",
               "answer_selector": "[data-answer]", "turn_status_selector": "[data-state]",
               "answer_classification_selector": "[data-outcome]",
               "history_complete_selector": "[data-complete]", "surface_revision": "qualified-example"}
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "example-l5-finops-executor")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("L5_ARCHIVE_BUCKET", "example-private-bucket")
    monkeypatch.setenv("L5_FINOPS_SURFACE", json.dumps(surface))
    browser, evidence = object(), object()
    executor = build_executor(sessions=sessions, transport=transport, browser=browser, evidence=evidence)
    assert isinstance(executor, ConsoleFinOpsExecutor)
    assert executor.browser is browser and executor.evidence_store is evidence
    assert (await executor.verify_work(authority))["lease_fence"] == 2
    assert transport.post.await_count == 1

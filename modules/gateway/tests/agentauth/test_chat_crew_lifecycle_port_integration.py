"""Crew completion and idle SIGTERM through real worker ports and gateway authorization."""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth import chat_data_routes, chat_model, chat_model_execution
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, sign_envelope
from src.agentauth.model_policy import canonical_json
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from tests.agentauth import test_chat_crew_model as crew
from tests.agentauth.test_chat_memory_port_integration import gateway_http as gateway_http_fixture
from tests.agentauth.test_chat_memory_port_integration import node_tools
from tests.agentauth.test_chat_model_execution import Price

client = crew.client
runtime = crew.runtime
store = crew.store
sts = crew.sts
retained_input_table = crew.retained_input_table
mailbox = crew.mailbox
bound = crew.bound
gateway_http = gateway_http_fixture
model_decision = chat_model.sandbox_model_decision
DRIVER = Path(__file__).with_name("fixtures") / "chat_crew_lifecycle_port.cjs"
pytestmark = [pytest.mark.integration, pytest.mark.chat_ports, pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)]


async def test_completed_turn_consolidates_before_sealing_and_idle_sigterm_uses_no_sealed_authority(
    bound, gateway_http, monkeypatch, db_session_factory
):
    signing_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(SIGNING_KEY_ID_ENV, "synthetic-crew-key")
    monkeypatch.setenv(
        SIGNING_KEY_ENV,
        signing_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
    )

    async def policy(*, record, grant, nonce, response_context, **kwargs):
        result = {
            "nonce": nonce,
            "invocation_id": record.invocation_id,
            "tenant_id": record.tenant_id,
            "attempt": record.current_attempt,
            "context": response_context,
            "model_policy": {
                "posture": "enforcing",
                "posture_verified": True,
                "status": "proposed",
                "assertion": "synthetic-policy",
                "decision": {
                    "runtime_posture": "enforcing",
                    "invocation_id": record.invocation_id,
                    "tenant_id": record.tenant_id,
                    "principal_kind": "human",
                    "principal_id": "human",
                    "resolved_model_id": "test-model",
                },
            },
        }
        assertion = sign_envelope(
            tenant_id=record.tenant_id,
            principal=record.principal,
            target_run_id=record.invocation_id,
            target_generation=record.current_attempt,
            action="model_policy_response",
            command_id=nonce,
            request_body=canonical_json(result),
            grant_id=grant.grant_id,
            revocation_epoch=grant.revocation_epoch,
            audience=MODEL_POLICY_AUDIENCE,
            now=datetime.fromtimestamp(bound.runtime[-1], UTC),
        )
        return {"result": result, "assertion": assertion}

    monkeypatch.setattr(chat_model, "sandbox_model_decision", model_decision)
    # The fixture models an enforcing decision, so the route runs under the enforced
    # chat posture; the advisory/off postures are covered in test_chat_sandbox_model.py (#7997).
    monkeypatch.setenv("BG_CHAT_MODEL_POLICY_POSTURE", "enforced")
    monkeypatch.setattr(chat_model, "resolved_model_response", policy)

    async def relay(authority, launch, operation_id, authorize):
        await authorize()

        async def publish(event):
            await authorize()

        return SimpleNamespace(stream_id="a" * 64, delivery=SimpleNamespace(thread_id="thread-one", task_id="task-one"), publish=publish)

    monkeypatch.setattr(chat_model.ChatDeliveryRelay, "create", relay)
    context = TokenContext(
        user_id="human",
        org_id="tenant",
        team_id="team",
        department_id="department",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    context._budget_enforcement_enabled = False
    enforcement = AsyncMock()
    enforcement.prepare_enforcement_context.return_value = None

    async def check(context, cost, **kwargs):
        context._budget_admission_targets = []
        return SimpleNamespace(allowed=True)

    enforcement.check_budget_hierarchy.side_effect = check
    monkeypatch.setattr(
        chat_model_execution, "quote_request", AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.1"), maximum_cost_usd=Decimal("0.1")))
    )
    monkeypatch.setattr(chat_model_execution, "confirm_quote_spendable", AsyncMock(return_value=None))
    provider = AsyncMock(
        return_value={
            "content": [{"type": "text", "text": "Consolidated ten messages"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 4},
            "price": Price(),
            "provider_request_id": "consolidation-receipt",
        }
    )

    def service(authority, db):
        return chat_model_execution.ChatModelExecution(
            authority,
            db,
            enforcement=enforcement,
            provider=provider,
            event_writer=AsyncMock(),
            readiness=AsyncMock(return_value=(context, SimpleNamespace(account_id=None, region="us-east-1"))),
            clock=lambda: bound.runtime[-1],
        )

    monkeypatch.setattr(chat_model, "ChatModelExecution", service)
    monkeypatch.setattr(chat_data_routes, "time", SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic() * 20))
    result = await node_tools(gateway_http, bound.runtime[-1], driver=DRIVER, crewRunId=bound.run_id)
    assert result["events"] == ["consolidation", "receipt", "idle", "SIGTERM", "stopped"]
    assert result["stop"]["consolidation"] == "completed"
    assert result["stop"]["status"] == "checkpoint_unverified"
    assert result["sealedModelStatus"] == 404
    assert result["modelRequestsAfterReceipt"] == 0
    receipt = bound.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]
    assert receipt["result_candidate"]["outcome"] == "completed"
    assert receipt["result_candidate"]["reply_granularity"] == "row"
    assert bound.runtime[1].store._read("TENANT#tenant", "EXEC#run-user")["chat_turn_sealed"] == {"BOOL": True}
    with pytest.raises(ChatAuthorizationRefusedError, match="chat turn already sealed"):
        await run_in_threadpool(
            bound.runtime[0].verify, bound.token, run_id="run-user", session_id="session-a", operation="model.invoke", now=bound.runtime[-1]
        )
    provider.assert_awaited_once()
    async with db_session_factory() as db:
        usage = (await db.scalars(select(UsageLog))).one()
        assert usage.root_human_id == "human" and usage.user_id == "human"
        assert usage.agent_run_id == bound.run_id
    enforcement.check_budget_hierarchy.assert_awaited_once()

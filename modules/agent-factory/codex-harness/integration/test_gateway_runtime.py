"""Real gateway storage/lifecycle -> TaskHost -> official SDK, fixture inference.

Run separately from worker tests: their Python packages both use the name tests.
No live model, IAM or service endpoints participate. DynamoDB and S3 use moto.
"""

# ruff: noqa: E402, F811
import asyncio
import hashlib
import base64
import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest

os.environ.setdefault("BG_TOKEN_SECRET_KEY", "codex-seam-fixture-only-not-a-live-secret")
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/agent-factory/agent-worker-image"))
sys.path.insert(0, str(ROOT / "modules/gateway"))
from lib.run_identity import CONTROL_ENDPOINT_ENV
from lib.task_dispatch import parse_task_envelope
from lib.task_host import TaskHost
from lib.task_run_client import TaskRunClient
from src.agentauth import task_harness, task_model
from src.agentauth.bootstrap import envelope_digest
from src.agentauth.task_admission import TaskAdmission
from src.agentauth.task_responses_contract import TaskResponsesResult
from src.agentauth.task_runtime import TaskRuntime
from src.agentauth.task_runtime_routes import ModelBody
from src.agentauth.task_turns import TaskTurnStore
from src.budget.reservations import ReservationTarget
from src.tasks.dynamo_read_store import DynamoTaskReadStore
from src.tasks.task_commands import TaskCommands

from tests.agentauth.test_task_model import Price
from tests.tasks import test_store as storage
from tests.tasks.test_store import client, store  # noqa: F401

spec = importlib.util.spec_from_file_location(
    "worker_fixture", ROOT / "modules/agent-factory/agent-worker-image/tests/test_task_host.py"
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.mark.parametrize(
    "scenario", ["success", "repair", "steer", "cancel", "unknown", "tools", "tools_repair"]
)
def test_real_gateway_worker_sdk_completion(client, store, tmp_path, monkeypatch, scenario):
    def now():
        return datetime.now(UTC)

    store._clock = now
    persona = "agent-task-gpt-intent-refinement"
    golden = json.loads(
        (
            ROOT / "docs/task-api/contracts/v1/fixtures/valid/bootstrap-codex-response.json"
        ).read_text()
    )
    tool_mode = scenario.startswith("tools")
    from src.agentauth.task_tool_policy import codex_tool_name
    from src.agentauth.task_model_binding import TASK_RESPONSES_TOOLS_REQUEST_SHAPE
    import rfc8785

    tool = {
        "permission": "validation.run",
        "capability": "tests.run",
        "definition": {
            "type": "function",
            "name": codex_tool_name("validation.run"),
            "description": "Validate the bound fixture workspace.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": False,
        },
    }
    if tool_mode:
        definition = json.loads(golden["harness"]["snapshot"]["definition"])
        definition["optionalCapabilities"] = ["tests.run"]
        raw = rfc8785.dumps(definition).decode()
        golden["harness"]["snapshot"].update(
            definition=raw, digest=hashlib.sha256(raw.encode()).hexdigest()
        )
        golden["model_binding"]["request_shape_version"] = TASK_RESPONSES_TOOLS_REQUEST_SHAPE
        monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", json.dumps({persona: [tool["permission"]]}))
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "snapshots": [golden["harness"]["snapshot"]],
                **({"tools": [tool]} if tool_mode else {}),
            }
        )
    )
    monkeypatch.setenv("ADP_CODEX_PERSONA_CATALOG_FILE", str(catalogue))
    monkeypatch.setattr(
        task_harness,
        "persona_compatibility_class",
        lambda name: "codex-sdk" if name == persona else None,
    )
    monkeypatch.setattr(
        "src.admin.persona_models.catalogue.persona_compatibility_class",
        lambda name: "codex-sdk" if name == persona else None,
    )
    client.update_item(
        TableName=storage.AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}},
        UpdateExpression="SET personas = :p",
        ExpressionAttributeValues={":p": {"SS": [persona]}},
    )
    binding = golden["model_binding"]
    policy = {
        "status": "active",
        "allowed_personas": [persona],
        "task_scopes": ["submit"],
        "version": 1,
        "model_policy_version": "1",
        "limits": {
            "max_duration_minutes": 5,
            "max_turns": 4,
            "max_output_tokens_per_turn": 4096,
            "max_usd_per_task": 1,
        },
    }
    if tool_mode:
        policy["allowed_tools"] = [tool["permission"]]
        monkeypatch.setattr(
            "src.agentauth.task_service_policy.TaskServicePolicyStore.get",
            lambda self, **kwargs: policy,
        )
    admission_budget = SimpleNamespace(
        reserve_admission=AsyncMock(
            return_value={"status": "reserved", "reservation_id": "fixture-admission"}
        ),
        settle_admission=AsyncMock(),
    )
    admission = TaskAdmission(
        store,
        policies=SimpleNamespace(get=lambda **kw: policy),
        budget=admission_budget,
        model_resolver=AsyncMock(return_value=binding),
        clock=now,
    )
    receipt = asyncio.run(
        admission.admit(
            caller=SimpleNamespace(
                tenant_id="tenant-a", principal_id="svc-principal-1", require=lambda scope: None
            ),
            submit={
                "persona": persona,
                "instructions": "Inspect the supplied task; report missing implementation evidence.",
                "acceptance_criteria": ["State evidence limitations."],
            },
            idempotency_key="sdk-gateway-fixture",
            db=None,
        )
    )
    work = store.resolve_work(store.read_task(receipt["task_id"])["dispatch_id"])
    envelope = work["envelope"]
    assignment = parse_task_envelope(envelope)
    pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace="adp-agents", name="fixture-pod")
    runtime = TaskRuntime(
        store,
        env={"AGENT_RUN_CREDENTIAL_KEY": "fixture-key-012345678901234567890123456789"},
        clock=now,
    )
    delivery = SimpleNamespace(
        require_assignment=lambda *args: None, read=lambda uid: {"body": json.dumps(envelope)}
    )
    events = []
    budget = SimpleNamespace(
        _target=lambda **kw: ReservationTarget(
            org_id="fixture",
            entity_type="run",
            entity_id="task",
            period_type="lifetime",
            period_start="now",
            headroom_usd=Decimal("1"),
        ),
        _initialize=AsyncMock(),
        verify_settlement=AsyncMock(),
    )
    enforcement = SimpleNamespace(
        check_budget_hierarchy=AsyncMock(return_value=SimpleNamespace(allowed=True)),
        reconcile_reservation=AsyncMock(),
    )
    model_requests = []
    report = {
        "summary": "Implementation evidence is unavailable.",
        "findings": [
            {
                "statement": "The request asks for evidence limitations.",
                "evidence_refs": ["instructions"],
            }
        ],
        "uncertainties": ["No implementation or test output was supplied."],
        "recommendations": ["Provide implementation evidence."],
        "evidence_refs": [{"ref": "instructions", "source": "instructions"}],
    }

    async def provider(_db, **kwargs):
        model_requests.append(kwargs["request"])
        if scenario == "unknown":
            raise TimeoutError("fixture unknown provider outcome")
        if len(model_requests) == 1 and scenario in {"steer", "cancel"}:
            commands.admit(
                task_id=assignment.task_id,
                command_id=str(uuid.uuid4()),
                kind="input" if scenario == "steer" else "cancel",
                payload={"text": "Also inspect the retry configuration."}
                if scenario == "steer"
                else {},
                principal="svc-principal-1",
                tenant="tenant-a",
                expires_at=now() + timedelta(minutes=5),
            )
        text = (
            "invalid first report"
            if (scenario == "repair" and len(model_requests) == 1)
            or (scenario == "tools_repair" and len(model_requests) == 2)
            else json.dumps(report)
        )
        response = {
            "content": [],
            "stop_reason": "completed",
            "responses_response": {
                "id": "resp_fixture",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "price": Price(),
            "provider_request_id": "provider-fixture",
        }
        if tool_mode and len(model_requests) == 1:
            response["responses_response"]["output"] = [
                {
                    "type": "function_call",
                    "id": "item_tool",
                    "namespace": "mcp__adp",
                    "call_id": "fixture_call",
                    "name": tool["definition"]["name"],
                    "arguments": "{}",
                }
            ]
        from src.agentauth.task_responses_tools_contract import TaskToolsResponsesResult

        contract = TaskToolsResponsesResult if tool_mode else TaskResponsesResult
        response["responses_response"] = contract.model_validate(
            response["responses_response"]
        ).model_dump(exclude_none=True)
        return response

    monkeypatch.setattr(
        task_model,
        "quote_request",
        AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0.01"))),
    )
    monkeypatch.setattr(task_model, "confirm_quote_spendable", AsyncMock(return_value=None))
    model = task_model.TaskModel(
        store,
        db=object(),
        budget=budget,
        provider=provider,
        enforcement=enforcement,
        readiness=AsyncMock(
            return_value=(
                binding,
                SimpleNamespace(context=SimpleNamespace()),
                SimpleNamespace(region="us-east-1", account_id="123456789012"),
            )
        ),
        usage_writer=AsyncMock(),
        event_writer=AsyncMock(),
        clock=now,
    )
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="codex-fixture-artifacts")
    reads = DynamoTaskReadStore(store, s3_client=s3, artifact_bucket="codex-fixture-artifacts")
    commands = TaskCommands(store)

    monkeypatch.setenv(CONTROL_ENDPOINT_ENV, "https://fixture.invalid/internal/v1/agent")
    monkeypatch.setattr(
        "lib.task_run_client.read_workload_token", lambda: "fixture-projected-workload"
    )
    monkeypatch.setattr(
        "lib.task_run_client.workload_identity",
        lambda token=None: {"pod_uid": pod.uid, "namespace": pod.namespace},
    )

    class Gateway(TaskRunClient):
        finalize_body = None

        def _post(self, action, body, *, run_bound, **kwargs):
            if run_bound:
                self._renew_for(action, body)
            # Substitute HTTP/IAM delivery only. Actual client bootstrap/renewal,
            # credential validation, gateway models and durable services run.
            return getattr(self, "gateway_" + action.replace("-", "_"))(
                json.loads(json.dumps(body))
            )

        def identity(self, require_attempt=True):
            return runtime.authenticate(
                credential=self._run_credential, pod=pod, require_attempt=require_attempt
            )

        def gateway_bootstrap(self, body):
            assert body["envelope_digest"] == envelope_digest(envelope)
            result = runtime.bootstrap(body=body, pod=pod, delivery=delivery)
            return result

        def gateway_attempt(self, body):
            runtime.register_attempt(identity=self.identity(False), body=body)
            return {
                "schema_version": "1.0",
                "operation_status": "confirmed",
                "request_id": body["runtime_attempt_id"],
            }

        def gateway_control(self, body):
            return commands.control(self.identity())

        def gateway_turn(self, body):
            return TaskTurnStore(store, clock=now).commit(
                identity=self.identity(),
                request_id=body["request_id"],
                expected_transcript_version=body["expected_transcript_version"],
                allow_autonomous=body.get("allow_autonomous", False),
            )

        def gateway_model(self, body):
            parsed = ModelBody.model_validate(body)
            return asyncio.run(
                model.execute(
                    identity=self.identity(),
                    turn_id=parsed.turn_id,
                    request_digest=parsed.request_digest,
                    request=parsed.invocation(),
                    responses_request=True,
                )
            )

        def gateway_tool_operation(self, body):
            from src.agentauth import task_tool_routes as routes
            from src.agentauth.task_tool_receipts import TaskToolReceipts

            journal = TaskToolReceipts(
                store,
                authorize=lambda identity, permission: routes.authorize_tool(
                    store, SimpleNamespace(get=lambda **kwargs: policy), identity, permission
                ),
                catalogue={tool["permission"]: tool["definition"]["name"]},
                clock=now,
            )
            monkeypatch.setattr(
                routes,
                "authenticate_task_attempt",
                AsyncMock(side_effect=lambda request: self.identity()),
            )
            monkeypatch.setattr(routes, "tool_journal", lambda identity: journal)
            parsed = (
                routes.ToolClaimBody if body["action"] == "claim" else routes.ToolSettleBody
            ).model_validate(body)
            return asyncio.run(routes.tool_operation(parsed, SimpleNamespace()))

        def tool(self, name, body):
            assert name == tool["permission"]
            events.append("tool-effect")
            content = b'{"validated":true}'
            record = reads.put_run_artifact(
                attempt=self.identity(),
                content=content,
                content_type="application/json",
                digest=hashlib.sha256(content).hexdigest(),
            )
            return {
                "schema_version": "1.0",
                "task_id": assignment.task_id,
                "operation_id": body["operation_id"],
                "operation_status": "confirmed",
                "result": {"validated": True},
                "artifact": {
                    "artifact_id": record.artifact_id,
                    "content_type": "application/json",
                    "content_sha256": record.content_sha256,
                    "byte_length": len(content),
                },
            }

        def gateway_report(self, body):
            identity = self.identity()
            result = reads.append_event(
                task_id=identity.task_id,
                report_id=body["report_id"],
                event_type=body["event_type"],
                data=body["data"],
                producer_timestamp=body.get("producer_timestamp"),
                timestamp=now(),
                expect_generation=identity.generation,
                expect_runtime_attempt_id=identity.runtime_attempt_id,
            )
            return {
                "schema_version": "1.0",
                "report_id": body["report_id"],
                "sequence": result.event.sequence,
                "event_id": result.event.event_id,
            }

        def gateway_artifact(self, body):
            record = reads.put_run_artifact(
                attempt=self.identity(),
                content=base64.b64decode(body["content_base64"]),
                content_type=body["content_type"],
                digest=body["content_sha256"],
            )
            return {
                "schema_version": "1.0",
                "artifact_id": record.artifact_id,
                "version": record.version,
                "content_type": record.content_type,
                "content_sha256": record.content_sha256,
                "expires_at": None,
            }

        def gateway_finalize(self, body):
            self.finalize_body = body
            from src.agentauth.task_budget_settlement import settle_task_admission

            identity = self.identity()
            result = commands.finalize(identity, body)
            settled = asyncio.run(settle_task_admission(store, identity, budget=admission_budget))
            assert settled is (scenario != "unknown")
            return result

    gateway = Gateway()
    monkeypatch.setattr(
        "lib.task_host.workload_identity", lambda: {"pod_uid": pod.uid, "namespace": pod.namespace}
    )
    node = os.environ.get("ADP_CODEX_TEST_NODE", "node")
    entry = ROOT / "modules/agent-factory/codex-harness/dist/task-entry.mjs"
    host = TaskHost(
        client=gateway,
        work_root=tmp_path / "work",
        command_resolver=lambda _: [node, str(entry), "--embedded"],
    )
    result = host.run(
        assignment,
        envelope,
        heartbeat=worker.FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    )
    task = store.read_task(assignment.task_id)
    expected_state = {"cancel": "cancelled", "unknown": "failed"}.get(scenario, "completed")
    assert task["state"] == expected_state, (
        result,
        task.get("error"),
        gateway.finalize_body,
        events,
    )
    assert len(model_requests) == (
        3 if scenario == "tools_repair" else 2 if scenario in {"repair", "steer", "tools"} else 1
    )
    if tool_mode:
        assert events.count("tool-effect") == 1
        for invocation in model_requests[1:]:
            assert (
                len(
                    [
                        item
                        for item in invocation["input"]
                        if item.get("type") == "function_call_output"
                    ]
                )
                == 1
            )
    if scenario == "unknown":
        enforcement.reconcile_reservation.assert_not_awaited()
        admission_budget.settle_admission.assert_not_awaited()
    else:
        assert enforcement.reconcile_reservation.await_count == len(model_requests)
        admission_budget.settle_admission.assert_awaited_once_with(
            {"status": "reserved", "reservation_id": "fixture-admission"},
            actual_usd=Decimal("0.001") * len(model_requests),
        )
    if expected_state == "completed":
        assert result == 0
        assert task["result"]["process_exit_validated"] is True
        for artifact_id in task["result"]["artifact_ids"]:
            record = reads.load_artifact(artifact_id=artifact_id)
            assert json.loads(reads.read_artifact(record=record)) == report
    else:
        assert result == 1 and task["result"] is None
    if scenario == "steer":
        assert "Also inspect the retry configuration." in json.dumps(model_requests[1])
    assert "ack" in events
    assert not list((tmp_path / "work").iterdir())

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
import subprocess
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


@pytest.fixture(autouse=True)
def sdk_otel_collector(request, monkeypatch):
    if getattr(request.node, "callspec", None) is None or request.node.callspec.params.get("scenario") != "tools_developer_otel":
        yield
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    documents = []
    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().strip(), 16)
                    if not size:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    assert self.rfile.read(2) == b"\r\n"
                raw = b"".join(chunks)
            else:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
            documents.append((self.path, json.loads(raw)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ADP_CODEX_OTEL_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    try:
        yield
        spans = [span for path, doc in documents if path == "/v1/traces"
            for resource in doc["resourceSpans"] for scope in resource["scopeSpans"] for span in scope["spans"]]
        run = next(span for span in spans if span["name"] == "adp.codex.run")
        turns = [span for span in spans if span["name"] == "adp.codex.turn"]
        assert turns and all(span["traceId"] == run["traceId"] for span in turns)
        assert any(path == "/v1/metrics" for path, _ in documents)
        serialized = json.dumps(documents).lower()
        assert all(secret not in serialized for secret in ("run-secret", "source.txt", "authorization", "access_token"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "repair",
        "steer",
        "cancel",
        "unknown",
        "tools",
        "tools_repair",
        "tools_docker",
        "tools_workspace",
        "tools_edit_validate",
        "tools_edit_validate_publish",
        "tools_developer",
        "tools_developer_moved",
        "tools_developer_otel",
        "tools_developer_many_turns",
        "tools_developer_live",
        "tools_developer_live_retry",
    ],
)
def test_real_gateway_worker_sdk_completion(client, store, tmp_path, monkeypatch, scenario, caplog):
    def now():
        return datetime.now(UTC)

    store._clock = now
    many_turns = scenario == "tools_developer_many_turns"
    retry_story = scenario == "tools_developer_live_retry"
    actual_live = scenario in {"tools_developer_live", "tools_developer_live_retry"}
    if actual_live and not os.environ.get("ADP_CODEX_LIVE_CONFIG_SOURCE"):
        pytest.skip("requires explicit isolated live gateway qualification")
    if actual_live:
        monkeypatch.syspath_prepend(str(Path(__file__).parent))
    live_metrics = []
    actual_developer = scenario.startswith("tools_developer")
    persona = "agent-task-gpt-developer" if actual_developer else "agent-task-gpt-intent-refinement"
    golden = json.loads(
        (
            ROOT / "docs/task-api/contracts/v1/fixtures/valid/bootstrap-codex-response.json"
        ).read_text()
    )
    tool_mode = scenario.startswith("tools")
    actual_validation = scenario == "tools_docker"
    actual_publication = scenario == "tools_edit_validate_publish" or actual_developer
    actual_workflow = scenario in {"tools_edit_validate", "tools_edit_validate_publish"} or actual_developer
    actual_workspace = scenario == "tools_workspace" or actual_workflow
    workflow_calls = 12 if many_turns else 5 if actual_publication else 4
    workflow = {}
    image = os.environ.get("ADP_CODEX_RETRY_IMAGE" if retry_story else "ADP_CODEX_DETACHED_IMAGE" if actual_developer else "ADP_CODEX_VALIDATION_IMAGE")
    if (actual_validation or actual_workflow) and not image:
        pytest.skip("requires an explicitly provisioned immutable Docker image")
    tool_arguments = {}
    if actual_validation:
        repository = tmp_path / "validation-repository"
        repository.mkdir()

        def git(*args):
            return subprocess.check_output(
                [
                    "/usr/bin/git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@localhost",
                    *args,
                ],
                cwd=repository,
                text=True,
            ).strip()

        git("init", "-q")
        (repository / "test.sh").write_text('test "$(cat value.txt)" = expected\n')
        (repository / "value.txt").write_text("expected\n")
        git("add", "test.sh", "value.txt")
        git("commit", "-qm", "Task validation fixture")
        validation_head = git("rev-parse", "HEAD")
        tool_arguments = {"check": "acceptance", "commit": validation_head}
        validation_binding = tmp_path / "validation-binding.json"
        monkeypatch.setenv("ADP_CODEX_VALIDATION_BINDING_FILE", str(validation_binding))
        monkeypatch.setenv(
            "ADP_TASK_TOOL_ROUTES",
            json.dumps({"validation.run": "local:lib.codex_validation_tool.create"}),
        )
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
    if actual_validation:
        tool["definition"]["parameters"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"check": {"type": "string"}, "commit": {"type": "string"}},
            "required": ["check", "commit"],
        }
    if actual_workspace:
        tool.update(permission="repository.read", capability="repository.read")
        tool["definition"].update(
            name=codex_tool_name("repository.read"),
            description="Read a file in the authorized repository.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )
        tool_arguments = {"path": "source.txt"}
    tools = [tool]
    if actual_workflow:
        for permission, capability, properties in [
            (
                "repository.write",
                "repository.write",
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "expected_sha256": {"type": ["string", "null"]},
                },
            ),
            ("repository.commit", "repository.write", {"message": {"type": "string"}}),
            (
                "validation.run",
                "tests.run",
                {"check": {"type": "string"}, "commit": {"type": "string"}},
            ),
        ]:
            tools.append(
                {
                    "permission": permission,
                    "capability": capability,
                    "definition": {
                        "type": "function",
                        "name": codex_tool_name(permission),
                        "description": "Run the admitted " + permission + " operation.",
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": list(properties),
                            "additionalProperties": False,
                        },
                        "strict": False,
                    },
                }
            )
    if actual_publication:
        tools.append({"permission": "change.create", "capability": "change.create", "definition": {
            "type": "function", "name": codex_tool_name("change.create"), "description": "Publish validated change.",
            "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in ("commit", "title", "body")},
                           "required": ["commit", "title", "body"], "additionalProperties": False}, "strict": False}})
    if tool_mode:
        definition = json.loads(golden["harness"]["snapshot"]["definition"])
        definition["optionalCapabilities"] = sorted({entry["capability"] for entry in tools})
        if actual_developer:
            definition = json.loads((ROOT / "modules/agent-factory/codex-harness/personas/developer.json").read_text())
            golden["harness"]["snapshot"]["instructions"] = definition["instructions"]
        raw = rfc8785.dumps(definition).decode()
        golden["harness"]["snapshot"].update(
            definition=raw, digest=hashlib.sha256(raw.encode()).hexdigest()
        )
        golden["model_binding"]["request_shape_version"] = TASK_RESPONSES_TOOLS_REQUEST_SHAPE
        monkeypatch.setenv(
            "ADP_TASK_PERSONA_TOOLS",
            json.dumps({persona: [entry["permission"] for entry in tools]}),
        )
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "snapshots": [golden["harness"]["snapshot"]],
                **({"tools": tools} if tool_mode else {}),
            }
        )
    )
    monkeypatch.setenv("ADP_CODEX_PERSONA_CATALOG_FILE", str(catalogue))
    monkeypatch.setenv("ADP_CODEX_TASK_PERSONAS", persona)
    client.update_item(
        TableName=storage.AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}},
        UpdateExpression="SET personas = :p",
        ExpressionAttributeValues={":p": {"SS": [persona]}},
    )
    binding = golden["model_binding"]
    if actual_live:
        binding["model_id"] = os.environ["ADP_CODEX_LIVE_MODEL"]
    policy = {
        "status": "active",
        "allowed_personas": [persona],
        "task_scopes": ["submit"],
        "version": 1,
        "model_policy_version": "1",
        "limits": {
            "max_duration_minutes": 5,
            "max_turns": 8 if actual_workflow else 4,
            "max_output_tokens_per_turn": 4096,
            "max_usd_per_task": 1,
        },
    }
    if many_turns:
        policy["limits"]["codex_max_turns"] = 20
    if actual_workspace:
        policy["repositories"] = {
            "application": {
                "provider": "github",
                "connection_id": "installation:123",
                "repository_id": "456",
                "repository": "org/repo",
                "base_branch": "main",
            }
        }
    if actual_workflow:
        policy["repositories"]["application"]["validation_checks"] = [
            {
                "name": "acceptance",
                "image": image,
                "argv": ["/opt/adp-checks/acceptance"] if actual_developer else ["/bin/sh", "test.sh"],
                "max_output_bytes": 8192,
            }
        ]
    if actual_developer:
        criterion = (
            "Implement CommonJS retryDelay(options) in retry-delay.js. attempt must be a nonnegative integer; "
            "baseMs and maxMs must be finite nonnegative numbers, defaulting to 100 and 30000. "
            "Reject invalid attempt/baseMs/maxMs with RangeError. attempt=0 returns 0 without sampling. "
            "For positive attempts call options.random (default Math.random) exactly once; require a finite numeric sample in [0,1), "
            "otherwise throw RangeError. Return floor(sample * min(maxMs, baseMs * 2**(attempt-1))), "
            "including finite correct results for huge attempts and zero base/cap. Do not mutate options."
        ) if retry_story else "source.txt contains exactly expected followed by a newline."
        policy["repositories"]["application"]["acceptance_checks"] = {
            hashlib.sha256(("0\0" + criterion).encode()).hexdigest(): "acceptance"}
    if tool_mode:
        policy["allowed_tools"] = [entry["permission"] for entry in tools]
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
                "instructions": (criterion + " Read the existing retry-delay.js, implement only this module, commit, run the named acceptance check, and create a ready PR after it passes.") if retry_story else (
                    "Fix source.txt so it contains exactly expected followed by a newline. Read the file before editing, "
                    "commit the change, run the named acceptance validation check on that commit, then create a ready PR. "
                    "Use the tool receipts to cite the final result. The check is supplied by the trusted host. "
                    "Do not alter other files."
                ) if actual_developer else "Inspect the supplied task; report missing implementation evidence.",
                "acceptance_criteria": [criterion] if actual_developer else ["State evidence limitations."],
                **({"inputs": {"repository_binding": "application"}} if actual_workspace else {}),
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
    validated_receipts = []
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
        if actual_live:
            from live_gateway import invoke_live_gateway
            try:
                document, measurement = await invoke_live_gateway(kwargs["request"], model=binding["model_id"])
            except Exception as error:
                events.append("live-inference-refused:" + type(error).__name__ + (":" + str(error) if isinstance(error, RuntimeError) else ""))
                raise
            live_metrics.append(measurement)
            from src.agentauth.task_responses_tools_contract import TaskToolsResponsesResult
            try:
                from src.agentauth.task_responses_contract import normalize_provider_result
                result = TaskToolsResponsesResult.model_validate(normalize_provider_result(document)).model_dump(exclude_none=True)
            except Exception as error:
                events.append("live-contract-error:" + json.dumps(error.errors(include_input=False, include_context=False)))
                raise
            return {"content": [], "stop_reason": "completed", "responses_response": result, "usage": result["usage"],
                    "price": Price(), "provider_request_id": measurement["provider_request_id"]}
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
        selected_tool, selected_args = tool, tool_arguments
        if actual_workflow and len(model_requests) <= workflow_calls:
            selected_tool = tools[min(len(model_requests), 5) - 1]
            selected_args = [
                {"path": "source.txt"},
                {
                    "path": "source.txt",
                    "content": "expected\n",
                    "expected_sha256": hashlib.sha256(
                        b"Task SDK workspace fixture source"
                    ).hexdigest(),
                },
                {"message": "Repair fixture value"},
                {"check": "acceptance", "commit": workflow.get("commit", "")},
                {"commit": workflow.get("commit", ""), "title": "Repair source", "body": "Validation passed."},
            ][min(len(model_requests), 5) - 1]
            if many_turns and len(model_requests) > 5:
                selected_tool, selected_args = tools[0], {"path": "source.txt"}
        if tool_mode and (
            len(model_requests) == 1 or (actual_workflow and len(model_requests) <= workflow_calls)
        ):
            response["responses_response"]["output"] = [
                {
                    "type": "function_call",
                    "id": "item_tool_" + str(len(model_requests)),
                    "namespace": "mcp__adp",
                    "call_id": "fixture_call_" + str(len(model_requests))
                    if actual_workflow
                    else "fixture_call",
                    "name": selected_tool["definition"]["name"],
                    "arguments": json.dumps(selected_args),
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
            try:
                return getattr(self, "gateway_" + action.replace("-", "_"))(
                    json.loads(json.dumps(body))
                )
            except Exception as error:
                events.append(f"fixture-gateway-error:{action}:{type(error).__name__}:{error}")
                raise

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
            if actual_validation:
                identity = self.identity()
                validation_binding.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "attempt": {
                                "run": {
                                    key: getattr(identity, key)
                                    for key in ("task_id", "invocation_id", "generation")
                                },
                                "runtime_attempt_id": identity.runtime_attempt_id,
                            },
                            "repository_path": str(repository),
                            "checks": [
                                {
                                    "name": "acceptance",
                                    "image": image,
                                    "argv": ["/bin/sh", "test.sh"],
                                    "max_output_bytes": 8192,
                                }
                            ],
                        }
                    )
                )
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
                catalogue={entry["permission"]: entry["definition"]["name"] for entry in tools},
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

        def gateway_repository_source(self, body):
            import io
            import tarfile
            from src.agentauth.github_provider import ArchiveSlice
            from src.agentauth.task_source_staging import TaskSourceStaging
            from src.agentauth.task_tool_routes import authorize_tool

            identity = self.identity()
            staging = TaskSourceStaging(
                store,
                s3=s3,
                bucket=reads.bucket,
                authorize=lambda current, permission: authorize_tool(
                    store, SimpleNamespace(get=lambda **kw: policy), current, permission
                ),
            )
            if staging.read(identity) is None:
                source = io.BytesIO()
                with tarfile.open(fileobj=source, mode="w:gz") as archive:
                    entry = tarfile.TarInfo("provider-root/" + ("retry-delay.js" if retry_story else "source.txt"))
                    content = (ROOT / "modules/agent-factory/codex-harness/test/fixtures/retry-story/retry-delay.js").read_bytes() if retry_story else b"Task SDK workspace fixture source"
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
                    if actual_workflow:
                        check = b'test "$(cat source.txt)" = expected\n'
                        entry = tarfile.TarInfo("provider-root/test.sh")
                        entry.size = len(check)
                        archive.addfile(entry, io.BytesIO(check))
                content = source.getvalue()
                staging.stage(
                    identity,
                    ArchiveSlice(
                        commit_sha="b" * 40,
                        total_bytes=len(content),
                        digest=hashlib.sha256(content).hexdigest(),
                        content=content,
                    ),
                )
                events.append("source-staged")
            return staging.chunk(identity, index=body["index"])

        def gateway_repository_publication(self, body):
            from src.agentauth.task_publication_service import TaskPublicationService
            from src.agentauth.task_source_staging import TaskSourceStaging
            from src.agentauth.task_validation_evidence import TaskValidationEvidence
            from src.agentauth.task_tool_routes import authorize_tool
            def authorize(current, permission):
                return authorize_tool(store, SimpleNamespace(get=lambda **kwargs: policy), current, permission)
            async def publish(**kwargs):
                await kwargs["reauthorize"]()
                proposal = kwargs["manifest"]
                workflow["manifest"] = proposal
                events.append("publication-effect")
                return {"schema_version": "1.0", "task_id": assignment.task_id, "provider": "github",
                        "repository_id": proposal["repository_id"], "source_revision": proposal["source_revision"],
                        "local_head": proposal["local_head"], "tree": proposal["tree"], "provider_head": "e" * 40,
                        "branch": "adp/task-" + assignment.task_id.removeprefix("tsk_"), "number": 7,
                        "url": "https://github.com/" + proposal["repository"] + "/pull/7", "state": "open", "draft": False}
            service = TaskPublicationService(store, artifacts=reads,
                staging=TaskSourceStaging(store, s3=reads.s3, bucket=reads.bucket, authorize=authorize),
                validations=TaskValidationEvidence(store, artifacts=reads, authorize=authorize), authorize=authorize, publisher=publish)
            arguments = {key: body[key] for key in ("artifact_id", "digest", "commit", "title", "body")}
            result = asyncio.run(service.execute(self.identity(), **arguments))
            assert result == asyncio.run(service.execute(self.identity(), **arguments))
            workflow["publication"] = service.read(self.identity())
            return result

        def gateway_repository_completion(self, body):
            from src.agentauth.task_completion_service import TaskCompletionService
            from src.agentauth.task_publication_service import TaskPublicationService
            from src.agentauth.task_source_staging import TaskSourceStaging
            from src.agentauth.task_validation_evidence import TaskValidationEvidence
            from src.agentauth.task_tool_routes import authorize_tool
            def authorize(current, permission):
                return authorize_tool(store, SimpleNamespace(get=lambda **kwargs: policy), current, permission)
            async def observe(**kwargs):
                await kwargs["reauthorize"]()
                events.append("completion-observed")
                receipt = dict(kwargs["receipt"])
                if scenario == "tools_developer_moved":
                    receipt["provider_head"] = "f" * 40
                return receipt
            publication = TaskPublicationService(store, artifacts=reads,
                staging=TaskSourceStaging(store, s3=reads.s3, bucket=reads.bucket, authorize=authorize),
                validations=TaskValidationEvidence(store, artifacts=reads, authorize=authorize), authorize=authorize, publisher=None)
            from src.tasks.store import TaskStoreError
            try:
                return asyncio.run(TaskCompletionService(publication, observe=observe).execute(self.identity()))
            except TaskStoreError:
                return {"schema_version": "1.0", "task_id": self.identity().task_id, "status": "unverified"}

        def gateway_tool_authorize(self, body):
            from src.agentauth.task_tool_routes import ToolAuthorizationBody, authorize_tool
            from src.agentauth.task_runtime_routes import require_body_attempt

            parsed = ToolAuthorizationBody.model_validate(body)
            identity = self.identity()
            require_body_attempt(identity, parsed.attempt)
            return authorize_tool(
                store, SimpleNamespace(get=lambda **kwargs: policy), identity, parsed.tool
            )

        def tool(self, name, body):
            assert name in {entry["permission"] for entry in tools}
            events.append("tool-effect")
            if actual_validation or actual_workspace:
                result = super().tool(name, body)
                if actual_live:
                    workflow.setdefault("tool_results", []).append({"tool": name, "status": result.get("result", {}).get("status")})
                if actual_workflow and name == "repository.commit":
                    workflow["commit"] = result["result"]["localHead"]
                return result
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
            if actual_validation or (actual_workflow and "commit" in workflow):
                final_head = workflow["commit"] if actual_workflow else validation_head
                from src.agentauth.task_validation_evidence import TaskValidationEvidence
                from src.agentauth.task_tool_routes import authorize_tool

                reader = TaskValidationEvidence(
                    store,
                    artifacts=reads,
                    authorize=lambda current, permission: authorize_tool(
                        store, SimpleNamespace(get=lambda **kwargs: policy), current, permission
                    ),
                )
                validated_receipts.extend(reader.read(identity=identity, commit=final_head))
                assert reader.read(identity=identity, commit="f" * 40) == []
                from src.tasks.store import TaskStoreError

                with monkeypatch.context() as altered:
                    altered.setattr(
                        reads, "read_artifact", lambda **kwargs: b"forged execution bytes"
                    )
                    with pytest.raises(TaskStoreError, match="artifact"):
                        reader.read(identity=identity, commit=final_head)
            if actual_developer and body["outcome"] == "completed":
                self.gateway_repository_completion({"schema_version": "1.0", "attempt": body["attempt"]})
            result = commands.finalize(identity, body)
            settled = asyncio.run(settle_task_admission(store, identity, budget=admission_budget))
            assert actual_live or settled is (scenario != "unknown")
            return result

    gateway = Gateway()
    monkeypatch.setattr(
        "lib.task_host.workload_identity", lambda: {"pod_uid": pod.uid, "namespace": pod.namespace}
    )
    node = os.environ.get("ADP_CODEX_TEST_NODE", "node")
    entry = ROOT / "modules/agent-factory/codex-harness/dist/task-entry.mjs"
    from lib.task_commands import task_agent_command
    def packaged_command(selected):
        command = task_agent_command(selected)
        assert command == ["node", "/app/codex-harness/dist/task-entry.mjs", "--embedded"]
        # Relocate only the packaged executable; exercise the real command registry.
        return [node, str(entry), command[2]]
    host = TaskHost(
        client=gateway,
        work_root=tmp_path / "work",
        command_resolver=packaged_command,
    )
    result = host.run(
        assignment,
        envelope,
        heartbeat=worker.FakeHeartbeat(events),
        acknowledge=lambda: events.append("ack"),
    )
    task = store.read_task(assignment.task_id)
    expected_state = {"cancel": "cancelled", "unknown": "failed", "tools_developer_moved": "failed"}.get(scenario, "completed")
    if actual_live and task["state"] != expected_state:
        print(json.dumps({"events": events, "model_requests": len(model_requests), "responses": len(live_metrics)}))
    assert task["state"] == expected_state, (
        result,
        task.get("error"),
        gateway.finalize_body,
        events,
        [(record.message, getattr(record, "exception_type", "")) for record in caplog.records],
        len(model_requests), len(live_metrics),
    )
    assert actual_live or len(model_requests) == (
        workflow_calls + 1
        if actual_workflow
        else 3
        if scenario == "tools_repair"
        else 2
        if scenario in {"repair", "steer", "tools", "tools_docker", "tools_workspace"}
        else 1
    )
    if actual_validation:
        from src.agentauth.task_tool_receipts import TaskToolReceipts
        from src.tasks.records import task_ops_partition

        stored = store._get(
            task_ops_partition(assignment.task_id), TaskToolReceipts._key("fixture_call")
        )
        assert stored["operation_status"] == "confirmed"
        execution = json.loads(stored["content"])["result"]
        assert execution["status"] == "passed" and execution["commit"] == validation_head
        assert execution["check"] == "acceptance" and execution["exitCode"] == 0
        assert len(validated_receipts) == 1
        assert (
            validated_receipts[0]["commit"] == validation_head
            and validated_receipts[0]["status"] == "passed"
        )
    if actual_workspace and not actual_live:
        from src.agentauth.task_tool_receipts import TaskToolReceipts
        from src.tasks.records import task_ops_partition

        row = store._get(
            task_ops_partition(assignment.task_id),
            TaskToolReceipts._key("fixture_call_1" if actual_workflow else "fixture_call"),
        )
        assert row["operation_status"] == "confirmed"
        execution = json.loads(row["content"])["result"]
        assert (
            execution["status"] == "completed"
            and execution["content"] == "Task SDK workspace fixture source"
        )
        assert events.count("source-staged") == 1
        assert not list((tmp_path / "work").iterdir())
        assert gateway._workspace_tools is None
    if actual_workflow:
        assert len(validated_receipts) == 1
        assert (
            validated_receipts[0]["status"] == "passed"
            and validated_receipts[0]["commit"] == workflow["commit"]
        )
        assert gateway._validation_tool is None
    if actual_developer:
        assert events.count("completion-observed") == (1 if scenario == "tools_developer_moved" else 2)
    if actual_publication:
        assert events.count("publication-effect") == 1
        assert workflow["publication"]["operation_status"] == "confirmed"
        assert workflow["publication"]["result"]["local_head"] == workflow["commit"]
        assert gateway._publication_tool is None
    if tool_mode and not actual_live:
        assert events.count("tool-effect") == (workflow_calls if actual_workflow else 1)
        for index, invocation in enumerate(model_requests[1:], 1):
            assert len(
                [item for item in invocation["input"] if item.get("type") == "function_call_output"]
            ) == (index if actual_workflow else 1)
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
            assert actual_live or json.loads(reads.read_artifact(record=record)) == report
    else:
        assert result == 1 and task["result"] is None
    if scenario == "steer":
        assert "Also inspect the retry configuration." in json.dumps(model_requests[1])
    if actual_live:
        destination = os.environ.get("ADP_CODEX_LIVE_EVIDENCE")
        if destination:
            Path(destination).write_text(json.dumps({"model": binding["model_id"], "effort": "medium",
                "status": task["state"], "model_calls": live_metrics, "tool_effects": events.count("tool-effect"),
                "publication": workflow.get("publication", {}).get("result"), "manifest": workflow.get("manifest"), "tool_results": workflow.get("tool_results"),
                "limitations": ["Task admission identity and ledger use Moto fixtures", "provider source and PR publication use fixtures", "gateway inference is live and charged to the authenticated user"]}, indent=2))
    assert "ack" in events
    assert not list((tmp_path / "work").iterdir())

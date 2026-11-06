import copy
import hashlib
import importlib
import io
import json
import time
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from src.activity import campaign_recovery_routes as recovery_routes
from src.agentauth import human_control
from src.agentauth import routes as agent_routes
from src.agentauth.bootstrap import BootstrapRefusedError, issue_bound_credential
from src.agentauth.campaign_pod_recovery import FINALIZER
from src.agentauth.execution import ExecutionStateError
from src.agentauth.workload import WorkloadRefusedError
from src.auth import agent_registry
from src.auth import middleware as auth_middleware
from src.auth.dependencies import get_current_user
from src.internal import auth_deps
from src.proxy.routes import get_token_context
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_campaign_execution import ENV
from tests.agentauth.test_campaign_execution import native as native_fixture
from tests.agentauth.test_campaign_pod_recovery import cluster as cluster_fixture
from tests.agentauth.test_campaign_pod_recovery import exit_pod

native = native_fixture
cluster = cluster_fixture


def private_write(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def harness(native, cluster, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/performance/gateway"))
    controller = importlib.import_module("campaign_controller")
    recovery = importlib.import_module("campaign_recovery")
    transport = importlib.import_module("campaign_native")
    signer = importlib.import_module("campaign_signer")
    tmp_path.chmod(0o700)
    role = "arn:aws:sts::000000000000:assumed-role/campaign-worker"
    configuration = {
        "schema_version": 1,
        "backend": "adp-native",
        "campaign": "native-test",
        "owner": "test-owner",
        "journal_dir": str(tmp_path),
        "target": "https://gateway.example.test",
        "revision": "test-revision",
        "approved_account": "000000000000",
        "approved_role": role,
        "aws_connection": "cleanup-connection",
        "aws_region": "us-east-1",
        "health_owner": "signal-owner",
        "health_file": str(tmp_path / "health.json"),
        "gateway_admin_token_file": str(tmp_path / "human-token"),
        "cleanup_seconds": 5,
        "native_target": "https://native.example.test",
        "native_aws_connection": "native-connection",
        "native_approved_role": role,
        "fixtures": [],
    }
    for invocation in ("root", "child"):
        fixture = {
            "kind": "workload",
            "binding": invocation,
            "tenant": "tenant",
            "root_human": "human",
            "ownership_receipt": "private-ownership-" + invocation,
            "workload_uid": "pod-" + invocation,
            "attempt": 1,
            "credential_epoch": 1,
            "parent": "root" if invocation == "child" else None,
            "run_credential_file": str(tmp_path / (invocation + "-credential")),
            "workload_token_file": str(tmp_path / (invocation + "-workload")),
            "token_file": str(tmp_path / (invocation + "-cache")),
        }
        private_write(Path(fixture["run_credential_file"]), native.credentials[invocation])
        private_write(Path(fixture["workload_token_file"]), invocation)
        configuration["fixtures"].append(fixture)
    private_write(Path(configuration["gateway_admin_token_file"]), "private-human-token")

    def health(backend=None):
        document = {key: configuration[key] for key in ("campaign", "target", "revision", "approved_account", "approved_role")}
        document.update(owner="signal-owner", observed_at=time.time(), healthy=True, no_owned_active_clients=True, normal_user_denials_ok=True)
        private_write(Path(configuration["health_file"]), document)

    health()
    monkeypatch.setattr(transport.NativeFixtureBackend, "_verify_aws", health)
    native.runtime.validate_flow = AsyncMock()
    current = TokenContext(
        user_id="human", org_id="tenant", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    edge_settings = SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="test-edge-proof")
    monkeypatch.setattr(auth_middleware, "get_settings", lambda: edge_settings)
    monkeypatch.setattr(auth_deps, "get_settings", lambda: edge_settings)
    registry_entry = {
        "agent_id": "campaign-worker",
        "agent_name": "campaign-worker",
        "role_arn": "arn:aws:iam::000000000000:role/campaign-worker",
        "org_id": "tenant",
        "team_id": "",
        "scope": "internal",
        "requires_run_identity": True,
    }
    registry = SimpleNamespace(get_agent_by_role_arn=Mock(side_effect=lambda arn: registry_entry if arn == registry_entry["role_arn"] else None))
    monkeypatch.setattr(agent_registry, "get_agent_registry_service", lambda: registry)
    app = FastAPI()
    app.include_router(recovery_routes.router)
    app.include_router(agent_routes.router)
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: Mock()
    app.dependency_overrides[recovery_routes.get_campaign_recovery] = lambda: native.service
    app.dependency_overrides[agent_routes.get_agent_runtime] = lambda: native.runtime
    monkeypatch.setattr(recovery_routes, "get_agent_runtime", lambda: native.runtime)

    @asynccontextmanager
    async def fresh_db():
        yield Mock()

    async def canonical(database, identifier, **kwargs):
        return identifier

    monkeypatch.setattr(recovery_routes, "get_session_factory", lambda: fresh_db)
    monkeypatch.setattr(recovery_routes, "resolve_canonical_user_id", canonical)
    monkeypatch.setattr(human_control, "resolve_canonical_user_id", canonical)
    monkeypatch.setattr(human_control, "require_live_human_membership", AsyncMock())
    observed = SimpleNamespace(models=[], signed=[], forwarded=[], auto_exit=True, lost_enrollment=False, wrong_enrollment=False)

    @app.get("/health/detailed")
    def healthy():
        return {"all_healthy": True}

    @app.post("/v1/chat/completions")
    @app.post("/openai/v1/responses")
    async def model(request: Request, context: TokenContext = Depends(get_token_context)):
        assert context.auth_source == "iam"
        assert context.agent_registry_id == "campaign-worker"
        assert context.requires_run_identity is True
        _, caller, _, _ = native.runtime.authenticate(request.headers["X-Adp-Run-Credential"], request.headers["X-Adp-Workload-Token"])
        assert context.org_id == caller.tenant_id
        assert request.headers["X-Agent-RunId"] == caller.invocation_id
        assert request.headers["X-Agent-OrgId"] == caller.tenant_id
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        observed.models.append((request.url.path, caller.invocation_id, await request.json()))
        events = (
            [{"choices": [{"delta": {"content": "answer"}}]}]
            if request.url.path.endswith("completions")
            else [{"type": "response.output_text.delta", "delta": "answer"}, {"type": "response.completed", "response": {"status": "completed"}}]
        )
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return StreamingResponse(iter([stream]), media_type="text/event-stream")

    class Response(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status, self.headers = response.status_code, response.headers
            self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Mock()))

        def isclosed(self):
            return self.tell() == len(self.getvalue())

    with TestClient(app) as client:
        original_opener = transport.urllib.request.build_opener

        class Opener:
            def open(self, request, timeout):
                if request.full_url.startswith("http://127.0.0.1"):
                    return original_opener().open(request, timeout=timeout)
                destination = request.full_url
                headers = {name.lower(): value for name, value in request.header_items()}
                parsed = urllib.parse.urlsplit(destination)
                target = urllib.parse.urlsplit(configuration["native_target"])
                if parsed.netloc == target.netloc:
                    stage = target.path.rstrip("/")
                    assert parsed.path.startswith(stage + "/")
                    path = parsed.path[len(stage) :]
                    headers["x-caller-identity"] = ""
                    headers["x-adp-edge-provenance"] = ""
                    if path.startswith(("/agent/", "/internal/")):
                        signed_headers = {name: value for name, value in request.header_items() if name.lower() != "authorization"}
                        signed = AWSRequest(method=request.method, url=request.full_url, data=request.data, headers=signed_headers)
                        signed.context["timestamp"] = headers["x-amz-date"]
                        auth = SigV4Auth(Credentials("testing", "testing", "testing"), "execute-api", configuration["aws_region"])
                        signature = auth.signature(auth.string_to_sign(signed, auth.canonical_request(signed)), signed)
                        expected = (
                            f"AWS4-HMAC-SHA256 Credential={auth.scope(signed)}, "
                            f"SignedHeaders={auth.signed_headers(auth.headers_to_sign(signed))}, Signature={signature}"
                        )
                        if headers.get("authorization") != expected or headers.get("x-amz-security-token") != "testing":
                            raise urllib.error.HTTPError(request.full_url, 403, "invalid signature", {}, io.BytesIO())
                        headers["x-caller-identity"] = role + "/session"
                        headers["x-adp-edge-provenance"] = edge_settings.apigw_provenance_secret
                        if path.startswith("/agent/"):
                            path = path.removeprefix("/agent")
                    destination = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
                    observed.forwarded.append((request.full_url, path))
                response = client.request(request.method, destination, content=request.data, headers=headers)
                if "/campaign-enrollment/" in request.full_url:
                    assert len(json.loads((tmp_path / "native-test.journal.json").read_text())["native_intents"]) == len(configuration["fixtures"])
                    if observed.lost_enrollment:
                        observed.lost_enrollment = False
                        raise TimeoutError("lost enrollment reply")
                    if observed.wrong_enrollment:
                        response = type(response)(200, json={"status": "prepared", "operation_id": "wrong"})
                if "/campaign-stop/" in request.full_url and response.status_code == 202 and observed.auto_exit:
                    exit_pod(cluster, request.full_url.split("/invocations/")[1].split("/")[0])
                if response.status_code >= 400:
                    raise urllib.error.HTTPError(
                        request.full_url, response.status_code, "fixture response", response.headers, io.BytesIO(response.content)
                    )
                return Response(response)

        monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *args: Opener())
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
        import boto3

        monkeypatch.setattr(
            boto3, "client", Mock(return_value=Mock(get_caller_identity=Mock(return_value={"Account": "000000000000", "Arn": role + "/session"})))
        )

        def selected(command, *, input, timeout):
            assert command[:7] == ["adp-cred", "assume", "--service", "aws", "--label", "native-connection", "--exec"]
            assert "adpr1." not in " ".join(command)
            document = json.loads(input)
            observed.signed.append(document)
            return SimpleNamespace(returncode=0, stdout=json.dumps(signer.sign(document)))

        monkeypatch.setattr(transport, "run_selected", selected)
        yield SimpleNamespace(
            controller=controller,
            recovery=recovery,
            transport=transport,
            signer=signer,
            config=configuration,
            root=tmp_path,
            observed=observed,
            native=native,
            cluster=cluster,
        )


def campaign(harness, cell=None):
    recovery = harness.config
    if cell is None:
        recovery["fixtures"] = recovery["fixtures"][:1]
    recovery_path = harness.root / "recovery.json"
    private_write(recovery_path, recovery)
    config = {
        "schema_version": 1,
        "scenario": "native-checkpoint",
        "seed": 1,
        "recovery_owner": "test-owner",
        **{key: recovery[key] for key in ("target", "revision", "approved_account", "approved_role", "campaign", "journal_dir")},
        "campaign_owner": recovery["owner"],
        "recovery_config": str(recovery_path),
        "evidence_dir": str(harness.root),
        "identities": [
            {"id": fixture["binding"], "kind": "agent", **{key: fixture[key] for key in ("tenant", "root_human", "ownership_receipt", "token_file")}}
            for fixture in recovery["fixtures"]
        ],
        "models": [
            {
                "api": api,
                "model": "test-model",
                "sdk_version": "test-sdk",
                "max_input_tokens": 100,
                "max_output_tokens": 10,
                "price_per_input_token": 0.001,
                "price_per_output_token": 0.002,
            }
            for api in ("chat", "responses")
        ],
        "limits": {
            "attempts": 40,
            "active_seconds": 15,
            "drain_seconds": 5,
            "input_tokens": 4000,
            "output_tokens": 400,
            "max_spend": 5,
            "setup_seconds": 15,
            "cleanup_seconds": 5,
        },
        "slos": {"ttft_ms": 3000, "http_ms": 5000, "terminal_ms": 3000},
        "stages": [{"name": api, "required": True, "desired_sessions": 1, "min_dwell_seconds": 0, "spawn_rate": 1} for api in ("chat", "responses")],
    }
    if cell:
        receipt = harness.root / "capacity.json"
        private_write(
            receipt,
            {
                **{key: config[key] for key in ("target", "revision", "approved_account", "approved_role")},
                "status": "pass",
                "protected_receipt": "fixture-only-not-live-proof",
                "safe_inflight_sessions": 2,
                "health": {"oom": False, "restarts": 0, "dependencies_ok": True},
            },
        )
        config.update(
            plan="comparison",
            comparison_cell=cell,
            ramp_api="chat",
            think_time_seconds=0.1,
            safe_inflight_sessions=2,
            qualified_capacity_receipt=str(receipt),
            stages=[{"name": cell, "ramp": True, "required": True, "desired_sessions": 2, "spawn_rate": 50, "min_dwell_seconds": 0.3}],
        )
    path = harness.root / "campaign.json"
    private_write(path, config)
    return harness.controller.load_config(path)


@pytest.mark.parametrize("cell", [None, "distinct_agents", "fan_out"])
def test_production_configuration_runs_native_streams_under_shared_permits_and_cleans(harness, cell):
    config = campaign(harness, cell)
    original = copy.deepcopy(harness.cluster.pods["unrelated"])
    execute = harness.controller.checkpoint if cell is None else harness.controller.model_ramp
    assert execute(config) == 0
    evidence = json.loads((harness.root / "checkpoint-results.json").read_text())
    assert evidence["success"] is True
    assert evidence["cleanup"]["status"] == "clean"
    assert evidence["campaign"]["physical_attempts"] == len(harness.observed.models)
    assert 2 <= len(harness.observed.models) <= config["limits"]["attempts"]
    assert {invocation for path, invocation, payload in harness.observed.models} == {identity["id"] for identity in config["identities"]}
    assert {path for path, invocation, payload in harness.observed.models} == (
        {"/v1/chat/completions", "/openai/v1/responses"} if cell is None else {"/v1/chat/completions"}
    )
    reported = harness.controller.load_config(harness.root / "campaign.json", for_report=True)
    assert evidence["config_hash"] == hashlib.sha256(json.dumps(reported, sort_keys=True).encode()).hexdigest()
    assert harness.cluster.pods["unrelated"] == original
    for fixture in harness.config["fixtures"]:
        assert not Path(fixture["token_file"]).exists()
        assert Path(fixture["run_credential_file"]).exists()
        assert FINALIZER not in harness.cluster.pods[fixture["binding"]]["metadata"]["finalizers"]
        with pytest.raises((BootstrapRefusedError, ExecutionStateError, WorkloadRefusedError)):
            harness.native.runtime.authenticate(harness.native.credentials[fixture["binding"]], fixture["binding"])


@pytest.mark.parametrize("stage", ["", "/test", "/test/"])
def test_native_api_routes_are_signed_with_stage_and_agent_prefix(harness, stage):
    harness.config["native_target"] += stage
    config = campaign(harness)
    assert harness.controller.checkpoint(config) == 0
    target = harness.config["native_target"].rstrip("/")
    expected_models = {target + "/agent/v1/chat/completions", target + "/agent/openai/v1/responses"}
    assert {document["url"] for document in harness.observed.signed if document["method"] == "POST"} == expected_models
    assert {document["url"] for document in harness.observed.signed if document["method"] == "GET"} == {
        target + "/internal/v1/agent/campaign-binding"
    }
    assert set(harness.observed.forwarded) == {
        (target + "/agent/v1/chat/completions", "/v1/chat/completions"),
        (target + "/agent/openai/v1/responses", "/openai/v1/responses"),
        (target + "/internal/v1/agent/campaign-binding", "/internal/v1/agent/campaign-binding"),
    }
    assert {path for path, invocation, payload in harness.observed.models} == {"/v1/chat/completions", "/openai/v1/responses"}


@pytest.mark.parametrize("route", ["/v1/chat/completions", "/openai/v1/responses"])
def test_public_native_signature_cannot_authenticate_a_model_request(harness, route):
    harness.config["native_target"] += "/test/"
    config = campaign(harness)
    journal = harness.controller.setup_fixtures(config)
    fixture = harness.config["fixtures"][0]
    credentials = {"run_credential": harness.native.credentials["root"], "workload_token": "root"}
    request = harness.transport.signed_request(harness.config, fixture, "POST", route, {}, credentials, 5)
    request.add_header("X-Caller-Identity", harness.config["native_approved_role"] + "/session")
    request.add_header("X-Adp-Edge-Provenance", "test-edge-proof")
    with pytest.raises(urllib.error.HTTPError) as refused:
        harness.transport.urllib.request.build_opener().open(request, timeout=5)
    assert refused.value.code == 401
    assert json.loads(refused.value.read())["detail"]["error"] == "invalid_token_format"
    assert harness.observed.models == []
    backend = harness.transport.NativeFixtureBackend(harness.config)
    backend.deadline = time.time() + 5
    assert journal.recover(backend, 5, force=True)["status"] == "clean"


def test_native_model_path_cannot_change_after_signing(harness):
    harness.config["native_target"] += "/test/"
    config = campaign(harness)
    journal = harness.controller.setup_fixtures(config)
    fixture = harness.config["fixtures"][0]
    credentials = {"run_credential": harness.native.credentials["root"], "workload_token": "root"}
    request = harness.transport.signed_request(harness.config, fixture, "POST", "/agent/v1/chat/completions", {}, credentials, 5)
    request.full_url = request.full_url.replace("/v1/chat/completions", "/openai/v1/responses")
    with pytest.raises(urllib.error.HTTPError) as refused:
        harness.transport.urllib.request.build_opener().open(request, timeout=5)
    assert refused.value.code == 403
    assert harness.observed.models == []
    backend = harness.transport.NativeFixtureBackend(harness.config)
    backend.deadline = time.time() + 5
    assert journal.recover(backend, 5, force=True)["status"] == "clean"


@pytest.mark.parametrize("interruption", ["partial_setup", "active", "cache_replaced"])
def test_restart_cleanup_tracks_all_descendants_and_retains_unknown_accounting(harness, interruption):
    config = campaign(harness, "fan_out")
    backend = harness.transport.NativeFixtureBackend(harness.config)
    backend.deadline = time.time() + 15
    journal = harness.recovery.RecoveryJournal(harness.root, "native-test", "test-owner")
    journal.bind_recovery(harness.config, initialize=True)
    harness.observed.lost_enrollment = interruption == "partial_setup"
    if interruption == "partial_setup":
        with pytest.raises(TimeoutError):
            backend.setup_native(journal)
    else:
        backend.setup_native(journal)
        identity = config["identities"][0]
        guard = harness.controller.CampaignGuard(config["limits"], config["stages"])
        assert guard.start_stage("fan_out", 2)
        with harness.controller.running_permits(guard, journal) as worker:
            attempt = worker.permit(100, 10, 0.12)
            row = harness.controller.stream_request(
                config,
                guard,
                identity,
                config["models"][0],
                Path(identity["token_file"]).read_text(),
                time.monotonic(),
                dispatch=lambda: worker.dispatch(attempt),
            )
        assert row["success"] is True
        assert len(harness.observed.models) == 1
        if interruption == "cache_replaced":
            Path(config["identities"][0]["token_file"]).unlink()
            private_write(Path(config["identities"][0]["token_file"]), "operator-replacement")
    record = json.loads(journal.path.read_text())
    assert record["version"] == 3
    assert {intent["fixture"] for intent in record["native_intents"]} == {"root", "child"}
    assert "adpr1." not in journal.path.read_text()
    for fixture in harness.config["fixtures"]:
        Path(fixture["run_credential_file"]).unlink()
        Path(fixture["workload_token_file"]).unlink()
    with journal._locked() as record:
        record["lease_until"] = 0
        journal._save(record)
    recovered = harness.recovery.resume_cleanup(harness.root / "recovery.json")
    receipt = json.loads(journal.path.read_text())["receipt"]
    assert recovered == (0 if interruption == "partial_setup" else 5)
    assert receipt["accounting"] == ("observed" if interruption == "partial_setup" else "unknown")
    for invocation in ("root", "child"):
        assert harness.native.store._read("TENANT#tenant", "EXEC#" + invocation)["status"] == {"S": "revoked"}
        assert FINALIZER not in harness.cluster.pods[invocation]["metadata"]["finalizers"]
    if interruption == "cache_replaced":
        assert Path(config["identities"][0]["token_file"]).read_text() == "operator-replacement"
        assert receipt["holds"]


def test_human_tokens_and_wrong_enrollment_receipts_fail_closed(harness):
    campaign(harness)
    backend = harness.transport.NativeFixtureBackend(harness.config)
    backend.deadline = time.time() + 15
    fixture = harness.config["fixtures"][0]
    with pytest.raises(harness.recovery.RecoveryError, match="never human"):
        backend.native_binding(fixture, {"run_credential": "human-jwt", "workload_token": "root"})
    journal = harness.recovery.RecoveryJournal(harness.root, "native-test", "test-owner")
    harness.observed.wrong_enrollment = True
    with pytest.raises(harness.recovery.RecoveryError, match="enrollment"):
        backend.setup_native(journal)
    assert not Path(fixture["token_file"]).exists()


def test_same_epoch_refresh_works_but_execution_replacement_cannot_send_a_model_request(harness):
    config = campaign(harness)
    harness.controller.setup_fixtures(config)
    fixture = harness.config["fixtures"][0]
    _, _, record, _ = harness.native.runtime.authenticate(harness.native.credentials["root"], "root")
    refreshed = issue_bound_credential(record, now=datetime.now(UTC), env=ENV)["credential"]
    private_write(Path(fixture["run_credential_file"]), refreshed)
    cache = json.loads(Path(fixture["token_file"]).read_text())
    request = harness.transport.model_request(harness.config, config["identities"][0], "/v1/chat/completions", {}, cache, 5)
    assert request.get_header("X-adp-run-credential") == refreshed
    execution = harness.native.store._read("TENANT#tenant", "EXEC#root")
    execution["current_credential_epoch"] = {"N": "2"}
    harness.native.client.put_item(TableName=harness.native.store.table, Item=execution)
    with pytest.raises((urllib.error.HTTPError, harness.recovery.RecoveryError)):
        harness.transport.model_request(harness.config, config["identities"][0], "/v1/chat/completions", {}, cache, 5)
    assert harness.observed.models == []


def test_native_attempt_budget_never_allows_a_qualifying_retry(harness):
    config = campaign(harness, "fan_out")
    config["limits"]["attempts"] = 2
    config["stages"][0]["min_dwell_seconds"] = 3
    assert harness.controller.model_ramp(config) != 0
    result = json.loads((harness.root / "checkpoint-results.json").read_text())
    assert len(harness.observed.models) == 2
    assert result["campaign"]["physical_attempts"] == 2
    assert result["campaign"]["stop_reason"] == "attempt_budget"
    assert result["success"] is False
    assert result["cleanup"]["status"] == "clean"


def test_native_signing_cannot_send_with_a_permit_that_expired_during_preparation(harness, monkeypatch):
    config = campaign(harness)
    worker_type = importlib.import_module("campaign_guard").WorkerClient
    dispatch = worker_type.dispatch
    checked = []

    def expired(worker, attempt_id):
        assert harness.observed.signed[-1]["method"] == "POST"
        assert harness.observed.signed[-1]["url"].endswith("/v1/chat/completions")
        checked.append(attempt_id)
        worker.expires[attempt_id] = time.time() - 1
        return dispatch(worker, attempt_id)

    monkeypatch.setattr(worker_type, "dispatch", expired)
    assert harness.controller.checkpoint(config) != 0
    assert len(checked) == 1
    assert harness.observed.models == []
    result = json.loads((harness.root / "checkpoint-results.json").read_text())
    assert result["success"] is False
    assert result["cleanup"]["status"] == "clean"


@pytest.mark.parametrize("problem", ["missing_parent", "cycle", "wrong_owner", "human_identity", "shared_credentials"])
def test_native_configuration_rejects_unbound_inventory_without_network_operations(harness, problem):
    campaign(harness, "fan_out")
    recovery_path = harness.root / "recovery.json"
    recovery = json.loads(recovery_path.read_text())
    if problem == "missing_parent":
        recovery["fixtures"][1]["parent"] = "absent"
    elif problem == "cycle":
        recovery["fixtures"][0]["parent"] = "child"
    elif problem == "wrong_owner":
        recovery["fixtures"][1]["root_human"] = "other-human"
    elif problem == "shared_credentials":
        recovery["fixtures"][1]["run_credential_file"] = recovery["fixtures"][0]["run_credential_file"]
    else:
        config = json.loads((harness.root / "campaign.json").read_text())
        config["identities"][0]["kind"] = "developer"
        private_write(harness.root / "campaign.json", config)
    private_write(recovery_path, recovery)
    with pytest.raises((harness.controller.ConfigError, harness.recovery.RecoveryError)):
        harness.controller.load_config(harness.root / "campaign.json")
    assert harness.observed.signed == []
    assert harness.cluster.writes == []


@pytest.mark.parametrize("problem", ["account", "role", "missing_credentials", "empty_credentials"])
def test_signer_requires_the_selected_account_role_and_explicit_session(harness, monkeypatch, problem):
    document = {
        "url": "https://native.example.test/v1/chat/completions",
        "method": "POST",
        "body": "{}",
        "headers": {},
        "region": "us-east-1",
        "account": harness.config["approved_account"],
        "role": harness.config["native_approved_role"],
    }
    if problem == "missing_credentials":
        monkeypatch.delenv("AWS_SESSION_TOKEN")
    elif problem == "empty_credentials":
        monkeypatch.setenv("AWS_SESSION_TOKEN", "")
    else:
        document[problem] = "different-connection"
    with pytest.raises((ValueError, KeyError)):
        harness.signer.sign(document)


def test_pending_stop_is_bounded_and_never_relabels_revocation_as_exit(harness):
    config = campaign(harness, "fan_out")
    journal = harness.controller.setup_fixtures(config)
    backend = harness.transport.NativeFixtureBackend(harness.config)
    harness.observed.auto_exit = False
    backend.deadline = time.time() + 0.2
    receipt = journal.recover(backend, 0.2, force=True)
    assert receipt["status"] == "hold"
    assert {hold["binding"] for hold in receipt["holds"] if hold.get("kind") == "workload"} == {"root", "child"}
    assert all(Path(fixture["token_file"]).exists() for fixture in harness.config["fixtures"])
    assert all(FINALIZER in harness.cluster.pods[invocation]["metadata"]["finalizers"] for invocation in ("root", "child"))


def test_older_backend_cannot_report_native_journal_clean(harness):
    config = campaign(harness)
    journal = harness.controller.setup_fixtures(config)
    with pytest.raises(harness.recovery.RecoveryError, match="compatible"):
        journal.recover(object(), 1, force=True)


def test_cleanup_refuses_a_newer_attempt_after_native_setup(harness):
    config = campaign(harness)
    journal = harness.controller.setup_fixtures(config)
    execution = harness.native.store._read("TENANT#tenant", "EXEC#root")
    execution["current_attempt"] = {"N": "2"}
    harness.native.client.put_item(TableName=harness.native.store.table, Item=execution)
    original = copy.deepcopy(harness.cluster.pods["root"])
    backend = harness.transport.NativeFixtureBackend(harness.config)
    backend.deadline = time.time() + 2
    receipt = journal.recover(backend, 2, force=True)
    assert receipt["status"] == "hold"
    assert harness.cluster.pods["root"] == original
    assert Path(config["identities"][0]["token_file"]).exists()

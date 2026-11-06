"""The installed checker refuses old/changed code before any fixture mutation."""

import importlib.util
import io
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/qualify-consumers.py"
spec = importlib.util.spec_from_file_location("qualify_consumers", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def installed():
    scope = dict(
        tenant_id="tenant",
        product_id="product",
        project_id="project",
        owner_id="owner",
        visibility="only_me",
        relationship_revision="1",
    )
    result = dict(
        tenant_id="tenant",
        project_id="project",
        source_connection_id="connection",
        record_id="result",
        record_type="transform_result",
        record_revision="1",
    )
    payload = dict(
        scope=scope,
        result_ref=result,
        contribution_id="publication",
        kind="transform_result",
        run_id="run",
        schema_version="1.0",
        result={"original": "retained"},
    )
    case = dict(
        scope=scope,
        result_ref=result,
        contribution_id="publication",
        kind="transform_result",
        run_id="run",
        schema_version="1.0",
        service_kind="transform",
        destination="product-context",
        contribution_hash=checker.digest(payload),
    )
    fixture = dict(
        protocol=checker.PROTOCOL,
        expected_account="000000000000",
        region="us-east-1",
        repair_function="arn:aws:lambda:us-east-1:000000000000:function:example-repair",
        expected_role_arn="arn:aws:iam::000000000000:role/example-repair",
        expected_code_sha256="A" * 43 + "=",
        service_id="transform",
        cases=[case],
        gateway_url="https://gateway.example.test",
        owner_token_file="/private/owner.json",
        foreign_token_file="/private/foreign.json",
        reads=[
            dict(
                contribution_id="publication",
                task_id="later",
                foreign_task_id="foreign-task",
                foreign_product_id="foreign-product",
                foreign_project_id="foreign-project",
            )
        ],
    )
    config = dict(
        FunctionArn=fixture["repair_function"],
        CodeSha256=fixture["expected_code_sha256"],
        Role=fixture["expected_role_arn"],
        Handler="l5_services.workers.recovery.lambda_handler",
        State="Active",
        LastUpdateStatus="Successful",
        RevisionId="revision",
        Version="$LATEST",
        Environment={
            "Variables": {
                "L5_SERVICE_ID": "transform",
                "L5_CONSUMER_QUALIFICATION_CASES": json.dumps([case]),
            }
        },
    )
    calls = []
    state = {"protocol": True, "revoked": False, "account": "000000000000"}
    receipt = dict(receipt_id="receipt", contribution_id="publication")
    usage = dict(
        scope=scope,
        result_ref=result,
        contribution_id="publication",
        task_id="later",
        actor_id="owner",
        citation="retained-original",
        receipt_id="task-receipt",
    )

    def invoke(**kwargs):
        event = json.loads(kwargs["Payload"])
        calls.append(event)
        if "l5_runtime_probe" in event:
            output = {"service_id": "transform"}
            if state["protocol"]:
                output["consumer_fixture_protocol"] = checker.PROTOCOL
        else:
            phase = event["phase"]
            if phase == "invalidate":
                state["revoked"] = True
            output = dict(
                protocol=checker.PROTOCOL,
                service_id="transform",
                phase=phase,
                cases_digest=event["cases_digest"],
                observations=[
                    dict(
                        contribution_id="publication",
                        contribution_hash=case["contribution_hash"],
                        destination="product-context",
                        receipt=receipt,
                        native_fingerprint="original-native",
                        state="invalidated" if state["revoked"] else "published",
                        invalidation_receipt={"receipt_id": "tombstone"}
                        if state["revoked"]
                        else None,
                        applied_watermark=2 if state["revoked"] else 0,
                        denied_status=422
                        if phase == "invalidate"
                        else 404
                        if phase == "late-replay"
                        else None,
                    )
                ],
            )
        return dict(
            StatusCode=200,
            ExecutedVersion="$LATEST",
            Payload=io.BytesIO(json.dumps(output).encode()),
        )

    def http_response(request):
        if (
            request.headers["authorization"] == "Bearer foreign"
            or "/foreign-" in request.url.path
            or state["revoked"]
        ):
            return httpx.Response(404, json={})
        value = (
            usage
            if "/tasks/" in request.url.path
            else receipt
            if request.url.path.endswith("/receipt")
            else {**payload, "contribution_hash": case["contribution_hash"]}
        )
        return httpx.Response(200, json=value)

    aws = SimpleNamespace(
        client=lambda name, **kwargs: (
            SimpleNamespace(
                get_function_configuration=lambda **kwargs: deepcopy(config),
                invoke=invoke,
            )
            if name == "lambda"
            else SimpleNamespace(
                get_caller_identity=lambda: {"Account": state["account"]}
            )
        )
    )
    with httpx.Client(
        base_url=fixture["gateway_url"], transport=httpx.MockTransport(http_response)
    ) as http:
        yield SimpleNamespace(
            fixture=fixture,
            config=config,
            calls=calls,
            state=state,
            checker=checker.Checker(fixture, aws, http, "owner", "foreign"),
        )


def test_two_phases_require_observed_baseline_and_preserve_native_receipt(installed):
    baseline = installed.checker.run("admit")
    assert baseline["phase_passed"] is True
    assert baseline["bounded_consumer_checks_complete"] is False
    result = installed.checker.run("invalidate", baseline)
    assert result["bounded_consumer_checks_complete"] is True
    assert result["native_acceptance_assessed"] is False
    assert (
        result["worker_observations"]["publication"]["receipt"]
        == baseline["worker_observations"]["publication"]["receipt"]
    )


@pytest.mark.parametrize(
    "changed", ["account", "code", "role", "selection", "broad_contract", "protocol"]
)
def test_changed_installation_or_old_worker_never_receives_fixture_event(
    installed, changed
):
    if changed == "account":
        installed.state["account"] = "999999999999"
    elif changed == "code":
        installed.config["CodeSha256"] = "B" * 43 + "="
    elif changed == "role":
        installed.config["Role"] += "-other"
    elif changed == "selection":
        installed.config["Environment"]["Variables"][
            "L5_CONSUMER_QUALIFICATION_CASES"
        ] = "[]"
    elif changed == "broad_contract":
        installed.config["Environment"]["Variables"]["L5_CONSUMER_CONTRACTS"] = (
            '{"transform":{}}'
        )
    else:
        installed.state["protocol"] = False
    with pytest.raises(checker.CheckFailed):
        installed.checker.run("admit")
    assert not any("l5_consumer_fixture" in event for event in installed.calls)


def test_invalidation_without_passing_baseline_cannot_mutate(installed):
    with pytest.raises(checker.CheckFailed, match="passing admission"):
        installed.checker.run("invalidate")
    assert installed.calls == [{"l5_runtime_probe": 1}]


def test_private_inputs_reject_public_permissions_and_repository_paths(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(checker.CheckFailed, match="0600"):
        checker.read_json(str(path))
    path.chmod(0o600)
    assert checker.read_json(str(path)) == {}
    with pytest.raises(checker.CheckFailed, match="outside the checkout"):
        checker.private_path(str(SCRIPT))

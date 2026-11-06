"""Offline B01 packet grading; fixtures never substitute for deployed evidence."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding.product_evidence import (
    PACKET_SCHEMA,
    SCENARIOS,
    build_packet,
)
from tests.e2e.product_onboarding.product_publish import main, publish

SOURCE = "a" * 40
WORKFLOW = "b" * 40
REQUIREMENTS = "c" * 40
IMAGE = "sha256:" + "d" * 64
CLI = "e" * 64


def scoped_evaluator(cfg):
    account = str(cfg["platform_account"])
    return {
        "aws": SimpleNamespace(
            call=lambda *_args, **_kwargs: {
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/adp-cli-uplift-eval-orchestrator/fixture",
            }
        )
    }


def complete_evidence():
    cases = {}
    cleanup = {}
    for name, (ac_id, scenario_id) in SCENARIOS.items():
        attempts = []
        if name == "api_recovery_cleanup":
            attempts.append({"attempt_id": f"{name}-1", "result": "interrupted",
                             "expected": "original write", "actual": "response lost",
                             "evidence_ref": "private:interrupt-attempt"})
        attempts.append({"attempt_id": f"{name}-2", "result": "pass",
                         "expected": "one committed mutation", "actual": "one mutation observed",
                         "evidence_ref": f"private:{name}-attempt"})
        cases[name] = {
            "ac_id": ac_id, "scenario_id": scenario_id,
            "run_id": "test-run-0001", "test_ref": f"test_v01.py::{name}",
            "expected": "expected Product behavior", "actual": "observed Product behavior",
            "status": "pass", "evidence_ref": f"private:{name}-report",
            "readback_ref": f"private:{name}-readback", "cleanup_ref": f"private:{name}-cleanup",
            "attempts": attempts,
        }
        cleanup[name] = {"status": "verified", "receipt_ref": f"private:{name}-cleanup",
                         "owned_absent": True, "shared_unchanged": True}
    return {
        "run_id": "test-run-0001", "plan_revision": "reviewed-plan-v1",
        "report_artifact": "private:b01/attempt-1/packet.json",
        "identities": {"requirements_revision": REQUIREMENTS, "source_revision": SOURCE,
                       "workflow_revision": WORKFLOW, "schema_head": "schema-01",
                       "api_image_digest": IMAGE, "cli_checksum": CLI},
        "deployment": {"receipt_ref": "private:d01-deployment",
                       "observation_ref": "private:independent-readback",
                       "observed": {"source_revision": SOURCE, "workflow_revision": WORKFLOW,
                                    "schema_head": "schema-01", "api_image_digest": IMAGE,
                                    "cli_checksum": CLI,
                                    "serving_pods": [{"pod_ref": "private:pod-1", "source_revision": SOURCE,
                                                      "api_image_digest": IMAGE,
                                                      "product_profile_enabled": True,
                                                      "product_onboarding_enabled": False}]}},
        "surfaces": {"api": {"status": "pass", "source_revision": SOURCE,
                             "expected": "saved Product", "actual": "fresh readback",
                             "artifact_ref": "private:api-report", "readback_ref": "private:api-readback"},
                     "cli": {"status": "pass", "source_revision": SOURCE,
                             "expected": "installed Product command", "actual": "same saved Product",
                             "artifact_ref": "private:cli-report", "command": "adp product show ...",
                             "stdout_ref": "private:cli-stdout", "stderr_ref": "private:cli-stderr",
                             "exit_code": 0, "cli_checksum": CLI}},
        "cases": cases, "cleanup": cleanup,
    }


@pytest.fixture
def evidence():
    return complete_evidence()


def test_complete_b01_inputs_produce_versioned_packet_with_all_attempts(evidence):
    evidence["owner_token"] = "secret-not-for-packets"
    result = build_packet(evidence)
    assert result["evidence_schema"] == PACKET_SCHEMA and result["status"] == "pass"
    assert result["packet_revision"].startswith("sha256:")
    assert len(result["packet_revision"]) == 71
    assert list(result["cases"]) == list(SCENARIOS)
    assert len(result["cases"]["api_recovery_cleanup"]["attempts"]) == 2
    assert "owner_token" not in result and "secret-not-for-packets" not in str(result)
    assert build_packet(evidence) == result
    evidence["cases"]["api_recovery_cleanup"]["attempts"][0]["result"] = "fail"
    assert result["cases"]["api_recovery_cleanup"]["attempts"][0]["result"] == "interrupted"
    assert build_packet(evidence)["packet_revision"] != result["packet_revision"]


@pytest.mark.parametrize("alter,expected", [
    (lambda data: data["cases"].pop("api_denial_oracle"), "AC/scenario mapping"),
    (lambda data: data["cases"]["api_product_lifecycle"].update(status="skip"), "did not pass"),
    (lambda data: data["cases"]["api_product_lifecycle"].update(attempts=[]), "attempts are missing"),
    (lambda data: data["cases"]["api_recovery_cleanup"]["attempts"].pop(0), "no recorded interrupted"),
    (lambda data: data["cases"]["api_recovery_cleanup"]["attempts"][-1].update(result="not_run"), "not pass"),
    (lambda data: data["cases"]["api_denial_oracle"].update(scenario_id="SCN-01"), "mapping"),
    (lambda data: data["cases"]["api_denial_oracle"].update(readback_ref=""), "readback_ref"),
    (lambda data: data["cleanup"]["api_recovery_cleanup"].update(owned_absent=False), "cleanup"),
    (lambda data: data["cleanup"]["api_denial_oracle"].update(shared_unchanged=False), "cleanup"),
    (lambda data: data["cleanup"]["api_product_lifecycle"].update(receipt_ref="private:wrong"), "receipt"),
    (lambda data: data["deployment"]["observed"].update(source_revision="f" * 40), "differs"),
    (lambda data: data["deployment"]["observed"].update(schema_head="stale"), "differs"),
    (lambda data: data["deployment"]["observed"].update(serving_pods=[]), "serving-pod"),
    (lambda data: data["deployment"]["observed"]["serving_pods"][0].update(product_profile_enabled=False), "core Product"),
    (lambda data: data["deployment"]["observed"]["serving_pods"][0].update(product_onboarding_enabled=True), "unsupported onboarding"),
    (lambda data: data["surfaces"].pop("cli"), "cli surface"),
    (lambda data: data["surfaces"]["cli"].update(cli_checksum="f" * 64), "CLI exit or checksum"),
    (lambda data: data.update(report_artifact=""), "report_artifact"),
    (lambda data: data["identities"].update(api_image_digest="invalid"), "artifact identity"),
    (lambda data: data["identities"].update(requirements_revision="main"), "full revision"),
])
def test_required_missing_failed_or_stale_evidence_is_incomplete(evidence, alter, expected):
    original = deepcopy(evidence)
    alter(evidence)
    result = build_packet(evidence)
    assert result["status"] == "incomplete"
    assert any(expected in issue for issue in result["issues"])
    assert result["cases"] == evidence["cases"]
    assert result["packet_revision"] != build_packet(original)["packet_revision"]


def test_repaired_attempt_retains_failed_attempt_but_requires_latest_pass(evidence):
    case = evidence["cases"]["api_denial_oracle"]
    case["attempts"].insert(0, {"attempt_id": "failure-1", "result": "fail",
                                "expected": "412", "actual": "200", "evidence_ref": "private:failure"})
    assert build_packet(evidence)["status"] == "pass"
    case["attempts"][-1]["result"] = "fail"
    result = build_packet(evidence)
    assert result["status"] == "incomplete"
    assert len(result["cases"]["api_denial_oracle"]["attempts"]) == 2


def test_optional_ui_evidence_must_pass_when_included(evidence):
    evidence["surfaces"]["ui"] = {"status": "pass", "source_revision": SOURCE,
                                    "artifact_ref": "private:ui-report", "screenshot_ref": "private:ui-screenshot"}
    assert build_packet(evidence)["status"] == "pass"
    evidence["surfaces"]["ui"]["screenshot_ref"] = ""
    assert build_packet(evidence)["status"] == "incomplete"


def test_malformed_attempt_does_not_fabricate_a_pass(evidence):
    evidence["cases"]["api_denial_oracle"]["attempts"].insert(0, {"attempt_id": [], "result": "pass"})
    result = build_packet(evidence)
    assert result["status"] == "incomplete"
    assert any("invalid attempt ID" in issue for issue in result["issues"])


def dispatch_for(evidence):
    return {
        "selected_cases": list(SCENARIOS), "expected_revision": SOURCE,
        "observed_revision": SOURCE, "workflow_revision": WORKFLOW,
        "run_id": evidence["run_id"],
        "observation_ref": evidence["deployment"]["observation_ref"],
        "status": "pass", "cleanup_verified": True,
    }


def write_inputs(tmp_path, evidence, dispatch):
    input_path, dispatch_path = tmp_path / "inputs.json", tmp_path / "dispatch.json"
    input_path.write_text(json.dumps(evidence))
    dispatch_path.write_text(json.dumps(dispatch))
    return input_path, dispatch_path


def test_workflow_publisher_seals_matching_inputs_and_report_path(tmp_path, evidence):
    output = tmp_path / "product-b01.json"
    evidence["report_artifact"] = str(output)
    source, attempt = write_inputs(tmp_path, evidence, dispatch_for(evidence))
    report = publish(source, attempt, output, selection=",".join(SCENARIOS),
                     source_revision=SOURCE, workflow_revision=WORKFLOW)
    assert report["status"] == "pass"
    assert report["dispatch"]["observation_ref"] == evidence["deployment"]["observation_ref"]
    assert json.loads(output.read_text()) == report
    assert main(["--inputs", str(source), "--dispatch-report", str(attempt),
                 "--output", str(output), "--cases", ",".join(SCENARIOS),
                 "--expected-revision", SOURCE, "--workflow-revision", WORKFLOW]) == 0


@pytest.mark.parametrize("alter,expected", [
    (lambda report: report.update(status="failure"), "API driver failed"),
    (lambda report: report.update(cleanup_verified=False), "cleanup"),
    (lambda report: report.update(observed_revision="f" * 40), "observe"),
    (lambda report: report.update(observation_ref=None), "independent"),
    (lambda report: report.update(observation_ref="private:other"), "independent"),
    (lambda report: report.update(run_id="other-run"), "selected release"),
    (lambda report: report.update(workflow_revision="f" * 40), "selected release"),
])
def test_publisher_rejects_failed_permission_or_stale_dispatch(tmp_path, evidence, alter, expected):
    output = tmp_path / "product-b01.json"
    evidence["report_artifact"] = str(output)
    dispatch = dispatch_for(evidence)
    alter(dispatch)
    source, attempt = write_inputs(tmp_path, evidence, dispatch)
    report = publish(source, attempt, output, selection=",".join(SCENARIOS),
                     source_revision=SOURCE, workflow_revision=WORKFLOW)
    assert report["status"] == "incomplete"
    assert any(expected in reason for reason in report["issues"])
    assert json.loads(output.read_text())["status"] == "incomplete"


@pytest.mark.parametrize("missing", ["inputs", "dispatch"])
def test_publisher_writes_incomplete_report_when_upstream_artifact_missing(tmp_path, evidence, missing):
    output = tmp_path / "product-b01.json"
    evidence["report_artifact"] = str(output)
    source, attempt = write_inputs(tmp_path, evidence, dispatch_for(evidence))
    (source if missing == "inputs" else attempt).unlink()
    report = publish(source, attempt, output, selection=",".join(SCENARIOS),
                     source_revision=SOURCE, workflow_revision=WORKFLOW)
    assert report["status"] == "incomplete"
    assert json.loads(output.read_text())["status"] == "incomplete"
    assert main(["--inputs", str(source), "--dispatch-report", str(attempt),
                 "--output", str(output), "--cases", ",".join(SCENARIOS),
                 "--expected-revision", SOURCE, "--workflow-revision", WORKFLOW]) == 1


def test_publisher_refuses_partial_selection_or_wrong_report_artifact(tmp_path, evidence):
    output = tmp_path / "product-b01.json"
    source, attempt = write_inputs(tmp_path, evidence, dispatch_for(evidence))
    report = publish(source, attempt, output, selection="api_product_lifecycle",
                     source_revision=SOURCE, workflow_revision=WORKFLOW)
    assert report["status"] == "incomplete"
    assert any("complete B01 inventory" in reason for reason in report["issues"])
    assert any("artifact path" in reason for reason in report["issues"])


def test_selected_gateway_workflow_uploads_packet_even_after_failed_api_step():
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/gateway-live-tests.yml").read_text()
    assert "--dispatch-report test-results/product-v01-dispatch.json" in workflow
    assert "if: always() && inputs.product_cases != '' && inputs.product_cases != 'none'" in workflow
    assert "python -m tests.e2e.product_onboarding.product_publish" in workflow
    assert "--inputs test-results/product-b01-inputs.json" in workflow
    assert "--output test-results/product-b01.json" in workflow
    assert '--workflow-revision "$GITHUB_SHA"' in workflow
    assert workflow.index("id: product_packet") < workflow.index("uses: ./.github/actions/upload-ci-evidence-s3")


@pytest.mark.parametrize("observed,expected", [
    (None, "PermissionError"), ("f" * 40, "ValueError"), (SOURCE, "PortError"),
])
def test_live_dispatch_report_retains_refusal_without_exposing_actor_credentials(
    tmp_path, monkeypatch, observed, expected,
):
    from tests.e2e.cli_uplift import config, ports
    from tests.e2e.product_onboarding import live, product_inventory, product_native

    monkeypatch.setenv("GITHUB_SHA", WORKFLOW)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    boundary = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(boundary))
    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", "fixture-bindings")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", "fixture-catalog")
    references = {
        "CLI_UPLIFT_EVAL_INSTANCE_PROFILE": "owned-instance-profile",
        "CLI_UPLIFT_EVAL_SECURITY_GROUP_ID": "sg-owned",
        "CLI_UPLIFT_EVAL_STATE_BUCKET": "private-state-bucket",
        "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME": "run-fixture-name",
        "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    for key, value in references.items():
        monkeypatch.setenv(key, value)
    received = []
    monkeypatch.setattr(config, "from_environment", lambda env: received.append(env) or {
        "gateway_deployment": "test-deployment", "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000000", "region": "us-east-1",
        "expected_revision": SOURCE,
        "instance_profile": references["CLI_UPLIFT_EVAL_INSTANCE_PROFILE"],
        "state_bucket": references["CLI_UPLIFT_EVAL_STATE_BUCKET"],
        "credential_secret_name": references["CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME"],
        "internal_invoke_url": references["CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL"],
    })
    monkeypatch.setattr(ports, "default_ports", scoped_evaluator)
    native_reads = []

    def unavailable_native_binding(cfg):
        native_reads.append(cfg)
        raise PermissionError("native observation denied: private-actor-secret")

    monkeypatch.setattr(product_native.deployment_provenance, "binding", unavailable_native_binding)
    if observed is None:
        def denied(*_):
            raise PermissionError("read-only observation unavailable")
        monkeypatch.setattr(product_inventory, "observe", denied)
    else:
        monkeypatch.setattr(product_inventory, "observe", lambda *_: {"source_revision": observed})
    output = tmp_path / "dispatch.json"
    with pytest.raises((PermissionError, ValueError, ports.PortError)) as error:
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", SOURCE,
                   "--dispatch-report", str(output)])
    assert type(error.value).__name__ == expected
    assert len(native_reads) == (1 if observed == SOURCE else 0)
    if observed == SOURCE:
        assert str(error.value) == (
            "Native migration-head and tick observation is unavailable or mismatched; Product writes refuse"
        )
        assert error.value.__suppress_context__ is True
    assert "private-actor-secret" not in str(error.value)
    assert len(received) == 1
    assert received[0]["CLI_UPLIFT_EVAL_EXPECTED_REVISION"] == SOURCE
    assert all(received[0][key] == value for key, value in references.items())
    report = json.loads(output.read_text())
    assert report["run_id"] == "product-123456-2"
    assert report["status"] == "incomplete" and report["cleanup_verified"] is False
    assert report["observed_revision"] == observed
    assert report["observation_ref"] is None
    assert boundary.read_text() == "cleanup_ok=false\n"
    assert "private-actor-secret" not in output.read_text()


@pytest.mark.parametrize("field", [
    "expected_revision", "instance_profile", "state_bucket", "credential_secret_name", "internal_invoke_url",
])
def test_product_entrypoint_refuses_unbound_launch_before_target_reads(tmp_path, monkeypatch, field):
    from tests.e2e.cli_uplift import config, ports
    from tests.e2e.product_onboarding import live

    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", "fixture-bindings")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", "fixture-catalog")
    references = {
        "expected_revision": SOURCE,
        "instance_profile": "owned-instance-profile",
        "state_bucket": "private-state-bucket",
        "credential_secret_name": "run-fixture-name",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    }
    for name in ("instance_profile", "state_bucket", "credential_secret_name", "internal_invoke_url"):
        monkeypatch.setenv("CLI_UPLIFT_EVAL_" + name.upper(), references[name])
    cfg = {**references, "gateway_deployment": "test-deployment"}
    cfg[field] = "other-target"
    monkeypatch.setattr(config, "from_environment", lambda *_: cfg)
    monkeypatch.setattr(ports, "default_ports", lambda *_: pytest.fail("target read started"))
    output = tmp_path / "dispatch.json"
    with pytest.raises(ValueError, match="launch and selected release bindings"):
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", SOURCE,
                   "--dispatch-report", str(output)])
    report = json.loads(output.read_text())
    assert report["status"] == "incomplete"
    assert report["observed_revision"] is None


def test_dispatch_report_refuses_wrong_gateway_target_before_observation(tmp_path, monkeypatch):
    from tests.e2e.cli_uplift import config, deployment_provenance, ports
    from tests.e2e.product_onboarding import live, product_inventory

    monkeypatch.setenv("GITHUB_SHA", WORKFLOW)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setenv("CLI_UPLIFT_EVAL_BINDINGS_JSON", "fixture-bindings")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", json.dumps({
        "test-deployment": {"gateway_url": "https://other.example.invalid",
                            "account": "000000000101", "region": "us-east-1"},
    }))
    monkeypatch.setenv("CLI_UPLIFT_EVAL_INSTANCE_PROFILE", "owned-instance-profile")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_STATE_BUCKET", "private-state-bucket")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME", "run-fixture-name")
    monkeypatch.setenv("CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL", "https://private.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.setattr(config, "from_environment", lambda *_: {
        "gateway_deployment": "test-deployment", "gateway_url": "https://gateway.example.invalid",
        "platform_account": "000000000101", "region": "us-east-1",
        "expected_revision": SOURCE,
        "instance_profile": "owned-instance-profile",
        "state_bucket": "private-state-bucket",
        "credential_secret_name": "run-fixture-name",
        "internal_invoke_url": "https://private.execute-api.us-east-1.amazonaws.com/dev",
    })
    monkeypatch.setattr(ports, "default_ports", scoped_evaluator)
    monkeypatch.setattr(product_inventory, "observe", lambda aws, cfg, rev: deployment_provenance.binding(cfg))
    output = tmp_path / "dispatch.json"
    with pytest.raises(deployment_provenance.PortError, match="target"):
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", SOURCE,
                   "--dispatch-report", str(output)])
    report = json.loads(output.read_text())
    assert report["status"] == "incomplete" and report["observed_revision"] is None


@pytest.mark.parametrize("run_id,attempt", [
    ("", "1"), ("123", ""), ("0", "1"), ("01", "1"),
    ("123", "00"), ("123;echo secret", "1"), ("9" * 21, "1"),
])
def test_product_dispatch_refuses_unbound_attempt_before_observation(
    tmp_path, monkeypatch, run_id, attempt,
):
    from tests.e2e.cli_uplift import ports
    from tests.e2e.product_onboarding import live

    monkeypatch.delenv("PRODUCT_API_FIXTURES_JSON", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    monkeypatch.setattr(ports, "default_ports", lambda *_: pytest.fail("observation started"))
    output = tmp_path / "dispatch.json"
    with pytest.raises(ValueError, match="workflow run and attempt"):
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", SOURCE,
                   "--dispatch-report", str(output)])
    assert json.loads(output.read_text())["run_id"] is None


def test_static_actor_secret_never_reaches_observation_or_report(tmp_path, monkeypatch):
    from tests.e2e.cli_uplift import ports
    from tests.e2e.product_onboarding import live

    monkeypatch.setenv("PRODUCT_API_FIXTURES_JSON", json.dumps({"owner_token": "private-actor-secret"}))
    monkeypatch.setattr(ports, "default_ports", lambda *_: pytest.fail("observation started"))
    output = tmp_path / "dispatch.json"
    with pytest.raises(ValueError, match="Static Product actor fixtures"):
        live.main(["--cases", "api_product_lifecycle", "--expected-revision", SOURCE,
                   "--dispatch-report", str(output)])
    assert "private-actor-secret" not in output.read_text()
    assert json.loads(output.read_text())["status"] == "incomplete"


def test_manual_b01_decision_remains_separate_from_packet_grading(evidence):
    packet = build_packet(evidence)
    assert packet["status"] == "pass" and packet["packet_revision"].startswith("sha256:")
    assert not {"accepted", "decision", "human_decision"}.intersection(packet)
    incomplete = deepcopy(evidence)
    incomplete["cleanup"]["api_recovery_cleanup"]["owned_absent"] = False
    rejected = build_packet(incomplete)
    assert rejected["status"] == "incomplete"
    assert rejected["packet_revision"] != packet["packet_revision"]
    assert not {"accepted", "decision", "human_decision"}.intersection(rejected)

    instructions = (Path(__file__).resolve().parents[3] /
                    "docs/product-onboarding/delivery/api-evidence-entrypoint.md").read_text()
    for required in ("V01/V03 packet", "D01 deployment receipt", "`packet_revision`",
                     "`plan_revision`", "one** accept or reject decision in ADP",
                     "agent checks are complete", "not** a", "retaining earlier attempts"):
        assert required in instructions

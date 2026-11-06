"""Protected GitLab workflows must not mint a v2 receipt from incomplete identity or steps."""

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[1] / "delivery_evidence.py"
SPEC = importlib.util.spec_from_file_location("gitlab_delivery_evidence", MODULE)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


def inputs():
    source = "a" * 40
    definition = "b" * 40
    contract = delivery.DeliveryContract(
        schema_version=2,
        connection_id="gitlab",
        resource_kind="gitlab-instance",
        resource_id="reviewed-instance",
        definition_revision=definition,
        baseline_revision=source,
        required_checks=["source", "identity", "config", "tools", "plan", "safety", "apply"],
    )
    env = {
        "ADP_DELIVERY_CONTRACT_JSON": contract.model_dump_json(),
        "ADP_CONTRACT_DIGEST": contract.digest,
        "GITHUB_SHA": source,
        "ADP_SOURCE_REVISION": source,
        "GITHUB_WORKFLOW_SHA": definition,
        "ADP_DEFINITION_REVISION": definition,
        "ACCOUNT_ID": "123456789012",
        "ADP_DELIVERY_ACCOUNT_ID": "123456789012",
        "ADP_DELIVERY_RESOURCE_KIND": contract.resource_kind,
        "ADP_DELIVERY_RESOURCE_ID": contract.resource_id,
        "ADP_DELIVERY_WORKFLOW": "gitlab-infra-apply.yml",
        "ADP_DELIVERY_STEP_OUTCOMES": json.dumps(dict.fromkeys(contract.required_checks, "success")),
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "1",
        "ADP_CORRELATION": "reviewed-attempt",
        "AWS_REGION": "us-east-1",
        "ADP_DELIVERY_COMPONENT": "gitlab-instance",
    }
    return env, contract, {"Account": env["ACCOUNT_ID"]}, source


def test_produces_generic_v2_receipt_for_actual_successful_workflow_steps():
    env, contract, identity, checkout = inputs()
    evidence = delivery.produce(env, identity, checkout)
    assert evidence.contract_digest == contract.digest
    assert evidence.workflow_revision == env["GITHUB_WORKFLOW_SHA"]
    assert evidence.source_revision == checkout
    assert evidence.account_id == identity["Account"]
    assert [item.id for item in evidence.checks] == contract.required_checks
    assert all(str(env["GITHUB_RUN_ID"]) in item.evidence_ref for item in evidence.checks)


def test_dispatch_head_can_advance_while_reviewed_checkout_stays_pinned():
    env, _, identity, checkout = inputs()
    env["GITHUB_SHA"] = env["GITHUB_WORKFLOW_SHA"]
    evidence = delivery.produce(env, identity, checkout)
    assert evidence.source_revision == checkout
    assert evidence.workflow_revision != checkout


def context_inputs():
    env, contract, identity, checkout = inputs()
    env["GITHUB_SHA"] = env["GITHUB_WORKFLOW_SHA"]
    env["ADP_CORRELATION"] = "orch:00000000-0000-4000-8000-000000000001"
    actual = {**contract.workflow_inputs, "adp_source_revision": checkout,
              "adp_definition_revision": env["GITHUB_WORKFLOW_SHA"],
              "adp_correlation": env["ADP_CORRELATION"], "adp_contract_digest": contract.digest}
    env["ADP_DELIVERY_INPUTS_JSON"] = json.dumps(actual)
    return env, contract, identity, checkout


def test_context_retains_the_contract_digest_and_separates_source_from_workflow():
    env, contract, identity, checkout = context_inputs()
    context = delivery.produce_context(env, identity, checkout)
    assert context["source_revision"] == checkout
    assert context["workflow_revision"] == env["GITHUB_WORKFLOW_SHA"]
    assert context["inputs"] == {"adp_contract_digest": contract.digest}
    assert context["correlation"] == env["ADP_CORRELATION"]
    assert context["account_id"] == identity["Account"]


@pytest.mark.parametrize("key,value", [
    ("adp_source_revision", "c" * 40), ("adp_definition_revision", "c" * 40),
    ("adp_correlation", "orch:another-operation"), ("adp_contract_digest", "c" * 64),
])
def test_context_refuses_changed_dispatch_inputs(key, value):
    env, _, identity, checkout = context_inputs()
    actual = json.loads(env["ADP_DELIVERY_INPUTS_JSON"])
    actual[key] = value
    env["ADP_DELIVERY_INPUTS_JSON"] = json.dumps(actual)
    with pytest.raises(ValueError, match="context inputs"):
        delivery.produce_context(env, identity, checkout)


@pytest.mark.parametrize("field,value", [
    ("ADP_CONTRACT_DIGEST", "0" * 64),
    ("ADP_SOURCE_REVISION", "c" * 40),
    ("ADP_DEFINITION_REVISION", "c" * 40),
    ("ADP_DELIVERY_ACCOUNT_ID", "111111111111"),
    ("ADP_DELIVERY_RESOURCE_KIND", "different"),
    ("ADP_DELIVERY_RESOURCE_ID", "another-instance"),
    ("ADP_CORRELATION", ""),
    ("ADP_DELIVERY_STEP_OUTCOMES", '{"source":"success"}'),
    ("ADP_DELIVERY_STEP_OUTCOMES", '{"source":"failure","identity":"success","config":"success","plan":"success","apply":"success"}'),
])
def test_refuses_missing_or_changed_provenance_and_checks(field, value):
    env, _, identity, checkout = inputs()
    env[field] = value
    with pytest.raises(ValueError):
        delivery.produce(env, identity, checkout)


def test_refuses_wrong_authenticated_account_or_checkout():
    env, _, identity, checkout = inputs()
    with pytest.raises(ValueError, match="account"):
        delivery.produce(env, {"Account": "111111111111"}, checkout)
    with pytest.raises(ValueError, match="source"):
        delivery.produce(env, identity, "c" * 40)


def test_cleanup_required_cannot_be_claimed_without_receipts():
    env, contract, identity, checkout = inputs()
    env["ADP_DELIVERY_CONTRACT_JSON"] = contract.model_copy(update={"cleanup_required": True}).model_dump_json()
    env["ADP_CONTRACT_DIGEST"] = delivery.DeliveryContract.model_validate_json(env["ADP_DELIVERY_CONTRACT_JSON"]).digest
    with pytest.raises(ValueError, match="cleanup"):
        delivery.produce(env, identity, checkout)


@pytest.mark.parametrize("missing", ["source", "identity", "config", "tools", "plan", "safety", "apply"])
def test_rejects_binding_that_omits_a_protected_workflow_check(missing):
    env, contract, identity, checkout = inputs()
    reduced = contract.model_copy(update={"required_checks": [check for check in contract.required_checks if check != missing]})
    env["ADP_DELIVERY_CONTRACT_JSON"] = reduced.model_dump_json()
    env["ADP_CONTRACT_DIGEST"] = reduced.digest
    env["ADP_DELIVERY_STEP_OUTCOMES"] = json.dumps(dict.fromkeys(reduced.required_checks, "success"))
    with pytest.raises(ValueError, match="omits required"):
        delivery.produce(env, identity, checkout)


def test_rejects_skipped_required_step():
    env, contract, identity, checkout = inputs()
    env["ADP_DELIVERY_STEP_OUTCOMES"] = json.dumps({**dict.fromkeys(contract.required_checks, "success"), "safety": "skipped"})
    with pytest.raises(ValueError, match="did not succeed"):
        delivery.produce(env, identity, checkout)


def qualify_inputs(tmp_path):
    env, contract, identity, checkout = inputs()
    required = sorted(delivery.REQUIRED_STEPS["gitlab-runners-qualify.yml"])
    contract = contract.model_copy(update={"required_checks": required, "cleanup_required": True})
    env.update(ADP_DELIVERY_CONTRACT_JSON=contract.model_dump_json(), ADP_CONTRACT_DIGEST=contract.digest,
               ADP_DELIVERY_WORKFLOW="gitlab-runners-qualify.yml",
               ADP_DELIVERY_STEP_OUTCOMES=json.dumps(dict.fromkeys(required, "success")),
               ADP_QUALIFICATION_RUN_ID="00000000-0000-4000-8000-000000000001",
               ADP_QUALIFICATION_BUNDLE_SHA256="c" * 64)
    fixtures = tmp_path / "fixtures.json"
    receipt = tmp_path / "final-verdict.json"
    fixtures.write_text(json.dumps({"run_id": env["ADP_QUALIFICATION_RUN_ID"],
                                    "source_revision": checkout, "account_id": identity["Account"]}))
    receipt.write_text(json.dumps({"verdict": "pass", "fresh_independent_api_readbacks": "passed",
                                   "run_id": env["ADP_QUALIFICATION_RUN_ID"],
                                   "workflow_run_id": int(env["GITHUB_RUN_ID"]),
                                   "bundle_sha256": env["ADP_QUALIFICATION_BUNDLE_SHA256"]}))
    fixtures.chmod(0o600)
    receipt.chmod(0o600)
    env.update(ADP_DELIVERY_FIXTURES=str(fixtures), ADP_DELIVERY_CLEANUP_RECEIPT=str(receipt))
    return env, contract, identity, checkout, fixtures, receipt


def test_qualification_emits_cleanup_only_after_current_live_final_verdict(tmp_path):
    env, contract, identity, checkout, _, _ = qualify_inputs(tmp_path)
    evidence = delivery.produce(env, identity, checkout)
    assert evidence.cleanup == "passed"
    assert {check.id for check in evidence.checks} == set(contract.required_checks)


@pytest.mark.parametrize("name", ["apply", "qualify"])
def test_workflow_environment_reaches_real_signed_publisher(tmp_path, name):
    env, contract, identity, checkout, fixtures, receipt = qualify_inputs(tmp_path)
    root = MODULE.parents[4]
    document = yaml.load((root / f".github/workflows/gitlab-runners-{name}.yml").read_text(), Loader=yaml.BaseLoader)
    job = document["jobs"][name]
    step = next(step for step in job["steps"] if step.get("name") == "Publish verified operational delivery")
    required = sorted(delivery.REQUIRED_STEPS[f"gitlab-runners-{name}.yml"])
    contract = contract.model_copy(update={"required_checks": required, "cleanup_required": name == "qualify"})
    context = {
        "inputs.environment": "dev", "inputs.account_id": identity["Account"], "inputs.region": "us-east-1",
        "inputs.activation_operation_id": "", "inputs.activation_group_id": "",
        "inputs.approved_plan_sha256": "d" * 64, "inputs.bundle_sha256": env["ADP_QUALIFICATION_BUNDLE_SHA256"],
        "inputs.qualification_run_id": env["ADP_QUALIFICATION_RUN_ID"],
        "inputs.qualification_run_id || vars.GITLAB_QUALIFICATION_RUN_ID": env["ADP_QUALIFICATION_RUN_ID"],
        "inputs.bundle_sha256 || vars.GITLAB_QUALIFICATION_BUNDLE_SHA256": env["ADP_QUALIFICATION_BUNDLE_SHA256"],
        "inputs.adp_correlation": env["ADP_CORRELATION"], "inputs.adp_source_revision": checkout,
        "inputs.adp_definition_revision": env["ADP_DEFINITION_REVISION"], "inputs.adp_contract_digest": contract.digest,
        "github.sha": checkout, "github.workflow_sha": env["GITHUB_WORKFLOW_SHA"], "runner.temp": str(tmp_path),
        "vars.GITLAB_RUNNER_CONFIG_JSON": "{}", "inputs.source_revision": "",
        "vars.ADP_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/example",
        "vars.GITLAB_RUNNERS_STATE_BUCKET": "private-example", "vars.GITLAB_RUNNERS_RELEASE_LOCK_SHA256": "e" * 64,
        **{f"steps.{check}.outcome": "success" for check in required},
    }
    prefix = "GITLAB_RUNNERS_QUALIFICATION" if name == "qualify" else "GITLAB_RUNNERS"
    context.update({f"vars.{prefix}_DELIVERY_{key}": value for key, value in {
        "CONTRACT_JSON": contract.model_dump_json(), "COMPONENT": env["ADP_DELIVERY_COMPONENT"],
        "RESOURCE_KIND": contract.resource_kind, "RESOURCE_ID": contract.resource_id,
    }.items()})
    supplied = {key: value for key, value in env.items() if key.startswith("GITHUB_")}
    supplied.update({key: re.sub(r"\$\{\{ (.*?) \}\}", lambda match: context[match[1]], value)
                     for scope in (document.get("env", {}), job["env"], step["env"]) for key, value in scope.items()})
    records = tmp_path / "gitlab-qualification/records"
    records.mkdir(parents=True)
    fixtures.rename(records / "fixtures.json")
    receipt.rename(records / "final-verdict.json")
    evidence = delivery.produce(supplied, identity, checkout)
    payload = tmp_path / "delivery.json"
    payload.write_text(evidence.model_dump_json())
    spec = importlib.util.spec_from_file_location("signed_publisher", root / "modules/gateway/scripts/publish-deployment-evidence.py")
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    stored = []

    def aws(command):
        if command == ["sts", "get-caller-identity"]:
            return identity
        assert command[:2] == ["s3api", "put-object"]
        stored.append(json.loads(Path(command[command.index("--body") + 1]).read_bytes()))
        assert command[command.index("--bucket") + 1] == publisher.bucket_name(identity["Account"], "dev")
        return {"VersionId": "synthetic-version"}

    token = Mock(return_value="synthetic-oidc-token")
    result = publisher.publish(supplied, payload, "delivery", "delivery-evidence", aws=aws, request_token=token)
    assert result["version_id"] == "synthetic-version" and len(stored) == 1
    token.assert_called_once_with(publisher.audience(payload.read_bytes()))


@pytest.mark.parametrize("change", ["missing", "failed", "stale", "other_run", "other_bundle", "unsafe", "fixtures", "skip", "no_cleanup"])
def test_qualification_rejects_missing_or_unbound_final_receipt(tmp_path, change):
    env, contract, identity, checkout, fixtures, receipt = qualify_inputs(tmp_path)
    value = json.loads(receipt.read_text())
    if change == "missing":
        receipt.unlink()
    elif change == "failed":
        value["verdict"] = "incomplete"
    elif change == "stale":
        value["workflow_run_id"] += 1
    elif change == "other_run":
        value["run_id"] = "another-run"
    elif change == "other_bundle":
        value["bundle_sha256"] = "d" * 64
    elif change == "unsafe":
        receipt.chmod(0o644)
    elif change == "fixtures":
        fixtures.write_text(json.dumps({"run_id": env["ADP_QUALIFICATION_RUN_ID"],
                                        "source_revision": checkout, "account_id": "999999999999"}))
    elif change == "skip":
        env["ADP_DELIVERY_STEP_OUTCOMES"] = json.dumps({**dict.fromkeys(contract.required_checks, "success"), "final": "skipped"})
    else:
        contract = contract.model_copy(update={"cleanup_required": False})
        env.update(ADP_DELIVERY_CONTRACT_JSON=contract.model_dump_json(), ADP_CONTRACT_DIGEST=contract.digest)
    if change in {"failed", "stale", "other_run", "other_bundle"}:
        receipt.write_text(json.dumps(value))
    with pytest.raises((ValueError, FileNotFoundError)):
        delivery.produce(env, identity, checkout)


# The protected runner check explicitly selects this test file. Import the new
# registration cases until its workflow test inventory includes their own file.
from test_registration_delivery import (  # noqa: E402,F401
    test_semantic_checks_use_private_versioned_refs_not_action_step_success,
    test_refuses_missing_stale_or_nonsemantic_live_evidence,
    test_refuses_successful_setup_relabelled_as_six_live_checks,
    test_live_checks_must_be_complete_in_contract,
)

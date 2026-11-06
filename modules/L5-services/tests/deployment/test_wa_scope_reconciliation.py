"""Permission repair must never become a general deployment or consumer restart."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("wa_scope", ROOT / "modules/L5-services/scripts/reconcile-wa-schedule-scope.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    config = {"schema_version": 1, "environment": "test", "account_id": "000000000000",
              "region": "us-east-1", "role_arn": "arn:aws:iam::000000000000:role/adp-test-l5-trusted-deployment",
              "state_bucket": "example-state", "platform": dict.fromkeys(MODULE.DEPLOY.COMMON, "example"),
              "services": {"well_architected": {"worker_factory": MODULE.LIFECYCLE.FACTORIES["well_architected"],
                                                "worker_environment": {"PRIVATE_DEPENDENCY": "private-value"}}}}
    config["platform"].update(gateway_api_arn="arn:aws:execute-api:us-east-1:000000000000:example/*/*/internal/*",
                               rds_connect_arn="arn:aws:rds-db:us-east-1:000000000000:dbuser:db-example/adp_l5_worker",
                               agent_registry_table_name="example-registry")
    desired = MODULE.expected_documents(config, {"RoleId": "example-role-id"})
    before = MODULE.legacy_documents(desired)
    variables = {**config["services"]["well_architected"]["worker_environment"],
                 "L5_SERVICE_ID": "well_architected", "L5_WORKER_FACTORY": MODULE.LIFECYCLE.FACTORIES["well_architected"],
                 "L5_RDS_CONNECT_ARN": config["platform"]["rds_connect_arn"], "L5_WORK_QUEUE_URL": "example-queue"}
    runtime = {"worker": {"Environment": {"Variables": variables}, "CodeSha256": "old-code", "RevisionId": "revision-1"},
               "repair": {"Environment": {"Variables": variables}, "CodeSha256": "old-code", "RevisionId": "revision-2"},
               "mapping": "Disabled", "rule": "DISABLED"}
    initial = {"runtime": runtime, "documents": before, "desired": desired, "mapping_id": "example-mapping"}
    plan = {"complete": True, "resource_changes": [], "output_changes": {"existing": {"actions": ["no-op"]}}}
    for address in desired:
        if address == MODULE.POLICY:
            field = "policy"
            attributes = {"name": "adp-test-l5-well-architected-repair", "role": "adp-test-l5-well-architected-repair"}
        else:
            field = "item"
            attributes = {"table_name": "example-registry", "hash_key": "agent_id", "id": "example-item"}
        plan["resource_changes"].append({"address": address, "mode": "managed", "change": {
            "actions": ["update"], "before": {**attributes, field: json.dumps(before[address])},
            "after": {**attributes, field: json.dumps(desired[address])}, "after_unknown": {}}})
    args = SimpleNamespace(action="apply", environment="test", region="us-east-1", role_arn=config["role_arn"],
                           bucket="example-release", source_sha="a" * 40, gateway_wheel_sha256="b" * 64,
                           manifest_version="immutable-manifest", reviewed_plan_digest="")
    calls = []
    applied = []
    monkeypatch.setattr(MODULE.LIFECYCLE, "identity", lambda *args: calls.append(("identity", args)))
    monkeypatch.setattr(MODULE.LIFECYCLE, "manifest_for", lambda *args: ("pinned/workers.zip", "version-1", "digest"))
    monkeypatch.setattr(MODULE.LIFECYCLE, "scope_readback", lambda *args: calls.append(("scope_readback", args)))

    def observe(_):
        value = copy.deepcopy(initial)
        if applied:
            value["documents"] = copy.deepcopy(desired)
        return value

    monkeypatch.setattr(MODULE, "observe", observe)

    def terraform(service, arguments, directory, environment):
        calls.append((service, arguments, environment))
        if arguments[0] == "plan":
            Path(arguments[-1].removeprefix("-out=")).write_bytes(b"the only saved plan")
        elif arguments[0] == "show":
            return json.dumps(plan)
        elif arguments[0] == "apply":
            assert Path(arguments[-1]).read_bytes() == b"the only saved plan"
            applied.append(arguments[-1])
        return ""

    monkeypatch.setattr(MODULE.DEPLOY, "terraform", terraform)
    return SimpleNamespace(config=config, initial=initial, desired=desired, plan=plan, args=args,
                           calls=calls, applied=applied, terraform=terraform, observe=observe, directory=tmp_path)


def review_digest(recovery):
    values = MODULE.DEPLOY.variables(recovery.config, MODULE.SERVICE, {
        "worker_artifact_bucket": recovery.args.bucket, "worker_artifact_key": "pinned/workers.zip",
        "worker_artifact_version": "version-1", "worker_artifact_sha256": "digest"})
    return MODULE.plan_review_digest(recovery.plan, recovery.args, values, recovery.initial)


def run(recovery, reviewed=True):
    if reviewed:
        recovery.args.reviewed_plan_digest = review_digest(recovery)
    receipt = {"apply_started": False, "apply_finished": False}
    MODULE.reconcile(recovery.args, recovery.config, recovery.directory, receipt)
    return receipt


def test_applies_the_single_verified_saved_plan_and_reads_back_scope(recovery):
    receipt = run(recovery)
    operations = [(name, arguments[0]) for name, arguments, *_ in recovery.calls if name == MODULE.SERVICE]
    assert operations == [(MODULE.SERVICE, operation) for operation in ("init", "plan", "show", "apply")]
    assert len(recovery.applied) == 1
    assert {item["address"] for item in receipt["changes"]} == {MODULE.POLICY, MODULE.REGISTRY}
    assert receipt["result"] == "reconciled" and receipt["runtime_unchanged"] and receipt["installed_scope_verified"]
    assert receipt["apply_started"] and receipt["apply_finished"]
    assert recovery.calls[-1][0] == "scope_readback"
    assert "private-value" not in json.dumps(receipt)
    for name, arguments, *_ in recovery.calls:
        if name == MODULE.SERVICE:
            assert not any("-target" in arg or "-lock=false" in arg for arg in arguments)
    backend = json.loads((recovery.directory / "backend.json").read_text())
    assert backend["use_lockfile"] and backend["key"] == "l5-services/test/us-east-1/well-architected/terraform.tfstate"


def test_plan_mode_never_applies_or_calls_scope_probe(recovery):
    recovery.args.action = "plan"
    receipt = run(recovery)
    assert receipt["result"] == "planned" and not receipt["apply_started"]
    assert not recovery.applied
    assert not any(call[0] == "scope_readback" for call in recovery.calls)


def test_apply_requires_an_explicit_prior_plan_review(recovery):
    with pytest.raises(MODULE.ReconciliationRejected, match="requires the reviewed"):
        run(recovery, reviewed=False)
    assert not recovery.calls


def test_apply_rejects_a_different_reviewed_plan_digest(recovery):
    recovery.args.reviewed_plan_digest = "f" * 64
    with pytest.raises(MODULE.ReconciliationRejected, match="differ from the reviewed"):
        run(recovery, reviewed=False)
    assert not recovery.applied


def test_review_digest_ignores_only_session_data_and_timestamps(recovery):
    original = review_digest(recovery)
    recovery.plan["timestamp"] = "another job time"
    recovery.plan["resource_changes"].append({"address": "data.aws_caller_identity.current", "mode": "data",
        "change": {"actions": ["read"], "after": {"arn": "a-new-OIDC-session"}}})
    assert review_digest(recovery) == original
    recovery.initial["runtime"]["worker"]["RevisionId"] = "different-runtime"
    assert review_digest(recovery) != original


@pytest.mark.parametrize("already_current", [(MODULE.POLICY,), (MODULE.REGISTRY,), (MODULE.POLICY, MODULE.REGISTRY)])
def test_partial_repair_and_repeat_only_change_remaining_permissions(recovery, already_current):
    for address in already_current:
        recovery.initial["documents"][address] = recovery.desired[address]
        change = next(item["change"] for item in recovery.plan["resource_changes"] if item["address"] == address)
        change["before"] = copy.deepcopy(change["after"])
        change["actions"] = ["no-op"]
    receipt = run(recovery)
    assert {item["address"] for item in receipt["changes"]} == {MODULE.POLICY, MODULE.REGISTRY} - set(already_current)
    assert bool(recovery.applied) == (len(already_current) < 2)
    assert receipt["installed_scope_verified"]


@pytest.mark.parametrize("fault", ["extra_resource", "create", "replace", "unknown", "stale_before", "widen_policy",
                                  "wrong_role", "wrong_registry", "extra_scope", "missing_resource", "incomplete",
                                  "deferred", "output_change", "state_move", "import", "duplicate"])
def test_unreviewed_changes_fail_before_apply(recovery, fault):
    plan = recovery.plan
    change = plan["resource_changes"][0]["change"]
    if fault == "extra_resource":
        plan["resource_changes"].append({"address": "module.runtime.aws_lambda_function.worker", "mode": "managed",
                                         "change": {"actions": ["update"]}})
    elif fault == "create":
        change.update(actions=["create"], before=None)
    elif fault == "replace":
        change["actions"] = ["delete", "create"]
    elif fault == "unknown":
        change["after_unknown"] = {"policy": True}
    elif fault == "stale_before":
        change["before"]["policy"] = "{}"
    elif fault == "widen_policy":
        policy = json.loads(change["after"]["policy"])
        policy["Statement"][-1]["Resource"].append("*")
        change["after"]["policy"] = json.dumps(policy)
    elif fault == "wrong_role":
        change["before"]["role"] = change["after"]["role"] = "foreign"
    elif fault in {"wrong_registry", "extra_scope"}:
        change = plan["resource_changes"][1]["change"]
        if fault == "wrong_registry":
            change["before"]["table_name"] = change["after"]["table_name"] = "foreign"
        else:
            item = json.loads(change["after"]["item"])
            item["credential_scopes"]["SS"].append("foreign")
            change["after"]["item"] = json.dumps(item)
    elif fault == "missing_resource":
        plan["resource_changes"].pop()
    elif fault == "incomplete":
        plan["complete"] = False
    elif fault == "deferred":
        plan["deferred_changes"] = [{"reason": "unknown"}]
    elif fault == "output_change":
        plan["output_changes"]["existing"]["actions"] = ["update"]
    elif fault == "state_move":
        plan["resource_changes"][0]["previous_address"] = "foreign.resource"
    elif fault == "import":
        change["importing"] = {"id": "foreign"}
    else:
        plan["resource_changes"].append(copy.deepcopy(plan["resource_changes"][0]))
    with pytest.raises(MODULE.ReconciliationRejected):
        run(recovery)
    assert not recovery.applied


def test_existing_excess_permissions_are_not_preserved_under_a_narrow_delta(recovery):
    recovery.initial["documents"][MODULE.POLICY]["Statement"][0]["Action"].append("iam:*")
    with pytest.raises(MODULE.ReconciliationRejected, match="Installed permissions"):
        run(recovery)
    assert not recovery.applied


@pytest.mark.parametrize("field", ["runtime", "documents", "saved_plan"])
def test_pre_apply_recheck_rejects_concurrent_changes(recovery, monkeypatch, field):
    reads = []

    def observe(config):
        value = recovery.observe(config)
        if reads:
            if field == "runtime":
                value["runtime"]["worker"]["CodeSha256"] = "concurrent-code"
            elif field == "documents":
                value["documents"][MODULE.REGISTRY]["status"] = {"S": "revoked"}
            else:
                (recovery.directory / "reconciliation.tfplan").write_bytes(b"different plan")
        reads.append(True)
        return value

    monkeypatch.setattr(MODULE, "observe", observe)
    with pytest.raises(MODULE.ReconciliationRejected, match="changed"):
        run(recovery)
    assert not recovery.applied


def test_post_apply_runtime_change_does_not_report_success(recovery, monkeypatch):
    def observe(config):
        value = recovery.observe(config)
        if recovery.applied:
            value["runtime"]["mapping"] = "Enabled"
        return value
    monkeypatch.setattr(MODULE, "observe", observe)
    with pytest.raises(MODULE.ReconciliationRejected, match="consumer changed"):
        run(recovery)
    assert len(recovery.applied) == 1
    assert not any(call[0] == "scope_readback" for call in recovery.calls)


def test_ambient_terraform_flags_cannot_bypass_lock_scope_or_backend(recovery, monkeypatch):
    monkeypatch.setenv("TF_CLI_ARGS_plan", "-target=foreign -lock=false")
    monkeypatch.setenv("TF_CLI_ARGS", "-lock=false")
    monkeypatch.setenv("TF_WORKSPACE", "foreign")
    monkeypatch.setenv("TF_VAR_environment", "foreign")
    monkeypatch.setenv("L5_INFRA_CONFIG_JSON", "private-values")
    run(recovery)
    for service, arguments, *extra in recovery.calls:
        if service == MODULE.SERVICE:
            environment = extra[0]
            assert environment["TF_WORKSPACE"] == "default"
            assert not any(key.startswith(("TF_CLI_ARGS", "TF_VAR_")) for key in environment)
            assert "L5_INFRA_CONFIG_JSON" not in environment


def test_registry_string_set_order_is_not_a_scope_change(recovery):
    value = copy.deepcopy(recovery.desired[MODULE.REGISTRY])
    value["credential_scopes"]["SS"].reverse()
    assert MODULE.document(MODULE.REGISTRY, value) == recovery.desired[MODULE.REGISTRY]


def test_target_mismatch_fails_before_aws_or_terraform(recovery):
    recovery.args.role_arn = "arn:aws:iam::000000000000:role/foreign"
    with pytest.raises(MODULE.ReconciliationRejected, match="protected target"):
        run(recovery)
    assert not recovery.calls


def test_immutable_artifact_failure_prevents_planning(recovery, monkeypatch):
    def invalid(*args):
        raise ValueError("Archive does not match pinned manifest")
    monkeypatch.setattr(MODULE.LIFECYCLE, "manifest_for", invalid)
    with pytest.raises(ValueError, match="Archive"):
        run(recovery)
    assert [call[0] for call in recovery.calls] == ["identity"]


@pytest.mark.parametrize("fault", [None, "enabled", "unhealthy", "foreign_role", "changed_mapping"])
def test_real_observer_uses_only_read_apis_and_requires_paused_owned_runtime(recovery, monkeypatch, fault):
    account = recovery.config["account_id"]
    worker, repair = MODULE.LIFECYCLE.functions("test", "well_architected")
    calls = []

    def aws(service, action, *arguments):
        calls.append((service, action))
        if (service, action) == ("lambda", "list-event-source-mappings"):
            return {"EventSourceMappings": [{"UUID": "example-mapping",
                    "EventSourceArn": f"arn:aws:sqs:us-east-1:{account}:adp-test-l5-well-architected-work"}]}
        if (service, action) == ("lambda", "get-function-configuration"):
            name = arguments[1]
            return {"FunctionArn": f"arn:aws:lambda:us-east-1:{account}:function:{name}",
                    "Role": f"arn:aws:iam::{account}:role/" + ("foreign" if fault == "foreign_role" else name),
                    "State": "Failed" if fault == "unhealthy" else "Active", "LastUpdateStatus": "Successful"}
        if (service, action) == ("lambda", "get-function-recursion-config"):
            assert arguments == ("--function-name", worker)
            return {"RecursiveLoop": "Allow"}
        if (service, action) == ("lambda", "get-event-source-mapping"):
            return {"UUID": "foreign" if fault == "changed_mapping" else "example-mapping",
                    "State": "Enabled" if fault == "enabled" else "Disabled"}
        if (service, action) == ("events", "describe-rule"):
            return {"Name": repair, "State": "DISABLED"}
        if (service, action) == ("iam", "get-role"):
            return {"Role": {"Arn": f"arn:aws:iam::{account}:role/{repair}", "RoleId": "example-role-id"}}
        if (service, action) == ("iam", "get-role-policy"):
            return {"PolicyDocument": recovery.initial["documents"][MODULE.POLICY]}
        if (service, action) == ("dynamodb", "get-item"):
            assert "--consistent-read" in arguments
            return {"Item": recovery.initial["documents"][MODULE.REGISTRY]}
        raise AssertionError("Unexpected cloud operation")

    monkeypatch.setattr(MODULE.LIFECYCLE, "aws", aws)
    # The fixture replaces observe for orchestration tests; exercise the real function here.
    spec = importlib.util.spec_from_file_location("wa_real_observe", Path(MODULE.__file__))
    actual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(actual)
    monkeypatch.setattr(actual.LIFECYCLE, "aws", aws)
    if fault:
        with pytest.raises(actual.ReconciliationRejected):
            actual.observe(recovery.config)
    else:
        observed = actual.observe(recovery.config)
        assert observed["desired"] == recovery.desired
        assert observed["documents"] == recovery.initial["documents"]
    assert calls and all(action.startswith(("get-", "list-", "describe-")) for _, action in calls)


def test_command_removes_private_temporary_inputs_and_emits_safe_failure_receipt(tmp_path, monkeypatch, capsys):
    private = "private-protected-input"
    monkeypatch.setenv("L5_INFRA_CONFIG_JSON", json.dumps({"private": private}))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["reconcile-wa-schedule-scope.py", "apply", "--environment", "test",
        "--region", "us-east-1", "--role-arn", "example", "--bucket", "example-release", "--source-sha", "a" * 40,
        "--gateway-wheel-sha256", "b" * 64, "--manifest-version", "immutable", "--receipt", str(receipt_path)])
    directories = []

    def failure(args, config, directory, receipt):
        assert config["private"] == private
        directories.append(directory)
        assert directory.stat().st_mode & 0o777 == 0o700
        (directory / "terraform.log").write_text(private)
        receipt["stage"] = "terraform_apply"
        receipt["apply_started"] = True
        raise RuntimeError(private)

    monkeypatch.setattr(MODULE, "reconcile", failure)
    assert MODULE.main() == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["result"] == "failed" and receipt["stage"] == "terraform_apply" and receipt["apply_started"]
    assert not receipt["apply_finished"]
    assert private not in json.dumps(receipt) and private not in capsys.readouterr().out
    assert all(not directory.exists() for directory in directories)
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_shared_workflow_keeps_reconciliation_protected_and_separate_from_lifecycle():
    workflow = yaml.load((ROOT / ".github/workflows/l5-services-worker-lifecycle.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["reconciliation_mode"]["default"] == "plan"
    job = workflow["jobs"]["lifecycle"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "adp-gateway-deploy-${{ inputs.environment }}"
    assert workflow["concurrency"]["group"] == "l5-services-release-${{ inputs.environment }}"
    identity = next(index for index, step in enumerate(job["steps"]) if step.get("uses") == "./.github/actions/trusted-deployment")
    commands = []
    for index, step in enumerate(job["steps"]):
        command = step.get("run", "")
        if 'test "$SERVICE" = well_architected' in command:
            assert index < identity and 'git merge-base --is-ancestor "$SOURCE_SHA" HEAD' in command
        if "reconcile-wa-schedule-scope.py" in command:
            assert index > identity and step["if"] == "inputs.operation == 'reconcile-wa-schedule-scope'"
            assert step["env"]["DEPLOY_ROLE"] == "${{ vars.ADP_L5_DEPLOY_ROLE_ARN }}"
            commands.append(command)
        if 'worker-lifecycle.py "$OPERATION"' in command:
            assert step["if"] == "inputs.operation != 'reconcile-wa-schedule-scope'"
    assert len(commands) == 1
    assert not any(text in commands[0] for text in ("publish-workers", "resume", "rollout", "playwright", "deploy-infra.py apply"))

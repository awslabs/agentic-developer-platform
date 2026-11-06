"""Exercise grouped installation ordering, immutable input and destructive-plan refusal."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from l5_services.adapters.finops.console_executor import ConsoleFinOpsExecutor

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("deploy_infra", ROOT / "modules/L5-services/scripts/deploy-infra.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

SURFACE = {field: "example-reviewed-surface" if field == "surface_revision" else "[data-example]"
           for field in sorted(deploy.FINOPS_SURFACE_FIELDS)}


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    config = {"schema_version": 1, "environment": "test", "account_id": "000000000000",
              "region": "us-east-1", "role_arn": "arn:aws:iam::000000000000:role/deployer",
              "state_bucket": "example-state", "gateway_role_name": "example-gateway",
              "platform": dict.fromkeys(deploy.COMMON, "example"),
              "services": {service: {"worker_factory": "l5_services.runtime:factory", "worker_environment": {}}
                           for service in ["security", "finops"]}}
    config["services"]["finops"]["executor_image_uri"] = (
        "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-test-l5-finops-executor@sha256:" + "a" * 64
    )
    config["services"]["finops"]["executor_surface"] = json.dumps(SURFACE)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    args = SimpleNamespace(action="apply", config=path, services="foundation,security,finops", bucket="example-artifacts",
                           source_sha="a" * 40, wheel_sha256="b" * 64, manifest_version="immutable-version",
                           receipt_dir=tmp_path / "private")
    calls = []
    monkeypatch.setattr(deploy.LIFECYCLE, "identity", lambda *args: calls.append(("identity", args)))
    monkeypatch.setattr(deploy.LIFECYCLE, "manifest_for", lambda *args: ("pinned/workers.zip", "version-1", "digest"))

    def terraform(service, arguments, directory, environment):
        calls.append((service, arguments))
        if arguments[0] == "show":
            return json.dumps({"resource_changes": [{"address": "module.runtime.aws_lambda_function.worker",
                                                    "change": {"actions": ["create"]}}]})
        if arguments[0] == "output":
            return json.dumps({"worker_function_name": {"value": f"{service}-worker"}})
        return ""

    monkeypatch.setattr(deploy, "terraform", terraform)
    return args, config, calls, terraform


def test_campaign_plans_all_selected_roots_before_any_apply(campaign):
    args, _, calls, _ = campaign
    assert deploy.deploy(args) == ["foundation", "security", "finops"]
    commands = [(service, command[0]) for service, command in calls if service != "identity"]
    first_apply = next(index for index, (_, cmd) in enumerate(commands) if cmd == "apply")
    assert [service for service, cmd in commands[:first_apply] if cmd == "plan"] == ["foundation", "security", "finops"]
    for service, command in calls:
        if service != "identity" and command[0] == "apply":
            assert command[-1].endswith(f"/{service}/install.tfplan")
    keys = set()
    for service in ["foundation", "security", "finops"]:
        receipt = json.loads((args.receipt_dir / service / "receipt.json").read_text())
        keys.add(receipt["state_key"])
        assert receipt["result"] == "applied" and receipt["qualification"] == "not_run"
    assert keys == {f"l5-services/test/us-east-1/{s}/terraform.tfstate" for s in ["foundation", "security", "finops"]}
    selected = json.loads((args.receipt_dir / "finops" / "inputs.tfvars.json").read_text())
    assert selected["executor_surface"] == json.dumps(SURFACE)
    assert "executor_surface" not in json.loads((args.receipt_dir / "security" / "inputs.tfvars.json").read_text())


def test_finops_release_does_not_mutate_gateway_permissions(campaign):
    _, config, _, _ = campaign
    config["services"]["finops"]["worker_environment"] = {"L5_ARCHIVE_BUCKET": "example-finops-answers"}
    values = deploy.variables(config, "finops", {})
    assert "gateway_role_name" not in values
    assert values["worker_environment"]["L5_ARCHIVE_BUCKET"] == "example-finops-answers"
    assert "l5_worker_artifact_bucket" not in values
    root = ROOT / "modules/L5-services/infra/finops"
    assert not (root / "answer-read.tf").exists()
    assert "aws_iam_role_policy_attachment" not in "\n".join(path.read_text() for path in root.glob("*.tf"))


async def test_deployed_finops_surface_supports_read_only_native_adoption(campaign):
    args, config, _, _ = campaign
    surface = {**SURFACE, "data_role_selector": "[data-role]"}
    config["services"]["finops"]["executor_surface"] = json.dumps(surface)
    args.config.write_text(json.dumps(config))
    deploy.deploy(args)
    values = json.loads((args.receipt_dir / "finops" / "inputs.tfvars.json").read_text())
    deployed_surface = json.loads(values["executor_surface"])
    assert deployed_surface == surface and "billing_view_selector" not in deployed_surface
    reference = {"record_type": "finops_agent", "record_id": "agent-example"}
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    authority = {"work_kind": "setup", "operation_id": "setup-example", "attempt_id": "attempt-example",
                 "lease_fence": 1, "provider_identity": identity, "native_resource_ref": reference,
                 "setup_verified": {"finops": {"native_agent_display_name": "Product-selected Agent"}}}
    native = {"state": "ready", "native_agent_display_name": "Product-selected Agent",
              "data_role_ref": "arn:aws:iam::000000000000:role/ExampleData"}
    page = SimpleNamespace(read_agent=AsyncMock(return_value=native))

    @asynccontextmanager
    async def session(verified, selected_surface):
        assert verified == authority and selected_surface == surface
        yield page

    evidence = AsyncMock()
    executor = ConsoleFinOpsExecutor(SimpleNamespace(session=session), AsyncMock(return_value=authority), evidence, deployed_surface)
    result = await executor.read_agent(native_job_ref=reference, authority=authority)
    assert result == {**native, "native_resource_ref": reference, "provider_identity": identity}
    page.read_agent.assert_awaited_once_with("agent-example", "Product-selected Agent")
    assert evidence.mock_calls == []


def test_invalid_final_service_inputs_prevent_all_terraform(campaign):
    args, config, calls, _ = campaign
    config["services"]["finops"]["worker_environment"]["L5_WORK_QUEUE_URL"] = "foreign"
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Terraform-owned"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


@pytest.mark.parametrize("value", [
    None, "", "example.invalid/image:latest",
    "111111111111.dkr.ecr.us-east-1.amazonaws.com/adp-test-l5-finops-executor@sha256:" + "a" * 64,
    "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-dev-l5-finops-executor@sha256:" + "a" * 64,
])
def test_finops_requires_selected_private_digest_before_grouped_mutation(campaign, value):
    args, config, calls, _ = campaign
    config["services"]["finops"]["executor_image_uri"] = value
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="FinOps executor image"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


def test_grouped_installation_requires_finops_image_coordinate(campaign):
    args, config, calls, _ = campaign
    del config["services"]["finops"]["executor_image_uri"]
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Each service must specify"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


@pytest.mark.parametrize("value", [
    None, "", "not-json", "[]", json.dumps({"surface_revision": "example-reviewed-surface"}),
    json.dumps({**SURFACE, "agent_identity_selector": ""}, sort_keys=True),
    json.dumps({**SURFACE, "surface_revision": 1}, sort_keys=True),
    json.dumps({**SURFACE, "unreviewed_selector": "[data-extra]"}, sort_keys=True),
    json.dumps({**SURFACE, "data_role_selector": ""}),
    json.dumps({**SURFACE, "data_role_selector": None}),
    json.dumps({**SURFACE, "data_role_selector": 1}),
], ids=["missing", "empty", "invalid-json", "array", "revision-only", "empty-selector", "numeric-revision", "extra-selector",
        "empty-data-role", "null-data-role", "numeric-data-role"])
def test_finops_rejects_unqualified_surface_before_grouped_mutation(campaign, value):
    args, config, calls, _ = campaign
    config["services"]["finops"]["executor_surface"] = value
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="FinOps executor surface"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


def test_grouped_installation_requires_finops_surface(campaign):
    args, config, calls, _ = campaign
    del config["services"]["finops"]["executor_surface"]
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Each service must specify"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


def test_destructive_last_plan_prevents_all_applies(campaign, monkeypatch):
    args, _, calls, normal = campaign

    def destructive(service, command, *others):
        if service == "finops" and command[0] == "show":
            return json.dumps({"resource_changes": [{"address": "queue", "change": {"actions": ["delete", "create"]}}]})
        return normal(service, command, *others)

    monkeypatch.setattr(deploy, "terraform", destructive)
    with pytest.raises(ValueError, match="deletes or replaces"):
        deploy.deploy(args)
    assert not any(command[0] == "apply" for _, command in calls)
    receipt = json.loads((args.receipt_dir / "finops" / "failure-receipt.json").read_text())
    assert receipt["category"] == "destructive_plan" and receipt["stage"] == "review_plan"
    assert receipt["qualification"] == "not_run"
    assert receipt["changes"] == [{"address": "queue", "actions": ["delete", "create"], "reason": "replacement_or_deletion"}]


def test_tainted_plan_retains_actionable_receipt_without_resource_values(campaign, monkeypatch):
    args, _, calls, normal = campaign
    address = "module.registration.aws_ssm_parameter.queue"

    def tainted(service, command, *others):
        if service == "security" and command[0] == "show":
            return json.dumps({"resource_changes": [{"address": address, "action_reason": "replace_because_tainted",
                               "change": {"actions": ["delete", "create"], "before": {"value": "private-old-value"},
                                          "after": {"value": "private-new-value"}}}]})
        return normal(service, command, *others)

    monkeypatch.setattr(deploy, "terraform", tainted)
    with pytest.raises(ValueError, match="tainted") as failure:
        deploy.deploy(args)
    receipt_text = (args.receipt_dir / "security" / "failure-receipt.json").read_text()
    receipt = json.loads(receipt_text)
    assert receipt["service"] == "security" and receipt["result"] == "failed"
    assert receipt["changes"] == [{"address": address, "actions": ["delete", "create"], "reason": "tainted"}]
    assert "verify creation completed" in receipt["next_action"]
    assert address in str(failure.value)
    assert "private-old-value" not in receipt_text + str(failure.value)
    assert "private-new-value" not in receipt_text + str(failure.value)
    assert not any(command[0] == "apply" for _, command in calls)


def test_incomplete_plan_retains_failure_before_any_apply(campaign, monkeypatch):
    args, _, calls, normal = campaign

    def incomplete(service, command, *others):
        if service == "security" and command[0] == "show":
            return json.dumps({"complete": False})
        return normal(service, command, *others)

    monkeypatch.setattr(deploy, "terraform", incomplete)
    with pytest.raises(ValueError, match="incomplete"):
        deploy.deploy(args)
    receipt = json.loads((args.receipt_dir / "security" / "failure-receipt.json").read_text())
    assert receipt["category"] == "incomplete_plan" and receipt["changes"] == []
    assert not any(command[0] == "apply" for _, command in calls)


def test_plan_never_applies(campaign):
    args, _, calls, _ = campaign
    args.action = "plan"
    deploy.deploy(args)
    assert not any(command[0] == "apply" for _, command in calls)


def test_missing_archive_proof_prevents_terraform(campaign, monkeypatch):
    args, _, calls, _ = campaign
    def reject(*_):
        raise ValueError("archive digest mismatch")
    monkeypatch.setattr(deploy.LIFECYCLE, "manifest_for", reject)
    with pytest.raises(ValueError, match="digest mismatch"):
        deploy.deploy(args)
    assert [service for service, _ in calls] == ["identity"]


def test_well_architected_alias_and_duplicate_selection():
    assert deploy.selection("well_architected,foundation,well-architected") == ["foundation", "well-architected"]
    with pytest.raises(ValueError):
        deploy.selection("../gateway")

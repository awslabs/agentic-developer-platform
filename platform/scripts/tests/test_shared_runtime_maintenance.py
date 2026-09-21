"""Offline maintenance scope tests. No test invokes production tools or APIs."""

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import shared_runtime_plan_guard as guard  # noqa: E402

SPEC = importlib.util.spec_from_file_location("shared_runtime_maintenance", SCRIPTS / "maintain-shared-runtime.py")
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)
IMAGE = f"{guard.ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64
KMS = f"arn:aws:kms:us-east-1:{guard.ACCOUNT}:key/test-key"
SIGNING_KEY = "offline-fixture-signing-value"


def resource(address, before, after, *, actions=None, unknown=None):
    return {"address": address, "mode": "managed", "change": {"actions": actions or ["update"], "before": before, "after": after, "after_unknown": unknown or {}}}


def check(resources, stage="prerequisites", enabled=False):
    return guard.check_plan({"resource_changes": resources}, stage=stage, enabled=enabled, gateway_image=IMAGE, signing_key=SIGNING_KEY, kms_key=KMS)


def wiring():
    return {
        "dispatch_queue_url": f"https://sqs.us-east-1.amazonaws.com/{guard.ACCOUNT}/adp-dev-agent-submit.fifo",
        "dispatch_queue_arn": f"arn:aws:sqs:us-east-1:{guard.ACCOUNT}:adp-dev-agent-submit.fifo",
        "webhook_events_table": "adp-dev-webhook-events", "webhook_events_kms_key_arn": KMS,
    }


def prerequisites():
    old_wiring = wiring()
    new_wiring = old_wiring | {"shared_worker_role_arn": guard.ROLE, "run_report_key_parameter": guard.KEY_NAME, **{k: False for k in guard.WIRING_FLAGS}}
    old_config = {"AGENT_AUTHORITY_ENABLED": "false", "UNCHANGED": "existing-value"}
    new_config = old_config | {"AGENT_WORKER_ROLE_ARN": guard.ROLE, **{k: "false" for k in guard.FLAGS}}
    return [
        resource(guard.KEY_ADDRESS, None, {"name": guard.KEY_NAME, "type": "SecureString", "key_id": KMS, "value": SIGNING_KEY}, actions=["create"]),
        resource(guard.WIRING_ADDRESS, {"value": json.dumps(old_wiring), "version": 4}, {"value": json.dumps(new_wiring), "version": None}, unknown={"version": True}),
        resource(guard.CONFIG_ADDRESS, {"data": old_config, "metadata": [{"name": "adp-worker-authority-config"}]}, {"data": new_config, "metadata": [{"name": "adp-worker-authority-config"}]}),
    ]


def tick_policy():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "PublishEngineDispatch", "Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": [wiring()["dispatch_queue_arn"]]},
            {"Sid": "EngineRuns", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem"], "Resource": [f"arn:aws:dynamodb:us-east-1:{guard.ACCOUNT}:table/adp-dev-webhook-events"], "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["orch:*"]}}},
            {"Sid": "EngineCommandEventsKMSDecrypt", "Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"], "Resource": [KMS]},
            {"Sid": "EngineCommandAckCredentials", "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": [f"arn:aws:secretsmanager:us-east-1:{guard.ACCOUNT}:secret:adp/dev/tenants/*"]},
        ],
    }


def tick_changes(enabled=False):
    policy = tick_policy()
    new_policy = copy.deepcopy(policy)
    new_policy["Statement"].append({"Sid": "ReadRunReportSigningMaterial", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": [guard.KEY_ARN]})
    env = {"BG_ORCH_DISPATCH_QUEUE_URL": wiring()["dispatch_queue_url"], "BG_ORCH_DISPATCH_REPO": "aws-e/adp", "BG_ORCH_DISPATCH_PERSONA": "developer", "BG_ORCH_DISPATCH_MAX_PER_TICK": "10", "WEBHOOK_EVENTS_TABLE": "adp-dev-webhook-events", "AGENT_AUTHORITY_ENABLED": "false"}
    new_env = env | {"AGENT_WORKER_ROLE_ARN": guard.ROLE, "AGENT_RUN_CREDENTIAL_KEY_PARAMETER": guard.KEY_NAME, **{k: str(enabled).lower() for k in guard.FLAGS}}
    return [
        resource(guard.IAM_ADDRESS, {"policy": json.dumps(policy), "role": "existing-role"}, {"policy": json.dumps(new_policy), "role": "existing-role"}),
        resource(guard.TICK_ADDRESS, {"image_uri": IMAGE, "environment": [{"variables": env}], "timeout": 60}, {"image_uri": IMAGE, "environment": [{"variables": new_env}], "timeout": 60}),
    ]


def test_safe_prerequisites_have_exactly_one_create_two_updates():
    changes = check(prerequisites())
    assert {r["address"] for r in changes} == guard.TARGETS["prerequisites"]
    assert [r["actions"] for r in changes].count(["create"]) == 1


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"], ["forget"]])
def test_destruction_refused_even_for_allowlisted_address(actions):
    with pytest.raises(guard.Refused, match="destruction|state removal"):
        check([resource(guard.KEY_ADDRESS, None, {}, actions=actions)])


def test_target_dependency_cannot_expand_scope():
    with pytest.raises(guard.Refused, match="unapproved resource"):
        check(prerequisites() + [resource("null_resource.keda_scaledjob", {}, {}, actions=["create"])])


@pytest.mark.parametrize("field,value", [("importing", {"id": "existing"}), ("previous_address", "aws_ssm_parameter.old")])
def test_no_op_import_and_state_move_are_refused(field, value):
    item = resource(guard.KEY_ADDRESS, {}, {}, actions=["no-op"])
    (item["change"] if field == "importing" else item)[field] = value
    with pytest.raises(guard.Refused, match="imports or moves"):
        check([item])


@pytest.mark.parametrize("field,value", [("value", "rotated"), ("key_id", "different-key"), ("type", "String"), ("overwrite", True)])
def test_signing_key_must_copy_current_material_without_overwrite(field, value):
    changes = prerequisites()
    changes[0]["change"]["after"][field] = value
    with pytest.raises(guard.Refused):
        check(changes)


def test_existing_signing_parameter_update_is_never_allowed():
    item = prerequisites()[0]
    item["change"]["before"] = copy.deepcopy(item["change"]["after"])
    item["change"]["actions"] = ["update"]
    with pytest.raises(guard.Refused, match="must not change"):
        check([item])


def test_unknown_signing_key_is_not_approved():
    item = prerequisites()[0]
    item["change"]["after_unknown"] = {"value": True}
    with pytest.raises(guard.Refused, match="unresolved"):
        check([item])


def test_unrelated_configmap_values_must_survive():
    changes = prerequisites()
    del changes[2]["change"]["after"]["data"]["UNCHANGED"]
    with pytest.raises(guard.Refused, match="unrelated gateway"):
        check(changes)


def test_gateway_enable_cannot_also_enable_tick_wiring():
    changes = prerequisites()
    for k in guard.FLAGS:
        changes[2]["change"]["after"]["data"][k] = "true"
    check([changes[2]], stage="gateway-enable", enabled=True)
    with pytest.raises(guard.Refused, match="unapproved resource"):
        check(changes[1:], stage="gateway-enable", enabled=True)


@pytest.mark.parametrize("enabled", [False, True])
def test_tick_adds_only_signing_read_and_expected_environment(enabled):
    assert len(check(tick_changes(enabled), stage="tick", enabled=enabled)) == 2


def test_existing_iam_permissions_are_preserved_semantically():
    changes = tick_changes()
    policy = json.loads(changes[0]["change"]["after"]["policy"])
    policy["Statement"].reverse()
    for statement in policy["Statement"]:
        statement["Action"].reverse()
    changes[0]["change"]["after"]["policy"] = json.dumps(policy)
    check(changes, stage="tick")
    policy["Statement"] = [s for s in policy["Statement"] if s["Sid"] != "PublishEngineDispatch"]
    changes[0]["change"]["after"]["policy"] = json.dumps(policy)
    with pytest.raises(guard.Refused, match="existing tick IAM"):
        check(changes, stage="tick")


def test_additional_iam_grant_is_refused():
    changes = tick_changes()
    policy = json.loads(changes[0]["change"]["after"]["policy"])
    policy["Statement"].append({"Sid": "Unreviewed", "Effect": "Allow", "Action": ["*"], "Resource": ["*"]})
    changes[0]["change"]["after"]["policy"] = json.dumps(policy)
    with pytest.raises(guard.Refused, match="unexpected new"):
        check(changes, stage="tick")


@pytest.mark.parametrize("mutation", ["image", "queue", "role", "timeout"])
def test_tick_code_and_unrelated_runtime_inputs_cannot_change(mutation):
    changes = tick_changes()
    after = changes[1]["change"]["after"]
    if mutation == "image":
        after["image_uri"] = IMAGE.replace("a" * 64, "b" * 64)
    elif mutation == "timeout":
        after["timeout"] = 120
    else:
        name = "BG_ORCH_DISPATCH_QUEUE_URL" if mutation == "queue" else "AGENT_WORKER_ROLE_ARN"
        after["environment"][0]["variables"][name] = "different"
    with pytest.raises(guard.Refused):
        check(changes, stage="tick")


def test_tick_overlay_preserves_live_ci_values_instead_of_empty_defaults():
    before = tick_changes()[1]["change"]["before"]
    result = guard.preserved_tick_inputs({"Environment": {"Variables": before["environment"][0]["variables"]}}, tick_policy(), wiring(), revision="b" * 40)
    assert result["orchestration_dispatch_queue_url"] == wiring()["dispatch_queue_url"]
    assert result["orchestration_webhook_events_table"] == "adp-dev-webhook-events"
    assert result["orchestration_github_app_secret_arn_pattern"].endswith("/tenants/*")
    assert result["orchestration_tick_image_tag"] == "b" * 40
    assert result["orchestration_agent_authority_enabled"] is False


def test_bad_saved_plan_is_never_applied(monkeypatch, tmp_path):
    calls = []
    def fake_command(args, **kwargs):
        calls.append(args)
        if args[1] == "plan":
            (tmp_path / "tick.tfplan").write_bytes(b"offline-plan")
            return ""
        if args[1] == "show":
            return json.dumps({"resource_changes": [resource("unapproved", {}, {})]})
        pytest.fail("Unapproved plan reached apply")
    monkeypatch.setattr(maintenance, "command", fake_command)
    with pytest.raises(guard.Refused, match="unapproved resource"):
        maintenance.plan_apply(maintenance.GATEWAY, "tick", {"gateway_image": IMAGE, "signing_key": SIGNING_KEY, "kms_key": KMS}, {}, tmp_path, enabled=False, execute=True)
    assert [call[1] for call in calls] == ["plan", "show"]


def test_verify_does_not_emit_false_flag_overlays_or_signing_material(monkeypatch, tmp_path):
    context = {"worker_image": "approved-worker", "gateway_image": IMAGE, "tick_overlay": {"retained": "input"}, "signing_key": SIGNING_KEY, "probe": {"flags": {k: "true" for k in guard.FLAGS}}, "queue": {}, "active_worker_images": []}
    monkeypatch.setattr(maintenance, "preflight", lambda *a: context)
    monkeypatch.setattr(sys, "argv", ["maintenance", "--account-id", guard.ACCOUNT, "--gateway-revision", "b" * 40, "--worker-revision", "c" * 40, "--worker-digest", "sha256:" + "d" * 64, "--stage", "verify", "--evidence-directory", str(tmp_path)])
    maintenance.main()
    assert [p.name for p in tmp_path.iterdir()] == ["verification.json"]
    assert SIGNING_KEY not in (tmp_path / "verification.json").read_text()


@pytest.mark.parametrize("difference", ["ID", "Who", "Operation", "Path", "Created"])
def test_unlock_refuses_any_other_lock_before_reading_github(monkeypatch, difference):
    info = {"ID": maintenance.LOCK_ID, "Who": maintenance.LOCK_OWNER, "Operation": "OperationTypePlan", "Path": maintenance.LOCK_PATH, "Created": "2026-09-20T23:34:38.654494254Z"}
    info[difference] = "different"
    monkeypatch.setattr(maintenance, "lock_info", lambda: info)
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: pytest.fail("Other lock must not reach commands"))
    with pytest.raises(guard.Refused, match="reviewed orphan"):
        maintenance.unlock_known_orphan()

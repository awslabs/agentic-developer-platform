"""Offline maintenance scope tests. No test invokes production tools or APIs."""

import ast
import base64
import copy
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

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


def test_only_rollout_wait_can_extend_the_kubernetes_request_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance, "command", lambda args, **kwargs: calls.append(args) or "")
    maintenance.kube("get", "pods")
    maintenance.kube("rollout", "restart", "deployment/bedrockgateway")
    maintenance.kube("rollout", "status", "deployment/bedrockgateway", "--timeout=300s", request_timeout="330s")
    assert calls == [
        ["kubectl", "--request-timeout=30s", "get", "pods"],
        ["kubectl", "--request-timeout=30s", "rollout", "restart", "deployment/bedrockgateway"],
        ["kubectl", "--request-timeout=330s", "rollout", "status", "deployment/bedrockgateway", "--timeout=300s"],
    ]


@pytest.mark.parametrize("already_enabled,execute", [(False, True), (True, True), (False, False)])
def test_gateway_enable_waits_for_full_rollout_even_if_one_pod_is_enabled(monkeypatch, tmp_path, already_enabled, execute):
    context = {"worker_image": "approved-worker", "gateway_image": IMAGE, "tick_overlay": {}, "signing_key": SIGNING_KEY,
               "probe": {"flags": {k: str(already_enabled).lower() for k in guard.FLAGS}}, "queue": {}, "active_worker_images": []}
    calls = []
    probes = []
    monkeypatch.setattr(maintenance, "preflight", lambda *a: context)
    monkeypatch.setattr(maintenance, "init", lambda *a: None)
    monkeypatch.setattr(maintenance, "plan_apply", lambda *a, **kwargs: None)
    monkeypatch.setattr(maintenance, "parameter", lambda name, **kwargs: SIGNING_KEY if name == guard.KEY_NAME else json.dumps({k: False for k in guard.WIRING_FLAGS}))
    monkeypatch.setattr(maintenance, "kube", lambda *args, **kwargs: calls.append((args, kwargs)) or "")
    monkeypatch.setattr(maintenance, "gateway_probe", lambda: probes.append(True) or {"flags": {k: "true" for k in guard.FLAGS}})
    argv = ["maintenance", "--account-id", guard.ACCOUNT, "--gateway-revision", "b" * 40, "--worker-revision", "c" * 40,
            "--worker-digest", "sha256:" + "d" * 64, "--stage", "gateway-enable", "--evidence-directory", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv + (["--execute"] if execute else []))
    maintenance.main()
    expected = []
    if execute:
        expected.append((("rollout", "restart", "deployment/bedrockgateway", "-n", maintenance.NAMESPACE), {}))
        expected.append((("rollout", "status", "deployment/bedrockgateway", "-n", maintenance.NAMESPACE, "--timeout=300s"), {"request_timeout": "330s"}))
    assert calls == expected
    assert len(probes) == int(execute)


@pytest.mark.parametrize("case", ["replacement_ready", "not_ready", "wrong_digest", "missing_status", "extra_pod", "wait_failed"])
def test_preflight_waits_before_fresh_snapshots_and_reports_safe_readiness(monkeypatch, tmp_path, capsys, case):
    calls = []
    state = {"waited": False}
    container = {"name": maintenance.DEPLOYMENT, "image": IMAGE,
                 "envFrom": [{"configMapRef": {"name": "adp-worker-authority-config"}}],
                 "env": [{"name": "AGENT_RUN_CREDENTIAL_KEY", "valueFrom": {"secretKeyRef": {"name": "agent-authority-signing", "key": "run-credential-key"}}},
                         {"name": "PRIVATE", "value": "private-env-fixture"}]}
    deployment = {"spec": {"replicas": 1, "selector": {"matchLabels": {"app": "gateway"}}, "template": {"spec": {"containers": [container]}}}}
    pod = {"metadata": {"name": "replacement-pod"}, "spec": {"containers": [container]},
           "status": {"containerStatuses": [{"name": maintenance.DEPLOYMENT, "ready": False, "imageID": "containerd://" + IMAGE}],
                      "private-log": "private-log-fixture"}}
    def aws(*args):
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": guard.ACCOUNT, "Arn": "reviewed-maintenance-role"}
        calls.append("after-pod-checks")
        raise RuntimeError("reached later preflight checks")
    def snapshot(*args):
        assert state["waited"], "deployment snapshot must be refreshed after the wait"
        calls.append("deployment-snapshot")
        return copy.deepcopy(deployment)
    def kube(*args, **kwargs):
        if args[:2] == ("rollout", "status"):
            calls.append("rollout-wait")
            assert args[-1] == "--timeout=300s" and kwargs == {"request_timeout": "330s"}
            state["waited"] = True
            pod["status"]["containerStatuses"][0]["ready"] = case != "not_ready"
            if case == "wait_failed":
                raise guard.Refused("rollout wait failed")
            return "rollout complete"
        assert args[:2] == ("get", "pods") and state["waited"]
        calls.append("pod-snapshot")
        if case == "wrong_digest":
            pod["status"]["containerStatuses"][0]["imageID"] = "containerd://" + IMAGE[:-1] + "b"
        if case == "missing_status":
            pod["status"]["containerStatuses"] = []
        pods = [pod]
        if case == "extra_pod":
            pods.append({**pod, "metadata": {"name": "unexpected-second-pod"}})
        return json.dumps({"items": pods})
    monkeypatch.setattr(maintenance, "aws", aws)
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: "")
    monkeypatch.setattr(maintenance, "checked_image", lambda *a, **k: IMAGE)
    monkeypatch.setattr(maintenance, "snapshot", snapshot)
    monkeypatch.setattr(maintenance, "kube", kube)
    args = SimpleNamespace(account_id=guard.ACCOUNT, stage="gateway-enable", gateway_revision="b" * 40,
                           worker_revision="c" * 40, worker_digest="sha256:" + "d" * 64)
    errors = {"replacement_ready": "reached later preflight checks", "not_ready": "gateway pod is not ready",
              "wrong_digest": "gateway pod image digest differs", "missing_status": "container status is missing",
              "extra_pod": "gateway rollout incomplete", "wait_failed": "rollout wait failed"}
    with pytest.raises(RuntimeError if case == "replacement_ready" else guard.Refused, match=errors[case]):
        maintenance.preflight(args, tmp_path)
    assert calls[:3] == ["rollout-wait", "deployment-snapshot", "pod-snapshot"]
    assert ("after-pod-checks" in calls) is (case == "replacement_ready")
    output = capsys.readouterr().out
    assert "private-" not in output
    projection = json.loads(output.splitlines()[-1])["gateway_pod_readiness"]
    assert projection["expected_image"] == IMAGE and projection["desired_replicas"] == 1
    assert projection["pods"][0]["pod"] == "replacement-pod" and projection["pods"][0]["spec_image"] == IMAGE
    assert set(projection["pods"][0]) == {"pod", "terminating", "status_count", "ready", "spec_image", "image_id"}


@pytest.mark.parametrize("difference", ["ID", "Who", "Operation", "Path", "Created"])
def test_unlock_refuses_any_other_lock_before_reading_github(monkeypatch, difference):
    info = {"ID": maintenance.LOCK_ID, "Who": maintenance.LOCK_OWNER, "Operation": "OperationTypePlan", "Path": maintenance.LOCK_PATH, "Created": "2026-09-20T23:34:38.654494254Z"}
    info[difference] = "different"
    monkeypatch.setattr(maintenance, "lock_info", lambda: info)
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: pytest.fail("Other lock must not reach commands"))
    with pytest.raises(guard.Refused, match="reviewed orphan"):
        maintenance.unlock_known_orphan()


def test_absent_lock_uses_explicit_nullable_projection_and_never_unlocks(monkeypatch, capsys):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args[:3] == ["aws", "dynamodb", "get-item"]
        assert args[args.index("--query") + 1] == "{Item: Item}"
        assert "--consistent-read" in args
        # Without this projection, the real AWS CLI prints an empty body for
        # the same successful missing-item service response.
        return '{"Item": null}'

    monkeypatch.setattr(maintenance, "command", command)
    maintenance.unlock_known_orphan()
    assert len(calls) == 1
    assert "no unlock performed" in capsys.readouterr().out


@pytest.mark.parametrize("response", ["", "invalid", "{}", '{"Item": []}', '{"Item": {}}',
                                      '{"Item": {"Info": {"S": ""}}}', '{"Item": {"Info": {"S": "null"}}}',
                                      '{"Item": {"Info": {"S": "[]"}}}', '{"Item": {"Info": {"S": 7}}}'])
def test_empty_malformed_or_incomplete_lock_response_is_not_unlocked(monkeypatch, response):
    calls = []
    monkeypatch.setattr(maintenance, "command", lambda args, **kwargs: calls.append(args) or response)
    with pytest.raises((guard.Refused, json.JSONDecodeError)):
        maintenance.unlock_known_orphan()
    assert len(calls) == 1 and calls[0][:3] == ["aws", "dynamodb", "get-item"]


def test_present_lock_preserves_ownership_information(monkeypatch):
    info = {"ID": maintenance.LOCK_ID, "Who": maintenance.LOCK_OWNER, "Operation": "OperationTypePlan"}
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: json.dumps({"Item": {"Info": {"S": json.dumps(info)}}}))
    assert maintenance.lock_info() == info


def test_json_failure_identifies_static_source_and_locations_without_private_data(monkeypatch, capsys):
    secret = "private-json-document-fixture"
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: secret)
    monkeypatch.setattr(maintenance, "main", lambda: maintenance.aws("ssm", "get-parameter", "--name", secret))
    assert maintenance.entrypoint() == 1
    output = capsys.readouterr()
    assert secret not in output.err and output.out == ""
    result = json.loads(output.err)["maintenance_stopped"]
    assert result["failure_type"] == "JSONDecodeError"
    assert result["json_source"] == "aws.ssm.parameter"
    assert any(frame["filename"] == "maintain-shared-runtime.py" and frame["function"] == "aws" for frame in result["locations"])
    assert all(set(frame) == {"filename", "function", "lineno"} and isinstance(frame["lineno"], int) for frame in result["locations"])


@pytest.mark.parametrize("error_kind", [ValueError, guard.Refused])
def test_failure_reporting_never_formats_values_source_locals_or_chained_errors(monkeypatch, capsys, error_kind):
    namespace = {"error_kind": error_kind}
    # Both the source and a local/exception contain sentinels. A normal Python
    # traceback or format_exception would reveal them; code locations do not.
    exec(compile(
        "def failing():\n"
        "    private = 'private-local-fixture'\n"
        "    try:\n"
        "        raise RuntimeError('private-cause-fixture')\n"
        "    except RuntimeError as cause:\n"
        "        raise error_kind(private + 'private-exception-fixture') from cause\n",
        "/private-directory-fixture/failing.py", "exec",
    ), namespace)
    monkeypatch.setattr(maintenance, "main", namespace["failing"])
    assert maintenance.entrypoint() == 1
    output = capsys.readouterr()
    assert "private-" not in output.err and output.out == ""
    result = json.loads(output.err)["maintenance_stopped"]
    assert result["locations"][-1] == {"filename": "failing.py", "function": "failing", "lineno": 6}
    assert result["json_source"] is None


def test_untrusted_exception_source_label_is_not_emitted():
    error = ValueError("private-error-fixture")
    for value in ["private-source-fixture", {"private": "source"}]:
        error.maintenance_json_source = value
        assert maintenance.failure_location(error)["json_source"] is None


def test_other_work_context_is_bounded_and_preserves_orphan_and_tenant_mismatch(monkeypatch):
    scripts = []
    monkeypatch.setattr(maintenance, "snapshot", lambda *a: {"data": {"run-credential-key": base64.b64encode(b"private-key-fixture").decode()}})
    monkeypatch.setattr(maintenance, "kube", lambda *a, stdin=None: scripts.append(stdin) or '{"key_matches": true}')
    maintenance.gateway_probe()
    literals = [node.value for node in ast.walk(ast.parse(scripts[0])) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    query = next(value for value in literals if value.startswith("SELECT n.org_id"))
    count_query = next(value for value in literals if value.startswith("SELECT count(*) FROM orchestration_nodes"))
    assert "SET TRANSACTION READ ONLY" in literals
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("CREATE TABLE orchestration_flows (id TEXT, org_id TEXT, state TEXT); CREATE TABLE orchestration_nodes (id TEXT, org_id TEXT, flow_id TEXT, state TEXT, attempts INTEGER, issue_ref TEXT, kind TEXT, title TEXT);")
    connection.executemany("INSERT INTO orchestration_flows VALUES (?, ?, ?)", [("inactive", "other", "failed"), ("mismatch", "wrong-tenant", "running")])
    rows = [(f"node-{i:02d}", "other", "inactive", "ready", 0, str(100+i), "story", "private-title-fixture") for i in range(22)]
    rows += [("orphan", "first", "orphan", "running", 1, "500", "story", "private-title-fixture"),
             ("mismatch", "first", "mismatch", "ready", 0, "501", "story", "private-title-fixture"),
             ("target", "aws-e", "0737183c-99c4-4e1f-bdb7-e4432b46ca20", "ready", 0, "5621", "story", "private-title-fixture")]
    connection.executemany("INSERT INTO orchestration_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    assert connection.execute(count_query).fetchone()[0] == 24
    actual = [dict(row) for row in connection.execute(query)]
    assert len(actual) == 20
    assert all(set(row) == {"org_id", "flow_id", "flow_state", "node_id", "state", "attempts", "issue_ref"} for row in actual)
    assert {row["node_id"] for row in actual if row["flow_state"] is None} == {"orphan", "mismatch"}
    assert any(row["flow_state"] == "failed" for row in actual)
    assert "private-" not in json.dumps(actual)
    connection.close()


def other_work(**overrides):
    facts = {"contract": "shared-runtime-dispatch-preflight/v1", "routing_blocker": None,
             "installation_id": 42, "approval_decision_id": "approval", "human_approval_verified": True}
    facts.update(overrides)
    row = {"org_id": "other", "flow_id": "legacy", "node_id": "node", "flow_state": "pending",
           "state": "ready", "attempts": 0, "issue_ref": "4213", "dispatch_preflight": facts}
    return {"other_ready_or_running_stories": 1, "other_ready_or_running_nodes": [row],
            "other_ready_or_running_nodes_truncated": False}


@pytest.mark.parametrize("blocker", [dict(routing_blocker="missing_issue_ref"), dict(routing_blocker="malformed_issue_ref"),
                                   dict(installation_id=None), dict(approval_decision_id=None, human_approval_verified=False),
                                   dict(human_approval_verified=False)])
def test_ready_work_requires_a_proven_early_dispatch_blocker(blocker):
    guard.check_other_work(other_work(**blocker))


def test_pending_flow_or_missing_policy_is_not_an_early_blocker():
    probe = other_work()
    probe["other_ready_or_running_nodes"][0]["execution_policy"] = None
    with pytest.raises(guard.Refused, match="may dispatch"):
        guard.check_other_work(probe)


@pytest.mark.parametrize("blocker", [dict(routing_blocker="missing_issue_ref"), dict(installation_id=None), dict(human_approval_verified=False)])
def test_running_work_is_never_ignored_when_its_dispatch_authority_disappears(blocker):
    probe = other_work(**blocker)
    probe["other_ready_or_running_nodes"][0]["state"] = "running"
    with pytest.raises(guard.Refused, match="running work"):
        guard.check_other_work(probe)


@pytest.mark.parametrize("case", ["truncated", "missing_count", "boolean_count", "count_mismatch", "missing_rows", "duplicate", "unknown_attempt",
                                 "missing_scope", "missing_facts", "unknown_contract", "missing_approval", "unknown_routing",
                                 "bad_installation", "bad_approval_flag", "contradictory_approval"])
def test_incomplete_or_unknown_dispatch_evidence_stops_activation(case):
    probe = other_work(installation_id=None)
    rows = probe["other_ready_or_running_nodes"]
    facts = rows[0]["dispatch_preflight"]
    if case == "truncated":
        probe["other_ready_or_running_nodes_truncated"] = True
    elif case == "missing_count":
        probe.pop("other_ready_or_running_stories")
    elif case == "boolean_count":
        probe["other_ready_or_running_stories"] = True
    elif case == "count_mismatch":
        probe["other_ready_or_running_stories"] = 2
    elif case == "missing_rows":
        probe.pop("other_ready_or_running_nodes")
    elif case == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
        probe["other_ready_or_running_stories"] = 2
    elif case == "missing_scope":
        rows[0].pop("org_id")
    elif case == "unknown_attempt":
        rows[0]["attempts"] = None
    elif case == "missing_facts":
        rows[0].pop("dispatch_preflight")
    elif case == "unknown_contract":
        facts["contract"] = "unknown"
    elif case == "missing_approval":
        facts.pop("human_approval_verified")
    elif case == "unknown_routing":
        facts["routing_blocker"] = "unknown"
    elif case == "bad_installation":
        facts["installation_id"] = "unknown"
    elif case == "bad_approval_flag":
        facts["human_approval_verified"] = "false"
    else:
        facts["approval_decision_id"] = None
    with pytest.raises(guard.Refused):
        guard.check_other_work(probe)


def test_empty_inventory_is_explicit_and_complete():
    guard.check_other_work({"other_ready_or_running_stories": 0, "other_ready_or_running_nodes": [],
                           "other_ready_or_running_nodes_truncated": False})


@pytest.mark.parametrize("approval_state", ["missing", "refused", "authorized", "different_flow", "unavailable"])
def test_probe_invokes_dispatch_helpers_with_actual_tenant_and_flow(monkeypatch, approval_state):
    scripts = []
    monkeypatch.setattr(maintenance, "snapshot", lambda *a: {"data": {"run-credential-key": base64.b64encode(b"private-key-fixture").decode()}})
    monkeypatch.setattr(maintenance, "kube", lambda *a, stdin=None: scripts.append(stdin) or '{"key_matches": true}')
    maintenance.gateway_probe()
    definition = next(node for node in ast.parse(scripts[0]).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_preflight")
    calls = []
    class GenesisRefusedError(Exception):
        pass
    def routing(**kwargs):
        calls.append(("routing", kwargs))
        return SimpleNamespace(value="missing_issue_ref")
    async def installation(session, **kwargs):
        calls.append(("installation", session, kwargs))
        return None
    async def approval(session, **kwargs):
        calls.append(("approval", session, kwargs))
        return None if approval_state == "missing" else "decision"
    async def genesis(session, **kwargs):
        calls.append(("genesis", session, kwargs))
        if approval_state == "unavailable":
            raise RuntimeError("unavailable")
        if approval_state == "refused":
            raise GenesisRefusedError("unattributed approval")
        return SimpleNamespace(flow_id="other" if approval_state == "different_flow" else "legacy")
    namespace = {"routing_blocker_for_node": routing, "resolve_installation_id": installation,
                 "_latest_approval_decision_id": approval, "resolve_engine_genesis": genesis, "GenesisRefusedError": GenesisRefusedError}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "probe", "exec"), namespace)
    operation = namespace["dispatch_preflight"]("session", {"org_id": "tenant", "flow_id": "legacy", "issue_ref": None})
    if approval_state == "unavailable":
        with pytest.raises(RuntimeError, match="unavailable"):
            asyncio.run(operation)
        return
    result = asyncio.run(operation)
    assert result["routing_blocker"] == "missing_issue_ref" and result["installation_id"] is None
    assert result["human_approval_verified"] is (approval_state == "authorized")
    assert calls[:3] == [("routing", {"kind": "story", "issue_ref": None}),
                         ("installation", "session", {"org_id": "tenant"}),
                         ("approval", "session", {"org_id": "tenant", "flow_id": "legacy"})]
    if approval_state != "missing":
        assert calls[-1] == ("genesis", "session", {"org_id": "tenant", "decision_id": "decision"})

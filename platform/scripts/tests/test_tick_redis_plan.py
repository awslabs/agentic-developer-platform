"""Adversarial checks for a Redis-only saved plan and stale live snapshots."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import tick_redis_plan_guard as guard  # noqa: E402
from shared_runtime_plan_guard import ACCOUNT, REGION, Refused  # noqa: E402

SPEC = importlib.util.spec_from_file_location("tick_redis_maintenance", SCRIPTS / "maintain-tick-redis.py")
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


def context():
    return {
        "image_uri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-gateway@sha256:" + "a" * 64,
        "configuration": {"Role": f"arn:aws:iam::{ACCOUNT}:role/adp-dev-orchestration-tick-role", "RevisionId": "revision", "VpcConfig": {"SecurityGroupIds": ["sg-tick"]}, "Environment": {"Variables": {"KEEP_SECRET": "never-print-fixture", "AGENT_AUTHORITY_ENABLED": "false", "ADP_SHARED_WORKER_CONTINUATION_ENABLED": "true"}}},
        "expected_env": {"BG_REDIS_URL": "rediss://existing.cache.amazonaws.com:6379/0", "BG_REDIS_IAM_AUTH": "true", "BG_REDIS_USERNAME": "user", "BG_REDIS_CACHE_NAME": "cache"},
        "redis_sg": "sg-redis", "port": 6379,
        "resource_arns": [f"arn:aws:elasticache:{REGION}:{ACCOUNT}:replicationgroup:cache", f"arn:aws:elasticache:{REGION}:{ACCOUNT}:user:user"],
    }


def resource(address, before, after, unknown=None):
    return {"address": address, "mode": "managed", "change": {"actions": ["create" if before is None else "update"], "before": before, "after": after, "after_unknown": unknown or {}}}


def plan(ctx):
    before = {"function_name": "adp-dev-orchestration-tick", "role": ctx["configuration"]["Role"], "image_uri": ctx["image_uri"], "environment": [ctx["configuration"]["Environment"]], "timeout": 300, "last_modified": "before"}
    after = deepcopy(before)
    # AWS provider's environment blocks use lowercase variables; AWS API uses uppercase.
    before["environment"] = [{"variables": ctx["configuration"]["Environment"]["Variables"]}]
    after["environment"] = [{"variables": ctx["configuration"]["Environment"]["Variables"] | ctx["expected_env"]}]
    after["last_modified"] = None
    egress = {"description": "existing HTTPS", "from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": [], "prefix_list_ids": [], "security_groups": [], "self": False}
    return {"complete": False, "resource_changes": [
        resource(guard.LAMBDA, before, after, {"last_modified": True}),
        resource(guard.SG, {"id": "sg-tick", "vpc_id": "vpc-1", "egress": [egress]}, {"id": "sg-tick", "vpc_id": "vpc-1", "egress": [egress, guard.egress_rule(ctx)]}),
        resource(guard.INGRESS, None, {"description": "Redis budget access from the orchestration tick", "type": "ingress", "protocol": "tcp", "from_port": 6379, "to_port": 6379, "security_group_id": "sg-redis", "source_security_group_id": "sg-tick", "self": False, "cidr_blocks": None, "ipv6_cidr_blocks": None, "prefix_list_ids": None}, {"id": True, "security_group_rule_id": True}),
        resource(guard.IAM, None, {"name": "adp-dev-orchestration-tick-redis", "role": "adp-dev-orchestration-tick-role", "policy": json.dumps(guard.redis_policy(ctx))}, {"id": True, "name_prefix": True}),
    ]}


def test_exact_additions_and_preserved_image_settings_are_allowed():
    ctx = context()
    changes = guard.check_plan(plan(ctx), ctx)
    assert {row["address"] for row in changes} == guard.TARGETS
    assert "never-print-fixture" not in json.dumps(changes)
    assert "rediss://" not in json.dumps(changes)


@pytest.mark.parametrize("field,value", [("image_uri", "other-image"), ("timeout", 900), ("role", "other-role")])
def test_unrelated_lambda_change_is_rejected(field, value):
    ctx = context()
    data = plan(ctx)
    data["resource_changes"][0]["change"]["after"][field] = value
    with pytest.raises(Refused):
        guard.check_plan(data, ctx)


@pytest.mark.parametrize("mutation", ["remove_setting", "authority_enabled", "wrong_redis", "unresolved", "stale_baseline"])
def test_environment_and_baseline_are_exact(mutation):
    ctx = context()
    data = plan(ctx)
    change = data["resource_changes"][0]["change"]
    env = change["after"]["environment"][0]["variables"]
    if mutation == "remove_setting":
        env.pop("KEEP_SECRET")
    elif mutation == "authority_enabled":
        env["AGENT_AUTHORITY_ENABLED"] = "true"
    elif mutation == "wrong_redis":
        env["BG_REDIS_URL"] = "other-store"
    elif mutation == "unresolved":
        change["after_unknown"]["environment"] = True
    else:
        change["before"]["environment"] = [{"variables": {}}]
    with pytest.raises(Refused) as error:
        guard.check_plan(data, ctx)
    assert "never-print-fixture" not in str(error.value)


@pytest.mark.parametrize("mutation", ["cidr", "port", "peer", "remove_old", "ingress_cidr", "ingress_peer", "new_security_group"])
def test_network_cannot_widen_or_replace_existing_rules(mutation):
    ctx = context()
    data = plan(ctx)
    change = data["resource_changes"][1]["change"]
    egress = change["after"]["egress"][-1]
    if mutation == "cidr":
        egress["cidr_blocks"] = ["0.0.0.0/0"]
    elif mutation == "port":
        egress["from_port"] = 0
    elif mutation == "peer":
        egress["security_groups"] = ["sg-other"]
    elif mutation == "remove_old":
        change["after"]["egress"].pop(0)
    elif mutation == "new_security_group":
        change["actions"] = ["delete", "create"]
    else:
        incoming = data["resource_changes"][2]["change"]["after"]
        incoming["cidr_blocks" if mutation == "ingress_cidr" else "source_security_group_id"] = ["0.0.0.0/0"] if mutation == "ingress_cidr" else "sg-other"
    with pytest.raises(Refused):
        guard.check_plan(data, ctx)


@pytest.mark.parametrize("mutation", ["wildcard_resource", "extra_action", "other_role", "update_existing", "unrelated_policy", "unresolved"])
def test_iam_cannot_change_existing_permissions_or_expand_redis_scope(mutation):
    ctx = context()
    data = plan(ctx)
    row = data["resource_changes"][-1]
    change = row["change"]
    if mutation in {"wildcard_resource", "extra_action"}:
        policy = json.loads(change["after"]["policy"])
        policy["Statement"][0]["Resource" if mutation == "wildcard_resource" else "Action"] = ["*"]
        change["after"]["policy"] = json.dumps(policy)
    elif mutation == "other_role":
        change["after"]["role"] = "agent-worker-role"
    elif mutation == "update_existing":
        change["actions"] = ["update"]
        change["before"] = deepcopy(change["after"])
    elif mutation == "unrelated_policy":
        row["address"] = guard.PREFIX + "aws_iam_role_policy.tick"
    else:
        change["after_unknown"]["policy"] = True
    with pytest.raises(Refused):
        guard.check_plan(data, ctx)


@pytest.mark.parametrize("mutation", ["image", "revision", "env", "role", "network"])
def test_freshness_check_refuses_live_change(mutation):
    ctx = context()
    function = {"Configuration": deepcopy(ctx["configuration"]), "Code": {"ResolvedImageUri": ctx["image_uri"]}}
    if mutation == "image":
        function["Code"]["ResolvedImageUri"] = "changed"
    else:
        key = {"revision": "RevisionId", "env": "Environment", "role": "Role", "network": "VpcConfig"}[mutation]
        function["Configuration"][key] = "changed"
    with pytest.raises(Refused):
        maintenance.snapshot_identity(ctx, function)


def test_incomplete_diagnosis_stops_before_terraform(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance.tick_redis.diag, "identity", lambda *a: None)
    monkeypatch.setattr(maintenance.tick_redis, "collect", lambda: ({}, {"complete": False}))
    monkeypatch.setattr(maintenance, "command", lambda *a, **k: pytest.fail("must not plan"))
    with pytest.raises(Refused, match="diagnosis is incomplete"):
        maintenance.run_stage(ACCOUNT, tmp_path, True)
    assert json.loads((tmp_path / "redis-before.json").read_text()) == {"complete": False}


def test_no_secret_values_can_enter_artifacts_or_override_existing_file(tmp_path):
    target = tmp_path / "override.json"
    target.write_text("operator file")
    with pytest.raises(FileExistsError):
        maintenance.write_private(target, {"value": "secret"})
    assert target.read_text() == "operator file"


def test_tick_redis_stage_has_release_lane_and_no_stale_image_requirement():
    workflow = (SCRIPTS.parents[1] / ".github/workflows/shared-runtime-maintenance.yml").read_text()
    assert 'elif [[ "$ROLLOUT_STAGE" == tick-redis ]]; then' in workflow
    assert "python platform/scripts/maintain-tick-redis.py" in workflow
    assert "'redis-diagnose') && 'gateway-diagnostics-dev' || 'gateway-release-dev'" in workflow


def test_partial_plan_cannot_skip_a_diagnosed_prerequisite():
    ctx = context()
    ctx["required_changes"] = guard.TARGETS
    data = plan(ctx)
    data["resource_changes"].pop()
    with pytest.raises(Refused, match="missing prerequisite"):
        guard.check_plan(data, ctx)


def test_equivalent_empty_network_collections_may_be_null_in_saved_plan():
    ctx = context()
    data = plan(ctx)
    rule = data["resource_changes"][1]["change"]["after"]["egress"][-1]
    for key in ("cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids"):
        rule[key] = None
    assert len(guard.check_plan(data, ctx)) == 4


def test_pre_guard_plan_shape_exposes_only_safe_addresses_and_actions():
    data = plan(context())
    data["resource_changes"].append(resource("module.redis[0].aws_elasticache_user.unrelated", {"secret": "hidden"}, {"secret": "changed-hidden"}))
    shape = guard.plan_shape(data)
    assert shape[-1] == {"address": "module.redis[0].aws_elasticache_user.unrelated", "actions": ["update"]}
    assert "hidden" not in json.dumps(shape) and "never-print-fixture" not in json.dumps(shape)
    data["resource_changes"][-1]["address"] = 'module.secret["sensitive-key"].resource.name'
    with pytest.raises(Refused, match="unprojectable"):
        guard.plan_shape(data)


def test_existing_policy_projection_compares_without_disclosing_values():
    old = {"Version": "2012-10-17", "Statement": [{"Sid": "RDSConnect", "Action": ["old-secret-action"], "Resource": "secret-resource"}, {"Sid": "secret-custom-sid", "Resource": "secret-resource"}]}
    new = deepcopy(old)
    new["Statement"][0]["Action"] = ["new-secret-action"]
    data = {"resource_changes": [resource(guard.EXISTING_IAM, {"policy": json.dumps(old), "id": "old-secret-id"}, {"policy": json.dumps(new), "id": "new-secret-id"})]}
    facts = guard.existing_policy_facts(data, old)[0]
    assert facts["changed_fields"] == ["id", "policy"]
    assert facts["before_matches_live_policy"] and not facts["after_matches_live_policy"]
    assert not facts["normalized_policy_unchanged"] and facts["policy_metadata_unchanged"]
    assert facts["statements"] == [{"sid": "RDSConnect", "before_count": 1, "after_count": 1, "unchanged": False, "changed_fields": ["Action"]}]
    assert facts["unprojected_statement_counts"] == {"before": 1, "after": 1}
    assert "secret" not in json.dumps(facts)
    with pytest.raises(Refused, match="unrelated resource"):
        guard.check_plan(data, context())


def test_existing_policy_projection_distinguishes_formatting_and_unknowns():
    policy = {"Version": "2012-10-17", "Statement": [{"Sid": "RDSConnect", "Action": ["two", "one"]}]}
    reordered = {"Statement": [{"Action": ["one", "two"], "Sid": "RDSConnect"}], "Version": "2012-10-17"}
    data = {"resource_changes": [resource(guard.EXISTING_IAM, {"policy": json.dumps(policy)}, {"policy": json.dumps(reordered)})]}
    facts = guard.existing_policy_facts(data, policy)[0]
    assert facts["changed_fields"] == ["policy"] and facts["normalized_policy_unchanged"]
    assert facts["before_matches_live_policy"] and facts["after_matches_live_policy"]
    change = data["resource_changes"][0]["change"]
    change["after"] = {"policy": None}
    change["after_unknown"] = {"policy": True}
    facts = guard.existing_policy_facts(data, policy)[0]
    assert facts["unresolved_fields"] == ["policy"] and not facts["after_policy_available"]


@pytest.mark.parametrize("drift", [None, "before_apply", "after_apply"])
def test_maintenance_preserves_existing_policy_and_checks_both_sides_of_apply(monkeypatch, tmp_path, capsys, drift):
    ctx = context()
    policy = {"Version": "2012-10-17", "Statement": [{"Sid": "RDSConnect", "Effect": "Allow", "Action": ["fixture:Read"], "Resource": "secret-resource"}]}
    state = {"applied": False, "policy_reads": 0}
    module = tmp_path / "gateway" / "modules" / "tick"
    module.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "GATEWAY", module.parents[1])
    monkeypatch.setattr(maintenance, "MODULE", module)
    monkeypatch.setattr(maintenance.tick_redis.diag, "identity", lambda *a: None)
    monkeypatch.setattr(maintenance, "preserved_tick_inputs", lambda *a, **k: {})
    def collect():
        present = state["applied"]
        evidence = {"complete": True, "checks": {"tick": {"redis_settings": {key: {"matches_gateway_store": present} for key in ctx["expected_env"]}},
                    "network": {"tick_to_redis_egress": present, "redis_from_tick_ingress": present}, "iam": {"elasticache_connect_allowed": present}}}
        return deepcopy(ctx), evidence
    monkeypatch.setattr(maintenance.tick_redis, "collect", collect)
    def aws(*args):
        if args[:2] == ("lambda", "get-function"):
            config = deepcopy(ctx["configuration"])
            if state["applied"]:
                config["Environment"]["Variables"].update(ctx["expected_env"])
            return {"Configuration": config, "Code": {"ImageUri": ctx["image_uri"], "ResolvedImageUri": ctx["image_uri"]}}
        if args[:2] == ("iam", "get-role-policy"):
            state["policy_reads"] += 1
            current = deepcopy(policy)
            if drift == "before_apply" and state["policy_reads"] > 1 or drift == "after_apply" and state["applied"]:
                current["Statement"][0]["Resource"] = "changed-secret-resource"
            return {"PolicyDocument": current}
        assert args[:2] == ("ssm", "get-parameter")
        return {"Parameter": {"Value": "{}"}}
    monkeypatch.setattr(maintenance, "aws", aws)
    def command(args, **kwargs):
        if args[:2] == ["terraform", "plan"]:
            Path(next(arg.removeprefix("-out=") for arg in args if arg.startswith("-out="))).write_bytes(b"private-fixture-plan")
            state["override"] = json.loads((module / "zz_tick_redis_maintenance_override.tf.json").read_text())
        if args[:2] == ["terraform", "show"]:
            data = plan(ctx)
            old = {"name": "adp-dev-orchestration-tick-policy", "role": "adp-dev-orchestration-tick-role", "policy": json.dumps(policy)}
            retained = resource(guard.EXISTING_IAM, old, deepcopy(old))
            retained["change"]["actions"] = ["no-op"]
            data["resource_changes"].append(retained)
            return json.dumps(data)
        if args[:2] == ["terraform", "apply"]:
            state["applied"] = True
        return ""
    monkeypatch.setattr(maintenance, "command", command)
    if drift:
        with pytest.raises(Refused, match="existing tick permissions changed"):
            maintenance.run_stage(ACCOUNT, tmp_path / "evidence", True)
    else:
        maintenance.run_stage(ACCOUNT, tmp_path / "evidence", True)
    retained = state["override"]["resource"]["aws_iam_role_policy"]["tick"]
    assert json.loads(retained["policy"]) == policy and retained["lifecycle"] == {"ignore_changes": ["policy"]}
    assert state["applied"] is (drift != "before_apply")
    assert (tmp_path / "evidence" / "redis-verified.json").exists() is (drift is None)
    assert state["policy_reads"] == (2 if drift == "before_apply" else 3)
    assert not (module / "zz_tick_redis_maintenance_override.tf.json").exists()
    assert not (module.parents[1] / "zz_tick_redis_maintenance_override.tf.json").exists()
    output = capsys.readouterr().out
    assert "secret-resource" not in output and "never-print-fixture" not in output

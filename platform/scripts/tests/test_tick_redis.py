"""Redis diagnostics expose only bounded facts and never provider payloads."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("tick_redis", Path(__file__).resolve().parents[1] / "tick_redis.py")
redis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redis)


def test_settings_projection_never_contains_url_or_identity_values():
    expected = redis.expected_environment("store.cache.amazonaws.com", "6379", "iam-user", "cache-id")
    facts = redis.environment_facts({"BG_REDIS_URL": "rediss://password-must-not-escape@host/0"}, expected)
    assert facts["BG_REDIS_URL"] == {"present": True, "matches_gateway_store": False}
    assert all(not v["present"] for k, v in facts.items() if k != "BG_REDIS_URL")
    assert "password" not in json.dumps(facts) and "cache-id" not in json.dumps(facts)


@pytest.mark.parametrize("host,port,user,cache", [
    ("user:secret@host.cache.amazonaws.com", 6379, "user", "cache"),
    ("host.cache.amazonaws.com", 0, "user", "cache"),
    ("host.cache.amazonaws.com", 65536, "user", "cache"),
    ("host.cache.amazonaws.com", 6379, "*", "cache"),
    ("other.example.com", 6379, "user", "cache"),
])
def test_unexpected_store_identity_is_refused(host, port, user, cache):
    with pytest.raises(redis.diag.DiagnosticError):
        redis.expected_environment(host, port, user, cache)


def test_network_requires_exact_group_port_and_protocol():
    rule = {"IpProtocol": "tcp", "FromPort": 6379, "ToPort": 6379, "UserIdGroupPairs": [{"GroupId": "sg-redis"}]}
    assert redis.permits_group([rule], "sg-redis", 6379)
    assert not redis.permits_group([rule], "sg-other", 6379)
    assert not redis.permits_group([{**rule, "FromPort": 0}], "sg-redis", 6379)
    assert not redis.permits_group([{**rule, "IpProtocol": "-1"}], "sg-redis", 6379)


def test_provider_failure_does_not_escape_or_stop_independent_tick_read(monkeypatch):
    calls = []
    def failure(*args):
        calls.append(args[:2])
        raise RuntimeError("credential-must-not-escape")
    monkeypatch.setattr(redis, "aws", failure)
    _, facts = redis.collect()
    assert calls == [("ssm", "get-parameter"), ("lambda", "get-function-configuration")]
    assert facts["complete"] is False
    assert facts["checks"]["tick"] == {"status": "unavailable", "failure_type": "RuntimeError"}
    assert "credential" not in json.dumps(facts)


def test_diagnostic_reads_cannot_call_writes(monkeypatch):
    monkeypatch.setattr(redis.diag, "run", lambda args: pytest.fail("write must never reach subprocess"))
    with pytest.raises(redis.diag.DiagnosticError, match="unapproved_read"):
        redis.aws("lambda", "update-function-configuration")


def test_full_missing_wiring_diagnosis(monkeypatch):
    expected = redis.expected_environment("store.cache.amazonaws.com", "6379", "iam-user", "cache-id")
    monkeypatch.setattr(redis, "parameter", lambda name: {"redis-host": "store.cache.amazonaws.com", "redis-port": "6379", "redis-iam-username": "iam-user", "redis-cache-name": "cache-id"}[name])
    monkeypatch.setattr(redis, "gateway_environment", lambda: expected)
    def aws(service, operation, *args):
        if service == "lambda":
            return {"FunctionArn": f"arn:aws:lambda:{redis.REGION}:{redis.ACCOUNT}:function:{redis.TICK}", "Role": f"arn:aws:iam::{redis.ACCOUNT}:role/tick", "State": "Active", "LastUpdateStatus": "Successful", "VpcConfig": {"VpcId": "vpc-1", "SecurityGroupIds": ["sg-tick"]}, "Environment": {"Variables": {"HIDDEN": "credential-must-not-escape"}}}
        if operation == "describe-replication-groups":
            return {"ReplicationGroups": [{"Status": "available", "TransitEncryptionEnabled": True, "MemberClusters": ["cache-1"], "UserGroupIds": ["ug"], "NodeGroups": [{"PrimaryEndpoint": {"Address": "store.cache.amazonaws.com", "Port": 6379}}]}]}
        if operation == "describe-cache-clusters":
            return {"CacheClusters": [{"SecurityGroups": [{"Status": "active", "SecurityGroupId": "sg-redis"}]}]}
        if operation == "describe-users":
            return {"Users": [{"Status": "active", "Authentication": {"Type": "iam"}, "UserGroupIds": ["ug"]}]}
        if service == "ec2":
            return {"SecurityGroups": [{"GroupId": group, "VpcId": "vpc-1"} for group in ["sg-redis", "sg-tick"]]}
        if service == "iam":
            resources = args[args.index("--resource-arns") + 1:]
            return {"EvaluationResults": [{"EvalActionName": "elasticache:Connect", "EvalResourceName": arn, "EvalDecision": "implicitDeny"} for arn in resources]}
        pytest.fail("unexpected read")
    monkeypatch.setattr(redis, "aws", aws)
    private, facts = redis.collect()
    assert facts["complete"]
    assert all(not v["present"] for v in facts["checks"]["tick"]["redis_settings"].values())
    assert facts["checks"]["network"]["tick_to_redis_egress"] is False
    assert facts["checks"]["network"]["redis_from_tick_ingress"] is False
    assert facts["checks"]["iam"]["elasticache_connect_allowed"] is False
    assert "credential-must-not-escape" in json.dumps(private)
    assert "credential-must-not-escape" not in json.dumps(facts)
    assert expected["BG_REDIS_URL"] not in json.dumps(facts)

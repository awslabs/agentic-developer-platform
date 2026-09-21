"""Read-only facts for the tick's existing gateway Redis store.

Provider payloads stay in memory. Only booleans and resource identities may be
retained: never URLs, tokens, environment values, policies or raw errors.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

SPEC = importlib.util.spec_from_file_location("shared_diagnostics", Path(__file__).with_name("diagnose-shared-runtime.py"))
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)
ACCOUNT, REGION = diag.ACCOUNT, diag.REGION
TICK = "adp-dev-orchestration-tick"
KEYS = ("BG_REDIS_URL", "BG_REDIS_IAM_AUTH", "BG_REDIS_USERNAME", "BG_REDIS_CACHE_NAME")


def require(value, reason):
    if not value:
        raise diag.DiagnosticError(reason)


def aws(*args):
    require(args[:2] in {
        ("ssm", "get-parameter"), ("lambda", "get-function-configuration"),
        ("elasticache", "describe-replication-groups"), ("elasticache", "describe-cache-clusters"),
        ("elasticache", "describe-users"), ("ec2", "describe-security-groups"),
        ("iam", "simulate-principal-policy"),
    }, "unapproved_read")
    return diag.decode(diag.run(["aws", *args, "--region", REGION, "--output", "json"]))


def parameter(suffix):
    return aws("ssm", "get-parameter", "--name", "/adp/dev/gateway/" + suffix)["Parameter"]["Value"]


def gateway_environment():
    fields = [json.dumps(key) + ':{{printf "%q" (index .data ' + json.dumps(key) + ')}}' for key in KEYS]
    return diag.decode(diag.run(["kubectl", "--request-timeout=30s", "get", "configmap", "bedrockgateway-config",
                                "-n", "adp-gateway", "-o", "go-template={" + ",".join(fields) + "}"]))


def expected_environment(host, port, username, cache_name):
    require(isinstance(host, str) and host.endswith(".cache.amazonaws.com") and all(c.isalnum() or c in ".-" for c in host), "invalid_redis_endpoint")
    require(str(port).isdigit() and 1 <= int(port) <= 65535, "invalid_redis_port")
    require(all(isinstance(x, str) and x and all(c.isalnum() or c == "-" for c in x) for x in (username, cache_name)), "invalid_redis_identity")
    return dict(zip(KEYS, (f"rediss://{host}:{int(port)}/0", "true", username, cache_name), strict=True))


def environment_facts(actual, expected):
    # Do not include hashes of URLs either; a plain presence/equality result is enough.
    return {key: {"present": bool(actual.get(key)), "matches_gateway_store": actual.get(key) == expected[key]} for key in KEYS}


def permits_group(rules, peer, port):
    return any(rule.get("IpProtocol") == "tcp" and rule.get("FromPort") == port and rule.get("ToPort") == port
               and any(pair.get("GroupId") == peer for pair in rule.get("UserIdGroupPairs", [])) for rule in rules)


def collect():
    """Return private facts plus the only projection permitted in artifacts."""
    evidence = {"observed_at": datetime.now(UTC).isoformat(), "read_only": True, "stage": "redis-diagnose", "checks": {}}
    private = {}

    def read(name, function):
        try:
            return function()
        except Exception as error:
            # Even a malformed provider body can put raw values in exception args.
            evidence["checks"][name] = {"status": "unavailable", "failure_type": type(error).__name__}
            return None

    def store():
        expected = expected_environment(parameter("redis-host"), parameter("redis-port"), parameter("redis-iam-username"), parameter("redis-cache-name"))
        private["expected_env"] = expected
        private["port"] = urlsplit(expected["BG_REDIS_URL"]).port
        private["resource_arns"] = [f"arn:aws:elasticache:{REGION}:{ACCOUNT}:replicationgroup:{expected['BG_REDIS_CACHE_NAME']}",
                                    f"arn:aws:elasticache:{REGION}:{ACCOUNT}:user:{expected['BG_REDIS_USERNAME']}"]
        gateway = gateway_environment()
        evidence["checks"]["gateway_store"] = {"status": "observed", "settings": environment_facts(gateway, expected)}
        require(gateway == expected, "gateway_store_configuration_mismatch")
        return expected

    expected = read("gateway_store", store)

    def tick():
        config = aws("lambda", "get-function-configuration", "--function-name", TICK)
        require(config.get("FunctionArn") == f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{TICK}", "tick_identity_mismatch")
        require(config.get("Role", "").startswith(f"arn:aws:iam::{ACCOUNT}:role/"), "tick_role_mismatch")
        require(config.get("State") == "Active" and config.get("LastUpdateStatus") == "Successful", "tick_update_in_progress")
        private["configuration"] = config
        evidence["checks"]["tick"] = {"status": "observed", "function": TICK, "role_arn": config["Role"],
                                            "security_group_ids": config["VpcConfig"]["SecurityGroupIds"],
                                            "redis_settings": environment_facts(config.get("Environment", {}).get("Variables", {}), expected) if expected else None}
        return config

    config = read("tick", tick)
    if expected:
        def redis():
            groups = aws("elasticache", "describe-replication-groups", "--replication-group-id", expected["BG_REDIS_CACHE_NAME"])["ReplicationGroups"]
            require(len(groups) == 1 and groups[0]["Status"] == "available" and groups[0].get("TransitEncryptionEnabled") is True, "redis_store_unavailable")
            group = groups[0]
            nodes = group.get("NodeGroups", [])
            require(len(nodes) == 1 and group.get("MemberClusters"), "redis_topology_unexpected")
            endpoint = nodes[0]["PrimaryEndpoint"]
            require(expected["BG_REDIS_URL"] == f"rediss://{endpoint['Address']}:{endpoint['Port']}/0", "redis_endpoint_mismatch")
            clusters = aws("elasticache", "describe-cache-clusters", "--cache-cluster-id", group["MemberClusters"][0])["CacheClusters"]
            require(len(clusters) == 1, "redis_cluster_ambiguous")
            security_groups = clusters[0]["SecurityGroups"]
            require(len(security_groups) == 1 and security_groups[0]["Status"] == "active", "redis_security_group_ambiguous")
            private["redis_sg"] = security_groups[0]["SecurityGroupId"]
            users = aws("elasticache", "describe-users", "--user-id", expected["BG_REDIS_USERNAME"])["Users"]
            require(len(users) == 1 and users[0]["Status"] == "active" and users[0]["Authentication"]["Type"] == "iam", "redis_user_unavailable")
            require(set(users[0].get("UserGroupIds", [])) & set(group.get("UserGroupIds", [])), "redis_user_not_attached")
            evidence["checks"]["redis"] = {"status": "observed", "tls_enabled": True, "iam_user_attached": True,
                                               "security_group_id": private["redis_sg"], "resource_arns": private["resource_arns"]}
        read("redis", redis)
    if config and private.get("redis_sg"):
        def network():
            ids = config["VpcConfig"]["SecurityGroupIds"]
            require(len(ids) == 1, "tick_security_group_ambiguous")
            groups = aws("ec2", "describe-security-groups", "--group-ids", ids[0], private["redis_sg"])["SecurityGroups"]
            by_id = {group["GroupId"]: group for group in groups}
            tick_sg, redis_sg = by_id[ids[0]], by_id[private["redis_sg"]]
            require(tick_sg["VpcId"] == redis_sg["VpcId"] == config["VpcConfig"]["VpcId"], "redis_vpc_mismatch")
            private["security_groups"] = by_id
            evidence["checks"]["network"] = {"status": "observed", "same_vpc": True,
                "tick_to_redis_egress": permits_group(tick_sg.get("IpPermissionsEgress", []), redis_sg["GroupId"], private["port"]),
                "redis_from_tick_ingress": permits_group(redis_sg.get("IpPermissions", []), tick_sg["GroupId"], private["port"])}
        read("network", network)
    if config and expected:
        def iam():
            result = aws("iam", "simulate-principal-policy", "--policy-source-arn", config["Role"], "--action-names", "elasticache:Connect",
                         "--resource-arns", *private["resource_arns"])
            rows = result.get("EvaluationResults", [])
            decisions = {r["EvalResourceName"]: r.get("EvalDecision") for r in rows if r.get("EvalActionName") == "elasticache:Connect"}
            require(set(decisions) == set(private["resource_arns"]), "iam_simulation_incomplete")
            evidence["checks"]["iam"] = {"status": "observed", "simulation_only": True,
                "elasticache_connect_allowed": all(x == "allowed" for x in decisions.values())}
        read("iam", iam)
    evidence["complete"] = all(evidence["checks"].get(k, {}).get("status") == "observed" for k in ("gateway_store", "tick", "redis", "network", "iam"))
    return private, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["AWS_PAGER"] = ""
    with tempfile.TemporaryDirectory(prefix="adp-redis-diagnose-") as scratch:
        identity = diag.identity(args.account_id, Path(scratch))
        _, evidence = collect()
        evidence["identity"] = identity | {"stage": "redis-diagnose"}
    args.evidence_directory.mkdir(parents=True, exist_ok=True)
    (args.evidence_directory / "redis-diagnostics.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))
    return 0 if evidence["complete"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit("Redis diagnosis failed: " + type(error).__name__) from None

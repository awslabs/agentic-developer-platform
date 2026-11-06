"""Offline fixture preparation and authentic provider-readback requirements."""

import importlib.util
import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[4]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "modules/L5-services/scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare_investigation", "prepare-devops-investigation-fixture.py")
readback = load("readback_investigation", "devops_investigation_readback.py")



@pytest.fixture
def bundle(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:4] == ["git", "-C", "/selected/repository", "show"]
        assert command[4].startswith("1" * 40 + ":")
        path = command[4].split(":", 1)[1]
        assert path in [prepare.SOURCE, *[prepare.TERRAFORM + name for name in ("main.tf", "variables.tf", "versions.tf")]]
        return SimpleNamespace(stdout=(ROOT / path).read_bytes())
    monkeypatch.setattr(prepare.subprocess, "run", run)
    config = {"schema_version": 1, "account_id": "000000000000", "region": "us-east-1", "repository": "/selected/repository",
              "commit": "1" * 40, "fixture_id": "0000000000000001", "native_space_id": "space-one"}
    output = tmp_path / "private"
    manifest = prepare.prepare(config, output)
    return SimpleNamespace(config=config, output=output, manifest=manifest, calls=calls)


def test_prep_has_no_provider_effects_and_pins_each_artifact(bundle):
    manifest = bundle.manifest
    assert manifest["aws_calls"] == manifest["native_submissions"] == 0 and manifest["deployed"] is False
    assert len(bundle.calls) == 4
    for path, digest in manifest["files"].items():
        assert sha256((bundle.output / path).read_bytes()).hexdigest() == digest
        assert (bundle.output / path).stat().st_mode & 0o077 == 0
    for phase in ("bootstrap", "sealed", "active"):
        values = json.loads((bundle.output / (phase + ".tfvars.json")).read_text())
        assert values["phase"] == phase and values["native_space_id"] == "space-one"
    assert json.loads((bundle.output / "dependency_timeout.json").read_text())["correlation_id"] == bundle.config["fixture_id"]
    with pytest.raises(FileExistsError):
        prepare.prepare(bundle.config, bundle.output)


@pytest.mark.parametrize("field,value", [("commit", "main"), ("native_space_id", "*"), ("account_id", "*")])
def test_unbounded_inputs_cannot_prepare(tmp_path, bundle, field, value):
    config = {**bundle.config, field: value}
    with pytest.raises(ValueError, match="exact immutable"):
        prepare.prepare(config, tmp_path / "invalid")
    assert len(bundle.calls) == 4


def test_actual_handler_is_synthetic_and_rejects_secret_inputs():
    subprocess.run(["node", "--test", str(ROOT / "modules/L5-services/fixtures/devops-investigation/index.test.mjs")], check=True, capture_output=True)


@pytest.fixture
def observed(bundle):
    manifest = bundle.manifest
    deployment = {key: manifest[key] for key in ("account_id", "region", "function_arn", "log_group_name", "telemetry_arn",
        "resource_role_arn", "execution_role_arn", "native_space_id", "source_commit", "code_sha256_base64")}
    deployment.update(phase="active", function_version_arn=manifest["function_arn"] + ":1", subnet_id="subnet-one", security_group_id="sg-one", vpc_id="vpc-one")
    config = {"FunctionArn": deployment["function_version_arn"], "Version": "1", "CodeSha256": manifest["code_sha256_base64"],
              "Role": manifest["execution_role_arn"], "Runtime": "nodejs22.x", "Handler": "index.handler", "MemorySize": 128, "Timeout": 5,
              "VpcConfig": {"SubnetIds": ["subnet-one"], "SecurityGroupIds": ["sg-one"], "VpcId": "vpc-one"}, "State": "Active"}
    function, logs = Mock(), Mock()
    def get_function(**kwargs):
        value = deepcopy(config)
        if kwargs["FunctionName"] == manifest["function_arn"]:
            value.update(FunctionArn=manifest["function_arn"], Version="$LATEST")
        return {"Configuration": value}
    function.get_function.side_effect = get_function
    function.get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 1}
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    events = []
    for index, (scenario, outcome, code) in enumerate((("dependency_timeout", "failed", "SYNTHETIC_DEPENDENCY_TIMEOUT"),
                                                     ("recovered", "recovered", "SYNTHETIC_RECOVERY"))):
        body = {"schema_version": 1, "fixture": "l5-devops-investigation", "synthetic": True, "correlation_id": manifest["fixture_id"],
                "scenario": scenario, "outcome": outcome, "code": code}
        events.append({"eventId": f"event-{index}", "logStreamName": "2026/01/01/[1]stream", "timestamp": now_ms - 1000 + index,
                       "message": "timestamp request-id INFO " + json.dumps(body)})
    logs.filter_log_events.return_value = {"events": events}
    return SimpleNamespace(manifest=manifest, deployment=deployment, config=config, function=function, logs=logs,
                           now=now, now_ms=now_ms, events=events)


def verify(item):
    return readback.verify(item.function, item.logs, manifest=item.manifest, deployment=item.deployment,
                           since_ms=item.now_ms - 5000, now=item.now)


def test_readback_requires_actual_correlated_failure_and_recovery(observed):
    result = verify(observed)
    assert result["native_qualified"] is False and result["synthetic"] is True
    assert {item["scenario"] for item in result["observed_events"]} == {"dependency_timeout", "recovered"}
    assert all("message" not in item for item in result["observed_events"])
    observed.function.invoke.assert_not_called()
    observed.logs.put_log_events.assert_not_called()


@pytest.mark.parametrize("change", ["artifact", "role", "network", "environment", "environment_error", "ipv6", "concurrency", "missing_recovery", "stale", "foreign", "wrong_version", "cycle"])
def test_readback_rejects_unverified_or_wrong_scope_evidence(observed, change):
    if change in {"artifact", "role", "network", "environment"}:
        key = {"artifact": "CodeSha256", "role": "Role", "network": "VpcConfig", "environment": "Environment"}[change]
        observed.config[key] = {"Variables": {"secret": "must-not-exist"}} if change == "environment" else {} if change == "network" else "changed"
    elif change == "environment_error":
        observed.config["Environment"] = {"Error": {"ErrorCode": "AccessDenied"}}
    elif change == "ipv6":
        observed.config["VpcConfig"]["Ipv6AllowedForDualStack"] = True
    elif change == "concurrency":
        observed.function.get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 2}
    elif change == "missing_recovery":
        observed.logs.filter_log_events.return_value = {"events": observed.events[:1]}
    elif change == "stale":
        observed.events[0]["timestamp"] = observed.now_ms - 6000
    elif change == "foreign":
        observed.events[0]["message"] = observed.events[0]["message"].replace(observed.manifest["fixture_id"], "0000000000000002")
    elif change == "wrong_version":
        observed.events[0]["logStreamName"] = "2026/01/01/[$LATEST]stream"
    else:
        observed.logs.filter_log_events.return_value["nextToken"] = "same"
    with pytest.raises(readback.FixtureUnverified):
        verify(observed)
    observed.function.invoke.assert_not_called()


@pytest.fixture
def isolation(observed):
    iam, ec2, logs = Mock(), Mock(), Mock()
    role = observed.manifest["execution_role_arn"]
    iam.get_role.return_value = {"Role": {"Arn": role, "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}}}
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    iam.list_role_policies.return_value = {"PolicyNames": ["exact-fixture-log-writes"]}
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    iam.get_role_policy.return_value = {"PolicyDocument": {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": actions, "Resource": observed.manifest["telemetry_arn"]},
        {"Effect": "Deny", "NotAction": actions, "Resource": "*"},
        {"Effect": "Deny", "Action": "*", "NotResource": observed.manifest["telemetry_arn"]}]}}
    ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-one", "VpcId": "vpc-one", "IpPermissions": [], "IpPermissionsEgress": []}]}
    ec2.describe_subnets.return_value = {"Subnets": [{"SubnetId": "subnet-one", "VpcId": "vpc-one", "MapPublicIpOnLaunch": False, "AssignIpv6AddressOnCreation": False}]}
    ec2.describe_route_tables.return_value = {"RouteTables": [{"VpcId": "vpc-one", "Routes": [{"GatewayId": "local", "DestinationCidrBlock": "10.254.241.0/24", "State": "active"}]}]}
    ec2.describe_vpc_attribute.return_value = {"EnableDnsSupport": {"Value": False}, "EnableDnsHostnames": {"Value": False}}
    logs.describe_log_groups.return_value = {"logGroups": [{"logGroupName": observed.manifest["log_group_name"], "arn": observed.manifest["telemetry_arn"], "retentionInDays": 1}]}
    return SimpleNamespace(iam=iam, ec2=ec2, logs=logs, observed=observed)


def test_actual_role_and_network_readbacks_confirm_isolation(isolation):
    result = readback.verify_isolation(isolation.iam, isolation.ec2, isolation.logs,
                                       manifest=isolation.observed.manifest, deployment=isolation.observed.deployment)
    assert result["sealed"] is True and result["native_qualified"] is False
    isolation.iam.put_role_policy.assert_not_called()


@pytest.mark.parametrize("change", ["policy", "managed", "egress", "route", "dns", "retention", "deployment"])
def test_isolation_readback_rejects_broadened_scope(isolation, change):
    if change == "policy":
        isolation.iam.get_role_policy.return_value["PolicyDocument"]["Statement"][0]["Resource"] = "*"
    elif change == "managed":
        isolation.iam.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": "extra"}]}
    elif change == "egress":
        isolation.ec2.describe_security_groups.return_value["SecurityGroups"][0]["IpPermissionsEgress"] = [{"IpProtocol": "-1"}]
    elif change == "route":
        isolation.ec2.describe_route_tables.return_value["RouteTables"][0]["Routes"].append({"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-one"})
    elif change == "dns":
        isolation.ec2.describe_vpc_attribute.return_value["EnableDnsSupport"]["Value"] = True
    elif change == "retention":
        isolation.logs.describe_log_groups.return_value["logGroups"][0]["retentionInDays"] = 365
    else:
        isolation.observed.deployment["execution_role_arn"] = "changed"
    with pytest.raises(readback.FixtureUnverified):
        readback.verify_isolation(isolation.iam, isolation.ec2, isolation.logs,
                                  manifest=isolation.observed.manifest, deployment=isolation.observed.deployment)

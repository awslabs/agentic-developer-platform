"""Read-only evidence for an explicitly invoked, isolated synthetic fixture.

Clients must be supplied from the reviewed fixture-owner session. This module
has no ambient credential factory and never invokes Lambda or a native agent.
"""

import json
import re
from datetime import UTC, datetime


class FixtureUnverified(ValueError):
    pass


def verify(lambda_client, logs_client, *, manifest, deployment, since_ms, now=None):
    now_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    if not isinstance(since_ms, int) or not 0 <= now_ms - since_ms <= 300000:
        raise FixtureUnverified("Select a bounded invocation window from the last five minutes")
    version_arn = deployment.get("function_version_arn")
    if (manifest.get("purpose") != "synthetic-investigation" or manifest.get("synthetic") is not True
            or deployment.get("phase") != "active" or not isinstance(version_arn, str)
            or not re.fullmatch(re.escape(manifest["function_arn"]) + r":[1-9][0-9]*", version_arn)
            or any(deployment.get(key) != manifest.get(key) for key in (
                "account_id", "region", "function_arn", "log_group_name", "telemetry_arn", "resource_role_arn",
                "execution_role_arn", "native_space_id", "source_commit", "code_sha256_base64"))):
        raise FixtureUnverified("Deployment does not match the prepared exact fixture")
    observed = lambda_client.get_function(FunctionName=version_arn)
    config = observed.get("Configuration") or {}
    network = config.get("VpcConfig") or {}
    if (config.get("FunctionArn") != version_arn or config.get("Version") != version_arn.rsplit(":", 1)[1]
            or config.get("CodeSha256") != manifest["code_sha256_base64"]
            or config.get("Role") != manifest["execution_role_arn"]
            or config.get("Runtime") != "nodejs22.x" or config.get("Handler") != "index.handler"
            or config.get("MemorySize") != 128 or config.get("Timeout") != 5
            or config.get("Environment", {}).get("Variables") or config.get("Environment", {}).get("Error") or config.get("Layers")
            or network.get("Ipv6AllowedForDualStack") is True
            or network.get("SubnetIds") != [deployment.get("subnet_id")]
            or network.get("SecurityGroupIds") != [deployment.get("security_group_id")]
            or network.get("VpcId") != deployment.get("vpc_id")
            or config.get("State") != "Active"):
        raise FixtureUnverified("Published fixture configuration or artifact readback changed")
    current = lambda_client.get_function(FunctionName=manifest["function_arn"]).get("Configuration") or {}
    if (current.get("FunctionArn") != manifest["function_arn"] or current.get("Version") != "$LATEST"
            or any(current.get(key) != config.get(key) for key in (
                "CodeSha256", "Role", "Runtime", "Handler", "MemorySize", "Timeout", "Environment", "Layers", "VpcConfig"))):
        raise FixtureUnverified("Unqualified workload no longer matches the invoked published version")
    if lambda_client.get_function_concurrency(FunctionName=manifest["function_arn"]).get("ReservedConcurrentExecutions") != 1:
        raise FixtureUnverified("Fixture invocation concurrency is not bounded")
    records, visited, token = {}, set(), None
    for _page in range(10):
        response = logs_client.filter_log_events(logGroupName=manifest["log_group_name"],
            filterPattern='"' + manifest["fixture_id"] + '"', startTime=since_ms, endTime=now_ms, limit=100,
            **({"nextToken": token} if token else {}))
        if not isinstance(response.get("events"), list):
            raise FixtureUnverified("Selected telemetry readback is incomplete")
        for event in response["events"]:
            message = event.get("message")
            if not isinstance(message, str) or len(message) > 8192:
                continue
            try:
                body = json.loads(message[message.index('{'):].strip())
            except (ValueError, TypeError):
                continue
            if not isinstance(body, dict):
                continue
            expected = {"dependency_timeout": ("failed", "SYNTHETIC_DEPENDENCY_TIMEOUT"),
                        "recovered": ("recovered", "SYNTHETIC_RECOVERY")}
            scenario = body.get("scenario")
            if (body.get("schema_version") != 1 or body.get("fixture") != "l5-devops-investigation"
                    or body.get("synthetic") is not True or body.get("correlation_id") != manifest["fixture_id"]
                    or scenario not in expected or (body.get("outcome"), body.get("code")) != expected[scenario]
                    or not isinstance(event.get("eventId"), str) or not event["eventId"]
                    or not isinstance(event.get("timestamp"), int) or not since_ms <= event["timestamp"] <= now_ms
                    or not isinstance(event.get("logStreamName"), str) or f"[{config["Version"]}]" not in event["logStreamName"]):
                continue
            records[event["eventId"]] = {"event_id": event["eventId"], "log_stream": event["logStreamName"],
                                         "timestamp_ms": event["timestamp"], "scenario": scenario,
                                         "outcome": body["outcome"], "code": body["code"]}
        token = response.get("nextToken")
        if not token:
            break
        if token in visited:
            raise FixtureUnverified("Selected telemetry listing did not advance")
        visited.add(token)
    else:
        raise FixtureUnverified("Selected telemetry listing exceeded bounds")
    if {record["scenario"] for record in records.values()} != {"dependency_timeout", "recovered"}:
        raise FixtureUnverified("Actual correlated failure and recovery telemetry is not yet available")
    return {"schema_version": 1, "purpose": "synthetic-investigation", "synthetic": True,
            "function_version_arn": version_arn, "code_sha256_base64": manifest["code_sha256_base64"],
            "telemetry_arn": manifest["telemetry_arn"], "correlation_id": manifest["fixture_id"],
            "since_ms": since_ms, "checked_at_ms": now_ms, "observed_events": sorted(records.values(), key=lambda item: item["event_id"]),
            "native_qualified": False}


def verify_isolation(iam, ec2, logs, *, manifest, deployment):
    """Verify the sealed writer role, private network and precreated log group."""
    if deployment.get("phase") not in {"sealed", "active"}:
        raise FixtureUnverified("Seal bootstrap authority before verifying isolation")
    if (manifest.get("purpose") != "synthetic-investigation" or manifest.get("synthetic") is not True
            or any(deployment.get(key) != manifest.get(key) for key in (
                "account_id", "region", "function_arn", "log_group_name", "telemetry_arn", "resource_role_arn",
                "execution_role_arn", "native_space_id", "source_commit", "code_sha256_base64"))):
        raise FixtureUnverified("Deployment does not match the prepared exact fixture")
    role_arn = manifest["execution_role_arn"]
    role_name = role_arn.rsplit("/", 1)[1]
    role = iam.get_role(RoleName=role_name).get("Role") or {}
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    if role.get("Arn") != role_arn or role.get("AssumeRolePolicyDocument") != trust or role.get("PermissionsBoundary"):
        raise FixtureUnverified("Fixture writer-role identity or trust changed")
    attached = iam.list_attached_role_policies(RoleName=role_name)
    inline = iam.list_role_policies(RoleName=role_name)
    if (attached.get("AttachedPolicies") != [] or attached.get("IsTruncated")
            or inline.get("PolicyNames") != ["exact-fixture-log-writes"] or inline.get("IsTruncated")):
        raise FixtureUnverified("Fixture writer role has additional or incomplete policies")
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    expected = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": actions, "Resource": manifest["telemetry_arn"]},
        {"Effect": "Deny", "NotAction": actions, "Resource": "*"},
        {"Effect": "Deny", "Action": "*", "NotResource": manifest["telemetry_arn"]}]}
    policy = iam.get_role_policy(RoleName=role_name, PolicyName="exact-fixture-log-writes").get("PolicyDocument")
    if policy != expected:
        raise FixtureUnverified("Fixture writer role is not sealed to exact log writes")
    vpc, subnet, group = (deployment.get(key) for key in ("vpc_id", "subnet_id", "security_group_id"))
    if not all(isinstance(value, str) and value for value in (vpc, subnet, group)):
        raise FixtureUnverified("Exact deployed network references are required")
    groups = ec2.describe_security_groups(GroupIds=[group]).get("SecurityGroups") or []
    subnets = ec2.describe_subnets(SubnetIds=[subnet]).get("Subnets") or []
    tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])
    if (len(groups) != 1 or groups[0].get("GroupId") != group or groups[0].get("VpcId") != vpc
            or groups[0].get("IpPermissions") != [] or groups[0].get("IpPermissionsEgress") != []
            or len(subnets) != 1 or subnets[0].get("SubnetId") != subnet or subnets[0].get("VpcId") != vpc
            or subnets[0].get("MapPublicIpOnLaunch") is not False or subnets[0].get("AssignIpv6AddressOnCreation") is not False
            or tables.get("NextToken") or not tables.get("RouteTables") or len(tables["RouteTables"]) > 20
            or any(table.get("VpcId") != vpc or not table.get("Routes") or any(
                route.get("GatewayId") != "local" or route.get("DestinationCidrBlock") != "10.254.241.0/24"
                or route.get("State") != "active" for route in table["Routes"]) for table in tables["RouteTables"])):
        raise FixtureUnverified("Fixture network has unverified routes, ingress or egress")
    for attribute, key in (("enableDnsSupport", "EnableDnsSupport"), ("enableDnsHostnames", "EnableDnsHostnames")):
        if ec2.describe_vpc_attribute(VpcId=vpc, Attribute=attribute).get(key, {}).get("Value") is not False:
            raise FixtureUnverified("Fixture DNS isolation changed")
    response = logs.describe_log_groups(logGroupNamePrefix=manifest["log_group_name"], limit=50)
    groups = [value for value in response.get("logGroups", []) if value.get("logGroupName") == manifest["log_group_name"]]
    if (response.get("nextToken") or len(groups) != 1 or groups[0].get("arn") != manifest["telemetry_arn"]
            or groups[0].get("retentionInDays") != 1):
        raise FixtureUnverified("Exact precreated log group or retention is unverified")
    return {"schema_version": 1, "sealed": True, "synthetic": True, "execution_role_arn": role_arn,
            "telemetry_arn": manifest["telemetry_arn"], "vpc_id": vpc, "subnet_id": subnet, "security_group_id": group,
            "writer_policy": policy, "native_qualified": False}

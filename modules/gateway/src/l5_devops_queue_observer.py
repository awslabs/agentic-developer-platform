"""Concrete, read-only qualification of one immutable synthetic queue worker."""

import base64
import io
import re
import zipfile
from hashlib import sha256

QUEUE_ACTIONS = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]


def worker_zip(source):
    if not isinstance(source, bytes) or not 1 <= len(source) <= 32768:
        raise ValueError("queue worker source must be one bounded index.mjs")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        member = zipfile.ZipInfo("index.mjs", date_time=(1980, 1, 1, 0, 0, 0))
        member.create_system = 3
        member.external_attr = 0o100644 << 16
        archive.writestr(member, source)
    return output.getvalue()


def worker_policy(queue_arn):
    # Explicit denies bound effective authority even if a resource policy grants
    # permissions to this role/session. This is stricter than enumerating Allows.
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": QUEUE_ACTIONS, "Resource": queue_arn},
            {"Effect": "Deny", "NotAction": QUEUE_ACTIONS, "Resource": "*"},
            {"Effect": "Deny", "Action": "*", "NotResource": queue_arn},
        ],
    }


def check_selection(selection):
    required = {
        "account_id",
        "region",
        "function_version_arn",
        "queue_url",
        "queue_arn",
        "mapping_id",
        "execution_role_arn",
        "vpc_id",
        "subnet_id",
        "security_group_id",
        "bucket",
        "prefix",
        "signing_key_arn",
        "producer",
        "source_ref",
        "deployment_ref",
        "target_ref",
        "credential_ref",
    }
    if not isinstance(selection, dict) or set(selection) != required:
        raise ValueError("exact server-installed queue fixture selection required")
    account, region = selection["account_id"], selection["region"]
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9]", region):
        raise ValueError("queue fixture provider identity invalid")
    patterns = {
        "function_version_arn": rf"arn:aws:lambda:{region}:{account}:function:[A-Za-z0-9_-]{{1,64}}:[1-9][0-9]*",
        "queue_arn": rf"arn:aws:sqs:{region}:{account}:[A-Za-z0-9_-]{{1,80}}",
        "execution_role_arn": rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]+",
        "mapping_id": r"[a-f0-9-]{36}",
        "vpc_id": r"vpc-[a-f0-9]+",
        "subnet_id": r"subnet-[a-f0-9]+",
        "security_group_id": r"sg-[a-f0-9]+",
        "bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        "prefix": r"l5/devops/queue-fixture/[a-f0-9]{64}/",
        "producer": r"[a-zA-Z0-9_.-]{1,100}",
        "signing_key_arn": rf"arn:aws:kms:{region}:{account}:key/[a-f0-9-]{{36}}",
    }
    if any(not isinstance(selection[k], str) or not re.fullmatch(pattern, selection[k]) for k, pattern in patterns.items()):
        raise ValueError("queue fixture must select exact immutable resources")
    if selection["queue_url"] != f"https://sqs.{region}.amazonaws.com/{account}/{selection['queue_arn'].split(':')[-1]}":
        raise ValueError("queue URL differs from the selected provider")
    for name, kind in (
        ("source_ref", "repository_commit"),
        ("deployment_ref", "deployment"),
        ("target_ref", "verification_target"),
        ("credential_ref", "secret"),
    ):
        ref = selection[name]
        if (
            not isinstance(ref, dict)
            or ref.get("record_type") != kind
            or any(
                not isinstance(ref.get(field), str) or not ref[field]
                for field in ("tenant_id", "project_id", "source_connection_id", "record_id", "record_revision")
            )
        ):
            raise ValueError("queue fixture references are incomplete")
    if (
        selection["deployment_ref"]["record_id"] != selection["function_version_arn"]
        or selection["target_ref"]["record_id"] != selection["security_group_id"]
        or selection["credential_ref"]["record_id"] != selection["execution_role_arn"]
    ):
        raise ValueError("queue fixture resource references differ from the installed target")
    if not re.fullmatch(r"[a-f0-9]{40}", selection["source_ref"]["record_revision"]):
        raise ValueError("an immutable Git source commit is required")


def observe_queue(session, selection, source):
    """Actual AWS reads only. A missing observation is never an isolation proof."""
    from botocore.config import Config

    check_selection(selection)
    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
    clients = {name: session.client(name, region_name=selection["region"], config=config) for name in ("lambda", "sqs", "iam", "ec2", "sts")}
    identity = clients["sts"].get_caller_identity()
    if identity.get("Account") != selection["account_id"]:
        raise ValueError("fixture observation account differs from the installation")
    arn = selection["function_version_arn"]
    function = clients["lambda"].get_function(FunctionName=arn)
    current = function.get("Configuration") or {}
    digest = base64.b64encode(sha256(worker_zip(source)).digest()).decode()
    expected = {
        "FunctionArn": arn,
        "Version": arn.rsplit(":", 1)[1],
        "Runtime": "nodejs22.x",
        "Handler": "index.handler",
        "Role": selection["execution_role_arn"],
        "CodeSha256": digest,
        "PackageType": "Zip",
        "State": "Active",
        "Timeout": 30,
        "MemorySize": 128,
    }
    if (
        any(current.get(k) != v for k, v in expected.items())
        or current.get("Layers")
        or (current.get("Environment") or {}).get("Variables")
        or current.get("DeadLetterConfig", {}).get("TargetArn")
        or current.get("VpcConfig", {}).get("SubnetIds") != [selection["subnet_id"]]
        or current.get("VpcConfig", {}).get("SecurityGroupIds") != [selection["security_group_id"]]
        or current.get("VpcConfig", {}).get("VpcId") != selection["vpc_id"]
        or current.get("VpcConfig", {}).get("Ipv6AllowedForDualStack", False)
    ):
        raise ValueError("immutable deployed code/configuration does not match the selected source and isolated fixture")
    concurrency = clients["lambda"].get_function_concurrency(FunctionName=arn.rsplit(":", 1)[0])
    mapping = clients["lambda"].get_event_source_mapping(UUID=selection["mapping_id"])
    if concurrency.get("ReservedConcurrentExecutions") != 2 or any(
        mapping.get(k) != v
        for k, v in {
            "UUID": selection["mapping_id"],
            "FunctionArn": arn,
            "EventSourceArn": selection["queue_arn"],
            "State": "Enabled",
            "BatchSize": 5,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        }.items()
    ):
        raise ValueError("queue mapping or concurrency differs from the selected immutable version")
    queue = clients["sqs"].get_queue_attributes(QueueUrl=selection["queue_url"], AttributeNames=["All"]).get("Attributes", {})
    if (
        queue.get("QueueArn") != selection["queue_arn"]
        or queue.get("SqsManagedSseEnabled") != "true"
        or queue.get("VisibilityTimeout") != "180"
        or queue.get("Policy") not in (None, "", "{}")
    ):
        raise ValueError("queue identity, encryption or resource authority unverified")
    group = clients["ec2"].describe_security_groups(GroupIds=[selection["security_group_id"]]).get("SecurityGroups")
    if (
        not isinstance(group, list)
        or len(group) != 1
        or group[0].get("GroupId") != selection["security_group_id"]
        or group[0].get("VpcId") != selection["vpc_id"]
        or group[0].get("OwnerId") != selection["account_id"]
        or group[0].get("IpPermissionsEgress") != []
        or group[0].get("IpPermissions") != []
    ):
        raise ValueError("fixture security group does not deny network ingress and egress")
    role_name = selection["execution_role_arn"].rsplit("/", 1)[1]
    iam = clients["iam"]
    role = iam.get_role(RoleName=role_name).get("Role") or {}
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    policies = iam.list_role_policies(RoleName=role_name)
    managed = iam.list_attached_role_policies(RoleName=role_name)
    if (
        role.get("Arn") != selection["execution_role_arn"]
        or role.get("AssumeRolePolicyDocument") != trust
        or role.get("PermissionsBoundary")
        or policies.get("PolicyNames") != ["scoped-queue-read"]
        or policies.get("IsTruncated")
        or managed.get("AttachedPolicies") != []
        or managed.get("IsTruncated")
    ):
        raise ValueError("fixture execution role is not the finite queue-only role")
    policy = iam.get_role_policy(RoleName=role_name, PolicyName="scoped-queue-read")
    if policy.get("PolicyDocument") != worker_policy(selection["queue_arn"]):
        raise ValueError("fixture execution role lacks exact effective permission bounds")
    return {
        "function": expected,
        "mapping": {k: mapping[k] for k in ("UUID", "FunctionArn", "EventSourceArn", "State", "BatchSize", "FunctionResponseTypes")},
        "code_sha256": digest,
        "source_sha256": sha256(source).hexdigest(),
        "security_group": group[0],
        "role": {"Arn": role["Arn"], "RoleId": role["RoleId"], "trust": trust, "policy": policy["PolicyDocument"]},
        "queue": queue,
        "concurrency": 2,
    }

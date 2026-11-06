#!/usr/bin/env python3
"""Explicit, protected L5 worker code rollout; durable SQL and queues stay Terraform-owned."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Rollout runs before package installation. Resolve only this reviewed checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from l5_services.runtime_names import functions


def aws(*arguments):
    result = subprocess.run(
        ["aws", *arguments, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def identity(account, role_arn):
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(
        rf"arn:aws:iam::{account}:role/[a-zA-Z0-9+=,.@_/-]+", role_arn
    ):
        raise ValueError("Expected a selected account and its exact deployment role")
    principal = aws("sts", "get-caller-identity")
    expected = f"arn:aws:sts::{account}:assumed-role/{role_arn.rsplit('/', 1)[-1]}/"
    if principal.get("Account") != account or not principal.get("Arn", "").startswith(
        expected
    ):
        raise ValueError(
            "Selected account or assumed role does not match the protected target"
        )


def manifest_for(bucket, source, wheel_digest, version):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket):
        raise ValueError("Missing private release bucket")
    if not re.fullmatch(r"[0-9a-f]{40}", source) or not re.fullmatch(
        r"[0-9a-f]{64}", wheel_digest
    ):
        raise ValueError("Expected pinned source and installed gateway wheel digests")
    if not version or version in {"None", "null"}:
        raise ValueError("An immutable manifest version is required")
    prefix = f"l5/security/{source}/{wheel_digest}"
    with tempfile.TemporaryDirectory() as temporary:
        manifest_path = Path(temporary) / "manifest.json"
        readback = aws(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            f"{prefix}/manifest.json",
            "--version-id",
            version,
            str(manifest_path),
        )
        if readback.get("VersionId") != version:
            raise ValueError("Manifest storage version changed")
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest.get("source_sha") != source
            or manifest.get("wheel_sha256") != wheel_digest
        ):
            raise ValueError(
                "Manifest does not match the reviewed source and gateway wheel"
            )
        archive_digest = manifest.get("archive_sha256", "")
        archive_version = manifest.get("archive_version")
        if not re.fullmatch(r"[0-9a-f]{64}", archive_digest) or archive_version in {
            None,
            "",
            "None",
            "null",
        }:
            raise ValueError("Manifest lacks a pinned worker archive")
        archive = Path(temporary) / "workers.zip"
        archive_readback = aws(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            f"{prefix}/workers.zip",
            "--version-id",
            archive_version,
            str(archive),
        )
        if archive_readback.get("VersionId") != archive_version:
            raise ValueError("Archive storage version changed")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_digest:
            raise ValueError(
                "Worker archive readback differs from the selected manifest"
            )
    return (
        f"{prefix}/workers.zip",
        archive_version,
        base64.b64encode(bytes.fromhex(archive_digest)).decode(),
    )


def mapping(worker, environment, account, service="security"):
    mappings = aws(
        "lambda", "list-event-source-mappings", "--function-name", worker
    ).get("EventSourceMappings", [])
    expected_worker, _ = functions(environment, service)
    expected = rf"arn:aws:sqs:[a-z0-9-]+:{account}:{expected_worker.removesuffix('-worker')}-work"
    if len(mappings) != 1 or not re.fullmatch(
        expected, mappings[0].get("EventSourceArn", "")
    ):
        raise ValueError("Expected exactly one environment-owned work queue mapping")
    return mappings[0]["UUID"]


def state(environment, worker, repair, uuid):
    return {
        "worker": aws(
            "lambda", "get-function-configuration", "--function-name", worker
        ),
        "repair": aws(
            "lambda", "get-function-configuration", "--function-name", repair
        ),
        "mapping": aws("lambda", "get-event-source-mapping", "--uuid", uuid)["State"],
        "rule": aws("events", "describe-rule", "--name", repair)["State"],
        "recursive_loop": aws("lambda", "get-function-recursion-config", "--function-name", worker)["RecursiveLoop"],
    }


FACTORIES = {
    "security": "l5_services.runtime:factory",
    "devops": "l5_services.runtime_devops:factory",
    "transform": "l5_services.runtime_transform:factory",
    "well_architected": "l5_services.runtime_well_architected:factory",
    "finops": "l5_services.runtime_finops:factory",
}

CONTRIBUTION_SUFFIXES = {
    "security": "l5-contributions",
    "devops": "l5-devops-contributions",
    "transform": "l5-transform-contributions",
    "well_architected": "l5-wa-contributions",
    "finops": "l5-cost-contributions",
}

SERVICE_RUNTIME_FIELDS = {
    "transform": (
        "L5_TRANSFORM_ARTIFACT_BUCKET", "L5_TRANSFORM_CLUSTER_ENDPOINT",
        "L5_TRANSFORM_CLUSTER_NAME", "L5_TRANSFORM_CLUSTER_CA",
        "L5_TRANSFORM_NODE_NAME", "L5_TRANSFORM_NODE_UID",
        "L5_TRANSFORM_PROVIDER_NAMESPACE", "L5_TRANSFORM_PROVIDER_IMAGE",
        "L5_TRANSFORM_CHECK_NAMESPACE", "L5_TRANSFORM_CHECK_IMAGE",
    ),
    "finops": ("L5_FINOPS_EXECUTOR_ARN",),
}


class WorkerConfigurationError(ValueError):
    """Public diagnosis contains known field names, never runtime values."""

    def __init__(self, fields):
        super().__init__("Installed worker configuration is missing or invalid")
        action = "Correct the selected service's private infrastructure inputs and apply its reviewed root. "
        if any(field.startswith("L5_TRANSFORM_") for field in fields):
            action += "Complete the Transform artifact/executor bootstrap and use its verified cluster, isolated node, " \
                      "namespaces and immutable image references for worker and repair configuration. "
        if "L5_FINOPS_EXECUTOR_ARN" in fields:
            action += "Install the selected FinOps executor and use its exact function reference. "
        if "L5_RDS_CONNECT_ARN" in fields:
            action += "L5_RDS_CONNECT_ARN must be injected from the same rds_connect_arn used by worker IAM. "
        self.diagnosis = {
            "category": "worker_configuration_invalid",
            "fields": sorted(fields),
            "next_action": action + "Verify worker and repair configuration before retrying rollout; do not enable consumers.",
        }


def additional_worker_policies(service, environment, account, region, configured):
    """Only reviewed service roots may extend the shared worker's IAM policy."""
    if service == "finops":
        name = f"adp-{environment}-l5-finops-executor"
        arn = f"arn:aws:lambda:{region}:{account}:function:{name}"
        if configured.get("L5_FINOPS_EXECUTOR_ARN") != arn:
            raise WorkerConfigurationError(["L5_FINOPS_EXECUTOR_ARN"])
        return {name + "-private": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": arn},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{name}:*"},
        ]}}
    if service == "transform":
        bucket = f"adp-{environment}-l5-transform-{account}"
        if configured.get("L5_TRANSFORM_ARTIFACT_BUCKET") != bucket:
            raise WorkerConfigurationError(["L5_TRANSFORM_ARTIFACT_BUCKET"])
        arn = f"arn:aws:s3:::{bucket}"
        return {"transform-artifacts": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObjectVersion"],
             "Resource": arn + "/l5/transform/artifacts/*"},
            {"Effect": "Allow", "Action": ["s3:GetObject"],
             "Resource": arn + "/l5/transform/artifacts/*/checkpoints/*"},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": arn,
             "Condition": {"StringLike": {"s3:prefix": "l5/transform/artifacts/*/checkpoints/*"}}},
        ]}}
    return {}


def scope_readback(environment, account, worker, repair, observed, service="security"):
    worker_arn = observed["worker"].get("FunctionArn", "")
    match = re.fullmatch(rf"arn:aws:lambda:([a-z0-9-]+):{account}:function:{worker}", worker_arn)
    if match is None:
        raise ValueError("Worker identity or region changed")
    region = match.group(1)
    expected_worker, expected_repair = functions(environment, service)
    if (worker, repair) != (expected_worker, expected_repair):
        raise ValueError("Service worker identities differ")
    prefix = worker.removesuffix("-worker")
    queue_arn = f"arn:aws:sqs:{region}:{account}:{prefix}-work"
    configured = observed["worker"].get("Environment", {}).get("Variables", {})
    gateway_url = configured.get("L5_GATEWAY_URL", "")
    gateway = re.fullmatch(
        rf"https://([a-z0-9]+)\.execute-api\.{region}\.amazonaws\.com/[a-zA-Z0-9_-]+",
        gateway_url,
    )
    database = configured.get("L5_RDS_CONNECT_ARN", "")
    bucket = configured.get("L5_ARCHIVE_BUCKET", "")
    checks = {
        "L5_GATEWAY_URL": gateway is not None,
        "L5_RDS_CONNECT_ARN": bool(re.fullmatch(
            rf"arn:aws:rds-db:{region}:{account}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+", database)),
        "L5_ARCHIVE_BUCKET": bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket)),
        "L5_WORKER_FACTORY": configured.get("L5_WORKER_FACTORY") == FACTORIES.get(service),
        "L5_SERVICE_ID": configured.get("L5_SERVICE_ID") in ({None, "security"} if service == "security" else {service}),
        **{name: bool(configured.get(name)) for name in (
            "L5_RDS_HOST", "L5_RDS_PORT", "L5_RDS_DATABASE", "L5_RDS_USER")},
        **{name: isinstance(configured.get(name), str) and bool(configured[name].strip())
           for name in SERVICE_RUNTIME_FIELDS.get(service, ())},
    }
    if not all(checks.values()):
        raise WorkerConfigurationError([name for name, valid in checks.items() if not valid])
    gateway_arn = f"arn:aws:execute-api:{region}:{account}:{gateway.group(1)}/*/*/internal/*"
    gateway_api = f"arn:aws:execute-api:{region}:{account}:{gateway.group(1)}"
    repair_contributions = [
        f"{gateway_api}/*/{method}/internal/v1/{consumer}/{CONTRIBUTION_SUFFIXES[service]}{suffix}"
        for consumer in (["context"] if service == "security" else ["context", "insights"])
        for method, suffix in (("POST", ""), ("POST", "/*/invalidate"), ("GET", "/*"))
    ]
    if service in {"well_architected", "transform", "security"}:
        repair_contributions.append(f"{gateway_api}/*/POST/internal/v1/l5/{service.replace('_', '-')}/schedule-admissions")
    archive_arn = f"arn:aws:s3:::{bucket}/l5/{service}/work/*"
    network_actions = [
        "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
        "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    ]
    additions = additional_worker_policies(service, environment, account, region, configured)
    roles = {}
    for label, function in (("worker", worker), ("repair", repair)):
        role_arn = f"arn:aws:iam::{account}:role/{function}"
        function_arn = f"arn:aws:lambda:{region}:{account}:function:{function}"
        if observed[label].get("FunctionArn") != function_arn or observed[label].get("Role") != role_arn:
            raise ValueError("Lambda execution role or function changed")
        role = aws("iam", "get-role", "--role-name", function)["Role"]
        boundary = role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn", "")
        if role.get("Arn") != role_arn or not re.fullmatch(rf"arn:aws:iam::{account}:policy/[A-Za-z0-9_/-]+", boundary):
            raise ValueError("Execution role or permissions boundary changed")
        if role.get("AssumeRolePolicyDocument") != {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                           "Principal": {"Service": "lambda.amazonaws.com"}}],
        }:
            raise ValueError("Execution role trust grants another principal")
        inline = aws("iam", "list-role-policies", "--role-name", function)
        attached = aws("iam", "list-attached-role-policies", "--role-name", function)
        allowed_additions = additions if label == "worker" else {}
        if (sorted(inline.get("PolicyNames", [])) != sorted([function, *allowed_additions])
                or attached.get("AttachedPolicies") != []):
            raise ValueError("Execution role has additional policies")
        if observed[label].get("Environment", {}).get("Variables", {}) != configured:
            raise ValueError("Worker and repair dependency configuration differ")
        queue_actions = (
            ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"]
            if label == "worker" else ["sqs:SendMessage"]
        )
        expected = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function}:*"},
                {"Effect": "Allow", "Action": queue_actions, "Resource": queue_arn},
                {"Effect": "Allow", "Action": ["rds-db:connect"], "Resource": database},
                *([{"Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": gateway_arn},
                   {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": archive_arn}] if label == "worker" else []),
                {"Effect": "Allow", "Action": network_actions, "Resource": "*"},
                {"Effect": "Deny", "Action": network_actions, "Resource": "*",
                 "Condition": {"ArnEquals": {"lambda:SourceFunctionArn": function_arn}}},
                *([{"Effect": "Allow", "Action": ["execute-api:Invoke"],
                    "Resource": repair_contributions}] if label == "repair" else []),
            ],
        }
        policy = aws("iam", "get-role-policy", "--role-name", function, "--policy-name", function)
        if policy.get("PolicyDocument") != expected:
            raise ValueError("Execution policy exceeds the owned queue and function scope")
        for name, document in allowed_additions.items():
            extra = aws("iam", "get-role-policy", "--role-name", function, "--policy-name", name)
            if extra.get("PolicyDocument") != document:
                raise ValueError("Service executor policy exceeds its installed resource scope")
        roles[label] = {"role_arn": role_arn, "boundary_arn": boundary, "policy_name": function,
                        "trust_policy": role["AssumeRolePolicyDocument"]}
    queues = {}
    for label, name, retention in (
        ("work", f"{prefix}-work", 345600),
        ("dead_letter", f"{prefix}-work-dlq", 1209600),
    ):
        address = aws("sqs", "get-queue-url", "--queue-name", name, "--queue-owner-aws-account-id", account)["QueueUrl"]
        attributes = aws("sqs", "get-queue-attributes", "--queue-url", address, "--attribute-names",
                         "QueueArn", "MessageRetentionPeriod", "ApproximateNumberOfMessages",
                         "ApproximateNumberOfMessagesDelayed", "ApproximateNumberOfMessagesNotVisible", "RedrivePolicy")["Attributes"]
        expected_arn = queue_arn + ("-dlq" if label == "dead_letter" else "")
        counts = {key: attributes.get(key, "") for key in (
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesDelayed", "ApproximateNumberOfMessagesNotVisible")}
        if attributes.get("QueueArn") != expected_arn or attributes.get("MessageRetentionPeriod") != str(retention) or any(
            not isinstance(value, str) or not value.isdecimal() for value in counts.values()
        ):
            raise ValueError("Retained queue identity, counts or retention changed")
        if label == "work":
            if configured.get("L5_WORK_QUEUE_URL") != address:
                raise ValueError("Worker queue URL differs from the owned queue")
            try:
                redrive = json.loads(attributes.get("RedrivePolicy", ""))
            except json.JSONDecodeError as error:
                raise ValueError("Dead-letter redrive policy unavailable") from error
            if redrive != {"deadLetterTargetArn": f"{queue_arn}-dlq", "maxReceiveCount": 5}:
                raise ValueError("Dead-letter redrive policy changed")
        queues[label] = {"arn": expected_arn, "retention_seconds": retention, "approximate_messages": counts}
    return {"roles": roles, "queues": queues}

def auxiliary_names(environment, service):
    return tuple(f"adp-{environment}-l5-devops-{kind}" for kind in ("native", "readback")) if service == "devops" else ()


def auxiliary_state(names, primary, account):
    """Inspect only the selected service's declared event functions and triggers."""
    result = {}
    worker = primary["worker"]
    configured = worker["Environment"]["Variables"]
    region = worker["FunctionArn"].split(":")[3]
    for name in names:
        function = aws("lambda", "get-function-configuration", "--function-name", name)
        arn = f"arn:aws:lambda:{region}:{account}:function:{name}"
        role_arn = f"arn:aws:iam::{account}:role/{name}"
        if (function.get("FunctionArn") != arn or function.get("Role") != role_arn
                or function.get("Environment", {}).get("Variables", {}) != configured):
            raise ValueError("DevOps event function differs from the selected runtime")
        role = aws("iam", "get-role", "--role-name", name)["Role"]
        if (role.get("Arn") != role_arn
                or role.get("AssumeRolePolicyDocument") != {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "lambda.amazonaws.com"}}]}
                or not re.fullmatch(rf"arn:aws:iam::{account}:policy/[A-Za-z0-9+=,.@_/-]+",
                    role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn", ""))
                or aws("iam", "list-role-policies", "--role-name", name).get("PolicyNames") != [name]
                or aws("iam", "list-attached-role-policies", "--role-name", name).get("AttachedPolicies") != []):
            raise ValueError("DevOps event role scope differs from its reviewed module")
        network = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface",
                   "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
        gateway_id = configured["L5_GATEWAY_URL"].split("//", 1)[1].split(".", 1)[0]
        prefix = name.rsplit("-", 1)[0]
        expected = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{name}:*"},
            {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": f"arn:aws:sqs:{region}:{account}:{prefix}-work"},
            {"Effect": "Allow", "Action": ["rds-db:connect"], "Resource": configured["L5_RDS_CONNECT_ARN"]},
            {"Effect": "Allow", "Action": ["execute-api:Invoke"],
             "Resource": f"arn:aws:execute-api:{region}:{account}:{gateway_id}/*/*/internal/*"},
            {"Effect": "Allow", "Action": network, "Resource": "*"},
            {"Effect": "Deny", "Action": network, "Resource": "*",
             "Condition": {"ArnEquals": {"lambda:SourceFunctionArn": arn}}},
        ]}
        if aws("iam", "get-role-policy", "--role-name", name, "--policy-name", name).get("PolicyDocument") != expected:
            raise ValueError("DevOps event policy differs from its reviewed module")
        rule = aws("events", "describe-rule", "--name", name)
        targets = aws("events", "list-targets-by-rule", "--rule", name)
        if (rule.get("Arn") != f"arn:aws:events:{region}:{account}:rule/{name}"
                or rule.get("State") not in {"ENABLED", "DISABLED"}
                or targets.get("NextToken")
                or targets.get("Targets") != [{"Id": name.rsplit("-", 1)[1], "Arn": arn}]):
            raise ValueError("DevOps event trigger is not bound to its selected function")
        result[name] = {"function": function, "rule": rule["State"]}
    return result


def auxiliary_receipt(observed):
    return {name: {"code_sha256": item["function"]["CodeSha256"], "rule_state": item["rule"]}
            for name, item in observed.items()}


def wait_mapping(uuid, expected):
    for _ in range(30):
        if (
            aws("lambda", "get-event-source-mapping", "--uuid", uuid)["State"]
            == expected
        ):
            return
        time.sleep(2)
    raise RuntimeError("Queue mapping did not reach the requested state")


def pause(repair, uuid, auxiliary=()):
    aws("lambda", "update-event-source-mapping", "--uuid", uuid, "--no-enabled")
    wait_mapping(uuid, "Disabled")
    for rule in (repair, *auxiliary):
        aws("events", "disable-rule", "--name", rule)
        if aws("events", "describe-rule", "--name", rule)["State"] != "DISABLED":
            raise RuntimeError("Service schedule did not disable")


def resume(repair, uuid, auxiliary=()):
    try:
        aws("lambda", "update-event-source-mapping", "--uuid", uuid, "--enabled")
        wait_mapping(uuid, "Enabled")
        for rule in (repair, *auxiliary):
            aws("events", "enable-rule", "--name", rule)
            if aws("events", "describe-rule", "--name", rule)["State"] != "ENABLED":
                raise RuntimeError("Service schedule did not enable")
    except Exception:
        pause(repair, uuid, auxiliary)
        raise


def allow_bounded_continuations(worker):
    """Keep intentional, SQL-bounded poll chains alive beyond Lambda's cutoff."""
    current = aws("lambda", "get-function-recursion-config", "--function-name", worker)
    if current.get("RecursiveLoop") != "Allow":
        aws("lambda", "put-function-recursion-config", "--function-name", worker,
            "--recursive-loop", "Allow")
    if aws("lambda", "get-function-recursion-config", "--function-name", worker).get("RecursiveLoop") != "Allow":
        raise RuntimeError("Bounded queue continuation policy did not take effect; consumers remain paused")


def rollout(worker, repair, uuid, bucket, key, version, digest, auxiliary=()):
    pause(repair, uuid, auxiliary)
    for name in (worker, repair, *auxiliary):
        previous = aws("lambda", "get-function-configuration", "--function-name", name)
        if previous.get("CodeSha256") == digest:
            continue
        aws(
            "lambda",
            "update-function-code",
            "--function-name",
            name,
            "--s3-bucket",
            bucket,
            "--s3-key",
            key,
            "--s3-object-version",
            version,
            "--revision-id",
            previous["RevisionId"],
            "--publish",
        )
        aws("lambda", "wait", "function-updated", "--function-name", name)
        aws("lambda", "wait", "function-active", "--function-name", name)
        current = aws("lambda", "get-function-configuration", "--function-name", name)
        if (
            current.get("CodeSha256") != digest
            or current.get("LastUpdateStatus") != "Successful"
            or current.get("State") != "Active"
        ):
            raise RuntimeError(
                "Worker readback differs from the selected archive; consumers stay paused"
            )


def receipt_state(observed):
    return {
        "worker_sha256": observed["worker"].get("CodeSha256"),
        "repair_sha256": observed["repair"].get("CodeSha256"),
        "mapping_state": observed["mapping"],
        "repair_state": observed["rule"],
        "recursive_loop": observed["recursive_loop"],
        "finops_admissions": {
            name: observed[name].get("Environment", {}).get("Variables", {}).get(
                "L5_FINOPS_ADMISSION_ENABLED"
            ) == "true" for name in ("worker", "repair")
        },
    }


def verify_runtime(worker, repair, service, auxiliary=()):
    """Load every installed entrypoint without admitting work or provider effects."""
    with tempfile.TemporaryDirectory() as temporary:
        for function in (worker, repair, *auxiliary):
            response = Path(temporary) / "probe.json"
            invoked = aws("lambda", "invoke", "--function-name", function,
                          "--cli-binary-format", "raw-in-base64-out",
                          "--payload", '{"l5_runtime_probe":1}', str(response))
            if invoked.get("StatusCode") != 200 or invoked.get("FunctionError"):
                raise ValueError("Installed service factory could not load")
            if json.loads(response.read_text()) != {"service_id": service}:
                raise ValueError("Installed factory does not own the selected service")


DATABASE_ACTIONS = {
    "worker_database_grants_missing": (
        "After migrations, the database owner must rerun "
        "modules/L5-services/scripts/bootstrap-worker-db.sql, verify protected-table denial, "
        "then retry resume. See docs/l5-services/deployment-authority.md."
    ),
    "worker_database_role_invalid": (
        "Use the dedicated adp_l5_worker database identity and have the database owner verify "
        "its non-elevated role attributes and sole rds_iam membership before retrying resume."
    ),
    "worker_database_protected_access": (
        "Have the database owner remove unexpected access to non-L5 tables or database/schema "
        "creation and verify worker isolation before retrying resume."
    ),
    "worker_database_schema_access_missing": (
        "Have the database owner rerun modules/L5-services/scripts/bootstrap-worker-db.sql "
        "to restore public schema usage, verify protected-table denial, then retry resume."
    ),
    "worker_database_schema_missing": (
        "Verify the selected worker database and apply the reviewed L5 migrations, then have the "
        "database owner run modules/L5-services/scripts/bootstrap-worker-db.sql before retrying resume."
    ),
    "worker_database_probe_unavailable": (
        "Check the worker's database connection, IAM authentication and database availability, "
        "then rerun the read-only readiness probe before retrying resume."
    ),
    "worker_database_probe_invalid": (
        "Deploy the reviewed worker archive supporting l5-worker-database-privileges/v1 to every "
        "selected entrypoint, then rerun the readiness probe before retrying resume."
    ),
}


class WorkerDatabaseReadinessError(ValueError):
    def __init__(self, code, function, *, missing=(), missing_total=0):
        super().__init__("Installed worker database readiness failed")
        self.diagnosis = {
            "category": code, "function": function,
            "missing_grants": list(missing), "missing_grants_total": missing_total,
            "next_action": DATABASE_ACTIONS[code] + " Consumers remain paused.",
        }


def verify_database_runtime(worker, repair, service, auxiliary=()):
    """Require each installed DB principal's read-only readiness before enabling consumers."""
    from l5_services.workers.database_probe import REQUEST, validate_response

    observations = {}
    with tempfile.TemporaryDirectory() as temporary:
        for function in (worker, repair, *auxiliary):
            response = Path(temporary) / "database-probe.json"
            response.unlink(missing_ok=True)
            try:
                invoked = aws("lambda", "invoke", "--function-name", function,
                              "--cli-binary-format", "raw-in-base64-out",
                              "--payload", json.dumps(REQUEST), str(response))
            except (subprocess.CalledProcessError, OSError):
                raise WorkerDatabaseReadinessError("worker_database_probe_unavailable", function) from None
            if invoked.get("StatusCode") != 200 or invoked.get("FunctionError"):
                raise WorkerDatabaseReadinessError("worker_database_probe_unavailable", function)
            try:
                if response.stat().st_size > 16384:
                    raise ValueError("Oversized database readiness response")
                result = validate_response(json.loads(response.read_text()), service)
            except (ValueError, TypeError, KeyError, OSError):
                raise WorkerDatabaseReadinessError("worker_database_probe_invalid", function) from None
            if not result["passed"]:
                raise WorkerDatabaseReadinessError(result["code"], function,
                    missing=result["missing_grants"], missing_total=result["missing_grants_total"])
            observations[function] = result
    return observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["status", "rollout", "rollback", "resume", "teardown"]
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--service", default="security", choices=["security", "devops", "transform", "well_architected", "finops"])
    parser.add_argument("--account", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--source-sha")
    parser.add_argument("--gateway-wheel-sha256")
    parser.add_argument("--manifest-version")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = {
        "action": args.action,
        "service": args.service,
        "result": "failed",
        "retained": "SQL history, queues and provider assets require reviewed drain and Terraform teardown",
    }
    stage = "target_identity"
    try:
        identity(args.account, args.role_arn)
        worker, repair = functions(args.environment, args.service)
        auxiliary = auxiliary_names(args.environment, args.service)
        stage = "installed_mapping"
        uuid = mapping(worker, args.environment, args.account, args.service)
        stage = "installed_scope"
        initial = state(args.environment, worker, repair, uuid)
        scope_readback(args.environment, args.account, worker, repair, initial, args.service)
        auxiliary_state(auxiliary, initial, args.account)
        if args.action in {"rollout", "rollback", "resume"}:
            stage = "immutable_artifact"
            key, version, digest = manifest_for(
                args.bucket,
                args.source_sha,
                args.gateway_wheel_sha256,
                args.manifest_version,
            )
        stage = args.action
        if args.action in {"rollout", "rollback"}:
            rollout(worker, repair, uuid, args.bucket, key, version, digest, auxiliary)
        elif args.action == "resume":
            observed = state(args.environment, worker, repair, uuid)
            gates = [observed[name].get("Environment", {}).get("Variables", {}).get(
                "L5_FINOPS_ADMISSION_ENABLED"
            ) for name in ("worker", "repair")]
            if gates[0] != gates[1] or gates[0] not in {None, "true", "false"}:
                pause(repair, uuid, auxiliary)
                raise ValueError("Worker and repair FinOps admission controls differ")
            if any(
                observed[name].get("CodeSha256") != digest
                or observed[name].get("LastUpdateStatus") != "Successful"
                or observed[name].get("State") != "Active"
                or not observed[name]
                .get("Environment", {})
                .get("Variables", {})
                .get("L5_WORKER_FACTORY")
                for name in ("worker", "repair")
            ):
                raise ValueError(
                    "Both workers must have matching healthy code and configured factories before resume"
                )
            others = auxiliary_state(auxiliary, observed, args.account)
            if any(item["function"].get("CodeSha256") != digest
                   or item["function"].get("LastUpdateStatus") != "Successful"
                   or item["function"].get("State") != "Active" for item in others.values()):
                raise ValueError("Event functions must use the same healthy reviewed worker archive")
            pause(repair, uuid, auxiliary)
            stage = "runtime_factory"
            verify_runtime(worker, repair, args.service, auxiliary)
            stage = "worker_database_readiness"
            receipt["database_readiness"] = verify_database_runtime(worker, repair, args.service, auxiliary)
            stage = "bounded_queue_continuations"
            allow_bounded_continuations(worker)
            stage = "resume"
            resume(repair, uuid, auxiliary)
        elif args.action == "teardown":
            pause(repair, uuid, auxiliary)
        stage = "final_readback"
        observed = state(args.environment, worker, repair, uuid)
        receipt["observed"] = receipt_state(observed)
        extras = auxiliary_state(auxiliary, observed, args.account)
        receipt["auxiliary"] = auxiliary_receipt(extras)
        receipt["scope"] = scope_readback(args.environment, args.account, worker, repair, observed, args.service)
        if args.action in {"rollout", "rollback", "teardown"} and (
            observed["mapping"] != "Disabled" or observed["rule"] != "DISABLED"
            or any(value["rule"] != "DISABLED" for value in extras.values())
        ):
            raise RuntimeError("Consumers are not stopped")
        receipt["result"] = "retained" if args.action == "teardown" else "passed"
    except (
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        KeyError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        receipt["failure"] = type(error).__name__
        receipt["failure_stage"] = stage
        if isinstance(error, (WorkerConfigurationError, WorkerDatabaseReadinessError)):
            receipt["diagnosis"] = error.diagnosis
        if "uuid" in locals():
            try:
                observed = state(args.environment, worker, repair, uuid)
                receipt["observed"] = receipt_state(observed)
                extras = auxiliary_state(auxiliary, observed, args.account)
                receipt["auxiliary"] = auxiliary_receipt(extras)
            except (
                ValueError,
                RuntimeError,
                subprocess.CalledProcessError,
                KeyError,
                OSError,
            ):
                receipt["readback"] = "failed"
        message = f"L5 {args.service} worker lifecycle failed at {stage}: {type(error).__name__}"
        if isinstance(error, WorkerConfigurationError):
            message += "; missing or invalid fields: " + ", ".join(error.diagnosis["fields"])
            message += ". " + error.diagnosis["next_action"]
        elif isinstance(error, WorkerDatabaseReadinessError):
            message += "; " + error.diagnosis["category"] + ". " + error.diagnosis["next_action"]
        print(message, file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{args.receipt.name}.", dir=args.receipt.parent)
        try:
            with os.fdopen(descriptor, "w") as destination:
                destination.write(json.dumps(receipt, sort_keys=True) + "\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, args.receipt)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return return_code


if __name__ == "__main__":
    sys.exit(main())

"""Offline lifecycle proof with a local AWS boundary; no cloud calls or infrastructure."""

import base64
import hashlib
import importlib.util
import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/L5-services"
SPEC = importlib.util.spec_from_file_location(
    "worker_lifecycle", MODULE / "scripts/worker-lifecycle.py"
)
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)
ACCOUNT = "000000000000"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-trusted-deployment"
WHEEL = "a" * 64
SOURCE = "b" * 40


class FakeAWS:
    def __init__(self, service="security"):
        self.service = service
        self.probe_service = service
        self.database_probe_responses = {}
        self.factory = "l5_services.runtime:factory" if service == "security" else f"l5_services.runtime_{service}:factory"
        self.extra_policies = {}
        if service == "finops":
            self.extra_environment = {"L5_FINOPS_EXECUTOR_ARN": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-dev-l5-finops-executor"}
            self.extra_policies = {"adp-dev-l5-finops-executor-private": {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": self.extra_environment["L5_FINOPS_EXECUTOR_ARN"]},
                {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                 "Resource": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/adp-dev-l5-finops-executor:*"},
            ]}}
        elif service == "transform":
            bucket = f"adp-dev-l5-transform-{ACCOUNT}"
            self.extra_environment = {
                "L5_TRANSFORM_ARTIFACT_BUCKET": bucket,
                "L5_TRANSFORM_CLUSTER_ENDPOINT": "https://cluster.example.invalid",
                "L5_TRANSFORM_CLUSTER_NAME": "isolated-cluster",
                "L5_TRANSFORM_CLUSTER_CA": "certificate-reference",
                "L5_TRANSFORM_NODE_NAME": "isolated-node",
                "L5_TRANSFORM_NODE_UID": "node-uid",
                "L5_TRANSFORM_PROVIDER_NAMESPACE": "transform-provider",
                "L5_TRANSFORM_PROVIDER_IMAGE": "provider@sha256:" + "a" * 64,
                "L5_TRANSFORM_CHECK_NAMESPACE": "transform-checks",
                "L5_TRANSFORM_CHECK_IMAGE": "checks@sha256:" + "b" * 64,
            }
            self.extra_policies = {"transform-artifacts": {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObjectVersion"],
                 "Resource": f"arn:aws:s3:::{bucket}/l5/transform/artifacts/*"},
                {"Effect": "Allow", "Action": ["s3:GetObject"],
                 "Resource": f"arn:aws:s3:::{bucket}/l5/transform/artifacts/*/checkpoints/*"},
                {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}",
                 "Condition": {"StringLike": {"s3:prefix": "l5/transform/artifacts/*/checkpoints/*"}}},
            ]}}
        else:
            self.extra_environment = {}
        self.prefix = "adp-dev-l5" + ("" if service == "security" else "-" + service.replace("_", "-"))
        self.calls = []
        self.code = {f"{self.prefix}-worker": "old-code", f"{self.prefix}-repair": "old-code"}
        self.auxiliary = tuple(f"{self.prefix}-{kind}" for kind in ("native", "readback")) if service == "devops" else ()
        self.code.update({name: "old-code" for name in self.auxiliary})
        self.event_rules = {name: "ENABLED" for name in self.auxiliary}
        self.foreign_event_target = False
        self.fail_event = False
        self.rule = "ENABLED"
        self.mapping = "Enabled"
        self.recursive_loop = "Terminate"
        self.ignore_recursion_update = False
        self.archive = b"selected-worker-archive"
        self.source = SOURCE
        self.fail_repair = False
        self.fail_resume = False
        self.fail_resume_rule = None
        self.fail_disable = False
        self.foreign_mapping = False
        self.tamper = False
        self.expand_policy = False
        self.repair_policy_fault = None
        self.bad_redrive = False
        self.extra_policy = False
        self.bad_trust = False
        self.missing_trust = False
        self.missing_queue = False
        self.finops_gate = dict.fromkeys(self.code, "false")

    def __call__(self, *args):
        self.calls.append(args)
        service, action = args[:2]
        if (service, action) == ("sts", "get-caller-identity"):
            return {
                "Account": ACCOUNT,
                "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/adp-dev-trusted-deployment/session",
            }
        if service == "s3api" and action == "get-object":
            key = args[args.index("--key") + 1]
            content = self.archive
            if key.endswith("manifest.json"):
                content = json.dumps(
                    {
                        "source_sha": self.source,
                        "wheel_sha256": WHEEL,
                        "archive_sha256": hashlib.sha256(self.archive).hexdigest(),
                        "archive_version": "archive-v1",
                    }
                ).encode()
            elif self.tamper:
                content = b"wrong-archive"
            Path(args[-1]).write_bytes(content)
            return {"VersionId": args[args.index("--version-id") + 1]}
        if service == "lambda" and action == "list-event-source-mappings":
            account = "111111111111" if self.foreign_mapping else ACCOUNT
            name = f"{self.prefix}-work"
            return {
                "EventSourceMappings": [
                    {
                        "UUID": "uuid",
                        "EventSourceArn": f"arn:aws:sqs:us-east-1:{account}:{name}",
                    }
                ]
            }
        if service == "lambda" and action == "get-event-source-mapping":
            return {"State": self.mapping}
        if service == "lambda" and action == "get-function-recursion-config":
            assert args[args.index("--function-name") + 1] == f"{self.prefix}-worker"
            return {"RecursiveLoop": self.recursive_loop}
        if service == "lambda" and action == "put-function-recursion-config":
            assert args[args.index("--function-name") + 1] == f"{self.prefix}-worker"
            assert self.mapping == "Disabled" and self.rule == "DISABLED"
            if not self.ignore_recursion_update:
                self.recursive_loop = args[args.index("--recursive-loop") + 1]
            return {"RecursiveLoop": self.recursive_loop}
        if service == "lambda" and action == "update-event-source-mapping":
            self.mapping = "Disabled" if "--no-enabled" in args else "Enabled"
            return {}
        if service == "lambda" and action == "get-function-configuration":
            name = args[args.index("--function-name") + 1]
            return {
                "FunctionArn": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}",
                "Role": f"arn:aws:iam::{ACCOUNT}:role/{name}",
                "CodeSha256": self.code[name],
                "RevisionId": "revision",
                "LastUpdateStatus": "Successful",
                "State": "Active",
                "Environment": {
                    "Variables": {"L5_SERVICE_ID": self.service, "L5_WORKER_FACTORY": self.factory, **self.extra_environment,
                                  "L5_FINOPS_ADMISSION_ENABLED": self.finops_gate[name],
                                  "L5_GATEWAY_URL": "https://testapi.execute-api.us-east-1.amazonaws.com/dev",
                                  "L5_RDS_CONNECT_ARN": f"arn:aws:rds-db:us-east-1:{ACCOUNT}:dbuser:db-resource/bgadmin",
                                  "L5_RDS_HOST": "db.example.invalid", "L5_RDS_PORT": "5432",
                                  "L5_RDS_DATABASE": "gateway", "L5_RDS_USER": "bgadmin",
                                  "L5_ARCHIVE_BUCKET": "private-artifacts",
                                  "L5_WORK_QUEUE_URL": f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/{self.prefix}-work"}
                },
            }
        if service == "lambda" and action == "update-function-code":
            name = args[args.index("--function-name") + 1]
            if (self.fail_repair and name.endswith("-repair")) or (self.fail_event and name.endswith("-native")):
                raise subprocess.CalledProcessError(1, args)
            self.code[name] = base64.b64encode(
                hashlib.sha256(self.archive).digest()
            ).decode()
            return {}
        if service == "lambda" and action == "invoke":
            from l5_services.workers.database_probe import REQUEST, response
            payload = json.loads(args[args.index("--payload") + 1])
            if payload == REQUEST:
                name = args[args.index("--function-name") + 1]
                result = self.database_probe_responses.get(name,
                    response(self.service, "worker_database_ready", tables_checked=42))
                Path(args[-1]).write_text(result if isinstance(result, str) else json.dumps(result))
            else:
                Path(args[-1]).write_text(json.dumps({"service_id": self.probe_service}))
            return {"StatusCode": 200}
        if service == "lambda" and action == "wait":
            return {}
        if service == "events" and action == "describe-rule":
            name = args[args.index("--name") + 1]
            return {"State": self.event_rules.get(name, self.rule), "Arn": f"arn:aws:events:us-east-1:{ACCOUNT}:rule/{name}"}
        if service == "events" and action == "list-targets-by-rule":
            name = args[args.index("--rule") + 1]
            target = name + "-foreign" if self.foreign_event_target else name
            return {"Targets": [{"Id": name.rsplit("-", 1)[1], "Arn": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{target}"}]}
        if service == "events" and action in {"disable-rule", "enable-rule"}:
            if self.fail_disable and action == "disable-rule":
                raise subprocess.CalledProcessError(1, args)
            if action == "enable-rule" and (self.fail_resume or args[args.index("--name") + 1] == self.fail_resume_rule):
                raise subprocess.CalledProcessError(1, args)
            name = args[args.index("--name") + 1]
            if name in self.event_rules:
                self.event_rules[name] = "DISABLED" if action == "disable-rule" else "ENABLED"
            else:
                self.rule = "DISABLED" if action == "disable-rule" else "ENABLED"
            return {}
        if service == "iam":
            name = args[args.index("--role-name") + 1]
            if action == "get-role":
                role = {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
                        "PermissionsBoundary": {"PermissionsBoundaryArn": f"arn:aws:iam::{ACCOUNT}:policy/approved-boundary"}}
                if not self.missing_trust or not name.endswith("repair"):
                    principal = ({"AWS": f"arn:aws:iam::{ACCOUNT}:root"} if self.bad_trust and name.endswith("worker")
                                 else {"Service": "lambda.amazonaws.com"})
                    role["AssumeRolePolicyDocument"] = {"Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": principal}]}
                return {"Role": role}
            if action == "list-role-policies":
                return {"PolicyNames": [name, *(self.extra_policies if name.endswith("worker") else [])]}
            if action == "list-attached-role-policies":
                return {"AttachedPolicies": [{"PolicyArn": "extra"}] if self.extra_policy else []}
            if action == "get-role-policy":
                policy_name = args[args.index("--policy-name") + 1]
                if policy_name in self.extra_policies:
                    return {"PolicyDocument": self.extra_policies[policy_name]}
                region = "us-east-1"
                network = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
                           "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses",
                           "ec2:UnassignPrivateIpAddresses"]
                queue_actions = (["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
                                  "sqs:ChangeMessageVisibility", "sqs:SendMessage"] if name.endswith("worker") else ["sqs:SendMessage"])
                if self.expand_policy:
                    queue_actions = ["sqs:*"]
                document = {"Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                     "Resource": f"arn:aws:logs:{region}:{ACCOUNT}:log-group:/aws/lambda/{name}:*"},
                    {"Effect": "Allow", "Action": queue_actions,
                     "Resource": f"arn:aws:sqs:{region}:{ACCOUNT}:{self.prefix}-work"},
                    {"Effect": "Allow", "Action": ["rds-db:connect"],
                     "Resource": f"arn:aws:rds-db:{region}:{ACCOUNT}:dbuser:db-resource/bgadmin"},
                    *([{"Effect": "Allow", "Action": ["execute-api:Invoke"],
                        "Resource": f"arn:aws:execute-api:{region}:{ACCOUNT}:testapi/*/*/internal/*"},
                       {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
                        "Resource": f"arn:aws:s3:::private-artifacts/l5/{self.service}/work/*"}]
                      if name.endswith("worker") else []),
                    {"Effect": "Allow", "Action": network, "Resource": "*"},
                    {"Effect": "Deny", "Action": network, "Resource": "*",
                     "Condition": {"ArnEquals": {"lambda:SourceFunctionArn": f"arn:aws:lambda:{region}:{ACCOUNT}:function:{name}"}}},
                ]}
                if name in self.auxiliary:
                    document["Statement"].insert(3, {"Effect": "Allow", "Action": ["execute-api:Invoke"],
                        "Resource": f"arn:aws:execute-api:{region}:{ACCOUNT}:testapi/*/*/internal/*"})
                if name.endswith("-repair") and self.repair_policy_fault != "missing":
                    suffix = {
                        "security": "l5-contributions", "devops": "l5-devops-contributions",
                        "transform": "l5-transform-contributions", "well_architected": "l5-wa-contributions",
                        "finops": "l5-cost-contributions",
                    }[self.service]
                    consumers = ["context"] if self.service == "security" else ["context", "insights"]
                    if self.repair_policy_fault == "extra_consumer":
                        consumers.append("insights" if self.service == "security" else "credentials")
                    if self.repair_policy_fault == "wrong_service":
                        suffix = "l5-devops-contributions" if self.service == "security" else "l5-contributions"
                    resources = []
                    for consumer in consumers:
                        base = f"arn:aws:execute-api:{region}:{ACCOUNT}:testapi"
                        path = f"internal/v1/{consumer}/{suffix}"
                        resources.extend([
                            f"{base}/*/POST/{path}",
                            f"{base}/*/POST/{path}/*/invalidate",
                            f"{base}/*/GET/{path}/*",
                        ])
                    if self.service in {"well_architected", "transform", "security"}:
                        resources.append(f"{base}/*/POST/internal/v1/l5/{self.service.replace('_', '-')}/schedule-admissions")
                    if self.repair_policy_fault == "wide_path":
                        resources[0] = f"arn:aws:execute-api:{region}:{ACCOUNT}:testapi/*/*/internal/*"
                    if self.repair_policy_fault == "wrong_method":
                        resources[0] = resources[0].replace("/POST/", "/DELETE/")
                    document["Statement"].append({
                        "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": resources,
                    })
                return {"PolicyDocument": document}
        if service == "sqs" and action == "get-queue-url":
            return {"QueueUrl": f"https://sqs.us-east-1.amazonaws.com/{ACCOUNT}/{args[args.index('--queue-name') + 1]}"}
        if service == "sqs" and action == "get-queue-attributes":
            queue_name = args[args.index("--queue-url") + 1].rsplit("/", 1)[-1]
            dead_letter = queue_name.endswith("-dlq")
            return {"Attributes": {
                "QueueArn": f"arn:aws:sqs:us-east-1:{ACCOUNT}:{queue_name}",
                "MessageRetentionPeriod": "1209600" if dead_letter else ("0" if self.missing_queue else "345600"),
                "ApproximateNumberOfMessages": "2",
                "ApproximateNumberOfMessagesDelayed": "0",
                "ApproximateNumberOfMessagesNotVisible": "1",
                "RedrivePolicy": json.dumps({
                    "deadLetterTargetArn": f"arn:aws:sqs:us-east-1:{ACCOUNT}:{self.prefix}-work-dlq",
                    "maxReceiveCount": 10 if self.bad_redrive else 5,
                }),
            }}
        raise AssertionError(f"Unexpected mocked operation: {service} {action}")


def invoke(
    fake, monkeypatch, tmp_path, action, *, source=SOURCE, wheel=WHEEL, role=ROLE, service="security"
):
    monkeypatch.setattr(lifecycle, "aws", fake)
    receipt = tmp_path / f"{action}.json"
    result = lifecycle.main(
        [
            action,
            "--service",
            service,
            "--environment",
            "dev",
            "--account",
            ACCOUNT,
            "--role-arn",
            role,
            "--bucket",
            "private-artifacts",
            "--source-sha",
            source,
            "--gateway-wheel-sha256",
            wheel,
            "--manifest-version",
            "manifest-v1",
            "--receipt",
            str(receipt),
        ]
    )
    return result, json.loads(receipt.read_text())


def test_status_records_owned_roles_boundary_queue_depth_and_retention(monkeypatch, tmp_path):
    fake = FakeAWS()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "status")
    assert result == 0
    assert stat.S_IMODE((tmp_path / "status.json").stat().st_mode) == 0o600
    assert receipt["scope"]["roles"]["repair"]["boundary_arn"] == f"arn:aws:iam::{ACCOUNT}:policy/approved-boundary"
    for label in ("worker", "repair"):
        assert receipt["scope"]["roles"][label]["trust_policy"]["Statement"] == [
            {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "lambda.amazonaws.com"}}]
    assert receipt["scope"]["queues"]["dead_letter"]["retention_seconds"] == 1209600
    assert receipt["scope"]["queues"]["work"]["approximate_messages"]["ApproximateNumberOfMessages"] == "2"
    assert receipt["observed"]["mapping_state"] == "Enabled"
    assert receipt["observed"]["finops_admissions"] == {"worker": False, "repair": False}


@pytest.mark.parametrize("service", list(lifecycle.FACTORIES))
def test_missing_database_reference_is_actionable_before_any_mutation(monkeypatch, tmp_path, capsys, service):
    fake = FakeAWS(service)

    def missing_reference(*args):
        result = fake(*args)
        if args[:2] == ("lambda", "get-function-configuration"):
            result["Environment"]["Variables"].pop("L5_RDS_CONNECT_ARN")
        return result

    result, receipt = invoke(missing_reference, monkeypatch, tmp_path, "rollout", service=service)
    assert result == 1 and receipt["failure_stage"] == "installed_scope"
    assert receipt["diagnosis"]["category"] == "worker_configuration_invalid"
    assert receipt["diagnosis"]["fields"] == ["L5_RDS_CONNECT_ARN"]
    assert "apply its reviewed root" in receipt["diagnosis"]["next_action"]
    assert "L5_RDS_CONNECT_ARN" in capsys.readouterr().err
    assert not any(call[:2] in {("lambda", "update-function-code"), ("lambda", "invoke"),
                               ("lambda", "update-event-source-mapping"), ("events", "enable-rule")}
                   for call in fake.calls)


def test_invalid_configuration_values_never_enter_lifecycle_diagnostics(monkeypatch, tmp_path, capsys):
    fake = FakeAWS()
    sensitive = "private-value-sentinel/credential"

    def invalid_configuration(*args):
        result = fake(*args)
        if args[:2] == ("lambda", "get-function-configuration"):
            result["Environment"]["Variables"]["L5_GATEWAY_URL"] = sensitive
        return result

    result, receipt = invoke(invalid_configuration, monkeypatch, tmp_path, "rollout")
    assert result == 1 and receipt["diagnosis"]["fields"] == ["L5_GATEWAY_URL"]
    assert sensitive not in json.dumps(receipt) + capsys.readouterr().err


@pytest.mark.parametrize("service", ["transform", "finops"])
@pytest.mark.parametrize("fault", ["missing", "foreign_resource"])
def test_executor_configuration_failure_names_fields_without_private_values(monkeypatch, tmp_path, capsys, service, fault):
    fake = FakeAWS(service)
    fields = sorted(fake.extra_environment)
    sensitive = "private-foreign-executor-reference"
    if fault == "missing":
        fake.extra_environment.clear()
    else:
        field = "L5_TRANSFORM_ARTIFACT_BUCKET" if service == "transform" else "L5_FINOPS_EXECUTOR_ARN"
        fake.extra_environment[field] = sensitive
        fields = [field]
    result, receipt = invoke(fake, monkeypatch, tmp_path, "rollout", service=service)
    message = capsys.readouterr().err
    assert result == 1 and receipt["failure_stage"] == "installed_scope"
    assert receipt["diagnosis"]["fields"] == fields
    assert all(field in message for field in fields)
    assert sensitive not in message + json.dumps(receipt)
    assert "L5_RDS_CONNECT_ARN must be injected" not in message
    assert ("Transform artifact/executor bootstrap" if service == "transform" else "selected FinOps executor") in message
    assert not any(call[:2] in {("lambda", "update-function-code"), ("lambda", "invoke"),
                               ("lambda", "update-event-source-mapping"), ("events", "enable-rule")}
                   for call in fake.calls)


def test_worker_resume_refuses_mismatched_finops_gate_without_dropping_artifacts(monkeypatch, tmp_path):
    fake = FakeAWS()
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    fake.finops_gate["adp-dev-l5-worker"] = "true"
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1 and receipt["failure"] == "ValueError"
    assert receipt["observed"]["finops_admissions"] == {"worker": True, "repair": False}
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert not any(args[:2] == ("events", "enable-rule") for args in fake.calls)
    fake.finops_gate["adp-dev-l5-repair"] = "true"
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 0 and receipt["observed"]["finops_admissions"] == {"worker": True, "repair": True}


@pytest.mark.parametrize("prior", ["existing", "symlink"])
def test_lifecycle_replaces_receipt_privately_without_following_old_link(monkeypatch, tmp_path, prior):
    path = tmp_path / "status.json"
    previous = tmp_path / "old.json"
    previous.write_text("old private receipt")
    if prior == "symlink":
        path.symlink_to(previous)
    else:
        path.write_text("old public receipt")
    result, receipt = invoke(FakeAWS(), monkeypatch, tmp_path, "status")
    assert result == 0 and receipt["result"] == "passed"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.is_symlink()
    assert previous.read_text() == "old private receipt"


@pytest.mark.parametrize("fault", [
    "expand_policy", "missing_queue", "bad_redrive", "extra_policy", "bad_trust", "missing_trust",
])
def test_unbounded_policy_or_missing_retention_prevents_resume(monkeypatch, tmp_path, fault):
    fake = FakeAWS()
    setattr(fake, fault, True)
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1
    assert receipt["failure"] == "ValueError"
    assert "scope" not in receipt
    assert fake.mapping == "Enabled"
    assert not any(args[:2] == ("events", "enable-rule") for args in fake.calls)


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
@pytest.mark.parametrize("fault", ["missing", "wrong_service", "wide_path", "wrong_method", "extra_consumer"])
def test_repair_contribution_scope_must_match_installed_service(monkeypatch, tmp_path, service, fault):
    fake = FakeAWS(service)
    fake.repair_policy_fault = fault
    result, receipt = invoke(fake, monkeypatch, tmp_path, "rollout", service=service)
    assert result == 1 and receipt["failure"] == "ValueError"
    assert "scope" not in receipt
    assert not any(args[:2] in {
        ("lambda", "update-function-code"), ("lambda", "update-event-source-mapping"),
        ("events", "enable-rule"), ("events", "disable-rule"),
    } for args in fake.calls)


def test_teardown_readback_failure_keeps_private_retention_record(monkeypatch, tmp_path):
    fake = FakeAWS()
    fake.missing_queue = True
    result, receipt = invoke(fake, monkeypatch, tmp_path, "teardown")
    assert result == 1
    assert receipt["result"] == "failed"
    assert receipt["failure"] == "ValueError"
    assert "SQL history" in receipt["retained"]
    assert receipt["observed"]["mapping_state"] == "Enabled"
    assert not any(args[:2] == ("lambda", "update-function-code") for args in fake.calls)


def test_explicit_release_pauses_both_consumers_before_code_update_then_resumes(
    monkeypatch, tmp_path
):
    fake = FakeAWS()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "rollout")
    assert result == 0 and receipt["result"] == "passed"
    assert receipt["observed"]["mapping_state"] == "Disabled"
    assert receipt["observed"]["repair_state"] == "DISABLED"
    assert fake.code["adp-dev-l5-worker"] == fake.code["adp-dev-l5-repair"]
    operations = [(service, action) for service, action, *_ in fake.calls]
    assert operations.index(("events", "disable-rule")) < operations.index(
        ("lambda", "update-function-code")
    )
    assert all(
        "--revision-id" in args and "--publish" in args
        for args in fake.calls
        if args[:2] == ("lambda", "update-function-code")
    )
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 0 and receipt["observed"]["mapping_state"] == "Enabled"
    assert receipt["observed"]["repair_state"] == "ENABLED"
    assert receipt["observed"]["recursive_loop"] == "Allow"
    result, receipt = invoke(fake, monkeypatch, tmp_path, "teardown")
    assert (
        result == 0 and receipt["result"] == "retained" and fake.mapping == "Disabled"
    )
    assert not any(action.startswith("delete-") for _, action, *_ in fake.calls)


def test_partial_publication_failure_stays_paused_and_compatible_rollback_repairs_pair(
    monkeypatch, tmp_path
):
    fake = FakeAWS()
    fake.fail_repair = True
    result, receipt = invoke(fake, monkeypatch, tmp_path, "rollout")
    assert result == 1 and receipt["failure"] == "CalledProcessError"
    assert receipt["observed"]["mapping_state"] == "Disabled"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert fake.code["adp-dev-l5-worker"] != fake.code["adp-dev-l5-repair"]
    fake.fail_repair = False
    fake.archive = b"previous-compatible-worker"
    fake.source = "c" * 40
    result, receipt = invoke(
        fake, monkeypatch, tmp_path, "rollback", source=fake.source
    )
    assert result == 0 and receipt["observed"]["mapping_state"] == "Disabled"
    assert fake.code["adp-dev-l5-worker"] == fake.code["adp-dev-l5-repair"]
    assert invoke(fake, monkeypatch, tmp_path, "resume", source=fake.source)[0] == 0


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_resume_allows_only_the_selected_workers_bounded_queue_chain(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service=service)[0] == 0
    assert invoke(fake, monkeypatch, tmp_path, "resume", service=service)[0] == 0
    updates = [call for call in fake.calls if call[:2] == ("lambda", "put-function-recursion-config")]
    assert len(updates) == 1 and updates[0][updates[0].index("--function-name") + 1] == f"{fake.prefix}-worker"
    assert fake.recursive_loop == "Allow"
    assert invoke(fake, monkeypatch, tmp_path, "resume", service=service)[0] == 0
    assert len([call for call in fake.calls if call[:2] == ("lambda", "put-function-recursion-config")]) == 1


def test_resume_stays_paused_when_continuation_policy_readback_disagrees(monkeypatch, tmp_path):
    fake = FakeAWS()
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    fake.ignore_recursion_update = True
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1 and receipt["failure_stage"] == "bounded_queue_continuations"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert receipt["observed"]["recursive_loop"] == "Terminate"


@pytest.mark.parametrize("case", ["bad-role", "wrong-wheel", "tampered-archive"])
def test_wrong_identity_incompatible_gateway_or_changed_archive_never_updates_code(
    monkeypatch, tmp_path, case
):
    fake = FakeAWS()
    if case == "tampered-archive":
        fake.tamper = True
    role = ROLE if case != "bad-role" else f"arn:aws:iam::{ACCOUNT}:role/other"
    wheel = WHEEL if case != "wrong-wheel" else "0" * 64
    result, receipt = invoke(
        fake, monkeypatch, tmp_path, "rollout", wheel=wheel, role=role
    )
    assert result == 1 and receipt["result"] == "failed"
    assert (
        fake.code["adp-dev-l5-worker"] == fake.code["adp-dev-l5-repair"] == "old-code"
    )
    assert not any(
        args[:2] == ("lambda", "update-function-code") for args in fake.calls
    )


def test_workflow_and_terraform_keep_publication_explicit_and_durable():
    lifecycle_workflow = yaml.load(
        (ROOT / ".github/workflows/l5-services-worker-lifecycle.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    artifact_workflow = yaml.load(
        (ROOT / ".github/workflows/l5-services-release.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert list(lifecycle_workflow["on"]) == ["workflow_dispatch"]
    lifecycle_job = lifecycle_workflow["jobs"]["lifecycle"]
    assert lifecycle_job["if"] == "github.ref == 'refs/heads/main'"
    assert (
        lifecycle_job["environment"]
        == artifact_workflow["jobs"]["install"]["environment"]
    )
    assert (
        lifecycle_workflow["concurrency"]["group"]
        == artifact_workflow["concurrency"]["group"]
    )
    steps = lifecycle_workflow["jobs"]["lifecycle"]["steps"]
    assert any(
        step.get("uses") == "./.github/actions/trusted-deployment" for step in steps
    )
    assert any("--receipt" in step.get("run", "") for step in steps)
    assert any(
        "s3api put-object" in step.get("run", "")
        and "if-none-match" in step.get("run", "")
        for step in steps
    )
    worker = (MODULE / "infra/modules/worker-runtime/main.tf").read_text()
    repair = (MODULE / "infra/modules/worker-runtime/repair.tf").read_text()
    queue = (MODULE / "infra/modules/worker-runtime/queues.tf").read_text()
    assert (
        "ignore_changes = [s3_bucket, s3_key, s3_object_version, source_code_hash]"
        in worker
    )
    assert (
        "ignore_changes = [s3_bucket, s3_key, s3_object_version, source_code_hash]"
        in repair
    )
    assert (
        'state               = "DISABLED"' in repair
        and "ignore_changes = [state]" in repair
    )
    assert (
        "enabled                 = false" in queue
        and "ignore_changes = [enabled]" in queue
    )
    assert queue.count("prevent_destroy = true") == 2


def test_failed_resume_pauses_both_consumers_and_retains_readback(
    monkeypatch, tmp_path
):
    fake = FakeAWS()
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    fake.fail_resume = True
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1 and receipt["failure"] == "CalledProcessError"
    assert receipt["observed"]["mapping_state"] == "Disabled"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    fake.fail_resume = False
    assert invoke(fake, monkeypatch, tmp_path, "resume")[0] == 0


def test_failed_teardown_records_partial_cleanup_and_keeps_queues(
    monkeypatch, tmp_path
):
    fake = FakeAWS()
    fake.fail_disable = True
    result, receipt = invoke(fake, monkeypatch, tmp_path, "teardown")
    assert result == 1 and receipt["failure"] == "CalledProcessError"
    assert receipt["observed"]["mapping_state"] == "Disabled"
    assert receipt["observed"]["repair_state"] == "ENABLED"
    assert "retained" in receipt
    assert not any(
        args[:2] == ("lambda", "update-function-code") for args in fake.calls
    )


def test_foreign_mapping_and_unverified_worker_factory_fail_closed(
    monkeypatch, tmp_path
):
    fake = FakeAWS()
    fake.foreign_mapping = True
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 1
    assert not any(
        args[:2] == ("lambda", "update-event-source-mapping") for args in fake.calls
    )
    fake.foreign_mapping = False
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    original = fake.__call__

    def no_factory(*args):
        result = original(*args)
        if args[:2] == ("lambda", "get-function-configuration"):
            result["Environment"]["Variables"].clear()
        return result

    monkeypatch.setattr(lifecycle, "aws", no_factory)
    receipt = tmp_path / "no-factory.json"
    result = lifecycle.main(
        [
            "resume",
            "--environment",
            "dev",
            "--account",
            ACCOUNT,
            "--role-arn",
            ROLE,
            "--bucket",
            "private-artifacts",
            "--source-sha",
            SOURCE,
            "--gateway-wheel-sha256",
            WHEEL,
            "--manifest-version",
            "manifest-v1",
            "--receipt",
            str(receipt),
        ]
    )
    assert result == 1 and json.loads(receipt.read_text())["result"] == "failed"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"


@pytest.mark.parametrize("service", ["devops", "transform", "well_architected", "finops"])
def test_service_rollout_only_changes_its_workers(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    result, receipt = invoke(fake, monkeypatch, tmp_path, "rollout", service=service)
    assert result == 0 and receipt["result"] == "passed"
    changed = [call[call.index("--function-name") + 1] for call in fake.calls if call[:2] == ("lambda", "update-function-code")]
    prefix = "adp-dev-l5-" + service.replace("_", "-")
    assert changed == [prefix + "-worker", prefix + "-repair", *fake.auxiliary]
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert receipt["service"] == service


def test_resume_keeps_consumers_paused_if_factory_owns_another_service(monkeypatch, tmp_path):
    fake = FakeAWS("finops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="finops")[0] == 0
    fake.probe_service = "security"
    fake.calls.clear()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume", service="finops")
    assert result == 1 and receipt["result"] == "failed"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert not any(call[:2] == ("events", "enable-rule") for call in fake.calls)


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_each_installed_factory_and_selected_executor_policy_can_roll_out(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service=service)[0] == 0
    assert invoke(fake, monkeypatch, tmp_path, "resume", service=service)[0] == 0


@pytest.mark.parametrize("service", ["devops", "transform", "well_architected", "finops"])
def test_sibling_cannot_use_security_factory(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    fake.factory = "l5_services.runtime:factory"
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service=service)[0] == 1
    assert not any(args[:2] == ("lambda", "update-function-code") for args in fake.calls)


@pytest.mark.parametrize("service", ["finops", "transform"])
def test_service_executor_policy_cannot_expand_resource_scope(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    next(iter(fake.extra_policies.values()))["Statement"][0]["Resource"] = "*"
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service=service)[0] == 1
    assert not any(args[:2] == ("lambda", "update-function-code") for args in fake.calls)


@pytest.mark.parametrize("service", ["finops", "transform"])
def test_service_executor_cannot_select_another_resource(monkeypatch, tmp_path, service):
    fake = FakeAWS(service)
    field = next(iter(fake.extra_environment))
    fake.extra_environment[field] += "-foreign"
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service=service)[0] == 1
    assert not any(args[:2] == ("lambda", "update-function-code") for args in fake.calls)


def test_devops_rollout_and_resume_cover_event_and_readback_workers(monkeypatch, tmp_path):
    fake = FakeAWS("devops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 0
    assert len(set(fake.code.values())) == 1
    assert set(fake.event_rules.values()) == {"DISABLED"}
    assert invoke(fake, monkeypatch, tmp_path, "resume", service="devops")[0] == 0
    assert set(fake.event_rules.values()) == {"ENABLED"}
    probes = {args[args.index("--function-name") + 1] for args in fake.calls if args[:2] == ("lambda", "invoke")}
    assert probes == set(fake.code)


def test_devops_partial_event_rollout_keeps_all_consumers_paused(monkeypatch, tmp_path):
    fake = FakeAWS("devops")
    fake.fail_event = True
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 1
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert set(fake.event_rules.values()) == {"DISABLED"}


def test_devops_foreign_event_target_is_rejected_before_mutation(monkeypatch, tmp_path):
    fake = FakeAWS("devops")
    fake.foreign_event_target = True
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 1
    assert not any(args[:2] == ("lambda", "update-function-code") for args in fake.calls)


def test_devops_partial_resume_pauses_every_consumer_and_records_events(monkeypatch, tmp_path):
    fake = FakeAWS("devops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 0
    fake.fail_resume_rule = fake.auxiliary[-1]
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume", service="devops")
    assert result == 1
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert set(fake.event_rules.values()) == {"DISABLED"}
    assert set(receipt["auxiliary"]) == set(fake.auxiliary)
    assert all(item["rule_state"] == "DISABLED" for item in receipt["auxiliary"].values())


def test_devops_resume_refuses_unrolled_event_worker(monkeypatch, tmp_path):
    fake = FakeAWS("devops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 0
    fake.code[fake.auxiliary[0]] = "old-code"
    result, _ = invoke(fake, monkeypatch, tmp_path, "resume", service="devops")
    assert result == 1
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert set(fake.event_rules.values()) == {"DISABLED"}


@pytest.mark.parametrize("suffix", ["worker", "repair", "native", "readback"])
def test_database_grant_gap_on_any_devops_entrypoint_keeps_all_consumers_paused(monkeypatch, tmp_path, suffix):
    from l5_services.workers.database_probe import response
    fake = FakeAWS("devops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 0
    function = fake.prefix + "-" + suffix
    fake.database_probe_responses[function] = response("devops", "worker_database_grants_missing", tables_checked=42,
        missing=[{"table": "l5_service_setup_authority_continuations", "privileges": ["SELECT"]}])
    fake.calls.clear()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume", service="devops")
    assert result == 1 and receipt["failure_stage"] == "worker_database_readiness"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert set(fake.event_rules.values()) == {"DISABLED"}
    assert not any(call[:2] == ("events", "enable-rule") for call in fake.calls)
    diagnosis = receipt["diagnosis"]
    assert diagnosis["category"] == "worker_database_grants_missing" and diagnosis["function"] == function
    assert diagnosis["missing_grants"][0]["table"] == "l5_service_setup_authority_continuations"
    assert "database owner" in diagnosis["next_action"] and "bootstrap-worker-db.sql" in diagnosis["next_action"]


@pytest.mark.parametrize("code", ["worker_database_protected_access", "worker_database_role_invalid",
                                 "worker_database_probe_unavailable", "worker_database_schema_missing", "worker_database_schema_access_missing"])
def test_database_safety_failures_keep_consumers_paused_with_action(monkeypatch, tmp_path, code):
    from l5_services.workers.database_probe import response
    fake = FakeAWS()
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    fake.database_probe_responses[fake.prefix + "-worker"] = response("security", code)
    fake.calls.clear()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1 and receipt["diagnosis"]["category"] == code
    assert "Consumers remain paused" in receipt["diagnosis"]["next_action"]
    assert not any(call[:2] == ("events", "enable-rule") for call in fake.calls)


@pytest.mark.parametrize("bad_response", [{"service_id": "security"}, {}, "not-json-private-marker", "x" * 16385,
    {"protocol": "old", "passed": True}, {"protocol": "l5-worker-database-privileges/v1", "passed": True}])
def test_missing_or_malformed_database_probe_cannot_resume(monkeypatch, tmp_path, capsys, bad_response):
    fake = FakeAWS()
    assert invoke(fake, monkeypatch, tmp_path, "rollout")[0] == 0
    fake.database_probe_responses[fake.prefix + "-worker"] = bad_response
    fake.calls.clear()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume")
    assert result == 1 and receipt["diagnosis"]["category"] == "worker_database_probe_invalid"
    assert fake.mapping == "Disabled" and fake.rule == "DISABLED"
    assert "private-marker" not in json.dumps(receipt) + capsys.readouterr().err
    assert not any(call[:2] == ("events", "enable-rule") for call in fake.calls)


def test_all_database_probes_pass_before_any_consumer_is_enabled(monkeypatch, tmp_path):
    from l5_services.workers.database_probe import REQUEST
    fake = FakeAWS("devops")
    assert invoke(fake, monkeypatch, tmp_path, "rollout", service="devops")[0] == 0
    fake.calls.clear()
    result, receipt = invoke(fake, monkeypatch, tmp_path, "resume", service="devops")
    assert result == 0 and set(receipt["database_readiness"]) == set(fake.code)
    probes = [index for index, call in enumerate(fake.calls)
              if call[:2] == ("lambda", "invoke") and json.loads(call[call.index("--payload") + 1]) == REQUEST]
    enabled = [index for index, call in enumerate(fake.calls) if call[:2] == ("events", "enable-rule")
               or (call[:2] == ("lambda", "update-event-source-mapping") and "--enabled" in call)]
    assert len(probes) == 4 and max(probes) < min(enabled)


def test_absent_second_probe_body_cannot_reuse_first_functions_success(monkeypatch, tmp_path):
    from l5_services.workers.database_probe import REQUEST
    fake = FakeAWS("devops")
    missing_function = fake.prefix + "-repair"

    def aws(*args):
        if (args[:2] == ("lambda", "invoke") and args[args.index("--function-name") + 1] == missing_function
                and json.loads(args[args.index("--payload") + 1]) == REQUEST):
            return {"StatusCode": 200}
        return fake(*args)

    monkeypatch.setattr(lifecycle, "aws", aws)
    with pytest.raises(lifecycle.WorkerDatabaseReadinessError) as failure:
        lifecycle.verify_database_runtime(fake.prefix + "-worker", missing_function, "devops", fake.auxiliary)
    assert failure.value.diagnosis["category"] == "worker_database_probe_invalid"
    assert failure.value.diagnosis["function"] == missing_function

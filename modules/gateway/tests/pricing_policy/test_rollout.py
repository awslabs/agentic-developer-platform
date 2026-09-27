"""Exercise release command ordering/failure states without AWS or Kubernetes."""

import importlib.util
import io
import json
import runpy
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

GATEWAY = Path(__file__).resolve().parents[2]
SCRIPT = GATEWAY / "scripts/pricing-rollout.py"
SPEC = importlib.util.spec_from_file_location("pricing_rollout_tested", SCRIPT)
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)
ACCOUNT = "123456789012"
IMAGE = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway:" + "a" * 40
FUNCTION = "bedrockgw-dev-pricing-refresh"
ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{FUNCTION}"


def pod(name, image=IMAGE, ready=True, terminating=False):
    return {
        "metadata": {"name": name, **({"deletionTimestamp": "2026-09-12"} if terminating else {})},
        "spec": {"containers": [{"name": "bedrockgateway", "image": image}]},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.state = "ENABLED"
        self.exists = True
        self.logging = True
        self.paused = False
        self.published = False
        self.function_error = False
        self.refresh_status = "published"
        self.partial = False
        self.failed_sources = []
        self.timeout = 180
        self.confirm_enable = True
        self.pods = [pod("new"), pod("old", "old:sha", terminating=True)]
        self.replicas = 1
        self.image = IMAGE
        self.account = ACCOUNT

    def run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        result = {}
        if cmd[0] == "kubectl":
            if "deployment/bedrockgateway" in cmd:
                result = {
                    "spec": {"replicas": self.replicas, "template": {"spec": {"containers": [{"name": "bedrockgateway", "image": self.image}]}}}
                }
            elif "pods" in cmd:
                result = {"items": self.pods}
            elif "alembic" not in cmd:
                result = {
                    "generation_id": 2 if self.published else 1,
                    "pointer_revision": 2 if self.published else 1,
                    "variants": 330,
                    "refresh_paused": self.paused,
                    "chat_logging_enabled": self.logging,
                }
        elif cmd[2] == "get-caller-identity":
            result = {"Account": self.account}
        elif cmd[2] == "describe-rule":
            if not self.exists:
                return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (ResourceNotFoundException)")
            result = {"State": self.state if self.confirm_enable else "DISABLED", "ScheduleExpression": "cron(0 6 * * ? *)"}
        elif cmd[2] == "disable-rule":
            self.state = "DISABLED"
        elif cmd[2] == "enable-rule":
            self.state = "ENABLED"
        elif cmd[2] == "get-function-configuration":
            result = {"Timeout": self.timeout}
        elif cmd[2] == "get-function-event-invoke-config":
            result = {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600, "DestinationConfig": {"OnFailure": {"Destination": "execution"}}}
        elif cmd[2] == "list-targets-by-rule":
            result = {
                "Targets": [
                    {
                        "Arn": ARN,
                        "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600},
                        "DeadLetterConfig": {"Arn": "delivery"},
                    }
                ]
            }
        elif cmd[2] == "invoke":
            self.published = True
            Path(cmd[cmd.index("--payload") + 2]).write_text(
                json.dumps(
                    {
                        "status": self.refresh_status,
                        "generation_id": 2,
                        "pointer_revision": 2,
                        "variants": 330,
                        "partial": self.partial,
                        "fresh_variants": 300,
                        "retained_variants": 30 if self.partial else 0,
                        "failed_sources": self.failed_sources,
                    }
                )
            )
            result = {"StatusCode": 200, **({"FunctionError": "Unhandled"} if self.function_error else {})}
        else:
            raise AssertionError(f"Unexpected external command: {cmd[:3]}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

    def operations(self):
        return [tuple(cmd[1:3]) for cmd, _ in self.calls if cmd[0] == "aws"]


@pytest.fixture
def args():
    return SimpleNamespace(account_id=ACCOUNT, region="us-east-1", environment="dev", namespace="adp-gateway", expected_image=IMAGE)


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCLI()
    monkeypatch.setattr(rollout.subprocess, "run", fake.run)
    monkeypatch.setattr(rollout, "verify_lambda_code", lambda *_: {"FunctionArn": ARN, "Timeout": fake.timeout})
    monkeypatch.setattr(rollout, "verify_queue", lambda *_: None)
    monkeypatch.setattr(rollout, "verify_alarm_routes", lambda *_: None)
    return fake


def test_quiesce_drains_actual_old_timeout_after_disabling(cli, args, monkeypatch):
    sleeps = []
    monkeypatch.setattr(rollout.time, "sleep", sleeps.append)
    cli.timeout = 73
    rollout.quiesce(args)
    assert cli.operations() == [("events", "describe-rule"), ("events", "disable-rule"), ("lambda", "get-function-configuration")]
    assert sum(sleeps) == 78 and max(sleeps) <= 30
    assert cli.state == "DISABLED"


def test_access_denied_is_not_missing_bootstrap(args, monkeypatch):
    monkeypatch.setattr(
        rollout.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (AccessDeniedException)")
    )
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        rollout.quiesce(args)


def test_migrate_only_ready_release_pod(cli, args):
    rollout.verify_seed(args, migrate=True)
    executions = [cmd for cmd, _ in cli.calls if cmd[:2] == ["kubectl", "exec"]]
    assert "alembic" in executions[0]
    assert all("new" in cmd and "old" not in cmd and "bedrockgateway" in cmd for cmd in executions)


@pytest.mark.parametrize("during_migration", [True, False])
@pytest.mark.parametrize("terminating", [True, False])
def test_replaced_pod_reselects_release_and_recovers(cli, args, monkeypatch, during_migration, terminating):
    original = cli.run
    failed = False
    migrations = []

    def run(cmd, **kwargs):
        nonlocal failed
        if cmd[:3] == ["kubectl", "get", "pod"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(pod("new", terminating=True)) if terminating else "", stderr="")
        if cmd[:2] == ["kubectl", "exec"]:
            if "alembic" in cmd:
                migrations.append(cmd)
            if not failed and ("alembic" in cmd) == during_migration:
                failed = True
                cli.pods = [pod("replacement")]
                return SimpleNamespace(returncode=137, stdout="", stderr="command terminated with exit code 137")
        return original(cmd, **kwargs)

    monkeypatch.setattr(rollout.subprocess, "run", run)
    evidence = rollout.verify_seed(args, migrate=True)
    assert failed and [item["pod"] for item in evidence] == ["replacement"]
    assert len(migrations) == (2 if during_migration else 1)


def test_exec_failure_on_live_pod_is_not_retried(cli, args, monkeypatch):
    original = cli.run
    executions = []

    def run(cmd, **kwargs):
        if cmd[:3] == ["kubectl", "get", "pod"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(pod("new")), stderr="")
        if cmd[:2] == ["kubectl", "exec"]:
            executions.append(cmd)
            return SimpleNamespace(returncode=137, stdout="", stderr="command terminated with exit code 137")
        return original(cmd, **kwargs)

    monkeypatch.setattr(rollout.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="exit code 137"):
        rollout.verify_seed(args, migrate=True)
    assert len(executions) == 1


def test_pod_replacement_retry_is_bounded(cli, args, monkeypatch):
    calls = []

    def replaced(*parts, **kwargs):
        calls.append(parts)
        raise rollout.PodReplaced("replaced again")

    monkeypatch.setattr(rollout, "pod_command", replaced)
    with pytest.raises(rollout.PodReplaced):
        rollout.verify_seed(args, migrate=True)
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["image", "replica", "terminating", "unready"])
def test_incomplete_rollout_never_migrates(cli, args, failure):
    if failure == "image":
        cli.image = "old:sha"
    elif failure == "replica":
        cli.replicas = 2
    else:
        cli.pods = [pod("bad", ready=failure != "unready", terminating=failure == "terminating")]
    with pytest.raises(RuntimeError):
        rollout.verify_seed(args, migrate=True)
    assert not any(cmd[:2] == ["kubectl", "exec"] for cmd, _ in cli.calls)


def test_finalize_full_publication_then_enable(cli, args):
    rollout.finalize(args)
    ops = cli.operations()
    assert ops.index(("events", "disable-rule")) < ops.index(("lambda", "invoke")) < ops.index(("events", "enable-rule"))
    assert cli.state == "ENABLED"


@pytest.mark.parametrize("failure", ["exception", "deferred", "paused", "infra", "confirm-enable"])
def test_finalize_failure_keeps_schedule_disabled(cli, args, failure):
    cli.function_error = failure == "exception"
    cli.refresh_status = "deferred" if failure == "deferred" else "published"
    cli.paused = failure == "paused"
    cli.timeout = 60 if failure == "infra" else 180
    cli.confirm_enable = failure != "confirm-enable"
    with pytest.raises((AssertionError, RuntimeError)):
        rollout.finalize(args)
    assert cli.state == "DISABLED"
    if failure != "confirm-enable":
        assert ("events", "enable-rule") not in cli.operations()


def test_optional_chat_logging_still_requires_seed(cli, args):
    cli.logging = False
    cli.exists = False
    rollout.finalize(args)
    assert any(cmd[:2] == ["kubectl", "exec"] for cmd, _ in cli.calls)
    assert not any(service == "lambda" for service, _ in cli.operations())
    assert ("events", "enable-rule") not in cli.operations()


def test_enabled_pricing_cannot_hide_missing_infra(cli, args):
    cli.exists = False
    with pytest.raises(AssertionError, match="schedule is missing"):
        rollout.finalize(args)
    assert ("events", "enable-rule") not in cli.operations()


def test_account_mismatch_prevents_mutation(cli, monkeypatch):
    cli.account = "999999999999"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "quiesce", "--account-id", ACCOUNT])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert result.value.code == 1
    assert cli.operations() == [("sts", "get-caller-identity")]


def test_lambda_zip_metadata_ignored_but_source_mismatch_rejected(args, monkeypatch):
    spec = importlib.util.spec_from_file_location("pricing_real_builder", GATEWAY / "scripts/build-budget-lambda-archives.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    entries = builder.manifest(GATEWAY, "pricing-refresh")

    def payload(changed=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            for name, path in reversed(list(entries.items())):
                archive.writestr(name, b"wrong source" if changed and name == "handler.py" else path.read_bytes())
        return stream.getvalue()

    monkeypatch.setattr(
        rollout,
        "aws",
        lambda *a, **k: {
            "Configuration": {"State": "Active", "LastUpdateStatus": "Successful"},
            "Code": {"Location": "https://example.invalid/private-download"},
        },
    )
    monkeypatch.setattr(rollout.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload()))
    rollout.verify_lambda_code(args, FUNCTION)
    monkeypatch.setattr(rollout.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload(True)))
    with pytest.raises(AssertionError, match="source differs"):
        rollout.verify_lambda_code(args, FUNCTION)


def test_workflows_serialize_and_pin_release_image():
    root = GATEWAY.parents[1]
    workflows = {
        name: yaml.load((root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
        for name in ("gateway-deploy.yml", "gateway-infra-apply.yml", "pricing-finalize.yml")
    }
    assert len({w["concurrency"]["group"] for w in workflows.values()}) == 1
    jobs = workflows["gateway-deploy.yml"]["jobs"]
    assert jobs["finalize-pricing"]["needs"] == ["deploy-backend", "run-migrations"]
    assert "finalize-pricing" in jobs["smoke-test"]["needs"]
    assert jobs["run-migrations"]["with"]["expected_image_tag"] == "${{ inputs.adp_source_revision || github.sha }}"
    assert "release_image" not in jobs["deploy-backend"].get("outputs", {})
    finalize = next(step for step in jobs["finalize-pricing"]["steps"] if step.get("name") == "Verify release pricing and enable the schedule")
    assert "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/adp-gateway:${PRICING_RELEASE_TAG}" in finalize["run"]
    migration = yaml.load((root / ".github/workflows/run-gateway-migrations.yml").read_text(), Loader=yaml.BaseLoader)
    migrate_steps = migration["jobs"]["migrate"]["steps"]
    context = next(i for i, step in enumerate(migrate_steps) if step.get("name") == "Select trusted EKS context")
    execute = next(i for i, step in enumerate(migrate_steps) if step.get("name") == "Migrate and verify activated pricing on the release image")
    assert context < execute
    assert "aws eks update-kubeconfig" in migrate_steps[context]["run"]
    assert "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/adp-gateway:${PRICING_EXPECTED_IMAGE_TAG}" in migrate_steps[execute]["run"]


@pytest.mark.parametrize(
    "changed",
    [
        "modules/gateway/pricing_policy/policy.py",
        "modules/gateway/lambda/shared/pricing_v2_reader.py",
        "modules/gateway/lambda/pricing-refresh/publication.py",
        "modules/gateway/scripts/pricing-rollout.py",
        "modules/gateway/scripts/build-budget-lambda-archives.py",
        ".github/workflows/pricing-finalize.yml",
        ".github/workflows/run-gateway-migrations.yml",
        "codebuild/bs-gateway-build.yml",
    ],
)
def test_real_change_detection_updates_backend_and_lambdas(changed, tmp_path):
    import os
    import subprocess
    from fnmatch import fnmatchcase

    root = GATEWAY.parents[1]
    workflow = yaml.load((root / ".github/workflows/gateway-deploy.yml").read_text(), Loader=yaml.BaseLoader)
    assert any(fnmatchcase(changed, pattern) for pattern in workflow["on"]["push"]["paths"])
    body = next(step["run"] for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "filter")
    body = body.replace("${{ github.event_name }}", "push")
    output = tmp_path / "outputs"
    subprocess.run(
        ["bash", "-eu", "-c", 'git() { printf "%s\\n" "$PRICING_TEST_CHANGED"; }\n' + body],
        env={**os.environ, "PRICING_TEST_CHANGED": changed, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=True,
    )
    decisions = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert decisions["backend"] == "true"
    assert decisions["budget_lambdas"] == "true"
    assert decisions["frontend"] == "false"


@pytest.mark.parametrize("problem", ["unencrypted", "short-retention"])
def test_queue_verification_rejects_missing_operational_guarantees(args, monkeypatch, problem):
    arn = f"arn:aws:sqs:us-east-1:{ACCOUNT}:pricing-test"
    attributes = {"QueueArn": arn, "MessageRetentionPeriod": "1209600", "SqsManagedSseEnabled": "true"}
    attributes["SqsManagedSseEnabled" if problem == "unencrypted" else "MessageRetentionPeriod"] = "false" if problem == "unencrypted" else "86400"
    monkeypatch.setattr(rollout, "aws", lambda *a, **k: {"QueueUrl": "queue-url"} if "get-queue-url" in a else {"Attributes": attributes})
    with pytest.raises(AssertionError):
        rollout.verify_queue(args, arn)


def test_freshness_alarm_cannot_pass_with_unconfirmed_destination(args, monkeypatch):
    def response(_args, service, operation, *parts):
        if service == "cloudwatch":
            return {
                "MetricAlarms": [
                    {"AlarmName": name, "ActionsEnabled": True, "TreatMissingData": "breaching", "AlarmActions": ["arn:aws:sns:topic"]}
                    for name in parts[1:]
                ]
            }
        return {"Subscriptions": [{"SubscriptionArn": "PendingConfirmation"}]}

    monkeypatch.setattr(rollout, "aws", response)
    with pytest.raises(AssertionError, match="confirmed subscription"):
        rollout.verify_alarm_routes(args)


def test_readiness_wait_recovers_after_node_replacement(cli, args, monkeypatch):
    args.readiness_timeout = 30
    cli.replicas = 2
    cli.pods = [pod("serving"), pod("replacement", ready=False)]
    ticks = [0.0]
    monkeypatch.setattr(rollout.time, "monotonic", lambda: ticks[0])

    def advance(seconds):
        ticks[0] += seconds
        cli.pods[1] = pod("replacement")

    monkeypatch.setattr(rollout.time, "sleep", advance)
    rollout.verify_seed(args, migrate=True)
    assert ticks[0] == 10
    executions = [cmd for cmd, _ in cli.calls if cmd[:2] == ["kubectl", "exec"]]
    assert "alembic" in executions[0]
    assert {cmd[cmd.index("adp-gateway") + 1] for cmd in executions} == {"serving", "replacement"}


def test_readiness_wait_times_out_without_migration(cli, args, monkeypatch):
    args.readiness_timeout = 12
    cli.pods = [pod("unready", ready=False)]
    ticks = [0.0]
    monkeypatch.setattr(rollout.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(rollout.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    with pytest.raises(RuntimeError, match="required gateway replicas"):
        rollout.verify_seed(args, migrate=True)
    assert ticks[0] == 12
    assert not any(cmd[:2] == ["kubectl", "exec"] for cmd, _ in cli.calls)


def test_readiness_wait_cannot_hide_wrong_release(cli, args, monkeypatch):
    args.readiness_timeout = 180
    cli.image = "wrong:release"

    def unexpected_sleep(_):
        pytest.fail("Wrong-image failure must be immediate")

    monkeypatch.setattr(rollout.time, "sleep", unexpected_sleep)
    with pytest.raises(RuntimeError, match="expected release image"):
        rollout.ready_pods(args)


@pytest.mark.parametrize("approved", [False, True])
def test_partial_finalization_requires_explicit_recovery_option(cli, args, approved):
    cli.partial = True
    args.allow_partial_refresh = approved
    if approved:
        rollout.finalize(args)
        assert cli.state == "ENABLED"
    else:
        with pytest.raises(RuntimeError, match="retained older rates"):
            rollout.finalize(args)
        assert cli.state == "DISABLED"


def test_partial_recovery_cannot_hide_transport_failure(cli, args):
    cli.partial = True
    cli.failed_sources = ["https://aws.example/failed"]
    args.allow_partial_refresh = True
    with pytest.raises(AssertionError, match="Transport failures"):
        rollout.finalize(args)
    assert cli.state == "DISABLED"

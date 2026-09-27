"""Cloud-free account bootstrap and bounded invocation contracts."""

import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "bedrock_access", ROOT / "platform/scripts/bedrock-model-access.py"
)
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)
READY = {
    "authorizationStatus": "AUTHORIZED",
    "entitlementAvailability": "AVAILABLE",
    "regionAvailability": "AVAILABLE",
    "agreementAvailability": {"status": "AVAILABLE"},
}
MODEL = "global.anthropic.claude-sonnet-5"


class Cloud:
    region = "us-east-1"

    def __init__(self, row=None):
        self.row = copy.deepcopy(READY if row is None else row)
        self.calls = []
        self.form = None
        self.optional_failure = False
        self.profile_status = "ACTIVE"
        self.reply = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
        self.body = None

    def call(self, service, operation, *args):
        self.calls.append((service, operation, args))
        if operation == "get-foundation-model-availability":
            if self.optional_failure and args[-1] == "anthropic.optional":
                raise access.AccessError("optional model unavailable")
            return copy.deepcopy(self.row)
        if operation == "get-use-case-for-model-access":
            return {"formData": self.form}
        if operation == "put-use-case-for-model-access":
            self.row["authorizationStatus"] = "AUTHORIZED"
            return {}
        if operation == "list-foundation-models":
            return {
                "modelSummaries": [
                    {
                        "modelId": "anthropic.optional",
                        "modelLifecycle": {"status": "ACTIVE"},
                    },
                    {
                        "modelId": "anthropic.old",
                        "modelLifecycle": {"status": "LEGACY"},
                    },
                ]
            }
        if operation == "list-foundation-model-agreement-offers":
            return {"offers": [{"offerToken": "test-offer"}]}
        if operation == "create-foundation-model-agreement":
            self.row["agreementAvailability"]["status"] = "AVAILABLE"
            self.row["entitlementAvailability"] = "AVAILABLE"
            return {}
        if operation == "get-inference-profile":
            return {"status": self.profile_status}
        if operation == "invoke-model":
            self.body = json.loads(
                Path(args[args.index("--body") + 1][len("fileb://") :]).read_text()
            )
            Path(args[-1]).write_text(json.dumps(self.reply))
            return {}
        raise AssertionError(operation)


@pytest.fixture
def form(tmp_path):
    path = tmp_path / "company details.json"
    path.write_text(
        json.dumps(
            {
                "companyName": "Test organization",
                "companyWebsite": "https://example.test",
                "intendedUsers": "operator-supplied",
                "industryOption": "operator-supplied",
                "otherIndustryOption": "",
                "useCases": "Developer assistance",
            }
        )
    )
    return path


def operations(cloud):
    return [c[1] for c in cloud.calls]


def test_defaults_follow_execution_sources(tmp_path):
    assert access.runtime_models() == [MODEL]
    for relative in [
        "modules/agent-factory/agent-worker-image/entrypoint.py",
        "modules/agent-factory/agent/k8s/chat-scaledjob.yaml",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            (ROOT / relative).read_text().replace("claude-sonnet-5", "claude-sonnet-99")
        )
    assert access.runtime_models(tmp_path)[0] == "global.anthropic.claude-sonnet-99"
    (tmp_path / "modules/agent-factory/agent/k8s/chat-scaledjob.yaml").write_text("")
    with pytest.raises(access.AccessError, match="Cannot identify"):
        access.runtime_models(tmp_path)


def test_existing_effective_access_never_submits_even_with_form(form):
    cloud = Cloud()
    access.prepare(cloud, [MODEL], [], form, 0)
    assert "get-use-case-for-model-access" not in operations(cloud)
    assert "put-use-case-for-model-access" not in operations(cloud)
    assert "create-foundation-model-agreement" not in operations(cloud)
    assert "invoke-model" not in operations(cloud)


def test_fresh_registration_then_agreement_and_all_access_checks(form):
    row = copy.deepcopy(READY)
    row.update(
        authorizationStatus="NOT_AUTHORIZED", entitlementAvailability="NOT_AVAILABLE"
    )
    row["agreementAvailability"]["status"] = "NOT_AVAILABLE"
    cloud = Cloud(row)
    access.prepare(cloud, [MODEL], [MODEL], form, 0)
    ops = operations(cloud)
    assert ops.index("put-use-case-for-model-access") < ops.index(
        "create-foundation-model-agreement"
    )
    assert ops.count("put-use-case-for-model-access") == 1
    assert cloud.calls[ops.index("put-use-case-for-model-access")][2] == (
        "--form-data",
        f"fileb://{form}",
    )
    assert "get-inference-profile" in ops


def test_missing_registration_input_stops_before_accepting_any_agreements():
    cloud = Cloud({**READY, "authorizationStatus": "NOT_AUTHORIZED"})
    with pytest.raises(access.AccessError, match="--anthropic-use-case"):
        access.prepare(cloud, [MODEL], [], None, 0)
    assert "create-foundation-model-agreement" not in operations(cloud)
    assert "invoke-model" not in operations(cloud)


def test_existing_registration_is_never_overwritten(form):
    cloud = Cloud({**READY, "authorizationStatus": "NOT_AUTHORIZED"})
    cloud.form = base64.b64encode(b'{"companyName":"Already registered"}').decode()
    with pytest.raises(access.AccessError, match="authorizationStatus"):
        access.prepare(cloud, [MODEL], [MODEL], form, 0)
    assert "put-use-case-for-model-access" not in operations(cloud)


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorizationStatus", "NOT_AUTHORIZED"),
        ("entitlementAvailability", "NOT_AVAILABLE"),
        ("regionAvailability", "NOT_AVAILABLE"),
        ("agreementAvailability", {"status": "NOT_AVAILABLE"}),
    ],
)
def test_available_agreement_alone_is_not_sufficient(field, value):
    cloud = Cloud({**READY, field: value})
    with pytest.raises(access.AccessError, match=field):
        access.check_required(cloud, [MODEL], 0)


def test_missing_availability_fields_fail_closed():
    with pytest.raises(access.AccessError, match="missing"):
        access.check_required(Cloud({}), [MODEL], 0)


def test_inactive_profile_refuses_verification():
    cloud = Cloud()
    cloud.profile_status = "INACTIVE"
    with pytest.raises(access.AccessError, match="profile"):
        access.check_required(cloud, [MODEL], 0)
    assert "invoke-model" not in operations(cloud)


def test_optional_model_failure_does_not_hide_default_readiness():
    cloud = Cloud()
    cloud.optional_failure = True
    access.prepare(cloud, [MODEL], [], None, 0)
    assert "get-inference-profile" in operations(cloud)
    assert not any("anthropic.old" in c[2] for c in cloud.calls)


def test_verify_is_bounded_and_does_not_change_access(monkeypatch):
    cloud = Cloud()
    monkeypatch.setattr(access, "AWS", lambda region: cloud)
    assert access.main(["--verify", "--wait-seconds", "0"]) == 0
    assert operations(cloud).count("invoke-model") == len(access.runtime_models())
    assert cloud.body["max_tokens"] == 8
    assert cloud.body["messages"] == [{"role": "user", "content": "Reply OK."}]
    assert not any(op.startswith(("put-", "create-")) for op in operations(cloud))


def test_prepare_and_verify_blocks_rollout_on_invocation_denial(monkeypatch):
    cloud = Cloud()
    original = cloud.call

    def call(service, operation, *args):
        if operation == "invoke-model":
            raise access.AccessError("invoke-model: AccessDeniedException")
        return original(service, operation, *args)

    cloud.call = call
    monkeypatch.setattr(access, "AWS", lambda region: cloud)
    with pytest.raises(access.AccessError, match="AccessDeniedException"):
        access.main(["--prepare-and-verify", "--wait-seconds", "0"])
    assert "get-inference-profile" in operations(cloud)


def test_prepare_and_verify_registers_before_invoking(monkeypatch, form):
    cloud = Cloud({**READY, "authorizationStatus": "NOT_AUTHORIZED"})
    monkeypatch.setattr(access, "AWS", lambda region: cloud)
    access.main(
        ["--prepare-and-verify", "--use-case-file", str(form), "--wait-seconds", "0"]
    )
    ops = operations(cloud)
    assert ops.index("put-use-case-for-model-access") < ops.index("invoke-model")
    assert ops.count("invoke-model") == len(access.runtime_models())


@pytest.mark.parametrize("mode", ["prepare-and-verify", "verify"])
def test_chat_rollout_stops_before_terraform_or_kubernetes_on_model_denial(tmp_path, mode):
    relative = "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"
    script = tmp_path / relative
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / relative, script)
    helper = tmp_path / "platform/scripts/enable-bedrock-models.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        f'#!/bin/bash\n[ "$1" = --{mode} ] || exit 9\necho "model denied" >&2\nexit 1\n'
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ["terraform", "kubectl", "aws"]:
        path = binary / name
        path.write_text('#!/bin/bash\necho "unexpected deployment call" >&2\nexit 9\n')
        path.chmod(0o700)
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "ENVIRONMENT": "test",
            "AGENT_IMAGE": "test:sha",
            "ADP_CHAT_MODEL_ACCESS_MODE": mode,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "model denied" in result.stderr
    assert "unexpected deployment call" not in result.stderr
    assert "Reading Terraform outputs" not in result.stdout


@pytest.mark.parametrize(
    "reply", [{}, {"type": "error"}, {"type": "message", "content": []}]
)
def test_empty_or_error_response_never_counts_as_first_run_success(reply):
    cloud = Cloud()
    cloud.reply = reply
    with pytest.raises(access.AccessError, match="no model text"):
        access.verify_invocations(cloud, [MODEL])
    assert operations(cloud).count("invoke-model") == 1


def test_verify_refusal_is_not_retried(monkeypatch):
    def fail(*args):
        raise access.AccessError("invoke-model: AccessDeniedException")

    cloud = Cloud()
    cloud.call = fail
    with pytest.raises(access.AccessError, match="AccessDeniedException"):
        access.verify_invocations(cloud, [MODEL])


def test_cli_uses_selected_identity_and_disables_hidden_retries(monkeypatch):
    calls = []
    monkeypatch.setenv("AWS_PROFILE", "customer-account")
    monkeypatch.setattr(
        access.subprocess,
        "run",
        lambda cmd, **kw: calls.append((cmd, kw))
        or SimpleNamespace(returncode=0, stdout="{}"),
    )
    access.AWS("eu-west-1").call("bedrock", "get-use-case-for-model-access")
    command, options = calls[0]
    assert command[command.index("--region") + 1] == "eu-west-1"
    assert options["env"]["AWS_PROFILE"] == "customer-account"
    assert options["env"]["AWS_MAX_ATTEMPTS"] == "1"
    assert options["timeout"] == 90


def test_provider_errors_do_not_echo_registration_contents(monkeypatch):
    monkeypatch.setattr(
        access.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stderr="An error occurred (ValidationException): secret company form contents",
        ),
    )
    with pytest.raises(access.AccessError, match="ValidationException") as error:
        access.AWS("us-east-1").call("bedrock", "put-use-case-for-model-access")
    assert "secret company" not in str(error.value)


def test_explicit_models_are_additional_required_not_replacements(monkeypatch):
    seen = []
    monkeypatch.setattr(
        access, "check_required", lambda aws, models, wait: seen.extend(models)
    )
    access.main(["--check", "global.anthropic.claude-opus-5"])
    assert seen == access.runtime_models() + ["global.anthropic.claude-opus-5"]


@pytest.mark.parametrize(
    "model", ["amazon.nova-pro-v1:0", "arn:aws:bedrock:custom", ""]
)
def test_unsupported_model_ids_fail_before_contacting_aws(monkeypatch, model):
    monkeypatch.setattr(
        access, "AWS", lambda *a: pytest.fail("invalid model contacted AWS")
    )
    with pytest.raises(access.AccessError, match="Anthropic"):
        access.main(["--verify", model])


def test_dry_run_never_calls_aws(monkeypatch):
    monkeypatch.setattr(access, "AWS", lambda *a: pytest.fail("dry-run contacted AWS"))
    access.main(["--dry-run"])


def test_real_shell_wrapper_runs_with_no_aws(tmp_path):
    env = {**os.environ, "AWS_PROFILE": "does-not-exist"}
    result = subprocess.run(
        ["bash", str(ROOT / "platform/scripts/enable-bedrock-models.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert MODEL in result.stdout


def test_main_wrapper_dry_run_preserves_backend_cache(tmp_path):
    shutil.copyfile(ROOT / "deploy.sh", tmp_path / "deploy.sh")
    helper = tmp_path / "platform/scripts/enable-bedrock-models.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text('#!/bin/bash\n[ "$1" = "--dry-run" ] || exit 9\n')
    cache = tmp_path / ".terraform/terraform.tfstate"
    cache.parent.mkdir()
    cache.write_text("existing backend")
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ["aws", "terraform", "kubectl", "node"]:
        file = binary / name
        file.write_text("#!/bin/bash\necho 123456789012\n")
        file.chmod(0o700)
    result = subprocess.run(
        ["bash", str(tmp_path / "deploy.sh"), "--dry-run"],
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    assert cache.read_text() == "existing backend"


def test_deployment_entrypoints_verify_before_reporting_success():
    root = (ROOT / "deploy.sh").read_text()
    direct = (ROOT / "platform/scripts/deploy-all.sh").read_text()
    assert root.index("enable-bedrock-models.sh' --verify") < root.index(
        'step "Deployment Complete"'
    )
    assert direct.index('enable-bedrock-models.sh" --verify') < direct.index(
        'step "Deployment complete"'
    )
    assert "export ADP_BEDROCK_VERIFY_DEFERRED=true" in root
    assert "ADP_BEDROCK_VERIFY_DEFERRED:-false" in direct
    assert "Bedrock model access (skipped — update mode)" not in direct


@pytest.mark.parametrize(
    "update,ci,destroy,expected",
    [
        (False, False, False, True),
        (True, False, False, True),
        (False, True, False, False),
        (False, False, True, False),
    ],
)
def test_access_preparation_runs_on_deploy_and_update_only(
    update, ci, destroy, expected
):
    source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
    start = source.index("# Upgrades may introduce a new runtime default too.")
    block = source[
        start : source.index(
            "\n# ---------------------------------------------------------------------------",
            start,
        )
    ]
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\nstep() { :; }\nbash() { echo prepare; }\nfail() { exit 9; }\n"
            + block,
        ],
        env={
            **os.environ,
            "SCRIPT_DIR": str(ROOT / "platform/scripts"),
            "UPDATE_MODE": str(update).lower(),
            "CI_MODE": str(ci).lower(),
            "DESTROY": str(destroy).lower(),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == (["prepare"] if expected else [])


def test_main_wrapper_stops_when_default_model_cannot_invoke(tmp_path):
    shutil.copyfile(ROOT / "deploy.sh", tmp_path / "deploy.sh")
    scripts = tmp_path / "platform/scripts"
    scripts.mkdir(parents=True)
    log = tmp_path / "calls"
    (scripts / "deploy-all.sh").write_text(
        '#!/bin/bash\n[ "$ADP_BEDROCK_VERIFY_DEFERRED" = true ] || exit 9\n'
        'echo deploy >> "$TEST_CALLS"\n'
    )
    (scripts / "enable-bedrock-models.sh").write_text(
        '#!/bin/bash\n[ "$1" = "--verify" ] || exit 9\n'
        'echo verify >> "$TEST_CALLS"\necho "model denied" >&2\nexit 1\n'
    )
    envdir = tmp_path / "environments/dev/modules"
    envdir.mkdir(parents=True)
    (envdir / "gateway.tfvars").write_text('aws_region = "us-east-1"\n')
    (envdir.parent / "platform.tfvars").write_text('aws_region = "us-east-1"\n')
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ["aws", "terraform", "kubectl", "node"]:
        path = binary / name
        path.write_text("#!/bin/bash\necho 123456789012\n")
        path.chmod(0o700)
    result = subprocess.run(
        ["bash", str(tmp_path / "deploy.sh"), "--skip-agents"],
        cwd=tmp_path,
        env={
            **os.environ,
            "TEST_CALLS": str(log),
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 1
    assert log.read_text().splitlines() == ["deploy", "verify"]
    assert "Deployment Complete" not in result.stdout
    assert "model denied" in result.stderr

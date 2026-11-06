import base64
import hashlib
import json
import os
import re
import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
build = runpy.run_path(str(ROOT / "modules/tools/interceptor/package.py"))["build"]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/tools-interceptor-deploy.yml").read_text(), Loader=yaml.BaseLoader
)


def aws_stub(tmp_path):
    command = tmp_path / "aws"
    command.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ['AWS_TEST_CALLS']).open('a') as calls:
    calls.write(json.dumps(args) + '\\n')
operation = args[:2]
if operation[0] == 'lambda' and 'AWS_TEST_LAMBDA_ACTIONS' in os.environ:
    action = 'lambda:' + ('GetFunctionConfiguration' if operation[1] == 'wait' else
                         ''.join(part.title() for part in operation[1].split('-')))
    if action not in json.loads(os.environ['AWS_TEST_LAMBDA_ACTIONS']):
        raise SystemExit('AccessDenied: DenyOtherAPIs: ' + action)
metadata_path = Path(os.environ['AWS_TEST_STATE'] + '.metadata')
metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {
    'RevisionId': 'revision-1',
    'RoutingConfig': {'AdditionalVersionWeights': json.loads(os.environ.get('AWS_TEST_ROUTING', 'null'))},
}
if operation == ['sts', 'get-caller-identity']:
    print(os.environ.get('AWS_TEST_EXPECTED_ACCOUNT', os.environ['ACCOUNT_ID']))
elif operation == ['lambda', 'get-function-configuration']:
    print(os.environ.get('AWS_TEST_INTERCEPTOR_ENVIRONMENT', json.dumps({'Variables': json.loads(os.environ.get('AWS_TEST_INTERCEPTOR_CONFIGURATION', json.dumps({
        'ADP_TOOLS_FACADE_ROLE_ARN': 'arn:aws:iam::123456789012:role/adp-dev-role-tools-facade',
        'ADP_TOOLS_ASSERTION_KEY_ARN': 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000',
    })))})))
elif operation == ['lambda', 'update-function-code']:
    print('42')
elif operation == ['lambda', 'get-function']:
    if os.environ.get('AWS_TEST_MISSING_VERSION'):
        raise SystemExit(1)
    if args[args.index('--query') + 1] == 'Configuration.Version':
        print(args[args.index('--qualifier') + 1])
    else:
        print(os.environ['AWS_TEST_SHA'])
elif operation == ['lambda', 'get-alias']:
    if not Path(os.environ['AWS_TEST_STATE']).exists():
        raise SystemExit('ResourceNotFoundException')
    snapshot = {**metadata, 'FunctionVersion':
        '1' if os.environ.get('AWS_TEST_STALE_ALIAS') else Path(os.environ['AWS_TEST_STATE']).read_text()}
    checked_routing = '--query' not in args or args[args.index('--query') + 1] == 'RoutingConfig.AdditionalVersionWeights'
    if os.environ.get('AWS_TEST_RACE_BEFORE_UPDATE') and checked_routing and metadata['RevisionId'] == 'revision-1':
        metadata = {'RevisionId': 'revision-2', 'RoutingConfig': {'AdditionalVersionWeights': {'41': 0.5}}}
        metadata_path.write_text(json.dumps(metadata))
    if '--query' not in args:
        print(json.dumps(snapshot))
    else:
        query = args[args.index('--query') + 1]
        if query == 'RoutingConfig.AdditionalVersionWeights':
            print(json.dumps(snapshot['RoutingConfig']['AdditionalVersionWeights']))
        else:
            print(snapshot[query])
elif operation in (['lambda', 'update-alias'], ['lambda', 'create-alias']):
    if os.environ.get('AWS_TEST_FAIL_ALIAS'):
        raise SystemExit(1)
    if operation[1] == 'update-alias' and args[args.index('--revision-id') + 1] != metadata['RevisionId']:
        raise SystemExit('PreconditionFailedException')
    Path(os.environ['AWS_TEST_STATE']).write_text(args[args.index('--function-version') + 1])
    if os.environ.get('AWS_TEST_RACE_AFTER_UPDATE'):
        metadata['RoutingConfig']['AdditionalVersionWeights'] = {'40': 0.5}
        metadata_path.write_text(json.dumps(metadata))
elif operation == ['lambda', 'wait']:
    pass
else:
    raise SystemExit(1)
"""
    )
    command.chmod(0o755)
    archive = tmp_path / "reference.zip"
    build(archive)
    digest = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
    state = tmp_path / "alias"
    state.write_text("1")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "TOOLS_INTERCEPTOR_FUNCTION_NAME": "example-interceptor",
        "ADP_TOOLS_RELEASE_DIR": str(tmp_path / "release"),
        "AWS_TEST_SHA": digest,
        "AWS_TEST_CALLS": str(tmp_path / "aws-calls"),
        "AWS_TEST_STATE": str(state),
    }
    return env, digest, state


def run_deploy(env):
    return subprocess.run(
        ["bash", "modules/tools/deploy.sh", "--interceptor"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def calls(env):
    return [json.loads(line) for line in Path(env["AWS_TEST_CALLS"]).read_text().splitlines()]


@pytest.mark.parametrize("arguments", [
    ["--unknown"], ["--interceptor", "--infra", "--targets"],
    ["--infra", "--targets", "--interceptor"],
])
def test_shared_entrypoint_refuses_incomplete_or_mixed_stages(tmp_path, arguments):
    env, _, state = aws_stub(tmp_path)
    result = subprocess.run(
        ["bash", "modules/tools/deploy.sh", *arguments],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert not Path(env["AWS_TEST_CALLS"]).exists()
    assert state.read_text() == "1"
    assert not (tmp_path / "release/release.json").exists()


def test_deterministic_package_contains_only_lambda_sources(tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    build(first)
    build(second)
    assert first.read_bytes() == second.read_bytes()
    from zipfile import ZipFile

    with ZipFile(first) as archive:
        assert set(archive.namelist()) == {
            "tools_interceptor/__init__.py",
            "tools_interceptor/handler.py",
        }


def test_publish_verifies_digest_and_alias_before_receipt(tmp_path):
    env, digest, state = aws_stub(tmp_path)
    result = run_deploy(env)
    assert result.returncode == 0, result.stderr
    assert state.read_text() == "42"
    assert json.loads((tmp_path / "release/release.json").read_text()) == {
        "version": "42", "code_sha256": digest
    }
    operations = [call[:2] for call in calls(env)]
    assert operations.index(["lambda", "update-function-code"]) < operations.index(["lambda", "get-function"])
    assert operations.index(["lambda", "get-function"]) < operations.index(["lambda", "update-alias"])
    assert calls(env)[operations.index(["lambda", "update-function-code"])].count("--publish") == 1


@pytest.mark.parametrize("deny_create_alias", [False, True])
def test_first_release_obeys_scoped_policy_action_ceiling(tmp_path, deny_create_alias):
    source = (ROOT / "platform/automation-infra/tools-deployment.tf").read_text()
    # Terraform mock tests check both rendered ceilings with Knowledge off/on.
    # Keep this subprocess stub tied to the actual interceptor grant and both
    # ceilings, while allowing the separately tested optional action set.
    expression = re.search(r"^\s*tools_actions\s*=\s*(.+)$", source, re.M).group(1)
    compact = re.sub(r"\s+", "", expression)
    assert "flatten([forstatementinlocal.tools_resource_grants:statement.Action])" in compact
    assert "local.tools_knowledge_actions" in compact
    assert len(re.findall(
        r'Sid\s*=\s*"DenyOtherAPIs",\s*Effect\s*=\s*"Deny",\s*'
        r'NotAction\s*=\s*local\.tools_actions,\s*Resource\s*=\s*"\*"', source
    )) == 2
    grant = re.search(r'Sid = "InterceptorCode", Effect = "Allow", Action = \[(.*?)\]', source, re.S)
    actions = re.findall(r'"(lambda:[A-Za-z]+)"', grant.group(1))
    if deny_create_alias:
        actions = [action for action in actions if action != "lambda:CreateAlias"]
    env, digest, state = aws_stub(tmp_path)
    state.unlink()
    env["TOOLS_INTERCEPTOR_FUNCTION_NAME"] = "adp-tools-dev-interceptor"
    env["AWS_TEST_LAMBDA_ACTIONS"] = json.dumps(actions)
    result = run_deploy(env)
    operations = [call[:2] for call in calls(env)]
    assert ["lambda", "create-alias"] in operations
    assert ["lambda", "update-alias"] not in operations
    if deny_create_alias:
        assert result.returncode != 0
        assert "AccessDenied: DenyOtherAPIs: lambda:CreateAlias" in result.stderr
        assert not state.exists()
        assert not (tmp_path / "release/release.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert state.read_text() == "42"
        assert json.loads((tmp_path / "release/release.json").read_text()) == {
            "version": "42", "code_sha256": digest
        }
        assert operations.index(["lambda", "get-function"]) < operations.index(["lambda", "create-alias"])
        assert operations[-1] == ["lambda", "get-alias"]


@pytest.mark.parametrize("failure", ["sha", "account", "missing_function"])
def test_failed_publish_never_changes_alias_or_emits_receipt(tmp_path, failure):
    env, _, state = aws_stub(tmp_path)
    if failure == "sha":
        env["AWS_TEST_SHA"] = "different"
    elif failure == "account":
        env["ACCOUNT_ID"] = "000000000000"
        env["AWS_TEST_EXPECTED_ACCOUNT"] = "123456789012"
    else:
        env.pop("TOOLS_INTERCEPTOR_FUNCTION_NAME")
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == "1"
    assert not (tmp_path / "release/release.json").exists()


def test_dispatch_guard_rejects_partial_engine_inputs(tmp_path):
    guard = WORKFLOW["jobs"]["deploy"]["steps"][0]["run"]
    env = {**os.environ, "GITHUB_REF": "refs/heads/main", "ADP_CORRELATION": "a" * 64,
           "ADP_SOURCE": "", "ADP_DEFINITION": "", "ACTUAL_DEFINITION": "b" * 40}
    result = subprocess.run(["bash", "-e", "-c", guard], cwd=tmp_path, env=env, check=False)
    assert result.returncode != 0


def test_workflow_pins_main_and_calls_shared_stage_before_upload():
    assert WORKFLOW["on"]["push"]["branches"] == ["main"]
    steps = WORKFLOW["jobs"]["deploy"]["steps"]
    assert WORKFLOW["jobs"]["deploy"]["environment"] == "adp-tools-deploy-${{ inputs.environment || 'dev' }}"
    commands = [step.get("run", "") for step in steps]
    assert any('git show "$ADP_DEFINITION:.github/workflows/tools-interceptor-deploy.yml"' in command for command in commands)
    assert any("bash modules/tools/deploy.sh --interceptor" in command for command in commands)
    assert not any("aws lambda" in command for command in commands)
    assert next(index for index, command in enumerate(commands)
                if "bash modules/tools/deploy.sh --interceptor" in command) < next(
        index for index, step in enumerate(steps) if step.get("name") == "Publish release receipt"
    )
    assert steps[-1]["name"] == "Publish release receipt" and "uses" not in steps[-1]
    assert steps[-1]["run"].endswith('release tools-interceptor "$RUNNER_TEMP/adp-release-tools-interceptor/release.json"')


def test_dispatch_guard_rejects_non_main_even_without_engine_inputs(tmp_path):
    guard = WORKFLOW["jobs"]["deploy"]["steps"][0]["run"]
    env = {**os.environ, "GITHUB_REF": "refs/heads/unreviewed", "ADP_CORRELATION": "",
           "ADP_SOURCE": "", "ADP_DEFINITION": "", "ACTUAL_DEFINITION": "b" * 40}
    result = subprocess.run(["bash", "-e", "-c", guard], cwd=tmp_path, env=env, check=False)
    assert result.returncode != 0


def test_missing_release_receipt_on_alias_verification_failure(tmp_path):
    env, _, _ = aws_stub(tmp_path)
    env["AWS_TEST_STALE_ALIAS"] = "true"
    result = run_deploy(env)
    assert result.returncode != 0
    assert not (tmp_path / "release/release.json").exists()


def test_rollback_flips_only_live_alias_and_records_existing_version(tmp_path):
    env, digest, state = aws_stub(tmp_path)
    state.write_text("42")
    env["ROLLBACK_TO_VERSION"] = "41"
    result = run_deploy(env)
    assert result.returncode == 0, result.stderr
    assert state.read_text() == "41"
    assert json.loads((tmp_path / "release/release.json").read_text()) == {
        "version": "41", "code_sha256": digest
    }
    recorded = calls(env)
    operations = [call[:2] for call in recorded]
    assert ["lambda", "update-function-code"] not in operations
    assert ["lambda", "create-alias"] not in operations
    assert ["lambda", "wait"] not in operations
    assert ["lambda", "update-alias"] in operations
    assert "--revision-id" in recorded[operations.index(["lambda", "update-alias"])]


@pytest.mark.parametrize("version,problem", [
    ("invalid", None), ("0", None), ("42", None), ("41", "missing"),
    ("41", "weighted"), ("41", "alias_update"),
])
def test_rollback_failure_does_not_emit_release(tmp_path, version, problem):
    env, _, state = aws_stub(tmp_path)
    state.write_text("42")
    env["ROLLBACK_TO_VERSION"] = version
    if problem == "missing":
        env["AWS_TEST_MISSING_VERSION"] = "true"
    elif problem == "weighted":
        env["AWS_TEST_ROUTING"] = '{"40": 0.5}'
    elif problem == "alias_update":
        env["AWS_TEST_FAIL_ALIAS"] = "true"
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == "42"
    assert not (tmp_path / "release/release.json").exists()


@pytest.mark.parametrize("event,correlation", [
    ("push", ""), ("workflow_dispatch", "a" * 64),
])
def test_rollback_dispatch_rejects_push_or_engine_correlation(tmp_path, event, correlation):
    guard = WORKFLOW["jobs"]["deploy"]["steps"][0]["run"]
    env = {**os.environ, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": event,
           "ADP_CORRELATION": correlation, "ADP_SOURCE": "", "ADP_DEFINITION": "",
           "ROLLBACK_TO_VERSION": "41", "ACTUAL_DEFINITION": "b" * 40}
    result = subprocess.run(["bash", "-e", "-c", guard], cwd=tmp_path, env=env, check=False)
    assert result.returncode != 0


def test_rollback_workflow_has_manual_input_and_bounded_alias_step():
    inputs = WORKFLOW["on"]["workflow_dispatch"]["inputs"]
    assert inputs["rollback_to_version"]["required"] == "false"
    steps = WORKFLOW["jobs"]["deploy"]["steps"]
    publish = next(step for step in steps if step.get("name") == "Publish verified Lambda version and promote live alias")
    assert 'timeout 60s bash modules/tools/deploy.sh --interceptor' in publish["run"]
    assert publish["env"]["ROLLBACK_TO_VERSION"] == "${{ inputs.rollback_to_version || '' }}"


def test_weighted_alias_cannot_claim_single_version_release(tmp_path):
    env, _, state = aws_stub(tmp_path)
    env["AWS_TEST_ROUTING"] = '{"41": 0.5}'
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == "1"
    assert not (tmp_path / "release/release.json").exists()
    assert ["lambda", "update-alias"] not in [call[:2] for call in calls(env)]


@pytest.mark.parametrize("rollback", [False, True])
def test_concurrent_routing_change_invalidates_checked_revision(tmp_path, rollback):
    env, _, state = aws_stub(tmp_path)
    state.write_text("42" if rollback else "1")
    if rollback:
        env["ROLLBACK_TO_VERSION"] = "41"
    env["AWS_TEST_RACE_BEFORE_UPDATE"] = "true"
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == ("42" if rollback else "1")
    assert not (tmp_path / "release/release.json").exists()
    update = next(call for call in calls(env) if call[:2] == ["lambda", "update-alias"])
    assert update[update.index("--revision-id") + 1] == "revision-1"


@pytest.mark.parametrize("rollback", [False, True])
def test_final_readback_refuses_routing_added_after_alias_update(tmp_path, rollback):
    env, _, state = aws_stub(tmp_path)
    if rollback:
        state.write_text("42")
        env["ROLLBACK_TO_VERSION"] = "41"
    env["AWS_TEST_RACE_AFTER_UPDATE"] = "true"
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == ("41" if rollback else "42")
    assert not (tmp_path / "release/release.json").exists()


@pytest.mark.parametrize("configuration", [
    {},
    {"ADP_TOOLS_GATEWAY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade", "ADP_TOOLS_ASSERTION_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"},
    {"ADP_TOOLS_FACADE_ROLE_ARN": "arn:aws:iam::000000000000:role/adp-dev-role-tools-facade", "ADP_TOOLS_ASSERTION_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"},
    {"ADP_TOOLS_FACADE_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-role-gateway-service", "ADP_TOOLS_ASSERTION_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"},
    {"ADP_TOOLS_FACADE_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade", "ADP_TOOLS_ASSERTION_KEY_ARN": "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"},
])
def test_publish_requires_matching_assertion_configuration_before_version_change(tmp_path, configuration):
    env, _, state = aws_stub(tmp_path)
    env["AWS_TEST_INTERCEPTOR_CONFIGURATION"] = json.dumps(configuration)
    result = run_deploy(env)
    assert result.returncode != 0
    assert state.read_text() == "1"
    assert not (tmp_path / "release/release.json").exists()
    assert ["lambda", "update-function-code"] not in [call[:2] for call in calls(env)]


@pytest.mark.parametrize("environment", [
    {"Error": {"ErrorCode": "KMSAccessDeniedException", "Message": "private-deployment-sentinel"}},
    {"Error": {"ErrorCode": "private-deployment-sentinel"}, "Variables": {}},
])
def test_environment_error_refuses_before_publish_without_echoing_provider_details(tmp_path, environment):
    env, _, state = aws_stub(tmp_path)
    env["AWS_TEST_INTERCEPTOR_ENVIRONMENT"] = json.dumps(environment)
    result = run_deploy(env)
    assert result.returncode != 0
    assert "environment cannot be read" in result.stderr
    assert "KMS decrypt authorization" in result.stderr
    assert "private-deployment-sentinel" not in result.stderr + result.stdout
    assert state.read_text() == "1"
    assert not (tmp_path / "release/release.json").exists()
    assert ["lambda", "update-function-code"] not in [call[:2] for call in calls(env)]

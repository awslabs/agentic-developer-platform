"""Deployment shortcuts must preserve configuration and artifact verification."""

import base64
import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "modules/gateway/scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ssm = load("batch-ssm")
updater = load("deploy-budget-lambda-code")


def test_batches_deduplicate_and_separate_decrypted_values():
    source = "\n".join(
        f'get_ssm "/adp/${{ENVIRONMENT}}/gateway/p{i}"' for i in range(23)
    )
    source += '\nget_ssm "/adp/${ENVIRONMENT}/gateway/p0"\nget_authority_ssm "/adp/${ENVIRONMENT}/gateway/p0"'
    calls = []

    def call(args):
        calls.append(args)
        names = [name for name in args if name.startswith("/")]
        assert len(names) <= 10
        return {
            "Parameters": [
                {
                    "Name": name,
                    "Value": "clear" if "--with-decryption" in args else "plain",
                }
                for name in names
            ]
        }

    values = ssm.read_parameters(source, "test", call)
    assert len(calls) == 4 and len(values) == 24
    assert values["plain:/adp/test/gateway/p0"] == "plain"
    assert values["decrypted:/adp/test/gateway/p0"] == "clear"


def test_individual_read_fallback_preserves_missing_parameters_and_decryption():
    calls = []

    def call(args):
        calls.append(args)
        if args[0] == "get-parameters":
            raise ssm.BatchReadDeniedError()
        name = args[2]
        if name.endswith("missing"):
            return {}
        assert "--with-decryption" in args
        return {"Parameter": {"Name": name, "Value": "secret"}}

    values = ssm.read_parameters(
        'get_ssm "/missing"\nget_authority_ssm "/present"', "test", call
    )
    assert values == {"decrypted:/present": "secret"}
    assert sum(args[0] == "get-parameters" for args in calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"Parameters": [{"Name": "/wrong", "Value": "x"}]},
        {"InvalidParameters": ["/wrong"]},
    ],
)
def test_incomplete_or_unrequested_ssm_data_fails(response):
    with pytest.raises(ValueError):
        ssm.read_parameters('get_ssm "/expected"', "test", lambda args: response)


def test_read_failures_do_not_turn_into_default_values():
    def call(args):
        raise RuntimeError("read failed")

    with pytest.raises(RuntimeError, match="read failed"):
        ssm.read_parameters('get_ssm "/expected" "default"', "test", call)


def test_shell_snapshot_preserves_values_defaults_and_has_no_code_execution(tmp_path):
    malicious = "$(touch injected) `touch injected` ' \" \\ $VAR\nsecond line"
    values = {"plain:/present": malicious, "plain:/empty": ""}
    script = (
        ssm.shell_values(values)
        + """
get_ssm() { printf '%s\\n' "${ADP_SSM_VALUES[plain:$1]-${2:-}}"; }
get_ssm /present fallback
get_ssm /empty fallback
get_ssm /missing fallback
"""
    )
    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == malicious + "\n\nfallback\n"
    assert not (tmp_path / "injected").exists()


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"real artifact bytes")
    return path


def configuration(artifact, **overrides):
    return {
        "CodeSha256": base64.b64encode(
            hashlib.sha256(artifact.read_bytes()).digest()
        ).decode(),
        "LastUpdateStatus": "Successful",
        "State": "Active",
        "RevisionId": "current",
        **overrides,
    }


def test_unchanged_lambda_requires_successful_active_matching_code(artifact):
    calls = []

    def call(args):
        calls.append(args)
        return configuration(artifact)

    assert updater.deploy("fixture", artifact, call) == "unchanged"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "initial",
    [{"CodeSha256": "old"}, {"LastUpdateStatus": "Failed"}, {"State": "Inactive"}],
)
def test_lambda_updates_pin_revision_and_verify_the_result(artifact, initial):
    calls = []

    def call(args):
        calls.append(args)
        return (
            configuration(artifact, **initial)
            if len(calls) == 1
            else configuration(artifact)
        )

    assert updater.deploy("fixture", artifact, call) == "updated"
    assert [args[0] for args in calls] == [
        "get-function-configuration",
        "update-function-code",
        "wait",
        "get-function-configuration",
    ]
    assert calls[1][-2:] == ["--revision-id", "current"]


def test_lambda_inflight_update_is_waited_for_before_reusing_code(artifact):
    calls = []

    def call(args):
        calls.append(args)
        return (
            configuration(artifact, LastUpdateStatus="InProgress")
            if len(calls) == 1
            else configuration(artifact)
        )

    assert updater.deploy("fixture", artifact, call) == "unchanged"
    assert [args[0] for args in calls] == [
        "get-function-configuration",
        "wait",
        "get-function-configuration",
    ]


def test_lambda_does_not_accept_wrong_code_after_wait(artifact):
    with pytest.raises(RuntimeError, match="did not converge"):
        updater.deploy(
            "fixture",
            artifact,
            lambda args: configuration(artifact, CodeSha256="wrong"),
        )


def test_missing_lambda_remains_a_noop(artifact):
    assert updater.deploy("fixture", artifact, lambda args: None) == "not provisioned"


def test_authority_deduplication_keeps_each_excluded_suite_in_the_required_coverage_job():
    import re
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/agent-control-ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    authority = next(
        step["run"]
        for step in jobs["agent-authority-tests"]["steps"]
        if step.get("name") == "Run delegated authorization tests"
    )
    excluded = set(re.findall(r"--ignore=(tests/agentauth/[^\s]+)", authority))
    assert excluded
    covered = set()
    for step in jobs["control-contract-tests"]["steps"]:
        command = step.get("run", "")
        if "--cov-branch" in command:
            covered.update(re.findall(r"tests/agentauth/test_[a-z_]+\.py", command))
    assert excluded <= covered
    gate = jobs["agent-control-tests"]
    assert {"control-contract-tests", "agent-authority-tests"} <= set(gate["needs"])
    assert gate["if"] == "always()"
    assert 'test "$CONTRACTS" = success' in gate["steps"][0]["run"]
    assert 'test "$AUTHORITY" = success' in gate["steps"][0]["run"]

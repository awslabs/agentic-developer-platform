"""Platform facade delegates once after in-lock source/account/state checks."""

import contextlib
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("platform_cli", Path(__file__).parents[2] / "cli/adp-platform.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
ACCOUNT = "123456789012"
ACTOR = dict(account_id=ACCOUNT, arn="arn:aws:iam::123456789012:user/operator")


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for relative in (cli.GUIDE, cli.QUICKSTART, cli.DEPLOY, cli.TEARDOWN):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/bash\nexit 0\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"], cwd=root, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, revision


@pytest.fixture
def plan(checkout, tmp_path, monkeypatch):
    root, revision = checkout
    monkeypatch.setattr(cli, "identity", lambda env: dict(ACTOR))
    path = tmp_path / "plan.json"
    args = cli.parser().parse_args(
        [
            "plan",
            "--source-checkout",
            str(root),
            "--source-revision",
            revision,
            "--environment",
            "dev",
            "--profile",
            "test",
            "--region",
            "us-east-1",
            "--output",
            str(path),
        ]
    )
    result = cli.prepare(args)
    assert result["status"] == "dry_run"
    value = json.loads(path.read_text())
    return root, path, value


def apply_args(path, value, *extra):
    return cli.parser().parse_args(
        ["apply", "--plan-file", str(path), "--expect-plan-hash", value["plan_hash"], "--confirm-account", ACCOUNT, *extra]
    )


@pytest.fixture
def deployment_calls(monkeypatch):
    calls = []
    original = cli.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "bash":
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)
        return original(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_pinned_clean_git_source_and_untracked_refusal(checkout):
    root, revision = checkout
    assert cli.source(root, revision)[0] == root
    (root / "rogue.sh").write_text("exit 1")
    with pytest.raises(cli.common.CliError, match="untracked"):
        cli.source(root, revision)


def test_source_mutation_refused(plan, deployment_calls):
    root, path, value = plan
    (root / cli.DEPLOY).write_text("changed")
    with pytest.raises(cli.common.CliError):
        cli.apply(apply_args(path, value))
    assert deployment_calls == []


def test_wrong_hash_or_account_stops_before_mutation(plan, deployment_calls, monkeypatch):
    _, path, value = plan
    args = apply_args(path, value)
    args.expect_plan_hash = "0" * 64
    with pytest.raises(cli.common.CliError):
        cli.apply(args)
    args.expect_plan_hash = value["plan_hash"]
    monkeypatch.setattr(cli, "identity", lambda env: dict(account_id="999999999999", arn="arn:aws:iam::999999999999:user/operator"))
    with pytest.raises(cli.common.CliError):
        cli.apply(args)
    assert deployment_calls == []


def test_apply_is_update_only_and_same_plan_never_replays(plan, deployment_calls):
    root, path, value = plan
    args = apply_args(path, value)
    assert cli.apply(args)["status"] == "pending"
    assert cli.apply(args)["detail"]["outcome"] == "previously_started"
    assert len(deployment_calls) == 1
    argv, options = deployment_calls[0]
    assert argv == ["bash", str(root / cli.DEPLOY), "--update", "--env", "dev", "--region", "us-east-1"]
    assert options["env"]["ADP_ACCOUNT_ID"] == ACCOUNT
    assert "--confirm-destructive" not in argv


def test_state_is_checked_after_waiting_for_lock(plan, deployment_calls, monkeypatch):
    root, path, value = plan

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        (root / ".adp-deploy-state.json").write_text("{}")
        yield

    monkeypatch.setattr(cli.common, "file_lock", lock)
    with pytest.raises(cli.common.CliError, match="state changed"):
        cli.apply(apply_args(path, value))
    assert deployment_calls == []


def test_forged_plan_fields_rejected_even_with_recomputed_hash(plan, deployment_calls):
    _, path, value = plan
    value["scope"] = "arbitrary-shell"
    value.pop("plan_hash")
    value["plan_hash"] = cli.digest(value)
    cli.common.write_json(path, value)
    with pytest.raises(cli.common.CliError):
        cli.apply(apply_args(path, value))
    assert deployment_calls == []


def test_environment_removes_execution_and_target_overrides(monkeypatch):
    for name in (
        "BASH_ENV",
        "PYTHONPATH",
        "TF_VAR_target",
        "TF_CLI_ARGS_apply",
        "ADP_DEPLOY_CONFIG_FILE",
        "ADP_RELEASE_DIR",
        "UPGRADE_RUN_DIR",
        "PUBLISH_SOURCE_ROOT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, "unreviewed")
    env = cli.environment("reviewed", "us-east-1", "dev")
    assert env["AWS_PROFILE"] == "reviewed"
    assert not any(value == "unreviewed" for value in env.values())


def test_status_never_claims_artifact_or_environment_verification(monkeypatch):
    api = Mock()
    api.request.return_value = dict(operations=[dict(id="capabilities.read"), dict(id="agents.activity.read")])
    monkeypatch.setattr(cli.common, "Api", lambda: api)
    monkeypatch.setattr(cli.common, "gateway_url", lambda: "https://selected.test")
    result = cli.status(cli.parser().parse_args(["status", "--environment", "prod"]))
    assert result["detail"]["full_deployment_verified"] is False
    assert result["detail"]["environment_verified"] is False
    assert result["detail"]["artifact_verification"] == "unknown"
    assert api.request.call_args.args[0] == "GET"


def test_source_rechecked_inside_lock(plan, deployment_calls, monkeypatch):
    root, path, value = plan

    @contextlib.contextmanager
    def lock(*args, **kwargs):
        (root / cli.DEPLOY).write_text("changed while waiting")
        yield

    monkeypatch.setattr(cli.common, "file_lock", lock)
    with pytest.raises(cli.common.CliError, match="modified"):
        cli.apply(apply_args(path, value))
    assert deployment_calls == []


def test_foreign_resume_journal_refused(plan, deployment_calls):
    root, path, value = plan
    journal = root / ".adp-deploy-state.json"
    journal.write_text(json.dumps(dict(account_id="999999999999", environment="dev")))
    journal.chmod(0o644)
    value["state_hash"] = cli.journal_hash(root, journal.name)
    value.pop("plan_hash")
    value["plan_hash"] = cli.digest(value)
    cli.common.write_json(path, value)
    args = cli.parser().parse_args(
        ["resume", "--state-file", str(journal), "--plan-file", str(path), "--expect-plan-hash", value["plan_hash"], "--confirm-account", ACCOUNT]
    )
    with pytest.raises(cli.common.CliError, match="another deployment"):
        cli.apply(args)
    assert deployment_calls == []


def test_teardown_keeps_terminal_gate_and_inventory_recheck(checkout, tmp_path, monkeypatch, deployment_calls):
    root, revision = checkout
    monkeypatch.setattr(cli, "identity", lambda env: dict(ACTOR))
    original = cli.command
    inventory = ["gateway + platform; protected state backend"]

    def command(argv, **kwargs):
        if argv[0] == "bash":
            assert "--dry-run" in argv
            return inventory[0]
        return original(argv, **kwargs)

    monkeypatch.setattr(cli, "command", command)
    path = tmp_path / "teardown.json"
    prepared = cli.prepare(
        cli.parser().parse_args(
            [
                "teardown",
                "plan",
                "--source-checkout",
                str(root),
                "--source-revision",
                revision,
                "--environment",
                "dev",
                "--profile",
                "test",
                "--region",
                "us-east-1",
                "--output",
                str(path),
            ]
        )
    )
    value = prepared["detail"]
    args = cli.parser().parse_args(
        ["teardown", "apply", "--plan-file", str(path), "--expect-plan-hash", value["plan_hash"], "--confirm-account", ACCOUNT]
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(cli.common.CliError, match="terminal confirmation"):
        cli.apply(args)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    inventory[0] = "another resource was added"
    with pytest.raises(cli.common.CliError, match="inventory changed"):
        cli.apply(args)
    assert deployment_calls == []
    inventory[0] = value["destruction_inventory"]
    cli.apply(args)
    assert deployment_calls[0][0] == ["bash", str(root / cli.TEARDOWN), "--environment", "dev"]

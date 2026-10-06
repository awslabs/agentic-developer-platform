"""CLI chooses one explicit bounded preparation path; no cloud commands run."""

import json
from types import SimpleNamespace

import pytest

from installation import runtime_cli
from installation.config import Refusal
from installation.runner import atomic

from .test_runtime_approval import save_plan, review_setup

__all__ = ["review_setup"]


@pytest.fixture
def arguments(tmp_path, environment):
    files = {}
    for name, data in (
        ("environment", environment),
        ("release-lock", {}),
        ("runtime-operator", {"review_id": "runtime-demo-review"}),
    ):
        path = tmp_path / (name + ".json")
        atomic(path, data)
        files[name] = path
    return runtime_cli.parser().parse_args(
        [
            "--prepare-domain-runtime",
            "--environment",
            str(files["environment"]),
            "--release-lock",
            str(files["release-lock"]),
            "--runtime-operator",
            str(files["runtime-operator"]),
            "--output",
            str(tmp_path / "output"),
        ]
    )


def test_default_runtime_mode_plans_and_exports_reviewable_manifest(
    arguments, environment, monkeypatch
):
    calls = []

    def prepare(request, env, lock, operator, inspector, commands, directory, **kwargs):
        calls.append((request, kwargs))
        assert inspector.commands is commands
        _, receipt = save_plan(directory, env)
        return receipt

    monkeypatch.setattr(runtime_cli.runtime_preparation, "prepare", prepare)
    result = runtime_cli.run(arguments, commands=object())
    assert result["status"] == "planned" and result["worker_ready"] is False
    assert result["apply_supported"] is True
    assert calls[0][1] == {"approved_plan_digest": None, "approval_check": None}
    assert (
        calls[0][0]["operator_role_arn"]
        == environment["deployment_identity"]["expected_role_arn"]
    )
    assert (
        json.loads((arguments.output / "runtime-plan-manifest.json").read_text())[
            "target"
        ]["cluster"]
        == environment["cluster"]
    )


@pytest.mark.parametrize("missing", ["resume", "digest", "review"])
def test_apply_without_complete_proof_inputs_stops_before_prepare(
    arguments, missing, monkeypatch
):
    arguments.execute, arguments.resume = True, missing != "resume"
    arguments.approved_plan_sha256 = None if missing == "digest" else "4" * 64
    arguments.plan_review = (
        None if missing == "review" else "https://github.com/aws-e/adp/pull/42"
    )
    monkeypatch.setattr(
        runtime_cli.runtime_preparation,
        "prepare",
        lambda *a, **kw: pytest.fail("prepare invoked"),
    )
    with pytest.raises(Refusal):
        runtime_cli.run(arguments)


def test_legacy_receipt_cannot_replan_away_reviewed_binary(
    arguments, environment, monkeypatch
):
    save_plan(arguments.output, environment, binary_bound=False)
    arguments.resume = True
    monkeypatch.setattr(
        runtime_cli.runtime_preparation,
        "prepare",
        lambda *a, **kw: pytest.fail("legacy plan overwritten"),
    )
    with pytest.raises(Refusal, match="preserve the reviewed binary"):
        runtime_cli.run(arguments)


def test_apply_passes_real_live_adapter_to_preparation(
    arguments, review_setup, monkeypatch
):
    adapter, args, state = review_setup
    arguments.output, arguments.execute, arguments.resume = (
        adapter.directory,
        True,
        True,
    )
    arguments.approved_plan_sha256 = args["plan_sha256"]
    arguments.plan_review = "https://github.com/aws-e/adp/pull/42"
    reached = []

    def prepare(*positional, **kwargs):
        result = kwargs["approval_check"].verify_plan(**args)
        reached.append(result["approver"])
        data = json.loads((adapter.directory / "runtime-preparation.json").read_text())
        data["status"] = "applied"
        atomic(adapter.directory / "runtime-preparation.json", data)
        return data

    monkeypatch.setattr(runtime_cli.runtime_preparation, "prepare", prepare)
    result = runtime_cli.run(arguments, commands=object(), approval_api=adapter.api)
    assert reached == ["github:user:2"] and result["status"] == "applied"
    assert state["pull_reads"] == 4


def test_main_installer_routes_runtime_flag_without_general_installer(monkeypatch):
    from installation import __main__ as entry

    monkeypatch.setattr(
        runtime_cli,
        "main",
        lambda args: 17 if args == ["--prepare-domain-runtime"] else 99,
    )
    assert entry.main(["--prepare-domain-runtime"]) == 17


def test_inspector_uses_selected_commands_and_refuses_mutations():
    calls = []
    commands = SimpleNamespace(call=lambda args, **kw: calls.append(args))
    inspector = runtime_cli.RuntimeInspector("us-east-1", commands)
    inspector.aws("sts", "get-caller-identity")
    assert calls[0] == [
        "aws",
        "--region",
        "us-east-1",
        "--no-cli-pager",
        "sts",
        "get-caller-identity",
        "--output",
        "json",
    ]
    with pytest.raises(Refusal):
        inspector.aws("iam", "create-role")
    assert len(calls) == 1

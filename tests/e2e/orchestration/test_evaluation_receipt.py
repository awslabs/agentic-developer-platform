"""Offline shared producer fixtures. Nothing here executes a live scenario."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.orchestration.evaluation_receipt import emit, models, parse_context

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def evidence(tmp_path):
    golden = json.loads((ROOT / "contracts/orchestration-evaluation/v1/evaluation-receipt.golden.json").read_text())
    receipt = golden["receipt"]
    context = {
        key: receipt[key]
        for key in (
            "org_id",
            "flow_id",
            "node_id",
            "execution_id",
            "cycle",
            "accepted_plan_version",
            "policy_hash",
            "claim_id",
            "claim_generation",
            "deployment_operation_key",
            "actual_revision",
        )
    }
    context["specification"] = golden["specification"]
    parsed = parse_context(json.dumps(context))
    for path, payload in golden["artifact_text"].items():
        (tmp_path / path).write_text(payload)
    config = SimpleNamespace(
        artifact_directory=tmp_path,
        connection_ref="connection",
        expected_account_id="123456789012",
        repository="example/repository",
    )
    observations = [
        dict(
            live=True,
            actual_revision=receipt["actual_revision"],
            target=receipt["target"],
            fixtures=receipt["fixtures"],
            criteria=receipt["criteria"],
            artifacts=[dict(path=a["path"], kind=a["kind"]) for a in receipt["artifacts"]],
        )
    ]
    env = dict(
        GITHUB_SHA="c" * 40,
        GITHUB_REPOSITORY="example/repository",
        GITHUB_REPOSITORY_ID="123",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="1",
    )
    return SimpleNamespace(
        context=parsed,
        config=config,
        observations=observations,
        env=env,
        target=SimpleNamespace(verified=True),
        golden=golden,
    )


def produce(ctx):
    return emit(
        ctx.context,
        ctx.observations,
        config=ctx.config,
        target=ctx.target,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC),
        environ=ctx.env,
    )


def test_actual_producer_validates_with_normative_shared_contract(evidence):
    directory = produce(evidence)
    actual = models().EvaluationReceipt.model_validate_json((directory / "evaluation-receipt.json").read_bytes())
    assert actual.model_dump(mode="json") == evidence.golden["receipt"]
    assert (directory / "api.json").read_text() == evidence.golden["artifact_text"]["api.json"]


@pytest.mark.parametrize(
    "failure",
    [
        "empty",
        "not_live",
        "wrong_release",
        "wrong_target",
        "wrong_harness",
        "wrong_repository",
        "unverified",
        "missing_file",
        "symlink",
    ],
)
def test_producer_cannot_invent_or_copy_unverified_evidence(evidence, failure):
    ctx = evidence
    if failure == "empty":
        ctx.observations = []
    elif failure == "not_live":
        ctx.observations[0]["live"] = False
    elif failure == "wrong_release":
        ctx.observations[0]["actual_revision"] = "0" * 40
    elif failure == "wrong_target":
        ctx.observations[0]["target"]["account_id"] = "999999999999"
    elif failure == "wrong_harness":
        ctx.env["GITHUB_SHA"] = "0" * 40
    elif failure == "wrong_repository":
        ctx.env["GITHUB_REPOSITORY_ID"] = "999"
    elif failure == "unverified":
        ctx.target.verified = False
    else:
        path = ctx.config.artifact_directory / "api.json"
        path.unlink()
        if failure == "symlink":
            path.symlink_to(ctx.config.artifact_directory / "data.json")
    with pytest.raises((ValueError, OSError)):
        produce(ctx)
    assert not (ctx.config.artifact_directory / "evaluation").exists()


def test_shared_schema_and_golden_remain_in_sync():
    directory = ROOT / "contracts/orchestration-evaluation/v1"
    assert json.loads((directory / "evaluation-receipt.schema.json").read_text()) == models().EvaluationReceipt.model_json_schema()


def test_default_mode_is_human_and_machine_cannot_omit_criteria():
    assert models().EvaluationSpecification().acceptance_mode == "human"
    with pytest.raises(ValueError):
        models().EvaluationSpecification(acceptance_mode="machine")


def test_runner_emits_the_same_contract_from_executed_adapters(evidence, monkeypatch):
    from tests.e2e.orchestration import run as runner

    ctx = evidence
    ctx.config.environment = "dev"
    ctx.config.max_runs = 1
    ctx.target.to_json = lambda: {"verified": True}
    calls = []

    def execute(**kwargs):
        calls.append(kwargs["inventory"].qualification_id)
        return ctx.observations[0]

    adapter = SimpleNamespace(execute=execute, providers=())
    monkeypatch.setattr(runner, "load_scenario_adapters", lambda config: {"fixture": adapter})
    monkeypatch.setattr(runner, "verified_target", lambda config: ctx.target)
    for key, value in ctx.env.items():
        monkeypatch.setenv(key, value)
    result = runner.run(ctx.config, evaluation_context=ctx.context)
    assert result.status == runner.STATUS_PASS and len(calls) == 1
    parsed = models().EvaluationReceipt.model_validate_json((Path(result.report["evaluation_bundle"]) / "evaluation-receipt.json").read_bytes())
    assert parsed.actual_revision == ctx.context.actual_revision and parsed.criteria[0].criterion_id == "API-1"


def test_runner_refuses_context_target_before_creating_inventory(evidence, monkeypatch):
    from tests.e2e.orchestration import run as runner

    evidence.config.connection_ref = "unapproved"
    for key, value in evidence.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        runner.Inventory,
        "create",
        lambda *a, **k: pytest.fail("No fixture inventory may be created"),
    )
    with pytest.raises(ValueError, match="target_mismatch"):
        runner.run(evidence.config, evaluation_context=evidence.context)

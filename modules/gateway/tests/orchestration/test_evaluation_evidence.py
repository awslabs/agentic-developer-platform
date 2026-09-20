"""Synthetic E1 protocol fixtures; these tests make no live acceptance claim."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.orchestration.deployment_runtime_contract import DeploymentReceipt, RuntimeComponent
from src.orchestration.evaluation_contract import models, specification
from src.orchestration.evaluation_evidence import (
    EvaluationEvidenceError,
    EvaluationExpectation,
    ProviderEvidence,
    specification_hash,
    validate_evaluation_evidence,
)
from src.orchestration.execution_state import ExecutionIdentity

ROOT = Path(__file__).resolve().parents[4]
GOLDEN = ROOT / "contracts/orchestration-evaluation/v1/evaluation-receipt.golden.json"
NOW = datetime(2026, 1, 1, 0, 0, 20, tzinfo=UTC)


@pytest.fixture
def evaluation():
    golden = json.loads(GOLDEN.read_text())
    data, spec = golden["receipt"], golden["specification"]
    observed = NOW - timedelta(seconds=40)
    digest = "sha256:" + "1" * 64
    component = RuntimeComponent(
        component="gateway-backend",
        actual_revision="a" * 40,
        artifact_hash="1" * 64,
        image_digest=digest,
        healthy=True,
        evidence_ref="release-artifact",
        observed_at=observed,
        migration_head="head",
        tick_digest=digest,
        pod_uids=["pod"],
    )
    deployment = DeploymentReceipt(
        org_id="org-a",
        flow_id="flow",
        node_id="story",
        execution_id="story-execution",
        cycle=1,
        accepted_plan_version=1,
        claim_id="story-claim",
        claim_generation=1,
        operation_key="deployment-proof",
        repo="example/repository",
        source_revision="b" * 40,
        actual_revision="a" * 40,
        merge_operation_key="merge-proof",
        workflow_operation_keys=["workflow"],
        manifest_entry_ids=["gateway"],
        targets=[{**spec["target"], "evidence": {"source": "registered-aws-role:connection", "verified_at": observed.isoformat()}}],
        components=[component],
        delivery_complete=True,
        observed_at=observed,
        valid_until=NOW + timedelta(minutes=5),
    )
    identity = ExecutionIdentity(org_id="org-a", node_id="eval", cycle=1, accepted_plan_version=1, claim_id="claim", claim_generation=1)
    expected = EvaluationExpectation(identity, "execution", "flow", "f" * 64, spec, deployment)
    evidence = ProviderEvidence(
        123,
        spec["runner"]["workflow_path"],
        "c" * 40,
        42,
        1,
        "github-actions:123:42:1",
        "github/actions/runs/42/artifacts/88",
        "9" * 64,
        json.dumps(data).encode(),
        {p: b.encode() for p, b in golden["artifact_text"].items()},
        NOW,
    )
    return SimpleNamespace(expected=expected, evidence=evidence, data=data)


def validate(ctx):
    return validate_evaluation_evidence(ctx.expected, replace(ctx.evidence, receipt=json.dumps(ctx.data).encode()), now=NOW)


def test_valid_receipt_binds_actual_containing_release(evaluation):
    result = validate(evaluation)
    assert result.mandatory_passed
    assert result.receipt.actual_revision == evaluation.expected.deployment.actual_revision != evaluation.expected.deployment.source_revision


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "foreign"),
        ("flow_id", "other-flow"),
        ("node_id", "other"),
        ("execution_id", "other"),
        ("cycle", 2),
        ("claim_generation", 2),
        ("claim_id", "other"),
        ("accepted_plan_version", 2),
        ("policy_hash", "0" * 64),
        ("actual_revision", "b" * 40),
        ("harness_revision", "0" * 40),
        ("deployment_operation_key", "other"),
    ],
)
def test_foreign_or_stale_scope_cannot_be_validated(evaluation, field, value):
    evaluation.data[field] = value
    with pytest.raises(EvaluationEvidenceError):
        validate(evaluation)


@pytest.mark.parametrize(
    "failure",
    [
        "producer",
        "artifact",
        "target",
        "expired",
        "missing",
        "skipped",
        "fixture",
        "baseline",
        "api_invariant",
        "not_live",
        "unapproved_suite",
        "stale_provider",
    ],
)
def test_forged_expired_incomplete_or_visual_only_evidence_refuses(evaluation, failure):
    ctx = evaluation
    if failure == "producer":
        ctx.data["producer"]["run_id"] = 99
    elif failure == "artifact":
        ctx.evidence = replace(ctx.evidence, artifacts={**ctx.evidence.artifacts, "api.json": b"tampered"})
    elif failure == "target":
        ctx.data["target"]["account_id"] = "999999999999"
    elif failure == "expired":
        ctx.data["expires_at"] = "2026-01-01T00:00:15Z"
    elif failure == "missing":
        ctx.data["criteria"].pop()
    elif failure == "skipped":
        ctx.data["criteria"][0]["outcome"] = "skipped"
    elif failure == "fixture":
        ctx.data["fixtures"]["row_counts"]["org-b"] = 1
    elif failure == "baseline":
        ctx.data["criteria"][1]["baseline_hash"] = "0" * 64
    elif failure == "api_invariant":
        value = json.loads(ctx.evidence.artifacts["api.json"])
        value["invariant"] = False
        payload = json.dumps(value).encode()
        ctx.evidence = replace(ctx.evidence, artifacts={**ctx.evidence.artifacts, "api.json": payload})
        next(a for a in ctx.data["artifacts"] if a["path"] == "api.json")["sha256"] = hashlib.sha256(payload).hexdigest()
    elif failure == "not_live":
        ctx.data["live"] = False
    elif failure == "unapproved_suite":
        ctx.data["specification_hash"] = "0" * 64
    else:
        ctx.evidence = replace(ctx.evidence, observed_at=NOW - timedelta(minutes=2))
    with pytest.raises(EvaluationEvidenceError):
        validate(ctx)


def test_failed_mandatory_criterion_is_eligible_failure_not_pass(evaluation):
    evaluation.data["criteria"][0]["outcome"] = "fail"
    result = validate(evaluation)
    assert not result.mandatory_passed and result.required_failures == ("API-1",)


def test_optional_criterion_may_be_absent_only_when_accepted_as_optional(evaluation):
    spec = deepcopy(evaluation.expected.specification)
    spec["criteria"].append(dict(criterion_id="optional", kind="control", required=False))
    evaluation.expected = replace(evaluation.expected, specification=spec)
    evaluation.data["specification_hash"] = specification_hash(specification(spec))
    assert validate(evaluation).mandatory_passed


def test_entry_receipt_cannot_establish_complete_deployment(evaluation):
    evaluation.expected = replace(evaluation.expected, deployment=evaluation.expected.deployment.model_copy(update={"delivery_complete": False}))
    with pytest.raises(EvaluationEvidenceError):
        validate(evaluation)


@pytest.mark.parametrize("field", ["schema_version", "cycle", "accepted_plan_version", "claim_generation"])
def test_bool_is_not_a_wire_identity_integer(evaluation, field):
    evaluation.data[field] = True
    with pytest.raises(ValueError):
        models().EvaluationReceipt.model_validate(evaluation.data)


@pytest.mark.parametrize("failure", ["duplicate_criteria", "duplicate_artifact", "missing_ref", "naive_timestamp", "extra_field"])
def test_normative_schema_refuses_ambiguous_shape(evaluation, failure):
    data = evaluation.data
    if failure == "duplicate_criteria":
        data["criteria"].append(data["criteria"][0])
    elif failure == "duplicate_artifact":
        data["artifacts"].append(data["artifacts"][0])
    elif failure == "missing_ref":
        data["criteria"][0]["artifact_paths"] = ["missing.json"]
    elif failure == "naive_timestamp":
        data["started_at"] = "2026-01-01T00:00:00"
    else:
        data["model_verdict"] = "trust me"
    with pytest.raises(ValueError):
        models().EvaluationReceipt.model_validate(data)


@pytest.mark.parametrize("payload", [b"null", b"[]", b"{", b'{"invariant":true,"org_refs":null}', b'{"invariant":"true"}'])
def test_malformed_invariance_is_a_typed_refusal(evaluation, payload):
    from src.orchestration.evaluation_evidence import EvidenceRefusal

    evaluation.evidence = replace(evaluation.evidence, artifacts={**evaluation.evidence.artifacts, "api.json": payload})
    next(a for a in evaluation.data["artifacts"] if a["path"] == "api.json")["sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(EvaluationEvidenceError) as error:
        validate(evaluation)
    assert error.value.reason is EvidenceRefusal.VISUAL_INVARIANCE_MISSING


def test_incomplete_human_spec_refuses_evidence_with_typed_reason(evaluation):
    spec = {**evaluation.expected.specification, "acceptance_mode": "human", "environment_connection_id": None}
    evaluation.expected = replace(evaluation.expected, specification=spec)
    with pytest.raises(EvaluationEvidenceError):
        validate(evaluation)


def test_contract_selfcheck_runs_from_staged_artifact_without_repo(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    stage = tmp_path / "app"
    package = stage / "src/orchestration"
    package.mkdir(parents=True)
    for name in ("evaluation_contract.py", "evaluation_contract_selfcheck.py"):
        shutil.copyfile(ROOT / "modules/gateway/src/orchestration" / name, package / name)
    shutil.copytree(ROOT / "contracts/orchestration-evaluation", stage / "contracts/orchestration-evaluation")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env["PYTHONPATH"] = str(stage)
    command = [sys.executable, "-m", "src.orchestration.evaluation_contract_selfcheck"]
    result = subprocess.run(command, cwd=stage, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    (stage / "contracts/orchestration-evaluation/v1/models.py").unlink()
    result = subprocess.run(command, cwd=stage, env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "evaluation_contract_unavailable" in result.stderr


def test_default_specification_preserves_human_acceptance():
    assert specification({}).acceptance_mode == "human"

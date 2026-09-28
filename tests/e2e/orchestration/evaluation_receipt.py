"""Produce E1 evidence only from executed, trusted scenario adapter observations."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import timedelta
from pathlib import Path


def models():
    name = "_adp_orchestration_evaluation_v1"
    if name in sys.modules:
        return sys.modules[name]
    source = Path(__file__).resolve().parents[3] / "contracts/orchestration-evaluation/v1/models.py"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def parse_context(raw):
    if len(raw.encode()) > 65536:
        raise ValueError("evaluation_context_too_large")
    context = models().EvaluationRunContext.model_validate_json(raw)
    if not context.specification.runner or not context.specification.target or not context.specification.fixtures:
        raise ValueError("evaluation_context_missing_approved_runner")
    return context


def validate_context(context, config, environ=None):
    env = os.environ if environ is None else environ
    spec = context.specification
    if config.connection_ref != spec.environment_connection_id or config.expected_account_id != spec.target.account_id:
        raise ValueError("evaluation_context_target_mismatch")
    if config.repository != spec.runner.repository or env.get("GITHUB_SHA") != spec.runner.harness_revision:
        raise ValueError("evaluation_context_harness_mismatch")
    if env.get("GITHUB_REPOSITORY") != spec.runner.repository or env.get("GITHUB_REPOSITORY_ID") != str(spec.runner.repository_id):
        raise ValueError("evaluation_context_producer_mismatch")


def emit(context, observations, *, config, target, started_at, completed_at, environ=None):
    """Return the bundle path; never infer criterion success from a process exit.

    Adapters return observed revision/target/fixtures/criteria and relative paths
    to their actual evidence files. An empty or non-live observation is refused.
    The input context identifies what was requested; it supplies no result.
    """
    validate_context(context, config, environ)
    env = os.environ if environ is None else environ
    spec = context.specification
    runner = spec.runner
    if not target.verified:
        raise ValueError("evaluation_target_unverified")
    if not observations or any(not isinstance(item, dict) or item.get("live") is not True for item in observations):
        raise ValueError("evaluation_live_observations_missing")
    reference = observations[0]
    for item in observations:
        if item.get("actual_revision") != context.actual_revision or item.get("target") != spec.target.model_dump():
            raise ValueError("evaluation_actual_release_mismatch")
        if item.get("fixtures") != reference.get("fixtures"):
            raise ValueError("evaluation_fixture_observations_disagree")
    fixture = models().FixtureObservation.model_validate(reference["fixtures"])
    if (fixture.fixture_set_id, fixture.definition_hash) != (
        spec.fixtures.fixture_set_id,
        spec.fixtures.definition_hash,
    ):
        raise ValueError("evaluation_fixture_definition_mismatch")
    criteria = [row for item in observations for row in item.get("criteria", [])]
    descriptors = [row for item in observations for row in item.get("artifacts", [])]
    if len(descriptors) > 256:
        raise ValueError("evaluation_artifact_limit")
    base = config.artifact_directory.resolve()
    files, artifacts, total = {}, [], 0
    for descriptor in descriptors:
        path, kind = descriptor["path"], descriptor["kind"]
        # Validate the path and kind before touching the filesystem.
        models().Artifact(path=path, kind=kind, sha256="0" * 64)
        if path == "evaluation-receipt.json" or path in files:
            raise ValueError("evaluation_artifact_collision")
        source = base / path
        if not source.resolve().is_relative_to(base) or source.is_symlink() or any(p.is_symlink() for p in source.parents if p != base):
            raise ValueError("evaluation_artifact_path_invalid")
        if not source.is_file() or not 0 < source.stat().st_size <= 8 * 1024 * 1024:
            raise ValueError("evaluation_artifact_size_invalid")
        payload = source.read_bytes()
        total += len(payload)
        if total > 32 * 1024 * 1024:
            raise ValueError("evaluation_artifact_limit")
        files[path] = payload
        artifacts.append(dict(path=path, kind=kind, sha256=hashlib.sha256(payload).hexdigest()))
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    receipt = models().EvaluationReceipt(
        **context.model_dump(exclude={"specification"}),
        harness_revision=runner.harness_revision,
        specification_hash=hashlib.sha256(json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        target=reference["target"],
        fixtures=fixture,
        producer=dict(
            repository_id=runner.repository_id,
            workflow_path=runner.workflow_path,
            run_id=run_id,
            run_attempt=attempt,
            producer_id=f"github-actions:{runner.repository_id}:{run_id}:{attempt}",
        ),
        criteria=criteria,
        artifacts=artifacts,
        started_at=started_at,
        completed_at=completed_at,
        expires_at=completed_at + timedelta(seconds=spec.max_age_seconds),
        live=True,
    )
    output = base / "evaluation"
    output.mkdir(exist_ok=False)
    for path, payload in files.items():
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    (output / "evaluation-receipt.json").write_text(receipt.model_dump_json(indent=2))
    return output

"""Authenticate and bind evaluation evidence; this module makes no graph decision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .deployment_runtime_contract import DeploymentReceipt
from .evaluation_contract import models, specification
from .execution_state import ExecutionIdentity


class EvidenceRefusal(StrEnum):
    SCHEMA_INVALID = "evaluation_schema_invalid"
    PROVIDER_UNAVAILABLE = "evaluation_provider_unavailable"
    PRODUCER_MISMATCH = "evaluation_producer_mismatch"
    ARTIFACT_INTEGRITY = "evaluation_artifact_integrity"
    SCOPE_CHANGED = "evaluation_scope_changed"
    DEPLOYMENT_CHANGED = "evaluation_deployment_changed"
    SPECIFICATION_CHANGED = "evaluation_specification_changed"
    EXPIRED = "evaluation_evidence_expired"
    CRITERIA_INCOMPLETE = "evaluation_criteria_incomplete"
    FIXTURE_MISMATCH = "evaluation_fixture_mismatch"
    VISUAL_INVARIANCE_MISSING = "evaluation_visual_invariance_missing"
    NOT_LIVE = "evaluation_not_live"


class EvaluationEvidenceError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason.value)


def require(condition, reason):
    if not condition:
        raise EvaluationEvidenceError(reason)


def specification_hash(spec):
    return hashlib.sha256(json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class EvaluationExpectation:
    """Resolved by E2 from protected current plan/execution/deployment state."""

    identity: ExecutionIdentity
    execution_id: str
    flow_id: str
    policy_hash: str
    specification: dict
    deployment: DeploymentReceipt


@dataclass(frozen=True)
class ProviderEvidence:
    """Only the authenticated pull adapter constructs this in production."""

    repository_id: int
    workflow_path: str
    harness_revision: str
    run_id: int
    run_attempt: int
    producer_id: str
    artifact_ref: str
    artifact_hash: str
    receipt: bytes
    artifacts: dict[str, bytes]
    observed_at: datetime


@dataclass(frozen=True)
class ValidatedEvaluation:
    receipt: object
    artifact_ref: str
    artifact_hash: str
    required_failures: tuple[str, ...]

    @property
    def mandatory_passed(self):
        return not self.required_failures


def validate_evaluation_evidence(expected: EvaluationExpectation, evidence: ProviderEvidence, *, now=None):
    now = now or datetime.now(UTC)
    try:
        spec = specification(expected.specification)
        receipt = models().EvaluationReceipt.model_validate_json(evidence.receipt)
    except ValueError:
        raise EvaluationEvidenceError(EvidenceRefusal.SCHEMA_INVALID) from None
    require(
        spec.runner is not None and spec.target is not None and spec.fixtures is not None and spec.environment_connection_id is not None,
        EvidenceRefusal.SPECIFICATION_CHANGED,
    )
    require(receipt.live, EvidenceRefusal.NOT_LIVE)
    require(
        all(getattr(receipt, name) == value for name, value in asdict(expected.identity).items())
        and (receipt.execution_id, receipt.flow_id, receipt.policy_hash) == (expected.execution_id, expected.flow_id, expected.policy_hash),
        EvidenceRefusal.SCOPE_CHANGED,
    )
    require(receipt.specification_hash == specification_hash(spec), EvidenceRefusal.SPECIFICATION_CHANGED)
    require(
        (evidence.repository_id, evidence.workflow_path, evidence.harness_revision)
        == (spec.runner.repository_id, spec.runner.workflow_path, spec.runner.harness_revision)
        and receipt.harness_revision == evidence.harness_revision
        and receipt.producer.model_dump()
        == dict(
            repository_id=evidence.repository_id,
            workflow_path=evidence.workflow_path,
            run_id=evidence.run_id,
            run_attempt=evidence.run_attempt,
            producer_id=evidence.producer_id,
        ),
        EvidenceRefusal.PRODUCER_MISMATCH,
    )
    deployment = expected.deployment
    require(
        deployment.delivery_complete
        and not deployment.docs_only
        and (deployment.org_id, deployment.flow_id, deployment.accepted_plan_version)
        == (expected.identity.org_id, expected.flow_id, expected.identity.accepted_plan_version)
        and (receipt.deployment_operation_key, receipt.actual_revision) == (deployment.operation_key, deployment.actual_revision)
        and receipt.target == spec.target
        and any(
            all(target.get(key) == value for key, value in spec.target.model_dump().items())
            and target.get("evidence", {}).get("source") == "registered-aws-role:" + spec.environment_connection_id
            for target in deployment.targets
        ),
        EvidenceRefusal.DEPLOYMENT_CHANGED,
    )
    require(
        deployment.observed_at <= receipt.started_at <= deployment.valid_until
        and receipt.completed_at - receipt.started_at <= timedelta(seconds=spec.max_duration_seconds)
        and receipt.completed_at <= now + timedelta(seconds=30)
        and receipt.expires_at > now
        and now - receipt.completed_at <= timedelta(seconds=spec.max_age_seconds)
        and receipt.expires_at - receipt.completed_at <= timedelta(seconds=spec.max_age_seconds)
        and now - timedelta(seconds=30) <= evidence.observed_at <= now + timedelta(seconds=30),
        EvidenceRefusal.EXPIRED,
    )
    fixtures = spec.fixtures
    require(
        (receipt.fixtures.fixture_set_id, receipt.fixtures.definition_hash) == (fixtures.fixture_set_id, fixtures.definition_hash)
        and set(receipt.fixtures.roles) == set(fixtures.roles)
        and set(receipt.fixtures.row_counts) == set(fixtures.org_refs)
        and all(count >= fixtures.minimum_rows_per_org for count in receipt.fixtures.row_counts.values()),
        EvidenceRefusal.FIXTURE_MISMATCH,
    )
    artifacts = {artifact.path: artifact for artifact in receipt.artifacts}
    require(set(evidence.artifacts) == set(artifacts), EvidenceRefusal.ARTIFACT_INTEGRITY)
    require(
        all(hashlib.sha256(evidence.artifacts[path]).hexdigest() == item.sha256 for path, item in artifacts.items()),
        EvidenceRefusal.ARTIFACT_INTEGRITY,
    )
    outcomes = {item.criterion_id: item for item in receipt.criteria}
    configured = {item.criterion_id: item for item in spec.criteria}
    require(set(outcomes) <= set(configured), EvidenceRefusal.CRITERIA_INCOMPLETE)
    failures = []
    for criterion in spec.criteria:
        outcome = outcomes.get(criterion.criterion_id)
        if not criterion.required and (outcome is None or outcome.outcome in {"skipped", "not_run"}):
            continue
        require(outcome is not None and outcome.outcome in {"pass", "fail"}, EvidenceRefusal.CRITERIA_INCOMPLETE)
        kinds = {artifacts[path].kind for path in outcome.artifact_paths}
        require(criterion.kind in kinds, EvidenceRefusal.CRITERIA_INCOMPLETE)
        if outcome.outcome == "fail":
            if criterion.required:
                failures.append(criterion.criterion_id)
            continue
        if criterion.kind == "visual":
            require(
                {"visual", "api", "data"} <= kinds and outcome.baseline_hash == criterion.baseline_hash, EvidenceRefusal.VISUAL_INVARIANCE_MISSING
            )
            for kind in ("api", "data"):
                proofs = []
                for path in outcome.artifact_paths:
                    if artifacts[path].kind != kind:
                        continue
                    try:
                        proof = models().InvarianceProof.model_validate_json(evidence.artifacts[path]).model_dump()
                    except ValueError:
                        raise EvaluationEvidenceError(EvidenceRefusal.VISUAL_INVARIANCE_MISSING) from None
                    proofs.append(proof)
                require(
                    any(
                        isinstance(p, dict)
                        and p.get("invariant") is True
                        and set(p.get("org_refs", [])) == set(fixtures.org_refs)
                        and set(p.get("roles", [])) == set(fixtures.roles)
                        and p.get("actual_revision") == receipt.actual_revision
                        for p in proofs
                    ),
                    EvidenceRefusal.VISUAL_INVARIANCE_MISSING,
                )
    return ValidatedEvaluation(receipt, evidence.artifact_ref, evidence.artifact_hash, tuple(failures))


def evidence_summary(validated):
    receipt = validated.receipt
    return dict(
        actual_revision=receipt.actual_revision,
        harness_revision=receipt.harness_revision,
        completed_at=receipt.completed_at.isoformat(),
        expires_at=receipt.expires_at.isoformat(),
        mandatory_passed=validated.mandatory_passed,
        criteria=[dict(criterion_id=c.criterion_id, outcome=c.outcome) for c in receipt.criteria],
    )

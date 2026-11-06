"""Product-owned delivery configuration and workflow verification evidence.

The accepted plan pins this snapshot. Application assertions run in the pinned
workflow; the engine checks identity, completeness and outcomes only.
"""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Reference = Annotated[str, Field(min_length=1, max_length=512)]
TRANSPORT_INPUTS = frozenset({"adp_correlation", "adp_source_revision", "adp_definition_revision", "adp_contract_digest"})
TERMINAL_WORKFLOW_FAILURES = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeliveryContract(StrictModel):
    schema_version: Literal[2]
    connection_id: Identifier
    resource_kind: Identifier = Field(max_length=64)
    resource_id: Reference
    definition_revision: Sha
    baseline_revision: Sha
    workflow_inputs: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    required_checks: list[Identifier] = Field(min_length=1, max_length=64)
    cleanup_required: bool = False
    evidence_artifact: Identifier = "delivery-evidence"
    job_mutation_role_arn: str | None = Field(default=None, pattern=r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")

    @model_serializer(mode="wrap")
    def preserve_existing_contract(self, handler):
        document = handler(self)
        if self.job_mutation_role_arn is None:
            document.pop("job_mutation_role_arn", None)
        return document

    @model_validator(mode="after")
    def bounded_configuration(self):
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("required checks must be unique")
        if TRANSPORT_INPUTS.intersection(self.workflow_inputs):
            raise ValueError("transport inputs are supplied by the engine")
        if any(len(value) > 1024 for value in self.workflow_inputs.values()):
            raise ValueError("workflow inputs must be bounded configuration values, not secrets")
        if any(ord(char) < 32 for char in self.resource_id):
            raise ValueError("target identity cannot contain control characters")
        return self

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def inputs(self):
        return {**self.workflow_inputs, "adp_contract_digest": self.digest}

    def manifest(self, release):
        from .deployment_manifest import DeploymentManifest, EntryStatus, ManifestEntry, WorkflowRef

        return DeploymentManifest(
            1,
            (
                ManifestEntry(
                    entry_id=release.target,
                    status=EntryStatus.ENABLED,
                    connection_id=self.connection_id,
                    component_selectors=(release.component,),
                    workflow=WorkflowRef(
                        path=f".github/workflows/{release.workflows[0]}",
                        definition_revision=self.definition_revision,
                        allowed_inputs={key: frozenset({value}) for key, value in self.inputs.items()},
                        correlation_input="adp_correlation",
                    ),
                    resource_kind=self.resource_kind,
                    resource_id=self.resource_id,
                    artifact_revision=self.baseline_revision,
                    verification_adapter="workflow-evidence-v2",
                ),
            ),
        )


def release_manifest(release, legacy_loader):
    delivery = getattr(release, "delivery_contract", None)
    return delivery.manifest(release) if delivery is not None else legacy_loader()


class CheckResult(StrictModel):
    id: Identifier
    outcome: Literal["passed", "failed", "skipped"]
    evidence_ref: Reference


class WorkflowDeliveryEvidence(StrictModel):
    schema_version: Literal[2]
    contract_digest: Hash
    correlation: Reference
    repository_id: int = Field(gt=0)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    workflow_path: Reference
    workflow_revision: Sha
    source_revision: Sha
    actual_revision: Sha
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Identifier
    resource_kind: Identifier
    resource_id: Reference
    component: Identifier
    checks: list[CheckResult] = Field(min_length=1, max_length=64)
    cleanup: Literal["passed", "failed", "not_required"]
    produced_at: datetime

    @model_validator(mode="after")
    def dated_unique_results(self):
        if self.produced_at.tzinfo is None or len({item.id for item in self.checks}) != len(self.checks):
            raise ValueError("evidence requires a timezone and unique check results")
        return self


class WorkflowRuntimeComponent(StrictModel):
    schema_version: Literal[2]
    component: Identifier
    actual_revision: Sha
    artifact_hash: Hash
    evidence_ref: Reference
    observed_at: datetime
    healthy: Literal[True] = True
    contract_digest: Hash
    checks: list[CheckResult] = Field(min_length=1, max_length=64)
    cleanup: Literal["passed", "not_required"]

    @model_validator(mode="after")
    def verified(self):
        if self.observed_at.tzinfo is None or any(item.outcome != "passed" for item in self.checks):
            raise ValueError("runtime receipt requires dated passing checks")
        return self


def verify_delivery_evidence(artifact, *, contract, context, component, digest, reference):
    from .review_cycle import CycleBlockedError

    fields = (
        "repository_id",
        "run_id",
        "run_attempt",
        "workflow_path",
        "workflow_revision",
        "source_revision",
        "account_id",
        "region",
        "resource_kind",
        "resource_id",
        "correlation",
    )
    if (
        any(getattr(artifact, key) != getattr(context, key) for key in fields)
        or artifact.component != component
        or artifact.contract_digest != contract.digest
        or artifact.actual_revision != context.source_revision
    ):
        raise CycleBlockedError("delivery_evidence_identity_mismatch")
    results = {item.id: item for item in artifact.checks}
    if not set(contract.required_checks) <= results.keys():
        raise CycleBlockedError("delivery_required_checks_missing")
    required = [results[name] for name in contract.required_checks]
    if any(item.outcome != "passed" for item in required):
        raise CycleBlockedError("delivery_checks_failed")
    if artifact.cleanup == "failed" or (contract.cleanup_required and artifact.cleanup != "passed"):
        raise CycleBlockedError("delivery_cleanup_failed")
    return WorkflowRuntimeComponent(
        schema_version=2,
        component=component,
        actual_revision=artifact.actual_revision,
        artifact_hash=digest,
        evidence_ref=reference,
        observed_at=artifact.produced_at,
        contract_digest=contract.digest,
        checks=required,
        cleanup=artifact.cleanup,
    )


def receipt_matches_contract(receipt, release):
    delivery = getattr(release, "delivery_contract", None)
    if delivery is None:
        return receipt.schema_version == 1
    return receipt.schema_version == 2 and all(
        item.contract_digest == delivery.digest
        and set(delivery.required_checks) <= {check.id for check in item.checks if check.outcome == "passed"}
        and (not delivery.cleanup_required or item.cleanup == "passed")
        for item in receipt.components
    )

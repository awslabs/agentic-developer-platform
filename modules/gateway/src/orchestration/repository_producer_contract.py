"""Explicit one-off repository scan authority; never a live deployment claim."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=256)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScannerEvidence(Contract):
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provenance_sha256: Digest


class ImageEvidence(Contract):
    grype: ScannerEvidence
    syft: ScannerEvidence


class ScanTarget(Contract):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$")
    resource_kind: Literal["repository_scan"] = "repository_scan"
    resource_id: Name


class RepositoryProducer(Contract):
    mode: Literal["dispatch_once"]
    workflow_criterion_id: Name
    target: ScanTarget
    inputs: dict[Name, str] = Field(default_factory=dict, max_length=16)
    receipt_artifact: Name
    receipt_path: Name
    images: dict[Name, ImageEvidence] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def transport_is_engine_owned(self):
        if {"adp_correlation", "adp_source_revision", "adp_definition_revision"}.intersection(self.inputs):
            raise ValueError("producer transport inputs are derived from the accepted execution")
        if any(len(value) > 1024 for value in self.inputs.values()):
            raise ValueError("producer inputs exceed the bounded contract")
        if self.inputs.get("expected_account_id") != self.target.account_id or self.inputs.get("region") != self.target.region:
            raise ValueError("producer inputs must explicitly bind the accepted account and region")
        return self


class RepositoryScanReceipt(Contract):
    evidence_schema: Literal["repository-scan-receipt/v2"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    correlation: Digest
    target: ScanTarget
    images: dict[Name, ImageEvidence] = Field(min_length=1, max_length=64)
    coverage_complete: StrictBool
    cleanup_complete: StrictBool
    # Findings and rollout are evaluated by the accepted artifact predicates.
    # Successful scan collection cannot itself mean the security objective is clean.


class ProbeCriterion(Contract):
    criterion_id: Name
    outcome: Literal["pass", "fail", "not_run"]
    evidence_references: list[Name] = Field(min_length=1, max_length=16)
    affected_source_address: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def attribution_only_for_failure(self):
        if self.outcome != "fail" and self.affected_source_address is not None:
            raise ValueError("only a failed criterion may attribute a source")
        return self


class ProbeRuntimeArtifact(Contract):
    component: Name
    actual_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    artifact_hash: Digest
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    tick_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    migration_head: Name | None = None
    asset_count: int = Field(ge=0, le=256)


class ProbeRuntimeReadback(Contract):
    address: str = Field(min_length=1, max_length=512)
    repository_id: int = Field(gt=0)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    components: list[Name] = Field(min_length=1, max_length=32)
    deployment_receipt_key: Name
    targets: list[dict] = Field(min_length=1, max_length=32)
    runtime_artifacts: list[ProbeRuntimeArtifact] = Field(min_length=1, max_length=32)
    serving: StrictBool
    completed: StrictBool = False
    observed_at: datetime
    evidence_reference: Name

    @model_validator(mode="after")
    def current(self):
        if self.observed_at.tzinfo is None or len({artifact.component for artifact in self.runtime_artifacts}) != len(self.runtime_artifacts):
            raise ValueError("probe readback requires aware observation and unique components")
        if (self.components == ["qualification-client"]) != self.completed or self.serving == self.completed:
            raise ValueError("probe readback must distinguish a serving workload from a completed client Job")
        return self


class DeployedProbeResult(Contract):
    evidence_schema: Literal["deployed-probe-result/v1"]
    correlation: Digest
    source_snapshot_hash: Digest
    source_manifest_digest: Digest
    specification_digest: Digest
    target_contract_hash: Digest
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    observed_at: datetime
    criteria: list[ProbeCriterion] = Field(min_length=1, max_length=128)
    runtime_readbacks: list[ProbeRuntimeReadback] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def distinct_criteria(self):
        if self.observed_at.tzinfo is None or len({item.criterion_id for item in self.criteria}) != len(self.criteria):
            raise ValueError("probe observation must be aware and criteria unique")
        return self

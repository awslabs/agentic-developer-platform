"""Explicit one-off repository scan authority; never a live deployment claim."""

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

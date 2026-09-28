"""Bounded runtime evidence: workflow completion never supplies these facts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=512)]
Component = Literal["gateway-backend", "gateway-frontend", "gateway-migrations"]


class StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseArtifact(StrictEvidence):
    """Built artifact hashes published by the pinned workflow, before acceptance."""

    schema_version: Literal[1]
    repository_id: int = Field(gt=0)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    source_revision: Sha
    workflow_revision: Sha
    workflow_path: Name
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    resource_id: Name
    component: Component
    image_digest: Digest | None = None
    assets: dict[Name, Hash] = Field(default_factory=dict, max_length=256)
    produced_at: datetime

    @model_validator(mode="after")
    def complete(self):
        if self.produced_at.tzinfo is None:
            raise ValueError("release evidence must carry an aware timestamp")
        if self.component == "gateway-frontend":
            if self.image_digest is not None or "index.html" not in self.assets:
                raise ValueError("frontend release requires actual build hashes")
            for path in self.assets:
                if path.startswith("/") or ".." in path.split("/") or "\\" in path or any(ord(c) < 32 for c in path):
                    raise ValueError("unsafe build artifact path")
        elif self.image_digest is None or self.assets:
            raise ValueError("backend and migration releases require a container digest")
        return self


class RuntimeComponent(StrictEvidence):
    component: Component
    actual_revision: Sha
    artifact_hash: Hash
    image_digest: Digest | None = None
    healthy: Literal[True]
    evidence_ref: Name
    observed_at: datetime
    # Actual SQL revision, compared with the heads in the verified running image.
    migration_head: Name | None = None
    tick_digest: Digest | None = None
    pod_uids: list[Name] = Field(default_factory=list, max_length=100)
    asset_count: int = Field(default=0, ge=0, le=256)

    @model_validator(mode="after")
    def complete(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("runtime observation timestamp must be aware")
        if self.component == "gateway-frontend":
            if self.asset_count == 0 or self.image_digest is not None:
                raise ValueError("frontend runtime must verify published build bytes")
        elif self.image_digest is None or not self.pod_uids or not self.migration_head:
            raise ValueError("runtime requires serving pods and actual schema evidence")
        if self.component == "gateway-backend" and self.tick_digest != self.image_digest:
            raise ValueError("scheduled tick must match the gateway release digest")
        return self


class DeploymentReceipt(StrictEvidence):
    """D3 runtime acceptance for E1; this does not accept an evaluation node."""

    schema_version: Literal[1] = 1
    org_id: Name
    execution_id: Name
    node_id: Name
    flow_id: Name
    cycle: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    claim_id: Name
    claim_generation: int = Field(ge=1)
    operation_key: Name
    repo: Name
    source_revision: Sha
    actual_revision: Sha
    merge_operation_key: Name
    workflow_operation_keys: list[Name] = Field(max_length=32)
    manifest_entry_ids: list[Name] = Field(max_length=32)
    targets: list[dict] = Field(max_length=32)
    components: list[RuntimeComponent] = Field(max_length=32)
    delivery_complete: bool = False
    docs_only: bool = False
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def complete(self):
        if self.observed_at.tzinfo is None or self.valid_until.tzinfo is None or self.valid_until <= self.observed_at:
            raise ValueError("deployment receipt requires bounded, aware observation validity")
        if self.docs_only:
            if self.components or self.targets or self.workflow_operation_keys or self.actual_revision != self.source_revision:
                raise ValueError("documentation receipt cannot fabricate runtime evidence")
        elif not self.components or not self.targets or not self.workflow_operation_keys:
            raise ValueError("deployment receipt requires runtime component evidence")
        if any(item.actual_revision != self.actual_revision for item in self.components):
            raise ValueError("component revisions must identify the same accepted release")
        return self

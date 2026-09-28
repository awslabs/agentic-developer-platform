"""Normative evaluation specification and receipt v1, shared by runner and gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, max_length=256)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=1)]
CriterionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Kind = Literal["functional", "control", "api", "data", "visual"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Target(Contract):
    provider: Literal["aws"]
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    resource_kind: Literal["eks-namespace"]
    resource_id: Name


class Runner(Contract):
    adapter: Literal["github-orchestration-harness-v1"]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_id: Count
    workflow_path: Literal[".github/workflows/orchestration-live-tests.yml"]
    harness_revision: Sha


class Fixtures(Contract):
    fixture_set_id: Name
    definition_hash: Hash
    org_refs: list[Name] = Field(min_length=1, max_length=8)
    roles: list[Name] = Field(min_length=1, max_length=8)
    minimum_rows_per_org: Count

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.org_refs)) != len(self.org_refs) or len(set(self.roles)) != len(
            self.roles
        ):
            raise ValueError("fixture orgs and roles must be unique")
        return self


class Criterion(Contract):
    criterion_id: CriterionId
    required: StrictBool = True
    kind: Kind
    baseline_hash: Hash | None = None

    @model_validator(mode="after")
    def visual_baseline(self):
        if self.kind == "visual" and self.baseline_hash is None:
            raise ValueError("visual criterion requires an approved baseline hash")
        return self


class EvaluationSpecification(Contract):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    acceptance_mode: Literal["human", "machine"] = "human"
    evidence_schema: Literal["orchestration-evaluation/v1"] = (
        "orchestration-evaluation/v1"
    )
    runner: Runner | None = None
    environment_connection_id: Name | None = None
    target: Target | None = None
    fixtures: Fixtures | None = None
    criteria: list[Criterion] = Field(default_factory=list, max_length=128)
    max_duration_seconds: StrictInt = Field(default=3600, ge=1, le=7200)
    max_age_seconds: StrictInt = Field(default=600, ge=1, le=3600)

    @model_validator(mode="after")
    def complete(self):
        if len({c.criterion_id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion ids must be unique")
        if self.acceptance_mode == "machine" and (
            not self.runner
            or not self.environment_connection_id
            or not self.target
            or not self.fixtures
            or not any(c.required for c in self.criteria)
        ):
            raise ValueError(
                "machine evidence requires approved runner, target, fixtures and mandatory criteria"
            )
        if any(c.kind == "visual" for c in self.criteria) and (
            self.fixtures is None
            or len(self.fixtures.org_refs) < 2
            or len(self.fixtures.roles) < 2
        ):
            raise ValueError(
                "visual evidence requires populated multi-org and multi-role fixtures"
            )
        return self


class Producer(Contract):
    repository_id: Count
    workflow_path: Literal[".github/workflows/orchestration-live-tests.yml"]
    run_id: Count
    run_attempt: Count
    # Resolved from provider metadata, not accepted from a model's declaration.
    producer_id: Name


class Artifact(Contract):
    path: Name
    sha256: Hash
    kind: Kind

    @model_validator(mode="after")
    def safe_path(self):
        if (
            self.path.startswith("/")
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or "\\" in self.path
            or any(ord(c) < 32 for c in self.path)
        ):
            raise ValueError("artifact path must be a safe relative file")
        return self


class CriterionOutcome(Contract):
    criterion_id: CriterionId
    outcome: Literal["pass", "fail", "skipped", "not_run"]
    artifact_paths: list[Name] = Field(default_factory=list, max_length=16)
    baseline_hash: Hash | None = None
    detail: str | None = Field(default=None, max_length=1024)


class FixtureObservation(Contract):
    fixture_set_id: Name
    definition_hash: Hash
    roles: list[Name] = Field(min_length=1, max_length=8)
    row_counts: dict[Name, Count] = Field(min_length=1, max_length=8)


class EvaluationReceipt(Contract):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    org_id: Name
    flow_id: Name
    node_id: Name
    execution_id: Name
    cycle: Count
    accepted_plan_version: Count
    policy_hash: Hash
    claim_id: Name
    claim_generation: Count
    deployment_operation_key: str = Field(min_length=1, max_length=512)
    actual_revision: Sha
    harness_revision: Sha
    specification_hash: Hash
    target: Target
    fixtures: FixtureObservation
    producer: Producer
    criteria: list[CriterionOutcome] = Field(min_length=1, max_length=128)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=256)
    started_at: datetime
    completed_at: datetime
    expires_at: datetime
    live: StrictBool

    @model_validator(mode="after")
    def complete(self):
        if any(
            t.tzinfo is None
            for t in (self.started_at, self.completed_at, self.expires_at)
        ):
            raise ValueError("receipt timestamps must be aware")
        if not self.started_at <= self.completed_at < self.expires_at:
            raise ValueError("receipt time window invalid")
        if len({c.criterion_id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion outcomes must be unique")
        if len({a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact paths must be unique")
        known = {a.path for a in self.artifacts}
        if any(
            len(set(c.artifact_paths)) != len(c.artifact_paths)
            or not set(c.artifact_paths) <= known
            for c in self.criteria
        ):
            raise ValueError("criterion artifact reference missing or duplicated")
        return self


class EvaluationRunContext(Contract):
    org_id: Name
    flow_id: Name
    node_id: Name
    execution_id: Name
    cycle: Count
    accepted_plan_version: Count
    policy_hash: Hash
    claim_id: Name
    claim_generation: Count
    deployment_operation_key: str = Field(min_length=1, max_length=512)
    actual_revision: Sha
    specification: EvaluationSpecification


class InvarianceProof(Contract):
    invariant: StrictBool
    org_refs: list[Name] = Field(min_length=1, max_length=8)
    roles: list[Name] = Field(min_length=1, max_length=8)
    actual_revision: Sha

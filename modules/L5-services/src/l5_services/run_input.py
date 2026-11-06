"""Strict public RunInput validation shared by manual and event admission."""

from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
Revision = Annotated[str, Field(min_length=1, max_length=512)]


class RecordRef(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    tenant_id: Identifier
    project_id: Identifier
    source_connection_id: Identifier
    record_type: Identifier
    record_id: Identifier
    record_revision: Revision

    @field_validator("record_revision")
    @classmethod
    def immutable_revision(cls, value):
        if value in {"latest", "null", "HEAD"}:
            raise ValueError("immutable revision required")
        return value


class ExecutionLimits(BaseModel):
    """Runtime counterpart of the shared v1 Limits contract; no defaults grant budget."""

    model_config = ConfigDict(strict=True, extra="forbid")

    deadline_seconds: Annotated[int, Field(ge=60, le=86400)]
    max_provider_attempts: Annotated[int, Field(ge=1, le=10)]
    max_polls: Annotated[int, Field(ge=1, le=2880)]
    concurrency: Annotated[int, Field(ge=1, le=10)]
    max_cost: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]{1,6})?$")]
    currency: Literal["USD"]
    policy_ref: RecordRef


class ExecutionSettings(BaseModel):
    """Shared settings needed before Security or Well-Architected execution."""

    model_config = ConfigDict(strict=True, extra="forbid")

    classification: Literal["public", "internal", "confidential", "restricted"]
    limits: ExecutionLimits


class RunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["security"] = "security"
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["threat_model", "code_review"]
    sources: Annotated[list[RecordRef], Field(min_length=1, max_length=100)]
    pr_base: Revision | None = None
    pr_head: Revision | None = None
    parent_task_ref: RecordRef | None = None

    @field_validator("pr_base", "pr_head", "parent_task_ref", mode="before")
    @classmethod
    def non_null_reference(cls, value):
        if value is None or isinstance(value, str) and value in {"latest", "null", "HEAD"}:
            raise ValueError("immutable reference required")
        return value

    @model_validator(mode="after")
    def source_types(self):
        expected = "document" if self.capability == "threat_model" else "repository_commit"
        if any(source.record_type != expected for source in self.sources):
            raise ValueError("source type does not match capability")
        if (self.pr_base is None) != (self.pr_head is None):
            raise ValueError("both PR revisions are required")
        if self.capability == "threat_model" and self.pr_base is not None:
            raise ValueError("PR revisions require code_review")
        return self


class WellArchitectedRunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["well_architected"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["architecture_review"]
    sources: Annotated[list[RecordRef], Field(min_length=1, max_length=100)]
    parent_task_ref: RecordRef | None = None

    @field_validator("parent_task_ref", mode="before")
    @classmethod
    def non_null_parent(cls, value):
        if value is None:
            raise ValueError("parent task reference must be pinned")
        return value

    @model_validator(mode="after")
    def source_types(self):
        if any(source.record_type not in {"document", "repository_commit"} for source in self.sources):
            raise ValueError("architecture review requires pinned document or repository commit")
        return self


class WellArchitectedSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    review_mode: Literal["source_only"]
    pillars: Annotated[list[Literal["COST_OPTIMIZATION", "SECURITY", "RESILIENCE",
                                   "PERFORMANCE", "OPERATIONAL_EXCELLENCE"]], Field(min_length=1, max_length=5)]
    goal_refs: Annotated[list[RecordRef], Field(max_length=100)] = Field(default_factory=list)
    execution_role_ref: RecordRef | None = None
    source_store_ref: RecordRef | None = None

    @field_validator("execution_role_ref", "source_store_ref", mode="before")
    @classmethod
    def non_null_provider_reference(cls, value):
        if value is None:
            raise ValueError("provider references must be pinned")
        return value

    @model_validator(mode="after")
    def distinct_pillars(self):
        if len(set(self.pillars)) != len(self.pillars):
            raise ValueError("pillars must be distinct")
        if any(goal.record_type != "document" for goal in self.goal_refs):
            raise ValueError("goals must be pinned document references")
        return self


class NoEffectProof(BaseModel):
    """Worker-owned definitive classification with immutable readback evidence."""

    model_config = ConfigDict(strict=True, extra="forbid")

    classification: Literal["definitive_no_effect"]
    evidence_ref: RecordRef


class InvestigationOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["investigation"]
    resource_refs: Annotated[list[RecordRef], Field(min_length=1, max_length=100)]
    telemetry_refs: Annotated[list[RecordRef], Field(max_length=100)]
    question: Annotated[str, Field(min_length=1, max_length=4000)]


class DevOpsInvestigationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["devops"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["investigation"]
    devops_input: InvestigationOptions

    @model_validator(mode="after")
    def scoped_sources(self):
        references = self.devops_input.resource_refs + self.devops_input.telemetry_refs
        if any((reference.tenant_id, reference.project_id) != (
            references[0].tenant_id, references[0].project_id,
        ) for reference in references):
            raise ValueError("investigation references must share scope")
        return self


class TransformTarget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["dependency", "runtime"]
    name: Identifier
    version: Identifier


class TransformGoal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["refactor", "dependency_upgrade"]
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    target: TransformTarget | None = None

    @model_validator(mode="after")
    def requires_target_for_upgrade(self):
        if (self.kind == "dependency_upgrade") != (self.target is not None):
            raise ValueError("only dependency upgrades require a target")
        return self


class TransformOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    goal: TransformGoal


class TransformRunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["transform"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["refactor", "dependency_upgrade"]
    sources: Annotated[list[RecordRef], Field(min_length=1, max_length=1)]
    transform_input: TransformOptions
    parent_task_ref: RecordRef | None = None

    @model_validator(mode="after")
    def immutable_repository_goal(self):
        if self.sources[0].record_type != "repository_commit":
            raise ValueError("transform requires a repository commit")
        if self.capability != self.transform_input.goal.kind:
            raise ValueError("goal must match capability")
        return self


class ArtifactRef(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    artifact_id: Identifier
    version: Revision
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_type: Identifier
    bytes: Annotated[int, Field(ge=0)]


class HistoricalVerificationOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["release_verification"]
    source_ref: RecordRef
    deployment_ref: RecordRef
    target_ref: RecordRef
    credential_ref: RecordRef
    scenarios: Annotated[list[ArtifactRef], Field(min_length=1, max_length=20)]


class TestProfileRef(RecordRef):
    record_type: Literal["test_profile"]
    record_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")]
    record_revision: Annotated[str, Field(max_length=10, pattern=r"^[1-9][0-9]*$")]


class VerificationOptions(HistoricalVerificationOptions):
    test_profile_ref: TestProfileRef


class HistoricalDevOpsVerificationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["devops"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["release_verification"]
    devops_input: HistoricalVerificationOptions

    @model_validator(mode="after")
    def scoped_references(self):
        references = (self.devops_input.source_ref, self.devops_input.deployment_ref,
                      self.devops_input.target_ref, self.devops_input.credential_ref)
        if any((reference.tenant_id, reference.project_id) != (
            references[0].tenant_id, references[0].project_id,
        ) for reference in references):
            raise ValueError("verification references must share scope")
        if len({scenario.artifact_id for scenario in self.devops_input.scenarios}) != len(self.devops_input.scenarios):
            raise ValueError("verification scenarios must be distinct")
        return self


class DevOpsVerificationInput(HistoricalDevOpsVerificationInput):
    devops_input: VerificationOptions

    @model_validator(mode="after")
    def scoped_profile(self):
        profile, source = self.devops_input.test_profile_ref, self.devops_input.source_ref
        if (profile.tenant_id, profile.project_id) != (source.tenant_id, source.project_id):
            raise ValueError("test profile must share verification scope")
        return self


class ReleaseReadinessOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["release_readiness"]
    repository_ref: RecordRef
    base_revision: Revision
    head_revision: Revision
    eligibility_ref: RecordRef | None = None

    @field_validator("base_revision", "head_revision")
    @classmethod
    def immutable_release_revision(cls, value):
        if value.lower() in {"latest", "null", "head"}:
            raise ValueError("immutable revision required")
        return value

    @model_validator(mode="after")
    def exact_change(self):
        if (self.repository_ref.record_type != "repository_commit"
                or self.repository_ref.record_revision != self.head_revision
                or self.base_revision == self.head_revision
                or self.eligibility_ref is not None and self.eligibility_ref.record_type != "native_eligibility"
                or self.eligibility_ref is not None and (self.repository_ref.tenant_id, self.repository_ref.project_id)
                != (self.eligibility_ref.tenant_id, self.eligibility_ref.project_id)):
            raise ValueError("release review requires distinct, scoped, immutable revisions")
        return self


class DevOpsReleaseReadinessInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["devops"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    capability: Literal["release_readiness"]
    devops_input: ReleaseReadinessOptions


class FinopsCostQuery(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    start: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    end: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    metric: Literal["UnblendedCost"]
    granularity: Literal["DAILY", "MONTHLY"]
    group_by: Annotated[list[Literal["SERVICE", "LINKED_ACCOUNT"]], Field(max_length=2)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

    @model_validator(mode="after")
    def fixed_period(self, info: ValidationInfo):
        try:
            start, end = date.fromisoformat(self.start), date.fromisoformat(self.end)
        except ValueError:
            raise ValueError("valid dates required") from None
        if start >= end:
            raise ValueError("end must be after start")
        today = info.context.get("today") if isinstance(info.context, dict) else None
        if end > (today if today is not None else datetime.now(UTC).date()):
            raise ValueError("period must end no later than today UTC")
        if len(self.group_by) != len(set(self.group_by)):
            raise ValueError("groupings must be distinct")
        return self


class FinopsRunInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["finops"]
    capability: Literal["cost_inquiry"]
    configuration_revision: Annotated[int, Field(ge=1)]
    binding_id: Identifier
    question: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    query: FinopsCostQuery
    sources: Annotated[list[RecordRef], Field(max_length=100)] = Field(default_factory=list)
    parent_task_ref: RecordRef | None = None

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if isinstance(value, dict) and any(
            key in value and value[key] is None for key in ("parent_task_ref", "sources")
        ):
            raise ValueError("null references are not allowed")
        return value

    @model_validator(mode="after")
    def scoped_sources(self):
        references = self.sources + ([self.parent_task_ref] if self.parent_task_ref else [])
        if references and any((reference.tenant_id, reference.project_id) != (
            references[0].tenant_id, references[0].project_id,
        ) for reference in references):
            raise ValueError("references must share scope")
        if len({reference.model_dump_json() for reference in self.sources}) != len(self.sources):
            raise ValueError("sources must be distinct")
        return self


def validate_run_input(value, *, today=None, allow_historical=False):
    try:
        if not isinstance(value, dict):
            raise ValueError("run input must be an object")
        branches = {"security": RunInput, "devops": DevOpsInvestigationInput,
                    "transform": TransformRunInput, "finops": FinopsRunInput,
                    "well_architected": WellArchitectedRunInput}
        service_id = value.get("service_id", "security")
        if not isinstance(service_id, str):
            raise ValueError("unsupported service")
        branch = branches.get(service_id)
        if branch is None:
            raise ValueError("unsupported service")
        if service_id == "devops" and value.get("capability") == "release_readiness":
            branch = DevOpsReleaseReadinessInput
        elif service_id == "devops" and value.get("capability") == "release_verification":
            branch = (HistoricalDevOpsVerificationInput if allow_historical
                      and "test_profile_ref" not in (value.get("devops_input") or {})
                      else DevOpsVerificationInput)
        branch.model_validate(value, context={"today": today} if today is not None else None)
    except (ValidationError, ValueError):
        raise HTTPException(422, detail={"code": "validation_error"}) from None

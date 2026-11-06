"""Runtime models for the reviewed ProjectConfiguration document."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S(?:.*\S)?$")]
NativeIdentifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Text = Annotated[str, Field(min_length=1, max_length=10000, pattern=r"\S")]


def check_calendar_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def check_unique_items(values: list) -> list:
    if any(value in values[:index] for index, value in enumerate(values)):
        raise ValueError("items must be unique")
    return values


CalendarDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(check_calendar_date)]


def manually_confirmed_paths(provenance: list) -> set[str]:
    return {
        path
        for proof in provenance
        if proof.get("state") == "accepted" and (proof.get("method") == "manual" or proof.get("confirmed_by"))
        for path in proof.get("paths", [])
    }


class ConfigurationObject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrincipalRef(ConfigurationObject):
    kind: Literal["user", "team", "service"]
    id: Identifier


class RecordRef(ConfigurationObject):
    tenant_id: Identifier
    project_id: Identifier
    source_connection_id: NativeIdentifier
    record_type: Name
    record_id: NativeIdentifier
    record_revision: str = Field(min_length=1, max_length=512)

    @field_validator("record_revision")
    @classmethod
    def check_record_revision(cls, value: str) -> str:
        if value in {"latest", "null"}:
            raise ValueError("record_revision must identify a specific revision")
        return value


RefList = Annotated[list[RecordRef], Field(max_length=100), AfterValidator(check_unique_items)]


class UserMapping(ConfigurationObject):
    provider: Name
    provider_user_id: NativeIdentifier
    user_id: Identifier


class Basic(ConfigurationObject):
    repositories: RefList = None
    trackers: RefList = None
    documents: RefList = None
    roadmap: RefList = None
    architecture: RefList = None
    collaboration: RefList = None
    decisions: RefList = None
    user_mappings: list[UserMapping] = Field(default=None, max_length=100)
    environments: RefList = None


class EnvironmentConflict(ConfigurationObject):
    legacy_ref: RecordRef
    advanced_ref: RecordRef


class Advanced(ConfigurationObject):
    environments: RefList = None
    environment_conflicts: list[EnvironmentConflict] = Field(default=None, max_length=100)
    deployments: RefList = None
    pipelines: RefList = None
    support: RefList = None
    observability: RefList = None
    runbooks: RefList = None
    test_strategy: RefList = None
    security_strategy: RefList = None
    deployment_strategy: RefList = None
    release_strategy: RefList = None
    agent_policy_ref: RecordRef = None
    skills: RefList = None


class Money(ConfigurationObject):
    amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,13})(\.[0-9]{1,6})?$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Allocation(ConfigurationObject):
    id: Identifier
    name: Name
    target_ref: RecordRef
    amount: Money
    category: Literal["ai", "cloud", "other"]
    period_start: CalendarDate
    period_end: CalendarDate
    approval_status: Literal["proposed"]
    owner_ref: PrincipalRef
    parent_allocation_ref: RecordRef = None
    enforcement_policy_ref: RecordRef = None

    @model_validator(mode="after")
    def check_period(self):
        if self.period_start > self.period_end:
            raise ValueError("period_start must not be after period_end")
        return self


class Financials(ConfigurationObject):
    business_cases: RefList = None
    initiatives: RefList = None
    allocations: list[Allocation] = Field(default=None, max_length=100)
    release_target_date: CalendarDate = None
    budget_policy_ref: RecordRef = None


class Applicability(ConfigurationObject):
    check_id: Name
    state: Literal["applicable", "not_applicable", "later"]
    reason: Text = None

    @model_validator(mode="after")
    def require_exclusion_reason(self):
        if self.state == "not_applicable" and not self.reason:
            raise ValueError("Not applicable requires a reason")
        return self


class CapabilitySelection(ConfigurationObject):
    capability: Name
    ownership_mode: Literal["managed", "integrated", "observed", "not_applicable"]
    configuration_ref: RecordRef = None
    reason: Text = None


class Governance(ConfigurationObject):
    criticality: Literal["low", "medium", "high", "critical"] = None
    data_classification: Literal["public", "internal", "confidential", "restricted"] = None
    cost_center: Name = None
    support_channel_ref: RecordRef = None
    access_review_date: CalendarDate = None
    execution_policy_ref: RecordRef = None
    retention_policy_ref: RecordRef = None


def normalize_environments(configuration: dict) -> dict:
    """Move compatible legacy references without discarding conflicting source revisions."""
    basic = configuration.get("basic")
    if not isinstance(basic, dict) or not isinstance(basic.get("environments"), list):
        advanced = configuration.get("advanced")
        if isinstance(advanced, dict) and "environment_conflicts" in advanced:
            advanced = dict(advanced)
            advanced.pop("environment_conflicts")
            return {**configuration, "advanced": advanced}
        return configuration
    advanced = configuration.get("advanced")
    if advanced is not None and not isinstance(advanced, dict):
        return configuration
    advanced = dict(advanced or {})
    references = list(advanced.get("environments") or [])
    remaining = []
    conflicts = []
    identity_fields = ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id")
    for legacy in basic["environments"]:
        if legacy in references:
            continue
        if not isinstance(legacy, dict):
            remaining.append(legacy)
            continue
        matching = [
            reference
            for reference in references
            if isinstance(reference, dict) and all(reference.get(field) == legacy.get(field) for field in identity_fields)
        ]
        if matching:
            remaining.append(legacy)
            conflicts.extend({"legacy_ref": legacy, "advanced_ref": reference} for reference in matching)
        else:
            references.append(legacy)
    advanced["environments"] = references
    if conflicts:
        advanced["environment_conflicts"] = conflicts
    else:
        advanced.pop("environment_conflicts", None)
    basic = dict(basic)
    if remaining:
        basic["environments"] = remaining
    else:
        basic.pop("environments", None)
    result = {**configuration, "advanced": advanced}
    if basic:
        result["basic"] = basic
    else:
        result.pop("basic", None)
    return result


class ProjectConfiguration(ConfigurationObject):
    basic: Basic = None
    advanced: Advanced = None
    financials: Financials = None
    selected_capabilities: Annotated[list[CapabilitySelection], Field(max_length=50), AfterValidator(check_unique_items)] = None
    governance: Governance = None

    applicability: list[Applicability] = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_environments(cls, value):
        if isinstance(value, dict):
            return normalize_environments(value)
        return value

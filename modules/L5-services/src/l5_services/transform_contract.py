"""Strict Transform payloads; validation is not execution authorization."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from l5_services.run_input import Identifier, RecordRef, TransformGoal


class TransformSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_ref: RecordRef
    executor_binding_ref: RecordRef
    test_policy_ref: RecordRef
    definition_ref: RecordRef | None = None
    region: Identifier
    read_paths: Annotated[list[str], Field(min_length=1, max_length=100)]
    write_paths: Annotated[list[str], Field(min_length=1, max_length=100)]
    excluded_paths: Annotated[list[str], Field(max_length=100)]
    goal_defaults: TransformGoal

    @field_validator("read_paths", "write_paths", "excluded_paths")
    @classmethod
    def relative_paths(cls, paths):
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("duplicate or case-colliding path")
        for path in paths:
            if (not path or path.startswith("/") or "\\" in path or "\x00" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or PurePosixPath(path).is_absolute()):
                raise ValueError("paths must be repository-relative")
        return paths


class TransformConfigurationInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["transform"]
    profile_ref: RecordRef
    source_refs: Annotated[list[RecordRef], Field(min_length=1, max_length=100)]
    capabilities: Annotated[list[Literal["refactor", "dependency_upgrade"]], Field(min_length=1, max_length=2)]
    enabled: bool
    transform: TransformSettings

    @model_validator(mode="after")
    def scoped_repository(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be distinct")
        allowed = ({"repository", "repository_commit"}
                   if isinstance(self, TransformConfigurationDraftInput) and not self.enabled
                   else {"repository_commit"})
        if any(source.record_type not in allowed for source in self.source_refs):
            raise ValueError("transform requires immutable repository sources")
        if self.transform.goal_defaults.kind not in self.capabilities:
            raise ValueError("default goal must be configured")
        return self


class TransformDraftSettings(TransformSettings):
    executor_binding_ref: RecordRef | None = None
    test_policy_ref: RecordRef | None = None


class TransformConfigurationDraftInput(TransformConfigurationInput):
    transform: TransformDraftSettings


class TransformCheck(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: Identifier
    status: Literal["passed", "failed", "unknown"]
    evidence_ref: RecordRef | None = None


class TransformResultDetail(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    service_id: Literal["transform"]
    original_patch_ref: RecordRef | None = None
    baseline_checks: list[TransformCheck]
    candidate_checks: list[TransformCheck]
    source_ref: RecordRef
    limitations: list[str]
    provider_state: Literal["not_started", "running", "stopped", "completed", "failed", "unknown"]
    cost_status: Literal["observed", "unknown"]

    @model_validator(mode="after")
    def verified_patch(self):
        if self.source_ref.record_type != "repository_commit":
            raise ValueError("result source must be a repository commit")
        if self.original_patch_ref is not None and self.provider_state != "completed":
            raise ValueError("patch requires completed provider observation")
        return self

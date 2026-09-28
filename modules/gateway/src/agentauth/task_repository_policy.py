"""Task repository selection from administrator-owned standing service policy.

Task inputs select an alias, never supply a provider URL or credentials. Provider
adapters must still verify connection ownership and immutable repository identity
before fetching or publishing. This module grants neither a token nor merge.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskRepositoryPolicyError(ValueError):
    pass


class TaskValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    image: str = Field(
        max_length=512,
        pattern=(
            r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?/"
            r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*@)?sha256:[a-f0-9]{64}$"
        ),
    )
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    memory_mb: int = Field(default=512, ge=64, le=8192)
    cpus: int = Field(default=1, ge=1, le=8)
    max_output_bytes: int = Field(default=16384, ge=1, le=16384)

    @field_validator("argv")
    @classmethod
    def arguments(cls, value):
        if any(not arg or "\x00" in arg or len(arg) > 4096 for arg in value):
            raise ValueError("invalid validation arguments")
        return value


class TaskRepositoryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["github", "gitlab"]
    connection_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    repository_id: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*$")
    repository: str = Field(min_length=3, max_length=512)
    base_branch: str = Field(min_length=1, max_length=255)
    validation_checks: list[TaskValidationCheck] = Field(default_factory=list, max_length=32)

    acceptance_checks: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("acceptance_checks")
    @classmethod
    def acceptance(cls, value):
        if any(not re.fullmatch(r"[a-f0-9]{64}", key) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) for key, name in value.items()):
            raise ValueError("invalid acceptance check mapping")
        return value

    @field_validator("validation_checks")
    @classmethod
    def checks(cls, value):
        if len({check.name for check in value}) != len(value):
            raise ValueError("duplicate validation check name")
        if len(json.dumps([check.model_dump() for check in value]).encode()) > 16384:
            raise ValueError("validation configuration exceeds bound")
        return value

    @field_validator("repository")
    @classmethod
    def repository_path(cls, value):
        parts = value.split("/")
        if len(parts) < 2 or any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", part) or part in {".", ".."} for part in parts):
            raise ValueError("invalid provider repository path")
        return value

    @field_validator("base_branch")
    @classmethod
    def branch(cls, value):
        if (
            value.startswith(("-", "/"))
            or value.endswith(("/", "."))
            or ".." in value
            or "@{" in value
            or value == "@"
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        ):
            raise ValueError("invalid base branch")
        return value


def repositories(policy):
    raw = policy.get("repositories", {})
    if not isinstance(raw, dict) or len(raw) > 32:
        raise TaskRepositoryPolicyError("invalid repository policy")
    result = {}
    for alias, value in raw.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", alias):
            raise TaskRepositoryPolicyError("invalid repository alias")
        # Preserve existing bindings without checks, including their grant digest.
        binding = TaskRepositoryBinding.model_validate(value)
        excluded = {name for name in ("validation_checks", "acceptance_checks") if not getattr(binding, name)}
        result[alias] = binding.model_dump(exclude=excluded)
    return result


def freeze_repository(inputs, policy):
    selector = inputs.get("repository_binding")
    if selector is None:
        return None
    if not isinstance(selector, str):
        raise TaskRepositoryPolicyError("repository selection must name a policy alias")
    allowed = repositories(policy)
    if selector not in allowed:
        raise TaskRepositoryPolicyError("repository is not authorized")
    return {"alias": selector, "binding": allowed[selector]}


def validate_frozen_repository(value):
    if not isinstance(value, dict) or set(value) != {"alias", "binding"}:
        raise TaskRepositoryPolicyError("invalid frozen repository")
    repositories({"repositories": {value["alias"]: value["binding"]}})
    return value


def require_current_repository(frozen, policy):
    validate_frozen_repository(frozen)
    if repositories(policy).get(frozen["alias"]) != frozen["binding"]:
        raise TaskRepositoryPolicyError("repository policy was revoked or changed")

"""Task repository selection from administrator-owned standing service policy.

Task inputs select an alias, never supply a provider URL or credentials. Provider
adapters must still verify connection ownership and immutable repository identity
before fetching or publishing. This module grants neither a token nor merge.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskRepositoryPolicyError(ValueError):
    pass


class TaskRepositoryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Literal["github", "gitlab"]
    connection_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    repository_id: str = Field(min_length=1, max_length=32, pattern=r"^[1-9][0-9]*$")
    repository: str = Field(min_length=3, max_length=512)
    base_branch: str = Field(min_length=1, max_length=255)

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
        result[alias] = TaskRepositoryBinding.model_validate(value).model_dump()
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

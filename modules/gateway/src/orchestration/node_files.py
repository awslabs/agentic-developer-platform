"""Bounded, informational file plans for story cards; never execution authority."""

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def repository_file_path(value: str) -> str:
    if (
        value != value.strip()
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or re.search(r"[\\:*?<>\x00-\x1f\x7f]", value)
    ):
        raise ValueError("file paths must be literal repository-relative paths")
    return value


RepositoryFilePath = Annotated[str, Field(min_length=1, max_length=512), AfterValidator(repository_file_path)]


class PredecessorFileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: RepositoryFilePath
    created_by: str = Field(min_length=1, max_length=128, pattern=r".*\S.*")


class NodeFileGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group: str = Field(min_length=1, max_length=120, pattern=r".*\S.*")
    purpose: str | None = Field(default=None, max_length=500)
    create: list[RepositoryFilePath] = Field(default_factory=list, max_length=128)
    modify: list[RepositoryFilePath] = Field(default_factory=list, max_length=128)
    modify_after: list[PredecessorFileEdit] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.create and not self.modify and not self.modify_after:
            raise ValueError("file groups must name at least one file")
        return self


def unique_file_groups(groups: list[NodeFileGroup]) -> list[NodeFileGroup]:
    names = [group.group for group in groups]
    paths = [path for group in groups for path in [*group.create, *group.modify, *(edit.path for edit in group.modify_after)]]
    if len(set(names)) != len(names) or len(set(paths)) != len(paths):
        raise ValueError("file group names and file paths must be unique within a node")
    if len(paths) > 256:
        raise ValueError("a node file plan may contain at most 256 files")
    return groups


NodeFileGroups = Annotated[list[NodeFileGroup], Field(max_length=16), AfterValidator(unique_file_groups)]
_FILE_GROUPS = TypeAdapter(NodeFileGroups)


def project_file_groups(document: dict) -> dict[str, list[NodeFileGroup]]:
    """Read only validated display fields, omitting malformed/ambiguous entries."""
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        return {}
    result = {}
    seen = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") != "story" or not isinstance(node.get("address"), str):
            continue
        address = node["address"]
        if address in seen:
            result.pop(address, None)
            continue
        seen.add(address)
        try:
            groups = _FILE_GROUPS.validate_python(node.get("file_groups", []))
        except ValueError:
            continue
        if groups:
            result[address] = groups
    return result

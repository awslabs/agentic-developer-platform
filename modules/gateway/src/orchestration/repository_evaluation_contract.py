"""Explicit, bounded repository evidence; no deployment or live verdict is implied."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, model_validator

Name = Annotated[str, Field(min_length=1, max_length=256)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(gt=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Runner(Contract):
    adapter: Literal["engine-repository-evidence-v1"]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_id: Count
    harness_sha256: Digest


class Check(Contract):
    name: Name
    app_id: Count


class Predecessor(Contract):
    address: Name
    required_checks: list[Check] = Field(min_length=1, max_length=64)


class ExternalPullRequest(Contract):
    criterion_id: Name
    issue_number: Count
    pr_number: Count
    head_sha: Sha
    merge_sha: Sha
    required_checks: list[Check] = Field(min_length=1, max_length=64)


class Revision(Contract):
    revision: Sha | None = None
    predecessor: Name | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (self.revision is None) == (self.predecessor is None):
            raise ValueError("revision must name an immutable commit or one accepted predecessor")
        return self


class Predicate(Contract):
    criterion_id: Name
    pointer: str = Field(max_length=1024)
    operation: Literal["equals", "set_equals", "records"]
    expected: JsonValue
    id_field: Name | None = None
    required_fields: dict[Name, list[JsonValue]] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def shape(self):
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("JSON pointer must be absolute")
        if len(json.dumps(self.expected)) > 128 * 1024:
            raise ValueError("predicate exceeds evidence bound")
        if self.operation in {"set_equals", "records"}:
            if not isinstance(self.expected, list) or not self.expected or len(self.expected) > 2048:
                raise ValueError("set/records predicates require bounded expected identifiers")
            if len({canonical(value) for value in self.expected}) != len(self.expected):
                raise ValueError("expected identifiers must be unique")
        if (self.operation == "records") != (self.id_field is not None):
            raise ValueError("records predicate requires an identifier field")
        if self.operation != "records" and self.required_fields:
            raise ValueError("record field predicates require records mode")
        if any(not values for values in self.required_fields.values()):
            raise ValueError("record field allowlists cannot be empty")
        return self


class Artifact(Contract):
    name: Name
    path: Name
    predicates: list[Predicate] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def safe_path(self):
        if self.path.startswith("/") or "\\" in self.path or any(part in {"", ".", ".."} for part in self.path.split("/")):
            raise ValueError("artifact path must be relative")
        return self


class Workflow(Contract):
    criterion_id: Name
    path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
    source: Revision
    definition: Revision
    dispatch_only: bool = True
    required_jobs: list[Name] = Field(min_length=1, max_length=64)
    artifacts: list[Artifact] = Field(min_length=1, max_length=16)


class RepositoryEvaluationSpecification(Contract):
    schema_version: Literal[1] = 1
    acceptance_mode: Literal["machine"] = "machine"
    evidence_schema: Literal["repository-evaluation/v1"]
    runner: Runner
    predecessors: list[Predecessor] = Field(default_factory=list, max_length=128)
    external_pull_requests: list[ExternalPullRequest] = Field(default_factory=list, max_length=32)
    workflows: list[Workflow] = Field(default_factory=list, max_length=16)
    max_age_seconds: StrictInt = Field(default=86400, ge=60, le=604800)

    @model_validator(mode="after")
    def complete(self):
        if not self.predecessors and not self.external_pull_requests and not self.workflows:
            raise ValueError("evaluation must declare real evidence")
        addresses = [item.address for item in self.predecessors]
        ids = addresses + [item.criterion_id for item in self.external_pull_requests]
        for workflow in self.workflows:
            ids.append(workflow.criterion_id)
            ids.extend(predicate.criterion_id for artifact in workflow.artifacts for predicate in artifact.predicates)
            for source in (workflow.source, workflow.definition):
                if source.predecessor and source.predecessor not in addresses:
                    raise ValueError("workflow revision requires a declared direct predecessor")
        if len(set(ids)) != len(ids):
            raise ValueError("criterion identities must be unique")
        return self


class CheckReceipt(Contract):
    name: Name
    app_id: Count
    check_run_id: Count
    head_sha: Sha


class PullReceipt(Contract):
    pr_number: Count
    head_sha: Sha
    merge_sha: Sha
    provider_pr_node_id: Name
    merged_at: datetime
    checks: list[CheckReceipt] = Field(min_length=1, max_length=64)
    criterion_id: Name | None = None
    issue_number: Count | None = None
    address: Name | None = None
    node_id: Name | None = None
    attempt: Count | None = None
    accepted_plan_version: Count | None = None
    execution_id: Name | None = None
    binding_id: Name | None = None
    binding_revision: Count | None = None
    review_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    merge_operation_key: Name | None = None


class JobReceipt(Contract):
    name: Name
    job_id: Count


class ArtifactReceipt(Contract):
    artifact_id: Count
    name: Name
    digest: Digest
    path: Name
    sha256: Digest


class CriterionReceipt(Contract):
    criterion_id: Name
    passed: StrictBool


class WorkflowReceipt(Contract):
    criterion_id: Name
    workflow_path: Name
    source_revision: Sha
    definition_revision: Sha
    workflow_blob_sha: Sha
    run_id: Count
    run_attempt: Count
    event: Literal["workflow_dispatch"]
    jobs: list[JobReceipt] = Field(min_length=1, max_length=64)
    artifacts: list[ArtifactReceipt] = Field(min_length=1, max_length=16)
    criteria: list[CriterionReceipt] = Field(min_length=1, max_length=2048)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def harness_digest():
    """Pin the actual deterministic verifier shipped in the gateway artifact."""
    digest = hashlib.sha256()
    for name in (
        "repository_evaluation_contract.py",
        "repository_evaluation_provider.py",
        "repository_evaluation.py",
        "evaluation_acceptance.py",
        "merge_evidence.py",
    ):
        data = Path(__file__).with_name(name).read_bytes()
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def predicate_passes(predicate, document):
    value = document
    try:
        for part in predicate.pointer.split("/")[1:] if predicate.pointer else []:
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        if predicate.operation == "equals":
            return canonical(value) == canonical(predicate.expected)
        if not isinstance(value, list):
            return False
        observed = value
        if predicate.operation == "records":
            if any(not isinstance(row, dict) or predicate.id_field not in row for row in value):
                return False
            observed = [row[predicate.id_field] for row in value]
            if any(
                field not in row or canonical(row[field]) not in {canonical(item) for item in allowed}
                for row in value
                for field, allowed in predicate.required_fields.items()
            ):
                return False
        return len(observed) == len(predicate.expected) and {canonical(item) for item in observed} == {canonical(item) for item in predicate.expected}
    except (ValueError, TypeError, KeyError, IndexError):
        return False

"""Shared declarations for multi-repository evaluation evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_serializer,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, max_length=256)]
Address = Annotated[str, Field(min_length=1, max_length=512)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Count = Annotated[StrictInt, Field(ge=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequiredCheck(Contract):
    name: Name
    app_id: Count
    workflow_path: str | None = Field(
        default=None, pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$"
    )
    workflow_source_revision: Sha | None = None
    workflow_definition_revision: Sha | None = None

    @model_validator(mode="after")
    def workflow_pins(self):
        if (self.workflow_source_revision is None) != (
            self.workflow_definition_revision is None
        ):
            raise ValueError(
                "workflow source and definition revisions must be pinned together"
            )
        if self.workflow_path is None and self.workflow_source_revision is not None:
            raise ValueError("workflow revisions require a workflow path")
        return self


class DeclaredInput(Contract):
    address: Address
    repository_id: Count
    evidence_kind: Literal["merge", "deployment"]
    required_checks: list[RequiredCheck] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_checks(self):
        identities = [
            (check.name, check.app_id, check.workflow_path)
            for check in self.required_checks
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("required check identities must be unique")
        return self


class Harness(Contract):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_id: Count
    installation_id: Count
    source_revision: Sha
    definition_revision: Sha | None = None
    harness_digest: Hash
    adapter: Name
    operation: Name


class Environment(Contract):
    connection_id: Name
    target_contract_id: Name
    target_contract_hash: Hash
    runtime_contract: Name
    deployment_receipt_keys: list[Name] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_receipts(self):
        if len(set(self.deployment_receipt_keys)) != len(self.deployment_receipt_keys):
            raise ValueError("deployment receipts must be unique")
        return self


class Criterion(Contract):
    criterion_id: Annotated[str, Field(min_length=1, max_length=128)]
    required: StrictBool = True


class ProbeWorkflow(Contract):
    path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
    definition_revision: Sha
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    artifact_name: Name
    artifact_path: Name
    inputs: dict[Name, str] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def bounded_inputs(self):
        if {
            "adp_correlation",
            "adp_source_revision",
            "adp_definition_revision",
            "source_snapshot_hash",
            "source_manifest",
            "specification_digest",
            "probe_criteria",
        }.intersection(self.inputs):
            raise ValueError("probe transport inputs are engine-owned")
        if any(len(value) > 1024 for value in self.inputs.values()):
            raise ValueError("probe input exceeds the bounded contract")
        return self


class EvaluationSpecification(Contract):
    schema_version: Literal[1] = 1
    evidence_schema: Literal["multi-repository-evaluation/v1"] = (
        "multi-repository-evaluation/v1"
    )
    mode: Literal["repository", "deployed"]
    acceptance_mode: Literal["human", "machine"]
    harness: Harness
    predecessors: list[DeclaredInput] = Field(min_length=1, max_length=128)
    criteria: list[Criterion] = Field(min_length=1, max_length=128)
    environment: Environment | None = None
    probe: ProbeWorkflow | None = None
    max_age_seconds: StrictInt = Field(default=600, ge=1, le=604800)

    @model_serializer(mode="wrap")
    def preserve_existing_specifications(self, handler):
        document = handler(self)
        if self.probe is None:
            document.pop("probe", None)
        return document

    @model_validator(mode="after")
    def complete(self):
        if len({source.address for source in self.predecessors}) != len(
            self.predecessors
        ):
            raise ValueError("predecessor addresses must be unique")
        if len({criterion.criterion_id for criterion in self.criteria}) != len(
            self.criteria
        ):
            raise ValueError("criterion ids must be unique")
        if self.acceptance_mode == "machine" and not any(
            criterion.required for criterion in self.criteria
        ):
            raise ValueError("machine evaluation requires a mandatory criterion")
        if (self.mode == "deployed") != (self.environment is not None):
            raise ValueError(
                "deployed mode requires an environment; repository mode forbids one"
            )
        if self.mode == "repository" and self.probe is not None:
            raise ValueError(
                "repository mode cannot invoke a deployed integration probe"
            )
        if (
            self.probe is not None
            and self.probe.definition_revision != self.harness.definition_revision
        ):
            raise ValueError(
                "probe definition must match the accepted harness revision"
            )
        if self.probe is not None and (
            self.probe.inputs.get("target_contract_hash")
            != self.environment.target_contract_hash
            or self.probe.inputs.get("runtime_contract")
            != self.environment.runtime_contract
        ):
            raise ValueError(
                "probe inputs must bind the accepted target and runtime contracts"
            )
        return self


def _digest(document):
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _harness_identity(harness: Harness):
    return harness.model_dump(mode="json", exclude={"repository"})


def _environment_identity(environment: Environment | None):
    if environment is None:
        return None
    document = environment.model_dump(mode="json")
    document["deployment_receipt_keys"].sort()
    return document


def specification_digest(specification: EvaluationSpecification) -> str:
    document = specification.model_dump(mode="json")
    document["harness"] = _harness_identity(specification.harness)
    document["predecessors"].sort(key=lambda source: source["address"])
    for source in document["predecessors"]:
        source["required_checks"].sort(
            key=lambda check: (
                check["name"],
                check["app_id"],
                check["workflow_path"] or "",
            )
        )
    document["criteria"].sort(key=lambda criterion: criterion["criterion_id"])
    document["environment"] = _environment_identity(specification.environment)
    return _digest(document)


class ObservedCheck(Contract):
    name: Name
    app_id: Count
    head_sha: Sha
    workflow_path: str | None = Field(
        default=None, pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$"
    )
    workflow_source_revision: Sha | None = None
    workflow_definition_revision: Sha | None = None
    conclusion: Literal["success"]


class SourceIdentity(Contract):
    address: Address
    org_id: Name
    flow_id: Name
    node_id: Name
    accepted_plan_hash: Hash
    accepted_plan_version: Count
    execution_id: Name
    cycle: Count
    claim_generation: Count
    source_binding_revision: Hash
    repository_id: Count
    observed_repository_name: Name | None = Field(default=None, exclude=True)
    checks: list[ObservedCheck] = Field(default_factory=list, max_length=64)


class MergeInput(SourceIdentity):
    evidence_kind: Literal["merge"]
    pull_request_node_id: Name
    pull_request_number: Count
    reviewed_head_sha: Sha
    merge_sha: Sha


class DeploymentInput(SourceIdentity):
    evidence_kind: Literal["deployment"]
    deployment_receipt_key: Name
    operation_key: Name
    deployed_revision: Sha


ResolvedInput = Annotated[
    MergeInput | DeploymentInput, Field(discriminator="evidence_kind")
]


class CriterionOutcome(Contract):
    criterion_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome: Literal["pass", "fail", "not_run"]
    evidence_references: list[Name] = Field(default_factory=list, max_length=16)
    affected_source_address: Address | None = None

    @model_validator(mode="after")
    def attribution_only_for_failure(self):
        if self.outcome != "fail" and self.affected_source_address is not None:
            raise ValueError("only a failed criterion may attribute a source")
        return self


class Producer(Contract):
    adapter: Name
    provider_identity: Name
    run_id: Name
    run_attempt: Count


class EvaluationReceipt(Contract):
    schema_version: Literal[1] = 1
    evidence_schema: Literal["multi-repository-evaluation/v1"] = (
        "multi-repository-evaluation/v1"
    )
    mode: Literal["repository", "deployed"]
    org_id: Name
    flow_id: Name
    node_id: Name
    execution_id: Name
    attempt: Count
    claim_generation: Count
    specification_digest: Hash
    input_set_digest: Hash
    harness: Harness
    environment: Environment | None = None
    sources: list[ResolvedInput] = Field(min_length=1, max_length=128)
    criteria: list[CriterionOutcome] = Field(min_length=1, max_length=128)
    producer: Producer
    observed_at: datetime
    expires_at: datetime
    verdict: Literal["pass", "fail", "not_run"]

    @model_validator(mode="after")
    def complete(self):
        if (
            self.observed_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.observed_at >= self.expires_at
        ):
            raise ValueError("receipt requires an aware, increasing observation window")
        if len({source.address for source in self.sources}) != len(self.sources):
            raise ValueError("receipt source addresses must be unique")
        if len({criterion.criterion_id for criterion in self.criteria}) != len(
            self.criteria
        ):
            raise ValueError("receipt criterion ids must be unique")
        return self


def input_set_digest(
    specification: EvaluationSpecification, sources: list[MergeInput | DeploymentInput]
) -> str:
    addresses = [source.address for source in sources]
    if len(set(addresses)) != len(addresses) or set(addresses) != {
        source.address for source in specification.predecessors
    }:
        raise ValueError("evaluation_input_set_mismatch")
    documents = []
    for source in sorted(sources, key=lambda source: source.address):
        identities = [
            (check.name, check.app_id, check.workflow_path) for check in source.checks
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("evaluation_required_checks_mismatch")
        document = source.model_dump(mode="json")
        document["checks"].sort(
            key=lambda check: (
                check["name"],
                check["app_id"],
                check["workflow_path"] or "",
            )
        )
        documents.append(document)
    return _digest(
        {
            "evidence_schema": "multi-repository-evaluation/v1",
            "specification_digest": specification_digest(specification),
            "sources": documents,
        }
    )


def verify_receipt(
    specification: EvaluationSpecification,
    receipt: EvaluationReceipt,
    current_sources: list[MergeInput | DeploymentInput],
    *,
    now: datetime,
) -> None:
    if (
        receipt.mode != specification.mode
        or receipt.specification_digest != specification_digest(specification)
    ):
        raise ValueError("evaluation_specification_mismatch")
    if _harness_identity(receipt.harness) != _harness_identity(
        specification.harness
    ) or _environment_identity(receipt.environment) != _environment_identity(
        specification.environment
    ):
        raise ValueError("evaluation_harness_or_target_mismatch")
    declared = {source.address: source for source in specification.predecessors}
    if set(declared) != {source.address for source in receipt.sources} or len(
        receipt.sources
    ) != len(declared):
        raise ValueError("evaluation_input_set_mismatch")
    for source in receipt.sources:
        expected = declared[source.address]
        if source.org_id != receipt.org_id or source.flow_id != receipt.flow_id:
            raise ValueError("evaluation_source_lineage_mismatch")
        if (
            source.repository_id != expected.repository_id
            or source.evidence_kind != expected.evidence_kind
        ):
            raise ValueError("evaluation_source_identity_mismatch")
        observed = {
            (
                check.name,
                check.app_id,
                check.workflow_path,
                check.workflow_source_revision,
                check.workflow_definition_revision,
            )
            for check in source.checks
        }
        required = {
            (
                check.name,
                check.app_id,
                check.workflow_path,
                check.workflow_source_revision,
                check.workflow_definition_revision,
            )
            for check in expected.required_checks
        }
        if observed != required or len(source.checks) != len(required):
            raise ValueError("evaluation_required_checks_mismatch")
        revision = (
            source.reviewed_head_sha
            if isinstance(source, MergeInput)
            else source.deployed_revision
        )
        if any(check.head_sha != revision for check in source.checks):
            raise ValueError("evaluation_check_revision_mismatch")
    if receipt.input_set_digest != input_set_digest(specification, current_sources):
        raise ValueError("evaluation_input_digest_mismatch")
    if input_set_digest(specification, receipt.sources) != receipt.input_set_digest:
        raise ValueError("evaluation_source_revision_mismatch")
    outcomes = {
        criterion.criterion_id: criterion.outcome for criterion in receipt.criteria
    }
    if set(outcomes) != {
        criterion.criterion_id for criterion in specification.criteria
    }:
        raise ValueError("evaluation_criteria_mismatch")
    if any(
        criterion.affected_source_address is not None
        and criterion.affected_source_address not in declared
        for criterion in receipt.criteria
    ):
        raise ValueError("evaluation_affected_source_mismatch")
    if (
        receipt.expires_at.timestamp() - receipt.observed_at.timestamp()
        > specification.max_age_seconds
    ):
        raise ValueError("evaluation_receipt_age_exceeded")
    if now.tzinfo is None or not receipt.observed_at <= now < receipt.expires_at:
        raise ValueError("evaluation_receipt_not_current")
    if receipt.verdict == "pass" and any(
        outcomes[criterion.criterion_id] != "pass"
        for criterion in specification.criteria
        if criterion.required
    ):
        raise ValueError("evaluation_mandatory_criterion_not_passed")

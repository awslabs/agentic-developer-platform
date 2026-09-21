"""Strict final CLI qualification wire contract; existing reports are insufficient."""

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, model_validator

from .repository_evaluation_contract import Contract, Digest, Name, RepositoryEvaluationSpecification, Runner, Sha, WorkflowReceipt, canonical
from .repository_producer_contract import RepositoryProducer, ScanTarget

NIGHTLY = ".github/workflows/nightly-cli-regression.yml"
NIGHTLY_SCHEDULE = [{"cron": "0 5 * * *"}]
Positive = Annotated[StrictInt, Field(gt=0)]
Nonnegative = Annotated[StrictInt, Field(ge=0)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PHASE = Literal["pre", "live"]
REQUIRED_CRITERIA = (
    {f"5516/AC-{i:02}" for i in range(1, 10)}
    | {f"5589/AC-{i:02}" for i in range(1, 11)}
    | {f"{issue}/CLI-{issue - 5613:02}-AC-{i:02}" for issue in range(5621, 5642) for i in range(1, 5)}
    | {f"5329/validation-{i:02}" for i in range(1, 9)}
    | {f"5331/validation-{i:02}" for i in range(1, 8)}
    | {f"5564/AC-{i:02}" for i in range(1, 7)}
)

# These source rows explicitly require live/deployed/real-provider evidence.
# Do not infer phase from an AC's position: e.g. CLI-24 AC03 is live, AC04 is
# the offline contract/regression row. Other phases remain explicitly reviewed.
MANDATORY_LIVE_CRITERIA = frozenset(
    {
        # #5516's Validation table explicitly labels these rows "Live".
        "5516/AC-05",
        "5516/AC-06",
        "5516/AC-07",
        "5516/AC-08",
        "5516/AC-09",
        # #5589's real-agent, spend-through, recovery and hosted evidence rows.
        "5589/AC-05",
        "5589/AC-06",
        "5589/AC-07",
        "5589/AC-08",
        "5589/AC-09",
        "5589/AC-10",
        "5621/CLI-08-AC-04",  # Fresh served EC2 capability/readiness evidence.
        "5622/CLI-09-AC-01",  # Selected tenant retained by local agent inference.
        "5622/CLI-09-AC-04",  # Served EC2 installation of both context helpers.
        "5624/CLI-11-AC-04",  # Live disposable identity lifecycle.
        "5626/CLI-13-AC-04",  # Real bounded Claude/Codex person-cap denial/recovery.
        "5627/CLI-14-AC-02",  # Actual RPM enforcement across gateway workers.
        "5627/CLI-14-AC-03",  # Real token usage/overlap and TPM/concurrent denial.
        "5627/CLI-14-AC-04",  # Both real agent binaries and restored fixture limits.
        "5628/CLI-15-AC-01",  # Real local and hosted marked inference/charge lookup.
        "5629/CLI-16-AC-02",  # Live pause/resume on a real hosted fixture.
        "5630/CLI-17-AC-04",  # Installed EC2 + bounded hosted integrated journey.
        "5631/CLI-18-AC-04",  # Actual behavior after revocation; live fixture cleanup.
        "5632/CLI-19-AC-04",  # Real bounded EC2 indexing and cleanup.
        "5633/CLI-20-AC-04",  # Real local/hosted inference and provider/account routing.
        "5634/CLI-21-AC-04",  # Real isolated OAuth/repository/webhook continuation.
        "5635/CLI-22-AC-01",  # Provider connect/resume after real approval.
        "5635/CLI-22-AC-04",  # Isolated live GitLab task, webhook/run/artifact evidence.
        "5636/CLI-23-AC-04",  # Real local/hosted model-decision evidence.
        "5637/CLI-24-AC-03",  # Served EC2 -> actual domain create/read/delete/handoff.
        "5638/CLI-25-AC-04",  # Separately authorized live disposable compute lifecycle.
        "5639/CLI-26-AC-04",  # Live isolated research proposal approval/rejection.
        "5640/CLI-27-AC-04",  # Real bounded multi-turn chat through served EC2.
        "5641/CLI-28-AC-04",  # Separately authorized disposable deployment/teardown.
        "5329/validation-04",  # Authorized live inputs -> trusted validation evidence.
        "5329/validation-08",  # Integrated deployed/live amendment acceptance scenario.
        "5331/validation-07",  # Authorized deployed CLI -> hosted planning -> dispatch smoke.
    }
)
# All 23 story bodies retain a live acceptance boundary. Where the source does
# not assign that boundary to a particular table row (#5623/#5625), acceptance
# must explicitly choose a real scenario rather than the engine guessing AC04.
STORIES_REQUIRING_LIVE_EVIDENCE = {5516, 5589, *range(5621, 5642)}


class CliRunner(Runner):
    adapter: Literal["engine-cli-live-evidence-v1"]
    qualification_config_path: str = Field(pattern=r"^tests/e2e/cli_regression/[A-Za-z0-9._/-]+\.json$")
    requirements_path: str = Field(pattern=r"^tests/e2e/cli_regression/[A-Za-z0-9._/-]+\.json$")

    @model_validator(mode="after")
    def path_scope(self):
        if any(part in {"", ".", ".."} for path in (self.qualification_config_path, self.requirements_path) for part in path.split("/")):
            raise ValueError("qualification configuration must be a safe repository path")
        return self


class Target(ScanTarget):
    resource_kind: Literal["cli_qualification"] = "cli_qualification"


class EvidenceFile(Contract):
    path: Name
    sha256: Digest


class CaseBinding(Contract):
    case_id: Name
    suite_id: Name
    phase: PHASE
    actor_id: Name
    command: str = Field(min_length=1, max_length=2048)
    expected: JsonValue
    requires_inference: StrictBool

    @model_validator(mode="after")
    def execution(self):
        if self.phase == "live" and not (self.command == "adp" or self.command.startswith("adp ")):
            raise ValueError("live product operations must use the served ADP CLI")
        if len(canonical(self.expected)) > 8192:
            raise ValueError("accepted case expectation exceeds the evidence bound")
        return self


class CriterionBinding(Contract):
    criterion_id: Name
    source_body_sha256: Digest
    requirement_sha256: Digest
    phases: list[PHASE] = Field(min_length=1, max_length=2)
    cases: list[CaseBinding] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def complete(self):
        if set(self.phases) != {case.phase for case in self.cases} or len(self.phases) != len(set(self.phases)):
            raise ValueError("criterion phases require actual case mappings")
        if len({(case.suite_id, case.case_id) for case in self.cases}) != len(self.cases):
            raise ValueError("criterion cases must be unique")
        if self.criterion_id in MANDATORY_LIVE_CRITERIA and "live" not in self.phases:
            raise ValueError("the source contract requires live evidence for this criterion")
        if self.criterion_id.startswith("5564/") and self.phases != ["pre"]:
            raise ValueError("profile implementation prerequisite does not authorize live spending")
        return self


class SuiteBinding(Contract):
    suite_id: Name
    workflow_path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
    revision: Sha
    job_name: Name
    artifact: Name
    report_path: Name


class Bounds(Contract):
    shared_meter_ref: Name
    max_instances: StrictInt = Field(default=1, ge=1, le=1)
    max_duration_seconds: StrictInt = Field(gt=0, le=3600)
    max_daily_inference_usd: Decimal = Field(gt=0, le=5, allow_inf_nan=False)
    max_requests: Positive
    max_input_tokens: Positive
    max_output_tokens: Positive
    max_output_tokens_per_request: Positive


class Deployment(Contract):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: Name
    gateway_url: str = Field(pattern=r"^https://[^\s?#]+$")
    deployment_id: Name
    gateway_revision: Sha
    gateway_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    served_release_sha256: Digest
    installed_files: dict[Name, Digest] = Field(min_length=1, max_length=128)
    worker_revisions: dict[Name, Sha] = Field(min_length=1, max_length=32)
    # Actor references are expected identities, never credentials.
    tenant_id: Name
    ordinary_user_id: Name | None = None
    admin_user_id: Name | None = None

    @model_validator(mode="after")
    def identities(self):
        if self.ordinary_user_id is not None and self.ordinary_user_id == self.admin_user_id:
            raise ValueError("ordinary and admin fixture identities must differ")
        return self


class Qualification(Contract):
    owner_issue: Literal[5644, 5329, 5331]
    requirements_sha256: Digest
    manifest_artifact: Name
    manifest_path: Name
    deployment: Deployment
    bounds: Bounds
    criteria: list[CriterionBinding] = Field(min_length=7, max_length=256)
    suites: list[SuiteBinding] = Field(min_length=1, max_length=15)

    @model_validator(mode="after")
    def complete(self):
        ids = [item.criterion_id for item in self.criteria]
        required = REQUIRED_CRITERIA if self.owner_issue == 5644 else {key for key in REQUIRED_CRITERIA if key.startswith(f"{self.owner_issue}/")}
        if set(ids) != required or len(ids) != len(set(ids)):
            raise ValueError("qualification requires the complete criterion set for its explicit owner issue")
        suites = {item.suite_id for item in self.suites}
        if len(suites) != len(self.suites) or any(case.suite_id not in suites for item in self.criteria for case in item.cases):
            raise ValueError("every case must map to exactly one declared suite")
        mappings = {}
        for item in self.criteria:
            for case in item.cases:
                key = (case.suite_id, case.case_id)
                contract = case.model_dump(mode="json")
                if key in mappings and canonical(mappings[key]) != canonical(contract):
                    raise ValueError("one case cannot claim different execution contracts")
                mappings[key] = contract
        if set(suite for suite, _ in mappings) != suites:
            raise ValueError("unused suites cannot widen qualification authority")
        actors = {case["actor_id"] for case in mappings.values() if case["phase"] == "live"}
        if self.owner_issue == 5644 and (
            not self.deployment.ordinary_user_id
            or not self.deployment.admin_user_id
            or not {self.deployment.ordinary_user_id, self.deployment.admin_user_id} <= actors
        ):
            raise ValueError("qualification must exercise separate ordinary and admin identities")
        if self.owner_issue == 5644 and not any(case["requires_inference"] and case["phase"] == "live" for case in mappings.values()):
            raise ValueError("qualification must exercise metered live inference")
        live_issues = {int(item.criterion_id.split("/", 1)[0]) for item in self.criteria if "live" in item.phases}
        covered_issues = {int(item.criterion_id.split("/", 1)[0]) for item in self.criteria}
        if not (STORIES_REQUIRING_LIVE_EVIDENCE & covered_issues) <= live_issues:
            raise ValueError("each story's source live acceptance boundary requires an explicit live scenario")
        bodies = {}
        for item in self.criteria:
            issue = item.criterion_id.split("/", 1)[0]
            if issue in bodies and bodies[issue] != item.source_body_sha256:
                raise ValueError("one issue cannot bind conflicting requirement bodies")
            bodies[issue] = item.source_body_sha256
        return self


class CliProducer(RepositoryProducer):
    target: Target
    # This is a qualification, never a repository image scan.
    images: None = None


class CliLiveSpecification(RepositoryEvaluationSpecification):
    evidence_schema: Literal["cli-live-evaluation/v1"]
    runner: CliRunner
    qualification: Qualification
    producer: CliProducer | None = None

    @model_validator(mode="after")
    def cli_complete(self):
        if len(self.workflows) != 1 or self.workflows[0].path != NIGHTLY or self.workflows[0].dispatch_only:
            raise ValueError("CLI qualification must retain the existing nightly workflow and its schedule")
        workflow, expected = self.workflows[0], self.qualification
        pairs = [(expected.manifest_artifact, expected.manifest_path)] + [(s.artifact, s.report_path) for s in expected.suites]
        if len(pairs) != len(set(pairs)) or set(pairs) != {(a.name, a.path) for a in workflow.artifacts}:
            raise ValueError("the exact parent manifest and every child report must be accepted artifacts")
        if any("{run_attempt}" not in name for name, _ in pairs):
            raise ValueError("CLI artifacts must be unique to the workflow attempt")
        if not {s.job_name for s in expected.suites} <= set(workflow.required_jobs):
            raise ValueError("every suite must bind a required provider job")
        if self.producer and (
            self.producer.workflow_criterion_id != workflow.criterion_id
            or (self.producer.receipt_artifact, self.producer.receipt_path) != pairs[0]
            or self.producer.target.account_id != expected.deployment.account_id
            or self.producer.target.region != expected.deployment.region
        ):
            raise ValueError("dispatch must bind the same CLI qualification target and manifest")
        if self.producer and (
            self.producer.inputs.get("qualification_contract") != "cli-live-qualification/v1"
            or self.producer.inputs.get("qualification_sha256") != hashlib.sha256(canonical(expected.model_dump(mode="json")).encode()).hexdigest()
        ):
            raise ValueError("dispatch must explicitly select this qualification and its exact bounds/mapping")
        if set(item.criterion_id for artifact in workflow.artifacts for item in artifact.predicates) & REQUIRED_CRITERIA:
            raise ValueError("artifact predicates cannot shadow the typed CLI criteria")
        return self


class RunIdentity(Contract):
    repository_id: Positive
    run_id: Positive
    run_attempt: Positive
    workflow_revision: Sha
    requirements_sha256: Digest
    qualification_sha256: Digest
    correlation: Digest | None = None


class RuntimeEvidence(Contract):
    deployment: Deployment
    client_kind: Literal["ec2"]
    instance_id: str = Field(pattern=r"^i-[0-9a-f]{8,17}$")
    instance_launched_at: datetime
    gateway_before: Sha
    gateway_after: Sha
    observed_at: datetime
    evidence: EvidenceFile


class InferenceRequest(Contract):
    request_id: Name
    input_tokens: Nonnegative
    output_tokens: Nonnegative
    cost_usd: Money
    completed_at: datetime


class GuardOperation(Contract):
    operation_ref: Name
    suite_id: Name
    case_id: Name
    actor_id: Name
    command: str = Field(min_length=1, max_length=2048)
    started_at: datetime
    completed_at: datetime
    requests: list[InferenceRequest] = Field(max_length=4096)
    created_resources: list[Name] = Field(max_length=256)
    changed_configuration: list[Name] = Field(max_length=128)


class GuardEvidence(Contract):
    limits: Bounds
    guard_id: Name
    enforcement: Literal["independent_hard_guard"]
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    window_started_at: datetime
    window_completed_at: datetime
    daily_spend_before_usd: Money
    daily_spend_after_usd: Money
    requests: Nonnegative
    input_tokens: Nonnegative
    output_tokens: Nonnegative
    max_observed_output_tokens_per_request: Nonnegative
    peak_instances: StrictInt = Field(ge=1, le=1)
    operations: list[GuardOperation] = Field(min_length=1, max_length=8192)
    evidence: EvidenceFile


class CleanupEvidence(Contract):
    complete: StrictBool
    configuration_restored: StrictBool
    restored_configuration: list[Name] = Field(max_length=128)
    unresolved_operations: list[Name] = Field(max_length=128)
    # Every actually created resource has an observed terminal/disposed state.
    resources: dict[Name, Literal["terminated", "deleted", "restored", "retained_owned_fixture", "unresolved"]] = Field(max_length=256)
    observed_at: datetime
    evidence: EvidenceFile


class CaseResult(Contract):
    case_id: Name
    phase: PHASE
    status: Literal["passed", "failed", "blocked", "not_run"]
    execution_path: Literal["served_cli", "offline_test"]
    actor_id: Name
    command: str = Field(min_length=1, max_length=2048)
    expected: JsonValue
    observed: JsonValue
    operation_refs: list[Name] = Field(max_length=64)
    started_at: datetime
    completed_at: datetime
    evidence: list[EvidenceFile] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded(self):
        if len(canonical(self.expected)) > 8192 or len(canonical(self.observed)) > 8192:
            raise ValueError("case state exceeds the evidence bound")
        return self


class SuiteReport(RunIdentity):
    evidence_schema: Literal["cli-live-suite/v1"]
    suite_id: Name
    job_id: Positive
    runtime: RuntimeEvidence | None
    started_at: datetime
    completed_at: datetime
    cases: list[CaseResult] = Field(min_length=1, max_length=512)
    guard_id: Name | None
    cleanup: CleanupEvidence


class ChildArtifact(Contract):
    suite_id: Name
    artifact: Name
    report_path: Name
    sha256: Digest


class QualificationManifest(RunIdentity):
    evidence_schema: Literal["cli-live-qualification/v1"]
    partial: Literal[False]
    started_at: datetime
    completed_at: datetime
    runtime: RuntimeEvidence
    guards: list[GuardEvidence] = Field(min_length=1, max_length=32)
    children: list[ChildArtifact] = Field(min_length=1, max_length=15)
    cleanup: CleanupEvidence


class CliWorkflowReceipt(WorkflowReceipt):
    event: Literal["workflow_dispatch", "schedule"]
    qualification: QualificationManifest

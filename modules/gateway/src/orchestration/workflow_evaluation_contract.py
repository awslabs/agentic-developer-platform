"""Bounded workflow execution with a separate human acceptance decision."""

from typing import Literal

from pydantic import Field, model_validator

from .repository_evaluation_contract import Contract, Name, RepositoryEvaluationSpecification


class WorkflowTarget(Contract):
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = Field(pattern=r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$")
    resource_kind: Literal["cli-evaluation"] = "cli-evaluation"
    resource_id: Literal["dev"] = "dev"


class WorkflowProducer(Contract):
    mode: Literal["dispatch_once"] = "dispatch_once"
    workflow_criterion_id: Name
    target: WorkflowTarget
    inputs: dict[Name, str]
    receipt_artifact: Name
    receipt_path: Name

    @model_validator(mode="after")
    def bounded_cli_run(self):
        if self.inputs.get("environment") != self.target.resource_id or self.inputs.get("suites") != "knowledge":
            raise ValueError("workflow evaluation is limited to the knowledge qualification suite in the accepted environment")
        if self.inputs.get("mode") != "start" or self.inputs.get("inject_fault") != "none":
            raise ValueError("qualification must start without fault injection")
        if self.inputs.get("fixtures_json") != "{}" or self.inputs.get("evaluation_id") != "":
            raise ValueError("qualification uses the protected environment fixtures and a fresh evaluation identity")
        if any(name.startswith("adp_") for name in self.inputs):
            raise ValueError("workflow correlation is engine-owned")
        return self


class WorkflowEvaluationSpecification(RepositoryEvaluationSpecification):
    evidence_schema: Literal["workflow-evaluation/v1"]
    acceptance_mode: Literal["human"] = "human"
    producer: WorkflowProducer

    @model_validator(mode="after")
    def workflow_binding(self):
        if len(self.workflows) != 1 or self.workflows[0].path != ".github/workflows/eval-cli-uplift.yml":
            raise ValueError("qualification must use the existing CLI Uplift workflow")
        workflow = self.workflows[0]
        if workflow.source.revision is None or workflow.definition.revision != workflow.source.revision:
            raise ValueError("qualification requires one immutable source and workflow revision")
        if self.producer.inputs.get("expected_revision") != workflow.source.revision:
            raise ValueError("deployed revision must match the accepted source")
        if set(self.producer.inputs) != {"environment", "expected_revision", "mode", "fixtures_json", "suites", "evaluation_id", "inject_fault"}:
            raise ValueError("qualification inputs must be fully specified")
        if set(workflow.required_jobs) != {"Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"}:
            raise ValueError("qualification requires live evaluation and independent cleanup evidence")
        if not workflow.dispatch_only or workflow.criterion_id != self.producer.workflow_criterion_id:
            raise ValueError("one correlated dispatch-only workflow is required")
        if not any((a.name, a.path) == (self.producer.receipt_artifact, self.producer.receipt_path) for a in workflow.artifacts):
            raise ValueError("qualification result artifact must be part of the accepted evidence")
        return self

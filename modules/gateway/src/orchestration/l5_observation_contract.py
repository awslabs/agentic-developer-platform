"""Human UI observations are attributed facts, never release pass declarations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT = "l5-human-observation/v1"
PREPARED = "l5_observation_prepared"
OBSERVED = "l5_observation_recorded"
ENDORSED = "l5_observation_owner_endorsed"
LAUNCH = "l5_observation_launch"
SERVICES = Literal["security", "devops", "finops", "well_architected", "transform"]
CHECKS = {"ui-cli-mcp-parity", "two-user-sso", "sso-gateway-only-ingress", "sso-denial-before-federation"}


class PrepareObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_attempt: int = Field(gt=0)
    workflow_action_id: str = Field(min_length=1, max_length=36)
    product_id: str = Field(min_length=1, max_length=36)
    project_id: str = Field(min_length=1, max_length=255)
    service_id: SERVICES
    binding_id: str = Field(min_length=1, max_length=36)
    check_id: str = Field(min_length=1, max_length=64)
    observer_id: str = Field(min_length=1, max_length=255)
    subject: Literal["configuration", "result", "native_ui", "native_denial", "run_submission"]
    result_id: str | None = Field(default=None, min_length=1, max_length=36)
    reason: str = Field(min_length=8, max_length=1000)

    @model_validator(mode="after")
    def selected(self):
        if self.check_id not in CHECKS or (self.subject == "result") != (self.result_id is not None):
            raise ValueError("Select one original UI observation and its exact result, when applicable")
        if self.subject == "run_submission" and (self.service_id != "devops" or self.check_id != "ui-cli-mcp-parity"):
            raise ValueError("UI run submission evidence requires the original DevOps parity check")
        if self.subject.startswith("native_") and self.service_id != "devops":
            raise ValueError("Native UI observations require the existing DevOps gateway entry")
        if self.check_id != "ui-cli-mcp-parity" and not self.subject.startswith("native_"):
            raise ValueError("SSO evidence must name the native UI or denial journey")
        if self.check_id == "sso-denial-before-federation" and self.subject != "native_denial":
            raise ValueError("The denial check requires a denied native launch")
        return self


class RecordObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation: Literal["displayed", "denied", "mismatch", "submitted"]
    # These are explicitly human-observed fields. They do not establish native
    # success, correct artifacts, provider eligibility, or full UI parity.
    displayed_configuration_revision: int | None = Field(default=None, gt=0)
    displayed_result_id: str | None = Field(default=None, min_length=1, max_length=36)
    displayed_result_revision: int | None = Field(default=None, gt=0)
    displayed_native_hostname: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{3,128}\.aidevops\.global\.app\.aws$")
    launch_receipt_id: str | None = Field(default=None, min_length=1, max_length=36)
    submitted_run_id: str | None = Field(default=None, min_length=1, max_length=36)


class EndorseObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

"""Selections are requests; authoritative views always come from the Gateway."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,255}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FixtureCase(Strict):
    tenant_id: Id
    product_id: Id
    project_id: Id
    binding_id: Id
    configuration_id: Id


class Selection(Strict):
    tenant_id: Id
    product_id: Id
    project_id: Id
    binding_id: Id
    configuration_id: Id
    configuration_revision: int = Field(gt=0)
    configuration_hash: Hash
    binding_revision: int = Field(gt=0)
    binding_generation: int = Field(gt=0)
    source_sha256: Hash
    connection_sha256: Hash
    provider_sha256: Hash
    observer_id: Id | None = None
    phase: Literal["readiness", "edit", "display"]
    ui_region: str = Field(min_length=1, max_length=64)
    ui_reason: str = Field(min_length=1, max_length=1000)
    ui_receipt: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    run_id: Id | None = None
    run_revision: int | None = Field(default=None, gt=0)
    run_input_sha256: Hash | None = None

    @model_validator(mode="after")
    def exact_phase(self):
        values = (self.run_id, self.run_revision, self.run_input_sha256)
        if (self.phase == "display" and any(value is None for value in values)) or (
            self.phase != "display" and any(value is not None for value in values)
        ):
            raise ValueError("Display selects the exact initial MCP run; edit has no run")
        return self


class Consent(Strict):
    snapshot_sha256: Hash
    expires_at: AwareDatetime = Field(strict=False)
    observer_id: Id


class Preview(Strict):
    observer_id: Id | None = None


class Observation(Strict):
    snapshot_sha256: Hash
    observation: Literal["displayed", "mismatch", "denied"]
    configuration_revision: int = Field(gt=0)
    binding_revision: int = Field(gt=0)
    run_id: Id
    run_revision: int = Field(gt=0)


class Registration(Strict):
    slot: Literal["api", "mcp", "cli", "legacy_ui"]


class Reference(Strict):
    request_id: Id
    tenant_id: Id


class LocalExport(Strict):
    ticket_id: Id


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Evidence timestamps must be timezone aware")
    return result

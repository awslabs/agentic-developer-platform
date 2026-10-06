"""Bounded historical evidence. None of these records asserts live readiness."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identity = Annotated[str, Field(min_length=1, max_length=255)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ResourceArn = Annotated[str, Field(min_length=1, max_length=2048)]


class HistoricalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    status: Literal["OBSERVED"] = "OBSERVED"
    org_id: Identity
    workspace_id: Identity
    artifact_id: Sha256
    recorded_at: AwareDatetime


class AppliedOwnershipEvidence(HistoricalEvidence):
    request_id: Identity
    original_operation_id: Identity
    current_operation_id: Identity
    apply_operation_id: Identity
    plan_revision: Sha256
    account_id: Annotated[str, Field(pattern=r"^[0-9]{12}$")]
    region: Annotated[str, Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")]
    owned_resources: Annotated[list[ResourceArn], Field(min_length=1, max_length=128)]
    preserved_resources: Annotated[list[ResourceArn], Field(max_length=128)]
    inventory_complete: Literal[False] = False


class CleanupPreparationEvidence(HistoricalEvidence):
    source_operation_id: Identity
    retirement_request_id: Identity
    preparation_request_id: Identity
    preparation_revision: Sha256
    preparation_plan_revision: Sha256
    preparation_approval_id: Identity
    operation_id: Identity
    producer_attempt_id: Identity
    producer_fence_token: Annotated[int, Field(gt=0)]
    grant_count: Annotated[int, Field(gt=0, le=128)]
    grant_set_sha256: Sha256
    fence_sha256: Sha256
    inventory_sha256: Sha256
    managed_workload_inventory_sha256: Sha256
    destroy_sha256: Sha256
    plan_file_sha256: Sha256
    plan_json_sha256: Sha256
    backend_sha256: Sha256
    retirement_plan_sha256: Sha256

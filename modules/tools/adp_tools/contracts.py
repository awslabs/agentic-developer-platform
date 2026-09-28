"""Wire contracts shared by domain services; authority is resolved server-side."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
TASK_ID = r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class TaskRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=TASK_ID)
    invocation_id: str = Field(pattern=UUID4)
    generation: int = Field(ge=1, le=64, strict=True)


class TaskAttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: TaskRunBody
    runtime_attempt_id: str = Field(pattern=UUID4)


class ToolIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(pattern=TASK_ID)
    invocation_id: str = Field(pattern=UUID4)
    generation: int = Field(ge=1, le=64, strict=True)
    runtime_attempt_id: str = Field(pattern=UUID4)
    tenant: str = Field(min_length=1, max_length=256)
    canonical_principal: str = Field(min_length=1, max_length=256)


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    identity: ToolIdentity
    task: dict

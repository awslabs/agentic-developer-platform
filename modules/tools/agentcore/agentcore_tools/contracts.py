"""Existing Task tool request envelope."""

from pydantic import BaseModel, ConfigDict, Field
from adp_tools.contracts import TaskAttemptBody


class CyberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(pattern=r"^1\.0$")
    attempt: TaskAttemptBody
    operation_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    operation: str
    payload: dict

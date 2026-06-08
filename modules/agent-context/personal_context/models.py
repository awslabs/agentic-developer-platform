"""Data models for personal-context entries stored in AGFS."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EntryType(str, Enum):
    """Types of personal-context entries."""

    learning = "learning"
    synthesis = "synthesis"
    pattern = "pattern"


class Visibility(str, Enum):
    """Visibility scope of an entry."""

    private = "private"
    shared = "shared"


class Persona(str, Enum):
    """Agent persona that created the entry."""

    operations = "operations"
    developer = "developer"
    architect = "architect"
    reviewer = "reviewer"


class PersonalContextEntry(BaseModel):
    """A single personal-context entry stored as AGFS JSON.

    Fields ``owner_sub`` and ``tenant_id`` are force-stamped from identity
    headers on writes — any client-supplied values are ignored.
    """

    id: str = Field(..., description="ULID identifier")
    type: EntryType
    owner_sub: str = Field(..., description="Cognito sub (UUID) from X-Owner-Sub header")
    tenant_id: str = Field(..., description="Tenant/org ID from X-Tenant-Id header")
    visibility: Visibility = Visibility.private
    persona: Persona = Persona.developer
    learning_type: str = ""
    content: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    validated: bool = False
    superseded_by: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_accessed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decay_score: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("owner_sub")
    @classmethod
    def validate_owner_sub_format(cls, v: str) -> str:
        """Ensure owner_sub looks like a UUID (basic format check)."""
        import re

        pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        if not re.match(pattern, v.lower()):
            raise ValueError(f"owner_sub must be a valid UUID, got: {v!r}")
        return v.lower()

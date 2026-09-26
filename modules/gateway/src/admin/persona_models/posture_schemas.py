"""Wire shapes for the platform-admin runtime-posture surface (PMM-07)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt


class SetRuntimePostureRequest(BaseModel):
    """One audited posture change.

    ``posture`` is typed as a plain string rather than a ``Literal`` so an
    unknown value reaches the service's closed-vocabulary check and is refused
    with the same stable reason code and the same refusal audit record as every
    other rejection, instead of being turned away by schema validation with no
    audit trail.

    ``expected_revision`` is mandatory: a caller that has not read the current
    revision cannot safely change the platform's enforcement state.  It is
    ``StrictInt`` with ``ge=1`` rather than a plain ``int``, which is load-bearing
    rather than stylistic: Pydantic coerces ``true`` to ``1`` and ``"1"`` to ``1``
    for a plain ``int`` field, so a malformed body reached the service already
    looking like a well-formed compare-and-set and the service's own
    bool-rejecting validator never saw the boolean.  Strictness must live at the
    boundary, where the untrusted value actually arrives.
    """

    operation_id: UUID | None = None
    posture: str = Field(description="Target posture: disabled, report_only or enforcing.")
    expected_revision: Annotated[StrictInt, Field(ge=1)] = Field(
        description="The posture_revision the caller believes is current. A stale value is refused.",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="Operator note recorded on the audit entry, e.g. 'operational rollback'.",
    )


class RuntimePostureResponse(BaseModel):
    """The stored posture plus what a caller needs to change it safely."""

    compatibility_class: str
    posture: str
    posture_revision: int
    updated_by: str | None = None
    updated_at: datetime | None = None
    supported_postures: list[str]
    propagation_bound_seconds: int = Field(
        description=(
            "Measured upper bound, in seconds, after which every gateway instance "
            "observes this value. Operational rollback waits this long, then verifies."
        ),
    )


class RollbackRuntimePostureRequest(BaseModel):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    historical_revision: Annotated[StrictInt, Field(ge=1)]
    operation_id: UUID
    reason: str = Field(min_length=1, max_length=512)

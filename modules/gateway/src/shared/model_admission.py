"""Short-lived worker diagnostics, never budget or execution authority."""

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

ModelAdmissionStatus = Literal["budget_wait", "budget_reservations_pending", "budget_unavailable", "not_waiting"]
MODEL_ADMISSION_STATUSES = {"budget_wait", "budget_reservations_pending", "budget_unavailable", "not_waiting"}
LEGACY_WAIT = re.compile(r"Waiting for budget holds to settle; retrying model admission \(up to [0-9]+s remaining\)")
# Existing workers refresh on the first retry after 60s; backoff plus jitter
# can add 36s. Leave room for that cadence without keeping old waits indefinitely.
WAIT_FRESH_SECONDS = 120


def reported_admission_status(fields):
    status = fields.get("model_admission_status")
    if status is not None:
        if status not in MODEL_ADMISSION_STATUSES:
            raise ValueError("unsupported model admission status")
        return status
    # Existing workers already send this exact heartbeat. Date it separately
    # from ordinary liveness so another heartbeat cannot keep an old wait alive.
    summary = fields.get("summary")
    return "budget_wait" if isinstance(summary, str) and LEGACY_WAIT.fullmatch(summary) else None


class RuntimeBudgetWait(BaseModel):
    blocker: str
    detail: str
    next_action: str
    observed_at: str


def runtime_budget_wait(*, status, updated_at, run_status, liveness, now=None):
    if run_status != "in_progress" or liveness != "live" or status not in MODEL_ADMISSION_STATUSES - {"not_waiting"}:
        return None
    try:
        observed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or not 0 <= ((now or datetime.now(UTC)) - observed).total_seconds() <= WAIT_FRESH_SECONDS:
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    detail = {
        "budget_reservations_pending": "Model admission is waiting for another request's budget reservation to settle.",
        "budget_unavailable": "Model admission is waiting for the budget meter to confirm spend.",
        "budget_wait": "The worker reports waiting for model budget admission.",
    }[status]
    return RuntimeBudgetWait(
        blocker=status,
        detail=detail,
        next_action="Wait for budget reconciliation; inspect the budget if the wait continues.",
        observed_at=observed.isoformat(),
    )

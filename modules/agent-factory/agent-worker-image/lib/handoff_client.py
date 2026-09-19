"""Report this run's delivery handoff and require a verifiable receipt (#5144).

## Why this is not `record_status()`

``lib/invocation_status.update_status`` is **advisory** and deliberately fail-soft:
it logs and continues, and ``status_gateway_client.record_status()`` returns
``None`` because a lost dashboard transition must never abort a run in flight. That
contract is correct and this module does not change it.

But the same swallowed failure is exactly how a worker comes to exit 0 while review,
deployment or evaluation are still outstanding — the run looks complete because the
process ended cleanly. So the authoritative handoff is a **separate call with a
strict result**: :func:`report_handoff` returns a typed object whose
:attr:`HandoffReceipt.accepted` is false unless the gateway committed a receipt and
that receipt came back on readback.

The split is the point. Advisory status stays fail-soft; the handoff does not.

## What is sent, and what is deliberately not

Sent: nothing that names the work. No tenant, node, cycle, flow, plan version,
claim generation, execution id or action id — there is no parameter for any of them.
The gateway resolves every one from the protected execution record keyed by the run
credential, which is what makes it impossible for this worker to commit a handoff
against another run even if it tried.

That is the same property ``lib/pr_binding.py`` relies on, and it is why this module
goes through :func:`lib.status_gateway_client.post_self` rather than opening its own
transport: all three authentication layers (SigV4, run credential, workload token),
the redirect refusal, ``trust_env = False`` and the uniform refusal reasons are
inherited rather than reimplemented.

## A missing receipt leaves delivery unfinished

:func:`handoff_note` never raises — the branch is pushed and the PR is open by the
time it runs, so bookkeeping must not destroy delivered work. But unlike the
advisory path its failure is **loud and consequential**: the caller gets a note
saying the handoff was not recorded, and the engine leaves the lane due/blocked.
A worker may report an accepted handoff **only** when ``accepted`` is true.

This module cannot and does not mark a lane complete. Nothing here writes a
terminal state; the gateway's continuation is always non-terminal by construction.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from lib.status_gateway_client import (
    StatusGatewayError,
    authority_enabled,
    post_self,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HANDOFF_REQUIRED_ENV",
    "HandoffReceipt",
    "handoff_note",
    "handoff_required",
    "report_handoff",
]

# Set during bootstrap from the dispatch envelope. Absent for a webhook-triggered
# run and for a legacy engine dispatch, both of which report nothing: this module is
# inert unless the engine asked for a durable handoff, so every pre-existing path
# behaves exactly as it did before it existed.
HANDOFF_REQUIRED_ENV = "ADP_HANDOFF_REQUIRED"

_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})

# The gateway path for this report. A `/self` path can only ever address the
# execution the run credential and workload token together identify.
_HANDOFF_PATH = "/handoff"

# Outcomes that license reporting an accepted handoff. Mirrors the server's
# `HandoffOutcome` accepted set. A value absent from this set — including one a
# newer server may add — is NOT accepted, which is the fail-closed direction.
_ACCEPTED_OUTCOMES = frozenset({"committed", "already_committed"})


@dataclass(frozen=True)
class HandoffReceipt:
    """The strict result of a handoff report.

    Unlike ``record_status()``'s ``None``, this cannot be mistaken for success:
    :attr:`accepted` requires both an accepted outcome and a non-empty receipt that
    the server read back from the row it committed.
    """

    outcome: str
    receipt_ref: str = ""
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """True only when a receipt is durable and belongs to this attempt."""
        return self.outcome in _ACCEPTED_OUTCOMES and bool(self.receipt_ref)


def handoff_required() -> bool:
    """Whether the engine asked this run to produce a durable handoff receipt.

    Defaults to **false**, so a run whose envelope carried no marker reports nothing
    and behaves exactly as it did before this module existed. This is what keeps an
    older gateway and a newer worker compatible in the safe direction.
    """
    return os.environ.get(HANDOFF_REQUIRED_ENV, "").strip().lower() in _TRUE_SPELLINGS


def report_handoff(*, summary: str = "") -> HandoffReceipt:
    """Report the handoff and return the receipt the gateway committed.

    The request body carries no identifier for the work — see the module docstring.
    ``summary`` is free text for operator diagnostics only; the gateway does not
    derive authority from it.

    Raises:
        StatusGatewayError: the report could not be sent, or the gateway refused it.
            Callers should use :func:`handoff_note`, which converts this to a note
            and never raises.
    """
    payload: dict[str, object] = {}
    if summary:
        payload["summary"] = summary[:4096]
    result = post_self(_HANDOFF_PATH, payload)

    outcome = result.get("outcome")
    receipt = result.get("receipt_ref")
    reason = result.get("reason")
    if not isinstance(outcome, str) or not outcome:
        # A response this worker cannot interpret is not evidence of a handoff. An
        # older gateway that does not implement this route answers 404, which
        # `post_self` already raises on; this covers a 200 whose body is unusable.
        raise StatusGatewayError("gateway returned no usable handoff outcome")
    return HandoffReceipt(
        outcome=outcome,
        receipt_ref=receipt if isinstance(receipt, str) else "",
        reason=reason if isinstance(reason, str) else "",
    )


def handoff_note(*, summary: str = "") -> str:
    """Report the handoff and return a comment section. Never raises.

    Returns "" when no handoff applies, so the caller appends nothing and every
    pre-existing closing comment is unchanged.

    A failure returns a **visible** note rather than failing quietly: an
    unrecorded handoff means the engine holds the lane as still-due, and the
    operator needs to know that at the moment it happened. Silence here would
    reproduce the original defect's worst property — a run that looks finished
    while its remaining work is invisible.
    """
    if not handoff_required():
        return ""
    if not authority_enabled():
        # The gateway path is how a handoff is authenticated; without it there is no
        # way to commit one. An explicit linked blocker, never a fallback to a
        # broader credential.
        return (
            "> **Delivery handoff not recorded.** Agent authority is not enabled for this "
            "deployment, so this run could not commit a continuation receipt. The engine will "
            "leave this work due rather than treating this run's exit as completion."
        )
    try:
        receipt = report_handoff(summary=summary)
    except StatusGatewayError as exc:
        logger.warning("handoff report failed: %s", exc)
        return (
            f"> **Delivery handoff not recorded:** {exc}. This run's exit does not complete the "
            "work; the engine will keep it due until a continuation receipt is committed."
        )

    if not receipt.accepted:
        # The server answered, and the answer was not an acceptance. Reported as
        # such: a superseded or stale outcome must never read as a handoff.
        logger.warning("handoff not accepted: outcome=%s reason=%s", receipt.outcome, receipt.reason)
        return (
            f"> **Delivery handoff not accepted** (`{receipt.outcome}`"
            + (f": {receipt.reason}" if receipt.reason else "")
            + "). Another attempt may now own this work. The engine keeps this work due rather "
            "than treating this run's exit as completion."
        )

    logger.info("handoff accepted: outcome=%s", receipt.outcome)
    return (
        f"> **Delivery handoff recorded.** Continuation receipt `{receipt.receipt_ref}` is durable; "
        "the remaining review/deployment/evaluation work stays tracked by the engine. This run's "
        "exit does not by itself complete the story."
    )

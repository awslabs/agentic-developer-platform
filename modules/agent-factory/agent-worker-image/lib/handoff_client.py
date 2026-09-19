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

## What the worker checks, and why checking the string was not enough

An earlier form of this module read acceptance off the outcome string plus a
non-empty reference. That is not a readback: a body that said ``accepted: false``
beside a recognised outcome passed, and so did a receipt for a *different* cycle,
tenant or ownership generation. Both report a handoff this run does not have, which
is the original defect wearing a receipt.

So the gateway now returns the committed continuation's **complete typed identity**
and this module validates it field by field against the fences the engine dispatched
this run with (``ADP_HANDOFF_EXPECT``, set from the trusted envelope):

* the gateway must say ``accepted: true`` positively — never inferred;
* the contract version must be one this worker field-checks, so rollout skew refuses
  rather than validating whichever subset it recognises;
* org, flow, node, cycle, accepted-plan version, claim id and claim generation must
  all be present, correctly typed, and equal to the dispatch's;
* the top-level and nested receipt references must agree — a self-contradictory body
  is not evidence;
* the action must be one this worker understands, and a next-check time must exist,
  because a continuation with no due time is not a continuation.

Anything else is ``accepted = False`` with a named mismatch. A lost response is
covered by the server side rather than here: the receipt is derived from the
execution and its fences, so a retry converges on the identical receipt and
validates identically instead of minting a second one.

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

import json
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
    "HANDOFF_EXPECT_ENV",
    "HANDOFF_RECEIPT_CONTRACT_VERSION",
    "HANDOFF_REQUIRED_ENV",
    "HandoffReceipt",
    "expected_identity",
    "handoff_note",
    "handoff_required",
    "report_handoff",
]

# Set during bootstrap from the dispatch envelope. Absent for a webhook-triggered
# run and for a legacy engine dispatch, both of which report nothing: this module is
# inert unless the engine asked for a durable handoff, so every pre-existing path
# behaves exactly as it did before it existed.
HANDOFF_REQUIRED_ENV = "ADP_HANDOFF_REQUIRED"

# The fences this run was dispatched under, as a JSON object set from the same
# trusted dispatch envelope. Absent for every pre-existing path, and absent is NOT
# permissive: with a handoff required and no expectation to compare against, there
# is nothing to distinguish a receipt for this run from a receipt for another, so
# the report refuses. That is the whole point of the readback.
HANDOFF_EXPECT_ENV = "ADP_HANDOFF_EXPECT"

# The receipt contract version this worker knows how to field-check. A server
# answering with a different version is refused rather than partially validated:
# validating the subset of fields we happen to recognise is indistinguishable from
# validating nothing, and it is the version skew during a rollout that would
# silently reintroduce the defect.
HANDOFF_RECEIPT_CONTRACT_VERSION = 1

_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})

# Every fence the receipt must state and the dispatch must agree on. Named as an
# explicit tuple, not derived from whatever keys happen to be present on either
# side: a receipt missing a field, or an expectation missing one, must be a refusal
# rather than a comparison that silently skips it.
_STRING_FENCES = ("org_id", "flow_id", "node_id", "claim_id")
_INT_FENCES = ("cycle", "accepted_plan_version", "claim_generation")

# The closed vocabulary of actions this worker understands. An action a newer server
# adds is refused here rather than guessed at — the worker cannot report "recorded"
# for an obligation whose meaning it does not know.
_KNOWN_ACTIONS = frozenset({"awaiting_review"})

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
    :attr:`accepted` is set **only** by :func:`report_handoff` after every check
    below has passed, and it is a stored field rather than a computed property so
    that "the server said yes" and "the receipt is for my work" are one decision
    made in one place instead of a predicate any caller could re-derive loosely.

    ``mismatch`` names which check failed, for the operator note. It is populated on
    every non-acceptance and empty on an acceptance.
    """

    outcome: str
    receipt_ref: str = ""
    reason: str = ""
    accepted: bool = False
    mismatch: str = ""


def handoff_required() -> bool:
    """Whether the engine asked this run to produce a durable handoff receipt.

    Defaults to **false**, so a run whose envelope carried no marker reports nothing
    and behaves exactly as it did before this module existed. This is what keeps an
    older gateway and a newer worker compatible in the safe direction.
    """
    return os.environ.get(HANDOFF_REQUIRED_ENV, "").strip().lower() in _TRUE_SPELLINGS


def expected_identity() -> dict[str, object]:
    """The fences this run was dispatched under, or ``{}`` if none were published.

    Parsed defensively and returned empty on anything unusable, because an
    unparseable expectation must produce a refusal in :func:`report_handoff` rather
    than an exception escaping into a run that has already delivered its work.
    """
    raw = os.environ.get(HANDOFF_EXPECT_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning(
            "handoff: %s is not valid JSON; the receipt cannot be checked against this dispatch",
            HANDOFF_EXPECT_ENV,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "handoff: %s is not an object; the receipt cannot be checked against this dispatch",
            HANDOFF_EXPECT_ENV,
        )
        return {}
    return parsed


def _validate_receipt(body: dict, expect: dict[str, object]) -> str:
    """Return "" if the response is a receipt for *this* dispatch, else why not.

    This is the readback the story turns on. Each check below is a way the previous
    implementation could report a handoff it did not have:

    1. ``accepted`` must be **positively** true. Reading acceptance off the outcome
       string alone meant a body that said ``accepted: false`` next to a recognised
       outcome was reported as recorded.
    2. ``contract_version`` must be the one this worker field-checks, so a rollout
       skew refuses instead of validating a subset.
    3. Every fence must be present, of the right type, and equal to the dispatch's.
       A receipt naming another cycle, another ownership generation or another
       tenant's node is another run's receipt.
    4. The receipt reference must be non-empty and the nested one must equal the
       top-level one — a body that disagrees with itself is not evidence.
    5. ``next_check_at`` must be present, because a continuation with no due time is
       not a continuation, and ``action`` must be one this worker understands.
    """
    if body.get("accepted") is not True:
        return "the gateway did not positively accept the handoff"

    receipt = body.get("receipt")
    if not isinstance(receipt, dict):
        return "the response carried no typed receipt to validate"

    if receipt.get("contract_version") != HANDOFF_RECEIPT_CONTRACT_VERSION:
        return f"receipt contract version {receipt.get('contract_version')!r} is not the version this worker validates ({HANDOFF_RECEIPT_CONTRACT_VERSION})"

    top_ref = body.get("receipt_ref")
    inner_ref = receipt.get("receipt_ref")
    if not isinstance(inner_ref, str) or not inner_ref:
        return "the typed receipt carried no receipt reference"
    if top_ref != inner_ref:
        # Self-contradiction. Whichever is right, the response is not trustworthy.
        return "the response's receipt reference disagrees with its typed receipt"

    if not expect:
        # Nothing to compare against. Refused rather than accepted on the server's
        # word alone: without the dispatch's own fences this worker cannot tell a
        # receipt for its work from a receipt for someone else's.
        return f"this run was given no dispatch identity ({HANDOFF_EXPECT_ENV}) to check the receipt against"

    for field in _STRING_FENCES:
        want, got = expect.get(field), receipt.get(field)
        if not isinstance(want, str) or not want:
            return f"the dispatch identity is missing {field}, so the receipt cannot be checked"
        if not isinstance(got, str) or got != want:
            return f"the receipt names {field}={got!r} but this run was dispatched with {want!r}"

    for field in _INT_FENCES:
        want, got = expect.get(field), receipt.get(field)
        # `bool` is an `int` in Python; excluded so `True` cannot satisfy a fence.
        if not isinstance(want, int) or isinstance(want, bool):
            return f"the dispatch identity is missing {field}, so the receipt cannot be checked"
        if not isinstance(got, int) or isinstance(got, bool) or got != want:
            return f"the receipt names {field}={got!r} but this run was dispatched with {want!r}"

    action = receipt.get("action")
    if not isinstance(action, str) or action not in _KNOWN_ACTIONS:
        return f"the receipt records action {action!r}, which this worker does not recognise"

    next_check = receipt.get("next_check_at")
    if not isinstance(next_check, str) or not next_check:
        return "the receipt asserts no next-check time, so no continuation is scheduled"

    action_id = receipt.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return "the receipt names no continuation action"

    return ""


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
    if not isinstance(result, dict):
        raise StatusGatewayError("gateway returned a handoff response that is not an object")

    outcome = result.get("outcome")
    receipt_ref = result.get("receipt_ref")
    reason = result.get("reason")
    if not isinstance(outcome, str) or not outcome:
        # A response this worker cannot interpret is not evidence of a handoff. An
        # older gateway that does not implement this route answers 404, which
        # `post_self` already raises on; this covers a 200 whose body is unusable.
        raise StatusGatewayError("gateway returned no usable handoff outcome")

    unusable = HandoffReceipt(
        outcome=outcome,
        receipt_ref="",
        reason=reason if isinstance(reason, str) else "",
    )

    if outcome not in _ACCEPTED_OUTCOMES:
        # A refusal, a supersession, or an outcome a newer server added. All three
        # are "not accepted", which is the fail-closed direction.
        return unusable

    mismatch = _validate_receipt(result, expected_identity())
    if mismatch:
        # The server answered with an accepted-looking outcome that did not survive
        # readback. `receipt_ref` is deliberately dropped: a caller must not be able
        # to quote a reference from a report that failed validation.
        logger.warning("handoff readback refused: outcome=%s reason=%s", outcome, mismatch)
        return HandoffReceipt(
            outcome=outcome, receipt_ref="", reason=unusable.reason, mismatch=mismatch
        )

    return HandoffReceipt(
        outcome=outcome,
        # Validated equal to the typed receipt's own reference above.
        receipt_ref=receipt_ref if isinstance(receipt_ref, str) else "",
        reason=unusable.reason,
        accepted=True,
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
        # The server answered, and the answer was not an acceptance — either
        # explicitly, or because the receipt it returned was not for this run's work.
        # Both are reported as unrecorded: a superseded, stale or mismatched outcome
        # must never read as a handoff.
        logger.warning(
            "handoff not accepted: outcome=%s reason=%s mismatch=%s",
            receipt.outcome,
            receipt.reason,
            receipt.mismatch,
        )
        detail = receipt.mismatch or receipt.reason
        return (
            f"> **Delivery handoff not accepted** (`{receipt.outcome}`"
            + (f": {detail}" if detail else "")
            + "). Another attempt may now own this work. The engine keeps this work due rather "
            "than treating this run's exit as completion."
        )

    logger.info("handoff accepted: outcome=%s", receipt.outcome)
    return (
        f"> **Delivery handoff recorded.** Continuation receipt `{receipt.receipt_ref}` is durable; "
        "the remaining review/deployment/evaluation work stays tracked by the engine. This run's "
        "exit does not by itself complete the story."
    )

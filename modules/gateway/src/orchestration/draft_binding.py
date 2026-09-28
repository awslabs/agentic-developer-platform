"""Server-side tenant resolution for internal-plane draft registration (Issue #4597).

## The problem this closes

`modules/agent-factory/agent-worker-image` registers a compiled AIDLC proposal by
POSTing to ``/orchestration/flows/drafts`` under the pod's IRSA identity. Every
hosted agent run on the platform authenticates as the single shared agent-registry
row ``scaledjob-worker``, whose registry ``org_id`` is the literal
``__platform__`` — which equals no real tenant. The proposal correctly declares
the tenant the run belongs to (``ADP_TENANT_ID``), and `compile_proposal`'s Gate 2
compares the document's ``org_id`` against the actor's, refusing a mismatch rather
than re-homing it. So **every** real-tenant auto-registration was refused with
``TenantMismatchError`` → 422, and the engine bridge could not complete its own
happy path.

The tenant the worker intends *is* available as ``TokenContext.attributed_org_id``
(``auth/middleware.py`` writes it from the pod's ``X-Agent-OrgId`` header). It must
not be used. The #4132 invariant, stated in as many words in
``shared/schemas/auth.py``, is that ``attributed_org_id`` is caller-influenced and
"MUST NEVER gate access" — and choosing which tenant a plan is filed into is
exactly gating access. Trusting it here would reintroduce the #4132 cross-tenant
escalation on a new surface: one header, and a plan lands in someone else's graph
where *their* approvers can accept it.

## The shape: inherited from #4337, not invented here

This is the same problem `budget/run_binding.py` solved for the per-run spend cap —
shared worker, org ``__platform__``, needs a real tenant, ``attributed_org_id``
forbidden as an authz input — and it is solved the same way, by **inverting the
direction of trust**. The run's ``webhook-events`` row is written at ingress by
webhook-ingress (``lambda/common/webhook_events.py``) *before any agent code ran*,
and its ``tenant_id`` is the authority. The caller supplies only a **reference** to
that row: the run id, in ``X-Agent-RunId``.

So the caller does not assert a tenant and is not asked to. It names a row, and the
row names the tenant. Read the module docstring of ``budget/run_binding.py`` before
changing anything here.

## Why this is safe even though the run id is not secret

The run id appears in the ``adp-invocation`` field of the correlation marker
``agent-worker-image/lib/correlation_marker.py`` prepends to every bot comment, so
it must be assumed readable by anyone who can read an ADP-touched issue. That does
not defeat the binding, because **the binding grants only the tenant its own row
names**. A borrowed run id therefore buys the borrower nothing but the borrowed
run's tenant — and a proposal declaring the borrower's own tenant then fails Gate 2
and is refused with a 422. Forging is self-defeating rather than profitable, which
is the #4337 property restated.

This is deliberately a **capability**, not a claim of identity, and it is worth
being precise about the reduction: it proves "the caller holds a live,
tenant-resolvable reference to this run", NOT "the caller owns this run". #4337
records at length why the stronger property cannot be had — the caller side of any
such comparison is the constant ``scaledjob-worker`` for every run on the platform,
so an identity check reduces to "is the caller the shared worker", which every
caller satisfies. Stating the weaker property is better than pretending the
stronger one.

Crucially, this resolves **no human at all**. It does not read ``root_human_id``,
does not touch ``user_identities``, and does not fabricate a ``TokenContext`` — so
it cannot convert the lineage plane's attribution into authority. That failure mode
is named in advance at ``shared/schemas/auth.py``: "If this is ever read as authz, a
sub-agent can act as the human who triggered it." The mechanism first proposed for
this issue would have done that; it was rejected in review for it.

## Fail closed, in every direction

`resolve_draft_tenant` returns a tenant or raises. There is no fallback, and in
particular **no fallback to** ``attributed_org_id`` — a fallback would be the whole
bypass, reachable by any caller who can make the lookup fail.

Every refusal below is a refusal, including the two that look like absences:

* a row with a blank ``tenant_id`` is ``unbindable_run``, not "skip the check".
  The pre-#4337 tenant guard was a three-way conjunction that skipped silently when
  either side was blank, and under a capability model that blank-skip **is** the
  cross-tenant bypass. Same bug, new surface, refused up front.
* a **lookup fault** (DynamoDB unreachable) is ``binding_unavailable`` → refuse.
  This is the one place this module's policy differs from `run_binding`'s, and the
  difference is deliberate: there, degrading preserves inference for a whole
  platform and the hierarchy caps still bound spend, so a DDB blip must not be an
  outage. Here the only thing at stake is one draft registration on one run, the
  worker is fail-soft (``engine_registration.draft_registration_note`` turns it into
  a warning in the closing comment and the run's real output is already committed),
  and the only available degradation would be trusting the caller's header. Refusing
  costs a retry; degrading costs the invariant.

A terminal run is refused for the reason #4337 gives: a finished run's id mints no
fresh authority, and without that check, rotating across one's own completed runs is
unbounded. Terminality is read from ``activity.liveness.OBSERVED_TERMINAL_STATUSES``
— the platform's single definition, reused rather than restated so this path and the
activity read path cannot drift about whether a run is over. An absent or
unrecognised status is **not** terminal, matching that module's "loss of contact is
not evidence of exit": denying on absent would deny every row whose writer never
advanced it.
"""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES
from src.budget.run_binding import RunBindingResolver
from src.shared.logging import get_logger

logger = get_logger(__name__)

__all__ = ["DraftBindingError", "resolve_draft_tenant"]


class DraftBindingError(Exception):
    """The asserted run id did not resolve to an owning tenant.

    Always a refusal. `code` is a stable machine-readable reason the route puts in
    the response body so an operator reading a worker's closing-comment warning can
    tell *which* fail-closed arm fired — "this run has no ingress row" and
    "DynamoDB was unreachable" are the same 403 but very different problems, and
    collapsing them to one opaque message is what makes this class of failure
    expensive to diagnose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def resolve_draft_tenant(*, run_id: str | None, resolver: RunBindingResolver) -> str:
    """Resolve the tenant a draft registration lands in, from server-written state.

    Args:
        run_id: The value of ``X-Agent-RunId`` — the envelope ``message_id``, which
            is the ``event_id`` partition key of the run's ``webhook-events`` row.
            **Not** the row's ``run_id`` attribute, which is the KEDA job/pod name
            (Issue #4348 documents both name collisions on this table; binding on
            the pod name would compare against a value no caller ever sends).
        resolver: The shared ``webhook-events`` row resolver. Reused rather than
            re-implemented so this path and the budget path cannot drift about how a
            row is found or normalized — it already does the composite-key ``Query``
            (``GetItem`` cannot work: the table's key is ``event_id`` HASH +
            ``arrived_at`` RANGE), already takes newest-first for GitHub
            re-delivery, already declines to negative-cache a miss, and already
            normalizes the fields read below.

    Returns:
        The row's server-written ``tenant_id``. Never a caller-supplied value.

    Raises:
        DraftBindingError: ``missing_run_id``, ``binding_unavailable``,
            ``unknown_run``, ``unbindable_run``, or ``terminal_run``.
    """
    asserted = (run_id or "").strip()
    if not asserted:
        # An internal-scope caller that names no run has nothing to resolve against,
        # and the only other source of a tenant would be the header #4132 forbids.
        raise DraftBindingError(
            "missing_run_id",
            "This caller must assert the run it is registering on behalf of; no X-Agent-RunId was sent.",
        )

    try:
        row = await resolver.resolve(asserted)
    except (ClientError, BotoCoreError) as exc:
        # Refuse, do not degrade. See the module docstring: the only degradation
        # available here is trusting the caller's asserted org, which is the bypass.
        logger.warning(f"Draft-binding lookup faulted for run {asserted}; refusing registration: {exc}")
        raise DraftBindingError(
            "binding_unavailable",
            f"The registry lookup for run {asserted} could not be completed; registration is refused rather than attributed on trust.",
        ) from exc

    if row is None:
        raise DraftBindingError(
            "unknown_run",
            f"Run {asserted} has no ingress record, so no owning tenant can be established for it.",
        )

    # `.strip()`, so a whitespace-only attribute is refused rather than becoming a
    # tenant. It is truthy, so an unstripped check would file plan rows under the
    # literal org id `"   "` — a tenant that exists in no membership table, whose
    # rows no human can therefore ever approve, and which reads as a successful
    # registration to the worker. That is worse than the refusal.
    tenant_id = str(row.get("tenant_id") or "").strip()
    if not tenant_id:
        # NOT "skip the check" — see the module docstring. An authority that cannot
        # be read is an authority that cannot be enforced.
        raise DraftBindingError(
            "unbindable_run",
            f"Run {asserted} carries no tenant on its ingress record; there is no authority to register against.",
        )

    status = str(row.get("status") or "")
    if status in OBSERVED_TERMINAL_STATUSES:
        raise DraftBindingError(
            "terminal_run",
            f"Run {asserted} has already finished; its id establishes no further authority.",
        )

    return tenant_id

"""Authoritative target resolution for the shared agent policy (#5028 AC3).

## Why this module is the one that decides AC3

``evaluate_grant`` refuses unauthorized siblings, ancestors, cross-flow and
cross-tenant targets — but it does so by reading
:attr:`~src.agentauth.grants.TargetFacts.relationships`. It never computes that
set. So whoever computes it decides whether those refusals actually happen: a
resolver that returned ``DESCENDANT`` for any run in the same tenant would pass
every existing policy test and still hand a coordinator control over unrelated
work. The grant confers authority; this module establishes facts. AC3's "a common
tenant or human root alone grants no control authority" is enforced here or
nowhere.

That is also why this is a separate module from ``store.py``: the store answers
"what is recorded?", which is a storage question, and this answers "how are these
two executions related?", which is an authorization question with its own
adversarial cases.

## Where the facts come from, and why not from the obvious place

Lineage is read from ``parent_principal`` on the **protected** authority table,
not from the webhook-events row's ``parent_invocation_id``. The worker role can
write that row today (``DynamoDBWebhookEventsUpdate``, no per-run key condition),
so a worker able to edit its own parent pointer could graft an unrelated run into
its own subtree and become its "ancestor". Lineage used as an authorization input
has to live where its subject cannot write it — see Decision 3 in
``docs/design/agent-delegated-authority.md``.

## The walk goes upward, from the target

Descendant resolution walks from the *target* toward the caller, following each
record's parent pointer. The opposite direction — enumerating the caller's
children and looking for the target — would need a query whose result set the
caller influences, and would have to be re-run at every level. Upward is a chain
of single point reads on records the worker cannot write, and it terminates at a
root.

The direction is also what makes two of the AC3 refusals fall out rather than
needing their own logic:

- an **ancestor** is refused because walking up from the target moves *away* from
  the caller and never reaches it;
- a **sibling** is refused because the walk from a sibling reaches the shared
  parent and then continues past it — the caller is not on that path at all.

Neither is a special case in the code below, which matters: a special case is a
branch that can be wrong, and these two are the attacks most likely to look
legitimate to a reviewer.
"""

from __future__ import annotations

import logging
from typing import Protocol

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import TargetFacts, TargetRelationship
from src.agentauth.run_credential import RunCredential

logger = logging.getLogger("bedrockgateway.agentauth.targets")

# How far up the lineage chain a descendant check will walk. A bound rather than
# an unbounded loop because the chain is data: a corrupted or maliciously written
# set of records could form a cycle, and an authorization check that can be made
# to spin is a denial-of-service on every other request sharing the process.
#
# 8 matches ``MAX_CHAIN_DEPTH`` in webhook-ingress ``spawn_persona.py``, which is
# the ceiling on how deep dispatch will actually nest. Kept equal deliberately: a
# smaller value here would refuse legitimate deep flows, and a larger one would
# only ever walk records that dispatch cannot create.
MAX_LINEAGE_DEPTH = 8

# Statuses that mean the run is over. Distinct from
# ``execution._ACTIONABLE_STATUSES``: PENDING is not actionable (nothing has been
# bound yet) but it is also not terminal, and reporting a pending run as terminal
# would tell a coordinator its child had finished before it started.
_TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset({ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED})


class ExecutionReader(Protocol):
    """The subset of the authority store this resolver needs.

    Narrower than :class:`~src.agentauth.store.AgentAuthorityStore` on purpose:
    resolution is a read-only question, and a resolver holding a handle that could
    write execution records would be able to create the lineage it then reports.
    """

    def load_execution(self, *, invocation_id: str, tenant_id: str) -> ExecutionRecord | None: ...


class GenerationReader(Protocol):
    """Reads a target's current control generation.

    Injected rather than read from the authority record because the control
    generation is assigned by the worker's own registration call (the atomic
    ``ADD control_generation`` in ``lib/invocation_status.py``), which has not yet
    migrated behind a service writer. Rather than duplicate that counter into the
    protected table — two counters that agree on the day they are written — the
    resolver asks for it and reports what it is told.
    """

    def read_generation(self, *, run_id: str, tenant_id: str) -> int | None: ...


class AuthorityTargetResolver:
    """Resolves target facts from protected state. Implements ``TargetResolver``.

    ``generation_reader`` is optional and defaults to reporting generation 0.
    That is safe *only* because generation is consumed by envelope binding, and
    every live-control verb in this deployment is refused with 501 before an
    envelope is minted (``SUPPORTED_AGENT_ACTIONS`` holds MONITOR alone). A
    deployment that enables a control verb must supply a reader; the envelope
    would otherwise bind a generation the listener will not match, which fails
    closed but is a confusing way to do so.
    """

    def __init__(
        self,
        *,
        executions: ExecutionReader,
        generation_reader: GenerationReader | None = None,
        max_depth: int = MAX_LINEAGE_DEPTH,
    ) -> None:
        self._executions = executions
        self._generations = generation_reader
        self._max_depth = max_depth

    def resolve(self, *, run_id: str, caller: RunCredential) -> TargetFacts | None:
        """Return facts about ``run_id``, or None if it does not exist for this caller.

        The target record is read **scoped to the caller's own tenant**, so a
        cross-tenant target is not found rather than found-and-refused. Both
        outcomes refuse, but not-found is the one that leaks nothing: a caller
        able to tell "exists but forbidden" from "does not exist" can enumerate
        another tenant's run IDs. ``evaluate_grant`` re-checks the tenant anyway —
        two independent checks, because this one is a lookup-key property and a
        future resolver change could weaken it without any test noticing.
        """
        if not run_id or not caller.tenant_id:
            return None

        try:
            target = self._executions.load_execution(invocation_id=run_id, tenant_id=caller.tenant_id)
        except Exception as exc:
            # Fail closed and loudly. Returning None here would be read by the
            # policy as "no such target", which is the correct refusal, but the
            # store being unreachable is an operational alarm rather than a
            # caller error and must not be silent.
            logger.error(
                "Authority store unavailable resolving target",
                extra={"run_id": run_id, "reason": str(exc)},
            )
            return None

        if target is None:
            return None
        # The store is keyed by tenant, so this should hold. Checked because "the
        # lookup returned an item" and "the item is in my tenant" are different
        # statements, and only the second may be relied on for isolation.
        if target.tenant_id != caller.tenant_id:
            logger.error(
                "Authority store returned a target outside the caller tenant",
                extra={"run_id": run_id},
            )
            return None

        relationships = self._relationships(target=target, caller=caller)

        return TargetFacts(
            run_id=target.invocation_id,
            tenant_id=target.tenant_id,
            flow_id=target.flow_id,
            relationships=relationships,
            generation=self._generation(run_id=target.invocation_id, tenant_id=target.tenant_id),
            is_terminal=target.status in _TERMINAL_STATUSES,
            repo=target.repo,
        )

    # -- relationships ----------------------------------------------------

    def _relationships(self, *, target: ExecutionRecord, caller: RunCredential) -> frozenset[TargetRelationship]:
        """Compute every relationship that authoritatively holds.

        A set, not a single value: a run can legitimately be both a descendant of
        the caller and a node in the caller's flow, and collapsing that to one
        answer would make authority depend on which the resolver happened to
        check first.
        """
        found: set[TargetRelationship] = set()

        if target.invocation_id == caller.invocation_id:
            # Self needs no lineage walk and no flow comparison. The caller's
            # attempt was already reconciled against this record's
            # ``current_attempt`` by the policy's live-execution step, so there is
            # no stale-attempt self-read to exclude here.
            return frozenset({TargetRelationship.SELF})

        if self._is_descendant(target=target, caller=caller):
            found.add(TargetRelationship.DESCENDANT)

        if self._shares_flow(target=target, caller=caller):
            found.add(TargetRelationship.FLOW_NODE)

        return frozenset(found)

    def _is_descendant(self, *, target: ExecutionRecord, caller: RunCredential) -> bool:
        """Walk up from ``target``, looking for the caller's exact principal.

        The comparison is against ``caller.principal`` — ``<invocation>#<attempt>``
        — not against the invocation ID alone. That is what stops a *previous
        attempt's* dispatch from conferring authority on a later attempt: attempt 2
        of a coordinator did not dispatch attempt 1's children, and treating them
        as its own would let a retried pod inherit control over work it never
        started.

        Intermediate records are looked up by invocation ID, and an attempt
        mismatch there is deliberately *not* fatal: ancestry is a property of
        invocations, so a parent that has since moved to a new attempt is still
        the same parent. Only the final caller comparison is attempt-exact.
        """
        seen: set[str] = {target.invocation_id}
        pointer = target.parent_principal
        depth = 0

        while pointer:
            if pointer == caller.principal:
                return True

            depth += 1
            if depth >= self._max_depth:
                # Refuse rather than continue. A chain longer than dispatch can
                # create is corrupt data, and "I ran out of budget" must not
                # become "the relationship holds".
                logger.warning(
                    "Lineage walk exceeded the depth bound; refusing descendant claim",
                    extra={"target_run_id": target.invocation_id, "depth": depth},
                )
                return False

            parent_invocation = _invocation_of(pointer)
            if not parent_invocation or parent_invocation in seen:
                # A cycle, or a pointer that is not a parseable principal. Both are
                # corrupt lineage, and neither establishes anything.
                if parent_invocation in seen:
                    logger.warning(
                        "Cycle in lineage chain; refusing descendant claim",
                        extra={"target_run_id": target.invocation_id},
                    )
                return False
            seen.add(parent_invocation)

            try:
                parent = self._executions.load_execution(
                    invocation_id=parent_invocation,
                    tenant_id=target.tenant_id,
                )
            except Exception as exc:
                logger.error(
                    "Authority store unavailable walking lineage",
                    extra={"target_run_id": target.invocation_id, "reason": str(exc)},
                )
                return False

            if parent is None:
                # A missing link breaks the chain. Fail closed: the caller might
                # genuinely be further up, but an unprovable relationship is not a
                # relationship. Skipping the gap and continuing from a guess is how
                # a deleted record would become an authority bypass.
                logger.warning(
                    "Lineage chain broken by a missing record; refusing descendant claim",
                    extra={"target_run_id": target.invocation_id, "missing": parent_invocation},
                )
                return False

            pointer = parent.parent_principal

        # Reached a root without meeting the caller. This is the path an ancestor
        # and an unrelated sibling both take.
        return False

    def _shares_flow(self, *, target: ExecutionRecord, caller: RunCredential) -> bool:
        """Report whether both executions belong to the same flow.

        A **fact**, not a grant. Sharing a flow authorizes nothing on its own:
        ``evaluate_grant`` still requires ``FLOW_NODE`` to be listed in the
        grant's ``target_relationships`` and, for a flow-scoped grant, still
        compares the target's flow against the grant's. This method existing is
        what makes coordinator-to-flow-node dispatch expressible; the grant is
        what makes it authorized.

        The caller's flow is read from the caller's own **protected record**, not
        from ``caller.flow_id`` on the credential. The credential's value was true
        when it was minted, and a long-lived credential outlives changes to it;
        the store's value is current. A missing flow on either side yields False —
        "no flow" is never "any flow".
        """
        if not target.flow_id:
            return False

        try:
            own = self._executions.load_execution(
                invocation_id=caller.invocation_id,
                tenant_id=caller.tenant_id,
            )
        except Exception as exc:
            logger.error(
                "Authority store unavailable reading caller flow",
                extra={"invocation_id": caller.invocation_id, "reason": str(exc)},
            )
            return False

        if own is None or not own.flow_id:
            return False
        return own.flow_id == target.flow_id

    # -- generation -------------------------------------------------------

    def _generation(self, *, run_id: str, tenant_id: str) -> int:
        if self._generations is None:
            return 0
        try:
            generation = self._generations.read_generation(run_id=run_id, tenant_id=tenant_id)
        except Exception as exc:
            logger.warning(
                "Could not read target control generation",
                extra={"run_id": run_id, "reason": str(exc)},
            )
            return 0
        # A negative or non-integer generation is reported as 0 rather than passed
        # through: 0 matches no registered generation, so a nonsense value refuses
        # at the listener instead of binding an envelope to an arbitrary number.
        if type(generation) is not int or generation < 0:
            return 0
        return generation


def _invocation_of(principal: str) -> str:
    """Extract the invocation ID from ``<invocation_id>#<attempt>``.

    Returns "" for anything that is not that shape, including a bare invocation ID
    with no attempt. Strict because a principal is compared for equality
    elsewhere: accepting a loose form here would create two spellings of one
    identity, and the looser one would not match the credential's.
    """
    invocation, separator, attempt = principal.partition("#")
    if not separator or not invocation or not attempt.isdigit():
        return ""
    return invocation

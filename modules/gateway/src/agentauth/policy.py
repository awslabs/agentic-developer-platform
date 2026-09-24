"""One authorization policy for agent spawn and agent control (#5028).

## Why one policy and not two adapters

``control_service.py`` already argues this for the two *human* control adapters:
a second implementation of "may this caller do this?" is two checks that agree
on the day they are written. The same applies with more force here, because the
agent-facing spawn path and the agent-facing control path have different shapes
(one dispatches a new run, one forwards a command to a pod) but exactly one
authorization question. So :class:`AgentAuthorizationService` is the only place
that answers it, and both adapters call :meth:`authorize`.

## The six steps, and why they are in this order

1. **Resolve the caller from its credential.** Not from the body, not from a
   header, not from the IAM role. This is the step that makes the other five
   meaningful — everything below is scoped by an identity the caller could not
   choose.
2. **Resolve authority and current revocation** from the authority store.
3. **Resolve the target independently** — its tenant, flow, generation and its
   relationship to the caller. The caller supplies a target ID and nothing else.
4. **Check action and relationship** against the grant (:func:`evaluate_grant`).
5. **Enforce budgets, depth and concurrency**, then record the decision.
6. **Hand back a decision** the adapter turns into a dispatch, a forward, or a
   refusal.

Revocation is re-checked here on *every* request rather than relying on the
envelope signature, which is what keeps the maximum revocation delay bounded by
the envelope TTL rather than by the grant's remaining lifetime.

## Storage is injected, not chosen here

:class:`GrantStore` and :class:`TargetResolver` are protocols. The authority
store's concrete backing must satisfy one property that the current
webhook-events table does **not**: no worker write path. The shipped
implementation and that property are documented in
``docs/design/agent-delegated-authority.md``; this module deliberately does not
hardcode a table so the protected store can be introduced without rewriting the
policy.

## What this module does not do

It does not implement control behaviour — pause/resume live in
``activity/control_service.py`` and abort's acceptance in
``agentauth/revalidation.py``. This module only decides whether a caller may ask.
An authorized request for a verb this deployment does not implement (STEER)
returns :data:`UNSUPPORTED_STATUS` (501) *after* authorization, preserving the
existing ladder. It does not enable any flag: ``FEATURE_AGENT_CONTROL_ENABLED``
and ``AGENT_AUTHORITY_ENABLED`` still gate the route regardless of what
:data:`SUPPORTED_AGENT_ACTIONS` contains.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from src.agentauth.execution import (
    ExecutionRecord,
    ExecutionStateError,
    evaluate_execution_state,
)
from src.agentauth.grants import (
    LIVE_CONTROL_ACTIONS,
    AgentAction,
    AuthorizationDecision,
    DelegatedGrant,
    TargetFacts,
    evaluate_grant,
)
from src.agentauth.run_credential import CredentialError, RunCredential, verify_credential

logger = logging.getLogger("bedrockgateway.agentauth.policy")

# Live controls with a signed forwarding and pre-delivery revalidation path.
# This set stays in lockstep with the human service and worker runtime (#5222).
#
# ABORT joins the set with #3963 (S4), which supplied the three things the verb
# was missing and that PAUSE/RESUME already had: a worker-side implementation
# (``IMPLEMENTED_CONTROL_VERBS`` in control-runtime.ts), a revalidation path
# that records durable abort intent *before* minting an acceptance receipt
# (``revalidation._accept_abort``), and a terminal outcome the status vocabulary
# can express (``aborted``, #3964 S5). Enabling it earlier would have advertised
# a verb with no transport behind it; the human service's ``SUPPORTED_ACTIONS``
# already carries ``abort`` for the same reason, so leaving it out here is now
# the drift rather than the safe default.
#
# STEER stays out. It has no revalidation branch and no worker verb, so an
# authorized STEER still lands on the 501 in ``require_supported``.
SUPPORTED_AGENT_ACTIONS: frozenset[AgentAction] = frozenset(
    {AgentAction.MONITOR, AgentAction.PAUSE, AgentAction.RESUME, AgentAction.ABORT}
)

# Status codes this policy produces. Named so the adapters cannot drift.
REFUSED_STATUS = 404
UNSUPPORTED_STATUS = 501
THROTTLED_STATUS = 429


class PolicyError(Exception):
    """A request was refused. ``status_code`` is what the adapter returns.

    ``audit_reason`` is the reason recorded in the decision log, which is
    deliberately separate from ``detail``: ``detail`` is what the caller may see
    and is usually the opaque "not found", while ``audit_reason`` is the specific
    cause and stays internal. Collapsing the two would either leak the cause to
    the caller or lose it from the audit trail.
    """

    def __init__(self, status_code: int, detail: str, *, audit_reason: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.audit_reason = audit_reason or detail


class ExecutionStore(Protocol):
    """Reads authoritative execution state from the protected authority store.

    Separate from :class:`GrantStore` because the two answer different questions —
    "is this execution still the live one?" versus "what may it do?" — and because
    a deployment could back them differently. The shipped
    :class:`~src.agentauth.store.AgentAuthorityStore` implements both.
    """

    def load_execution(self, *, invocation_id: str, tenant_id: str) -> ExecutionRecord | None:
        """Return current execution state, or None if the store has no record.

        None must not be read as "unrestricted". The policy fails closed on it.
        """
        ...


class GrantStore(Protocol):
    """Reads delegated grants from the protected authority store.

    Read-only by design: nothing in the request path writes a grant. Grants are
    written by trusted dispatch when a human authorization is turned into
    delegated authority, which is a different code path with different
    permissions.
    """

    def load_grant(self, *, principal: str, tenant_id: str) -> DelegatedGrant | None:
        """Return the live grant for this execution identity, or None.

        ``principal`` is ``"<invocation_id>#<attempt>"`` taken from the verified
        credential — never from request data.
        """
        ...

    def active_dispatch_count(self, *, grant_id: str, tenant_id: str) -> int:
        """How many runs this grant currently has in flight, for concurrency.

        ``tenant_id`` is required, not optional, because the protected store
        partitions by tenant: a backend that had to build a key without it could
        only return a default, and the safe-looking default for a count is ``0``,
        which silently disables the ceiling. A limit that fails open under a
        missing argument is worse than no limit, because it reads as enforced.
        """
        ...


class TargetResolver(Protocol):
    """Resolves what is true about a target run, independently of the caller."""

    def resolve(self, *, run_id: str, caller: RunCredential) -> TargetFacts | None:
        """Return resolved facts about ``run_id``, or None if it does not exist.

        The ``relationships`` field on the result must be computed from
        authoritative lineage/flow state. A resolver that echoed a
        caller-asserted relationship would defeat the whole policy.
        """
        ...


@dataclass(frozen=True)
class AuthorizedRequest:
    """An allowed request, carrying everything the adapter needs to act.

    The adapter receives the *resolved* target rather than the ID it was given,
    so it cannot accidentally act on the caller's version of the target.

    ``grant`` is optional for exactly one reason: a run reading its own status is
    authorized by :data:`~src.agentauth.grants._IMPLICIT_SELF_ACTIONS` and has no
    grant to carry. It is never None on a path that mutates a target — only
    :meth:`AgentAuthorizationService.authorize_read` can produce a grantless
    result, and that method cannot express a mutating action.
    """

    credential: RunCredential
    grant: DelegatedGrant | None
    action: AgentAction
    target: TargetFacts
    decision: AuthorizationDecision
    # Current protected state for the *caller's own* execution, as validated in
    # step 1b. Carried so an adapter binds an envelope to state the store
    # confirmed live rather than to whatever the credential happened to claim.
    execution: ExecutionRecord | None = None


class AgentAuthorizationService:
    """The single policy both agent-facing adapters call."""

    def __init__(
        self,
        *,
        grant_store: GrantStore,
        target_resolver: TargetResolver,
        execution_store: ExecutionStore,
        now: Callable[[], datetime] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._grants = grant_store
        self._targets = target_resolver
        # Required, not optional. An optional execution store would mean the
        # liveness check silently degrades to the credential's own claims wherever
        # a caller forgot to pass one — and "signature plus expiry" is exactly
        # what this store exists to stop being sufficient.
        self._executions = execution_store
        self._now = now or (lambda: datetime.now(UTC))
        self._env = env

    # -- step 1 -----------------------------------------------------------

    def resolve_caller(self, credential_token: str) -> RunCredential:
        """Authenticate the individual invocation and attempt (AC2).

        The transport-level SigV4 check has already happened by the time this
        runs, and it is not sufficient: it authenticates the shared worker role.
        This authenticates the *run*.
        """
        try:
            return verify_credential(credential_token, now=self._now(), env=self._env)
        except CredentialError as exc:
            # Deliberately the same refusal as an unauthorized target. A caller
            # who can distinguish "your credential is bad" from "that target
            # isn't yours" learns whether the target exists.
            logger.warning("Agent caller credential rejected", extra={"reason": str(exc)})
            raise PolicyError(REFUSED_STATUS, "not found") from exc

    # -- step 1b ----------------------------------------------------------

    def resolve_live_execution(
        self,
        caller: RunCredential,
        *,
        presented_workload_binding: str | None = None,
    ) -> ExecutionRecord:
        """Confirm the caller's execution is still the live, current one (AC6).

        :meth:`resolve_caller` proves the credential is genuine and unexpired.
        That is not the same as the execution still being current, and the gap is
        wide enough to matter: an attempt-1 pod holding a valid credential keeps
        authorizing for the credential's full TTL after attempt 2 supersedes it,
        a rotated-past credential epoch stays cryptographically valid, and a
        cancelled flow's credential does not change when the flow is cancelled.

        So this compares the credential against protected state and refuses a
        superseded attempt, a superseded credential epoch outside the bounded
        rotation overlap, a non-active execution, and a workload binding that
        disagrees. A missing record is refused too — see
        :func:`~src.agentauth.execution.evaluate_execution_state`.
        """
        try:
            record = self._executions.load_execution(
                invocation_id=caller.invocation_id,
                tenant_id=caller.tenant_id,
            )
        except Exception as exc:
            # The authority store is unavailable. Fail closed and say so in the
            # log: this is an operational alarm, not a caller error, and it must
            # not degrade to "allow because we could not check".
            logger.error(
                "Authority store unavailable resolving execution state",
                extra={"invocation_id": caller.invocation_id, "reason": str(exc)},
            )
            raise PolicyError(REFUSED_STATUS, "not found", audit_reason="execution_store_unavailable") from exc

        try:
            return evaluate_execution_state(
                record=record,
                invocation_id=caller.invocation_id,
                attempt=caller.attempt,
                tenant_id=caller.tenant_id,
                credential_epoch=caller.credential_epoch,
                now=self._now(),
                presented_workload_binding=presented_workload_binding,
            )
        except ExecutionStateError as exc:
            # The reason reaches the audit record; the caller gets the same opaque
            # refusal as every other failure on this path.
            logger.warning(
                "Agent caller execution state rejected",
                extra={"invocation_id": caller.invocation_id, "reason": exc.reason},
            )
            raise PolicyError(REFUSED_STATUS, "not found", audit_reason=exc.reason) from exc

    # -- steps 2-5 --------------------------------------------------------

    def authorize(
        self,
        *,
        credential_token: str,
        action: AgentAction,
        target_run_id: str,
        presented_workload_binding: str | None = None,
        dispatch_replay: bool = False,
    ) -> AuthorizedRequest:
        """Authorize one agent request that requires a delegated grant.

        This is the entry point for anything that acts on a target: dispatch and
        every live-control verb. A caller with no grant is refused even if the
        target happens to be itself, because ``permits_action`` is the only thing
        that can confer a mutating action and there is nothing to ask without a
        grant.

        Reads use :meth:`authorize_read` instead.
        """
        authorized = self._authorize(
            credential_token=credential_token,
            action=action,
            target_run_id=target_run_id,
            presented_workload_binding=presented_workload_binding,
            dispatch_replay=dispatch_replay,
        )
        if authorized.grant is None:
            # Reachable only if a caller routed a mutating action through the
            # implicit self-monitor path. Refused rather than proceeding with no
            # authority object — an action with no grant behind it has no
            # revocation epoch, no authority reference and nothing to audit
            # against, so it must not produce an envelope.
            logger.warning(
                "Refusing a grantless request on the granted path",
                extra={"action": action.value, "principal": authorized.credential.principal},
            )
            raise PolicyError(REFUSED_STATUS, "not found")
        return authorized

    def authorize_read(
        self,
        *,
        credential_token: str,
        target_run_id: str,
        presented_workload_binding: str | None = None,
    ) -> AuthorizedRequest:
        """Authorize a status read, which may be grantless (AC1).

        A separate method rather than a flag on :meth:`authorize`, because the
        difference between the two is *which actions are expressible*: this one
        hardcodes ``MONITOR`` and therefore cannot be talked into authorizing a
        mutation without a grant. A boolean parameter would put that distinction
        in the caller's hands, and the caller is the adapter — one wrong argument
        away from a grantless abort.

        The grantless case is narrow and safe: a run reading its own state
        escalates nothing, and requiring a provisioned grant for it would mean
        every ordinary run needed one before it could see itself.
        """
        return self._authorize(
            credential_token=credential_token,
            action=AgentAction.MONITOR,
            target_run_id=target_run_id,
            presented_workload_binding=presented_workload_binding,
        )

    def _authorize(
        self,
        *,
        credential_token: str,
        action: AgentAction,
        target_run_id: str,
        presented_workload_binding: str | None = None,
        dispatch_replay: bool = False,
    ) -> AuthorizedRequest:
        """The six steps. Raises :class:`PolicyError` on refusal.

        Every refusal below is recorded before it is raised, so the audit trail
        contains rejected attempts and not only successful ones (AC7).
        """
        caller = self.resolve_caller(credential_token)

        if not target_run_id:
            raise PolicyError(REFUSED_STATUS, "not found")

        # Step 1b: is the caller's own execution still live? Before the grant and
        # target lookups, because a superseded or cancelled execution has no
        # business consuming either — and because an audit record naming the
        # target of a request from a dead attempt would imply that target was
        # meaningfully considered.
        try:
            execution = self.resolve_live_execution(caller, presented_workload_binding=presented_workload_binding)
        except PolicyError as exc:
            # Recorded with the caller and action it claimed. The target is
            # recorded as supplied, marked by the reason as unresolved — the
            # resolver never ran, so this is the honest shape.
            self._record(
                AuthorizationDecision(
                    allowed=False,
                    principal=caller.principal,
                    action=action,
                    target_run_id=target_run_id,
                    tenant_id=caller.tenant_id,
                    reason=exc.audit_reason,
                )
            )
            raise

        # Step 2: authority and current revocation state, read fresh. Not cached
        # from an earlier request in this run — a cached grant is a grant that
        # outlives its revocation.
        grant = self._grants.load_grant(principal=caller.principal, tenant_id=caller.tenant_id)

        # Step 3: the target, resolved from authoritative state.
        target = self._targets.resolve(run_id=target_run_id, caller=caller)
        if target is None:
            self._record(
                AuthorizationDecision(
                    allowed=False,
                    principal=caller.principal,
                    action=action,
                    target_run_id=target_run_id,
                    tenant_id=caller.tenant_id,
                    reason="target_not_found",
                )
            )
            raise PolicyError(REFUSED_STATUS, "not found")

        # Step 4: the grant check.
        decision = evaluate_grant(
            grant=grant,
            action=action,
            target=target,
            caller_tenant_id=caller.tenant_id,
            caller_principal=caller.principal,
            now=self._now(),
        )
        if not decision.allowed:
            self._record(decision)
            raise PolicyError(REFUSED_STATUS, "not found")

        # Step 5: ceilings. After the grant check, so a caller with no authority
        # cannot probe another flow's concurrency by watching for a 429. Skipped
        # for the grantless self-read: there is no grant to read a ceiling from,
        # and a run reading its own state consumes no dispatch budget.
        #
        # The decision is recorded AFTER this, not before, because a limit refusal
        # is the request's actual outcome. Recording the grant check's `allowed`
        # verdict first meant a throttled dispatch left an audit trail whose only
        # entry said `allowed=true` while the caller received 429 and nothing was
        # dispatched — an audit record contradicting the effect. The grant verdict
        # is a step, not the outcome, and AC7 asks for the outcome.
        if grant is not None:
            try:
                self._enforce_limits(grant=grant, action=action, caller=caller, dispatch_replay=dispatch_replay)
            except PolicyError as exc:
                # Same caller, authority, target and action as the allowed
                # decision — only the outcome and reason differ, so the refusal
                # stays attributable rather than becoming an anonymous 429.
                self._record(replace(decision, allowed=False, reason=exc.audit_reason))
                raise

        self._record(decision)

        return AuthorizedRequest(
            credential=caller,
            grant=grant,
            action=action,
            target=target,
            decision=decision,
            execution=execution,
        )

    def require_supported(self, action: AgentAction) -> None:
        """Refuse an unimplemented verb with 501, *after* authorization.

        Called by the adapter once ``authorize`` has succeeded. Order matters and
        matches the existing human path: an unauthorized caller must not learn
        which verbs this deployment implements, and an authorized caller asking
        for a verb that does not exist deserves the honest answer rather than a
        silent success. STEER is the verb that still lands here; MONITOR,
        PAUSE, RESUME and ABORT pass.
        """
        if action not in SUPPORTED_AGENT_ACTIONS:
            raise PolicyError(UNSUPPORTED_STATUS, f"{action.value} is not implemented in this deployment")

    def _enforce_limits(
        self,
        *,
        grant: DelegatedGrant,
        action: AgentAction,
        caller: RunCredential,
        dispatch_replay: bool = False,
    ) -> None:
        """Budgets, depth and concurrency — bounds the caller cannot raise.

        Each refusal carries its own ``audit_reason`` so the record written by
        ``_authorize`` names which ceiling stopped the request, rather than
        logging a generic failure the operator then has to correlate by hand.
        """
        if action is AgentAction.DISPATCH:
            if grant.max_dispatch_concurrency <= 0:
                # A grant conveying DISPATCH with no concurrency budget cannot
                # dispatch anything. 404 rather than 429 because retrying will
                # never succeed — this is a misprovisioned grant, not congestion.
                raise PolicyError(REFUSED_STATUS, "not found", audit_reason="no_dispatch_budget")
            # Only the dispatch service sets this after matching the protected
            # reservation to the exact intent. Reading an existing outcome takes
            # no additional slot; every new reservation still checks atomically.
            if dispatch_replay:
                return
            in_flight = self._grants.active_dispatch_count(grant_id=grant.grant_id, tenant_id=grant.tenant_id)
            if in_flight >= grant.max_dispatch_concurrency:
                # 429, not 404: the caller IS authorized, so telling it to retry
                # later leaks nothing and is the actionable answer.
                raise PolicyError(
                    THROTTLED_STATUS,
                    "dispatch concurrency limit reached for this grant",
                    audit_reason="dispatch_concurrency_exceeded",
                )
            # NOTE: this is a read-then-check, which two concurrent callers can
            # both pass at the ceiling. It is not the enforcement point — the
            # actual spawn path must claim its slot with
            # ``AgentAuthorityStore.reserve_dispatch``, passing a reservation ID
            # derived from the unit of work being dispatched. That method writes an
            # identified reservation record and the counter in one transaction, so
            # neither a concurrent claim nor a duplicate release can push the real
            # in-flight count past the ceiling.
            #
            # This read stays because it produces the correct 429 and audit reason
            # for the overwhelmingly common sequential case; it is a fast path in
            # front of the atomic claim, not a substitute for it. The spawn path is
            # not wired yet (see the pending list in
            # docs/design/agent-delegated-authority.md), so the reservation
            # primitive currently has no caller besides its tests. When it is
            # wired, the claim must happen on the dispatch path itself — moving
            # this read earlier or trusting its result instead of reserving would
            # reintroduce the race it explicitly does not close.

    def revalidate_epoch(self, *, grant_id: str, tenant_id: str, principal: str, envelope_epoch: int) -> bool:
        """Re-check a queued action's authority before it takes effect (AC6).

        A signature cannot express revocation, so an action that sat in a queue
        must ask again. Returns False when the grant is gone, revoked, or has
        moved to a later epoch than the envelope was issued under.

        A False result must leave a truthful recorded outcome and must NOT apply
        the pending action — the caller is responsible for that, and the
        listener's journal records it as refused rather than as applied.
        """
        grant = self._grants.load_grant(principal=principal, tenant_id=tenant_id)
        if grant is None or grant.grant_id != grant_id:
            return False
        if not grant.is_live(self._now()):
            return False
        return grant.revocation_epoch == envelope_epoch

    def _record(self, decision: AuthorizationDecision) -> None:
        """Emit the auditable decision record.

        Structured log fields rather than free text so the record is queryable,
        and :meth:`AuthorizationDecision.to_log_fields` is the only serializer so
        no caller can log the parts it should not.
        """
        logger.info(
            "agent authorization decision",
            extra={"agent_authorization": decision.to_log_fields()},
        )


def is_live_control(action: AgentAction) -> bool:
    """Whether this action mutates a running agent.

    Used by adapters to decide whether an envelope must be signed at all — a
    monitor read needs no worker-side command authorization.
    """
    return action in LIVE_CONTROL_ACTIONS

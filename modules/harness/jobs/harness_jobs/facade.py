"""The operation facade: the surface consumers already declare.

Issue #5525 (w6-02), EPIC #4910, Wave 6. Implements the `operation_facade` port from
#5524's registry (`superplane_contracts.integration`, owner `harness_jobs`).

## Why the shape is not ours to choose

`OperationFacade` is declared twice in the domain app -- at
`src/superplane-api/app/services/provisioning.py:226` and at
`contracts/superplane_contracts/provisioning_adapter.py:83` -- and #5524's
`test_integration_contract.py` records that duplication as a known finding. This
class satisfies the *service* spelling (`open_operation` / `report_progress`), which
is the one the provisioning path calls.

Matching it exactly is the point: when #5535 (w6-12) composes this facade into the
API, it should install an object and change no call site. A facade that offered a
nicer signature would require the consumer to change, and a consumer changed to suit
its supplier is how a port stops being a port.

## What the port contract obliges, restated as code

From the registry entry:

* **binds** `operation_id`, `workspace_id`, `org_id` -- all three on every record.
* **permission** `workspace:provision` -- checked against the *resolved* principal.
* **acts as** "the facade's own resolved principal, never the request body's
  `org_id`". The `org_id` argument in `open_operation` is a value the API boundary
  extracted from a verified token, and this facade uses its own resolved principal
  regardless; see `_resolve` below for why the argument is checked for *agreement*
  rather than trusted.
* **unknown answer** raise unavailable. `OperationUnavailable` is raised when the
  store cannot be reached or is not installed -- never a `None` that a caller could
  read as "no such operation", and never a fabricated progress report.

## Why there is no composition here

This module constructs no connection and reads no configuration. `connect` is a
callable the composer supplies. A facade that could build its own pool would be a
second composition root, and whether this port is installed would become a property
of this file rather than of the reviewed startup sequence that installs it -- the
same reason #5524's registry refuses to hand back implementations.

## Why this facade goes through the admission gate (#5526, w6-03)

`open_operation` calls `admission.admit_operation`, not `store.admit`. An earlier
revision called the store directly, and the consequence was reproduced against a real
database: the port that the provisioning path actually calls admitted operations with no
approval and no reservation at all, so the gate protected only callers who chose to use
it. A control that the maintained entry point routes around is not a control.

The port's signature is fixed by the consumer and has nowhere to put an approval, so the
approval arrives through an injected `ApprovalSource` and the reservation through an
injected `BudgetLedger`. **Both are required to construct this service.** Not optional
with a permissive default, and not checked at call time: a facade that could be built
without them would be one whose safety depended on how the composer happened to call it,
and "is the gate installed" would again be a property of a call site rather than of the
type. `__post_init__` refuses instead, so a misconfigured facade fails at startup --
where a deployment notices -- rather than by admitting unpaid work at request time.

The `ApprovalSource` is a Protocol for the same reason `BudgetLedger` is: *where* an
approval record comes from (a HITL ticket store, a policy engine) is not this package's
question, and shipping an implementation would be shipping a second answer to it. A
source that returns no record is a refusal, because absence is not permission.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .admission import BudgetDenied, BudgetLedger, BudgetUnavailable, admit_operation
from .approval import (
    ApprovalRecord,
    ApprovalRefused,
    ApproverStatus,
    SpendEnvelope,
)
from .identity import REQUIRED_PERMISSION as _REQUIRED_PERMISSION
from .identity import (
    ContractViolation,
    OperationRefused,
    OperationRequest,
    OperationState,
    ResolvedPrincipal,
    payload_digest,
)
from .schema import SchemaMismatch
from .store import Connection, OperationStore

__all__ = [
    "PORT_REFUSAL_NAMES",
    "ApprovalContext",
    "ApprovalSource",
    "OperationFacadeService",
    "OperationProgress",
    "OperationUnavailable",
    "PrincipalResolver",
]

# How this package's refusals map onto the names #5524's registry declares for the
# `operation_facade` port (`refusal_exceptions=("ProvisioningUnavailable",
# "ProvisioningRefused")`).
#
# The registry matches declared names against the *raised* exception's MRO
# (`conformance.py:962`), and those names belong to `app.services.provisioning` --
# which this package must not import, for the reason the registry itself gives for
# holding names rather than classes. So `OperationUnavailable` cannot match by name,
# and an adapter at the composition seam has to translate.
#
# Published here rather than left for #5535 (w6-12) to infer, because the failure mode
# of an un-translated raise is specific and bad: the conformance suite sees an
# exception it did not declare and reads it as "an adapter with a typo raised
# AttributeError" -- a crash, not this port's answer. That is exactly the manufactured
# evidence the registry exists to prevent, arriving through the gap between two
# correct halves.
#
# The mapping is data, not behaviour: this module raises its own types, and the
# composer's thin adapter re-raises as the declared ones. A translation living here
# would require the import this package refuses.
#
# The gate's four answers are here too (#5526, w6-03), because `open_operation` now
# admits through `admit_operation` and can therefore raise them at the port boundary.
# They collapse onto the same two declared names -- an approval refusal and a budget
# denial are both "refused" to a consumer, and an unanswerable ledger is "unavailable"
# -- but each needs its own entry, because the registry matches the raised type's MRO
# and `ApprovalRefused` does not inherit from `OperationRefused`. Leaving them out would
# let a denied spend reach the conformance suite as an undeclared exception.
PORT_REFUSAL_NAMES: dict[str, str] = {
    "OperationUnavailable": "ProvisioningUnavailable",
    "OperationRefused": "ProvisioningRefused",
    "ApprovalRefused": "ProvisioningRefused",
    "BudgetDenied": "ProvisioningRefused",
    "BudgetUnavailable": "ProvisioningUnavailable",
}


# One message for every backend-availability refusal, so the three points where a
# connection can fail (construct, enter, query) are indistinguishable to a caller. They
# should be: the caller's response is identical, and a message that leaked which stage
# failed would describe the store's internals to whoever asked.
_UNREACHABLE = (
    "the operation store is unreachable; no operation outcome can be established"
)


class OperationUnavailable(RuntimeError):
    """No operation record could be reached.

    The port's mandated unknown answer: *raise unavailable*. A `RuntimeError` because
    nothing about the caller's request is wrong -- the store is unreachable or not
    installed.

    Named to match `ProvisioningUnavailable` in the consumer
    (`services/provisioning.py`), which is the exception its refusal path already
    expects, so a consumer catching that shape catches this.

    Critically NOT a `None` return and NOT a synthesized "unknown" progress report. A
    `None` would be indistinguishable from "no such operation", and a synthesized
    report would be this facade making a claim about an operation it could not read --
    which is the "reports success while the provider has provisioned nothing" failure
    the whole arrangement exists to prevent.
    """


@dataclass(frozen=True)
class OperationProgress:
    """Progress for one operation, as reported *through* the facade.

    Field-for-field compatible with `OperationProgress` in
    `services/provisioning.py`, including the three derived properties, so the
    consumer's existing checks work unchanged.

    Frozen, and the facade holds no status attribute of its own. A mutable status on
    the service is what a caller or a test reads instead of asking the store.
    """

    operation_id: str
    state: str
    detail: str | None = None

    @property
    def is_terminal(self) -> bool:
        """True when the facade will report no further change."""
        from .identity import TERMINAL_STATES

        return self.state in {member.value for member in TERMINAL_STATES}

    @property
    def is_conclusive_success(self) -> bool:
        """True only for a reported success. ``unknown`` is not success."""
        return self.state == OperationState.SUCCEEDED.value

    @property
    def is_conclusive_failure(self) -> bool:
        """True only for a reported failure. ``unknown`` is **not** a failure."""
        return self.state in (
            OperationState.FAILED.value,
            OperationState.CANCELLED.value,
        )


class PrincipalResolver(Protocol):
    """Resolves the acting principal from the authenticated context.

    Supplied by whatever composes this facade, because *how* identity is established
    is an authentication concern this package must not reimplement. The obligation
    the port places on it: the returned principal reflects the verified request
    context, never a request body.

    Takes the boundary-extracted `org_id`/`workspace_id` so an implementation can
    confirm the authenticated principal is entitled to act on them -- the arguments
    are an assertion to be checked, not a source of authority.
    """

    async def resolve(
        self, *, org_id: str, workspace_id: str, permission: str
    ) -> ResolvedPrincipal | None: ...


@dataclass(frozen=True)
class ApprovalContext:
    """Everything the admission gate needs about authority, for one request.

    A single return value rather than three, because the three are only meaningful
    together: an approval record checked against a different request's envelope, or
    against approver statuses read at a different time, is not the check the gate
    specifies. Bundling them means a source cannot supply two of the three and leave the
    gate to default the rest.

    ``record`` is optional because "there is no approval" is an answer a source must be
    able to give. It produces a refusal -- absence is not permission -- and the gate is
    where that is decided, not here.

    ``approver_statuses`` is the *current* authority of each approver, which is why
    it is supplied per request rather than stored on the record: an approver who has
    since lost the permission or left the workspace must not still be able to authorize
    a spend, and that is a fact about now rather than about when the approval was
    decided.
    """

    record: ApprovalRecord | None
    requested_envelope: SpendEnvelope
    approver_statuses: dict[str, ApproverStatus]


class ApprovalSource(Protocol):
    """Where an approval for one request comes from.

    A `Protocol`, and this package implements none of it, for the reason
    `BudgetLedger` is one: whether a request is approved is adjudicated elsewhere (a
    HITL ticket store, a policy engine), and a concrete implementation here would be a
    second authority that could disagree with the real one.

    Takes the *resolved* principal and the request, so an implementation looks up an
    approval for the identity the facade established rather than one the caller named.
    Returning `None` inside the context (`ApprovalContext.record is None`) is permitted
    and means "no approval"; the gate refuses on it.
    """

    async def approval_for(
        self, *, principal: ResolvedPrincipal, request: OperationRequest
    ) -> ApprovalContext: ...


@dataclass
class OperationFacadeService:
    """Durable create/get/status for operations, as consumers call it.

    ``connect`` returns an async context manager yielding a connection. A callable
    rather than a pool so this class never owns connection lifecycle -- see the module
    docstring on composition.

    ``approvals`` and ``ledger`` are **required**, and `__post_init__` refuses without
    them. See the module docstring: this facade admits through the approval gate, and a
    constructor that tolerated their absence would make the gate optional again.
    """

    connect: Callable[[], AbstractAsyncContextManager[Connection]]
    resolver: PrincipalResolver
    approvals: ApprovalSource = None  # type: ignore[assignment]
    ledger: BudgetLedger = None  # type: ignore[assignment]
    store: OperationStore = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = OperationStore()
        # Refused at construction rather than defaulted, and rather than checked in
        # `open_operation`. A default would be this package supplying a budget authority
        # it does not own; a call-time check would let a misconfigured facade pass a
        # startup probe and fail only once a real provisioning request arrived.
        if self.approvals is None:
            raise ContractViolation(
                "OperationFacadeService requires an ApprovalSource: this facade admits "
                "operations through the approval gate, and a facade without one would "
                "admit unapproved work"
            )
        if self.ledger is None:
            raise ContractViolation(
                "OperationFacadeService requires a BudgetLedger: admission reserves "
                "and confirms budget before it writes, and the ledger is the domain's"
            )

    # ------------------------------------------------------------------
    # The declared port surface
    # ------------------------------------------------------------------

    async def open_operation(
        self,
        *,
        action: str,
        workspace_id: str,
        org_id: str,
        permission: str,
        parameters: dict[str, str],
    ) -> OperationProgress:
        """Authorize and open one durable operation; return its first report.

        Signature fixed by the consumer's Protocol -- keyword-only, these names, this
        return type. See the module docstring.

        The idempotency key is taken from ``parameters['idempotency_key']`` when the
        caller supplies one, and otherwise derived from the request payload so that
        two identical retries collapse. Deriving rather than minting a fresh UUID is
        deliberate: a random key would make every retry a new operation, which is
        exactly the duplicate this story exists to prevent.

        **Admits through `admit_operation`, never `store.admit`** (#5526, w6-03). See
        the module docstring: the store answers "well-formed, unique, tenant-scoped" and
        this port must also answer "approved and within budget". `ApprovalRefused`,
        `BudgetDenied` and `BudgetUnavailable` propagate as themselves -- an approval
        refusal reported as unavailable would invite a retry of something no retry will
        make permissible, and a budget denial is not an outage.
        """
        principal = await self._resolve(
            org_id=org_id, workspace_id=workspace_id, permission=permission
        )
        supplied = dict(parameters or {})
        # Removed before validation, not after: `identity.OperationRequest` refuses
        # unknown control keys inside `parameters`, and the idempotency key is the
        # facade's own control field rather than part of the request payload.
        idempotency_key = supplied.pop("idempotency_key", None)
        if not idempotency_key:
            idempotency_key = _derive_idempotency_key(action, supplied)
        request = OperationRequest(
            action=action,
            idempotency_key=idempotency_key,
            parameters=supplied,
        )
        approval = await self._approval_for(principal=principal, request=request)
        # `OperationRefused` from `admit` -- a changed payload under a used key, or a
        # principal without the permission -- propagates unchanged; `_connection` lets
        # this package's refusals through deliberately, because converting one into an
        # unavailable would tell the caller to retry something that will never be
        # accepted. A schema mismatch or a backend failure becomes the port's
        # unavailable answer there.
        async with self._connection() as connection:
            outcome = await admit_operation(
                connection,
                self.store,
                self.ledger,
                principal=principal,
                request=request,
                approval=approval.record,
                requested_envelope=approval.requested_envelope,
                approver_statuses=approval.approver_statuses,
                # The gate compares this against the approval's expiry, so it is read
                # once here and passed in rather than read inside the gate. A gate
                # that called `now()` itself would be untestable at its own boundary,
                # which is the one place "is this approval still current" is decided.
                now=datetime.now(UTC),
            )
        return _progress(outcome.operation.record)

    async def _approval_for(
        self, *, principal: ResolvedPrincipal, request: OperationRequest
    ) -> ApprovalContext:
        """Ask the approval source, translating only what it cannot answer.

        A source that *raises* has not refused -- it failed to answer, which is the
        port's unavailable condition and not a denial. The distinction is the same one
        `BudgetUnavailable` draws against `BudgetDenied`, and collapsing it here would
        mean an unreachable approval store read as "not approved", which is at least the
        safe direction but tells the caller something false about why.

        A source that returns the wrong shape is a `ContractViolation`, not an
        unavailable: the deployment is wired incorrectly, and retrying will not fix it.
        """
        try:
            approval = await self.approvals.approval_for(
                principal=principal, request=request
            )
        except (OperationRefused, ContractViolation):
            raise
        except asyncio.CancelledError:
            # See `_connection`: cancellation is this process stopping, not the approval
            # source being unavailable.
            raise
        except Exception as error:
            raise OperationUnavailable(
                "the approval for this request could not be established; the operation "
                "is not admitted"
            ) from error
        if not isinstance(approval, ApprovalContext):
            raise ContractViolation(
                "the approval source must return an ApprovalContext"
            )
        return approval

    async def report_progress(self, operation_id: str) -> OperationProgress:
        """The facade's current report. The only way an outcome is learned.

        **Tenant-scoped, through the resolved principal.** This is the method the
        consumer's Protocol declares and therefore the method that is actually called
        (`services/provisioning.py:390`, and the boot-time capability probe). An earlier
        revision documented that it resolved the principal and then queried by
        `operation_id` alone -- so anyone holding another tenant's operation id could
        read that operation's state and detail. The correctly-scoped method existed
        beside it and nothing called it, which is the worst shape for a protection to
        have: real code, guarding nothing, reading as though the path were covered.

        The Protocol has no principal parameter, so the tenant cannot come from an
        argument -- which is the same reason the store refuses caller-supplied identity.
        It is resolved from the authenticated context via the same resolver
        `open_operation` uses, and the read is scoped in the WHERE clause.

        A principal that cannot be resolved is a refusal, not an unavailable: the
        request reached a facade that works and was not entitled to an answer.

        An operation in another tenant raises `OperationUnavailable` -- the same answer
        as one that does not exist, because a distinguishable "exists but forbidden"
        reply confirms another tenant's operation exists. Never a synthesized report,
        per the port's mandated unknown answer.
        """
        principal = await self._resolve_acting_principal()
        return await self.report_progress_for(principal, operation_id)

    async def _resolve_acting_principal(self) -> ResolvedPrincipal:
        """The acting principal, for a port method whose signature carries no tenant.

        Calls the resolver with the permission this store admits operations under and
        with no asserted tenant, because there is none to assert: the resolver's whole
        job is to produce the tenant from the verified context. The empty strings are
        not a tenant claim -- `_resolve`'s agreement check exists to catch a caller that
        named one, and there is no caller-named value on this path -- so agreement is
        checked against what the resolver itself returned.

        **The permission is required, not merely requested.** Passing
        `permission=workspace:provision` to the resolver asks for a principal; it does
        not establish that the principal came back holding it. A resolver that returns a
        correctly-scoped `ResolvedPrincipal` with an empty permission set was accepted
        here, and the read then returned that tenant's operation state -- reproduced
        against a real database. `_resolve`, used on admission, checks `may_provision`;
        this method checked the resolver's *type* and stopped. That asymmetry is the
        defect: the published `operation_facade` contract governs this port by
        `workspace:provision`, and belonging to the right tenant does not establish
        holding the right permission. Two different questions, and only one was asked.
        """
        try:
            principal = await self.resolver.resolve(
                org_id="", workspace_id="", permission=_REQUIRED_PERMISSION
            )
        except OperationRefused:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise OperationUnavailable(
                "the acting principal could not be resolved; no operation outcome can "
                "be established"
            ) from error
        if principal is None:
            raise OperationRefused(
                "the acting principal could not be resolved from the authenticated "
                "context; no operation may be read"
            )
        if not isinstance(principal, ResolvedPrincipal):
            raise ContractViolation(
                "the principal resolver must return a ResolvedPrincipal"
            )
        if not principal.may_provision:
            # Refused before any query runs. A refusal rather than an unavailable,
            # matching `_resolve`: the store is reachable and working, and this caller
            # is not entitled to an answer from it. Telling it "unavailable" would
            # invite a retry of something that will never be permitted.
            #
            # The message names the permission but not the operation, and deliberately
            # does not say whether the operation exists -- see `report_progress_for` on
            # why a distinguishable answer is itself the disclosure.
            raise OperationRefused(
                f"the resolved principal lacks {_REQUIRED_PERMISSION}; no operation "
                "may be read"
            )
        return principal

    # ------------------------------------------------------------------
    # Tenant-scoped reads
    # ------------------------------------------------------------------

    async def report_progress_for(
        self, principal: ResolvedPrincipal, operation_id: str
    ) -> OperationProgress:
        """Scoped `report_progress`, for callers that hold a resolved principal.

        Exists because the Protocol's `report_progress(operation_id)` has nowhere to
        put a tenant, and a public HTTP route must not use the unscoped form. An
        operation belonging to another tenant raises the same
        `OperationUnavailable` as one that does not exist -- a distinguishable answer
        would confirm another tenant's operation exists.
        """
        async with self._connection() as connection:
            record = await self.store.get(connection, principal, operation_id)
        if record is None:
            raise OperationUnavailable(
                f"no operation record for {operation_id!r} in this tenant"
            )
        return _progress(record)

    async def list_operations(
        self, principal: ResolvedPrincipal, *, limit: int = 50
    ) -> tuple[OperationProgress, ...]:
        """Recent operations for the principal's tenant. Bounded by the store."""
        async with self._connection() as connection:
            records = await self.store.list_for_tenant(
                connection, principal, limit=limit
            )
        return tuple(_progress(record) for record in records)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Connection]:
        """Open a connection, converting any backend failure into the port's answer.

        Covers three points, and an earlier revision covered only the first:

        1. **constructing** the context manager -- a synchronous raise from `connect()`;
        2. **entering** it -- `async with`, where a real pool does its work. An
           `acquire()` that times out or finds the pool closed raises here, and a
           read-only probe against the previous revision produced a bare
           `ConnectionError` out of this exact point. Inside the domain API that is
           an unhandled 500 rather than the declared refusal the consumer's error
           path expects;
        3. **the body** -- a query failing mid-read on a connection that opened fine,
           which is what a server restart or a killed backend looks like.

        Wrapping the body is what makes this a context manager rather than a function
        returning one: a caller's `async with self._connection() as c` puts its queries
        inside this frame, so one `except` covers them all and no call site has to
        remember to add its own.

        What passes through untranslated:

        * `asyncio.CancelledError` -- cancellation is not unavailability. Swallowing it
          would stop a shutting-down process from shutting down, and on 3.8+ it is a
          `BaseException` precisely so that `except Exception` does not catch it. It is
          named explicitly anyway, because the ordering is the kind of thing a later
          edit changes without noticing.
        * this package's own contract answers (`OperationRefused`, `ContractViolation`,
          `OperationUnavailable`). A refusal reported as "unavailable" tells a caller to
          retry something that will never be accepted, and a `ContractViolation` about a
          corrupt stored payload must not read as a transient outage -- retrying it
          forever is the wrong response to a row that needs a human.
        * **the gate's answers** -- `ApprovalRefused`, `BudgetDenied`,
          `BudgetUnavailable` (#5526, w6-03). `open_operation` calls `admit_operation`
          *inside* this context manager, so before they were named here an approval
          refusal was rewritten into "the operation store is unreachable": a caller was
          told to retry a request no retry will ever permit, and an operator
          investigating a denied spend was pointed at the database. `BudgetUnavailable`
          is kept distinct from `OperationUnavailable` for the same reason it exists at
          all -- which system could not answer is the whole diagnostic.

        `SchemaMismatch` **is** translated, and translated here rather than at each call
        site: an installed-but-wrong-version store cannot answer, which is exactly the
        port's unavailable condition. Its message is preserved because it tells an
        operator which direction the mismatch runs, and that is the whole diagnostic.
        """
        try:
            manager = self.connect()
        except Exception as error:
            raise OperationUnavailable(_UNREACHABLE) from error
        try:
            async with manager as connection:
                yield connection
        except asyncio.CancelledError:
            raise
        except SchemaMismatch as error:
            raise OperationUnavailable(str(error)) from error
        except (
            OperationUnavailable,
            OperationRefused,
            ApprovalRefused,
            BudgetDenied,
            BudgetUnavailable,
            ContractViolation,
        ):
            raise
        except Exception as error:
            raise OperationUnavailable(_UNREACHABLE) from error

    async def _resolve(
        self, *, org_id: str, workspace_id: str, permission: str
    ) -> ResolvedPrincipal:
        """Resolve the acting principal, and refuse anything that does not agree.

        Three refusals, and the third is the one the port entry is about:

        1. no principal -> refused. The resolver could not establish who is acting.
        2. wrong permission demanded -> refused. A caller asking to open a
           provisioning operation under some other permission string is asking for
           the check to be done against the wrong authority.
        3. **resolved tenant disagrees with the arguments** -> refused. The port says
           this facade acts as "the facade's own resolved principal, never the
           request body's `org_id`". The arguments are checked for agreement and
           discarded; the principal's values are what reach the store. Refusing on
           disagreement rather than silently preferring the resolved value means a
           mismatch is a visible error instead of a request that quietly operated on
           a different workspace than the caller named.
        """
        if permission != _REQUIRED_PERMISSION:
            raise OperationRefused(
                f"operations are admitted under {_REQUIRED_PERMISSION!r}; "
                f"got {permission!r}"
            )
        try:
            principal = await self.resolver.resolve(
                org_id=org_id, workspace_id=workspace_id, permission=permission
            )
        except OperationRefused:
            raise
        except asyncio.CancelledError:
            # Not an identity-provider failure. See `_connection` on why cancellation is
            # never translated.
            raise
        except Exception as error:
            raise OperationUnavailable(
                "the acting principal could not be resolved; the operation is not "
                "admitted"
            ) from error
        if principal is None:
            raise OperationRefused(
                "the acting principal could not be resolved from the authenticated "
                "context; the operation is not admitted"
            )
        if not isinstance(principal, ResolvedPrincipal):
            # A resolver returning some other shape would bypass the validation
            # `ResolvedPrincipal.__post_init__` performs on tenant identifiers.
            raise ContractViolation(
                "the principal resolver must return a ResolvedPrincipal"
            )
        if principal.org_id != org_id or principal.workspace_id != workspace_id:
            raise OperationRefused(
                "the resolved principal does not match the requested tenant and "
                "workspace; the operation is not admitted"
            )
        if not principal.may_provision:
            raise OperationRefused(
                f"the resolved principal lacks {_REQUIRED_PERMISSION}; the operation "
                "is not admitted"
            )
        return principal


def _derive_idempotency_key(action: str, parameters: dict[str, str]) -> str:
    """A key for a caller that supplied none, derived from what it asked for.

    Deriving rather than minting a fresh UUID is the whole point: a random key would
    make every retry a new operation, which is exactly the duplicate this story exists
    to prevent, reintroduced by the convenience of not requiring a key.

    The digest is computed over a request carrying a **fixed placeholder** key, because
    `payload_digest` covers the key itself -- so the value being derived cannot be an
    input to its own derivation. The placeholder is a constant rather than an empty
    string because `OperationRequest` refuses an empty key, and reaching for one here
    is what made an earlier revision of this function unable to construct the request
    it needed in order to compute the digest at all.

    Two identical payloads therefore derive the same key and collapse to one operation;
    two different payloads derive different keys. The key is prefixed so an operator
    reading a row can tell a derived key from one a caller chose -- they have different
    debugging implications, since a derived key changes whenever the payload does.
    """
    probe = OperationRequest(
        action=action,
        idempotency_key=_DERIVED_KEY_PLACEHOLDER,
        parameters=parameters,
    )
    return f"derived-{payload_digest(probe)[:32]}"


# A fixed, non-empty stand-in used only while computing a derived key. Its value never
# reaches the store; it exists so the digest is computed against a *constructible*
# request. Any constant works, so long as it is the same one every time -- a value that
# varied would make two identical requests derive different keys.
_DERIVED_KEY_PLACEHOLDER = "derived-key-placeholder"


def _progress(record: object) -> OperationProgress:
    """Project a store record onto the consumer's progress shape."""
    return OperationProgress(
        operation_id=record.operation_id,  # type: ignore[attr-defined]
        state=record.state.value,  # type: ignore[attr-defined]
        detail=record.detail,  # type: ignore[attr-defined]
    )

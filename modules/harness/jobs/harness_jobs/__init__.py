"""Shared durable execution: the operation store, dispatch outbox and admission gate.

Issues #5525 (w6-02), #5526 (w6-03), and #5527 (w6-04), EPIC #4910, Wave 6.
First implementation behind the ``operation_facade`` port published by #5524 (w6-01).

## What this package is

The one place a request to run an operation is written down. It owns:

* **durable identity** -- server-resolved tenant, immutable request/plan binding,
  operation and attempt IDs, status and version (``identity.py``, ``schema.py``);
* **atomic admission** -- the admission record and its outbox row in one transaction,
  so an accepted operation is always a dispatched one (``store.py``);
* **approved, budget-bound admission** -- a current approval bound to an immutable plan
  and a spend envelope, reserve/confirm against the domain ledger, one-time consumption
  and the compensation ordering (``approval.py``, ``admission.py``);
* **duplicate-safe delivery** -- resumable, at-least-once, marked delivered last
  (``outbox.py``);
* **the consumer-facing surface** -- matching the ``OperationFacade`` Protocol the
  domain app already declares (``facade.py``).

``admission.admit_operation()`` is the entry point a request handler should reach, not
``store.admit()``: the store answers "is this well-formed, unique and tenant-scoped" and
deliberately never "is this allowed".

It is shared, not Superplane's: per #5524's ownership table, anything that is a job,
approval, lease or credential lives in ``modules/harness/`` or ``modules/gateway/``.
Superplane is the first consumer.

## What it is not, and who owns each

Named explicitly because the tempting failure is to fill one of these in partially
and let a later story disagree with it:

| Not here | Owner |
|---|---|
| Leases, fence tokens, cancellation, crash-recovery executor | #5527 (w6-04) |
| The reservation ledger -- balances, amounts, spend totals | Superplane (domain) |
| Credential authorization and trusted delivery | #5528 (w6-05) |
| Report authority, allocation inventory | #5529 (w6-06) |
| Installing/composing this facade into the API | #5535 (w6-12) |
| Applying the schema to any database | #5538 (w6-15) |

``attempt_id`` and the ``version`` column are stored now so those stories add
behaviour rather than a migration that rewrites live rows.

## Composition is elsewhere, deliberately

Nothing here opens a connection, reads a DSN or holds a credential. Every entry point
is handed a connection or a factory. A package that could build its own pool would be
a second composition root, and whether this port is installed would become a property
of this source tree rather than of the reviewed startup sequence -- which is the
reason #5524's registry refuses to hand back implementations, restated for the
implementation side.

Importing this package runs no DDL and touches no database.
"""

from __future__ import annotations

from .admission import (
    DELIVERABLE_RESERVATION_STATES,
    AdmissionIntent,
    AdmissionOutcome,
    BudgetDenied,
    BudgetLedger,
    BudgetUnavailable,
    ConsumedApproval,
    CreationFence,
    DispatchEvidence,
    IntentStage,
    ReconciliationReport,
    Reservation,
    ReservationState,
    admit_operation,
    cancel_before_dispatch,
    derive_operation_identity,
    list_interrupted_admissions,
    read_consumption,
    read_consumption_privileged,
    reconcile_interrupted_admissions,
    retain_for_uncertain_dispatch,
)
from .approval import (
    APPROVAL_PERMISSION,
    NON_PERMISSIVE_RESULTS,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRefused,
    ApprovalResult,
    ApproverStatus,
    SpendEnvelope,
    evaluate_approval,
    requires_distinct_approver,
)
from .execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    OperationExecutor,
    OperationStatus,
    ProviderCall,
    ProviderCallRefused,
    audit,
    derive_idempotency_key,
    observe,
    read_audit,
    read_call,
    reconcile,
    record_intent,
    unresolved_calls,
)
from .facade import (
    PORT_REFUSAL_NAMES,
    ApprovalContext,
    ApprovalSource,
    OperationFacadeService,
    OperationProgress,
    OperationUnavailable,
    PrincipalResolver,
)
from .identity import (
    CONTRACT_VERSION,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_PARAMETER_COUNT,
    MAX_PARAMETER_VALUE_LENGTH,
    MAX_TOTAL_PARAMETER_BYTES,
    REQUIRED_PERMISSION,
    SUPPORTED_CONTRACT_VERSIONS,
    TERMINAL_STATES,
    ContractViolation,
    OperationBinding,
    OperationRefused,
    OperationRequest,
    OperationState,
    ResolvedPrincipal,
    decode_payload,
    encode_payload,
    forbidden_parameters,
    payload_digest,
)
from .leases import (
    DEFAULT_LEASE_DURATION,
    DEFAULT_MAX_CONCURRENT_OPERATIONS,
    DEFAULT_MAX_EXECUTION_ATTEMPTS,
    MAX_LEASE_DURATION,
    ExecutionLease,
    ExpiredLeaseTakeover,
    LeaseRefusal,
    LeaseRefused,
    acquire,
    close,
    fence_expired_lease,
    fenced_update,
    is_fenced_out,
    read_lease,
    release,
    renew,
)
from .outbox import (
    DEFAULT_CLAIM_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DeliveryReport,
    DispatchEnvelope,
    DispatchExecutor,
    DispatchOutbox,
)
from .recovery import (
    CancellationRecord,
    RecoveryReport,
    SweepResult,
    check_cancel_requested,
    request_cancellation,
    sweep_expired_leases,
    sweep_unresolved_calls,
)
from .schema import (
    SCHEMA_VERSION,
    SchemaMismatch,
    apply,
    check_schema_version,
    current_version,
    downgrade,
)
from .store import (
    AdmittedOperation,
    ConcurrentUpdate,
    OperationRecord,
    OperationStore,
)

__all__ = [
    # approval (#5526)
    "APPROVAL_PERMISSION",
    "NON_PERMISSIVE_RESULTS",
    "ApprovalBinding",
    "ApprovalDecision",
    "ApprovalRecord",
    "ApprovalRefused",
    "ApprovalResult",
    "ApproverStatus",
    "SpendEnvelope",
    "evaluate_approval",
    "requires_distinct_approver",
    # admission (#5526)
    "DELIVERABLE_RESERVATION_STATES",
    "AdmissionIntent",
    "AdmissionOutcome",
    "BudgetDenied",
    "BudgetLedger",
    "BudgetUnavailable",
    "ConsumedApproval",
    "CreationFence",
    "DispatchEvidence",
    "IntentStage",
    "ReconciliationReport",
    "Reservation",
    "ReservationState",
    "admit_operation",
    "cancel_before_dispatch",
    "derive_operation_identity",
    "list_interrupted_admissions",
    "read_consumption",
    "read_consumption_privileged",
    "reconcile_interrupted_admissions",
    "retain_for_uncertain_dispatch",
    # identity
    "CONTRACT_VERSION",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_PARAMETER_COUNT",
    "MAX_PARAMETER_VALUE_LENGTH",
    "MAX_TOTAL_PARAMETER_BYTES",
    "REQUIRED_PERMISSION",
    "SUPPORTED_CONTRACT_VERSIONS",
    "TERMINAL_STATES",
    "ContractViolation",
    "OperationBinding",
    "OperationRefused",
    "OperationRequest",
    "OperationState",
    "ResolvedPrincipal",
    "decode_payload",
    "encode_payload",
    "forbidden_parameters",
    "payload_digest",
    # schema
    "SCHEMA_VERSION",
    "SchemaMismatch",
    "apply",
    "check_schema_version",
    "current_version",
    "downgrade",
    # store
    "AdmittedOperation",
    "ConcurrentUpdate",
    "OperationRecord",
    "OperationStore",
    # outbox
    "DEFAULT_CLAIM_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DeliveryReport",
    "DispatchEnvelope",
    "DispatchExecutor",
    "DispatchOutbox",
    # facade
    "PORT_REFUSAL_NAMES",
    "ApprovalContext",
    "ApprovalSource",
    "OperationFacadeService",
    "OperationProgress",
    "OperationUnavailable",
    "PrincipalResolver",
    # leases (#5527)
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_MAX_CONCURRENT_OPERATIONS",
    "DEFAULT_MAX_EXECUTION_ATTEMPTS",
    "MAX_LEASE_DURATION",
    "ExecutionLease",
    "ExpiredLeaseTakeover",
    "LeaseRefusal",
    "LeaseRefused",
    "acquire",
    "close",
    "fence_expired_lease",
    "fenced_update",
    "is_fenced_out",
    "read_lease",
    "release",
    "renew",
    # execution (#5527)
    "BudgetDisposition",
    "CallOutcome",
    "CallStage",
    "OperationExecutor",
    "OperationStatus",
    "ProviderCall",
    "ProviderCallRefused",
    "audit",
    "derive_idempotency_key",
    "observe",
    "read_audit",
    "read_call",
    "reconcile",
    "record_intent",
    "unresolved_calls",
    # recovery and cancellation (#5527)
    "CancellationRecord",
    "RecoveryReport",
    "SweepResult",
    "check_cancel_requested",
    "request_cancellation",
    "sweep_expired_leases",
    "sweep_unresolved_calls",
]

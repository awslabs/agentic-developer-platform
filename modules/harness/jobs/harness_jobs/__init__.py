"""Shared durable execution: the operation store and dispatch outbox.

Issue #5525 (w6-02), EPIC #4910, Wave 6. First implementation behind the
``operation_facade`` port published by #5524 (w6-01).

## What this package is

The one place a request to run an operation is written down. It owns:

* **durable identity** -- server-resolved tenant, immutable request/plan binding,
  operation and attempt IDs, status and version (``identity.py``, ``schema.py``);
* **atomic admission** -- the admission record and its outbox row in one transaction,
  so an accepted operation is always a dispatched one (``store.py``);
* **duplicate-safe delivery** -- resumable, at-least-once, marked delivered last
  (``outbox.py``);
* **the consumer-facing surface** -- matching the ``OperationFacade`` Protocol the
  domain app already declares (``facade.py``).

It is shared, not Superplane's: per #5524's ownership table, anything that is a job,
approval, lease or credential lives in ``modules/harness/`` or ``modules/gateway/``.
Superplane is the first consumer.

## What it is not, and who owns each

Named explicitly because the tempting failure is to fill one of these in partially
and let a later story disagree with it:

| Not here | Owner |
|---|---|
| Leases, fence tokens, cancellation, crash-recovery executor | #5527 (w6-04) |
| Approval currency, expiry, one-time consumption | #5526 (w6-03) |
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

from .facade import (
    PORT_REFUSAL_NAMES,
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
from .outbox import (
    DEFAULT_CLAIM_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DeliveryReport,
    DispatchEnvelope,
    DispatchExecutor,
    DispatchOutbox,
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
    "OperationFacadeService",
    "OperationProgress",
    "OperationUnavailable",
    "PrincipalResolver",
]

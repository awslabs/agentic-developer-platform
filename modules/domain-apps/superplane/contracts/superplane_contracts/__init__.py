"""Versioned domain contracts for the Superplane domain app.

Issue #5043 (U8) — observation contracts, R11's contracts half.
Issue #5049 (U11) — durable-handle, reconciliation and provider-truth contracts,
R15's A half.

This package defines **contracts only**: the shape of a fleet or budget
observation, the version discipline around it, who may submit one, which
workspace's observations a submitter may write and read, and — from U11 — the
identity a provider operation records before it runs and what a provider re-check
must establish before a release can be claimed. There is deliberately no server,
no route and no HTTP client here — the receiver is implemented upstream by **U15**
(`src/superplane-api/`), and domain persistence is upstream too (design §3 line
171; handle persistence is **U11c**). An ADP-side `api/` was explicitly withdrawn
in the design's revision 3.

| Module | What it owns |
|---|---|
| `version` | contract version, and refusal of missing/unknown/disagreeing versions |
| `health` | check results that cannot claim health they did not observe; corrected severity ordering |
| `observation` | the fleet-health and budget-usage payloads and their wire form |
| `auth` | submission requires an authenticated **and** signed caller |
| `scoping` | workspace scoping of submit **and** read, both fail-closed |
| `leases` | lock acquire/release with fencing, needing no domain table grant |
| `handles` | a provider operation's identity, recorded **before** the call can be lost |
| `reconciliation` | an ambiguous outcome established against the provider, never retried blind |
| `accounting` | no release or cost clearance while exposure is unresolved |
| `provider_truth` | cleanup failure reported as failure, with a non-zero result |
| `adapter` | the record-then-call ordering, performed under B's authority |

Nothing here confers budget authority. A budget observation reports observed
spend; enforcement is B's admission-time concern (M6) and is not added locally,
even behind a flag. Equally, nothing here owns an operation's lifecycle: U11
records handles, reconciles ambiguity and reports provider truth, while **B** owns
cancellation ordering, leases/fencing and the recovery worker, and **C** owns the
reservation ledger.

The full rationale, including the specific upstream bugs each rule prevents, is in
`contracts/README.md` and in each module's docstring.
"""

from __future__ import annotations

from .accounting import (
    CostExposure,
    ReleaseAssessment,
    ReleaseState,
    assess_release,
)
from .adapter import (
    HandleStore,
    OperationAuthority,
    OperationResult,
    ProviderAdapter,
    ProviderClient,
)
from .auth import (
    AUTH_HEADER,
    SIGNATURE_HEADER,
    SUBMITTER_HEADER,
    AuthResult,
    Submitter,
    SubmitterResolver,
    canonical_body,
    compute_signature,
    verify_signature,
    verify_submission,
)
from .handles import (
    CallDecision,
    CallOutcome,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    authorize_provider_call,
)
from .health import (
    POSITIVE_DETAILS,
    POSITIVE_STATUSES,
    SEVERITY_RANK,
    CheckResult,
    CheckStatus,
    ContractViolation,
    aggregate_status,
    is_more_severe,
)
from .leases import (
    DEFAULT_LEASE_DURATION,
    MAX_LEASE_DURATION,
    Lease,
    LeaseDecision,
    LeaseRequest,
    authorize_release,
    grant,
    is_fenced_out,
)
from .observation import (
    OBSERVATION_KINDS,
    BudgetUsage,
    ClusterRef,
    Observation,
)
from .provider_truth import (
    MAX_EXIT_CODE,
    Finding,
    RecreationDriver,
    ReleaseIntent,
    TeardownReport,
)
from .reconciliation import (
    ProviderObservation,
    ProviderPresence,
    ReconcileDecision,
    ReconcileRequest,
    ReconcileResult,
    reconcile,
)
from .scoping import (
    ScopeDecision,
    authorize_read,
    authorize_submit,
    visible_workspaces,
)
from .version import (
    CONTRACT_VERSION,
    SUPPORTED_VERSIONS,
    VERSION_FIELD,
    VERSION_HEADER,
    VersionCheck,
    check_version,
)

__all__ = [
    "AUTH_HEADER",
    "CONTRACT_VERSION",
    "DEFAULT_LEASE_DURATION",
    "MAX_EXIT_CODE",
    "MAX_LEASE_DURATION",
    "OBSERVATION_KINDS",
    "POSITIVE_DETAILS",
    "POSITIVE_STATUSES",
    "SEVERITY_RANK",
    "SIGNATURE_HEADER",
    "SUBMITTER_HEADER",
    "SUPPORTED_VERSIONS",
    "VERSION_FIELD",
    "VERSION_HEADER",
    "AuthResult",
    "BudgetUsage",
    "CallDecision",
    "CallOutcome",
    "CheckResult",
    "CheckStatus",
    "ClusterRef",
    "ContractViolation",
    "CostExposure",
    "Finding",
    "HandleRecord",
    "HandleStore",
    "Lease",
    "LeaseDecision",
    "LeaseRequest",
    "Observation",
    "OperationAuthority",
    "OperationKind",
    "OperationResult",
    "ProviderAdapter",
    "ProviderClient",
    "ProviderHandle",
    "ProviderObservation",
    "ProviderPresence",
    "ReconcileDecision",
    "ReconcileRequest",
    "ReconcileResult",
    "RecreationDriver",
    "ReleaseAssessment",
    "ReleaseIntent",
    "ReleaseState",
    "ScopeDecision",
    "Submitter",
    "SubmitterResolver",
    "TeardownReport",
    "VersionCheck",
    "aggregate_status",
    "assess_release",
    "authorize_provider_call",
    "authorize_read",
    "authorize_release",
    "authorize_submit",
    "canonical_body",
    "check_version",
    "compute_signature",
    "grant",
    "is_fenced_out",
    "is_more_severe",
    "reconcile",
    "verify_signature",
    "verify_submission",
    "visible_workspaces",
]

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

U17a adds the provisioning binding, intent and progress contract and its adapter.
The provider receives server-resolved operation context separately from caller
options. B remains a mocked dependency; R14 live acceptance stays open.

The full rationale, including the specific upstream bugs each rule prevents, is in
`contracts/README.md` and in each module's docstring.

U7 adds provider-connection and workspace-binding contracts, credential rotation,
disablement and secret guards. Its vault client uses the gateway HTTP boundary.
"""

from __future__ import annotations

from .accounting import (
    AllocationResources,
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
    AuthResult,
    SIGNATURE_HEADER,
    SUBMITTER_HEADER,
    Submitter,
    SubmitterResolver,
    canonical_body,
    compute_signature,
    verify_signature,
    verify_submission,
)

from .connections import (
    ConnectionState,
    ConnectionStatus,
    CredentialReference,
    DISABLEMENT_LIMITATION,
    Decision,
    RENEW_CREDENTIAL_PERMISSION,
    RotationResult,
    ValidationReport,
    VaultOwnership,
    WorkspaceBinding,
    accept_connection_request,
    activate,
    authorize_delegation,
    authorize_use,
    disable,
    rotate,
)

from .emission import (
    PLACEHOLDER,
    SecretRedactingFilter,
    connection_response,
    install_log_redaction,
    scrub,
    validation_response,
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
    CheckResult,
    CheckStatus,
    ContractViolation,
    POSITIVE_DETAILS,
    POSITIVE_STATUSES,
    SEVERITY_RANK,
    aggregate_status,
    is_more_severe,
)

from .leases import (
    DEFAULT_LEASE_DURATION,
    Lease,
    LeaseDecision,
    LeaseRequest,
    MAX_LEASE_DURATION,
    authorize_release,
    grant,
    is_fenced_out,
)

from .observation import BudgetUsage, ClusterRef, OBSERVATION_KINDS, Observation

from .provider_truth import (
    Finding,
    MAX_EXIT_CODE,
    RecreationDriver,
    ReleaseIntent,
    TeardownReport,
)

from .provisioning import (
    FORBIDDEN_PARAMETER_KEYS,
    FORBIDDEN_PARAMETER_PREFIXES,
    INCONCLUSIVE_STATES,
    OperationBinding,
    OperationState,
    PROVISION,
    PROVISIONING_ACTIONS,
    ProvisioningIntent,
    ProvisioningProgress,
    REQUIRED_PERMISSION,
    ResolvedPrincipal,
    TEARDOWN,
    TERMINAL_STATES,
    forbidden_parameters,
)

from .provisioning_adapter import (
    OperationFacade,
    ProvisioningAdapter,
    ProvisioningProvider,
    ProvisioningRefused,
    summarize,
)

from .reconciliation import (
    ProviderObservation,
    ProviderPresence,
    ReconcileDecision,
    ReconcileRequest,
    ReconcileResult,
    reconcile,
)

from .scoping import ScopeDecision, authorize_read, authorize_submit, visible_workspaces

from .secrets import (
    assert_no_secret_material,
    find_secret_material,
    key_names_secret,
    looks_like_arn,
    value_is_secret_shaped,
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
    "AllocationResources",
    "AUTH_HEADER",
    "AuthResult",
    "BudgetUsage",
    "CONTRACT_VERSION",
    "CallDecision",
    "CallOutcome",
    "CheckResult",
    "CheckStatus",
    "ClusterRef",
    "ConnectionState",
    "ConnectionStatus",
    "ContractViolation",
    "CostExposure",
    "CredentialReference",
    "DEFAULT_LEASE_DURATION",
    "DISABLEMENT_LIMITATION",
    "Decision",
    "FORBIDDEN_PARAMETER_KEYS",
    "FORBIDDEN_PARAMETER_PREFIXES",
    "Finding",
    "HandleRecord",
    "HandleStore",
    "INCONCLUSIVE_STATES",
    "Lease",
    "LeaseDecision",
    "LeaseRequest",
    "MAX_EXIT_CODE",
    "MAX_LEASE_DURATION",
    "OBSERVATION_KINDS",
    "Observation",
    "OperationAuthority",
    "OperationBinding",
    "OperationFacade",
    "OperationKind",
    "OperationResult",
    "OperationState",
    "PLACEHOLDER",
    "POSITIVE_DETAILS",
    "POSITIVE_STATUSES",
    "PROVISION",
    "PROVISIONING_ACTIONS",
    "ProviderAdapter",
    "ProviderClient",
    "ProviderHandle",
    "ProviderObservation",
    "ProviderPresence",
    "ProvisioningAdapter",
    "ProvisioningIntent",
    "ProvisioningProgress",
    "ProvisioningProvider",
    "ProvisioningRefused",
    "RENEW_CREDENTIAL_PERMISSION",
    "REQUIRED_PERMISSION",
    "ReconcileDecision",
    "ReconcileRequest",
    "ReconcileResult",
    "RecreationDriver",
    "ReleaseAssessment",
    "ReleaseIntent",
    "ReleaseState",
    "ResolvedPrincipal",
    "RotationResult",
    "SEVERITY_RANK",
    "SIGNATURE_HEADER",
    "SUBMITTER_HEADER",
    "SUPPORTED_VERSIONS",
    "ScopeDecision",
    "SecretRedactingFilter",
    "Submitter",
    "SubmitterResolver",
    "TEARDOWN",
    "TERMINAL_STATES",
    "TeardownReport",
    "VERSION_FIELD",
    "VERSION_HEADER",
    "ValidationReport",
    "VaultOwnership",
    "VersionCheck",
    "WorkspaceBinding",
    "accept_connection_request",
    "activate",
    "aggregate_status",
    "assert_no_secret_material",
    "assess_release",
    "authorize_delegation",
    "authorize_provider_call",
    "authorize_read",
    "authorize_release",
    "authorize_submit",
    "authorize_use",
    "canonical_body",
    "check_version",
    "compute_signature",
    "connection_response",
    "disable",
    "find_secret_material",
    "forbidden_parameters",
    "grant",
    "install_log_redaction",
    "is_fenced_out",
    "is_more_severe",
    "key_names_secret",
    "looks_like_arn",
    "reconcile",
    "rotate",
    "scrub",
    "summarize",
    "validation_response",
    "value_is_secret_shaped",
    "verify_signature",
    "verify_submission",
    "visible_workspaces",
]

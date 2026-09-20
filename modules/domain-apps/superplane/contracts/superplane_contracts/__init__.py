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
| `delivery` | a credential scoped to one recipient, run and workspace; per-tenant isolation |
| `delivery_executor` | the provider executor, which runs only under a delivery lease |
| `integration` | every production port's owner, bound identifiers, permission and unknown answer |
| `conformance` | the shared probes a candidate adapter must refuse |

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

U10 adds the trusted credential-delivery contract and the provider executor that
consumes it. Delivery is authorized by a lease scoped to one recipient executor, one
credential and one active run/allocation/workspace operation — never by the vault's
credential-management endpoints, which carry no run binding and no run-tied
revocation, and never by a raw secret read. The executor holds no standing credential
and writes no domain record. B's trusted-delivery contract does not exist in ADP:
`TRUSTED_DELIVERY_IS_MOCKED` is True, every outcome carries the marker, and R8's live
acceptance (a real provider call by the bound executor) stays open.

w6-01 (#5524) adds the production integration registry and the shared conformance
probes. `integration` records, for every port above, who supplies the production
implementation, which identifiers an operation through it must be bound to, which
permission it must demand, whose authority it acts under, and what it must answer
when it cannot establish the truth. `conformance` publishes the negative probes an
implementation must refuse — forged workspace, forged operation identity, unknown
authority, stale contract version — so sixteen Wave 6 stories are checked against
one agreement rather than sixteen private readings of it.

Neither module imports an adapter, holds a reference to one, or can construct one.
A registry able to hand back an implementation would be a second composition root,
and a port could then be satisfied by something this package chose rather than by
something a reviewed startup composition installed. Whether a port is composed is a
property of a running process, answered by `app.installation.capabilities()`; it is
deliberately not a boolean in this package.
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
from .conformance import (
    CONFORMANCE_LIMITATION,
    PROBE_ALLOCATION_ID,
    PROBE_AUTHORITY,
    PROBE_DIGEST,
    PROBE_OPERATION_ID,
    PROBE_ORG_ID,
    PROBE_UNGRANTED_PERMISSION,
    PROBE_WORKSPACE,
    SMOKE_LIMITATION,
    STALE_VERSION,
    UNSERVED_FUTURE_VERSION,
    ConformanceReport,
    Probe,
    ProbeKind,
    ProbeResult,
    ProbeTimeout,
    ProbeVerdict,
    build_request,
    classify_response,
    declared_kinds,
    isolation_probes_for,
    not_exercised_for,
    report_for,
    run_probes,
    smoke_not_exercised_for,
    smoke_probes_for,
    varied_field,
)
from .connections import (
    DISABLEMENT_LIMITATION,
    RENEW_CREDENTIAL_PERMISSION,
    ConnectionState,
    ConnectionStatus,
    CredentialReference,
    Decision,
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
from .delivery import (
    CREDENTIAL_MANAGEMENT_PERMISSION,
    DELIVERY_PERMISSION,
    EXTERNAL_SECRETS_REPLICATION_RETIRED,
    REVOCATION_LIMITATION,
    TRUSTED_DELIVERY_IS_MOCKED,
    DeliveryLease,
    DeliveryRefused,
    ExecutorIdentity,
    IsolationRoot,
    ProviderOperation,
    RevocationState,
    RunBinding,
    SecretMaterial,
    TrustedDeliveryChannel,
    assert_workload_environment,
    file_is_executor_only,
    management_credentials_in,
    refuse_external_secret_replication,
    restricted_materialization,
)
from .delivery_executor import (
    CREDENTIAL_FILENAME,
    DeliveryOutcome,
    DeliveryRequest,
    ProviderExecutor,
    forbidden_request_parameters,
)
from .delivery_executor import summarize as summarize_delivery
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
    POSITIVE_DETAILS,
    POSITIVE_STATUSES,
    SEVERITY_RANK,
    CheckResult,
    CheckStatus,
    ContractViolation,
    aggregate_status,
    is_more_severe,
)
from .integration import (
    API_CAPABILITY_PORTS,
    PORTS_BY_NAME,
    PRODUCTION_PORTS,
    PortContract,
    PortOwner,
    UnknownOutcome,
    check_port_version,
    externally_owned_ports,
    port,
    ports_owned_by,
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
from .observation import OBSERVATION_KINDS, BudgetUsage, ClusterRef, Observation
from .provider_truth import (
    MAX_EXIT_CODE,
    Finding,
    RecreationDriver,
    ReleaseIntent,
    TeardownReport,
)
from .provisioning import (
    FORBIDDEN_PARAMETER_KEYS,
    FORBIDDEN_PARAMETER_PREFIXES,
    INCONCLUSIVE_STATES,
    PROVISION,
    PROVISIONING_ACTIONS,
    REQUIRED_PERMISSION,
    TEARDOWN,
    TERMINAL_STATES,
    OperationBinding,
    OperationState,
    ProvisioningIntent,
    ProvisioningProgress,
    ResolvedPrincipal,
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
    "API_CAPABILITY_PORTS",
    "AUTH_HEADER",
    "CONFORMANCE_LIMITATION",
    "CONTRACT_VERSION",
    "CREDENTIAL_FILENAME",
    "CREDENTIAL_MANAGEMENT_PERMISSION",
    "DEFAULT_LEASE_DURATION",
    "DELIVERY_PERMISSION",
    "DISABLEMENT_LIMITATION",
    "EXTERNAL_SECRETS_REPLICATION_RETIRED",
    "FORBIDDEN_PARAMETER_KEYS",
    "FORBIDDEN_PARAMETER_PREFIXES",
    "INCONCLUSIVE_STATES",
    "MAX_EXIT_CODE",
    "MAX_LEASE_DURATION",
    "OBSERVATION_KINDS",
    "PLACEHOLDER",
    "PORTS_BY_NAME",
    "POSITIVE_DETAILS",
    "POSITIVE_STATUSES",
    "PROBE_ALLOCATION_ID",
    "PROBE_AUTHORITY",
    "PROBE_DIGEST",
    "PROBE_OPERATION_ID",
    "PROBE_ORG_ID",
    "PROBE_UNGRANTED_PERMISSION",
    "PROBE_WORKSPACE",
    "PRODUCTION_PORTS",
    "PROVISION",
    "PROVISIONING_ACTIONS",
    "RENEW_CREDENTIAL_PERMISSION",
    "REQUIRED_PERMISSION",
    "REVOCATION_LIMITATION",
    "SEVERITY_RANK",
    "SIGNATURE_HEADER",
    "SMOKE_LIMITATION",
    "STALE_VERSION",
    "SUBMITTER_HEADER",
    "SUPPORTED_VERSIONS",
    "TEARDOWN",
    "TERMINAL_STATES",
    "TRUSTED_DELIVERY_IS_MOCKED",
    "UNSERVED_FUTURE_VERSION",
    "VERSION_FIELD",
    "VERSION_HEADER",
    "AllocationResources",
    "AuthResult",
    "BudgetUsage",
    "CallDecision",
    "CallOutcome",
    "CheckResult",
    "CheckStatus",
    "ClusterRef",
    "ConformanceReport",
    "ConnectionState",
    "ConnectionStatus",
    "ContractViolation",
    "CostExposure",
    "CredentialReference",
    "Decision",
    "DeliveryLease",
    "DeliveryOutcome",
    "DeliveryRefused",
    "DeliveryRequest",
    "ExecutorIdentity",
    "Finding",
    "HandleRecord",
    "HandleStore",
    "IsolationRoot",
    "Lease",
    "LeaseDecision",
    "LeaseRequest",
    "Observation",
    "OperationAuthority",
    "OperationBinding",
    "OperationFacade",
    "OperationKind",
    "OperationResult",
    "OperationState",
    "PortContract",
    "PortOwner",
    "Probe",
    "ProbeKind",
    "ProbeResult",
    "ProbeTimeout",
    "ProbeVerdict",
    "ProviderAdapter",
    "ProviderClient",
    "ProviderExecutor",
    "ProviderHandle",
    "ProviderObservation",
    "ProviderOperation",
    "ProviderPresence",
    "ProvisioningAdapter",
    "ProvisioningIntent",
    "ProvisioningProgress",
    "ProvisioningProvider",
    "ProvisioningRefused",
    "ReconcileDecision",
    "ReconcileRequest",
    "ReconcileResult",
    "RecreationDriver",
    "ReleaseAssessment",
    "ReleaseIntent",
    "ReleaseState",
    "ResolvedPrincipal",
    "RevocationState",
    "RotationResult",
    "RunBinding",
    "ScopeDecision",
    "SecretMaterial",
    "SecretRedactingFilter",
    "Submitter",
    "SubmitterResolver",
    "TeardownReport",
    "TrustedDeliveryChannel",
    "UnknownOutcome",
    "ValidationReport",
    "VaultOwnership",
    "VersionCheck",
    "WorkspaceBinding",
    "accept_connection_request",
    "activate",
    "aggregate_status",
    "assert_no_secret_material",
    "assert_workload_environment",
    "assess_release",
    "authorize_delegation",
    "authorize_provider_call",
    "authorize_read",
    "authorize_release",
    "authorize_submit",
    "authorize_use",
    "build_request",
    "canonical_body",
    "check_port_version",
    "check_version",
    "classify_response",
    "compute_signature",
    "connection_response",
    "declared_kinds",
    "disable",
    "externally_owned_ports",
    "file_is_executor_only",
    "find_secret_material",
    "forbidden_parameters",
    "forbidden_request_parameters",
    "grant",
    "install_log_redaction",
    "is_fenced_out",
    "is_more_severe",
    "isolation_probes_for",
    "key_names_secret",
    "looks_like_arn",
    "management_credentials_in",
    "not_exercised_for",
    "port",
    "ports_owned_by",
    "reconcile",
    "refuse_external_secret_replication",
    "report_for",
    "restricted_materialization",
    "rotate",
    "run_probes",
    "scrub",
    "smoke_not_exercised_for",
    "smoke_probes_for",
    "summarize",
    "summarize_delivery",
    "validation_response",
    "value_is_secret_shaped",
    "varied_field",
    "verify_signature",
    "verify_submission",
    "visible_workspaces",
]

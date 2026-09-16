"""Versioned observation contracts for the Superplane domain app.

Issue #5043 (U8), EPIC #4910. R11, contracts half.

This package defines **contracts only**: the shape of a fleet or budget
observation, the version discipline around it, who may submit one, and which
workspace's observations a submitter may write and read. There is deliberately no
server, no route and no HTTP client here — the receiver is implemented upstream by
**U15** (`src/superplane-api/`), and domain persistence is upstream too (design §3
line 171). An ADP-side `api/` was explicitly withdrawn in the design's revision 3.

| Module | What it owns |
|---|---|
| `version` | contract version, and refusal of missing/unknown/disagreeing versions |
| `health` | check results that cannot claim health they did not observe; corrected severity ordering |
| `observation` | the fleet-health and budget-usage payloads and their wire form |
| `auth` | submission requires an authenticated **and** signed caller |
| `scoping` | workspace scoping of submit **and** read, both fail-closed |
| `leases` | lock acquire/release with fencing, needing no domain table grant |

Nothing here confers budget authority. A budget observation reports observed
spend; enforcement is B's admission-time concern (M6) and is not added locally,
even behind a flag.

The full rationale, including the specific upstream bugs each rule prevents, is in
`contracts/README.md` and in each module's docstring.
"""

from __future__ import annotations

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
    "CheckResult",
    "CheckStatus",
    "ClusterRef",
    "ContractViolation",
    "Lease",
    "LeaseDecision",
    "LeaseRequest",
    "Observation",
    "ScopeDecision",
    "Submitter",
    "SubmitterResolver",
    "VersionCheck",
    "aggregate_status",
    "authorize_read",
    "authorize_release",
    "authorize_submit",
    "canonical_body",
    "check_version",
    "compute_signature",
    "grant",
    "is_fenced_out",
    "is_more_severe",
    "verify_signature",
    "verify_submission",
    "visible_workspaces",
]

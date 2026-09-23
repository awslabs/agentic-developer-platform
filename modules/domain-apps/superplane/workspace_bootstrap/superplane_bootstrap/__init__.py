"""Workspace bootstrap, BYOC validation and ADP registration.

Issue #5533 (w6-10), EPIC A #4910, requirements row A4. Turns an ACTIVE managed
cluster — or an explicitly supplied existing one — into a verified, registered
Superplane workspace target.

## What this package is, in one sentence

A sequence of refusal gates. Every module here answers one question with
"verified" or "refused", and nothing downstream may treat an unanswered question
as a pass.

## Why it is a separate package from `installation/`

`installation/` (U23, #5327) installs the MANAGEMENT surface and, at
`runner.py::Installer.workspace()`, *verifies* that a workspace cluster already
has the CRDs, the namespace and a correctly scoped controller credential. It has
no way to make any of that true — it only checks. This package is what makes
those checks able to pass, which is the gap requirements row A4 records as
"Partial registration surfaces (U16a/U16b); no bootstrap".

Keeping them separate also keeps their failure domains separate. A management
installation that refuses must not be able to leave a half-bootstrapped tenant
cluster behind, and a workspace bootstrap that refuses must not roll back the
management surface.

## What this package does NOT own

- **Provider construction.** The real `ProvisioningProvider` (the
  `provisioning_provider` port, owner `WORKSPACE_INFRA`) and mode-aware
  retirement are #5534 (w6-11). This package is called *under* a bound
  operation; it does not open one.
- **Infrastructure.** VPC/EKS/IAM are #5532 (w6-09), already merged at
  `../infra/workspaces/`. This package READS that module's published outputs and
  never re-derives or re-creates them.
- **Account/cluster ownership modes.** `OwnershipMode` and `ClusterOwnership`
  are #5530 (w6-07), at `../infra/account-factory/account_factory/modes.py`.
  This package consumes them so "supplied cluster" means the same thing in both.

## Standard library only

Like `../contracts/` and `../infra/account-factory/`, this package imports
nothing outside the standard library and this repository. Cluster and provider
access are `Protocol` seams (`access.py`); the tests pass fakes. That is what
makes the whole suite offline: it proves the decision logic and every refusal
without contacting AWS or a Kubernetes API server.

Importing this package performs no external action. The CLI and trusted service
composition execute the provider, Kubernetes and registration adapters explicitly.
Offline tests use transport doubles and disposable databases. Live evidence belongs
to the Wave 5 gate and the Wave 6 operations evaluator (#5540 AC-03).
"""

from __future__ import annotations

from .access import (
    ClusterAccess,
    ClusterIdentity,
    ObservedNamespace,
    ObservedPod,
    ObservedWorkload,
    ProviderIdentity,
    RegistrationStore,
)
from .adapters import (
    AwsObserver,
    AwsPrerequisiteAccess,
    CommandResult,
    CommandRunner,
    KubectlClusterAccess,
    SubprocessRunner,
)
from .admission import (
    DECLARED_PROOF_CHECKS,
    ORDERING_PROOFS,
    RESTRICTED_ENFORCE_LABEL,
    RESTRICTED_ENFORCE_VERSION_LABEL,
    RESTRICTED_POLICY,
    UNSAFE_POD_FIELDS,
    AdmissionProof,
    IsolationEvidence,
    prove_tenant_isolation,
    required_proofs,
    tenant_namespace_labels,
)
from .components import (
    BOOTSTRAP_OWNER_LABEL,
    CONTROLLER_IMAGE_MARKER,
    WORKSPACE_CRDS,
    ComponentInstallation,
    InstalledObject,
    install_components,
)
from .errors import BootstrapRefused
from .inventory import (
    OwnedPrerequisite,
    PrerequisiteInventory,
    adopt_prerequisites,
)
from .prerequisites import (
    ACCESS_ENTRY,
    ENDPOINT_RULE,
    MANAGEMENT_RULE,
    REQUIRED_PREREQUISITE_KINDS,
    ExpectedPrerequisites,
    PrerequisiteAccess,
    require_inventory,
    verify_prerequisites,
)
from .readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
    REQUIRED_SYSTEM_WORKLOADS,
    SYSTEM_NAMESPACE,
    ReadinessCheck,
    RuntimeReadiness,
    establish_runtime_readiness,
    prepare_system_workloads,
)
from .registration import (
    RegistrationReservation,
    WorkspaceRegistration,
    WorkspaceTarget,
    finalize_registration,
    reserve_registration,
)
from .registry import (
    REGISTERED,
    RESERVED,
    SqlRegistrationStore,
    TransactionalStore,
)
from .retire import CleanupPlan, plan_cleanup
from .state import (
    ADOPTED,
    ADP_CREATED,
    STATE_VERSION,
    BootstrapState,
    FileStateStore,
    NamespaceRecord,
    StateStore,
    load_state,
    namespace_ownership,
    state_from_mapping,
)
from .target import VerifiedTarget, verify_target
from .workspace import (
    BOOTSTRAP_TAINT_KEY,
    WORKSPACE_CONTROLLER_NAME,
    BootstrapOutcome,
    bootstrap_workspace,
    recover_interrupted_bootstrap,
)

__all__ = [
    "ACCESS_ENTRY",
    "ADOPTED",
    "ADP_CREATED",
    "BOOTSTRAP_OWNER_LABEL",
    "BOOTSTRAP_TAINT_KEY",
    "CONTROLLER_IMAGE_MARKER",
    "DECLARED_PROOF_CHECKS",
    "ENDPOINT_RULE",
    "FORBIDDEN_CONTROLLER_PERMISSIONS",
    "MANAGEMENT_RULE",
    "ORDERING_PROOFS",
    "REGISTERED",
    "REQUIRED_CONTROLLER_PERMISSIONS",
    "REQUIRED_PREREQUISITE_KINDS",
    "REQUIRED_SYSTEM_WORKLOADS",
    "RESERVED",
    "RESTRICTED_ENFORCE_LABEL",
    "RESTRICTED_ENFORCE_VERSION_LABEL",
    "RESTRICTED_POLICY",
    "STATE_VERSION",
    "SYSTEM_NAMESPACE",
    "UNSAFE_POD_FIELDS",
    "WORKSPACE_CONTROLLER_NAME",
    "WORKSPACE_CRDS",
    "AdmissionProof",
    "AwsObserver",
    "AwsPrerequisiteAccess",
    "BootstrapOutcome",
    "BootstrapRefused",
    "BootstrapState",
    "CleanupPlan",
    "ClusterAccess",
    "ClusterIdentity",
    "CommandResult",
    "CommandRunner",
    "ComponentInstallation",
    "ExpectedPrerequisites",
    "FileStateStore",
    "InstalledObject",
    "KubectlClusterAccess",
    "IsolationEvidence",
    "NamespaceRecord",
    "ObservedNamespace",
    "ObservedPod",
    "ObservedWorkload",
    "OwnedPrerequisite",
    "PrerequisiteAccess",
    "PrerequisiteInventory",
    "ProviderIdentity",
    "ReadinessCheck",
    "RegistrationReservation",
    "RegistrationStore",
    "RuntimeReadiness",
    "SqlRegistrationStore",
    "StateStore",
    "SubprocessRunner",
    "TransactionalStore",
    "VerifiedTarget",
    "WorkspaceRegistration",
    "WorkspaceTarget",
    "adopt_prerequisites",
    "bootstrap_workspace",
    "establish_runtime_readiness",
    "finalize_registration",
    "install_components",
    "load_state",
    "namespace_ownership",
    "plan_cleanup",
    "prepare_system_workloads",
    "prove_tenant_isolation",
    "recover_interrupted_bootstrap",
    "require_inventory",
    "required_proofs",
    "reserve_registration",
    "state_from_mapping",
    "tenant_namespace_labels",
    "verify_prerequisites",
    "verify_target",
]

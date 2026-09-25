# Remaining installed shared-bootstrap wiring

Shared execution is still disabled by `runtime.validate_phase` and absent from
`supported_runtime_modes`. `bootstrap_runtime.bootstrap` accepts shared placement
only with explicit protected composition. No lifecycle input, inferred role ARN,
local AWS profile or boolean readiness assertion supplies that composition.

## Implemented tenant inventory

`shared_tenant_inventory.installed_tenant_principals` now supplies the EKS half of
the canonical `tenant_authorization.namespace_mutation_allowed` proof directly.
It pages identity-provider configs, access entries and attached access policies;
rechecks the original operation and installed authority around every SDK call;
refuses external identities, unknown entry/policy types, failed/incomplete reads
and pagination that does not advance. All non-node, non-issuer identities remain
in the returned inventory, including IAM username templates and groups.

Only the exact registered issuer entry is exempt: original access-entry ARN,
username, group and STANDARD type must match, and it must have no attached EKS
access policies. Another entry with the same issuer group is still a tenant
subject and is evaluated by the namespace authorization proof. Cluster policy,
issuer role/entry and current registry checks still run through the authority
backend. Existing canonical code separately reads actual namespace SAs,
RoleBindings and ClusterRoleBindings and submits authorization reviews for their
subjects. The hooks object no longer accepts a tenant-inventory callback.

The installed issuer IAM policy must allow the read operations above in addition
to DescribeCluster/DescribeAccessEntry. Its Kubernetes RBAC must permit the
existing bounded namespace probes, namespace SA/RoleBinding and cluster binding
inventory, and SubjectAccessReview. Missing permission refuses; bootstrap never
adds it.

## Missing pinned dependency descriptor

The current `credential_controller.registry.Authority` version-1 document pins
cluster TLS identity, issuer/projector principals and access entries, admission
policy/binding UIDs and Secret projection identities. It has no CRD schema digest,
controller release identity or networking policy inventory. Its closed schema
correctly refuses extra fields, so a callback must not silently invent those facts.

The smallest honest next contract is an immutable, installation-owned dependency
descriptor, linked by ID/digest to the registered authority, containing:

- Each required CRD's name, original UID, served/storage versions and expected
  schema digest from the maintained release.
- The sole management controller's cluster/namespace/Deployment UID and pinned
  image digest, plus the supported management observation protocol/release.
- Approved management/worker-to-target API connectivity facts: exact relevant
  security groups, rule IDs and network topology/ownership, and the required
  platform-eligibility policy for this shared cluster.

A production verifier must read those exact objects/provider facts and compare
against the descriptor before namespace effects and again at readiness. Existing
`shared_dependency_checks` reads CRD Established conditions and system workload
health, and `ManagementObservation` proves the original member/claim reaches the
manager. Neither establishes schema/release compatibility. Existing
`verify_network_prerequisites` can check supported network facts, but requires its
explicit `ExpectedPrerequisites`; those are not in the installed registry.
`establish_network` is a mutating dedicated-bootstrap recipe and must not run as a
substitute for shared dependency verification.

Until that descriptor and read-only verifier exist, the remaining
`SharedRuntimeHooks.verify_cluster_dependencies` is an explicit uncomposed
requirement. It must not be replaced with a no-op, a request-provided callback,
`Established` alone or an arbitrary current-object digest.

## Missing private management session delivery

`runtime.delivery_session` obtains exactly the role delivered for the current
approved operation and verifies its account against that operation's target.
`context.authority.delivery_role` has no separate installed management-projector
purpose/target contract. The base worker session and installed controller role
are not interchangeable with a delivered management session.

A protected broker method must authenticate the existing operation (or recovery
grant), load its canonical membership and installed authority, authorize projection
for that organization/cluster, and deliver only the registered management source
role needed to assume the pinned projector. Every refresh must recheck the same
operation/installation and immutable IAM RoleId. No request may choose this role,
account or Secret target. The resulting private session fills
`SharedRuntimeHooks.management_source_session` and is passed separately to
`compose_transports(..., management_source_session=...)`. This uses existing
operation authority; it does not create a new principal/grant engine.

## Missing partial-bootstrap cleanup dispatch

`recovery_partial.PartialLifecycleRecovery` currently classifies incomplete
bootstrap journals and explicitly reports `cleanup_authorized=False`. It builds
the dedicated network recipe for every bootstrap-workspace phase. Its call to
`runtime.validate_phase(require_fresh=False)` still refuses shared execution.
`recovery_bootstrap.BootstrapRecovery` verifies already completed result anchors;
it does not clean up an incomplete namespace. Neither currently reconstructs a
shared factory and calls `recover_interrupted_bootstrap`.

The recovery dispatch must first select the original approved membership before
any dedicated network recipe. It must obtain fresh existing RecoveryGrant
verification and narrowly delivered issuer/projector sessions, preserve the
original operation/org/workspace/registration claim and target incarnation, and
reconstruct `SharedBootstrapAuthorityFactory` over the original state/journals.
The backend binding resolver must distinguish cleanup authority from permission
to acquire/issue/open. Provider callbacks must continue to check the irreversible
recovery latch and the original claim under the same database lock. Then the
existing namespace recovery engine can close the exact UID, withdraw digest-owned
projections, revoke original SA/RBAC, and release only the verified claim.

Expired execution credentials, a completed bootstrap result or the renewal
controller lease cannot substitute for this recovery grant. The state file is
already persisted at the canonical operation `bootstrap-state.json`; transient
transport files do not carry authority. Add recovery takeover/process-loss tests
at the runtime dispatch before enabling execution.

## Enablement order

Implement and validate the dependency descriptor/verifier, private management
session broker delivery and partial-bootstrap recovery dispatch. Compose the
remaining two hook fields in the installed worker only after their real contracts
are available. Then add shared discovery/execution routing before the dedicated
network path and test it through real journals with provider transports. Change
preview/capability advertising last. None of those enablement changes is made by
this tenant-inventory repair. Local validation remains static; remote CI and an
authorized live deployment establish runtime/provider behavior.

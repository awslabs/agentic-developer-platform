# Remaining installed shared-bootstrap wiring

Shared execution is still disabled by `runtime.run_lifecycle` and absent from
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

## Version-2 pinned dependency descriptor

The installed authority remains version 1 for renewal-only deployments. Version 2
retains every existing field and adds exactly `shared_dependencies`; shared
bootstrap refuses version 1. This immutable source contract follows
[DESIGN.md section 7.1](../DESIGN.md#71-shared-membership-credential-composition).
The authorized installer supplies the descriptor before registration; runtime
never derives expected digests or UIDs from the live objects it is checking.

`shared_dependencies` contains exactly:

- `version: 1`, `observation_protocol: "shared-member-v1"`, and
  `platform_eligible` as an explicit boolean matching canonical cluster policy
  (must be true when the target is management EKS).
- `crds`: one entry for every maintained workspace CRD, each with `name`, original
  `uid`, and `spec_sha256`. The hash covers canonical JSON of the maintained CRD
  spec, including names/scope and every version's served/storage/schema fields.
- `controller`: `namespace`, `namespace_uid`, `deployment`, `deployment_uid`,
  `container`, `image` (digest-pinned), and `template_sha256` (canonical JSON of the
  explicitly installed Pod template). It must use the maintained controller image
  marker and the same management namespace as the credential projections.
- `network`: `topology: "same-vpc-private"`, `owner_workspace_id`,
  `endpoint_rule_id`, `sts_rule_id`, `sts_endpoint_id`, and `expected`, whose closed fields are the
  canonical `ExpectedPrerequisites` dataclass. `retained_sts_rule_id` must equal
  `sts_rule_id`; protocol/port are TCP/443. The owner must be the canonical shared
  cluster's original workspace, not the member being bootstrapped.

The first implementation deliberately supports only private target and management
EKS endpoints in the same account/region/VPC, with node/management/API/STS groups
and exact existing rules in that VPC. Other topologies refuse until a separately
reviewed routing/DNS/peering proof is available. This restriction is a bounded
supported contract, not an assertion that SG rules prove arbitrary connectivity.

Before namespace effects and again at readiness, the read-only verifier checks
current installed authority, canonical owner/eligibility, exact cluster VPC/private
endpoint settings, group VPC/owners, the original available private-DNS STS
interface endpoint, canonical network rule checks and pinned rule IDs; then verifies CRD identity/Established/spec and supported maintained release.
It checks compatible stored CRD versions and the exact healthy management
Deployment, its namespace UID, template and image, and refuses competing controller
deployments on management/target clusters.
System workload and original-claim management observation remain the existing
engine's live readiness checks. No verified network/CRD/controller object enters
member cleanup inventory. This descriptor replaces the dependency callback seam;
it does not deliver the still-missing management source session or recovery grant.

Version-2 installation must explicitly grant the worker read-only EKS/EC2 network
inventory and the issuer/projector the corresponding CRD/Deployment inventory
reads, including complete controller listing across namespaces. Those reads add no
namespace, controller or network mutation permission. A renewal-only v1 projector
that can read/write only its projection Secrets may lack the v2 observation
permissions; bootstrap refuses that installation rather than expanding its roles.

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
`runtime.validate_phase(require_fresh=False)` validates original phase lineage;
that validation does not supply a shared cleanup dispatcher or provider authority.
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

The version-2 dependency verifier and direct tenant inventory are implemented.
Implement private management session broker delivery and partial-bootstrap
recovery dispatch before composing the remaining management-session hook in the
installed worker. Then add shared discovery/execution routing before the dedicated
network path and test it through real journals with provider transports. Change
preview/capability advertising last. None of those enablement changes is made by
this tenant-inventory repair. Local validation remains static; remote CI and an
authorized live deployment establish runtime/provider behavior.

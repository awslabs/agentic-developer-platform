# Workspace provisioning and retirement

Public retirement review reads completed bootstrap ownership and verifies the
original source operation and historical artifact. It returns
`admission_available: false` and no approval request until staged cleanup access
is implemented. The retirement admission route refuses without changing workspace
state or opening a paid operation. This is a review capability, not executable
retirement.

The remaining cleanup-access stage must use a separate governed control
allocation, derived from the original allocation and request identity. Its exact
temporary EKS/RBAC recipe needs approval and an immutable grant artifact. The
teardown request then binds `cleanup_access_artifact_id`, removes only owned
resources, and revokes the exact temporary grant identities. It must retain the
original allocation and its seal. Bootstrap's revoked PROVISION grant cannot be
reused as teardown authority. Managed destruction also requires a saved destroy
plan, complete workload/storage ownership and an enforced admission interlock.
Provider inventory and financial settlement remain separate from operation
success. These execution criteria have not been established by the review route.

Automated managed provisioning currently requires owned networking. Supplied
networking is refused before credential delivery, worker state creation or
Terraform preparation. EKS creates the node security group during apply, so an
external owner cannot precreate its exact private STS ingress rule for the saved
plan. Enabling this mode requires an explicit preexisting node security group
design. An operator adding a rule during a partial apply does not repair the
reviewed plan. Adoption remains supported when discovery verifies an existing
cluster and its exact private STS ingress prerequisite.

This package implements the workspace provider boundary for #5534. The admitted
runtime prepares an immutable managed Terraform proposal or discovers an adopted
cluster. A separately approved continuation applies the exact saved plan, followed
by a separately approved canonical bootstrap. Each phase executes through the
shared RPC, lease and provider-intent machinery. A prepared proposal reports
`awaiting_plan_approval`; readiness requires canonical bootstrap registration.
New-account provisioning remains unavailable until its complete account recipe
and composed acceptance checks are in place.

The private `account_creation` composer binds the maintained account producer to
one original shared RPC intent. Its logical creation key aliases that real intent;
it cannot open a retry generation. Accepted request IDs remain evidence through
interruption, cancellation and lease expiry. Live resumption uses the maintained
creation observer and an immutable child handoff. Bootstrap uses a separate
approval; management credential delivery keeps the management account identity,
and child credentials require the exact original successful creation record.
This composer is not routed by the public worker. Private account bootstrap and
infrastructure phases join the canonical registration composer. Protected account
recovery reads the exact accepted request under a current recovery claim, including
when no artifact exists. Positive pending status preserves the original intent and
backoff; failures and unknown outcomes retain accounting. Success publishes an
immutable original-producer handoff and a separate recovery audit, then settles
through the shared recovery engine. Its API role is configured separately as
`SUPERPLANE_ACCOUNT_RECOVERY_OBSERVATION_ROLE_ARN` and permits only caller identity,
DescribeCreateAccountStatus and success-only ListParents reads in the approved
management account. Full remote composed acceptance remains required before
new-account capability can be enabled.

An applied result records exact cluster, node-group, launch-template, CNI and
retained private STS identities. Protected recovery can observe that completed
result without delivering provider credentials to the worker; persisted plan
bytes and original admission lineage are checked independently. Missing results,
uncertain partial applies and changed provider identities remain unresolved, with
allocation retained. Protected bootstrap recovery checks terminal output against
the exact canonical registration and completed revoked-authority journal anchor.
It establishes the original operation's completion; it is not a new live health
observation and delivers no credentials. A partial mutation without terminal
output is classified from the maintained finite-effect and authority journals.
Unknown effects retain the original reservation and are never replayed. Removing
outstanding grants requires a separately approved cleanup composer; observation
authority cannot perform that cleanup.

`load_bootstrap_retirement_inventory` reads deletion candidates from
the completed bootstrap registration and its PostgreSQL ownership journals. It
requires a current service-resolved teardown binding. It checks the canonical
Workspace/Cluster association and metadata before returning any ownership.

The reader refuses outstanding bootstrap recovery, missing or partial prerequisite
inventories, changed canonical targets and ambiguous retained grants. Supervisor
grants adopted across bootstrap retries are deduplicated by exact immutable
identity. A pre-existing namespace remains preserved even when it carries an ADP
owner label. BYOC cluster preservation is explicit in the returned inventory.

The native bootstrap adapter journals its controller objects before each
create, including ServiceAccount and both scopes of RBAC. Legacy registrations may
also contain a Deployment; management bootstrap uses five objects. It records
the provider UID and full observed specification digest. A retry reuses these
identities; a lost response is recovered from the committed creation marker and
provider read. Pre-existing matching objects are recorded as adopted and are never
promoted to deletion ownership. Changed objects refuse instead of being overwritten.
CRDs remain shared and are excluded from this ownership inventory.

The retirement reader exposes `components` and `components_complete`. Missing
legacy component records leave `components_complete=False`: downstream retirement
must refuse automatic component deletion in that case. A completion flag with a
missing object, unresolved creation, changed target or contradictory UID refuses.
Registration outcomes now carry the same component ownership and UIDs as the journal.

Recovery keeps the interrupted owner's state intact when another bootstrap attempt
is refused. That contender cannot clear the persisted recovery claim or restore the
owner's scheduling interlock. A process interrupted after creating the controller
uses the durable component record for handover, including when the old local
`controller_installed` flag never committed.

`RetirementRuntime` is a trusted provider hook for the maintained
`ExecutionRPCServer`; workers submit only admitted step IDs. Required composition:

- `connect`: the harness connection context manager.
- `context(call)`: resolve the current original operation and authenticated teardown
  `OperationBinding`; revalidate run authority on every call. The runtime also
  checks the live shared lease, attempt, fence, cancellation, original allocation,
  plan digest and exact ordered descriptor list.
- `registration_store`: canonical `SqlRegistrationStore` on the domain database.
- `removals`: `OwnedResourceRemover(kubernetes=KubeGrants(...), eks=EksGrants(...),
  network=SecurityGroupRules(session=..., target=VerifiedTarget(...),
  expected=ExpectedPrerequisites(...)))` using trusted Terraform outputs,
  brokered credentials and pinned transports. Component deletion checks full
  observed spec, creation marker, UID and resourceVersion. Shared ClusterRoles,
  ClusterRoleBindings, adopted namespaces and adopted prerequisites are preserved.
- `lifecycle`: `RetirementLifecycle(domain_pool=..., execution_pool=...,
  workspace=Workspace(...))`. It withdraws active admission while preserving all
  canonical ownership records. Drain requests maintained cancellation and polls
  paginated real governed Kubernetes roots/pods and allocation retirement state.
- `artifact_for(operation, inventory)`: optional trusted saved-artifact resolver;
  returns `ReviewedDestroy` for managed infrastructure only. Call
  `compose_retirement_plan(inventory, managed_destroy=artifact)` before approval.
- `terraform`: `TerraformDestroy(python_binary=..., guard_script=...,
  terraform_binary=...)`, with the guard path fixed to the maintained
  `infra/workspaces/scripts/apply_workspace_plan.py`. The approved parameters must
  contain equal `allocation_id` and `original_allocation_id`, plus
  `terraform_plan_file_sha256` and `terraform_backend_sha256`. The latter hashes
  canonical sorted compact JSON of the reviewed authorization's `backend` object.
  `ReviewedDestroy` carries trusted paths to the actual saved binary, rendered JSON,
  authorization and module directory, and all six target bindings. No caller path
  or `terraform destroy` replan is accepted. Creating/replacement plans refuse.
- `verify_inventory`: wire `RetirementFinalizer.verify_step`. Construct the finalizer
  with `execution_pool`, `domain_pool`, `resolve(operation_id)`,
  `observations=RetirementObservations(session=..., kubernetes=..., eks=...)`, the
  maintained authority `authenticate` callback and `token_for(operation)` returning
  the actual opaque authority that callback verifies. `resolve` revalidates the live
  original operation and returns `(operation, inventory, reviewed_destroy_or_none)`.
  Also wire the same finalizer as the execution RPC's `after_step` hook: the shared
  implementation enumerates before deletion, then begins a fresh listing, seals,
  queries every retained handle, and publishes its accounting assessment before the
  completed operation closes its lease. Provider observation credentials must remain
  usable after the workspace Terraform role is destroyed.

`RetirementObservations` queries the current workspace module's Terraform resource
types through AWS SDK reads, including IAM policy attachments, EKS addons/node groups,
KMS, log groups and network associations. It also discovers actual VPC instances,
interfaces, their volume/address dependencies, and workspace-tagged resources. A
detached volume no longer in a listing remains queried by its retained original ID.
Unrecognized discovered resources, incomplete bootstrap ownership, provider errors
and scheduled-but-not-complete KMS deletion stay present/unknown and retain exposure.
Adopted infrastructure is excluded from deletion ownership. An owned namespace is
absent only after an exact Kubernetes absence or confirmed deletion of its managed
EKS parent; merely failing to contact Kubernetes never proves absence.

An accepted deletion is not absence: asynchronous/uncertain replies remain UNKNOWN
under the original shared intent and retain accounting. The shared executor owns
durable ordered progress and prevents automatic replay of uncertain mutations.
Never release an entire allocation from an individual call's RELEASE disposition.
The exact removal allowlist requires the schema trigger refresh migration on existing
databases; fresh installations use the same entries directly.

Managed destruction additionally requires producer-owned `managed_objects` tuples
`(kind, namespace, name, UID)`, the digest of their sorted compact JSON in approved
`managed_workload_inventory_sha256`, and `managed_fence(operation, inventory)` that
verifies a persistent cluster admission interlock. The runtime checks every actual
workload/storage object's UID against that set, including all namespaces, and keeps
checking the interlock while Terraform runs. Missing proof, replacement objects or
unreadable cluster-wide inventory retain the operation. The current bootstrap
journal does not produce this complete system/workload inventory or interlock;
managed destruction stays blocked until that producer handoff is implemented.

Remaining obligations are explicit. Workload allocations still running at the drain
deadline need separately admitted cleanup; cancellation alone cannot finish them.
The real bootstrap retains generation-specific supervisor ClusterRole and
ClusterRoleBinding objects. They are owned grants, not shared merely because they
are cluster-scoped. The namespaced cleanup recipe cannot remove them. A complete
retirement therefore requires independently provisioned exact-name cluster RBAC
cleanup authority whose own lifecycle is external and verified. No such authority
is configured by this checkpoint, so real bootstrap inventories refuse access
planning and executable retirement remains gated. The original supervisor receives
no additional permissions, and no cluster-admin fallback is used. New cleanup
grants must eventually be proved absent and their separate allocation settled as
well as the original allocation; neither successful access setup nor deletion of
its final EKS entry alone proves those obligations complete.
Owned namespace cascading deletion remains unauthorized. The plan's
`completes_teardown` cannot substitute for provider inventory/cost evidence, even
after a successful reviewed Terraform apply. Account closure remains outside
workspace retirement. API/worker composition and live EC2 regression evidence are
required before this source checkpoint can close the story.

Run the producer-to-consumer tests in remote CI or the disposable EC2 regression
harness, from the repository root:

```sh
python -m pytest modules/domain-apps/superplane/workspace_provisioning/tests -q
```

Tests execute the real bootstrap and canonical registration against disposable
PostgreSQL; only cloud/Kubernetes transport is doubled. They perform no live
provisioning or retirement. `test_lifecycle_runtime_postgres.py` composes admission,
managed/adopt phase handoffs, persisted artifacts and network intents; its bootstrap
boundary refuses and never fabricates readiness. `test_bootstrap_runtime_postgres.py`
separately executes the production bootstrap composer, real read-token issuance,
grant journal and canonical registration against stateful transport doubles.

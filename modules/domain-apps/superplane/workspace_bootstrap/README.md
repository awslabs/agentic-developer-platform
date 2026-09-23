# Workspace bootstrap

The maintained entry point is `python -m workspace_bootstrap.superplane_bootstrap`
from the Superplane domain directory. `plan` reads target identity; `bootstrap`
executes the gated installation; `recover` restores an interrupted interlock;
`state` reports durable local evidence. Installation and live acceptance require
the existing deployment gates; merging this code performs neither.

## Execution authority and target

`--binding` contains only `{"operation_id":"..."}`. It is a lookup reference,
not authorization. `--binding-resolver MODULE:CALLABLE` names a trusted service
adapter that authenticates to the operation facade, loads the server-held binding,
and returns the genuine `OperationBinding`. Caller-provided principal, workspace,
permission, expiry or action fields are rejected. The adapter rechecks provision
permission, action, contract version and expiry before execution. Runtime service
composition and credential delivery remain with #5534/#5535; a factory must never
reconstruct authority from the request file.

The selected kubeconfig context must match the verified EKS endpoint and inline
certificate authority. Insecure TLS and server-name overrides are refused. The
verified context is pinned to an adapter-owned private temporary file for all Kubernetes calls; replacing the source kubeconfig cannot redirect a mutation. The CLI removes the snapshot on exit. Raw kubeconfig content is never
written into the registration or printed in an error.

The selected output artifact includes account, region, cluster name/ARN/CA, org,
workspace and network identities. Network checks use the API supplemental group
(`workspace_api_security_group_id`), management source group, workspace node group
(`workspace_node_security_group_id`), private STS endpoint group
(`sts_endpoint_security_group_id`) and its VPC (`sts_endpoint_vpc_id`). Both
TCP/443 ingress rules require exact provider IDs and matching `OrgId` and
`WorkspaceId` tags. Stateful return traffic requires no reverse ingress rule.

## Publication and recovery

Finalization holds the reservation token and atomically writes the canonical
`workspaces`/`clusters` records and the reservation completion. It requires an
explicitly bound organization, preserves an existing selected cluster ID and
rejects tenant, namespace, endpoint or provider-identity conflicts. Controller
management discovers the result through its normal canonical query. The opaque
credential reference and verified bootstrap metadata live under
`clusters.actual_state_json.workspace_bootstrap`; no credential value is stored
there or delivered by the metadata-only controller endpoint.

A durable `taint_clear_pending` intent precedes taint removal. A crash after the
cluster mutation and before its response therefore leaves recoverable evidence.
Recovery retains the existing reservation fingerprint fence: stale evidence may
not release or re-taint a successor's workspace.

## Isolation probes

`--imds-probe-image` must name a release-selected Python image by SHA-256 digest.
A restricted, nonroot probe Pod performs bounded IMDSv2 token and credential-path
requests for IPv4 and IPv6. Any HTTP response, including 401, proves reachability.
Only an explicit network-unreachable result counts as isolation; malformed
responses and failed Pod execution leave the proof unanswered. No response body
or token is emitted. Tenant admission-label checks enumerate EKS STANDARD access
entries and their additive access policies, all namespace service accounts and
RoleBinding subjects. SubjectAccessReview checks both patch and update, with EKS
Kubernetes usernames/groups and matching exact role-session User bindings.
Cluster-scoped EKS policies, unknown policies, external OIDC configurations and
unanswered inventories refuse instead of claiming isolation. This does not grant
impersonation permission or equate an ADP subject with a Kubernetes username.

## Offline validation

Run `python -m pytest modules/domain-apps/superplane/workspace_bootstrap/tests -q`
from the repository root with the domain test dependencies, asyncpg and pgserver
installed. Database tests create disposable PostgreSQL instances and apply the
maintained migration chain there. They do not use a deployed database. Full
Superplane Domain CI, affected Harness checks and a final-head code review are
required before merge. These tests establish offline behavior, not live acceptance.

## Bootstrap authority lifecycle

`--authority-resolver MODULE:CALLABLE` receives the authenticated binding and
verified output expectations and returns `BootstrapRuntime`. That runtime supplies
`BootstrapAuthorityFactory`, the observer, cluster/network adapters and the real
`SqlRegistrationStore`. Production bootstrap and recovery refuse without this
composition. The factory's `resolve_clients(binding, target, release)` returns
`BootstrapClients` from the service's credential resolver. Caller flags cannot
supply grant documents, actor identities or policies. #5534/#5535 own the service
and vault implementation of this interface.

The release pins the namespace, controller/service account, CRDs, Pod Security
version and system workload inventory. After network verification and an exclusive
registration reservation, the factory journals generation-bound access for three
distinct service roles in the verified target account:

1. A temporary registrar uses EKS cluster administration on the selected cluster
   to prepare the Restricted namespace and install the compiled worker RBAC.
2. A temporary installer receives collection-create and named-object update/bind/
   escalate permissions for the pinned components, namespaced installation access,
   and patch access to the selected CoreDNS deployment. Required and forbidden
   effective permissions are checked before component installation.
3. A retained workspace supervisor receives inventory and SubjectAccessReview reads
   plus node-taint control. It cannot install workloads, read secrets or edit RBAC.
   This separate identity permits final verification and interlock recovery after
   installer access is removed.

Every grant intent precedes provider mutation and every mutation holds the current
reservation/generation fence. Temporary installer and registrar access is positively
revoked before final tenant enumeration, readiness, taint removal and registration.
Only the exact, actively verified registrar is temporarily exempt from the tenant
inventory; final enumeration has no exemption. Failed/unknown revocation retains the
claim and refuses registration. Recovery never reacquires grants.

The decision to retain the bounded supervisor commits before revocation. Exact grant
specifications and immutable identities remain in `workspace_bootstrap_authority`,
keyed by workspace, operation and generation. Retries adopt that same supervisor only
from a completed matching journal; altered UIDs, permissions, aggregation labels,
principals or release inventories refuse. #5534 retirement consumes this ownership
record. The namespace is retained as a resource, with creation/adoption tracked
separately; pre-existing access is never converted into temporary owned authority.
The exact prerequisite rule IDs and ownership classifications are persisted both
in file state and the PostgreSQL journal before component installation. A legacy
`prerequisites_recorded` flag without that inventory supplies no deletion authority.

Public recovery requires fresh service authentication for the same operation and
verified target. Expiration does not erase the old journal or authorize new grants:
a renewed binding must still match its operation, workspace and current claim.
If credentials or observations cannot be re-established, recovery retains the claim.
A completed bootstrap replay returns its canonical registration without installing
or acquiring access and does not assert fresh readiness or taint clearance.

The workspace module now publishes both `workspace_api_security_group_id` and
`workspace_node_security_group_id`. Shared STS endpoint IDs/VPC are outside that
Terraform module's ownership: the trusted provisioning composer must join its
selected network handoff into the bootstrap output artifact. They are required
inputs, with no environment defaults or inferred reverse ingress rule.

Interrupted interlock recovery holds the exact reservation's PostgreSQL advisory and
row locks while restoring the bootstrap taint. It releases that claim only after
every observed node has the NoSchedule taint and temporary authority is confirmed
revoked. A failed or partial restore
retains the claim for retry; a stale claim or unreachable registry authorizes no cluster
mutation. This prevents a successor from reserving between claim release and a late
recovery taint write. The unresolved outcome continues to report possible scheduling.

Temporary Kubernetes grants now require a verified target and a DynamicClient whose
endpoint, CA bytes and TLS settings match it. These checks run again before each
provider operation, so a changed client configuration or replaced CA file refuses
before reading or modifying a grant. These checks also run through the production lifecycle above. EKS region,
cluster ARN, endpoint and CA are independently checked through its SDK client.
EKS authentication must be `API`, matching the maintained workspace module.
BYOC clusters using legacy `aws-auth` mappings refuse because EKS access-entry
enumeration alone cannot prove the permissions of those additional identities.

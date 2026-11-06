# Verified tenant and principal boundaries — design for #7841

## Review status and scope

**Architect-approved / ready for implementation of this design, not approved
for deployment.** Reviewer: ADP architect, native Codex SDK (automated architectural
review, not human approval). Review date: 2026-10-16 UTC. Reviewed `main` source:
`c958000f0829b1462c2f4ce23bfb743676b97306`. The task branch initially contains only
an additional worker checkpoint, with no source difference from that revision.

The capability is tenant isolation for credential evidence/revocation, organization
SSO configuration/token admission, and repository identification in the Knowledge
Door. Requests must not acquire authority by naming a tenant, owner or repository.
An authenticated service can act for another principal only through a specifically
authorized delegation path; authenticating a shared workload is not authenticating
the human or agent it names.

This is design-only. No application, infrastructure or deployed configuration is
changed. The [story](https://github.com/aws-e/adp/issues/7841) remains the acceptance
authority; its native parent is [#7840](https://github.com/aws-e/adp/issues/7840),
verified through GitHub's parent API. No AC/scenario IDs existed in the reviewed
story; this refinement introduces AC-01–AC-08 and S-01–S-12 without retiring any.
See [scenario contracts](validation.md), [implementation backlog](backlog.md), and
[source inventory and coverage limits](coverage.md). Finding references are only:

- `f-64bf79a4-9add-405b-8efb-9bdf2c39443d`
- `f-2944ab04-f38e-4b29-9aba-54f163a9d65f`
- `f-5615e522-036a-4eab-8013-ece1d93dfbef`

**Remaining implementation-readiness blockers: none for the contracts below.**
Live resource ownership, installed clients/providers, permitted issuer hosts,
current caller registrations and staging access are unverified. These are explicit
deployment gates, not assertions that those resources are missing. Operations and
identity owners must verify them before enabling federation or new denials.
Independent implementation review and required CI are still required. Repository
coverage here is a reproducible source inventory and focused boundary review, not
proof that every inventoried file is secure or a live penetration-test result.

## Findings against current source

| Surface | Classification and practical consequence | Source evidence |
|---|---|---|
| Vault evidence | Confirmed missing caller-to-tenant and caller-to-acting-principal binding. The route passes body fields directly into the reader. The reader's ownership/delegation predicates are useful secondary constraints, not authentication. Secret values are not returned. | [transport](../../../modules/gateway/src/internal/vault_evidence_routes.py#L339), [reader](../../../modules/gateway/src/auth/vault_evidence.py#L328) |
| Revocation state | Confirmed caller-selected tenant; the binding is constructed from body fields. The helper checks credential/delegation state, not the caller's authority over an operation. | [transport](../../../modules/gateway/src/internal/vault_evidence_routes.py#L391), [helper](../../../modules/gateway/src/auth/vault_delivery.py#L401) |
| Internal authentication | Verified IAM provenance and registry scope are already enforced. The finding is **not** unauthenticated access or acceptance of a shared API key. The dependency exposes a `TokenContext`, not a universally valid tenant/principal delegation. | [dependency](../../../modules/gateway/src/internal/auth_deps.py#L24), [service authorization](../../../modules/gateway/src/internal/service_authorization.py#L6) |
| Superplane SSO | Confirmed reversed organization mapping and truncated provider identifier. Actual tenant-claim overwrite is conditional on the installed provider/client settings. Current Terraform excludes tenant attributes from public client writes; that protection must not be removed to make federation work. No live takeover was tested. | [SSO service](../../../modules/domain-apps/superplane/src/superplane-api/app/services/sso.py#L32), [client attributes](../../../modules/gateway/infra/modules/cognito/main.tf#L315) |
| Token/domain admission | The trigger copies event attributes and returns the event on exceptions. The domain token policy trusts the org claim; later explicit domain bindings, grants and optional current-identity checks add protection. A forged org claim alone does not prove every domain operation is reachable. | [trigger](../../../modules/gateway/infra/modules/cognito/lambda/pre_token_generation.py#L45), [policy](../../../modules/domain-apps/superplane/auth/superplane_auth/policy.py#L478), [guard](../../../modules/domain-apps/superplane/src/superplane-api/app/domain_guard.py#L235) |
| Repository identify | Confirmed existence signal: `resolve_input` computes `repo_indexed` before ACL filtering and identify returns it even with no findings. Existing finding filtering and the hardened verify action do not remove this signal. Plan calls identify and must share the repair. | [resolution](../../../modules/agent-context/door/secure_backend.py#L612), [identify](../../../modules/agent-context/door/secure_backend.py#L1388), [verify](../../../modules/agent-context/door/secure_backend.py#L1203), [plan](../../../modules/agent-context/door/secure_backend.py#L1596) |

Related work: merged [#7816](https://github.com/aws-e/adp/pull/7816) changes Gateway
membership enforcement, not these evidence handlers or the Superplane IdP service.
Merged [#5790](https://github.com/aws-e/adp/pull/5790) supplies Door fail-closed ACL
patterns but leaves identify's pre-filter existence lookup. Open
[#2448](https://github.com/aws-e/adp/pull/2448) touches Door dispatch/other verbs;
it is not a merged fix. [#6979](https://github.com/aws-e/adp/issues/6979) overlaps
provider naming/ownership: use one migration and test contract, not competing
implementations. [#5535](https://github.com/aws-e/adp/issues/5535) owns production
adapter composition and remains a compatibility dependency. None of these issue
or PR states establishes deployment or live acceptance.

## 1. Gateway authority contract

### Existing callers and the identity split

The production evidence client is Superplane's
[AdpVaultClient](../../../modules/domain-apps/superplane/src/superplane-api/app/adapters/adp_vault_client.py#L24),
installed by [composition](../../../modules/domain-apps/superplane/src/superplane-api/app/composition.py#L273).
It uses SigV4 with the API workload's web identity, not an agent run credential.
Its IAM route allowlist includes evidence in
[api_producer.tf](../../../modules/domain-apps/superplane/infra/control-plane/api_producer.tf#L32).
Provider-connection admission and the private installation evidence observation
consume this port. Requiring `_verified_executor` for every evidence request would
break these calls before the executor exists.

There is no non-test direct HTTP caller of `credential-revocation-state` in the
reviewed tracked source. This does not prove there are no external callers.
Delivery and preflight have authenticated run paths and invoke revocation logic
internally; preserve those contracts. Do not grant a hypothetical sweep or support
tool global authority merely because the issue mentions one.

ADP tenant identifiers and Superplane organization UUIDs are distinct.
[DomainBinding](../../../modules/gateway/src/internal/domain_operation_store.py#L28)
pins both plus the producer/worker registry IDs. The existing
[producer boundary](../../../modules/gateway/src/internal/domain_operation_routes.py#L158)
requires the current registry identity to match that binding and its ADP tenant.
An ADP/domain ID translation is **not** cross-tenant authority.

### Shared dependency and exact interfaces

Keep `verify_internal_or_irsa` as the transport authenticator, compatible with
existing `Depends` uses. Add a typed request-state identity result distinguishing
`service`, `agent_run`, `domain_executor`, and `tools_assertion`; it contains the
verified service ID, optional verified tenant, and the authentication provenance.
Use `None` for an unbound tenant, not a sentinel such as `__platform__` treated as
a real tenant. Tools assertions do not become ordinary service capabilities.

Add a shared, async metadata authorization dependency in the Gateway internal
plane, called by **both** evidence and revocation handlers before data access:

```text
authorize_vault_metadata(request, db, *, action, requested_tenant_id,
                         requested_workspace_id, requested_principal=None,
                         operation_selector=None) -> VaultMetadataAuthority

VaultMetadataAuthority:
  tenant_id                 verified ADP tenant used for ordinary vault queries
  principal                 authenticated actor (service or run)
  acting_principal          verified owner/human for the authorized delegation
  workspace_id              authorized workspace, never just copied from input
  domain_org_id              optional server-bound domain UUID
  operation_binding         optional verified operation/attempt/job binding
  authority_kind            tenant_service | domain_producer | agent_run
```

These are proposed interfaces, not existing APIs. Keep external `org_id` fields
for wire compatibility; use `tenant_id` in new internal authority types. The
authority object is created only by this dependency, never Pydantic request input.
Helpers accept this context (or explicit verified fields extracted from it), not
independently reusable body fields. For ordinary evidence, the query tenant is
`authority.tenant_id`; for reserved domain-provider evidence, pass **both** the
verified ADP tenant and verified domain org to the existing reserved resolver.
For revocation, build `OperationBinding` from the verified operation record and
use a separate ADP vault tenant just as delivery already does; do not put a domain
UUID into an ordinary ADP credential query.

For the domain producer's existing response port, `org_id` remains the verified
domain UUID; ordinary service responses use the verified ADP tenant. Perform this
explicit transport translation after the tenant-scoped read, not by changing its
SQL predicate. The adapter compares the response org with its requested domain
org. Credential ownership identifiers such as `user:<canonical-user-id>` are not
Cognito subjects: resolve that relationship through platform user records, retain
the existing owner identifier in responses, and never manufacture ownership by
prefixing a request string. Delegated-workspace authorization remains a separate
valid path. Test both different-ID and legacy equal-ID bindings.

### Authorized paths

1. **Tenant-bound service:** require current registry identity, real tenant and
   an operation-specific capability using `require_service_operation` and
   `service_tenant`. New capabilities are `credential:evidence:read` and
   `credential:revocation:read`. Evidence is limited to the authenticated service
   principal's own stored ownership/delegation unless explicitly authorized as a
   domain producer below. A service naming another owner is denied, even within
   its tenant. Internal-plane scope alone is not a capability. Refresh protected
   registry authority per sensitive call rather than relying on a cached grant.
2. **Superplane producer:** reuse `domain:operation-producer`, `current_registry`,
   `binding_for` and the producer's exact registry/ADP-tenant checks. Select a
   single configured binding matching the authenticated producer and requested
   domain; require body org equality with its domain UUID. A shared physical API
   service still has only these registered bindings, not all tenants. This
   capability explicitly delegates evidence reads on behalf of a human already
   authorized by the domain API. Independently recheck that human with
   `current_human_identity` and current domain workspace/grant records through
   `domain_session(binding)`; require matching workspace ownership, non-revoked
   grant and the existing permission for the initiating provider-connection
   operation (`workspace:renew_credential` for the current evidence consumers,
   including the private installation observation). Reject a body principal that
   cannot satisfy that delegation. Keep
   the reserved provider resolver's stronger installation/subject/membership
   checks; ordinary vault evidence still requires owner/delegation predicates.
   Do not translate a service ID into a human by email or accept an arbitrary
   `owner_principal`. No new global cross-tenant grant is needed for this caller.
3. **Agent or domain executor:** reuse `_verified_executor`, run/workload
   authentication, current live grant and operation validation. Use the run
   tenant and run principal, not the shared IAM role as a human owner. If the
   evidence consumer needs an original human, obtain it from the verified run or
   operation authority and revalidate that delegation; no fallback to the body.
   Revocation additionally binds operation/job/attempt/workspace to that actor.
   Delivery and preflight retain all existing material-delivery checks.
4. **Explicit cross-tenant service:** `internal:cross-tenant` **plus** the exact
   read capability is necessary, not sufficient to impersonate an owner. Require
   a nonempty requested target and an explicit server-held delegation for the
   target workspace/principal/operation. Deny missing target, missing delegation
   or ambiguous binding. No current caller was found needing this path; default
   enrollment is empty. Do not change `service_tenant(..., None, ...)` globally:
   other discovery routes legitimately use its existing optional-target contract.

Use existing ownership, membership, workspace grants, run grants and operation
records; no new authorization database or secret path. The production producer
enrollment is constrained by an **exact capability-set equality** in
[current_registrations](../../../modules/gateway/src/internal/domain_provider_authority.py#L125).
Keep its current `domain:operation-producer` scope, interpreted narrowly above;
blindly adding read scopes would break reserved provider evidence. New ordinary
service read grants use the protected registry provisioning path, never the public
admin API. Publish the caller/capability mapping with implementation tests.

### Errors and regression boundary

Reject caller/tenant/principal/capability mismatch before credential existence
queries with the existing route-specific uniform 403 shape. A missing credential
on an authorized evidence read remains the same 403. Authorized revocation reads
retain `admits_work=false` for missing/revoked/expired/delegation-absent credentials;
unauthorized callers do not receive that boolean. Genuine registry, identity,
operation or ACL-store unavailability is a generic 503, not a successful empty
authority. Malformed bodies remain 422. Log actor, action and reason internally,
without secret material, foreign ownership maps or tenant-enumerating messages.

Do not put these routes into `BROKER_PATHS`: its webhook-user matching contract
does not describe pre-execution domain API calls. Do not use
`resolve_effective_org_id` as a fix: it returns an existing claim unchanged and
its subject-only fallback does not model every multi-org identity.

Add the requested short invariant comment at each repaired site during
implementation. Replace contradictory module documentation that says all internal
callers may assert tenant identity. Establish a mounted-route authorization
manifest and a fail-on-unclassified-route integration check. For tenant-scoped
data helpers, instrument the call boundary to require verified context and assert
no call for missing/mismatched tenant. Merely checking decorator names or trusting
an author-written classification table does not prove this property. Existing
asset callbacks, service discovery and run-specific routes keep their distinct
authentication contracts; an exemption requires a named verifier and a negative
test, not `internal` scope alone.

## 2. SSO and domain organization contract

### Platform-owned selection, not external claims

Reuse the existing server-managed Cognito `custom:org_id` **as a selected-tenant
projection**, backed by ADP membership and workspace selection, not as arbitrary
IdP data. Do not add a second tenant table or infer organization from email,
provider display name, a truncated UUID, or an external claim. The platform's
[workspace selector](../../../modules/gateway/src/auth/workspaces.py#L208) writes
the selected tenant after membership resolution; preserve multi-org switching.

The projection is trustworthy only after all three writer controls are verified:
no provider maps tenant/role/team/department authority; no public client can write
those attributes; platform admin/broker assignment paths validate membership and
are the only authorized writers. Reconcile historical values against platform
records before activation. `AdminGetUser` alone does not sanitize a value an old
mapping already corrupted. Keep federation disabled for any unresolved record.

Change the pre-token trigger to read the selected organization from the
platform-controlled user record using `AdminGetUser` for the event's trusted pool
and username, checking its `sub` against the Cognito subject. Ignore candidate
tenant values in event attributes, client metadata and provider claims. Use the
protected record for other authority attributes too. Explicitly override or
suppress organization in **both ID and access tokens**, including refresh-token
events, rather than leaving a stale ID-token claim in place. An unassigned user
may receive onboarding-only tokens with organization authority suppressed; tenant
APIs deny them. Unknown user, subject mismatch or authority-read error must fail
closed, not reach the current broad `except: return event` branch. Preserve the
separate M2M registry-derived claim path, with explicit fail-closed tests.

Add only `cognito-idp:AdminGetUser` on the exact managed pool ARN to the pre-token
role; this is a scoped infrastructure change, not a wildcard grant or a new store.
Never log the full trigger event or token. Native and GitHub login, HostedAuth,
refresh and workspace-switch flows are compatibility controls, not exclusions.

At domain admission, treat the signed org claim as a **candidate selection**, not
a completed `DomainPrincipal`. Split signature/client/token-kind validation from
org authorization. Resolve an active platform membership for the authenticated
subject and candidate tenant through the existing `CurrentIdentityReader`, then
map through `Organization.adp_org_id`, then construct the tenant-bound principal.
Keep current workspace/organization grants as additional authorization. The
reader result must match the requested subject/type/tenant; never select the first
membership for a multi-org user. Absent/revoked membership returns 403; unavailable
identity authority returns 503. Invalid bearer validation remains 401.

For legacy unbound domain UUIDs, require a current server-held local user
membership and applicable grants; never admit on the UUID claim alone. If no
reviewed identity adapter can prove this relationship, deny the tenant route until
the explicit ADP binding is installed. This preserves proven legacy members, not
unverified legacy authority. Remove reliance on the optional current-identity flag
for these tenant decisions; no fallback to legacy JWT acceptance on reader failure.
Existing policy, organization binding and domain guard are the integration points,
not a parallel middleware chain.

### Provider configuration, ownership and compatibility

Remove the organization entry entirely from SAML and OIDC `AttributeMapping`.
Use a positive allowlist of non-authority attributes (initially email), not a
blacklist of a single known tenant key. Validate provider type and URL at the
Pydantic schema boundary: HTTPS, canonical exact hostname on an operator-managed
per-organization allowlist, no credentials/fragment/query, no IP literals, only
the reviewed port (443). The allowlist is private configuration, cannot be changed
by the SSO request, and has no permissive empty/default behavior. Tests use
`https://idp.example.test/metadata`. Do not invent a vendor-wide suffix allowlist or
claim DNS rebinding is prevented by syntax checks. Operators must approve the
actual issuer/metadata origin; this task does not add a general URL fetcher.

New provider names use `org-` plus the full organization UUID. The existing
`sso_provider_name` is a display name and must stay one. Add a nullable unique
`sso_cognito_provider_name` column to the existing organizations table, with an
additive migration and no automatic prefix-based backfill. It records the exact
owned Cognito resource, including a reconciled legacy name. Do not rename a live
provider as an incidental consequence of deploying the new name function.

Before **read, update or delete**, obtain the organization from verified caller
authority, lock its row, and compare the persisted provider ownership binding,
provider type, and provider-side organization identifier where present. Legacy
resources without independently confirmed ownership require explicit operator
reconciliation; matching the old eight-character prefix is never proof. Unknown,
foreign or ambiguous ownership yields a uniform 409 configuration conflict, with
no Cognito mutation or foreign configuration disclosure. Creating a missing
resource is permitted only for that organization's full canonical name; retries
describe and verify it, never adopt a same-named foreign resource.

Serialize changes to the shared app client's provider list across replicas using
the existing Postgres database (transaction-scoped advisory lock keyed by pool and
client). Describe and explicitly preserve supported providers, write/read
attributes, OAuth flows/scopes, callback/logout URLs, auth flows, token lifetimes,
revocation and other mutable settings accepted by the SDK; filter out read-only
response fields. Reassert the protected-attribute write exclusion on every update.
The current `_add_provider_to_client` only copies a subset of settings. Re-read
after mutation before persisting `sso_enabled=true`; external AWS errors are
sanitized 502. A failed partial operation leaves federation inactive and retries
reconcile observed state; a SQL rollback alone does not undo a Cognito update.

Use the same ownership and migration implementation for #6979. A provider-name
change can create a new federated user subject: retain the verified old provider
binding until the identity owner has tested account linking and recovery login.
Disabling federation removes the client reference before optional provider
deletion; never delete another organization's resource or make provider deletion
the rollback for a failed sign-in experiment.

## 3. Knowledge Door repository contract

Reuse `CallerPrincipal`, `PostgresACLStore`, `_build_allowed_lookup`,
`_repo_is_allowed` and `record_acl_denial`, as the verify action already does.
Make `caller` and `acl_store` explicit required arguments for identify and plan;
accept `None` only as an explicit unavailable state that fails closed. The server
must pass the resolved caller; a tool argument must not select the authority.

Resolve permitted repositories **before** any catalog/existence/finding query.
For a named repository outside that set, return the same action-specific
not-found/no-findings envelope as an unknown repository: same status, keys, values,
`repo_indexed=false`, and no distinguishable timing-dependent metadata. Caller
input echoed in `query` may differ only because the supplied name differs. Do not
check global existence merely to decide whether to log a denial. Emit a denial
for every out-of-set name, whether real or nonexistent. The audit event must not
include an allowed-repository sample or foreign catalog facts.

For an allowed repository, canonicalize using the existing full-name matching
rules, and constrain SQL existence and finding queries with that same allowed
set (`repo_name = ANY(...)` or an equivalent parameterized join). Never authorize
by short basename. With CVE/package-only queries, query only allowed repositories;
an empty set skips repository queries entirely. Preserve permitted repository
`repo_indexed=true` even if there are legitimately no findings. Keep ACL filtering
after retrieval as defense in depth, not the sole predicate.

Missing/unresolved identity returns the existing unauthorized empty envelope.
Unavailable/raising ACL store returns one generic unavailable envelope, with zero
repository information and an internal denial reason; it never reports a healthy
empty scan. Use identical failure behavior regardless of the requested repository.
Cover the branch where filtering removes every finding, plan's delegation to
identify, and verify regressions. Preserve legitimate same-tenant diagnostics and
public CVE facts only after authorization; no secret existence flag survives in
summary text, metadata, counts or secondary reachability calls. Byte-level response
equivalence is tested with a fixed clock; constant-time execution is not claimed.

## 4. Delivery, ownership and rollback

This assignment does not run the following operations. The implementation owner
must preserve the canonical [deployment guide](../../adp-platform-deployment/deploy-with-agent.md)
and its selected-connection/approval boundaries. Private configuration and live
evidence stay outside public documentation. The [operator handoff](rollout-handoff.md)
gives the pre-merge gates, private reconciliation receipt, recovery decisions and
post-deployment evaluator handoff; it does not authorize an apply.

| Component / owner | Actual source delivery control | Required order and safe recovery |
|---|---|---|
| Gateway application / Gateway owner | [gateway-deploy.yml](../../../.github/workflows/gateway-deploy.yml#L5) builds/deploys matching source changes on main; manual dispatch also exists. | Validate new service grants/bindings and domain positive controls before enabling strict reads. Coordinate the automatic rollout; do not promise a staging-first merge without selecting a supported staging lane. Roll back to a last-known **secure** image; disable the affected route/caller rather than restoring caller-selected tenancy. |
| Cognito trigger, IAM and client policy / identity + infrastructure owners | [lambda.tf](../../../modules/gateway/infra/modules/cognito/lambda.tf#L12) packages the trigger; [gateway-infra-apply.yml](../../../.github/workflows/gateway-infra-apply.yml#L3) is manual. Gateway image deploy does not publish this trigger. | Freeze SSO mutations, inventory/reconcile mappings and client writers, sanitize selected-tenant projections from approved membership records, grant pool-scoped read authority, deploy fail-closed trigger, validate new and refresh tokens, then enable federation. Keep native recovery login. Rollback retains mapping removal/write restrictions; never reapply the insecure copier as a normal rollback. |
| Superplane API + schema / Superplane owner | [superplane-api-build.yml](../../../.github/workflows/superplane-api-build.yml#L36) builds on main; [superplane-k8s-deploy.yml](../../../.github/workflows/superplane-k8s-deploy.yml#L41), [superplane-migrate.yml](../../../.github/workflows/superplane-migrate.yml) and infra apply are manual. Lock/build scripts and app installer also deliver the component. | Expand schema first; deploy ownership-aware reader/writer; reconcile legacy resources while SSO mutations are paused. Keep additive columns during image rollback. A down migration may remove the new column only after exporting ownership bindings privately and disabling all dependent code. Do not revert provider resources by deleting/recreating them. |
| Door / Agent Context owner | [agent-context-images-build.yml](../../../.github/workflows/agent-context-images-build.yml) builds matching changes; [agent-context-deploy.yml](../../../.github/workflows/agent-context-deploy.yml#L16) chains from successful main builds or manual dispatch. | Deploy ACL precheck and filtered query together; verify positive queries and unavailable-store readiness. Prefer disabling secure identify/plan if rollback would restore the oracle. No new infrastructure/schema is needed for this component. |

No new managed resource is needed for Gateway metadata or Door. SSO adds a database
column and a narrow IAM permission plus reconciliation work. Additional Cognito
reads have latency/quota/cost impact that is **not measured here**, not zero cost.
Avoid per-request logs containing credential metadata; use bounded reason counters
and private audit records. An unexpected denial spike stops rollout; do not turn
off authorization to resolve it. Operations chooses target/staging access and
owns private receipts; the identity owner owns federation repair; the implementer
and reviewer own pre-merge tests; the designated evaluator owns post-deploy S-12.

## 5. Completion and evidence

The design delivery ends at a ready documentation PR and synchronized issue body.
Implementation merge, live verification, and story closure are separate. See
[coverage](coverage.md) for what was actually inspected and checked. Implementation
acceptance requires the story's AC table, the scenario mappings, final source
revision, all attempts/results, and private references for live evidence. Mocked
tests do not establish production availability. No scan ledger or AWS resource
was accessed in this design run.

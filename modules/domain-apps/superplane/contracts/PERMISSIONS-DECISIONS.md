# Permissions contract v1 — #6484

`action-permissions-v1.json` enumerates **all mounted domain and private-domain
routes**; public login/health and machine-only internal routes remain in
`src/superplane-api/app/endpoint_inventory.py`, not user-grant actions. `routes: []`
means no mounted user API or maintained client surface exists, not that an
administration or cluster permission has shipped. The recorded permission on an
organization-scoped route is its *current inventory label*, not a workspace
grant: bound organizations require a separate typed `organization:read` or
`organization:administer` grant (and organization-scoped provisioning/credential
actions currently require administer); unbound legacy organizations use the
conservative all-live-workspaces gate. No ordinary ADP role provides either grant.

Only the mounted route, scope and permission labels are proven by the inventory
test. `principal` and `extra` record declared requirements, not evidence that all
of them are enforced by every listed endpoint. UI route names are checked; CLI
and tool descriptions still need integration evidence. Route presence also does
not mean a capability is enabled. Consumers must preserve these verification
limits rather than generate production authorization from this JSON alone.

Workspace creation uses organization-scoped provisioning authority and records an
explicit creator grant. It is not restricted to the installation's first
workspace; the narrow initial-administrator bootstrap must not become a blanket
rule denying later authorized creation/adoption. Cluster grant resolution accepts
both verified human and service principals with explicit scopes. A dedicated
machine credential for observation transport is not the only possible holder of
cluster observation authority; there is no new principal-type restriction here.

## Authoritative inputs

| Source | What it actually assigns or checks |
| --- | --- |
| `modules/gateway/src/admin/config.py` | `AdminRole`: platform_admin, org_admin, dept_admin, member. Platform authority derives from verified token `is_admin`, not an organization membership row; `membership_role_to_admin_role` maps stored `platform_admin`/`admin` only to **org_admin**, unknown to member. `ROLE_RANK`/`ASSIGNABLE_ROLES` govern platform assignment, not Superplane grants. |
| `modules/gateway/src/auth/dependencies.py` | Verified `account_type` distinguishes human and service; service identities require a validated `client_id`, and revocation checks run for membership context. A token role/group may identify platform admin but does not mint a Superplane workspace grant. |
| `modules/gateway/src/auth/workspaces.py` | ADP workspace selector changes *selected organization* via active `tenant_memberships`; it is not the execution workspace identifier. A selected org_admin is not platform admin. |
| `auth/superplane_auth/policy.py` | Five permissions and implications; the four unused `workspace_viewer`, `workspace_operator`, `workspace_provisioner`, `workspace_owner` mapping names are **candidate domain presets**, not ADP roles. Production reads explicit typed grants and drops unknown permission strings. |

`access-cases-v1.json` pins the two-org/two-workspace/two-human/service examples;
the SQLite-backed grant-layer contract test exercises stored principal type and revocation,
as well as same-org disjoint grants. The role-source check executes Gateway's
membership normalization: even a membership row labeled `platform_admin` remains
selected-org `org_admin`, while an unknown or candidate preset label becomes
`member`. Gateway's verified platform-admin token predicate is distinct from
that selection, and a typed service uses its validated client ID. The fixture
tests deny raw role labels, including department administrator and a service
presenting a platform-admin label, while explicit typed grants remain usable.
Inactive membership/disabled identity cases
are explicitly expected failures pending the live ADP membership contract (#6127),
not assertions that a domain-local grant proves current identity. These cases call
the grant reader, not the HTTP identity guard: their expected failures do not
establish an ingress regression and must be supplemented with composed identity
tests under #6127. The `adp_role` column labels scenarios; no role claim is passed
to this grant-only evaluator, which is why production role consumption remains
#6485's responsibility.

## Owner decisions requested (proposed; no widening shipped)

1. **Preset names/mapping:** Retire the unused `workspace_*` ADP-role helper as a
   role *assignment* path; if presets are needed in an eventual domain access UI,
   propose viewer→read, operator→spend, provisioner→provision+renew, owner→administer.
   Store explicit permission sets, not preset strings. Domain/auth owner approval
   required; existing stored grants are unchanged, raw JWT roles remain inert.
2. **Privilege ceiling:** Propose typed human workspace administrators may grant
   only permissions they hold on the same workspace; organization administrators
   may bootstrap the *first* workspace and grant there only through the reviewed
   installation path. No service self-grant or cross-tenant grant. This would
   constrain future admin surfaces, not change existing ADP admin-role assignment.
3. **Revocation bound:** Propose no positive authorization cache: re-read live
   typed grant at admission and each sensitive operation (current DB behavior),
   fail closed on unavailable storage; require current ADP membership at request
   and at long-running execution (#6127). No fixed seconds-based cache promise
   until shared ADP contract owner confirms its membership revocation bound.
4. **Last administrator:** Propose refuse removal of the final active human
   organization/workspace administrator, with audited owner-operated recovery
   using ADP identity plus explicit grants; never synthesize access from a
   platform_admin role. Requires domain/auth owner approval and a safe admin API.

## Remaining acceptance / owners

- **#6485 domain/auth:** consume this matrix in production decisions; the JSON
  by itself is not a production authorizer. Reconcile
  organization-scoped `workspace:*` inventory labels with explicit organization
  grant semantics before introducing new public routes. No routes are added here.
- **#6486 access administration:** implement the access mutation surface,
  assignment ceiling and final-admin recovery after the owner decisions.
- **#6127 ADP identity/auth owner:** authoritative selected-org active membership,
  disabled principal and typed service/run delegation at admission/execution;
  existing domain grant reads alone do not prove current membership. Shared
  interface changes require separate owner review.
- **#6048 cluster owner:** separate cluster use/admin/observe grants and placement
  eligibility; internal observation receiver routes remain machine-authenticated,
  not a public cluster-observer administration surface.
- **Harness/domain approval owner:** verify selected, distinct, current human
  approver and exact request/workspace/plan binding for approval decision;
  existing organization route permission is only the first gate. CLI `--yes`
  and service tokens never constitute a human approval.
- **Domain/auth owners:** record review of the four proposals before enabling
  assignment/recovery. This PR supplies code-only evidence, not live acceptance,
  migration, infrastructure, or an approval sign-off.

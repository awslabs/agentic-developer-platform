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
conservative all-live-workspaces gate. No ordinary ADP role provides either grant. Each organization-scoped matrix
row now records `organization_grant`, the minimum explicit grant checked for
bound organizations; the `permission` field remains the exact endpoint inventory
label. An organization administrator grant also satisfies an organization read,
but neither organization grant supplies a workspace grant. The same label on an
unbound legacy organization instead requires live grants on every live workspace.
Only the eligible current selected-org administrator may receive the initial
installation grant through bootstrap; revoked access cannot revive that path.

Only the mounted route, scope and permission labels are proven by the inventory
test. `principal` and `extra` record declared requirements, not evidence that all
of them are enforced by every listed endpoint. UI route names and Gateway/onboarding/lifecycle/research CLI action availability are checked; the
MCP capacity tools use their own contract and do not call the domain API. An
`also_surfaces` entry records a second maintained client for the same action;
`unavailable_surfaces` records a command that exists but refuses execution.
Research CLI scan/generate intentionally return `unavailable` before a request,
even though their API endpoints are mounted. Other `surface: absent` entries have no maintained domain-API client action
verified by this matrix. CLI cluster listing is an eligibility view of the
organization workspace list, not a cluster-use grant. Redirected org/user
administration commands remain absent as domain administration surfaces. Route presence also does
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

`access-cases-v1.json` pins the two-org/two-workspace/two-human/service examples
and separate bound-organization/legacy cases for zero and multiple workspaces;
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

## Owner decisions requested (proposals only; no widening shipped)

D1 and D5 require recorded domain/auth-owner disposition; the Harness/domain
approval owner must also agree to the D1 approver handoff. The architect approved
fixed contract fixtures, **not** these policy choices. A grant fixture is not a
safe grant-mutation surface or evidence of current ADP identity at admission.

| Decision | Proposed disposition | Compatibility impact and handoff |
| --- | --- | --- |
| D1 distinct approver access | Before a first-workspace or continuation/retirement approval, an audited, owner-controlled access workflow (#6486) must place a **second**, verified, currently eligible human in the exact organization/workspace approval scope. For the first workspace, issue explicit organization authority before any workspace exists; on existing workspaces require an explicit typed workspace administrator grant. Never use requester self-approval, a service identity, a raw ADP admin role or an approval from another plan. | An installation with only one eligible administrator cannot submit an approved operation until the separate approver handoff exists. No public domain grant administration surface exists today; keep activation pending D1 rather than silently substituting the requester or bypassing approval. The Harness/domain approval owner must confirm the handoff and recovery of no-approver cases. |
| D5 candidate presets | Retire `workspace_*` as an ADP-role assignment path. If a domain access UI later offers presets, propose viewer→read, operator→spend, provisioner→provision+renew, owner→administer; persist explicit typed permission sets, never a preset name or raw token role. | Existing stored grants remain unchanged and candidate role strings remain inert. Offering provisioner would bundle credential renewal with provisioning, so do not expose or migrate this bundle without domain/auth-owner review; importing canonical ADP roles as workspace grants would violate the fixed no-inheritance boundary. |
| D5 assignment ceiling | A verified human workspace administrator may grant only permissions they hold on that workspace. An eligible current selected-org administrator may bootstrap the initial organization/first-workspace grant through the reviewed installation path, not manage later workspaces by role. No service self-grant, cross-tenant grant or ordinary ADP role-based administration. | Constrains a future #6486 administration workflow; does not change Gateway role assignment or existing typed grants. Check original principal and target scope before enabling it; no public mutation route exists now. |
| D5 revocation bound | No positive authorization cache: read live typed grants at admission and each sensitive operation, fail closed when unavailable, and require #6127 current ADP membership/disabled-state checks at request and long-running execution. The shared auth owner must specify the maximum age of the current-identity read before claiming a numeric revocation bound. | Stored domain grants already re-read; composed membership/disabled checks are not established by this fixture. Enforcement may refuse a previously accepted run after revocation, which is intentional. Do not advertise a seconds-based bound or enable stale positive identity caches before shared-owner approval. |
| D5 last-administrator recovery | Refuse removal of the last active **human** explicit organization/workspace administrator. Use a separately audited, owner-operated restoration with verified ADP identity and scoped typed grants; never synthesize access from `platform_admin`. | Recovery remains unavailable until #6486 provides a safe surface and domain/auth owners approve the policy. Single-administrator installations need an authorized second human before removing their only administrator. |

The executable approval fixtures exercise the shared Harness decision predicate with
first-workspace and teardown cases. They do not test the unavailable grant handoff
or replace a composed current-identity check. Neither CLI `--yes` nor a service
run's delegated workspace permission constitutes a human approval; the executor
still checks its own admission authority.

## DESIGN.md and mounted-route reconciliation

This is the maintained action matrix's gap register, not a second authorization
policy. `DESIGN.md` calls `developer`, `workspace-admin` and `org-admin` **display
roles**; these are not ADP roles or aliases for the unused four `workspace_*`
helper names. D5 must select any eventual preset vocabulary. Neither list may
be applied to token role strings or converted into new grants by this PR.

| Boundary | What source and fixtures establish | Remaining obligation / owner |
| --- | --- | --- |
| Mounted API versus capability | The matrix and `endpoint_inventory.py` agree on every domain/private route, including the separate CLI `DELETE` and UI retirement preview/admit routes. UI action names, Gateway/onboarding/lifecycle/research CLI source and unavailable research commands are checked. MCP capacity tools call a separate contract, not these API routes. | A mounted route or `served: true` client declaration is **not** provider readiness, approval, or end-to-end authorization. #6485 consumes fixed grants and permissions in production; #5534/#5535 own protected lifecycle composition. #5386/#5462 can continue their independent job/serving work. |
| Effective access and safe mutation | `workspace.access.effective`, `org.access.effective`, `workspace.access.manage`, `org.access.manage` and `workspace.share` have **no mounted public API or maintained UI/CLI action**. Existing org `/users` and `/orgs/current` endpoints are not a domain typed-grant management or recovery API. | #6486 supplies scoped self-read, typed administration and sharing after D1/D5 disposition, without a role-string fallback. Until then, do not present an effective-access view or self-service grant/approver assignment as implemented. |
| Current identity and typed delegation | SQLite cases exercise domain grants, not a live ADP membership reader; disabled/stale cases remain expected failures. `DESIGN.md` says `CURRENT_IDENTITY_ENFORCED` defaults false until a protected reader is composed. | #6127/shared auth supplies original verified subject, active selected-org membership, disabled status, typed service/run delegation and revocation rechecks. #6485 consumes the shared result; neither a route label nor an expected failure proves current identity at ingress. |
| Cluster placement | CLI `cluster list` is an organization-scoped eligibility view on `GET /workspaces`, not `cluster:use`. No public cluster use/admin/observe grant routes are mounted. Shared-cluster co-location does not share workspace grants. | #6048 owns explicit cluster use/admin/observe and same-org placement; separate executor admission stays required. Internal observation receiver machine credentials are not public cluster-grant administration. |
| Human decisions | `approval.request`, `approval.decision`, lifecycle continuation and retirement admission have distinct, request-bound checks; the Harness decision fixture exercises supplied current-human status, self/revoked/stale denials and exact plan/tenant binding. | D1/Harness/domain approval owner and #6486 must authorize a separate human approver grant handoff. CLI `--yes`, service delegation and an approved request do not replace decision/executor checks. No first-workspace or retirement live acceptance is claimed. |

Code-only fixtures require no Superplane service in ordinary ADP installs; no
runtime service, infrastructure, grant assignment or migration is introduced by
this PR. The #6414 review/live-test hold remains in force. Acceptance here is
bounded source evidence, not rollout, owner approval, or a Demo 1 pass for every
original criterion.

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

# Identity alignment delivery status (#6127)

The authority remains `../DESIGN.md` sections 2.2 and 4.1. This implementation
does not complete #6127 or establish live acceptance.

Workspace HTTP grant reads now bind principal type as well as immutable subject,
mapped organization and workspace. Human and service grants cannot substitute
for each other. The user projection permits the same immutable subject in two
organizations; projection rows are never membership evidence. Tests exercise two
explicit ADP organization mappings and a grant revoked between HTTP requests.

Strict user-management authorization uses the current organization administration
grant, not a display role or token role. These human routes reject service callers.
For ADP-bound organizations, invite, role change and removal explicitly refuse
the legacy Cognito mutation. A domain administrator must not globally disable an
ADP identity or change its global role to remove/change one membership. The legacy
unbound path also refuses when another organization has a projection with the same
subject or email. This comparison only denies mutation; it never links identities
or grants access. Supported ADP membership mutation remains unavailable here.

## Shared interfaces still required

Gateway's bound service `verify-run` operation validates current run/task authority
but does not itself expose current human organization membership. Its
`domain_operation_approval.py` rereads domain approval/grant rows; that is not an
ADP principal-status check. Gateway owns `tenant_memberships` and identity links;
the domain must not copy their lifecycle or infer membership from email.

The domain-owned `app/current_identity.py` exports the typed principal/scope
contract for #6485 and #6048: immutable subject, human/service type, selected ADP
organization, current membership identifier, active and enabled state, and service
delegation. The additive integration is explicitly enabled with
`CURRENT_IDENTITY_ENFORCED=true`; the default false retains the existing strict
signed-token/live-domain-grant behavior and its documented upstream-membership gap.
Enabling requires a configured reader; a refusal never falls back to old behavior.
When enabled, mapped HTTP requests require current evidence, operations derive
mapping from the stored organization rather than optional request fields, and
approvers require current human membership. No positive result is cached.
Unbound legacy installations retain their bounded compatibility path.

Gateway now provides the versioned `/internal/v1/controller-execution/current-identity`
route for registered producers. It returns current, nonrevoked human membership
and Cognito's enabled and selected-organization evidence, and refuses unsupported
service requests. The domain has a typed reader for its signed producer transport,
but startup still does not install the reader. Until API and worker composition are
qualified together, `CURRENT_IDENTITY_ENFORCED` stays off. Explicitly enabling it
without a reader fails `/readyz` and mapped-tenant requests; `/health` reports the
required/configured status. `/api/auth/workspaces` still supplies only human
bootstrap information, not disabled-state or service delegation. The domain never
reads Gateway-owned tables. Worker-side revalidation of the durably recorded
requester and approver at credential and execution boundaries is still required.
Service delegation remains a separate unsupported identity contract.

Migration 036 changes only the `users.cognito_sub` unique index from global to
organization-local; it rewrites no users or references. The PostgreSQL migration
test starts with the preceding index, retains an existing user's ID and reference,
then proves a second organization can hold the same subject. Downgrade cannot
restore the former global uniqueness once such a second row exists: it must fail
pending an explicit, reviewed data-preservation decision, not delete or merge rows.

A supported organization-local membership mutation interface is also required
before these domain user mutations can be enabled for ADP-bound organizations.
It must resolve targets through ADP immutable identities and cannot globally
disable another organization's member. Cluster-use, administration and observation
authority must integrate with #6048 independently of workspace membership.

Remote code-only tests with a deterministic ADP fixture and composed Gateway/API/
registered-worker authority remain required. PostgreSQL migration preservation
passed in the API lane at the reviewed source checkpoint; this does not establish
worker revalidation, service delegation or live acceptance. Refusal of unsafe
mutations is not their completion.

Supervisor repair: this PR is additive integration preparation, not completion of
#6127. It does not turn off the existing `domain_auth_enforced` default or bypass
live domain grants. API and workers must receive the same enablement setting and
protected reader/original-principal composition before rollout; a test fixture is
not a production adapter. Positive request and database-backed worker cases,
missing/mismatched identity and post-read revocation cases are required in CI.

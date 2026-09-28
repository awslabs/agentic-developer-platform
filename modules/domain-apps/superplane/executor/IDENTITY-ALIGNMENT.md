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

Read-only review of Gateway `src/internal/domain_operation_routes.py` found a
bound service `verify-run` operation. It validates current run/task authority but
does not expose current human organization membership. Its
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

The gateway does **not** currently expose an authenticated, current selected-org
identity introspection interface with disabled-principal state and service
delegation. `/api/auth/workspaces` supports only human bootstrap role/membership
checking; `is_current` reflects token context and the response contains neither
enabled state nor service delegation. Consequently the application does not install
a production `current_identity_reader`. Enabling the integration without one
fails `/readyz` and mapped-tenant admission; `/health` separately reports whether
it is required and whether a reader is configured. Do not enable it in a release
until the shared identity owner provides and composes a protected reader. A reader must
authenticate to ADP, validate its response for each immutable subject/selected
organization and principal type, and provide stable membership-generation evidence.
No gateway-owned table is accessed from this domain module. An authenticated
worker-side identity lookup independent of HTTP request context remains a separate
integration requirement for registered-worker recovery and delivery paths.

A supported organization-local membership mutation interface is also required
before these domain user mutations can be enabled for ADP-bound organizations.
It must resolve targets through ADP immutable identities and cannot globally
disable another organization's member. Cluster-use, administration and observation
authority must integrate with #6048 independently of workspace membership.

Remote code-only tests with a deterministic ADP fixture and real PostgreSQL/API/
registered-worker composition remain required. Actual ADP membership withdrawal,
disabled-principal propagation, service delegation, and worker acceptance remain
pending the shared interface. Refusal of unsafe mutations is not their completion.

Supervisor repair: this PR is additive integration preparation, not completion of
#6127. It does not turn off the existing `domain_auth_enforced` default or bypass
live domain grants. API and workers must receive the same enablement setting and
protected reader/original-principal composition before rollout; a test fixture is
not a production adapter. Positive request and database-backed worker cases,
missing/mismatched identity and post-read revocation cases are required in CI.

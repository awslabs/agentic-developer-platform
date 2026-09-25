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

The existing domain HTTP path verifies signed token expiry, explicit tenant
mapping and current domain grants. It does **not** yet independently revoke an
otherwise valid token when ADP membership is removed or its principal is disabled.
Closing that gap requires an authenticated shared interface for exact immutable
subject, selected ADP organization and principal type, returning current membership
and enabled status, with unavailable/ambiguous results refused. The same authority
must be checked during approval and registered-worker mutation, not just login.
No invented endpoint or local membership cache has been added.

A supported organization-local membership mutation interface is also required
before these domain user mutations can be enabled for ADP-bound organizations.
It must resolve targets through ADP immutable identities and cannot globally
disable another organization's member. Cluster-use, administration and observation
authority must integrate with #6048 independently of workspace membership.

Remote code-only tests are required for these changes. Actual ADP membership
withdrawal/disabled-principal propagation and registered-worker acceptance remain
pending the shared interface; refusal of unsafe mutations is not their completion.

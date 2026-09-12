# Organization workspace switching

Part of EPIC #4839; follows the org-local placement model used by #4943/#4946.

## User behavior

The header's **Organization** selector lists the signed-in person's memberships,
including organizations with no GitHub installation. Selecting an organization
saves its org, primary team, department, and membership role to the existing
Cognito login. The browser refreshes both tokens, verifies their scope, and
navigates to the dashboard with a fresh document. This also resets component
state, pending requests, and query caches. The Connections-page switch uses the
same transition.

The selection persists through token refresh and subsequent login. It is a
preference on the Cognito login: other sessions retain their existing signed
scope until refresh, at which point they adopt the latest saved selection. It is
not an independent, permanent workspace choice per browser tab.

A platform administrator retains platform authority from current Cognito attributes
or groups; a stale token cannot restore a revoked global role. Their workspace still
chooses the organization/team used for ordinary usage. Selecting a workspace
does not promote that organization's membership to platform administrator.

## Identity and authority

- `users.cognito_sub` stays unique on the original login account. Each placement
  retains its own `users.id`, membership, teams, and organization-owned data.
- Authorized placement adds a `user_identities` link with provider `cognito`, the
  login's sub, and verification method `org_placement`. A proven workspace switch
  can establish the same link. This supports native Cognito accounts without
  requiring GitHub.
- Existing GitHub placements are discoverable using the immutable numeric ID in
  the **validated token's** `GitHub_<id>` username. Arbitrary self-linked external
  identities and mutable email addresses are not login proof. Conflicting account
  ownership and ambiguous matches fail closed.
- Role resolution reads the membership for the signed `org_id`. Another
  session's `is_active` flag cannot supply authority for that token. The existing
  least-privilege fallback for an old native account's own org is retained.
- Service and IAM principals cannot use the human workspace-switch endpoints.

No schema migration or additional AWS permissions are required. Existing native
placements made before this change, with no trusted login link, need the admin
placement action repeated once to establish that link. The action is idempotent;
login resolution deliberately does not infer ownership from matching email.

## Cost, routing, and credentials

The refreshed token supplies the selected organization and its primary team and
department together. No primary team means empty team/department claims, clearing
any previous workspace's values. Budgets and rate limits consume that scope.
The direct-user spend identity remains the Cognito sub; native placements share
the same person identity for existing aggregate/default budget enforcement.
This does not introduce a new namespace for authoring individual person caps.

Vault, AWS connection, and Bedrock user routing lookups resolve the selected
organization's local user ID. Credentials and existing history stay in their
original organizations. A legacy membership attached to another org's user row
uses the selected org/team's routing; it cannot carry the original user's AWS
override across. Admin placement creates the local account needed to configure
personal routing in that workspace. This change does not copy credentials, migrate history,
or implement Wave 2's per-run scoped GitHub credential minting.

## Writes and failure handling

A database lock on the canonical login serializes selections through the Cognito
write. Target user and membership rows are locked and reloaded so a concurrent
removal or role change is observed. Role-edit claim synchronization uses the same
login lock and updates only the role when its organization is currently selected.
Explicit global promotions/demotions apply without changing the workspace; a global
demotion restores the selected org's membership role. Ordinary org-role edits do
not revoke global administrator access.

Cognito updates are required for switch success. A failed update rolls back the
membership-selection transaction and returns 503. If the database commit fails
after Cognito succeeds, the previous attributes are restored on a best-effort
basis. These stores do not support a shared transaction; an ambiguous network
failure or failed compensation can require reselecting the workspace. Existing
tokens remain signed to their previous scope.

If the browser cannot refresh or receives mismatched claims after a successful
selection, it clears local tokens and requests sign-in. It never announces a
successful switch with a stale token. A background refresh that changes workspace
or role also reloads the document to clear old page state.

## Validation and rollout

Regression coverage includes native placement, existing GitHub secondary rows,
role/team scope, platform administrators, unauthorized and ambiguous identities,
credential isolation, budget/rate keys, Bedrock destination selection, rollback,
role edits, and UI refresh failures. PostgreSQL tests exercise simultaneous
switches and membership deletion/demotion while a switch waits on a row lock.
Run those against a disposable database with `TEST_WORKSPACE_POSTGRES_URL`.

Deploy the gateway and frontend together through their normal release workflow.
For the live check, sign in with a user in two orgs, select the second org in the
header, confirm its agent/usage scope, refresh and sign in again, then switch back.
Reverting the application change leaves placement links and memberships intact;
legacy UI behavior returns, so reselect/sign-in may be needed after rollback.

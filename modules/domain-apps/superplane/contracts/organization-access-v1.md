# Organization access mutation contract

Apply domain migration `044_organization_grant_changes` before rolling out the
updated API. It adds revision 1 to existing organization grants and an empty,
tenant-bound mutation ledger. It neither creates authority nor clears revoked
records. Downgrading after ledger entries exist refuses to discard that evidence.
The release remains unverified; the schema pin is not rollout approval.

## Supported human operations

The existing organization self-access and administrator assignment-list routes
now return stored grant revisions and sanitized mutation provenance. Their
organization authorization remains independent of workspace and cluster grants.

- `POST /orgs/current/access/v1/grants` assigns or replaces explicit
  `organization:read` and/or `organization:administer`. Administration implies
  organization read, not workspace or cluster authority.
- `POST /orgs/current/access/v1/grants/{grant_id}/revoke` revokes the exact
  organization assignment. The row and its original permission values remain as
  evidence. Parent revocation also denies its cluster child scopes under #6048's
  existing contract; child rows and generations are not modified.

Requests identify the immutable `target_subject`, `principal_type: human`, an
`expected_revision` and a UUID `request_id`. Assignment includes a nonempty list
of known `permissions` and `reason: access_assignment`; revocation uses
`reason: access_revocation`. Revision 0 means a new assignment; stored grants
require their current positive revision. Unknown permissions, duplicates,
presets, service targets, free-form reasons and extra fields are rejected.

The actor and organization are derived from verified identity, never the body.
Both actor and target need current same-organization human membership from ADP.
Mutations serialize on the bound organization, refresh and lock its explicit
administrator grant, enforce the assignable ceiling, and recheck membership
before committing. Platform roles, workspace grants and cluster administration
cannot substitute for organization administrator authority.

## Audit, replay and policy gates

The grant, revision, tenant-scoped audit event and actor-bound replay identity
commit together. Event or ledger failure rolls back the whole mutation.
Identical retries return the persisted result only while current authority and
the recorded grant revision still match. Changed payloads, reused request IDs
from another actor/operation, and stale revisions conflict. Revoked grants
cannot be restored, including by replaying an earlier assignment request.

Responses distinguish the original stored `granted_by`/`granted_at` from the
latest audited `changed_by`/`changed_at`, reason and request identity. Existing
grants retain honest stored provenance until an audited mutation occurs. The
existing organization event API exposes evidence only within the caller's
authorized tenant; workspace read alone does not authorize that event API.

Self-assignment is rejected. Self-revocation has an explicit pending-policy
conflict, preserving the unresolved #6484 last-administrator/recovery gate.
There is no restoration endpoint or platform/org-role override. Missing current
target membership fails closed rather than inventing a departed-principal
recovery exception. Service assignment, presets and cluster grant administration
remain gated on their recorded owner decisions/contracts.

Revocation removes future authority; it does not erase already-issued
credentials or cancel admitted work. Existing governed cancellation/recovery
checks still apply. No numeric revocation bound or live qualification is claimed.

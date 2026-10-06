# Workspace access mutation contract

The versioned workspace access API uses explicit, current human workspace
administrator grants. Organization/platform roles, cluster permissions and
workspace co-location do not authorize these operations. The configured ADP
identity reader must establish current same-organization human memberships for
both actor and target before a mutation and again before its commit.

## Revoke an assignment

`POST /workspaces/{workspace_id}/access/v1/grants/{grant_id}/revoke` accepts:

- `target_subject`: the immutable subject on the stored grant, not an email.
- `principal_type`: `human`; service-target policy remains gated.
- `reason`: `access_revocation`; free-form text and extra fields are rejected.
- `expected_revision`: the positive integer revision from assignment listing.
- `request_id`: a UUID retained across retries of this exact request.

The scope and grant identity come from the path; actor and organization come
from verified server-side identity. A successful response contains the persisted
grant identity, incremented revision, revocation time, actor, reason and request
identity. Effective permissions are empty and `revocation_effect` is
`future_authority_only`.

The workspace lock serializes this operation with assignment/replacement. The
grant row and its assigned permissions remain as revocation evidence. The
tombstone, tenant-scoped audit event and actor-bound request ledger commit in
one transaction. An audit failure rolls back the mutation. Identical retries
return the persisted result only while the actor remains authorized, current
memberships remain valid, and the grant still matches the recorded revision.
Reusing a request ID for a different actor, operation or payload conflicts.
Stale revisions and new requests to revoke an already revoked grant return 409.
Assignment cannot restore the tombstone, including by replaying an older grant
request. Assignment listing includes sanitized revocation provenance; the
existing workspace event stream requires independent current read authority.

## Policy gates and effect boundary

An administrator may revoke another current human's grant, including another
administrator's grant. Self-revocation returns an explicit pending-policy
conflict: last-administrator removal and recovery are not enabled while the
recorded #6484 owner decisions remain outstanding. There is no organization or
platform administrator bypass and no restoration endpoint. Removal of a target
whose current membership cannot be established also fails closed; this contract
does not invent a recovery exception for departed principals.

Later authority checks deny the revoked grant. This does not erase issued
credentials or cancel already-admitted work; those remain subject to governed
cancellation/recovery and their own operation-time authority checks. No numeric
revocation bound, deployment, live migration or live qualification is claimed.

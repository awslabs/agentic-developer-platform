# S11 offline identity provenance inventory

This tool inventories an offline snapshot and produces candidate evidence and
exact state fingerprints. It has no database connection, provider client,
apply/relabel/downgrade/promotion/rollback or projection mode. It preserves all
input authority labels and never declares a historical link proven merely from
its current method. No live rows or deployment dates have been established.

Reviewed source: `113858f2bd4915d8dd80626792e90e5ccb52e9fe`.
History `e7622b2a27bc44d661a3a6953bbb17d97ed30e12` introduced the automatic
`admin_manual` writer, shadow audit, `is_shadow` and migration008 together.
`1f56d35de5b795add4596a49faf3c23ad51bae5e` changed new automatic writes to
`channel_placement`. Commit dates are not deployment evidence. A pre008
automatic writer or audit gap is not supported by that history.

## Read-only export contract

An independently authorized operator can export these three SELECT results
from one consistent read-only PostgreSQL snapshot, bound to `$1 = target org`.
The tool itself never executes SQL. Do not export passwords, nonce/token
payloads, usernames, email, channel contexts or arbitrary audit JSON. Provider
account IDs are personal identifiers even when numeric; protect the input and
manifest files. Assemble the result arrays as `user_identities`, `users`, and
`security_audit_logs`; timestamps must be ISO8601 with an explicit timezone,
booleans JSON booleans and nulls JSON nulls. Identity fields below are mandatory,
including explicit nullable version fields; missing is not equivalent to null.

```sql
SELECT ui.id, ui.org_id, ui.user_id, ui.provider, ui.provider_user_id,
       ui.verification_method, ui.created_at, ui.verified_at, ui.updated_at,
       ui.team_id, ui.is_primary
FROM user_identities ui
WHERE ui.org_id = $1
ORDER BY ui.id;

SELECT u.id, u.org_id, u.is_shadow, u.user_kind, u.bot_kind,
       u.created_at, u.updated_at
FROM users u
WHERE u.org_id = $1
   OR EXISTS (SELECT 1 FROM user_identities ui
              WHERE ui.org_id = $1 AND ui.user_id = u.id)
ORDER BY u.id;

SELECT sal.id, sal.org_id, sal.event_type, sal.actor_id,
       jsonb_build_object(
           'provider', sal.details->>'provider',
           'provider_user_id', sal.details->>'provider_user_id',
           'shadow_user_id', sal.details->>'shadow_user_id',
           'verification_method', sal.details->>'verification_method',
           'delivery_method', sal.details->>'delivery_method',
           'ownership_proven', sal.details->'ownership_proven'
       ) AS details,
       sal.created_at
FROM security_audit_logs sal
WHERE sal.org_id = $1
  AND sal.event_type IN ('shadow_user_created', 'identity_linked',
                        'identity_unlinked', 'magic_link_consumed',
                        'magic_link_issued')
ORDER BY sal.created_at, sal.id;
```

Users referenced by a tenant identity are retained even if their tenant is
wrong. A mismatched user causes an explicit contaminated-snapshot failure,
instead of disappearing through an inner join. A missing user remains a
per-identity review flag. The synthetic SQL test executes these SELECTs against
an in-memory schema and checks missing/mismatched users are not silently lost;
it is not a live PostgreSQL export receipt. Tenant/user/provider/account event
matching is tested independently at the actual classifier boundary.

## Invocation and output

```
python modules/gateway/scripts/identity_provenance_inventory.py \
  --snapshot private-snapshot.json --tenant org-example \
  --output new-private-manifest.json
```

The optional `--source-sha` must be a full lowercase SHA. It records the operator's
source reference; it does not authenticate a snapshot or enable proof. The
script defaults to the reviewed source above. Schema version is 2.0.0.

Stdout contains counts, a manifest hash and status only. It omits tenant,
source/output paths and raw IDs. Private output is created exclusively, mode
0600; existing files (including the input), symlinks and public output targets
are refused. Input is a regular file up to32MiB, maximum100,000rows/table; output
is capped at64MiB. Errors use a fixed redacted message. Do not treat file hashes
as evidence that an export is authentic.

Exit0 means an empty inventory with no conflicting audit IDs, not security
closure. Exit1 means unresolved provenance. Exit2 means malformed/contaminated
input, invalid metadata or protected-output failure. All nonempty inventories
currently remain unresolved: the available source schema does not carry the
trusted current-identity lifecycle linkage needed to establish historical
proof. The tool is useful for finding exact candidate evidence, contradictions,
missing users, dedup conflicts and stale review manifests; it is not an
identity authority oracle.

## Evidence classification

`recorded_trust_label` separates a current proven/unproven/unknown method from
historical evidence classification. `review_required` flags contradictions,
possible shadow origin, missing users, lifecycle anomalies and relinks;
`insufficient_evidence` describes a row without sufficient trusted history.
`authority_action` is always `none`. Genuine manual/OAuth/platform/bot links,
including manual additions on still-shadow users, are preserved unchanged.
Their current label or user kind is not proof of historical origin.

Exact candidate matching uses tenant, provider, external account and user.
`shadow_user_created` names its user in `details.shadow_user_id`.
Current `identity_linked` and `magic_link_consumed` use `actor_id` for the
consumer; they do not have `details.user_id`. Unlink/issue actors cannot be
assumed to establish ownership. Account-related events with another or missing
user are flagged separately and never treated as matched evidence.

`shadow_user_created` lacks identity-row ID, so even a matching tuple does not
prove origin after deletion/recreation. Events before current-row creation are
flagged using chronological timezone-aware comparison. Current
`identity_linked` records `ownership_proven` and delivery, but lacks a durable
identity-lifecycle binding/trusted exporter chain. `magic_link_consumed` has no
delivery field. Neither an invented delivery field, `provider_dm`,
`provider_asserted`, caller `user_id`, `identity_id` nor a `trusted` boolean
promotes a row. Same-holder proven confirmation preserves both method and
verified_at; repeated link events are ambiguous rather than proof of a relink.
A null verified_at plus an ownership claim is a review flag, not proof of when
or why a timestamp was cleared.

Different versions of one event ID are excluded from evidence and cause review;
identical duplicates are deduplicated. Canonical order and duplicate processing
are deterministic. No arbitrary audit details are copied to private output.
A different account's event cannot classify a later legitimate manual link as
automatically created. Absence of audit, shadow status, actor null, timestamps
or user labels alone never authorize correction.

## Stale-review detection and future work

Canonical JSON row fingerprints bind identity ID, tenant, user, provider,
account, created_at, method, null-safe verified_at/updated_at, team and primary
state. They avoid delimiter collisions and distinguish null from a string.
Semantic snapshot hashes bind all input rows and evidence; manifest hashes also
bind classification/source/version. Reordered rows and identical audit
duplicates are stable; new evidence, changed users, partial exports,
delete/recreate, same-method updates and nonnull-to-null transitions invalidate
`snapshot_still_matches`. This is an offline comparison, not a SQL executor or
proof that no change occurred after comparison.

Any later repair requires separately reviewed canonical evidence, locked
current-row and evidence revalidation, exact null-safe predicates, atomic
mutation+audit and cross-store reconciliation. No automatic restoration to
`admin_manual` or another proven method is safe. Forward recovery is a new
inventory and independent proof/reverification. Existing GitHub-specific
`backfill_identity_provenance.py` has orphan/cross-store atomicity limits and is
not universal multi-provider repair. A09/S11/S12/S13 own the related authority
and audit contracts; this tool does not alter them. S11 #5610 remains open.

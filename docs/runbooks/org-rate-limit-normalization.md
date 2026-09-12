# Organization rate-limit normalization (#4952)

The admin API stores organization limits as `org`. The limiter previously looked
them up as `organization`, so those configured limits did not apply. The fix uses
`org` throughout and accepts `organization` as a legacy input alias.

## Before merge and deployment

`gateway-deploy.yml` automatically deploys backend changes on merge, then runs
new migrations. Review the target environment's stored limits **before merging**:

```sql
SELECT id, org_id, entity_type, entity_id, rpm, tpm,
       concurrent_requests, updated_at
FROM rate_limit_configs
WHERE entity_type IN ('org', 'organization')
ORDER BY org_id, entity_id, entity_type;

SELECT org_id, entity_id, COUNT(*) AS configs
FROM rate_limit_configs
WHERE entity_type IN ('org', 'organization')
GROUP BY org_id, entity_id
HAVING COUNT(*) > 1;
```

Confirm that these are the limits the operator intends to enforce. Previously
inert settings can begin returning 429 responses when the new backend serves
traffic. Compare the settings with recent usage, including the configured burst
capacity. Resolve duplicate scopes explicitly; do not choose one by row order.
This review does not authorize changing any live limits.

## Deployment behavior

The backend accepts both stored spellings immediately, including before migration
044 runs. Its config reload uses the same key builder as enforcement. If both
spellings occur for a scope, it logs a warning and applies the strictest positive
value for each limit; zero or missing values cannot override a positive ceiling.

Migration `044_ratelimit_org_type` stops before writing if an org scope has
multiple config rows. Otherwise it changes only `entity_type='organization'` to
`org`. Limits, IDs and timestamps are preserved; rerunning it is safe. Resolve
any duplicate warning before deployment rather than relying on this fallback.

## Verification

Use an approved test organization and record its original settings. Configure a
small org RPM limit through the admin UI. Send enough requests to exhaust its
configured token-bucket burst capacity, and verify that subsequent requests are
denied with 429 and the expected org limit. Requests in another organization must
remain unaffected. Restore the original settings and verify they reload within
the service's 60-second config interval.

Confirm the admin list returns `entity_type: org`, the migration reached head,
and no duplicate-scope warnings remain. Team, department and user limits retain
their existing key formats.

## Rollback

Reverting the backend fix restores the old behavior, including the ineffective
admin-authored org limits. Downgrading migration 044 intentionally leaves `org`
unchanged because the old admin API already uses that spelling. Do not rename
all rows back to `organization`; that would break the admin API's lookup and
deletion paths. Changes to Redis counter keys may reset the affected org buckets
during rollout or rollback.

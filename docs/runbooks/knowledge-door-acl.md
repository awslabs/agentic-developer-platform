# Knowledge Door ACL and ingestion rollout (#5658)

The code requires a working ACL catalogue, explicit ingestion scope, and recorded
repository ownership. Missing configuration, unknown private ownership and
ambiguous repository aliases deny reads. This change has not deployed services,
changed IAM, repaired live rows or demonstrated live tenant isolation.

## Before an authorized rollout

1. Resolve the existing ACL database and ensure the service identity can read its
   `repositories` schema. Both `deploy.sh` and the Actions deployment use
   `resolve_acl_config` before rendering the ConfigMap: `AC_RDS_HOST` is preserved
   when set, otherwise read from `/adp/<environment>/rds/endpoint`; defaults are
   database `agent_context` and user `agent_context_svc`. No database is created.
   Missing resolution stops deployment. Keep `TENANT_SCOPE_ENABLED=true` and the
   existing authenticated Door transport configured.
2. Reconcile legacy `tenant_id` and `owner_sub` from the authoritative registered
   tenant/user records, including their stored artifacts. Do not infer the current
   tenant ID from a GitHub organization name or run `backfill_tenant_scope.py`
   blindly. The ACL backfill below does **not** repair ownership or move artifacts.
   Re-ingestion refuses conflicting ownership instead of silently relabelling it.
3. Ensure ingestion publishers send explicit `scope.visibility` plus the relevant
   tenant/owner identifier. Trusted shared publishers must explicitly say
   `shared`; malformed or absent scope is refused. Tenant and personal artifacts
   use `tenants/<tenant>/...` and `users/<owner>/...` consistently through reads.
4. Check that the GitHub credential can inspect each private repository and its
   collaborators/teams. For classic tokens this includes appropriate `repo` and
   `read:org` scopes; installation tokens need equivalent installed repository
   and organization permissions. Failed ACL derivation produces a deny ACL.
5. Inventory external S3 sources. Configure only reviewed `bucket/prefix` entries
   in `S3_SOURCE_ALLOWLIST`; whole-bucket entries are refused. The pipeline bucket
   does not imply shared access, and allowlisting cannot override another tenant's
   or person's prefix. Preserve legitimate source ownership before rollout.
   Gateway admission uses `AGENT_CONTEXT_S3_BUCKET` for this same pipeline bucket;
   keep it consistent with the ingestion configuration. Registration, retries,
   background change probes and queue consumption all validate sources. HTTP
   documents remain supported by trusted file publishers; the customer document
   registry retains its existing S3-only source contract.
6. Review the proposed IAM prefix list against deployed consumers, including
   `personal-context/*`, `tenants/*`, `users/*`, legacy code indexes and shared
   content. Prefix narrowing is code only until separately applied. A broad
   `tenants/*` IAM prefix is not tenant isolation; the application ACL enforces it.

## Backfill and verification

Run from `modules/agent-context` in an authorized environment with the existing
DB connection settings and GitHub credential. Commands below are guidance; none
were executed against a live database as part of this change.

```sh
# Dry-run is the default; there is no --dry-run flag.
python scripts/backfill_repo_acls.py
python scripts/backfill_repo_acls.py --repo org/repository

# After reviewing the plan, use a new journal path for each apply.
python scripts/backfill_repo_acls.py --apply --journal /secure/path/acl-change.json
python scripts/backfill_repo_acls.py --verify
```

The tool re-derives rows carrying the legacy `["*"]` sentinel. Positively public
repositories remain public; private repositories receive derived ACLs. Unknown
rows are reported and skipped unless `--deny-unknown` is explicitly chosen.
`--verify` re-confirms public rows and fails for unresolved/private sentinel rows.
Do not roll out while those rows are unresolved. Verification does not establish
correct ownership or exercise live Door reads.

The journal is created with mode 0600, flushed and fsynced before the database
commit; an existing journal is never overwritten. Apply compares the planned old
ACL so a newer concurrent update is preserved. Protect the journal and record
any skipped rows for reconciliation.

```sh
# Review rollback first; add --apply only for an authorized restoration.
python scripts/backfill_repo_acls.py --rollback /secure/path/acl-change.json
```

Rollback restores a row only if its current ACL still matches the journal's new
ACL, preserving subsequent changes. Restoring `["*"]` can reopen disclosure;
review each row and re-derive with corrected permissions afterwards. Do not
restore availability by disabling Door authentication or tenant enforcement.

## Compatibility and runtime acceptance

Public shared repositories and authorized tenant/personal repositories remain
readable. Supported aliases resolve to the exact permitted catalogue identity;
colliding aliases fail closed. Unowned private records require reconciliation.
Semantic search applies current repository ACLs after index selection, including
when access has been revoked or an index still contains a stale private chunk.
Crawling preserves JavaScript-rendered public pages, but internal destinations,
unsafe redirects, WebSockets and service-worker registration are refused.

After an authorized deployment, confirm `/ready` succeeds only with a usable ACL
schema, fails during database loss, and recovers afterwards. `/health` remains a
liveness signal. Exercise search, browse, understand, impact and secure verify
with two tenants and two owners: each must see its own permitted content and
shared public content, never the other's private content. Confirm normal agent
issue dispatch still works and automatic PR-open review remains disabled.

Actions use ARC and the existing CodeBuild projects. The current main-branch
image build chains into `Agent Context Deploy`; a code-only merge must prevent
that exact merge's automatic rollout. Do not disable the webhook stack or
repository-wide workflows to achieve this. Live rollout and data repair remain
separate operations requiring authorization.

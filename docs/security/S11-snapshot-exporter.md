# S11 identity snapshot exporter

Bounded read-only PostgreSQL snapshot exporter for S11 identity provenance
inventory (#6071). Connects to a dedicated `s11_inventory` database role via
projection views, exports per-tenant snapshot files, and runs offline
provenance classification via `identity_provenance_inventory.py`.

The dedicated role, projection views, and their grants are owned by root DBA
setup, not this script. This script never executes DDL, writes to the database,
accesses base tables directly, or stores credentials.

## Prerequisites

The DBA must create the `s11_inventory` role and four projection views before
this exporter can run:

| View | Source | Columns |
|------|--------|---------|
| `s11_inventory.organizations` | `public.organizations` | `id` |
| `s11_inventory.identities` | `public.user_identities` | `id, org_id, user_id, provider, provider_user_id, verification_method, created_at, verified_at, updated_at, team_id, is_primary` |
| `s11_inventory.users` | `public.users` | `id, org_id, is_shadow, user_kind, bot_kind, created_at, updated_at` |
| `s11_inventory.audit` | `public.security_audit_logs` | `id, org_id, event_type, actor_id, created_at, details` |

The `s11_inventory` role must have SELECT on these views only. No INSERT,
UPDATE, DELETE, or TRUNCATE on any table. No SECURITY DEFINER routines. The
views must expose only the columns listed above — no passwords, email, tokens,
nonce payloads, or arbitrary audit JSON.

The audit view's `details` column must filter to the allowlisted keys:
`provider`, `provider_user_id`, `shadow_user_id`, `verification_method`,
`delivery_method`, `ownership_proven`. The `ownership_proven` key preserves
its boolean/null type.

## Invocation

```bash
python modules/gateway/scripts/identity_snapshot_exporter.py \
  --host db.example.com \
  --port 5432 \
  --sslrootcert /secure/us-east-1-bundle.pem \
  --dbname gateway \
  --user s11_inventory \
  --output-dir /secure/exports \
  --source-sha <40-char-hex-sha> \
  --region us-east-1
```

The script generates an ephemeral RDS IAM auth token in-process. It never
accepts master credentials, password URLs, human sessions, or prints/stores the
token.

## Safety contract

1. **Read-only transaction**: a single `REPEATABLE READ READ ONLY` transaction.
   The script verifies the connected principal, isolation level, read-only
   status, UTC timezone, and view schema/columns/ownership before any data
   queries.

2. **Privilege verification**: refuses to proceed if the role has write
   or raw-read privileges on base tables or can invoke SECURITY DEFINER routines,
   including PUBLIC grants and roles reachable through SET ROLE.

3. **Bounds**: 100,000 rows per table per tenant, 32 MiB per tenant, 128 MiB of snapshot/manifest output per run, 300 second
   cooperative wall-clock deadline, 15 second maximum statement timeout, 2 second lock timeout, 60
   second idle-in-transaction timeout. Named PostgreSQL cursors fetch one row at
   a time; row and byte limits are checked while consuming results. Tenant
   discovery is limited to 10,000 identifiers and 1 MiB. A single fetched row
   can exceed the byte limit before rejection. Deadline checks surround fetches
   and classification; this is not a process-level watchdog.

4. **All-or-nothing completeness**: if any tenant fails or any limit is hit, the
   entire run is marked INCOMPLETE. Partial files stay quarantined; no final
   success receipt is written.

5. **Output security**: private 0700 run directory, exclusive 0600 files, no
   symlink following or overwrite. No raw identity values in filenames, argv, or
   stdout. The private receipt records tenant identifiers, source, schema, file hashes,
   and row counts. Do not publish it; stdout reports aggregate counts only.

6. **No credential leakage**: IAM token is ephemeral and in-process only. Tests
   use local SQLite with dummy AWS credentials and disabled IMDS.

## Tenant scope

Canonical tenant set: `organizations.id UNION DISTINCT org_id` from identities,
users, and audit views. This preserves orphan tenants that appear in only one
table.

Users referenced by a tenant's identity are included even if their `org_id`
differs (cross-tenant reference). This makes mismatches visible in the snapshot;
the offline inventory will flag the mismatch rather than silently dropping the
user.

## Output

Each run creates a unique directory under `--output-dir`:

```
run-20260925T120000Z-12345/
  snapshot-<tenant-index>-<hash16>.json    # per-tenant raw snapshot (one per tenant)
  manifest-<tenant-index>-<hash16>.json    # per-tenant inventory manifest (one per tenant)
  receipt.json              # private run-level receipt (tenant identifiers)
```

Export completion is separate from provenance outcome. A tenant with status
`EXPORTED` has its data written; provenance classification may still be
unresolved (the inventory intentionally leaves rows unresolved without
trustworthy lifecycle proof).

## Relationship to existing tools

- **`identity_provenance_inventory.py`**: the offline classifier, unchanged.
  This exporter produces snapshot files that the inventory consumes.
- **`backfill_identity_provenance.py`**: the DDB projection backfill (issue
  #5664). Separate concern — this exporter does not touch DynamoDB.
- **S11 #5610**: the parent security story. This exporter is the missing DB
  snapshot acquisition adapter. Root reviews and executes later.

## Tests

55 tests in `tests/admin/test_identity_snapshot_exporter.py`:

- Tenant discovery (canonical + orphan + empty)
- Identity/user/audit fetch with field preservation
- Cross-tenant referenced user inclusion
- Audit event allowlist filtering and detail key filtering
- Full export pipeline with inventory classification
- Classification failure for cross-tenant snapshots (expected)
- Bounds enforcement (row/byte/wall-clock limits)
- Output security (exclusive files, permissions, no symlink, no ID leakage)
- Connection verification (principal, isolation, read-only, timezone)
- Schema validation (missing views)
- CLI argument validation

Tests use disposable local SQLite with dummy AWS credentials and disabled IMDS.
Additional disposable PostgreSQL tests exercise streaming export, snapshot isolation,
read-only transactions, PUBLIC grants, inherited grants and SECURITY DEFINER rejection.
No production database connections in tests.

# Runbook: Gateway Alembic Migrations

This runbook covers the full lifecycle of Alembic migrations for the gateway's
dev Postgres instance: checking state, triggering migrations manually, understanding
when auto-trigger fires, recovering from a partial apply, and troubleshooting common
errors.

**Related workflows**

| Workflow | Purpose |
|---|---|
| [`.github/workflows/run-gateway-migrations.yml`](../../.github/workflows/run-gateway-migrations.yml) | One-shot migration runner (manual + called by deploy) |
| [`.github/workflows/gateway-deploy.yml`](../../.github/workflows/gateway-deploy.yml) | Deploy pipeline with migration auto-detection (PR #444) |

---

## Section 1 — Check current migration state

Run this before and after any migration operation to confirm what revision the
database is on.

```bash
export AWS_PROFILE=<profile>
aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini current'
```

**Interpreting the output**

| Output | Meaning |
|---|---|
| `008_magic_link (head)` | DB is fully up to date. No action needed. |
| `005_identity_columns` (no `(head)`) | DB is behind. Revisions 006–008 are unapplied. Run migrations. |
| *(empty)* | Alembic has never been run against this DB, or `alembic_version` table is missing. Run `upgrade head`. |
| `Error: Can't locate revision identified by '...'` | The revision in `alembic_version` doesn't exist in `versions/`. See Section 5. |

To see the full chain and which revisions are pending, run:

```bash
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini history --verbose'
```

---

## Section 2 — Manually trigger migrations

### Via GitHub Actions UI (preferred)

1. Go to **Actions** → **Run Gateway Alembic Migrations (one-shot)**.
2. Click **Run workflow** → select branch `main` → **Run workflow**.
3. The job prints `=== Before ===` (current revision), runs `alembic upgrade head`,
   then prints `=== After ===` (new revision). Both should read `(head)` after a
   successful run.

### Via GitHub CLI

```bash
gh workflow run run-gateway-migrations.yml -R aws-e/adp --ref main
```

Watch the run:

```bash
gh run list -R aws-e/adp --workflow=run-gateway-migrations.yml --limit 1
gh run watch <run-id> -R aws-e/adp
```

### Directly in the pod (break-glass only)

Use this only when GitHub Actions is unavailable or you need to run a specific
revision rather than `head`.

```bash
export AWS_PROFILE=<profile>
aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1

# Upgrade to head
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini upgrade head'

# Upgrade to a specific revision
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini upgrade 006_user_roles_table'
```

---

## Section 3 — When auto-migration triggers automatically

`gateway-deploy.yml` calls `run-gateway-migrations.yml` **after** the backend deploy
job. Its `run-migrations` condition currently runs when the changes job selected
a backend deployment **or** the workflow was manually dispatched, provided the
backend job succeeded or was skipped. A migration-only push selects a backend
rebuild and deployment because migration files are baked into the release image.
A frontend-only push does not select a backend migration run.

The called workflow runs `scripts/pricing-rollout.py migrate`, which invokes
`alembic upgrade head` and performs release/pricing verification. Manual dispatch
does not exempt a deployment from migrations. Check the actual jobs and
`alembic current` after a release; a successful backend rollout alone does not
prove its migration job succeeded.

This ordering matters for validation-only revisions such as 048: the new gateway
image may already be serving traffic when a migration refuses to advance. A
checkpoint failure is an upgrade failure, not an application rollback or runtime
access barrier. Perform the pre-deploy audit in Section 6 first.

---

## Section 4 — Partial-apply recovery

### Why partial applies happen

Postgres DDL runs outside an implicit transaction when issued via SQLAlchemy's
`op.create_table()` / `op.add_column()`. If a migration script creates several
objects and then raises an exception before completing, the objects created up to
that point are committed to the DB — but Alembic's `alembic_version` row is never
updated because the version bump is part of the same transaction as the migration
body.

The next `alembic upgrade head` attempt will re-run the same migration and fail
immediately with `DuplicateTableError` or `DuplicateColumnError`.

### The idempotent upgrade pattern

Migrations 005–008 in this repo use an inspector-based guard to handle this
transparently. When writing new migrations, follow the same pattern:

```python
import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_cols = {c["name"] for c in inspector.get_columns("users")}

    # Guard table creation
    if "my_new_table" not in existing_tables:
        op.create_table(
            "my_new_table",
            sa.Column("id", sa.String(length=36), nullable=False),
            # ... other columns ...
            sa.PrimaryKeyConstraint("id"),
        )

    # Guard column addition
    if "new_col" not in existing_cols:
        op.add_column(
            "users",
            sa.Column("new_col", sa.String(length=255), nullable=True),
        )

    # Guard index creation — fetch fresh after potential table create
    existing_indexes = {i["name"] for i in inspector.get_indexes("my_new_table")}
    if "ix_my_new_table_id" not in existing_indexes:
        op.create_index("ix_my_new_table_id", "my_new_table", ["id"])
```

### Recovery procedure

1. **Identify what was partially applied** — run `alembic current` (Section 1) and
   check which revision was last committed. Then inspect the schema directly:

   ```bash
   kubectl exec -n adp-gateway deploy/bedrockgateway -- \
     sh -c 'cd /app && PYTHONPATH=/app python3 -c "
   import sqlalchemy as sa
   from src.config import settings
   e = sa.create_engine(settings.database_url)
   print(sa.inspect(e).get_table_names())
   "'
   ```

2. **Add idempotent guards** to the offending migration (see pattern above) and
   push the fix to `main`. The push will trigger a new deploy + migration run.

3. **Re-run migrations** — the guards ensure the already-created objects are
   skipped, and the remaining objects are created. Alembic updates
   `alembic_version` on success.

4. **Verify** — run `alembic current` again and confirm it shows `(head)`.

---

## Section 5 — Troubleshooting

### `DuplicateTableError` or `DuplicateColumnError` on upgrade

**Cause**: a previous migration attempt created some objects but aborted before
`alembic_version` was updated. The migration is re-running from scratch and
hitting the already-created objects.

**Fix**: add idempotent inspector guards to the migration (Section 4), then
re-trigger `run-gateway-migrations.yml`.

---

### `alembic current` shows a revision that doesn't exist in `versions/`

**Symptom**:
```
ERROR [alembic.util.messaging] Can't locate revision identified by 'abc123xyz'
```

**Cause**: a migration file was pushed, the version was stamped (or an apply
succeeded), and then the migration file was removed from the repo.

**Fix**: stamp the database to the most recent valid revision manually:

```bash
# Find the most recent valid revision
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini history' | head -5

# Stamp to that revision (replaces the invalid value in alembic_version)
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini stamp <valid-revision-id>'

# Verify
kubectl exec -n adp-gateway deploy/bedrockgateway -- \
  sh -c 'cd /app && PYTHONPATH=/app alembic -c alembic.ini current'
```

---

### Migration workflow times out or hangs

**Checks to run in order**:

1. Confirm the pod is in `Running` state:
   ```bash
   kubectl get pods -n adp-gateway
   ```
   If the pod is `CrashLoopBackOff` or `Pending`, the migration workflow cannot
   exec into it. Fix the pod first (`kubectl describe pod -n adp-gateway <pod>`).

2. Confirm DB connectivity from inside the pod:
   ```bash
   kubectl exec -n adp-gateway deploy/bedrockgateway -- \
     sh -c 'PYTHONPATH=/app python3 -c "
   from src.database import engine
   with engine.connect() as c: print(c.execute(\"SELECT 1\").scalar())
   "'
   ```
   A timeout here indicates a security group issue — the pod's security group must
   allow outbound TCP to the RDS instance on port 5432.

3. Check the workflow run logs:
   ```bash
   gh run list -R aws-e/adp --workflow=run-gateway-migrations.yml --limit 3
   gh run view <run-id> -R aws-e/adp --log
   ```

---

### Reference incidents (session context)

- **#457 — table collision**: migration 008 first attempted to create a table
  named `audit_logs`, which collided with an existing admin table. The migration
  aborted mid-run, leaving `magic_link_nonces` in the DB without an
  `alembic_version` entry. Documented as a `DuplicateTableError` partial-apply.
- **#458 — idempotent recovery**: migration 008 was fixed to use inspector-based
  guards (the pattern in Section 4) and re-run successfully. The `security_audit_logs`
  rename resolved the collision; `magic_link_nonces` was skipped because it already
  existed.

---

## Section 6 — Team integrity checkpoint (048, issue #4924)

Revision `048_team_integrity_gate`, after `047_claude_pricing_v2`, refuses to
advance when a membership references a missing user/team, its org disagrees with
its user's or team's org, or a nonempty `users.team_id` names a missing/foreign
team. It checks ownership only: a valid legacy pointer without a membership row
is allowed, and `team_id = ''` remains the intentional no-team sentinel. It does
not impose a new primary-team policy.

Applied revision 040 remains unchanged. On an older upgrade path, 040 can still
copy a cross-org legacy pointer; 048 rejects that resulting state. It also rejects
invalid nonempty pointers that 040 skipped, so those cannot be mistaken for a
clean upgrade. This change provides **detection and upgrade refusal**, not
automatic repair, a database constraint, or a runtime authorization barrier.
Queries neither update/delete rows nor create missing memberships.

The [September 13 read-only audit](https://github.com/aws-e/adp/issues/4924#issuecomment-5652135838)
found zero ownership/pointer mismatches in adp-dev-example-profile at revision 047 across
three transactions. Its successful source reads contained 25 users, 32 teams,
27 orgs and 25 memberships. No data repair was justified there; this is a
point-in-time observation for that environment only.

### Before deploying this revision

Run the candidate checkout's script from an authorized environment that can
reach the intended database. It uses the gateway's existing `BG_DATABASE_URL` or
`BG_RDS_*` configuration and IAM/TLS support; do not print credentials. Confirm
the AWS account with the active profile before accessing an AWS environment.
Use the candidate checkout before rollout: the gateway image does not package
the `scripts/` directory. This is operator checkout tooling.

```bash
aws sts get-caller-identity
cd modules/gateway
PYTHONPATH=. python scripts/audit_team_integrity.py
```

All counts and optional details use a PostgreSQL `REPEATABLE READ`, `READ ONLY`
transaction, and the script verifies read-only mode. Counts are the default;
`--include-ids --detail-limit 100` adds bounded internal identifiers per finding
class for placement review. It never includes emails or credentials. Store that
output as restricted operator evidence rather than posting user identifiers in
a public issue.

| Exit / status | Meaning |
|---|---|
| 0 / `CLEAN` | Every checked ownership predicate passed in this snapshot; inspect source counts. An empty database is valid but does not prove live-user coverage. |
| 3 / `SOURCE_POINTERS_CLEAN` | An explicit `--source-pointers-only` diagnostic passed. `memberships_checked` is false. Full validation remains incomplete; this never returns exit 0. |
| 1 / `INCONSISTENT` | At least one membership or pointer requires placement review. Stop rollout and investigate. |
| 2 / `ERROR` | Configuration, connection, schema or query failed. No consistency conclusion is available. Error output is redacted to the exception class. |

The default audit returns `ERROR` if any required table is absent, including
`team_memberships`: missing schema is never inferred to mean a pre-040 database.
For a verified pre-040 database, `--source-pointers-only` can diagnose its legacy
pointers, but always leaves full validation incomplete (exit 3 if pointers are
clean). On an uninitialized database without source tables, verify that it is
the intended fresh database before following the normal initial-deployment
procedure. A clean fresh migration chain is accepted by 048.

### If the audit or upgrade refuses

Preserve before-images of affected rows and collect their canonical/org-local
identity linkage, all valid memberships, valid primary, selected workspace and
identity-pointer projections. An operator must choose the intended placement;
do not infer a replacement from the oldest team, move membership org IDs, remove
history, or clear a pointer merely to make the checkpoint pass. Any approved
repair needs its own reviewed transactional procedure and separate review of
Cognito/DynamoDB consequences. Neither this script nor 048 has an apply mode.

Do not edit 040, stamp past 048, or treat a failed query as zero mismatches.
Before retrying, rerun the audit and verify the actual database revision. The
048 checkpoint itself performs no data writes, so dirty rows are preserved for
review. PostgreSQL can roll back the current migration transaction, but this
does not undo earlier committed transactions or explicit autocommit operations
elsewhere in a long upgrade chain, and does not roll back the deployed image.
Downgrading 048 only removes its version marker; it changes no rows or schema.

---

## Section 7 — Cross-references

| Resource | Link |
|---|---|
| One-shot migration workflow | [`.github/workflows/run-gateway-migrations.yml`](../../.github/workflows/run-gateway-migrations.yml) |
| Deploy pipeline with auto-migration detection (PR #444) | [`.github/workflows/gateway-deploy.yml`](../../.github/workflows/gateway-deploy.yml) |
| Example idempotent migration (005) | [`modules/gateway/alembic/versions/005_identity_columns.py`](../../modules/gateway/alembic/versions/005_identity_columns.py) |
| Example idempotent migration (008) | [`modules/gateway/alembic/versions/008_magic_link.py`](../../modules/gateway/alembic/versions/008_magic_link.py) |
| Incident: table collision | Issue #457 |
| Incident: idempotent recovery | Issue #458 |
| Gateway README | [`modules/gateway/README.md`](../../modules/gateway/README.md) |

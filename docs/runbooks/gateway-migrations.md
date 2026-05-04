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

`gateway-deploy.yml` calls `run-gateway-migrations.yml` automatically, but only
under a specific condition:

**Auto-trigger fires when**: a `push` to `main` changes at least one file matching
`modules/gateway/alembic/versions/*.py`.

**Auto-trigger does NOT fire when**:

| Scenario | Why migrations don't auto-run |
|---|---|
| `workflow_dispatch` of `gateway-deploy.yml` | The `changes` job explicitly sets `migrations=false` for manual deploys. |
| A PR adds a migration file without touching `modules/gateway/src/**` | The `gateway-deploy.yml` `push` trigger only fires when `src/**`, `Dockerfile`, `k8s/**`, `frontend/**`, or `alembic/versions/**` changes. A migration-only PR that doesn't touch `src/` still triggers the push filter (because `alembic/versions/**` is in the path list), **but** if `gateway-deploy.yml` itself was dispatched manually for that merge it would set `migrations=false`. |
| A migration PR is merged via squash and the squash commit doesn't touch `alembic/versions/` | Unlikely, but the filter is path-based, not PR-label-based. |

**Bottom line**: after any `workflow_dispatch` deploy, and after any merge where
you're unsure whether the push filter fired, manually verify with `alembic current`
(Section 1) and trigger `run-gateway-migrations.yml` if needed (Section 2).

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

## Section 6 — Cross-references

| Resource | Link |
|---|---|
| One-shot migration workflow | [`.github/workflows/run-gateway-migrations.yml`](../../.github/workflows/run-gateway-migrations.yml) |
| Deploy pipeline with auto-migration detection (PR #444) | [`.github/workflows/gateway-deploy.yml`](../../.github/workflows/gateway-deploy.yml) |
| Example idempotent migration (005) | [`modules/gateway/alembic/versions/005_identity_columns.py`](../../modules/gateway/alembic/versions/005_identity_columns.py) |
| Example idempotent migration (008) | [`modules/gateway/alembic/versions/008_magic_link.py`](../../modules/gateway/alembic/versions/008_magic_link.py) |
| Incident: table collision | Issue #457 |
| Incident: idempotent recovery | Issue #458 |
| Gateway README | [`modules/gateway/README.md`](../../modules/gateway/README.md) |

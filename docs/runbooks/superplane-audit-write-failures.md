# Runbook: Superplane audit-write failures

Covers the alert that fires when the Superplane control plane cannot write an audit
record, what it does and does not imply, and how to confirm the audit trail is healthy
after the A17 change (issue #5673).

## What the audit trail records, and why a gap matters

`app/middleware/audit.py` is the **only** application-level audit trail in Superplane.
There is no database trigger, no outbox and no second writer behind it. The
infrastructure access logs carry request lines but no authenticated principal, so they
cannot answer "who did this" and are not a substitute.

Since #5673 each record carries the acting **principal** (who), the **tenant** (whose
resources), and an **outcome** of `allowed`, `denied` or `error`. Denied and unauthenticated
attempts are recorded, which is the point: a probe against another tenant's workspace is
the event the trail exists to surface.

## The alert

**Signal.** A `WARNING` log line from `app.services.audit`:

```
audit record NOT persisted: method=<M> path=<P> reason=<R> total_failures=<N>
```

`total_failures` is a per-process count for the lifetime of that pod, so it resets on
restart. Alert on the **rate** of these lines, not on the absolute number.

**Recommended condition.** Any occurrence over a 5-minute window, since a healthy audit
path produces none. Route it wherever the Superplane API's other application warnings go.

> **Not yet provisioned as infrastructure.** This repository has no CloudWatch metric
> filter or alarm for the Superplane API (checked: `platform/infra/modules/` has no
> alerting module for it, and `superplane-platform-monitor` watches budget and cluster
> health, not application logs). This section is the alert specification; whoever adds
> the metric filter owns turning it into a live alarm. Until that exists, **the
> fail-loudly behaviour has a log line but no automated consumer** — the query below is
> the manual check.

## What the alert means — and what it does not

An audit write failure means **a record was lost**, not that a request failed. The
failure policy is deliberate and tested: an audit fault never fails the caller's
request, because a transient database problem in the audit path would otherwise reject
legitimate provisioning calls for every tenant at once, turning a logging outage into a
control-plane outage. The lesser failure — a gap operators can see — is the one chosen.

So when this alert fires:

- Customer requests are **still being served**. Do not treat it as an outage.
- There is a **hole in the audit trail** for the duration. Record the window; any
  incident review covering it has incomplete evidence.

The log line deliberately contains no exception text, because a database error can carry
a connection string and this path runs on already-rejected requests where
attacker-supplied material is in scope. The underlying exception goes to the separate
`audit persistence raised for ...` line with a traceback.

## Triage

Substitute the installation's `namespace` value (`superplane` in the shipped defaults)
for `<namespace>` below.

1. **Confirm the scope** — is it one pod or all of them?

   ```bash
   kubectl logs -n <namespace> -l app.kubernetes.io/name=superplane-api \
     --since=15m --prefix | grep 'audit record NOT persisted'
   ```

   `--prefix` names the pod on each line, which is what distinguishes one unhealthy
   replica from a database-wide problem.

2. **Find the cause.** The warning carries a fixed `reason`. Driver exception
   text is deliberately excluded because it can contain SQL parameters or connection
   credentials. Inspect database health, connection-pool pressure and migration state
   using the operator's existing observability tools.

   Most likely causes, in order: database connection exhaustion (the audit write opens
   its own session, so it competes for the pool), the events table being unwritable, or
   a migration not yet applied — if `029_add_event_principal_outcome` has not run, every
   insert fails on the missing `principal`/`outcome` columns.

3. **Check the migration state** if the failures started right after a rollout. Schema
   changes run as a one-shot `superplane-migrate-<run-id>` Job, not from the API pod, so
   check the Job rather than exec-ing into the API:

   ```bash
   kubectl get jobs -n <namespace> -l app.kubernetes.io/part-of=adp-superplane \
     --sort-by=.metadata.creationTimestamp
   kubectl logs -n <namespace> job/superplane-migrate-<run-id>
   ```

   The installer asserts the reported revision equals the head pinned in
   `releases/superplane.lock.yaml` (`029_add_event_principal_outcome`) and refuses
   otherwise, so a mismatch surfaces as a failed install rather than a running pod with
   the wrong schema. If the Job succeeded and inserts still fail on the new columns,
   the API image and the applied schema are from different releases — see "Ordering".

## Confirming the trail is healthy

Run this after the A17 rollout, and after resolving any alert. It is also the check that
distinguishes "no traffic" from "recording nothing" — the failure mode A17 fixed, where
the enforced-authentication configuration silently wrote zero rows.

```sql
-- Records in the last hour, split by outcome. BOTH counts should be non-zero on a
-- system taking real traffic: zero denials means refusals are not being recorded.
SELECT outcome, COUNT(*)
FROM events
WHERE created_at > now() - interval '1 hour'
GROUP BY outcome;
```

```sql
-- Attribution check on NEW rows: `principal` must name the acting subject, never the
-- tenant. The pre-A17 defect wrote the organization id into the actor column, so a new
-- row where principal equals org_id means the defect has returned by another route.
SELECT COUNT(*) AS wrongly_attributed
FROM events
WHERE principal IS NOT NULL
  AND principal = org_id::text;
```

Expect `wrongly_attributed = 0`.

Note that this query does **not** flag the historical defect, because pre-A17 rows have
`principal IS NULL` — those rows carry the tenant in `user_id` instead, which is why that
column is documented as legacy rather than trusted:

```sql
-- Extent of the historical mis-attribution. Informational: these rows cannot be
-- repaired, because the acting person was never recorded anywhere to recover from.
SELECT COUNT(*) AS legacy_tenant_as_actor
FROM events
WHERE principal IS NULL
  AND user_id = org_id::text;
```

## Reading historical rows

Rows written **before** this change mean something different, and the difference is
visible in the data rather than needing to be remembered:

| Column | Pre-#5673 rows | New rows |
|---|---|---|
| `principal` | `NULL` | the acting subject, or `unresolved` |
| `outcome` | `NULL` | `allowed`, `denied` or `error` |
| `user_id` | an **organization** id (not a person) | left to other writers |
| `org_id` | always set | `NULL` when no identity was established |

Two consequences for anyone querying this table:

- **`outcome IS NULL` does not mean "allowed."** Pre-fix rows only ever recorded
  successes, so it happens to be true of them, but it is not a recorded fact. The column
  was left nullable rather than defaulted precisely so a reader can tell the difference.
- **`org_id IS NULL` rows are unattributed attempts** — recorded because an
  unauthenticated probe is worth keeping. They are deliberately **not** visible through
  the tenant-scoped `GET /events` API, since an attempt with no established identity
  cannot be shown to a tenant without guessing whose it was. Query them directly.

## Ordering: migration before image

Migration `029_add_event_principal_outcome` must be applied **before or with** the image
that writes the new columns. It is additive (two columns, one index) plus one constraint
relaxation (`events.org_id` becomes nullable), so:

- The **previously deployed image keeps working** against the new schema — it references
  neither new column and always supplies an `org_id`. Applying the migration early is
  safe.
- **Rolling back the image does not require reversing the migration.** The old code
  ignores the new columns.
- **Downgrade refuses while unattributed rows exist.** It preserves those records
  rather than deleting evidence to restore `org_id NOT NULL`. Exporting, preserving
  and cleaning up that evidence requires a separate operator decision. Reversing the
  migration also removes the principal/outcome columns; prefer an image rollback.

## Volume and the read-coverage flag

`AUDIT_READ_COVERAGE` controls whether **reads** are audited. It is `false` in
`installation/manifests.py` and defaults to `false` in code. It does **not** gate
auditing of mutating requests, which are always recorded.

Recording denials means a caller who can generate rejected traffic can drive audit
writes, and enabling read coverage multiplies row volume further by putting a write on
the hot path of every `GET`. If audit volume becomes a storage or latency problem:

1. Set `AUDIT_READ_COVERAGE=false` (or confirm it already is) to shed read volume — this
   is the first lever, and it needs no code change.
2. Check row growth before enabling it anywhere:

   ```sql
   SELECT date_trunc('hour', created_at) AS hour, COUNT(*)
   FROM events
   GROUP BY 1 ORDER BY 1 DESC LIMIT 24;
   ```

The events table has **no retention or archival policy** in this repository. With
denials now recorded, growth is higher than before A17; sizing that policy is
outstanding follow-up work, not something this runbook can resolve.

## Related

| Path | Purpose |
|---|---|
| `modules/domain-apps/superplane/src/superplane-api/app/middleware/audit.py` | The middleware, with the failure policy and content boundary documented inline |
| `modules/domain-apps/superplane/src/superplane-api/app/services/audit.py` | `log_event` and the `audit_write_failures` counter |
| `modules/domain-apps/superplane/src/superplane-api/tests/test_audit_middleware.py` | The invariants, including "no path returns without a row or a counted failure" |
| `modules/domain-apps/superplane/src/superplane-api/alembic/versions/029_add_event_principal_outcome.py` | The schema change and evidence-preserving downgrade refusal |

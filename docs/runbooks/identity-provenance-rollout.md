# Identity Provenance Enforcement — Ordered Rollout

**Subsystem:** Webhook ingress (agent authority) + Gateway (identity index projection)
**Issue:** #5664 (A10), parent #5677

## Why this runbook exists

`user_identities.verification_method` records HOW a link between a platform user
and an external account was established. Before #5664 the webhook path granted
human dispatch authority from the mere EXISTENCE of an identity row: a link a user
merely asserted about themselves ("my GitHub id is `<someone else's id>`") was
indistinguishable, at the point of decision, from one the provider confirmed via
OAuth. Naming another person's GitHub user id was enough to have their comments
attributed to them and to act with their authority.

`common/agent_authority.py::from_verified_webhook` now refuses to mint authority
for a resolution that is not proven, **unconditionally** — there is no flag to
re-permit unproven links.

**That refusal is what makes ordering matter.** The DynamoDB identity tables the
webhook hot path reads are projections of Postgres, and until this change neither
carried `verification_method` at all. So every pre-existing row answers "unknown",
and unknown fails closed. Republish the enforcing Lambda before the projection is
in place and **every human dispatch is denied platform-wide** — an outage, not a
fix. An earlier slice on this issue shipped the check behind an allow-by-default
flag for exactly this reason; the flag is now gone because the data gap it covered
for is closed by the steps below.

This is the same code-before-config hazard as
[GitHub Auth Allowlist Remediation](./github-auth-allowlist-remediation.md), with
the same remedy: land the thing the new code depends on first.

## What must land before enforcement

| Order | Component | Where | What it does |
|---|---|---|---|
| 1 | Gateway writers | `src/admin/identity/*`, `src/admin/identity_index.py` | Set `verification_method` on every new/updated identity row, on both tables |
| 2 | Backfill | `modules/gateway/scripts/backfill_identity_provenance.py` | Projects the attribute onto rows that already exist |
| 3 | Enforcing Lambda | `webhook-ingress/lambda/common/agent_authority.py` | Denies unproven/unknown/mismatched provenance |

Steps 1 and 2 are **forward-compatible and inert**: the old Lambda ignores an
attribute it does not read, so both can land, be verified, and sit in production
for as long as you like before step 3. That slack is the point — do not compress
it.

> **Terraform:** no change is required. Neither identity table has a GSI on
> `verification_method`, and DynamoDB is schemaless for non-key attributes, so
> adding it is a data-plane change only.

## Step 1 — Deploy the gateway writers

Ships with the normal gateway deploy (`gateway-deploy.yml`, or
`modules/gateway/scripts/` for a manual run). No special sequencing.

Verify that newly written rows carry the attribute — pick an account that has
signed in or been re-approved since the deploy:

```bash
aws dynamodb get-item \
  --table-name adp-<env>-user-identity-index \
  --key '{"provider":{"S":"github"},"provider_user_id":{"S":"<numeric-github-id>"}}' \
  --query 'Item.verification_method' --profile <profile>
```

Expect a method string (`oauth`, `org_placement`, `admin_manual`,
`magic_link_confirmed`). If the attribute is absent, step 1 has not actually
reached this environment — stop here.

## Step 2 — Backfill existing rows

Dry-run first. It writes nothing and prints the reduction it would apply per
account:

```bash
cd modules/gateway
DATABASE_URL=postgresql+asyncpg://... \
IDENTITY_INDEX_TABLE=adp-<env>-identity-index \
USER_IDENTITY_INDEX_TABLE=adp-<env>-user-identity-index \
AWS_REGION=us-east-1 \
python scripts/backfill_identity_provenance.py --dry-run
```

Read the output before proceeding, specifically:

* **`holds disagreeing provenance across tenants`** warnings. One external account
  can hold a proven link in tenant A and a self-asserted one in tenant B (the
  unique index is per `provider, provider_user_id, org_id`), and the DDB key has no
  org component. Those accounts project **unknown** — deliberately, because
  projecting the more permissive of the two would let proof earned in A mint
  authority in B. They are not broken: the resolver's canonical lookup is
  tenant-scoped, so it answers precisely and overwrites the projected value. They
  only need the gateway to be reachable, which step 3's verification covers.
* **The ambiguous count** in the summary line. If it is a large fraction of your
  accounts, investigate before enforcing rather than after.

Then run it for real:

```bash
DATABASE_URL=... IDENTITY_INDEX_TABLE=... USER_IDENTITY_INDEX_TABLE=... \
python scripts/backfill_identity_provenance.py
```

It is idempotent and safe to re-run. It **exits non-zero on any failure** — treat a
non-zero exit as "the backfill did not complete" and do not proceed to step 3.

Read the summary line, not just the exit code. It reports four counts:

```
Projection complete: 412 succeeded, 7 skipped (no existing row), 0 failed, 2 ambiguous (projected unknown), 421 total
```

* **succeeded** — provenance now on the row in both tables. This is the number that
  must be non-trivial before you proceed.
* **skipped** — no such row in a projection table, so there was nothing to update.
  Not a failure (the normal write-through creates those rows carrying provenance
  already), but it is *not* progress either. A run that is nearly all skips means
  you are pointed at the wrong table or the wrong environment — check before
  proceeding, because the exit code will still be 0.
* **failed** — non-zero exit. Fix and re-run.
* **ambiguous** — one account whose tenants disagree; projected as unknown on
  purpose, and decided per-request by the tenant-scoped canonical lookup.

The script only copies what Postgres recorded. It never upgrades a value: legacy
ambiguous `magic_link` rows stay `magic_link` (unproven), because rewriting them to
a proven value would be inventing evidence that was never collected.

### Verify the backfill

Count rows still missing provenance. A full scan is acceptable here — these tables
are small (one row per linked account):

```bash
aws dynamodb scan --table-name adp-<env>-user-identity-index \
  --filter-expression 'attribute_not_exists(verification_method)' \
  --select COUNT --query 'Count' --profile <profile>
```

Expect `0`. A non-zero count is the population that will be refused in step 3.

## Step 3 — Republish the enforcing Lambda

Only after step 2 verifies clean:

```bash
cd modules/agent-factory/webhook-ingress
./scripts/deploy-webhook-ingress.sh
```

### Verify enforcement, both directions

Both halves matter. Checking only that dispatch still works proves nothing about
the gate; checking only the refusal proves nothing about availability.

1. **Legitimate path still works.** Have an OAuth-linked user comment
   `@agent-developer` on an issue in an installed repo. It must dispatch as before.
2. **Refusals are visible.** Watch the deny metric:

```bash
aws cloudwatch get-metric-statistics \
  --namespace ADP/AgentAuthority --metric-name UnprovenIdentityAuthority \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Sum --profile <profile>
```

A **zero** count with normal dispatch traffic is the healthy state. A **non-zero**
count means real senders are being refused because their rows lack provenance —
that is a backfill gap, and the response is to go back to step 2 for those
accounts, **not** to weaken the gate:

```bash
python scripts/backfill_identity_provenance.py --provider-user-id <numeric-github-id>
```

Also check the Lambda logs for the withheld-provenance path, which indicates the
canonical lookup authoritatively disagreed with a DDB row:

```bash
aws logs tail /aws/lambda/adp-<env>-github-webhook --since 30m --profile <profile> \
  | grep -i "withholding provenance"
```

## Rollback

There is no flag to flip, and that is intentional — an env var that re-permits
unproven links would reintroduce the vulnerability by configuration.

If enforcement must be backed out, **roll back the Lambda code** to the prior
published version. The gateway writers and the backfilled attribute can stay: the
old code does not read them, so leaving them in place costs nothing and means a
re-attempt does not need step 2 again.

```bash
aws lambda update-function-code --function-name adp-<env>-github-webhook \
  --s3-bucket <artifact-bucket> --s3-key <previous-key> --profile <profile>
```

Prefer fixing the backfill gap over rolling back: a rollback restores a window in
which a self-asserted identity link mints human dispatch authority.

## Notes

* **Nothing here rewrites Postgres.** The backfill only writes the DynamoDB
  projection. `user_identities` is the source of truth and is untouched.
* **The projection is a cache, not the authority.** When the tenant-scoped
  canonical lookup answers, its provenance wins over the projected value; the
  projection exists so the hot path is not blocked on a gateway round-trip.
* **Both sides of the proven-method vocabulary are drift-guarded** by
  `modules/gateway/tests/internal/test_provenance_policy_lockstep.py`. The Lambda
  cannot import the gateway's `PROVEN_METHODS` (the Lambda zip is rooted at
  `lambda/`), so the sets are duplicated and that test is what keeps them equal. A
  Lambda set WIDER than the gateway's is a bypass.

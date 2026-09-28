# Identity Provenance Enforcement — Ordered Rollout

**Subsystem:** Webhook ingress (agent authority) + Gateway (identity projection)

**Issue:** #5664 (A10), parent #5677

## Why ordering matters

`user_identities.verification_method` records how a platform user was linked to
an external account. The webhook authority gate refuses unproven or unknown
links. Older DynamoDB projections do not carry that method, so enforcing the
gate before projection repair can deny legitimate senders when canonical proof
is unavailable. A source merge alone does not establish rollout completion.

For the combined A10 release that also introduces durable installation
revocation, follow the
[combined rollout sequence](installation-revocation.md#combined-a10-rollout).
It requires installation traffic and mutations to remain quiesced across the
entire mixed-version and reconciliation window. The table below describes
standalone provenance enforcement; it must not be used to enable new teardown
while old installation readers remain. Its writer verification and backfill
completion gates still apply before traffic resumes in the combined rollout.

| Order | Component | Required outcome |
|---|---|---|
| 1 | Gateway identity writers | Canonical provider/account/user/tenant binding and truthful method reach both required projection tables |
| 2 | Backfill and reconciliation | Existing matching bindings carry their canonical methods; required mapping gaps are resolved |
| 3 | Enforcing webhook Lambda | Proven users can dispatch and unproven users are refused; per-tenant evidence is recorded |

Use the approved deployment procedure for the target environment. These steps
involve live data and deployments; this runbook is not evidence they were run.
No Terraform schema change is needed for the non-key provenance attribute.

## Step 1 — Deploy and verify the gateway writers

Deploy the reviewed gateway release through the normal deployment path. Verify
new identity writes against Postgres and **both** projection tables. Checking
only the presence of a method does not verify its ownership binding.

```bash
aws dynamodb get-item \
  --table-name adp-<env>-user-identity-index --consistent-read \
  --key '{"provider":{"S":"github"},"provider_user_id":{"S":"<numeric-github-id>"}}' \
  --query Item --profile <profile> --region <region>

aws dynamodb get-item \
  --table-name adp-<env>-identity-index --consistent-read \
  --key '{"identity_type":{"S":"github_user"},"identity_value":{"S":"<numeric-github-id>"}}' \
  --query Item --profile <profile> --region <region>
```

For each row, compare its provider/account key, `user_id`, `org_id` and
`verification_method` to the **same** canonical `user_identities` row. Methods
must reflect the actual enrollment path; legacy `magic_link`, `self_asserted`
and other unproven links must not be relabeled as proven to restore dispatch.
If a required table is absent or dual-write is disabled, resolve that deployment
configuration before using this two-table cutover procedure.

## Step 2 — Backfill existing bindings

The script reads canonical GitHub accounts and both projection tables. It only
changes `verification_method` and `updated_at`; membership, user/bot classification
and other attributes remain with their existing owners. It does not create a
missing mapping, reassign a user or tenant, or change Postgres.

One provider account can have different links in different tenants. For example,
`github:123 → user-a/org-a/oauth` and `github:123 → user-b/org-b/self_asserted`
are distinct canonical bindings. A DDB row for `user-a/org-a` receives `oauth`;
a row for `user-b/org-b` receives `self_asserted`. A mixed `user-b/org-a` row
receives no proof. The source tenant is not necessarily the repository's target
tenant: `any_adp_user` and membership policies remain the resolver's routing
responsibility. Do not infer that cross-tenant routing itself invalidates proof.

Dry-run first; it reads both stores and plans changes without writing:

```bash
cd modules/gateway
DATABASE_URL=postgresql+asyncpg://... \
IDENTITY_INDEX_TABLE=adp-<env>-identity-index \
USER_IDENTITY_INDEX_TABLE=adp-<env>-user-identity-index \
AWS_REGION=us-east-1 \
python scripts/backfill_identity_provenance.py --dry-run
```

Inspect every incomplete account and its per-table result:

| Result | Meaning | Next action |
|---|---|---|
| `planned` / `updated` | Exact canonical binding found; its method will be / was copied | Verify whether that canonical method is proven for the intended action |
| `missing` | This account's required projection row is absent; nothing created | Repair the full mapping through the canonical writer, then retry |
| `mismatched` | Existing user/tenant tuple has no canonical match | Real run conditionally clears proof; repair the full mapping before retry |
| `ambiguous` | Multiple canonical records claim the exact projected tuple | Real run conditionally clears proof; investigate source consistency |
| `conflict` | Binding, method or version changed between DDB read and write | Reread canonical state and retry after the concurrent writer settles |
| `error` | Canonical read, table schema, permission or service failure | Correct the actual error and rerun; it is not a missing row |

An incomplete dry-run exits nonzero, even though it wrote nothing. A missing
`github_user` row for an account selected from Postgres is a required projection
gap. Legacy installation and reverse-index rows do not represent users and are
not targets for this script; they do not need `verification_method`.

After reviewing the plan, perform the authorized write pass:

```bash
DATABASE_URL=... IDENTITY_INDEX_TABLE=... USER_IDENTITY_INDEX_TABLE=... \
python scripts/backfill_identity_provenance.py
```

Both tables are attempted independently. An account counts as complete only when
both succeed; `partial` means at least one succeeded and another required result
did not. **Any incomplete account exits nonzero.** Clearing stale proof is a
security repair, but is still incomplete until the mapping is reconciled. Do not
advance the cutover on a partial result. Retry a repaired account with:

```bash
python scripts/backfill_identity_provenance.py --provider-user-id <numeric-github-id>
python scripts/backfill_identity_provenance.py --user-id <ADP-users.id>
```

`--user-id` chooses accounts and retains all their canonical tenant bindings.
Legacy ambiguous methods are copied unchanged. A successful backfill reports
projection agreement, not that every user now qualifies for protected actions.

### Concurrency and completeness boundaries

The write pass rereads each account under Postgres `FOR SHARE` row locks and
holds them through both DDB attempts. This prevents updates or deletion of those
selected identity rows during the pass. DDB conditional writes require the
observed user, tenant, method and `updated_at` to remain unchanged, including
attributes that were absent. A concurrent replacement cannot silently receive
the prior mapping's proof.

These are **not cross-store transactions**. They do not drain already pending
write-through retries, prevent changes after the pass, or lock an absent source
row. The script enumerates accounts from Postgres, so it also cannot discover
orphan DDB keys with no canonical identity at enumeration time. Its success
message is not a global drift or revocation audit. Before cutover, establish that
the deployed writers preserve binding/proof together, pending older writes are
settled, and the environment's approved reconciliation evidence covers orphan
and revoked projections. Unresolved writer/reader coordination blocks acceptance.

### Verify the backfill

Retain the exit status and per-table summary. Compare representative repaired
rows against the complete canonical tuple, including a proven sender, an
unproven sender and any multi-tenant source bindings. Confirm required missing
mappings were recreated by the canonical writer and then backfilled successfully.

A count of rows lacking `verification_method` is insufficient: an empty method,
an unproven method, or stale proof can all be present attributes. Whole-table
counts also conflate non-GitHub accounts and unrelated legacy index rows. Use the
required GitHub account inventory and exact binding checks above, together with
the approved orphan/revocation reconciliation evidence.

## Step 3 — Publish and verify enforcement

Only after step 2 and its coordination checks are complete, publish the reviewed
webhook Lambda through the approved deployment path.

Verify both directions in an authorized test repository: a known proven user
must dispatch with the expected platform identity, and a controlled unproven
identity must be refused. Exercise the environment's cross-tenant routing policy
where applicable; a same-tenant success alone does not validate that policy.

The denial metric is published with the **`TenantId` dimension**. Query each
relevant tenant (and `unknown` if events can lack a tenant), using the same AWS
account and region as the emitting Lambda. Supply UTC timestamps for the actual
verification window:

```bash
TENANT_ID='org-example'
START_TIME='2026-09-24T12:00:00Z'
END_TIME='2026-09-24T13:00:00Z'
aws cloudwatch get-metric-statistics \
  --namespace ADP/AgentAuthority --metric-name UnprovenIdentityAuthority \
  --dimensions "Name=TenantId,Value=$TENANT_ID" \
  --start-time "$START_TIME" --end-time "$END_TIME" \
  --period 300 --statistics Sum \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --profile <profile> --region <region>
```

CloudWatch does not aggregate custom metric dimensions implicitly. A query with
no dimensions cannot measure the tenant series. This emitter publishes on
refusal only: **`[]` means no datapoints, not an observed zero**. It can mean no
refusals, wrong dimensions/region/window, delayed delivery, or a metrics failure.
Confirm the controlled denial appears in its tenant's series and correlate the
window with successful dispatch and refusal logs; metric absence alone cannot
establish either availability or enforcement.

Nonzero values count refusals, not necessarily backfill defects. Investigate the
identity and reason: missing/stale projection of a proven canonical link needs
reconciliation; a legacy or unproven link needs a legitimate proof-establishing
flow. Rerunning the script cannot create that proof. Do not weaken the gate to
make the metric quiet.

Also inspect withheld-provenance logs, which can indicate a canonical lookup
disagreed with the DDB row:

```bash
aws logs tail /aws/lambda/adp-<env>-github-webhook --since 30m \
  --profile <profile> --region <region> | rg -i 'withholding provenance'
```

## Rollback

There is no flag to permit unproven links. If a deployment rollback is approved,
use the normal release rollback procedure and record that prior Lambda code may
restore the vulnerable authority behavior. Prefer repairing the demonstrated
projection or writer defect. Retaining the projection attribute is compatible
with older readers, but a later cutover requires fresh verification; intervening
identity changes or delayed writes can invalidate an earlier backfill result.

## Policy consistency

The gateway and Lambda proven-method vocabularies are checked by
`modules/gateway/tests/internal/test_provenance_policy_lockstep.py`. That check
prevents policy drift; it does not verify live projection data, enrollment proof,
write-through ordering or completed rollout.

## Supported proof and account-link recovery

Self-service account claims remain unverified. The internal linking flow posts a
confirmation in a shared conversation and records `shared_channel` delivery with
no target user; consuming that link supplies no new ownership proof. An existing
proven mapping held by the same user retains its method and verification time.
There is no private DM delivery adapter in this change. Consumer tests that seed `provider_dm` or
`provider_asserted` user-bound nonces verify the consumer contract only; they are
not evidence that a provider delivered a private message.

Existing provider-confirmed onboarding and the authenticated platform administrator
identity flow remain the supported ways to establish a proven mapping. The admin
identity routes require platform admin authority; workspace administration alone
does not grant access. Platform administrators must verify the external account
and intended tenant/user before assigning it. If an
unproven claim occupies the tenant's unique provider-account key, inspect the claim
and proof, remove the incorrect claim through the admin identity flow, then create
the verified mapping for the rightful user. This is an operator reconciliation,
not automatic self-service recovery; a proven mapping must not be reassigned merely
because another user requests it. Validate canonical and projected bindings after
reconciliation, following the ordered checks above. No live reconciliation is
performed or authorized by these source changes.

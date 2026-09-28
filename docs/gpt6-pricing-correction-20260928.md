# GPT-6 fallback pricing correction

Sol and Luna were missing from the pricing manifest. Unknown-model fallback
charged cached input at $3/million instead of the published cache-read rate.
AWS US Geo CRIS Sol short-context pricing is $2.20 input, $0.22 cache read,
$2.75 cache write, and $11 output per million tokens. Long-context prices apply
to the full request above 272,000 total input tokens, including cached input.

Sources reviewed on September 28, 2026:

- https://developers.openai.com/api/docs/pricing
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-sol.md
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.md
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-astra.md

An audited call with 2 uncached input, 201,153 cache-read, 480 cache-write and
1,169 output tokens was recorded as $0.622440; the published price is $0.058437.
This establishes an ADP accounting overstatement, not an AWS invoice error.
The audited developer invocation had 207 calls over 22.4 minutes: $78.375687
recorded versus $7.133013 at the published rates ($3.50/min versus $0.32/min).
The current-day sample of 188 service requests reconciled exactly with Cloud
Agents ($85.838292). The page now polls the spend and budget endpoints every
30 seconds so active spend becomes visible while the page remains open.

## Historical credits

Deploy the pricing release and migration 079 before running the repair module
in a maintenance Job using the pinned gateway image, service account, and
database configuration. Follow the existing deployment guide for maintenance.

```sh
python -m src.budget.reconcile_gpt6 \
  --account-id ACCOUNT --org-id ORGANIZATION --bucket CHAT_LOG_BUCKET \
  --actor INCIDENT_OPERATOR --start START_WITH_TIMEZONE --end END_WITH_TIMEZONE
```

The interval is inclusive at the start and exclusive at the end. Use a fixed
end time after the corrected pricing release is serving. Review the JSON plan,
including skipped records and aggregate old/new totals. Apply the exact plan
with the same arguments plus `--apply --plan-sha256 HASH_FROM_DRY_RUN`.
A changed plan rolls back the entire batch. Do not substitute a fresh hash
without reviewing the new plan. Already-corrected requests are skipped.

The repair only credits verified, positive, overcharged Sol/Luna fallback
requests. It verifies the original SQL/S3 decision, owner, token total and
settlement allocation hash. Missing receipts, ambiguous routing, zero-cost
rounding and possible undercharges are reported for separate review. It never
guesses historical ownership or adjusts unrelated models.

Each credit updates the usage-log cost and pricing provenance, plus all original
daily/weekly/monthly budget entries in the same transaction. Token and request
counts stay unchanged. `budget_pricing_corrections` retains both decisions and
the operator/source reference; original settlement receipts and S3 objects stay
immutable, so their retries cannot debit again. Application rollback must retain
the correction table and its audit records.

Verify the report's credits against before/after Cloud Agents and invocation
totals. Requests without durable evidence remain explicitly unresolved.

### Operators with separate S3 read access

The serving gateway may intentionally have write-only chat-log access. Do not
add read permissions to that role for this repair. If the initial dry-run
reports S3 `ClientError`, an operator with existing read access can export only
pricing and attribution evidence:

```sh
python modules/gateway/scripts/gpt6-receipt-export.py export \
  preview.json arguments.json receipts.json.gz
```

`arguments.json` is the same CLI argument array used for the dry-run. The
operator must have the active ADP CLI session for the target organization and
AWS credentials for the target account. The export checks both identities,
reads only inaccessible request IDs, removes conversation contents, and records
source ETags/version IDs. Review its failure count and save the printed SHA-256.

Mount the gzip and helper in a private, immutable maintenance ConfigMap. Run
with the deployed correction image and existing gateway database identity:

```sh
python /repair/gpt6-receipt-export.py run \
  /receipts/receipts.json.gz EXPECTED_SHA256 /repair/arguments.json
```

Use server-side ConfigMap creation/application to avoid duplicating the export
in a last-applied annotation. No operator credentials or presigned URLs belong
in the Job. The adapter changes only the repair's receipt reader; account,
SQL, pricing-decision and allocation checks still run. Review the resulting
plan before creating a separate apply Job with its exact hash. Retain reports
and remove temporary Jobs/ConfigMaps after verification. Missing objects remain
skipped and must never be replaced with inferred evidence.

### Request lookup index

Migration 080 creates `ix_usage_org_request` concurrently, with bounded lock
and statement timeouts and retry handling for an invalid interrupted index.
Apply it before the historical repair: without it, each request lookup scans
the organization's usage rows. An operator may run the migration's `upgrade`
through Alembic Operations in a pinned maintenance Job before the regular
release; the regular migration recognizes the valid index. Never stamp the
revision manually. Protect temporary repair Jobs against voluntary node
consolidation with `karpenter.sh/do-not-disrupt: "true"` and remove them after
verification.

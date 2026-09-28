# Recover missing direct Codex spend

The Responses proxy wrote `usage_logs` but did not emit the S3 event consumed by
`budget-usage-tracker`. Consequently requests had costs in the usage API while
`GET /me/budget` and budget enforcement read a settled ledger missing those costs.

The fix emits one usage-only chat-log event on completion, including the authenticated
caller and server-resolved tenant/root-person attribution. It covers streaming and
non-streaming calls and uses the existing logging configuration and exclusions.
These records contain token counts and attribution, without conversation content.
They remain asynchronous, so allow time for settlement after each response.

Gateway Deploy now packages and updates the provisioned budget tracker and pricing
refresh functions when their source changes, before deploying the backend. It also
does this on manual deploys; environments without those optional functions skip
their code updates (Terraform still owns provisioning).

Deploy the updated tracker pricing fallback and gateway before recovery. The Lambda
must have the same existing OpenAI model prices as the gateway; otherwise it can
replace a correctly priced usage row with its generic fallback. Models absent from
both price tables, including GPT-6 Astra at the time of this fix, still use the
existing default estimate. This change does not establish new provider prices.

## Historical direct use

`scripts/backfill_mantle_budget_usage.py` reads an explicit tenant/person/time window
from `usage_logs`. It excludes hosted runs, other providers, rows already linked to
S3, and requests with no measured tokens. It checks the original deterministic S3
key and creates only absent objects, with a conditional PUT to prevent overwrites
on retries. The existing tracker performs the ledger update.

Run in an environment configured with the gateway's database access and permission
to read the chat logs bucket. Applying also needs S3 `PutObject`. Set the exclusive
cutoff **before the fix was deployed**, so the script cannot race new live records.

```bash
python scripts/backfill_mantle_budget_usage.py \
  --org-id <tenant> --user-id <cognito-sub> \
  --since 2026-09-01T00:00:00Z --until <pre-deployment-UTC-cutoff> \
  --bucket <chat-logs-bucket>
```

The default is read-only. Review `missing`, `existing`, and `recorded_cost_usd`,
then rerun the same command with `--apply`. The amount is the usage API's recorded
cost; the tracker reprices with its current database/fallback table. Restoring
settled spend also restores budget enforcement, and can exhaust an existing cap.

Do not delete or overwrite existing objects to retry settlement: the existing
tracker increments on each delivered event and does not deduplicate S3 delivery.
An existing object without a linked usage row requires investigation of the tracker,
not automatic replay. Conditional creation prevents this script from emitting a
second event for the same key; it does not change S3's delivery guarantees.

After the tracker drains, verify the daily/weekly/monthly `user` rows and the org/team
rows increased by the recovered cost. Check `GET /me/budget` using the same person
and tenant, and confirm fresh Codex requests continue increasing the direct line.
Hosted historical usage needs verified run attribution and is excluded from this tool.

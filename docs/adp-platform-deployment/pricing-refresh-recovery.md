# Recover a partial pricing refresh

The daily Lambda may publish a complete generation containing both freshly
verified prices and retained last-known-good prices, then raise
`PartialRefreshError`. Retained prices keep their original verification times;
partial publication never means those prices were verified again.

On 24 September 2026, account 879318057152 had generation 148 active and its
06:00 UTC EventBridge schedule disabled. AWS's global Claude pricing widget no
longer included eight model/tier groups (264 endpoint variants). That prevented
strict rollout finalization from re-enabling scheduling even though the other
1,072 variants refreshed successfully. This is a source-coverage gap, not an
empty database or complete refresh outage.

## Recovery after a coordinated pricing release

Deploy the gateway, both budget Lambda archives and migration 070 together,
using the canonical deployment guide. Migration 070 adds 53 audited Opus 5.5
variants and preserves all existing prices and operator flags. The runtime
snapshot is 2026-09-24.1; earlier snapshots and migrations remain immutable.

Normal finalization still requires a full refresh. When a reviewed AWS source
gap prevents full verification, run the standalone **Gateway Pricing Finalize**
workflow against the deployed release SHA with `allow_partial_refresh=true`, or:

```bash
python3 modules/gateway/scripts/pricing-rollout.py finalize \
  --account-id 879318057152 --environment dev --region us-east-1 \
  --expected-image '<actual reviewed gateway release image>' \
  --allow-partial-refresh
```

The recovery option requires matching Lambda source, ready gateway replicas,
validated database coverage/content hash, a newly advanced generation/pointer,
working failure queues and alarm routes, nonzero fresh prices and no failed
source fetches. Paused refresh, missing schema, transport errors, zero fresh
prices and rejected generations still prevent scheduling.

The verifier requests a structured partial result with `report_partial=true`.
That flag changes reporting only: it cannot supply rates, alter coverage or
turn partial freshness metrics into success. Scheduled events continue raising
on partial results, preserving retries, failure destinations and alerts.
The completion message explicitly reports degraded operation. Re-enable daily
updates without pretending missing prices are fresh, then track source recovery.

## Remaining unpublished prices

AWS lists GPT-6 Sol/Luna as available models, but the model-card pricing URLs
returned page-not-found and the queried AmazonBedrock pricing API returned no
GPT-6 Sol product during this audit. Their pricing must come from a verified AWS
publication or account agreement. Do not reuse Astra or GPT-5.6 prices, fabricate
rates, or mark catalogue membership as billing readiness. Epic #5911 retains
this source dependency and full end-to-end acceptance.

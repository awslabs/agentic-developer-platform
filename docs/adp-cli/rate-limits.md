# Rate-limit CLI

`adp ratelimit me --json` reads the signed-in token's applicable identity, team,
department and organization rungs. It accepts no user ID. Each rung applies its
saved dimension or the account-type default; the strictest applicable rung wins.
This is configuration readback, not current bucket balance or proof of denial.

Administrators use:

```sh
adp admin ratelimit list --org ORG --page-size 20 --max-pages 1 --json
adp admin ratelimit show --org ORG --scope team --target TEAM --json
adp admin ratelimit status --org ORG --scope team --target TEAM --json
adp admin ratelimit set --org ORG --scope team --target TEAM --rpm 20 --dry-run --json
adp admin ratelimit set --org ORG --scope team --target TEAM --rpm 20 --expect-absent --yes --json
adp admin ratelimit set --org ORG --scope team --target TEAM --rpm unset --expected-revision REV --yes --json
adp admin ratelimit delete --org ORG --scope team --target TEAM --expected-revision REV --yes --json
```

Scopes are `user`, `team`, `department`, `org`. Organization target must equal
`--org`. User targets may name the SQL ID or login subject; readback returns the
Cognito subject actually used by the limiter. Users without a supported login
subject are refused rather than configuring an unused identity key. Department
administrators use exact target reads; unfiltered listing is refused.

`set` patches only named `--rpm`, `--tpm`, `--concurrent-requests` dimensions.
Integers are 1 through 2147483647. `unset` removes that dimension's override and
restores its account-type default at that rung; omission preserves it. Zero is
not accepted by new writes. Legacy saved zero remains readable and bypasses that
dimension at its rung; other rungs still apply. Deletion removes only this
configuration row. Neither mutation resets counters or usage.

Dry-run performs reads only. Existing rows require the reviewed timestamp;
creation requires `--expect-absent`. `--yes` supplies no missing permission or
revision. Unknown delivery returns `pending`, reads back the same target and never
automatically replays the write. Inspect before retrying. HTTP 403/404/409/422
retain normal permission, missing-target, conflict and validation errors. An old
server without the scoped adapter is unavailable; no legacy write fallback occurs.

Status separates saved configuration from runtime metadata. Forced database
refresh happens on each admission with a three-second deadline. Redis identifies
shared storage; process memory identifies a local quota. Neither configuration
nor a single responding process proves backend health or all-worker convergence.
RPM and concurrency remain unqualified until live calls prove their behavior.
Burst capacity is `max(1, int(rpm * burst_multiplier / 60 * refill_buffer_seconds))`;
denial at a fixed request ordinal must not be assumed.

TPM is explicitly `unavailable_actual_usage_not_reconciled`: middleware currently
consumes a token value of one per request without actual model-token reconciliation.
Saving TPM does not establish actual-token enforcement. Full #5627 acceptance
remains held for this backend dependency and real Claude/Codex denial, Retry-After,
overlap, multi-worker and cleanup evidence under the existing bounded inference
envelope. No paid requests or live fixture mutations were made by this change.

Existing nightly scenario E36 calls the served `adp ratelimit me` and verifies
source metadata and the named TPM gap. It adds no schedule and makes no enforcement
claim. The reference client remains disposable EC2; an EKS run is labelled separately.

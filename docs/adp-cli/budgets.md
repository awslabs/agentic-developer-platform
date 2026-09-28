# Budget CLI

`adp budget me --period daily|weekly|monthly --json` reads the existing own-budget
API. Direct and cloud-agent lines are separate ledgers. Combined informational
spend is not a shared cap. An absent cap is not zero headroom; unknown spend is
not evidence of free inference.

Administrators can list and inspect exact configured caps:

```bash
adp admin budget list --org ORG --page-size 20 --max-pages 2 --json
adp admin budget show --org ORG --team TEAM --period daily --json
adp admin budget status --org ORG --user USER --usage cloud-agents --period monthly --json
```

Choose exactly one `--user`, `--team`, `--department`, or `--scope org` target.
User targets additionally require `--usage personal` (Cognito-sub ledger) or
`--usage cloud-agents` (canonical users.id ledger). The server resolves aliases
within the selected tenant and enforces administrator and department scope.
List pagination is bounded and does not promise a consistent snapshot.

Changes require an explicit daily, weekly or monthly period, positive decimal
USD with at most two places, and hard or soft enforcement mode:

```bash
adp admin budget set --org ORG --team TEAM --period daily --amount-usd 5.00 --mode hard --dry-run --json
adp admin budget set --org ORG --team TEAM --period daily --amount-usd 5.00 --mode hard --expect-absent --yes --json
adp admin budget set --org ORG --team TEAM --period daily --amount-usd 6.00 --mode soft --expected-revision TIMESTAMP_FROM_SHOW --yes --json
adp admin budget delete --org ORG --team TEAM --period daily --expected-revision TIMESTAMP_FROM_SHOW --yes --json
```

Create requires `--expect-absent`; update and delete require the revision from
`show` or `--dry-run`. Conflicts require a fresh inspection. Each command changes
only that entity/ledger/period cap and preserves accumulated usage and other
periods. Deletion removes a cap; it does not refund usage or eliminate ancestor
limits. Soft limits do not deny inference. Ancestor caps and in-flight estimates
still affect admission and possible overshoot.

Writes use the human session and the server's `budget.managed.write` capability.
They perform one readback. A lost or malformed acknowledgement remains
`pending` with an unknown outcome even if desired state is observed: the CLI
never replays a write or claims the observed state proves who changed it.

E26 in the existing EC2 nightly regression calls the served `budget me` for all
three periods and checks uncapped semantics. It performs no mutation or paid
inference. This provides read regression coverage, not live hard/soft enforcement
acceptance. Real Claude/Codex spend-through, hosted identity attribution and
administrator lifecycle qualification still need explicitly owned live fixtures.

# Person-wide budgets

`adp budget me --period monthly --json` reads the canonical own-budget envelope,
including period bounds, direct/cloud lines, person totals and blockers when the
server can establish them. Unknown or estimated values are not settled spend.

`adp budget person-cap show --period monthly --json` reads the applicable limit
and its source: individual, team default, org default, or platform default.
The token determines the person. There is no self `--person` override.
Self `set` and `delete` return an actionable refusal without an HTTP write:
the September 7 operator ruling makes person limits admin-governed.

Platform admins manage the canonical person anchor, not a tenant `users.id`, a
`root_user` cloud ledger, or a tenant entity cap:

```sh
adp admin budget person-cap show --person github:12345 --period monthly --json
adp admin budget person-cap set --person github:12345 --period monthly --amount-usd 25 --dry-run --json
# After reviewing the preview, copy its exact expected_revision (or absent):
adp admin budget person-cap set --person github:12345 --period monthly --amount-usd 25 --expected-revision REV --yes --json
adp admin budget person-cap delete --person github:12345 --period monthly --expected-revision REV --yes --json
adp admin budget person-default show --scope team:ORG:TEAM --period monthly --json
adp admin budget person-default set --scope org:ORG --amount-usd 10 --dry-run --json
adp admin budget person-default delete --scope platform --expected-revision REV --yes --json
adp admin budget member-report --org ORG --period monthly --page-size 20 --max-pages 2 --json
```

All targeted person/default reads and writes, including member-report, retain
platform-admin authorization. An organization admin cannot govern a person's
spend in other tenants. The member report exposes only the selected organization's
settled spend; subtracting that amount from a cross-tenant limit would give false
headroom. The CLI labels global headroom unknown and never requests other tenants'
line items to fill that gap.

Defaults target `platform`, `org:ORG`, or `team:ORG:TEAM`. Individual rows override
defaults for their period. Deleting an individual row restores the applicable
default ladder; deleting a default restores broader rules for members without a
more specific rule. An absent authored row does not imply uncapped effective
spending. Preview explains these effects; exact post-change resolved limits need
person readback. Set always writes hard enforcement, accepts only positive finite
USD with at most two decimal places, and daily/weekly/monthly calendar periods.
Legacy soft caps remain visibly soft until explicitly rewritten.

`--dry-run` wins over `--yes`. Confirmed writes require the exact reviewed revision
or `absent`. The server checks existing revisions in the UPDATE/DELETE statement
and rejects concurrent creates rather than overwriting the winner. A network or
malformed-acknowledgement failure reports pending and does not resend a mutation.
Inspect current state and review again before a retry; readback alone cannot prove
which operation wrote a value. Budget usage is never reset. Other caps continue
to apply, and person-limit enforcement cache refresh can take 60 seconds.

E35 runs own reads and self-write refusal through the served CLI in the existing
nightly evaluation. Live admin explicit/default/reset, multi-tenant privacy,
org-admin refusal and bounded real-client spend-through/restoration remain
acceptance holds until isolated person/default fixtures are provisioned. Readback
and passing unit tests do not establish those live behaviors.

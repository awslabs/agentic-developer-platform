# Recorded node cost estimates

`GET /workspaces/{workspace_id}/cost`, `GET /orgs/cost` and the workspace
`/budget` response describe estimates from recorded node rates and intervals.
They do not report provider bills, complete provider inventory, storage/network
charges, discounts or historical rate changes.

The existing `total_cost_usd` and `current_daily_cost_usd` fields are nullable.
Missing node records, missing/invalid rates or invalid intervals cannot establish
a zero total. `estimate_status` is `available` when every returned node has an
estimate, `partial` when only a known subset is estimated, or `unavailable`.
`known_subtotal_usd` remains separate from the complete total. A recorded rate of
zero is distinct from a missing rate; even that zero is an estimate, not a bill.
Clients must handle null totals and breakdown values without replacing them with
zero. `observed_cost_usd` remains null and `cost_reconciliation` is `unavailable`.

The source records identify clusters, not workload allocations. Workspace rows
therefore declare `cost_scope=workspace_cluster`. Several workspaces may share
that cluster estimate; organization totals count each recorded node only once.
These figures must not be presented as an individual job's charge. All node reads
also require the workspace's organization identity.

Date windows include nodes that began before the window and continued running
within it. Intervals are clipped to the requested window and the query time;
naive timestamps mean UTC. Reversed/empty windows, including future-only windows after clipping, return 422. Missing
timestamps remain unestimated. `checked_at` is the API query time, not evidence
that the provider has supplied fresh billing data. Zero configured budget caps
remain zero in responses. Budget percentages use the unrounded Decimal estimate;
monetary display values round to cents.

The existing background budget detector uses the known recorded-rate subtotal
to detect threshold breaches. Its subtotal cannot certify unused budget. Paid
operation admission still reserves the separately approved finite budget through
the operation ledger. This change does not settle reservations, mutate provider
resources or claim provider billing reconciliation.

The Domain CI suite exercises real SQL filtering, time-window overlap, incomplete
and zero-rate estimates, cross-organization exclusion, shared-cluster deduplication
and response serialization. These are offline database checks, not live billing
acceptance.

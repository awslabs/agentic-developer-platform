# Workload accounting reads

`GET /workspaces/{workspace_id}/batch-jobs/{job_id}/accounting` and the serving
`/deployments/{deployment_id}/accounting` route require a current workspace READ
grant. The workload screen advertises the budget button when the current API has
an operation store and the caller has READ permission. Reads need no provider
credential or manager connection and continue after a profile is removed.

The server validates each operation against the immutable workload registration,
original request, approved envelope and both admission/owning-domain ledgers. The
original provision reservation and separately approved stop reservation remain
separate. One read-only repeatable-read transaction captures those records and
workspace committed budget. Permissions are checked again before returning data.
A missing or mismatched record is unavailable, never a zero-cost result.

Amounts are decimal strings of USD millionths. The UI formats them without
floating-point conversion. The API reports approved ceilings, reservation states,
conservatively held budget and record timestamps. Different ledger states are
shown as incomplete accounting acknowledgement; no read attempts settlement.
The configured reservation cap is the same conservative minimum of hourly/daily
caps used by admission. A cap of zero means exhausted, not unconfigured.
Workspace commitments include all currently reserved, confirmed and retained
attempts. They are neither today's actual spend nor a forecast of future charges.

Provider-billed cost and estimated cost remain explicitly unavailable. Existing
node-rate aggregation does not supply per-allocation provider billing evidence
and must not be relabeled as reconciled workload cost. A released reservation
cannot prove a zero provider bill. Allocation membership counts include historical
resources and do not prove current presence or complete absence. Cleanup status
continues to come only from the maintained workload finalizer/cancellation state.

Remote tests use real PostgreSQL admission, ledgers and worker/finalizer flows;
only external transports are simulated. Browser scenarios are isolated fixture
evidence. No billing reconciliation, live workload or infrastructure mutation is
performed by these routes or their UI controls.

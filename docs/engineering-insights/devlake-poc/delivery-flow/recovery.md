# POC code-gate and accounting recovery

Implementation merge does not close this POC. Preserve the accepted graph,
worker history, spending and live acceptance while repairing admission.

## Confirmed code-gate defects

S12 PR [#9180](https://github.com/aws-e/adp/pull/9180) predates
`.github/workflows/insights-ci.yml`. Its applicable `DevLake POC / verify`
check passed on the exact merged PR head in
[run 40684995468](https://github.com/aws-e/adp/actions/runs/40684995468).
The authored contract now selects that check from workflow ID `399225481`,
`.github/workflows/devlake-poc.yml`, alongside Gateway `Lint` and `Test`.
All twelve reviewed predecessors remain required.

Apply the same bounded correction with the native evaluation preview/accept
API against the current plan version, hash and preview snapshot. The evaluation
policy projection must remove repair-only settings when it removes the worker's
repair actions. The original worker policy and its limits remain unchanged.

## Expired model accounting

The original meter retained charges for two days although accepted flows can
run longer. New meter retention covers the maximum supported shared wall-clock
window and accepted expiry, with a day of margin. This prevents early charges
from disappearing during a permitted window extension. It does not repair an
already expired meter or relax refusal when accounting is unavailable.

A missing hash cannot be recreated from successful usage totals alone: those
totals do not prove that all reserved or unreported requests settled. Preserve a
pre-expiry backup if available. An operator must verify its exact flow key,
initialization marker, complete request inventory and observation time, compare
later durable receipts, and fence workers before applying a reviewed recovery.
Preserve unresolved exposure conservatively and keep an attributed before/after
record. Do not restore an entire shared cache over active flows, zero the meter,
disable enforcement, or mark the code gate passed manually.

Existing meters also need their old per-request deadlines extended. Merely
changing the TTL used for new reservations leaves an idle or paused flow's
earlier charges exposed to expiration. The model spending boundary and flow
admission now extend the retained inventory atomically before using it. The
extension preserves exact amounts, pending receipt deadlines, unknown markers
and longer existing retention. A late finalizer cannot delete an expired charge
from an initialized meter.

For idle or paused flows, run the privileged
`modules/gateway/scripts/extend-flow-meter-retention.py` with the selected AWS
account, organization, flow and reason. Its default preview makes no changes.
Apply with the preview's `--expected-digest` and `--apply`. An append-only
decision captures the original inventory before the compare-and-swap update;
another records completion. Concurrent accounting changes, a missing meter or
malformed entries refuse the operation. The command changes no spending limits,
work receipts, execution state or pause setting. Retention maintenance does not
establish that an already partially lost inventory is complete.

If a complete inventory cannot be recovered, the privileged
`restore_exhausted_flow_meter` primitive can retain the entire enforced
historical ceiling. Before using it, an operator must fence old workers, verify
that ceiling over the historical execution window and record immutable recovery
evidence. It preserves attributed receipts as actual spend and records the
remaining amount as a separate unresolved exposure hold. The atomic write only
accepts a missing meter; it neither overwrites surviving state nor changes the
usage ledger. Its result has **zero available headroom**. Continuing then needs
an explicit human-approved budget increase through the existing budget API;
restoration itself grants no new allowance. This is a conservative bound, not a
claim that the lost request inventory has been reconstructed exactly.

### Explain and reconcile retained amounts

The budget view separates unsettled model requests from historical recovery
holds. Both remain included in `reserved_usd` and excluded from available
headroom. A recovery hold is identified by the flow's restoration decision,
not merely by a request name. This display change does not relax admission.

For an interrupted text request, a gateway error or zero-valued diagnostic row
does not establish zero provider usage. The privileged
`modules/gateway/scripts/reconcile-flow-zero-usage.py` reads the configured AWS
provider invocation log and requires the exact provider request, account, region,
model and complete zero token counters. Preview with the selected `--account`,
`--region`, `--org`, `--flow`, `--reason` and repeated `--request` IDs. Apply with
`--apply --expected-snapshot` using the returned snapshot. It atomically records
the provider evidence and a zero settlement receipt; ordinary scheduler
reconciliation then releases the request. Missing, ambiguous or nonzero evidence
leaves the reservation intact. No prompts or generated content enter the report.

A synthetic recovery hold has no individual provider request to settle. When a
complete historical inventory remains unavailable, an owner can explicitly
accept that uncertainty through the privileged
`modules/gateway/scripts/resolve-flow-recovery-hold.py`. Preview with `--account`,
`--org`, `--flow`, `--recovery` and `--reason`. The preview states the exact release,
current limits and that historical usage has **not** been reconstructed. Applying
requires the current `--expected-snapshot`, `--acknowledge-unverified-history`
and an `--approval-reference` identifying the owner's actual decision. A generic
restart request or an agent-created acknowledgement is not that decision.

This administrative adjustment preserves original usage, recovery evidence and
configured limits. An append-only intent precedes an atomic change to only the
named recovery hold; the operation retains a replay proof and a completion
record. Ordinary model requests and other flows cannot be released through this
path. Lost history is never relabelled as verified zero cost. These commands are
operator maintenance, not new automatic story gates or task-board requirements.

## Remaining live handoff

The original launch contract deliberately contains unresolved private bindings.
Resolve source consent and scopes, the registered target, IAM database and
cleanup authority before the operations stage can run. Publish an accepted
native amendment preserving completed nodes.

Reconcile the operations/evaluation contract with the implemented producer.
The current `devlake-poc-qualify.yml` dispatch accepts `campaign_id` and runs
twelve four-hour observation windows followed by `read-back`; the authored
operation still names `phase` and `launch_manifest`, and its evaluator expects
`qualify` plus an uploaded `summary.json`. The current workflow only reads
private observation files and does not supply that artifact or a live collection
producer. Correct and test this producer/consumer integration before dispatch;
renaming inputs alone cannot satisfy the 41 pilot criteria.
[Defect #9425](https://github.com/aws-e/adp/issues/9425) records the reproduced
CLI failure and owns this integration repair.

Closure still requires the original 48-hour measurements, independent live
receipts, S08 recommendation and resource cleanup. A merged recovery PR or a
green offline qualification workflow cannot substitute for those results.

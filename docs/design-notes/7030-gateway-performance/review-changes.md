# Review finding-to-change mapping

**Final design review record; exact approval is recorded in PR #7039.** Revises the proposal reviewed at
`f51fb801f6f113e1f4b52874244c638b5fb112ef` in
[review 5422134420](https://github.com/aws-e/adp/pull/7039#pullrequestreview-5422134420),
as requested by the [epic amendment](https://github.com/aws-e/adp/issues/7030#issuecomment-6005911251).
The exact new design commit is the commit containing this record, published with
immutable document links and the finding mapping in [PR #7039](https://github.com/aws-e/adp/pull/7039).
This design commit changes documentation only. Approved issue-body handoff is
a separate recorded action; no runtime or environment mutation occurs here.

| Finding | Change and implementation ownership | Re-review status |
|---|---|---|
| R1: S3 intent does not authoritatively retain spend across Redis loss | [SQL authority](accounting.md#r1-durable-scoped-admission-authority) adds five explicit tables; every request locks shared hierarchy/person/policy scopes, commits a hold, arms dispatch and atomically exchanges holds for settled charges. Period-independent barriers, alias fencing, cache rebuild proof and bounded failure behavior replace the no-schema-change assumption. #7032 owns state/schema, #7031 transaction/connection cost, #7035 signals; F01–F04/F12–F14 cover integration. | Revised design supplied; runtime invariants not implemented/tested. |
| R2: exact pre-receipt recovery remains impossible to promise | [Conservative reconciliation contract](accounting.md#r2-conservative-recovery-boundary) limits disposition to trusted receipts or proven-unsubmitted evidence. Unknown usage retains its barrier; financial exceptions are excluded. F05–F08 distinguish containment from exact recovery. | **Resolved conservatively in final review.** AC-02 correction quoted below; no manual release or requester-approved risk exception is claimed. |
| R3: journal waits can invalidate a quote before provider dispatch | [Dispatch contract](accounting.md#r3-final-dispatch-and-quote-validity) rechecks policy at SQL arming, then checks quote generation/expiry and a one-use permit after transport preparation at submission. No awaited journal/queue work after the final guard; ambiguous sends retain exposure without replay. #7032 owns contract, #7033 transport integration; F09–F11 test the new boundary. | Revised design supplied; actual transport guard is an implementation deliverable, not an existing hook assumed safe. |

The [epic](README.md), all six [draft story designs](stories.md),
[campaign/fault matrix](campaigns.md#accounting-and-dispatch-fault-matrix),
[source audit](audit.md) and [validation record](validation.md) are updated together.
The exhaustive baseline inventory remains pinned; a checked supplemental ledger
maps all design additions rather than hiding them outside repository coverage.

## Final review resolution — 2026-10-06

R1 and R3 are accepted as design contracts after source review; their runtime
claims remain acceptance tests, not completed implementation. R2 is resolved
without a financial-risk exception. `close_unknown_by_exception` and its
enablement flag are removed from scope; human authority does not substitute for
a trusted receipt or proven-unsubmitted evidence. No requester approval of a
write-off is claimed.

The original #7032 AC-02 was drafted by the reviewing assistant, not supplied as
an exact-token-recovery requirement by the requester. Its impossible blanket
promise is corrected explicitly as part of the authorized design review.

**Original AC-02:** crash/restart after model completion but before persistence/
acknowledgement recovers usage and settlement exactly once at the business-record
level, without silent loss, double charge or cross-tenant association.

**Corrected AC-02:** After a crash, replay any durable trusted usage receipt and settle exactly once at the business-record level, without duplicate or cross-tenant charges. If provider usage was lost before durable receipt storage, retain the durable attempt as unknown and block affected spend until trusted evidence resolves it; do not invent usage, treat it as zero, release it by administrative exception, or replay inference.

This keeps the stronger availability restriction instead of approving a release
of uncertain exposure. It does not change measured usage into an estimate or
assert that data lost before persistence can be reconstructed. F05 proves unknown
containment; F06 proves exactly-once durable-receipt recovery. A qualifying live
campaign cannot pass with unresolved accounting. The issue update must include
this correction and the exact design approval permalink.

The source diagnosis of blocking SQL and Opus failures, assignment of actual
executors/operators and predeclared qualification SLO remain prerequisites to
the corresponding implementation/live stages. Design review approval is not
capacity certification, deployment permission or automatic developer dispatch.

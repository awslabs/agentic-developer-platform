# Bandit source review — issue #6108

The original scope is **1,470 LOW/MEDIUM observations** from frozen source
`b1d0894c17c686f27c2747057dead0b5a0e6b17e`. It remains **open**.

The first review scanned different directories and produced 963 observations.
That scan is retained as supplemental evidence; its blanket category dispositions
and completion claim have been withdrawn. In particular, excluding every file
named `scanner.py` was broader than XML owner #6116's scope. A file being an
operator tool or running in a container does not establish that its findings are
non-applicable. Runtime and operator assertions still require review.

| Original selector disposition | Count |
| --- | ---: |
| Fixed fail-soft logging gaps | 4 |
| Verified test assertions | 187 |
| Reviewed parameterized SQL boundaries | 2 |
| Fixed source boundary, runtime acceptance open | 2 |
| Pending source review, owned by #6108 | 1275 |
| Total original selectors | 1470 |

Every original selector is retained exactly once in
[the inventory](bandit-source-review-selectors-inventory.json), with its original
file, rule and source line. Test dispositions require both a test source path and
an AST assert at the exact original location; no production or operator assert
is covered by that classification. The two separate HIGH B324 Git-object-ID
records remain in the master scan and retain their prior scoped review.

Four handlers now report static warnings while preserving fail-soft behavior.
Supervisor review removed raw exception logging, because database/HTTP exception
messages can include credentials or request content. The focused privacy test
executes all four production handler bodies with a private exception payload and
checks that only the fixed event text is logged:

`python3 modules/gateway/tests/unit/security/test_failure_log_privacy.py`

This PR can deliver those bounded fixes and the exact reconciliation. It does
not close #6108 or transfer unfinished runtime findings to another owner.

The gateway list/count query selectors `ri=1118` and `ri=1119` now have a
[source-specific SQL review](bandit-gateway-asset-list-review.md). Their MEDIUM
B608 scanner severity is retained: the interpolated text contains fixed SQL
fragments, while caller values are bound parameters. The review also found and
fixed a separate production authorization defect: default list/count results
included other users' personal assets. This is a source fix only; gateway rollout
and acceptance remain held, and the remaining 1,275 pending selectors retain #6108.

Original MEDIUM B310 selector `ri=652` has a bounded
[ingest resolver transport fix](bandit-ingest-resolver-review.md). Synthetic
loopback tests reproduce internal-key forwarding on the original 301/302/303
redirect paths and prove it is refused after the change. Scanner severity and
original identity are retained. Source verification does not establish an ingest
Lambda rollout or live acceptance; that remains open under #6108.

Original MEDIUM B310 selector `ri=653` has a bounded
[Slack response-router transport fix](bandit-slack-router-review.md). Actual
loopback tests reproduce default redirect forwarding of a synthetic Bearer key;
the fix refuses redirects, bounds request duration and redacts failure details.
No real Slack message or response Lambda deployment was performed. Original
identity/severity and #6108 ownership remain, with runtime acceptance open.

## Optimization-safe validation follow-up

203 retained B101 selectors now use explicit conditional failures that survive `python -O` and `python -OO`. These cover deployment target selection, archive identity, replay/journal state, workload inventory, recovery validation, pricing rollout and identity-policy invariants. The ten files retain the same guard expressions and AssertionError messages, verified by AST normalization. Six refusal regressions fail on baseline and pass on candidate; 243 component tests pass. No AWS deployment was performed. The eight gateway identity-module selectors retain runtime-open status pending the owning rollout. Evidence: `evidence/bandit-optimization-guards.json`. The full 1,470-selector inventory is preserved; 1,072 remain pending source review.

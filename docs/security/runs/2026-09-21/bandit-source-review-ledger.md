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
| Pending source review, owned by #6108 | 1279 |
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

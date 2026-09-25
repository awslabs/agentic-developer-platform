# Semgrep source review — issue #6119

**The original 13,139-observation scope remains open.** The initial agent's fresh
`auto` scan produced 1,035 observations, not the original scanner population.
Supervisor reconciliation now retains every original selector exactly once and
checks its result index and rule against the frozen SARIF artifact.

| Original disposition | Count |
| --- | ---: |
| Informational AI technology detection | 7728 |
| Non-security localization rules | 1286 |
| Pending source review, retained by #6119 | 4050 |
| Existing suppressions needing revalidation | 75 |
| Total | 13139 |

The reviewed categories use the exact rule definitions in the frozen SARIF:
"Possibly found usage of AI" rules identify technology use in an AI platform;
i18next portability rules identify missing translation/translation-key formatting.
Each original record binds to its specific rule rationale in the ledger. These
are semantic classifications, not scanner disables or renewed suppressions. They
do not validate any separate credential, execution or data-flow boundary.

All other original observations remain owned by #6119. The earlier transfer of
520 fresh-scan records to sibling/generic teams is not acceptance or closure.
The supplemental 1,035-record scan remains available as a review proposal only.

The source changes in this PR replace constant SAVEPOINT interpolation with
constant SQL and bind numeric timeout parameters through psycopg2. The prior
expressions already used module constants; eliminating a scanner pattern is not
proof that 27 exploitable SQL injection findings were fixed. No original record
is marked fixed solely from that fresh-scan count. Existing CI passed for the
source revision `e6e74bf2ee5a5eb4f2ffbfef8f5aab0c9ca4a71b`.

Frozen source: `b1d0894c17c686f27c2747057dead0b5a0e6b17e`. Exact original selectors are
anchored by commit `74c48e78647afbe8c4eaf83ce3b01499e5cc61fe` at
`docs/security/runs/2026-09-25/followon-inputs/source-semgrep-review-selectors.json`.
The original 75 suppressed records remain visible and unapproved by this review.

Merging this reconciliation and bounded SQL cleanup does **not** close #6119.

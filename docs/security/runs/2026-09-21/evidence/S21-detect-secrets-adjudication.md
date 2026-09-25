# Detect-secrets reconciliation — issue #6110

**The original review remains open.** The frozen scope is 1,859 scan records
and 1,849 overlapping audit groups; these populations must not be added together
as distinct leaks.

Supervisor reconciliation loaded the original frozen scan and audit artifacts.
All 1,859 original scan records now join the supplemental 2,431-record scan by
**file, line, detector and candidate hash**, not merely shared paths/types. Every
original audit group also joins its original scan records by full candidate hash,
file, line and detector. The ledger preserves all 3,708 original selector IDs,
with no raw candidate values or source-line content published.

| Original scan record disposition | Count |
| --- | ---: |
| Existing Git object identifiers, independently resolved | 209 |
| Exact AWS published example identifier | 41 |
| Pending context review, retained by #6110 | 1609 |
| Total original scan records | 1859 |

The initial agent's supplemental classifications remain available as review
proposals. Their path/category heuristics do not prove that every candidate is a
fixture. The earlier zero-secrets and no-rotation-needed conclusions are withdrawn.
No credential was exercised. Suspected live credentials require private handling
and coordination with existing rotation owner #4726, not publication in this ledger.

Source: `b1d0894c17c686f27c2747057dead0b5a0e6b17e`. Original manifests are indexed at
`74c48e78647afbe8c4eaf83ce3b01499e5cc61fe`, under
`docs/security/runs/2026-09-25/followon-inputs/`.

The exact joins remove the original evidence-access blocker. Completion still
requires context-based dispositions for the remaining candidates; this PR does
not close #6110 or transfer those candidates to the epic owner.

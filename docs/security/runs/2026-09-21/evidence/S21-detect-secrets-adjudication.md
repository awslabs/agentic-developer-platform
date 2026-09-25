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
| Exact AWS published example identifier or secret-access-key example | 72 |
| Complete public PEM delimiter literal, without key payload | 6 |
| Artifact SHA256 with immutable bytes and verified checksum context | 496 |
| Pending context review, retained by #6110 | 1076 |
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

An additional **405 original scan records** are verified artifact SHA256 values,
bringing that batch to **655/1859** verified nonsecret dispositions. Each candidate was
joined using the private full original hash, checked at the exact frozen source
line, compared to SHA256 computed from tracked artifact bytes, and checked in an
explicit JSON checksum field or matching artifact-filename key. Merely residing
in a fixture or manifest was not sufficient. That first batch held back 38 artifact hash matches without its narrow JSON
context proof; the second batch below supplies their missing context evidence. The per-selector receipt
is `S21-detect-secrets-artifact-digest-review.json`; the repeatable verifier is
`scripts/security/s21/verify_nonsecret_artifact_digests.py`. It requires the
private original scan/audit files and emits counts only. No raw candidate value
is published. That batch left 1,204 original scan records pending.

The second batch verifies those **38** held-back candidates: 20 installed Python
module digest mappings, three explicit dependency-lock YAML checksums, and 15
Python literals with AST-confirmed checksum use. The Python checks include
checksum comparison/metadata consumers, filename-keyed integrity dictionaries,
a `sha256` constructor argument, and the pricing seed tuple's exact mapping to
`source_content_sha256`. Every candidate also independently matches artifact
bytes read from the frozen Git revision; no file was executed to adjudicate it.
The second receipt is `S21-detect-secrets-artifact-digest-review-2.json`.

That second batch reached **693/1859** verified nonsecret dispositions and left
**1,166** original scan records pending. The overlapping audit population
is reconciled separately; fixtures are not accepted merely by pathname.

A third review verifies **31** exact AWS-published secret-access-key examples
and **six** whole public PEM delimiter literals with no key payload. The AWS
values match complete HTML code elements in the official IAM access-key guide;
its URL, retrieval time and document SHA256 are recorded in
`S21-detect-secrets-public-example-review.json`. The six delimiter findings are
parsed Python string literals containing only the public delimiter and optional
hyphens/whitespace. Adjacent literals are parsed together, preventing a header
from being accepted when key material is appended. The other 27 marker-associated
records remain pending; no fixture is accepted merely by path or test naming.

The verifier `scripts/security/s21/verify_public_secret_examples.py` checks
private full original candidate joins, exact immutable Git source lines, and the
public evidence without printing values. The document snapshot is retained at
`/tmp/security6110-aws-public-example.html` for independent local verification;
its contents and candidate values are not published in this receipt. Nine
synthetic regression cases cover public-example substring mismatches, document
tampering, dirty checkouts, delimiter payloads/adjacent strings and duplicates.

The public-example review reached **730/1859** verified nonsecret dispositions
and left **1,129** original scan records pending.

A fourth batch verifies **53** historical artifact digests. These do not match
the current frozen file contents because they identify earlier installed/source
versions. Each receipt records an immutable Git blob OID and historical
commit:path; the verifier proves that resolution, verifies the commit is an
ancestor of the original frozen revision, and computes SHA256 of those exact
blob bytes. The original candidate still requires its exact full original join,
frozen source line and decisive checksum context. Nine additional byte-matched
candidates without complete path/ancestry proof remain pending. See
`S21-detect-secrets-historical-digest-review.json`.

Current verified nonsecret dispositions: **783/1859**. **1,076** original scan
records remain pending. #6110 remains open; no credential was exercised and no
rotation or zero-secrets conclusion is claimed.

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
| Existing Git object identifiers, independently resolved | 217 |
| Exact AWS published example identifier or secret-access-key example | 72 |
| Complete public PEM delimiter literal, without key payload | 6 |
| Artifact SHA256 with immutable bytes and verified checksum context | 512 |
| Derived checksums recomputed from immutable source inputs | 36 |
| Resource references with explicit field/consumer binding | 60 |
| Pending context review, retained by #6110 | 956 |
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

The historical-artifact review reached **783/1859** verified nonsecret
dispositions and left **1,076** original scan records pending.

A fifth batch verifies **22** derived digests: 16 security ownership record-key
projections, four canonical pricing-source manifest digests, and two embedded
contract-artifact text digests. Each is recomputed from exact immutable Git
inputs and requires the full original candidate join, frozen source line and
checksum-role context. Projection selection/count/unique keys, both pricing
source hashes, and embedded artifact path-to-text declarations are checked. The
receipt `S21-detect-secrets-derived-digest-review.json` links source algorithms
and per-record input paths. No fixture is accepted merely because of its path.
Seven focused tests reject incorrect projection populations and changed inputs.

The derived-digest review reached **805/1859** verified nonsecret dispositions
and left **1,054** original scan records pending.

A sixth batch verifies **33** Secrets Manager resource references. Each has a
complete reference shape and an explicit ARN field or API `SecretId` argument;
Python AST binding checks reject unconsumed names, unrelated shadowed variables,
and concatenated prefixes. Frozen source consumers separately pass `SecretId`
to `get_secret_value`, whose returned secret value is distinct from the
identifier. Official API documentation provenance is recorded in
`S21-detect-secrets-resource-reference-review.json`. No resource was queried.
Short numeric account placeholders retain reference semantics but are not
asserted to be valid live ARNs. Resource-identifier privacy policies remain
separate; this receipt publishes no ARN, account or session value.

One KMS reference, two candidates without sufficient binding proof, three
templates and 24 abbreviated stubs remain pending. Ten focused tests cover
incorrect payload fields, missing bindings, shadowing, concatenation and
unsupported/incomplete identifier shapes.

Current verified nonsecret dispositions: **838/1859**. **1,021** original scan
records remain pending. #6110 remains open; no credential was exercised and no
rotation or zero-secrets conclusion is claimed.

A further **four** original candidates match independently recomputed canonical
JSON checksums: one evaluation specification, two pricing decisions with the
checksum field omitted, and one frozen pricing-rate population. Specific output
fields and exact source/input paths are enforced; a checksum-looking value or
fixture pathname alone is insufficient. The receipt is
`S21-detect-secrets-canonical-json-review.json`. The existing derived-digest
verifier checks the complete original scan/audit joins and immutable Git blobs;
22 synthetic tests cover valid recipes and altered inputs, outputs, paths and
context. This reaches **842 / 1,859** verified nonsecret records, with **1,017**
pending. All original selectors, overlapping audit links and supplemental
proposals remain intact. No candidate credential was exercised.

A further **eight** source-revision findings resolve to locally available commit
objects. The verifier rehashes each complete commit object with the Git object
header, binds the complete candidate to its explicit source-revision field at
the immutable source line, and checks the original scan index and full private
audit hash join. Working-copy contents cannot supply this evidence. See
`S21-detect-secrets-source-commit-review.json` and
`scripts/security/s21/verify_secret_git_objects.py`. Fourteen regressions cover
inexact selectors, full-hash mismatches, absent objects, context substitution,
dirty worktrees and failure-output redaction. No network lookup or credential
exercise occurred.

Current verified nonsecret dispositions: **850/1859**, with **1,009** pending
under #6110. All 3,708 original selector identities and audit joins are retained;
these eight observations do not establish acceptance of the complete story.

A further **ten** fixture content hashes are independently recomputed from
strict base64-decoded canonical fixture payloads, with verified byte lengths.
Seven invalid variants preserve the canonical artifact fields and differ only
by their explicitly named deliberate mutation plus fixture metadata. Two
canonical fixtures prove their own payload hashes; one start-frame reference
matches the complete canonical artifact identity, content type, length and hash.
A fixture pathname or checksum-shaped value alone supplies no acceptance.

The receipt is `S21-detect-secrets-fixture-payload-review.json`; verifier
`scripts/security/s21/verify_fixture_payload_digests.py` checks the exact original
scan index and full private audit join against immutable Git source. Twelve new
regression tests exercise candidate/payload/context/identity/length mismatches,
undeclared mutations, original selector misbinding and dirty working copies.
The complete verifier suite passes 71 tests.

Current verified nonsecret dispositions: **860/1859**, with **999** pending under
#6110. All 3,708 original identities and audit joins are preserved. The original
story remains open; no credential was exercised or additional scope accepted.

A further **16** source-document hashes exactly reproduce four public AWS
documents: a versioned Bedrock pricing JSON and three Markdown model cards.
The compressed public response bytes are retained under
`public-pricing-documents/`, with URL, response time, size and SHA512 integrity
metadata in `S21-detect-secrets-public-document-review.json`. SHA512 provenance
avoids publishing the candidate SHA256 values. The retrieval used an isolated
environment, no authentication/cookies/proxy, an HTTPS host allowlist, rejected
redirects, and bounded response size/time. Unmatched or changed public pages
remain pending; absence of a match is not an acceptance decision.

The offline verifier `scripts/security/s21/verify_public_document_digests.py`
recomputes hashes from the retained bytes and binds each exact original scan
index/full private audit hash to its immutable JSON scalar path and source line.
The paired URL must be in that same source object. Nineteen new tests cover
archive/payload corruption, bounds, URL/path substitution, duplicate keys and
selectors, full original hash mismatches, and repeated-digest field/line
misbinding. The complete verifier suite passes **90 tests**.

Current verified nonsecret dispositions: **876/1859**, with **983** pending under
#6110. All 3,708 original identities and audit joins are retained. This is
partial evidence adjudication; the story remains open. No credential was
exercised or private candidate used in a request.

A further **19** fixture reference literals are directly bound to the imported
`UserCredential(secret_arn=...)` constructor in two vault model tests. The
verifier requires exact original full scan/audit joins, complete source literals
and their constructor/import lines, and rejects class rebinding, shadowing,
concatenation and dynamic constructor arguments. The frozen ORM column and
typed delivery path pass this attribute to `SecretsManagerHelper` and ultimately
the AWS `SecretId` argument. Shape or test pathname alone supplies no acceptance.

See `S21-detect-secrets-model-reference-review.json` and
`scripts/security/s21/verify_model_reference_fixtures.py`. Nineteen new tests
reject substituted imports, payload fields, helper types, consumer arguments,
original hashes/indexes, and duplicate selectors. Only source ASTs are read;
no fixture, application credential flow or resource lookup is executed.

Current verified nonsecret dispositions: **895/1859**, with **964** pending under
#6110. All 3,708 original identities and audit joins are preserved. The original
story remains open.

A further **3** complete synthetic resource literals are bound to the `SecretId`
argument of exact mocked AWS operation expectations. Each expectation follows
the helper call with the same literal, using a fixture that injects a `MagicMock`
client. Frozen helper code carries the identifier to provider `SecretId`,
including the delete helper's keyword dictionary. Source shape alone does not
supply acceptance, and no fixture/provider operation is executed by the verifier.

`S21-detect-secrets-mock-secret-id-review.json` retains exact original records
and source hashes. The new verifier's **25** regressions pass in normal and
optimized Python, rejecting substituted constructors, client injection, payload
fields, provider arguments, original lines/full hashes and duplicate selectors.
Full original scan/audit candidate joins are privately reverified without values
in output. Verified nonsecret dispositions are now **898/1859**, with **961**
pending under #6110. All **3,708** original identities and supplemental proposals
are retained; the story remains open.

A further **5** complete SecretId-name literals are passed to the imported
`UserCredential(secret_arn=...)` constructor. Three use module-level direct
imports and two use direct imports local to the containing function. The
extended verifier requires an unconditional import in the containing scope
before the call, rejects shadowing/rebinding and preserves the existing
ORM-to-delivery-to-provider `SecretId` proof. Names alone do not establish
resource-reference status.

`S21-detect-secrets-model-name-reference-review.json` retains the exact source
records and file hashes. Seven new import/name regressions join the existing
model/mock-reference tests: **51** pass normally and **51** under optimized
Python. Both the original 19-reference receipt and this five-reference receipt
reverify against full private scan/audit joins without candidate output. All
**3,708** original identities and supplemental proposals are retained. Current
verified dispositions: **903/1859**, with **956** pending; #6110 remains open.

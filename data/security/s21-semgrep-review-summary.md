# Semgrep source review — issue #6119

**The original 13,139-observation scope remains open.** The initial agent's fresh
`auto` scan produced 1,035 observations, not the original scanner population.
Supervisor reconciliation now retains every original selector exactly once and
checks its result index and rule against the frozen SARIF artifact.

| Original disposition | Count |
| --- | ---: |
| Informational AI technology detection | 7728 |
| Non-security localization rules | 1286 |
| Pending source review, retained by #6119 | 4048 |
| Source fixed, runtime acceptance open | 2 |
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

## Reviewer GitHub redirect boundary

Exact original selector `run=0|ri=3081` (native CRITICAL, unsuppressed) is
source-fixed with runtime acceptance still open. The GitHub client formerly
followed off-origin redirects before checking the final origin. Native-fetch
loopback regressions reproduce five forbidden destination requests across
301/302/303/307/308; the repaired client makes zero. Each hop is now manual,
bounded and validated before transit. Same-origin read redirects still support
renamed repositories; mutation redirects require method-preserving 307/308.

All 113 component tests pass on Node24, including 22 new regressions (13 fail
against frozen source). Exact-rule Semgrep1.80.0 retains one observation before
and after, with zero errors; scanner absence is not the acceptance evidence.
The receipt at `docs/security/runs/2026-09-26/reviewer-redirect-boundary/review.json`
preserves the complete original identity/severity/candidate join. All13,139
original identities remain, and no runtime deployment or whole-story closure
is claimed. Earlier installation receipts remain independently applicable.

## Explanation event-source redirect boundary

Exact original selector `run=0|ri=9866` retains native CRITICAL rule metadata
and is source-fixed with runtime acceptance open. This is browser code; the
review does not assert server-side SSRF or a browser CORS bypass. The configured
API event stream now refuses redirects before transit and delivery of redirected
events. Direct streams preserve authentication headers, cursors and aborts.

Native-fetch loopback tests reproduce ten baseline destination requests and
ten redirected event deliveries across same-origin/cross-origin301/302/303/307/308;
the candidate makes zero. Two direct200 controls still pass. All21 service/UI
tests and focused ESLint pass. The exact Semgrep1.80.0 rule still emits one
observation before/after with zero errors. Full identity/severity/candidate joins
are in `docs/security/runs/2026-09-26/explanations-redirect-boundary/review.json`.
Redirected API deployments now fail closed; configured endpoints must serve the
stream directly. All13,139 original identities and all runtime holds remain.

## GitLab browser handoff boundary

Exact original selector `run=0|ri=9878` is source-fixed with runtime acceptance
open. The browser now requests a JSON handoff from the authenticated backend
with `redirect: error`, instead of following an opaque redirect and using its
final response URL. The configured GitLab URL is the destination authority;
caller `redirect_uri` and `next` parameters cannot override it. The backend
validates that URL before minting, returns a non-cacheable JSON handoff to the
SPA, and retains 302 responses for legacy clients. Existing external HTTPS,
same-origin `/gitlab`, and configured internal HTTP deployments remain supported.

Thirty frontend tests cover native loopback redirect refusal, direct JSON
controls, malformed destinations and browser opaque-response retry prevention.
Six fail against the baseline implementation. Thirty-nine backend tests cover
signed canonical/tenant identity, legacy behavior, JSON auth and outage refusal,
and malformed configured URLs rejected before credential minting. The frozen
Semgrep rule reports one match before and after; no scanner absence or severity
change is claimed. All 13,139 selector identities remain and 4,047 source reviews
are pending. Receipt: `docs/security/runs/2026-09-26/gitlab-handoff-boundary/review.json`.

Remaining acceptance belongs to #6119: deploy the additive backend before the
frontend and demonstrate actual browser GitLab login on the configured topology.
Frontend-first rollout against an older backend falls back to `/gitlab/` rather
than following a redirect; authenticated handoff needs the new backend. No live
probes or rollout were performed. Existing HTTP transport and callback JWT query
handling are separate open concerns; this patch does not claim to resolve them.

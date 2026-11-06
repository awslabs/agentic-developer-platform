# Extracted POC evidence: Transform, Well-Architected and FinOps

Extraction date: 2026-10-27. Eight stories under the original
[POC epic #7919](https://github.com/aws-e/adp/issues/7919) inform new productization
epics under [L5 #8150](https://github.com/aws-e/adp/issues/8150). Original stories
and acceptance remain intact. All eight source PRs below were open and unmerged
at extraction; their code must be reviewed and adapted, not treated as shipped.

## What was recovered

Retrieved issue bodies/comments, PR metadata, immutable report/helper sources,
15 authenticated ADP invocation records and their transcripts. Private copies
retain source hashes and native evidence references; this public summary omits
deployment identities, raw billing values, credentials and operational storage
coordinates. The historical reports are linked at exact commits.

The API reports all 15 worker invocations as `complete`. That is worker status,
not provider success, cleanup acceptance or an epic completion verdict. Historical
native outcomes below come from reports/transcripts; this extraction did not rerun
AWS operations or independently download every original evidence archive. Six
GitHub Actions job lookups using old run links returned 404, so no new Actions
conclusion is inferred from them. ADP invocation reads succeeded.

| Story | Retrieved worker invocations | Execution date (UTC) | ADP recorded execution cost (USD) | Historical provider outcome |
|---|---:|---|---:|---|
| [#7922](https://github.com/aws-e/adp/issues/7922) | 2 | 2026-10-16 | 9.127104 | Well-Architected Support prerequisite failed; no review |
| [#7923](https://github.com/aws-e/adp/issues/7923) | 3 | 2026-10-16 | 46.250012 | Native FinOps answer matched independent Cost Explorer; scoped resources cleaned |
| [#7980](https://github.com/aws-e/adp/issues/7980) | 2 | 2026-10-17 | 17.301583 | Well-Architected discovery/identity succeeded; Support prerequisite failed |
| [#7981](https://github.com/aws-e/adp/issues/7981) | 3 | 2026-10-17 | 51.097880 | FinOps agent/inquiry created; terminal answer reported billing unavailable |
| [#7982](https://github.com/aws-e/adp/issues/7982) | 2 | 2026-10-17 | 29.192970 | Transform produced a bounded refactor with passing before/after tests |
| [#8014](https://github.com/aws-e/adp/issues/8014) | 1 | 2026-10-17 | 7.964438 | Profile-driven IaC preparation; live apply stopped before AWS calls |
| [#8015](https://github.com/aws-e/adp/issues/8015) | 1 | 2026-10-17 | 12.379023 | 41 Cost Explorer service rows; no native FinOps inquiry |
| [#8016](https://github.com/aws-e/adp/issues/8016) | 1 | 2026-10-17 | 11.148057 | Transform upgraded Uvicorn with unchanged unrelated lock records and passing tests |

Costs are sums of the retrieved invocation `total_cost_usd` values per story.
They are ADP execution accounting, **not AWS service invoices or provider-job
prices**, and do not establish remaining POC allowance. Settlement fields were
unset in these historical records. Provider/ancillary cost reconciliation remains
separate. No spending limit is changed by this extraction.

## Immutable source inventory and reusable assets

Paths in this table are historical sources, not new runtime locations. Follow-up
helpers sometimes copied earlier helpers at a pinned revision; these copies are
not separate platform integrations. Productization belongs in `modules/L5-services/`.

| Story / source PR | Report at extracted revision | Reusable implementation or evidence |
|---|---|---|
| #7922 / [#7940](https://github.com/aws-e/adp/pull/7940) | [Well-Architected setup report][wa-first] — `a06a0b55d1` | Documentation of real Support denial, source/role boundaries and no-created-resource readback; report only |
| #7923 / [#7942](https://github.com/aws-e/adp/pull/7942) | [FinOps successful inquiry][finops-first] — `46bbf63486` | Console procedure, filtered Billing View, role readbacks/simulation, conversation identity, decimal reconciliation and owned cleanup; report only |
| #7980 / [#7985](https://github.com/aws-e/adp/pull/7985) | [Well-Architected helper report][wa-helper] — `72e06b2160` | `scripts/aws-agent-onboarding/well_architected.py`, example YAML, setup/generation/collection lifecycle; 19 reported offline checks |
| #7981 / [#7987](https://github.com/aws-e/adp/pull/7987) | [FinOps helper report][finops-helper] — `5730628092` | `scripts/aws-agent-onboarding/finops.py`, `test_finops.py`, two examples; 35 reported offline tests |
| #7982 / [#7991](https://github.com/aws-e/adp/pull/7991) | [Transform refactor report][transform-refactor] — `cebebe4c4b` | `scripts/aws-agent-onboarding/transform.py`, `transform_runtime.py`, `audit_transform_scope.py`, tests/example; 35 reported workflow tests |
| #8014 / [#8018](https://github.com/aws-e/adp/pull/8018) | [Profile-driven IaC report][wa-iac] — `b442cf8904` | `scripts/frontier-agent-spikes/well-architected-iac.py`, `well_architected_lifecycle.py`, examples, pinned dependencies, source manifest; 20 reported offline tests |
| #8015 / [#8021](https://github.com/aws-e/adp/pull/8021) | [Profile-driven cost inquiry][finops-cost] — `83673b8808` | `scripts/frontier-agent-spikes/finops-cost-inquiry.py`, examples, pinned dependencies, test file and backlog; 33 reported offline tests |
| #8016 / [#8020](https://github.com/aws-e/adp/pull/8020) | [Dependency-upgrade report][transform-upgrade] — `398cf85c60` | `scripts/frontier-agent-spikes/transform-dependency-upgrade.py`, `transform_dependency/`, examples/tests and actual manifest/lock change; 55 reported helper tests |

## Evidence that determines scope

**Transform has two native code-change demonstrations.** The first added
`revision_field(family)` in `modules/gateway/cli/adp-model-policy.py`: six additions,
two deletions, original 13 and expanded 21 relevant tests passed before/after.
The second changed Uvicorn 0.34.0 to 0.34.3 with 26 keepalive/WebSocket tests
passing before/after frozen-lock installs. Independent comparison preserved
unrelated lock records and checked PyPI artifact metadata. Both used `atx 3.19.0`
custom conversations, not continuous-modernization jobs. Reported agent usage
was 4.6357 and 1.8494 minutes; historical price estimates were approximately
$0.16225 and $0.064729, not invoice-verified. Completed replay was demonstrated;
interrupted paid continuation/remote cancellation was not. Full gateway CI and
deployment were not proven by the dependency test subset.

**Well-Architected has preparation and negative-path evidence, no native review.**
Both initial runs encountered `SubscriptionRequiredException`; the later helper
prepared three networking IaC files and six excerpts from five distinct files,
but stopped with the existing Support hold plus missing authenticated tenant and
current budget evidence. No Agent Profile/generation/recommendations were created.
The historical SDK exposed profiles, goals, contexts and recommendation generation;
it did not expose a generation stop/cancel operation. Recheck current eligible
Support plans/API semantics during qualification. Local context readbacks do not
stand in for provider recommendations.

**FinOps has one successful native inquiry and two different follow-up limits.**
The earliest run (#7923) created a custom account-filtered Billing View, two
dedicated roles and an agent through the supported console. One question was
refused; an ordinary rephrasing succeeded with unchanged permissions/guardrails.
The answer matched Cost Explorer's exact amount, USD, interval, metric, view/account
and `Estimated: false`. A displayed conversation ID was retained. Source refresh
time was unavailable. Agent/roles/view absence was checked; context `Deleted`
did not prove permanent erasure. This run used synthetic Product context and real
billing, not the later current Profile API.

The #7981 inquiry reported billing unavailable and the consolidated helper was
not live-requalified. The #8015 independent account-filtered Cost Explorer query
returned 41 rows, but native setup stopped at `DescribeOrganization` denial and
unqualified primary-view scope. It created no agent or inquiry. A Decimal-zero
normalization fix replayed the saved page without another billing request.
These follow-ups do not invalidate #7923, and #7923 does not qualify the later
helper's durable recovery. No supported public FinOps SDK lifecycle/result API
was established; console access is demonstrated, production automation remains
to be qualified. Cost Explorer output must retain its separate provenance.

## Handoff into the new epics

Use the [common extension design](capability-expansion.md) and service-specific
pages to turn these assets into typed, Product-scoped services. Preserve reported
test counts as history; do not mark them as newly executed. Retrieve original
private artifacts through authorized stores when a live acceptance needs them;
an old upload receipt alone is not a fresh byte-for-byte archival verification.
Each new epic needs current integration evidence and portable non-ADP fixtures.

[wa-first]: https://github.com/aws-e/adp/blob/a06a0b55d1905da47c40318caa179220df904515/docs/spikes/aws-agent-setup/well-architected.md
[finops-first]: https://github.com/aws-e/adp/blob/46bbf63486b3af677bddd4a694de9a1c793cfd5a/docs/spikes/aws-agent-setup/finops.md
[wa-helper]: https://github.com/aws-e/adp/blob/72e06b2160263e305078aba1278f54a345f71736/docs/spikes/aws-agent-setup/well_architected-adp.md
[finops-helper]: https://github.com/aws-e/adp/blob/573062809251f240634f998f045d9716834c6f79/docs/spikes/aws-agent-setup/finops-adp.md
[transform-refactor]: https://github.com/aws-e/adp/blob/cebebe4c4bb6ed13cfbd5b9c68749b144cec79f9/docs/spikes/aws-agent-setup/transform-adp.md
[wa-iac]: https://github.com/aws-e/adp/blob/b442cf8904ad003d119659520bced807a7d8131c/docs/frontier-agents/spikes/well-architected-iac.md
[finops-cost]: https://github.com/aws-e/adp/blob/83673b8808bc266ee52f1d1a2d97c6b3a55add1a/docs/frontier-agents/spikes/finops-cost-inquiry.md
[transform-upgrade]: https://github.com/aws-e/adp/blob/398cf85c6017cc5492e246eae440fd4ccf241341/docs/frontier-agents/spikes/transform-dependency-upgrade.md

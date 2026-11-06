# Product Profile release and regression: operator handoff

This is the command and evidence inventory for [Product regression #8774](https://github.com/aws-e/adp/issues/8774).
It describes the **intended** main-branch, protected `dev` path; it is not a
record of a deployment. The release workflow now produces its desired/current
component observations from signed Actions receipts and live runtime readback.
It verifies backend, frontend, installed CLI and schema together. The regression workflow cannot publish
all 31 active checks without the remaining live producers and fixture adapters. Do not
use a synthetic manifest, unit-test result or a manually dispatched engine job
to replace those prerequisites. Deployment and two live passes are owned by
[#8775](https://github.com/aws-e/adp/issues/8775) and
[#8776](https://github.com/aws-e/adp/issues/8776), not this wiring PR.

## Before dispatch

Use the accepted version-2 delivery contract, a reviewed source and workflow
revision on `main`, the authorized protected environment and private component
configuration. Obtain release identity and target observations from their
source owners; never enter a target account, URL, resource ID, credentials or
private receipts in this document or public issue comments. Check the
[release workflow](../../../.github/workflows/product-profile-release.yml)
and [engine workflow](../../../.github/workflows/product-profile-regression.yml)
for their current input contracts. The release has fixed `dev`,
`product-profile` and concurrency `8`; the source, definition, correlation and
contract digest must be pinned to the accepted attempt. Backend and frontend
deployment use the existing push-triggered Actions workflows. Finish those
deployments before dispatching the shared release observer.

The protected release configuration uses the existing foundation connection
role, deployment role, gateway URL and deployment region, plus
`PRODUCT_PROFILE_RELEASE_CONTRACT` (the exact accepted contract JSON) and
`PRODUCT_PROFILE_RUNTIME_PROOF_URL` (the existing protected schema proof route).
The workflow authenticates its attempt with the engine before obtaining AWS
credentials. It retains a provider context, a single-file delivery receipt,
the manifest, and `product-profile-observations-<run ID>-<attempt>` for component
readback. The two engine handoffs are
`adp-deployment-context-product-profile-release.yml-<attempt>` and
`delivery-evidence-<attempt>`; their existing runtime readers require these
GitHub artifact identities.

Live Engineering Insights integration and Context/L5 integration are explicitly
deferred. The manifest records both in `deferred_integrations` with
`live_acceptance: false`; neither is reported as a passing deployment. Catalog
remains in scope. The original Insights criterion inventory below is retained
for later integration acceptance, not evidence that it ran in this release.

After those prerequisites and deployment adapters are available, the authorized
release dispatcher can use the following in one terminal. The four variables
must come from the private accepted delivery attempt, not from the examples in
public documentation:

```bash
: "${ADP_CORRELATION:?private accepted correlation required}"
: "${ADP_SOURCE_REVISION:?reviewed main commit required}"
: "${ADP_DEFINITION_REVISION:?reviewed workflow commit required}"
: "${ADP_CONTRACT_DIGEST:?accepted version-2 digest required}"
gh workflow run product-profile-release.yml --repo aws-e/adp --ref main \
  -f adp_correlation="$ADP_CORRELATION" \
  -f adp_source_revision="$ADP_SOURCE_REVISION" \
  -f adp_definition_revision="$ADP_DEFINITION_REVISION" \
  -f adp_contract_digest="$ADP_CONTRACT_DIGEST" \
  -f environment=dev -f suite=product-profile -f max_parallel=8
```

Find the dispatched run and verify its conclusion and source before selecting
its manifest; a successful dispatch is not a successful release. Reuse an
unchanged component only when its matching readback and receipt are verified.
The release ID is `product-<release run ID>-<attempt>` and the **single**
immutable artifact is `product-profile-release-<release run ID>-<attempt>`.
For a successful run, use the actual private run ID and attempt (not the
illustrative placeholders) to inspect the manifest in a private directory:

```bash
: "${RELEASE_RUN_ID:?successful release run ID required}"
: "${RELEASE_ATTEMPT:?successful release attempt required}"
umask 077
PRIVATE_DIR="$(mktemp -d)"
gh run view "$RELEASE_RUN_ID" --repo aws-e/adp
gh run download "$RELEASE_RUN_ID" --repo aws-e/adp \
  --name "product-profile-release-${RELEASE_RUN_ID}-${RELEASE_ATTEMPT}" \
  --dir "$PRIVATE_DIR"
sha256sum "$PRIVATE_DIR/manifest.json"
```

Compare the computed digest and manifest's release ID, source and component
receipts with the private accepted record. Publish the verified release ID and
digest to the protected regression configuration, not to public docs. Retain
private receipts under the authorized evidence store; dispose of the local
scratch directory using the operator's approved retention procedure.

## Run and inspect regression

Only the delivery engine may dispatch
[`product-profile-regression.yml`](../../../.github/workflows/product-profile-regression.yml)
with its accepted contract, correlation and authorized run identity. A manual
`gh workflow run` of this engine job is **not** an engine attempt. It downloads
the named release manifest and binds its digest to the authorized attempt;
`delivery-evidence-<regression attempt>` contains only `delivery.json` after
all 31 active required checks and owned cleanup verify. Inspect the engine run and
private artifact with the actual run and attempt:

```bash
: "${REGRESSION_RUN_ID:?authorized engine run ID required}"
: "${REGRESSION_ATTEMPT:?authorized engine attempt required}"
umask 077
PRIVATE_DIR="$(mktemp -d)"
gh run view "$REGRESSION_RUN_ID" --repo aws-e/adp
gh run download "$REGRESSION_RUN_ID" --repo aws-e/adp \
  --name "delivery-evidence-${REGRESSION_ATTEMPT}" --dir "$PRIVATE_DIR"
```

The separate [standalone workflow](../../../.github/workflows/product-profile-regression-standalone.yml)
is scheduled or manually selects an existing release. Once its full independent
result producer is wired, an operator with the privately approved release ID
and manifest digest can dispatch it in a separate terminal:

```bash
: "${RELEASE_ID:?verified release ID required}"
: "${MANIFEST_SHA256:?verified manifest digest required}"
gh workflow run product-profile-regression-standalone.yml --repo aws-e/adp \
  --ref main -f release_id="$RELEASE_ID" -f manifest_sha256="$MANIFEST_SHA256"
```

Its artifact is `product-regression-<run ID>-<attempt>`; it never impersonates
engine correlation or supplies the engine's delivery evidence. The current
standalone job selects the manifest but does not yet produce
`standalone-results.json`.

## Known CI shard instability

`Test shard 8` of Gateway CI has failed this regression repair's head twice for
reasons unrelated to the change, which only adds live source producers under
`tests/e2e/product_onboarding`: once on an unrelated Bedrock pool deadline, and
once in run `40323515459` on
`modules/gateway/tests/proxy/test_mantle_real_route_fault_scenarios.py::test_f07_late_receipt_settles_unknown_before_new_spend[6]`
with `AssertionError: assert ['held'] == ['unknown']` (1 failed, 8379 passed).
That failure is the first recovery assertion of the `second_scan_delay=6`
parametrization: the scan reports `held` only for an attempt already resolved to
`unknown`, so a concurrently executing sibling scenario sharing the same
Postgres target can leave `killed-after` unknown before this case scans.

A third Gateway CI run of the same change, `40328534766`, then failed a
different shard with a different pre-existing concurrency scenario:
`Test shard 7` reported
`FAILED tests/orchestration/test_draft_revision_postgres.py::test_save_and_approval_races[revision-save_first] - TimeoutError`
(1 failed, 6203 passed, 1 skipped), while shard 8 and every other shard passed.
Gateway CI only collects `modules/gateway/tests`, so none of this repair's
files - live source producers under `tests/e2e/product_onboarding` and this
runbook - are even collected by the shards that fail, and the failing shard
moves between runs.

Treat a single-shard failure in these shared-Postgres race or Bedrock deadline
scenarios as shard instability: publish a fresh head so the required `Test`
aggregate re-evaluates, and record the run ID. Do not weaken, skip or re-scope
the proxy, metering or orchestration assertions, which are outside this
repair's accepted paths, to land a regression change.

## Release-to-issue evidence index

Keep the private release manifest and its five release observations
(`release-manifest`, `gateway-runtime`, `frontend-runtime`, `installed-cli`,
`release-health`) with the authorized run/attempt and
component readbacks. For every regression check below, retain the actual
selected command, JUnit or installed-CLI receipt, expected and observed result,
run/attempt/source and the actual `product-suite-cleanup.json` written by the
fixture teardown. The consolidated executor runs `pass-1` and `pass-2` with
distinct suite namespaces and unchanged GitHub run/attempt identity. Each
`issue-<number>-ac-<number>.json` indexes both passes and their cleanup receipts.
Cleanup includes independent retirement, membership restoration and released
ownership-lock readbacks. `evidence-index.json` lists the private files and
hashes; a report validator is not a live producer.

| Original verification issue | Required regression checks | User surface and independent proof |
|---|---|---|
| [V04 #8250](https://github.com/aws-e/adp/issues/8250) | `issue-8250-ac-01`, `issue-8250-ac-02`, `issue-8250-ac-03` | Served UI, gateway and installed CLI scope isolation, denials and cleanup. |
| [V05 #8252](https://github.com/aws-e/adp/issues/8252) | `issue-8252-ac-01`, `issue-8252-ac-02`, `issue-8252-ac-03` | Served UI and CLI resume, progress and recovery. |
| [V06 #8256](https://github.com/aws-e/adp/issues/8256) | `issue-8256-ac-01`, `issue-8256-ac-02`, `issue-8256-ac-03` | Live connected-source reuse, status and recovery; offline report validators do not qualify. |
| [V07 #8258](https://github.com/aws-e/adp/issues/8258) | `issue-8258-ac-01`, `issue-8258-ac-02`, `issue-8258-ac-03` | Served guidance journeys plus live effect and resume proof. |
| [V08 #8261](https://github.com/aws-e/adp/issues/8261) | `issue-8261-ac-01`, `issue-8261-ac-02`, `issue-8261-ac-03` | Owned administrator, repository and provider handoff readbacks. |
| [V09 #8264](https://github.com/aws-e/adp/issues/8264) | `issue-8264-ac-01`, `issue-8264-ac-02`, `issue-8264-ac-03` | Saved budget provenance, denials and resume. |
| [V10 #8267](https://github.com/aws-e/adp/issues/8267) | `issue-8267-ac-01`, `issue-8267-ac-02`, `issue-8267-ac-03` | Installed-CLI full journey, unavailable peer and release evidence. |
| [V11 #8268](https://github.com/aws-e/adp/issues/8268) | `issue-8268-ac-01`, `issue-8268-ac-02`, `issue-8268-ac-03` | Served full journeys, cross-client resume and release regressions. |
| [V12 #8273](https://github.com/aws-e/adp/issues/8273) | `issue-8273-ac-01`, `issue-8273-ac-02`, `issue-8273-ac-03` | Real Catalog API, served UI, history and recovery; provider-native work resolution remains a source prerequisite. |
| [V13 #8276](https://github.com/aws-e/adp/issues/8276) | `issue-8276-ac-01`, `issue-8276-ac-02`, `issue-8276-ac-03`, `issue-8276-ac-04` | Source-owned published Insights snapshots and installed-CLI readback; offline checks are not live evidence. |

Browser verification accepts a deployment from an earlier commit only when
the complete component input fingerprint matches the selected source. It still
verifies the signed deployment receipt and every served asset. Evidence keeps
both the selected source and the actual backend/frontend source revisions;
changes to application code or build inputs require a matching deployment.
The checkout must retain both revisions so compatibility can be verified even
when a test runs from an isolated evidence directory.

The engine pins its own reviewed `main` revision, which advances every time a
repair or follow-up change merges, while the named shared release stays
immutable. Setup therefore verifies **lineage** rather than equality:
[`product_shared_release.py`](../../../tests/e2e/product_onboarding/product_shared_release.py)
requires the release manifest's `source_revision` to be the engine source
itself or an ancestor of it, with both revisions present in the engine
attempt's full-history checkout, and the engine source identical to the
revision actually checked out. A release built from a revision the attempt does
not contain, a short or non-commit revision, or a checkout that differs from the
pinned source still fails before any fixture allocation, and the exported
`PRODUCT_EXPECTED_REVISION` remains the release's own source revision so live
readback keeps asserting the deployed components' commit. Standalone selection
transports no engine source and so can never claim engine lineage. Run
`40338993789` failed with
`ValueError: Selected Product release differs from the engine source` because
the old equality check made the shared release unusable as soon as `main` moved
past it.

The other four required checks are `baseline-regression`, `repeatability`,
`cleanup` and `evidence-index` are assembled by `product_completion_attempt.py`.
It executes the existing Product create/conflict/evidence baseline alongside
each full inventory, checks identical successful selectors and disjoint owned
Product/Project IDs between passes, and validates cleanup before indexing
evidence. An incomplete producer or fixture inventory fails before allocation.
This assembly does not supply missing scenario adapters or configure deployment
credentials; those prerequisites must be completed before live dispatch. With the four live Insights criteria explicitly
deferred, there are **31 active regression checks**. The original Insights
inventory above remains recorded for later integration and cannot be reported
as passed. Link the private packet to each issue's
own acceptance criterion without pasting sensitive evidence. Associate both
full live passes with the **same** release ID and digest; record failures,
skips, not-run checks and recovery attempts rather than treating them as passes.
The [inventory](../../../tests/e2e/product_onboarding/product_completion_inventory.py),
[runner](../../../tests/e2e/product_onboarding/product_completion_runner.py) and
[delivery adapter](../../../tests/e2e/product_onboarding/product_completion_delivery.py)
are the executable source of the check/fixture mapping and refusal rules.

# S19 — One-off scan coverage and severity reporting: disposition

Work-package **S19** of the 2026-09-21 security scan (parent #5599, issue #5618).

This package is a **coverage/correctness** fix, not a newly rated vulnerability. The
2026-09-21 run reported success while scanning a fraction of what it claimed and
mis-rating what it did find. Each defect below made a broken scan look green.

## Findings and disposition

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | Only 8/17 Grype images and 6/17 Syft SBOMs produced results; the build passed at ≥50% coverage | **Fixed** — every expected target must produce a usable result; missing targets are named and fail the build |
| 2 | Superplane images not covered; runtime images with no local Dockerfile invisible to the inventory | **Fixed** — inventory derives from the release lock: 3 maintained source images + SkyPilot pinned by digest |
| 3 | detect-secrets artifact was a newline-separated detector-name list, not JSON | **Fixed** — `--list-all-plugins` misuse removed; results read back out of the baseline |
| 4 | `detect-secrets audit --report` crashed on every run (baseline named a deleted file) | **Fixed** — baseline reconciled; the audit now consumes the updated current-run scan and a test runs the real command |
| 5 | cfn-nag aborted on all three templates and still reported success | **Fixed** — FATAL matched on `id` (the real record is `id=FATAL`, `type=FAIL`), parsed as JSON |
| 6 | Deny-list encoding raised during YAML parse, becoming a FATAL on every template | **Fixed** — no `--deny-list-path` (documented no-op); deny-list asserted pure ASCII |
| 7 | A raw CVSS score outranked the scanner's own rating | **Fixed** — native ratings win; CVSS is the fallback |
| 8 | SARIF `level: error` was read as an explicit high severity | **Fixed** — reported `unrated`, still counted; details remain in the private summary |
| 9 | Summary published via `actions/upload-artifact` (found while verifying) | **Fixed** — published to the private S3 rendezvous; logs contain aggregate counts only |
| 10 | New coverage/severity tests were not bound to CI (found while verifying) | **Fixed** — pinned into Script Tests with path triggers |
| 11 | Unrated and gated finding fingerprints leaked rule IDs, paths, and lines into Actions logs (review finding) | **Fixed** — differ output is aggregate by tool/severity; per-finding data stays in private `summary.json` |
| 12 | detect-secrets audited the empty committed baseline and was absent from reconciliation (review finding) | **Fixed** — current scan candidates are validated against the audit and deduplicated into the private summary |
| 13 | Identical Grype findings in different images collapsed into one occurrence (review finding) | **Fixed** — Grype fingerprints include the SARIF report/image name; a legacy aggregate baseline matches only when current image attribution is unambiguous |
| 14 | The one-off commands dispatched a commit SHA and selected the newest unrelated run (review finding) | **Fixed** — dispatch uses a branch/tag ref, resolves its current commit independently, and rejects missing or ambiguous correlation-bound runs |

Nothing was baselined away, and no finding was suppressed to make it disappear.
Global finding exceptions and baseline reconciliation remain **S21's** (#5620) call.

## Severity: native rating vs CVSS vs SARIF level

The gate was wrong in both directions, so the fix had to narrow what may be called
high *without* dropping anything.

**A raw CVSS number is not a verdict.** CVE-2020-15778 is the canonical case: the feed
maintainer rates it `low`, its CVSS base score is `7.8`. Reading the number first
reported a low-rated CVE as high and failed the build on it. A maintainer's rating
accounts for how the package is actually built and shipped; the base score does not.
Native ratings are now consulted first, and CVSS is used only when the scanner
published no qualitative rating of its own.

**A SARIF `level` is not a severity judgement.** It is a document-classification
default, and many tools stamp *every* result `error`. Mapping `error → high` therefore
promoted a whole tool's output into the gate. Findings whose only signal is a level are
now reported `unrated`.

**Unrated is not ignored.** This is the part that keeps the fix honest. SARIF findings
record `native`, `cvss`, or `default-level` in `new_severity_sources`; tools without a
severity model, including detect-secrets, record `tool-unrated`. Unrated findings are
counted in `new_unrated_count`, and the workflow prints aggregate per-tool counts. The
private summary retains each fingerprint for manual triage without exposing repository
locations in Actions logs.

## Commands for S21's final one-off run

The workflow is **dispatch-only** and must stay that way: no `pull_request`, `push`, or
`schedule` trigger. Two guard tests assert this by parsing the workflow.

The engine dispatches the accepted producer after S21 and all direct predecessors merge.
For a diagnostic reproduction, bind the source, workflow definition, account, region, and
correlation explicitly; never rely on the current branch or ambient account as evidence:

```bash
SOURCE_REVISION=<40-character source commit to scan>
DISPATCH_REF=<branch-or-tag-containing-the-reviewed-workflow>
git fetch --quiet --no-tags origin "$DISPATCH_REF"
DEFINITION_REVISION=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
test "$(printf '%s' "$SOURCE_REVISION" | wc -c)" -eq 40
test "$(printf '%s' "$DEFINITION_REVISION" | wc -c)" -eq 40
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
CORRELATION=$(printf '%s' '<evaluation execution id>' | sha256sum | cut -d' ' -f1)

gh workflow run security-scan.yml --ref "$DISPATCH_REF" \
  -f adp_correlation="$CORRELATION" \
  -f adp_source_revision="$SOURCE_REVISION" \
  -f adp_definition_revision="$DEFINITION_REVISION" \
  -f expected_account_id="$ACCOUNT_ID" \
  -f region="$REGION" \
  -f scan_scope=all
```

The workflow compares `github.sha` with `adp_definition_revision` before doing any scan
work. If the branch or tag resolves to a different commit during dispatch, the context
job fails rather than producing evidence under the wrong definition.

A `superplane` dispatch is diagnostic only and cannot satisfy the accepted whole-repository
producer contract. It uses the same five transport/target inputs with
`-f scan_scope=superplane`. The run name must be `ADP deployment <correlation>`.

Locate the exact run by its unique correlation title and independently bind its event,
branch/tag, and definition commit. Reject zero or multiple matches; never substitute the
newest workflow run. Record both the run ID and run attempt because reruns use distinct
context, receipt, reconciliation, findings, child-state, and source-archive names:

```bash
RUN_TITLE="ADP deployment ${CORRELATION}"
MATCHES='[]'
for _ in $(seq 1 30); do
  RUNS=$(gh run list --workflow=security-scan.yml \
    --branch "$DISPATCH_REF" --commit "$DEFINITION_REVISION" \
    --event workflow_dispatch --limit 100 \
    --json attempt,databaseId,displayTitle,event,headBranch,headSha,workflowName)
  MATCHES=$(jq -c \
    --arg title "$RUN_TITLE" \
    --arg branch "$DISPATCH_REF" \
    --arg revision "$DEFINITION_REVISION" \
    '[.[] | select(
      .displayTitle == $title and
      .event == "workflow_dispatch" and
      .headBranch == $branch and
      .headSha == $revision and
      .workflowName == "Security Scan"
    )]' <<<"$RUNS")
  MATCH_COUNT=$(jq 'length' <<<"$MATCHES")
  test "$MATCH_COUNT" -le 1 || { echo "ambiguous security scan correlation" >&2; exit 1; }
  test "$MATCH_COUNT" -eq 0 || break
  sleep 2
done
test "$(jq 'length' <<<"$MATCHES")" -eq 1 || {
  echo "correlated security scan run not found" >&2
  exit 1
}
RUN_ID=$(jq -er '.[0].databaseId | select(type == "number" and . > 0)' <<<"$MATCHES")
RUN_ATTEMPT=$(jq -er '.[0].attempt | select(type == "number" and . > 0)' <<<"$MATCHES")

while :; do
  RUN=$(gh run view "$RUN_ID" --attempt "$RUN_ATTEMPT" \
    --json attempt,conclusion,databaseId,displayTitle,event,headBranch,headSha,status,workflowName)
  jq -e \
    --argjson run_id "$RUN_ID" \
    --argjson run_attempt "$RUN_ATTEMPT" \
    --arg title "$RUN_TITLE" \
    --arg branch "$DISPATCH_REF" \
    --arg revision "$DEFINITION_REVISION" \
    '.databaseId == $run_id and
     .attempt == $run_attempt and
     .displayTitle == $title and
     .event == "workflow_dispatch" and
     .headBranch == $branch and
     .headSha == $revision and
     .workflowName == "Security Scan"' <<<"$RUN" >/dev/null
  STATUS=$(jq -r '.status' <<<"$RUN")
  test "$STATUS" != completed || break
  sleep 15
done
jq -e '.status == "completed" and .conclusion == "success"' <<<"$RUN" >/dev/null
```

Download and validate the authenticated context before trusting any scan evidence. This
binds the source revision, workflow definition, correlation, run ID, run attempt, account,
and region. Artifact names include the selected attempt, so a later rerun cannot replace
the evidence being read:

```bash
EVIDENCE_DIR=$(mktemp -d)
CONTEXT_ARTIFACT="adp-deployment-context-security-scan.yml-${RUN_ATTEMPT}"
gh run download "$RUN_ID" -n "$CONTEXT_ARTIFACT" -D "$EVIDENCE_DIR/context"
CONTEXT="$EVIDENCE_DIR/context/deployment-context.json"
jq -e \
  --argjson run_id "$RUN_ID" \
  --argjson run_attempt "$RUN_ATTEMPT" \
  --arg definition "$DEFINITION_REVISION" \
  --arg source "$SOURCE_REVISION" \
  --arg correlation "$CORRELATION" \
  --arg account "$ACCOUNT_ID" \
  --arg region "$REGION" \
  '.run_id == $run_id and
   .run_attempt == $run_attempt and
   .workflow_path == ".github/workflows/security-scan.yml" and
   .workflow_revision == $definition and
   .source_revision == $source and
   .correlation == $correlation and
   .account_id == $account and
   .region == $region' "$CONTEXT" >/dev/null
```

The detailed summary is **not** a run artifact because it contains finding locations. Read
it from the private, attempt-scoped rendezvous only after the context passes. The typed
receipt and sanitized reconciliation are the repository evidence-reader artifacts:

```bash
aws s3 cp \
  "s3://adp-dev-security-scans-${ACCOUNT_ID}/findings/${RUN_ID}/${RUN_ATTEMPT}/summary/summary.json" -
gh run download "$RUN_ID" -n "adp-repository-scan-${RUN_ATTEMPT}" \
  -D "$EVIDENCE_DIR/receipt"
gh run download "$RUN_ID" -n "security-reconciliation-${RUN_ATTEMPT}" \
  -D "$EVIDENCE_DIR/reconciliation"
```

Per-image coverage and provenance for both scanners, including the actual image digest and
artifact hash, use the same attempt-scoped evidence prefix:

```bash
aws s3 cp \
  "s3://adp-dev-security-scans-${ACCOUNT_ID}/repository-scans/${RUN_ID}/${RUN_ATTEMPT}/grype/coverage.json" -
aws s3 cp \
  "s3://adp-dev-security-scans-${ACCOUNT_ID}/repository-scans/${RUN_ID}/${RUN_ATTEMPT}/syft/coverage.json" -
```

### Expected inventory

Confirm what the run is required to cover before trusting a clean result. `--format count`
is the number the Grype and Syft jobs enforce; a shortfall now fails the job.

```bash
python3 codebuild/security_image_targets.py --format count           # all targets
python3 codebuild/security_image_targets.py --scope superplane --format tsv
```

At the scanned commit, `--scope superplane` resolves to the three maintained source
images (`superplane-api`, `superplane-controller`, `superplane-platform-monitor`) plus
`skypilot-api` pinned by digest. An unpinned runtime image is a hard error, so the scan
can never silently follow a moving tag.

## Validation

```bash
pip install pytest pytest-timeout pyyaml detect-secrets==1.5.0

cd .github/scripts && pytest tests/test_security_scan_tool_invocations.py \
                             tests/test_diff_security_findings.py \
                             tests/test_security_agent_dedup.py \
                             tests/test_securityagent_preflight.py -q
```

```bash
pytest codebuild/tests/test_security_image_scan.py -q
```

`detect-secrets` must be installed: two gates shell out to the real binary and skip
silently without it. The planted-key gate verifies the candidate reaches the raw scan,
the current-run audit, and the private reconciliation summary.

The new gates were mutation-tested — restoring CVSS-first ordering, promoting `error`
to high, neutering the dispatch-only check, and reintroducing a `pull_request` trigger
each make the intended test fail. Tests that cannot fail are not evidence.

Pre-existing on main and untouched by this package: 21 failures in
`.github/scripts/tests/test_author_grouping_plan.py` (identical on `main`).

## Limitations

Verification is by focused pipeline/parser tests against real record shapes and real
scanner output, plus the live inventory resolution. The repaired workflow itself has
**not** been dispatched — an end-to-end one-off run is S21's final-evidence step, and
the coverage numbers it reports are what confirm findings 1 and 2 in a live run.

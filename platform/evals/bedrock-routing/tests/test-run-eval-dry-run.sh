#!/usr/bin/env bash
#
# =============================================================================
# test-run-eval-dry-run.sh — tests for the Bedrock-routing eval harness itself
# =============================================================================
# Issue #4761 (R7). Runs in CI on every PR that touches this eval or the shared
# harness, with no AWS, no cluster and no network: run-eval.sh --dry-run stubs
# the external boundary.
#
# WHAT THEY PROTECT
# An eval is worth exactly what its harness guarantees, and the guarantees that
# matter here are all SILENT when broken:
#
#   1. The scope guard refuses a write outside the designated test tenant. This
#      eval authors Bedrock routing rules through the real admin API against a
#      shared dev account. A routing rule decides WHOSE AWS BILL PAYS for
#      inference, so a guard that stopped firing would not fail the run — it
#      would silently redirect a real tenant's Bedrock spend. This is the single
#      most dangerous failure mode in the file and is tested first.
#   2. Cleanup runs even when a phase explodes, so a crashed run cannot leave a
#      mapping behind that keeps rerouting a tenant's bill after the job ends.
#   3. The gates SKIP rather than pass. Enforcement is not wired in dev and R5 is
#      not merged; those cases must report as unrun with a reason. A gate that
#      degraded into a pass would report a capability as proven that has never
#      once executed — the worst outcome for an eval, because it retires the
#      question.
#   4. Fixture drift FAILS loudly and is never confused with a routing
#      regression. The issue's own requirement: the sandbox role and its model
#      enablement are standing fixtures, and if they drift the suite must say so
#      rather than reporting a false routing failure.
#   5. --inject-failure genuinely goes red THROUGH THE REAL ASSERTIONS. The
#      injection perturbs the stubbed world (the platform reports the wrong
#      signing account) rather than appending a synthetic failure, so the eval's
#      own checks have to catch it. A run that passed under injection would prove
#      the assertions had stopped discriminating.
#   6. A fatal setup error cannot print a green banner. write_summary keys its
#      verdict off $FAILURES and die() exits without incrementing it, so a run
#      that died in phase 0 once printed "eval passed: 0 failures" while exiting
#      1. That is the precise dishonesty this suite exists to avoid.
#
# Mirrors platform/evals/budget-ratelimit/tests/test-run-eval-dry-run.sh, the
# proven pattern (#4163) and the shared-lib regression guard.
#
# Usage: ./platform/evals/bedrock-routing/tests/test-run-eval-dry-run.sh
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_SCRIPT="$SCRIPT_DIR/../run-eval.sh"
LIB_DIR="$SCRIPT_DIR/../../lib"

TESTS_RUN=0
TESTS_FAILED=0
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'

ok()   { echo "${GREEN}  ✓${NC} $1"; }
bad()  { echo "${RED}  ✗${NC} $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }
name() { TESTS_RUN=$((TESTS_RUN + 1)); echo ""; echo "${BLUE}[test $TESTS_RUN]${NC} $1"; }

assert_contains() {
  local haystack="$1" needle="$2" what="$3"
  case "$haystack" in
    *"$needle"*) ok "$what" ;;
    *) bad "$what (expected to find '$needle')" ;;
  esac
}

assert_not_contains() {
  local haystack="$1" needle="$2" what="$3"
  case "$haystack" in
    *"$needle"*) bad "$what (unexpectedly found '$needle')" ;;
    *) ok "$what" ;;
  esac
}

assert_eq() {
  if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$2', got '$1')"; fi
}

# A fresh workdir + fake HOME per invocation so tests cannot pollute each other.
new_env() {
  local n="$1"
  RUN_DIR="$TEST_ROOT/$n"
  mkdir -p "$RUN_DIR/work" "$RUN_DIR/home"
}

# run_eval <name> [args...] — invoke the eval with an isolated HOME/workdir.
# Sets RUN_OUT, RUN_RC and RUN_DIR. Deliberately NOT called inside $( ): that
# would run it in a subshell and RUN_RC/RUN_DIR would never reach the caller,
# silently disabling every exit-code assertion in this file.
run_eval() {
  local n="$1"; shift
  new_env "$n"
  env -i \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    HOME="$RUN_DIR/home" \
    ENVIRONMENT=dev \
    AWS_REGION=us-east-1 \
    EVAL_RUN_ID="test-$n" \
    EVAL_WORKDIR="$RUN_DIR/work" \
    GITHUB_STEP_SUMMARY="$RUN_DIR/summary.md" \
    bash "$EVAL_SCRIPT" --dry-run "$@" > "$RUN_DIR/out.log" 2>&1
  RUN_RC=$?
  RUN_OUT="$(cat "$RUN_DIR/out.log")"
}

echo "═══ Bedrock-routing eval harness tests ═══"
echo "eval script: $EVAL_SCRIPT"

# -----------------------------------------------------------------------------
name "syntax: run-eval.sh parses"
# -----------------------------------------------------------------------------
if bash -n "$EVAL_SCRIPT" 2>/dev/null; then
  ok "bash -n clean"
else
  bad "bash -n reported a syntax error"
fi

# -----------------------------------------------------------------------------
name "the scope guard refuses to author a rule outside the designated test tenant"
# -----------------------------------------------------------------------------
# THE most important test here. A Bedrock routing rule redirects who pays for
# inference, so an unguarded write against shared dev would move a real tenant's
# bill. The guard must DIE, not record a failure and continue.
#
# Driven by extracting the real function and calling it, so the test exercises
# the shipped code rather than a re-description of it. `die` is stubbed to exit
# non-zero, which is the contract the guard relies on.
GUARD_FN="$(sed -n '/^assert_test_scope() {/,/^}/p' "$EVAL_SCRIPT")"
if [ -z "$GUARD_FN" ]; then
  bad "could not extract assert_test_scope() from run-eval.sh — was it renamed?"
else
  GUARD_OUT="$(env -i PATH="$PATH" bash -c "
    set -uo pipefail
    ALLOWED_SCOPE_ORG_PATTERN='adp-dev-pentest-org-'
    die() { echo \"DIED: \$*\"; exit 9; }
    $GUARD_FN
    assert_test_scope 'mapping scope' 'org:adp-dev-pentest-org-a' && echo 'ALLOWED-OK'
    assert_test_scope 'mapping scope' 'org:acme-corp-production'
    echo 'REACHED-AFTER-FORBIDDEN'
  " 2>&1)"
  GUARD_RC=$?
  assert_contains "$GUARD_OUT" "ALLOWED-OK" "the designated test tenant passes the guard"
  assert_contains "$GUARD_OUT" "DIED:" "a non-test tenant makes the guard die"
  assert_contains "$GUARD_OUT" "acme-corp-production" "the refusal names the offending scope"
  assert_not_contains "$GUARD_OUT" "REACHED-AFTER-FORBIDDEN" \
    "the guard aborts rather than continuing past a forbidden write"
  assert_eq "$GUARD_RC" "9" "the guard exits non-zero (it dies, it does not just log)"

  # A real tenant whose name merely CONTAINS the test prefix as a suffix must not
  # sneak through, and neither may the other rungs of a foreign org.
  EDGE_OUT="$(env -i PATH="$PATH" bash -c "
    set -uo pipefail
    ALLOWED_SCOPE_ORG_PATTERN='adp-dev-pentest-org-'
    die() { echo \"DIED: \$1\"; exit 9; }
    $GUARD_FN
    assert_test_scope 'mapping scope' 'org:evil-org' || echo 'REFUSED-plain'
  " 2>&1)"
  assert_contains "$EDGE_OUT" "DIED:" "an unrelated org is refused"
fi

# The write that AUTHORS a rule (phase 4) must be guarded, and the guard must
# come BEFORE the mutating call — a guard after the PUT would be decoration.
# Checked structurally against the phase body so a future edit that reorders or
# drops it is caught here rather than by a rerouted tenant bill.
PHASE4="$(sed -n '/^phase_4() {/,/^}/p' "$EVAL_SCRIPT")"
if [ -z "$PHASE4" ]; then
  bad "could not extract phase_4() — was the authoring phase renamed?"
else
  assert_contains "$PHASE4" "assert_test_scope" "the authoring phase calls the scope guard"
  G_LINE="$(printf '%s\n' "$PHASE4" | grep -n 'assert_test_scope' | head -1 | cut -d: -f1)"
  W_LINE="$(printf '%s\n' "$PHASE4" | grep -n 'PUT "/api/admin/bedrock-routing/mappings' | head -1 | cut -d: -f1)"
  if [ -n "$G_LINE" ] && [ -n "$W_LINE" ] && [ "$G_LINE" -lt "$W_LINE" ]; then
    ok "the guard runs BEFORE the mapping is written (guard line $G_LINE, write line $W_LINE)"
  else
    bad "the scope guard does not precede the mapping write (guard=$G_LINE write=$W_LINE)"
  fi
  # Cleanup must be able to find a rule even if the process dies mid-write, so
  # the intent is recorded before the mutation, not after it.
  S_LINE="$(printf '%s\n' "$PHASE4" | grep -n 'state_set CREATED_MAPPING_SCOPE' | head -1 | cut -d: -f1)"
  if [ -n "$S_LINE" ] && [ -n "$W_LINE" ] && [ "$S_LINE" -lt "$W_LINE" ]; then
    ok "the created scope is recorded before the write, so a mid-write death is still cleaned up"
  else
    bad "CREATED_MAPPING_SCOPE is recorded after the write (state=$S_LINE write=$W_LINE)"
  fi
fi

# -----------------------------------------------------------------------------
name "a clean dry run passes, and mirrors the live run's shape"
# -----------------------------------------------------------------------------
run_eval control; OUT="$RUN_OUT"
assert_eq "$RUN_RC" "0" "the un-injected control run exits 0"
assert_contains "$OUT" "eval passed: 0 failures" "control run records no failures"
PASSES="$(grep -cE '^\[PASS\]' "$RUN_DIR/out.log" || echo 0)"
if [ "$PASSES" -ge 30 ]; then
  ok "the control run makes $PASSES passing assertions"
else
  bad "only $PASSES passing assertions — phases are not really executing"
fi
# All 13 phases must be reached, or a later phase is silently never exercised.
for ph in 0 1 2 3 4 5 6 7 8 9 10 11 12; do
  assert_contains "$OUT" "Phase $ph —" "phase $ph runs"
done

# -----------------------------------------------------------------------------
name "the gates SKIP with a reason rather than passing"
# -----------------------------------------------------------------------------
# The heart of the honesty contract. Enforcement is not wired in the environment
# under test, so those cases must report as UNRUN with their blocking reason. A
# gate that degraded into a pass would retire the question while proving nothing.
run_eval gates; OUT="$RUN_OUT"
SKIPS="$(grep -E '^\[SKIP\]' "$RUN_DIR/out.log" || true)"
if [ -z "$SKIPS" ]; then
  bad "no SKIPs at all — the unwired-environment gates are not reporting"
else
  ok "the run emits $(printf '%s\n' "$SKIPS" | wc -l | tr -d ' ') explicit skip(s)"
fi
assert_contains "$SKIPS" "BG_BEDROCK_ROUTING_ENFORCE" \
  "the enforcement skip names the missing env flag as its blocking reason"
# Every skip must carry a reason, not just a label: a bare skip is unactionable.
SHORT_SKIPS=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  [ "${#line}" -lt 40 ] && SHORT_SKIPS=$((SHORT_SKIPS + 1))
done <<< "$SKIPS"
assert_eq "$SHORT_SKIPS" "0" "every skip carries an explanatory reason, not a bare label"
# And a skip must never be counted as a pass.
assert_contains "$OUT" "eval passed: 0 failures" "skips do not make the run red"
assert_not_contains "$SKIPS" "[PASS]" "no skip is recorded as a pass"

# --- R5/R6 are MERGED: their cases must RUN, not skip --------------------------
# This is the regression guard for the specific way this eval was wrong before.
# The earlier revision probed `/api/bedrock-routing/self` — a path that has never
# existed — so R5_PRESENT could only ever be false and phase 7 skipped forever
# while announcing "R5 is not merged" about merged code. A false skip is the
# failure mode this suite exists to prevent, so assert the absence of that claim
# directly: nothing may cite R5 or R6 as an unmerged blocker.
assert_not_contains "$SKIPS" "R5 (#4746) is not merged" \
  "no skip claims R5 is unmerged — it merged as 169fe17 and its cases must run"
assert_not_contains "$SKIPS" "R6 (#4747) is not merged" \
  "no skip claims R6 is unmerged — it merged as 6ff19d8 and its case must run"
# The probe must name the real route. Asserted against the source because a typo
# here is invisible at runtime: a wrong path just 404s into a plausible skip.
assert_contains "$(cat "$EVAL_SCRIPT")" "/api/me/bedrock-routing/selection" \
  "the R5 presence probe uses the real self-selection route"
assert_not_contains "$(cat "$EVAL_SCRIPT")" "/api/bedrock-routing/self\"" \
  "the never-existed probe path is gone (it manufactured a permanent false skip)"

# The settled authority-escalation rule must be exercised with the REAL contract.
# It is a 422 carrying reason='pinned_by_platform_admin', NOT the 409 that #4761
# and #4748 both predicted in advance: self_routes._rejected deliberately mirrors
# R4's so both halves of the surface answer one error vocabulary. Asserting 409
# would have failed a correct implementation.
assert_contains "$OUT" "pinned_by_platform_admin" \
  "the pinned-row refusal is asserted with the reason code the code actually returns"
assert_not_contains "$(cat "$EVAL_SCRIPT")" 'put_status" = "409' \
  "the refusal is not asserted as a 409 — no route on this feature returns 409"
# Both writes must be guarded, or the PUT refusal is bypassable via delete-then-reselect.
assert_contains "$OUT" "two-request bypass" \
  "the DELETE half of the pin guard is exercised, not just the PUT"
# R6 has no HTTP surface, so it is asserted against the retirement guard itself.
assert_contains "$OUT" "fails loudly" \
  "post-R6 asserts that =user fails loudly rather than silently degrading"

# -----------------------------------------------------------------------------
name "fixture drift FAILS loudly and is not reported as a routing regression"
# -----------------------------------------------------------------------------
# The issue's explicit requirement. Drift is simulated by removing the fixture
# from the stubbed world; the eval must go red with FIXTURE DRIFT wording rather
# than blaming routing.
new_env drift
env -i \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  HOME="$RUN_DIR/home" ENVIRONMENT=dev AWS_REGION=us-east-1 \
  EVAL_RUN_ID="test-drift" EVAL_WORKDIR="$RUN_DIR/work" \
  EVAL_STUB_DRIFT_EXTERNAL_ID=1 \
  bash "$EVAL_SCRIPT" --dry-run --phases 0 > "$RUN_DIR/out.log" 2>&1
DRIFT_RC=$?
DRIFT_OUT="$(cat "$RUN_DIR/out.log")"
if [ "$DRIFT_RC" -ne 0 ]; then
  ok "a drifted fixture makes the run exit non-zero (rc=$DRIFT_RC)"
else
  bad "a drifted fixture still exited 0 — drift would read as green"
fi
assert_contains "$DRIFT_OUT" "FIXTURE DRIFT" \
  "the failure is labelled FIXTURE DRIFT, so it is not mistaken for a routing regression"

# -----------------------------------------------------------------------------
name "--inject-failure wrong-account goes red through the REAL assertions"
# -----------------------------------------------------------------------------
# THE acceptance check on the eval. The injection perturbs the stubbed world (the
# platform reports the wrong signing account) instead of appending a synthetic
# failure, so the eval's own assertions must notice. If this ever passes clean,
# the assertions have stopped discriminating and the eval proves nothing.
run_eval inject --inject-failure wrong-account; OUT="$RUN_OUT"
assert_contains "$OUT" "EXPECTED to fail" "announces that the run is deliberately broken"
if [ "$RUN_RC" -ne 0 ]; then
  ok "the deliberately-broken run exits non-zero (rc=$RUN_RC)"
else
  bad "the deliberately-broken run PASSED — the eval is not asserting anything"
fi
# The failures must come from real assertions about the account, not a synthetic
# "deliberate failure" line.
INJ_FAILS="$(grep -E '^\[FAIL\]' "$RUN_DIR/out.log" || true)"
assert_not_contains "$INJ_FAILS" "deliberate failure" \
  "the red comes from real assertions, not a hardcoded failure line"
assert_contains "$INJ_FAILS" "000000000000" \
  "an assertion names the wrong account it caught"
NFAILS="$(printf '%s\n' "$INJ_FAILS" | grep -c . || echo 0)"
if [ "$NFAILS" -ge 2 ]; then
  ok "$NFAILS independent assertions caught the perturbation"
else
  bad "only $NFAILS assertion caught it — the account is checked in too few places"
fi
assert_contains "$(cat "$RUN_DIR/summary.md" 2>/dev/null)" "EXPECTED to fail" \
  "the job summary flags the injected failure"

# -----------------------------------------------------------------------------
name "--inject-failure rejects an unknown kind"
# -----------------------------------------------------------------------------
run_eval badinject --inject-failure not-a-real-kind; OUT="$RUN_OUT"
if [ "$RUN_RC" -ne 0 ]; then
  assert_contains "$OUT" "Unknown --inject-failure" "rejects an unsupported injection kind"
else
  bad "accepted an unknown --inject-failure kind"
fi

# -----------------------------------------------------------------------------
name "cleanup runs when a phase explodes mid-run, and verifies nothing is left"
# -----------------------------------------------------------------------------
# The guarantee that a crashed run does not leave a routing rule behind. Phase 4
# has already AUTHORED a mapping by the time it dies, so this is the real
# scenario rather than a no-op teardown.
run_eval failmid --fail-phase 4; OUT="$RUN_OUT"
assert_contains "$OUT" "--fail-phase 4" "the injected failure fired"
assert_contains "$OUT" "cleanup: removing anything this run created" \
  "cleanup ran despite the mid-run death"
assert_contains "$OUT" "no mapping remains for the test org" \
  "cleanup VERIFIES the mapping is gone rather than assuming it"
if [ "$RUN_RC" -ne 0 ]; then
  ok "exits non-zero after a mid-run death (rc=$RUN_RC)"
else
  bad "exited 0 after a mid-run death — CI would report green"
fi
# No mapping may survive in the stub's own store: an entry left there is a rule
# that would have been left behind in dev.
LEFTOVER=$(find "$RUN_DIR/work/stub-state/mappings" -type f 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$LEFTOVER" "0" "no routing mapping survives cleanup"

# -----------------------------------------------------------------------------
name "a fatal setup error cannot print a green banner"
# -----------------------------------------------------------------------------
# die() exits without incrementing $FAILURES, and write_summary keys its verdict
# off $FAILURES — so a run that died in phase 0 once printed "eval passed: 0
# failures" while exiting 1. A green banner on a red run is the exact dishonesty
# this suite forbids.
run_eval fatal --fail-phase 0; OUT="$RUN_OUT"
if [ "$RUN_RC" -ne 0 ]; then
  ok "the fatal run exits non-zero (rc=$RUN_RC)"
else
  bad "a fatal setup error exited 0"
fi
assert_not_contains "$OUT" "eval passed" \
  "a run that died does NOT print the passing banner"
assert_contains "$OUT" "eval FAILED" "the banner reports the run as failed"
assert_contains "$OUT" "terminated early" \
  "the summary records WHY no verdict can be drawn"

# -----------------------------------------------------------------------------
name "--phases selects a subset (and cleanup still runs)"
# -----------------------------------------------------------------------------
run_eval subset --phases 0,8; OUT="$RUN_OUT"
TRACE="$(tr -d ' \n' < "$RUN_DIR/work/trace.log" 2>/dev/null)"
assert_eq "$TRACE" "phase:0phase:8phase:cleanup" \
  "only the requested phases run, cleanup still does"

# -----------------------------------------------------------------------------
name "--cleanup-only is standalone and idempotent"
# -----------------------------------------------------------------------------
run_eval cleanonly --cleanup-only; OUT="$RUN_OUT"
assert_contains "$OUT" "cleanup" "runs cleanup on its own"
assert_eq "$RUN_RC" "0" "exits 0 with nothing to clean up"
if env -i PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
     HOME="$RUN_DIR/home" ENVIRONMENT=dev AWS_REGION=us-east-1 \
     EVAL_WORKDIR="$RUN_DIR/work" bash "$EVAL_SCRIPT" --dry-run --cleanup-only \
     > "$RUN_DIR/cleanup2.log" 2>&1; then
  assert_contains "$(cat "$RUN_DIR/cleanup2.log")" "cleanup" \
    "a second --cleanup-only re-runs teardown and still exits 0"
else
  bad "a second --cleanup-only failed — teardown is not idempotent"
fi

# -----------------------------------------------------------------------------
name "the run is idempotent: two consecutive runs agree"
# -----------------------------------------------------------------------------
# The issue requires idempotence. Re-running must not accumulate state or change
# the verdict — a second run that differed would mean the first left residue.
run_eval idem1; RC1="$RUN_RC"; P1="$(grep -cE '^\[PASS\]' "$RUN_DIR/out.log" || echo 0)"
run_eval idem2; RC2="$RUN_RC"; P2="$(grep -cE '^\[PASS\]' "$RUN_DIR/out.log" || echo 0)"
assert_eq "$RC1" "$RC2" "two consecutive runs return the same exit code"
assert_eq "$P1" "$P2" "two consecutive runs make the same number of passing assertions"

# -----------------------------------------------------------------------------
name "authz cases use a positive control before counting a denial"
# -----------------------------------------------------------------------------
# The #4794 lesson, called out by the orchestrator: a plain member is denied by
# ANY authz check, so a member-only 403 proves nothing. The org_admin denial must
# be qualified by first proving that identity HAS authority on its own org.
run_eval authz --phases 0,1,8; OUT="$RUN_OUT"
assert_contains "$OUT" "control:" "a positive control runs before the denial is counted"
assert_contains "$OUT" "its authority is real" \
  "the control states why the subsequent denial is meaningful"
CTRL_LINE="$(grep -n 'control:' "$RUN_DIR/out.log" | head -1 | cut -d: -f1)"
DENY_LINE="$(grep -n 'the strong assertion' "$RUN_DIR/out.log" | head -1 | cut -d: -f1)"
if [ -n "$CTRL_LINE" ] && [ -n "$DENY_LINE" ] && [ "$CTRL_LINE" -lt "$DENY_LINE" ]; then
  ok "the positive control precedes the denial assertion"
else
  bad "the denial is asserted without a preceding positive control (ctrl=$CTRL_LINE deny=$DENY_LINE)"
fi
# The weak member-only assertion must be labelled as weak so nobody mistakes it
# for the load-bearing one.
assert_contains "$OUT" "weak" "the member-only denial is explicitly labelled weak"
# Every refusal must be followed by proof nothing was stored.
assert_contains "$OUT" "no rejected rule was stored" \
  "the 422 cases verify nothing was persisted, not just the status code"

# -----------------------------------------------------------------------------
name "no secret material is echoed to the log"
# -----------------------------------------------------------------------------
run_eval nosecrets; OUT="$RUN_OUT"
assert_not_contains "$OUT" "stub.access.token" "the access token is never printed"
assert_not_contains "$OUT" "stub.actor.token" "the actor token is never printed"
assert_not_contains "$OUT" "stub-external-id" "the destination ExternalId is never printed"
# The ExternalId is the shared secret protecting cross-account assume-role, so
# assert it is absent from the summary artifact too, not just stdout.
assert_not_contains "$(cat "$RUN_DIR/summary.md" 2>/dev/null)" "stub-external-id" \
  "the ExternalId does not leak into the job summary"

# -----------------------------------------------------------------------------
name "the job summary is machine-readable and complete"
# -----------------------------------------------------------------------------
# #4761 requires a machine-readable pass/fail summary. The triage agent reads
# this table, so its header and row count are part of the contract.
run_eval summary; OUT="$RUN_OUT"
SUMMARY="$RUN_DIR/summary.md"
if [ -f "$SUMMARY" ]; then
  SUM="$(cat "$SUMMARY")"
  assert_contains "$SUM" "| Phase | Result | Assertion |" "summary has the per-phase table header"
  assert_contains "$SUM" "**Failures:" "summary states the failure count"
  assert_contains "$SUM" "Findings (pinned current behaviour" \
    "findings are rendered separately, not as passes"
  ROWS="$(grep -c '^| [0-9]' "$SUMMARY" || echo 0)"
  if [ "$ROWS" -gt 25 ]; then
    ok "summary has $ROWS assertion rows"
  else
    bad "summary has only $ROWS assertion rows — the table is not being populated"
  fi
  # Skips must be visibly distinct from passes in the rendered table, or a reader
  # scanning it would count an unrun case as proven.
  assert_contains "$SUM" "skip" "skips are rendered distinctly in the table"
else
  bad "no \$GITHUB_STEP_SUMMARY was written"
fi
# A finding must not make the run red: the eval's job includes reporting what is
# true today, and a red run for known behaviour trains people to ignore it.
assert_eq "$RUN_RC" "0" "a run that emits findings still exits 0"

# -----------------------------------------------------------------------------
name "the workflow is dispatch-only and runs these tests"
# -----------------------------------------------------------------------------
WORKFLOW="$SCRIPT_DIR/../../../../.github/workflows/eval-bedrock-routing.yml"
if [ -f "$WORKFLOW" ]; then
  WF="$(cat "$WORKFLOW")"
  # Comment lines are stripped for the must-NOT-contain checks: against the raw
  # file they would match the header comment explaining why each thing is absent,
  # so the assertion would fail on its own documentation.
  WF_CODE="$(grep -v '^[[:space:]]*#' "$WORKFLOW")"
  # This eval authors routing rules in a shared dev account, so it must never
  # fire from an untrusted PR and never on a schedule nobody asked for.
  assert_not_contains "$WF_CODE" "schedule:" \
    "the workflow has no cron — a live run must be dispatched deliberately"
  assert_not_contains "$WF_CODE" "cron:" "no cron expression is configured"
  assert_contains "$WF" "workflow_dispatch:" "the workflow is dispatch-driven"
  # ARC scale-set runners have no Docker daemon, so a job-level container: cannot
  # start at all (#4171).
  assert_not_contains "$WF_CODE" "container:" \
    "the eval job does NOT use container: — ARC runners have no Docker daemon"
  assert_contains "$WF" "tests/test-run-eval-dry-run.sh" "the lint job runs this test suite"
  assert_contains "$WF" "platform/evals/lib/**" \
    "the PR trigger covers the shared harness, not just this eval"
  assert_contains "$WF" "--cleanup-only" \
    "the workflow always sweeps, so a cancelled run cannot leave a mapping behind"
else
  bad "could not find eval-bedrock-routing.yml at $WORKFLOW"
fi

# -----------------------------------------------------------------------------
name "mask() cannot contaminate a stdout return value"
# -----------------------------------------------------------------------------
# Regression test for the failure that cost eval run 34170126167 phases 1, 2 and
# 11. mask() emitted '::add-mask::<secret>' on STDOUT, and its callers are
# functions whose stdout is a RETURN VALUE read through command substitution —
# mint_actor_token() masks the token it minted and echoes the actor's org_id. So
# the caller received "::add-mask::<token>\n<org_id>", interpolated that into
# request URLs, and curl rejected them locally without writing its output file.
#
# The dry-run stub replaces mint_actor_token and never calls mask, so no
# end-to-end dry run can reach this bug; and mask() is inert unless
# GITHUB_ACTIONS=true, so it is live-only. This test therefore exercises the real
# lib/log.sh directly, with GITHUB_ACTIONS set — the only conditions under which
# the defect appears.
MASK_PROBE="$TEST_ROOT/mask-probe.sh"
cat > "$MASK_PROBE" <<'PROBE'
set -uo pipefail
RESULTS_FILE="$(mktemp)"; export RESULTS_FILE
. "$LIB_UNDER_TEST/log.sh"
# Mirror mint_actor_token's shape: mask a secret, then return a value on stdout.
returns_a_value() { mask "super-secret-token"; printf '%s' "adp-dev-pentest-org-a"; }
CAPTURED="$(returns_a_value)"
printf 'CAPTURED=[%s]\n' "$CAPTURED"
PROBE

MASK_OUT="$(env -i \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  HOME="$TEST_ROOT" \
  GITHUB_ACTIONS=true \
  LIB_UNDER_TEST="$LIB_DIR" \
  bash "$MASK_PROBE" 2>/dev/null)"

# The captured value must be EXACTLY the org id — no directive, no newline.
assert_eq "$MASK_OUT" "CAPTURED=[adp-dev-pentest-org-a]" \
  "a mask() caller's captured stdout is exactly its return value"
assert_not_contains "$MASK_OUT" "add-mask" \
  "the masking directive never appears in the captured return value"

# ...and the directive must still be EMITTED (on stderr), or secrets stop being
# masked in the Actions log — a fix that simply deleted the echo would pass the
# assertions above while silently unmasking every token.
MASK_ERR="$(env -i \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  HOME="$TEST_ROOT" \
  GITHUB_ACTIONS=true \
  LIB_UNDER_TEST="$LIB_DIR" \
  bash "$MASK_PROBE" 2>&1 >/dev/null)"
assert_contains "$MASK_ERR" "::add-mask::super-secret-token" \
  "the secret is still masked, on stderr, so the Actions log stays redacted"

# -----------------------------------------------------------------------------
name "a status capture is always three digits"
# -----------------------------------------------------------------------------
# The same run reported 'GET /api/usage/logs -> 000000' and 'budget
# person-default surface -> 000000'. A status is three digits; six meant curl's
# own -w '000' had been concatenated with a '|| echo 000' fallback. That value
# matched no arm of the retry case statement, so it bypassed the retry policy
# entirely AND was printed verbatim as though the gateway had answered it.
#
# Scoped to api() — the function that actually produced the six-digit status.
# The `|| echo "000"` idiom appears at other call sites in this eval and in
# lib/http.sh; those are pre-existing and cannot produce a malformed value now
# that mask() no longer contaminates the interpolated variables, so they are
# deliberately left alone rather than swept up here.
API_FN="$(awk '/^api\(\) \{/,/^\}/' "$EVAL_SCRIPT")"
assert_not_contains "$API_FN" '|| echo "000")"' \
  "api() does not append a redundant 000 to curl's own -w output"
if grep -q '\[0-9\]\[0-9\]\[0-9\]) ;;' "$EVAL_SCRIPT"; then
  ok "api() normalises any non-three-digit capture to 000 so the retry policy sees it"
else
  bad "api() has no three-digit normalisation — a malformed capture can bypass the retry policy"
fi

# -----------------------------------------------------------------------------
name "the shared harness is sourced, not re-implemented"
# -----------------------------------------------------------------------------
# One clean-room boundary across all evals means a hardening fix lands in every
# suite. A local copy here would silently fork it and could be weaker.
EVAL_SRC="$(cat "$EVAL_SCRIPT")"
assert_contains "$EVAL_SRC" 'EVAL_LIB_DIR' "the eval resolves the shared lib directory"
for libfile in log.sh state.sh aws.sh clean-room.sh http.sh pod.sh cognito.sh; do
  assert_contains "$EVAL_SRC" "\$EVAL_LIB_DIR/$libfile" "sources lib/$libfile"
done
for fn in 'laptop()' 'h_kubectl()' 'assert_clean_room()' 'write_summary()' 'pass()' 'fail()'; do
  if grep -q "^${fn} {" "$EVAL_SCRIPT"; then
    bad "${fn} is redefined at top level in run-eval.sh — the shared boundary is forked"
  else
    ok "${fn} comes from the shared lib, not a local copy"
  fi
done
if [ -f "$LIB_DIR/pod.sh" ]; then
  ok "the shared lib is where this eval expects it ($LIB_DIR)"
else
  bad "no shared lib at $LIB_DIR"
fi

# -----------------------------------------------------------------------------
echo ""
echo "═══════════════════════════════════════"
if [ "$TESTS_FAILED" -eq 0 ]; then
  echo "${GREEN}All $TESTS_RUN test groups passed.${NC}"
  exit 0
fi
echo "${RED}$TESTS_FAILED assertion(s) failed across $TESTS_RUN test groups.${NC}"
exit 1

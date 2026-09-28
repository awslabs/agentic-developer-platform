#!/usr/bin/env bash
#
# =============================================================================
# test-run-eval-dry-run.sh — tests for the budget/rate-limit eval harness itself
# =============================================================================
# Issue #4163. Runs in CI on every PR that touches the eval or the shared
# harness, with no AWS, no cluster and no network: run-eval.sh --dry-run stubs
# every external CLI.
#
# WHAT THEY PROTECT
# An eval is only worth what its harness guarantees, and the guarantees that
# matter here are silent when broken:
#
#   1. The tag guard refuses an untagged admin write. This eval WRITES budget and
#      rate-limit config through the real admin API against a shared dev account.
#      If the guard stopped firing, a bug in id construction would not fail the
#      run — it would silently cap a real tenant's spend or throttle their
#      traffic. This is the single most dangerous failure mode in the file, so it
#      is tested first and directly.
#   2. Cleanup runs even when a case explodes. A crashed run must not leave a
#      $1 cap or an rpm=1 limit behind in dev.
#   3. The clean-room detector actually trips. If it stopped detecting
#      contamination, every future run would report green from inside the
#      agent-worker image — whose baked-in sigv4-proxy makes requests
#      authenticate as an AGENT, i.e. against a different budget entity than the
#      seeded human, so EVERY cascading-cap case would be false green.
#   4. Every laptop command really executes in the pod. A laptop call that ran on
#      the runner would ride its IRSA and be enforced against the wrong entity.
#   5. --inject-failure genuinely goes red. The dry-run stubs DERIVE each verdict
#      from the config the eval wrote, rather than being handed the expected
#      answer per case, which is what makes this discriminating: a stub told the
#      answer could not fail, and the acceptance check would be worthless.
#   6. The findings are still reported. Four of the twelve cases answer with a
#      pinned FINDING rather than a pass (see the eval header). If those silently
#      stopped being emitted, the eval would look cleaner while proving less.
#
# Mirrors platform/evals/cli-onboarding/tests/test-run-eval-dry-run.sh, which is
# the proven pattern (#4157) and the regression guard for the shared lib.
#
# Usage: ./platform/evals/budget-ratelimit/tests/test-run-eval-dry-run.sh
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

# These tests necessarily run in the agent/dev container, which is itself
# contaminated by the eval's own definition: `codex` is on PATH and something may
# be listening on the proxy port. Two narrow hatches let the tests exercise
# everything else; each is still asserted directly in its own case below, so
# bypassing them here cannot hide a broken detector. The workflow sets NEITHER,
# and a test asserts that.
FREE_PORT=19388
UNCONTAMINATE=(EVAL_SKIP_CLI_PATH_CHECK=true "EVAL_FORBIDDEN_PORTS=$FREE_PORT")

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

# A fresh workdir + a fresh fake HOME per invocation, so tests cannot pollute
# each other and the clean-room checks see a genuinely empty home.
new_env() {
  local n="$1"
  RUN_DIR="$TEST_ROOT/$n"
  mkdir -p "$RUN_DIR/work" "$RUN_DIR/home"
}

# run_eval <name> [args...] — invoke the eval with an isolated HOME/workdir.
# Sets RUN_OUT (combined output), RUN_RC and RUN_DIR. Deliberately NOT called in
# a $( ) substitution: that would run it in a subshell and RUN_RC/RUN_DIR would
# never reach the caller, silently disabling every exit-code assertion.
run_eval() {
  local n="$1"; shift
  new_env "$n"
  env -i \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    HOME="$RUN_DIR/home" \
    "${UNCONTAMINATE[@]}" \
    ENVIRONMENT=dev \
    AWS_REGION=us-east-1 \
    ADP_DB_USER=eval_test_user \
    EVAL_RUN_ID="test-$n" \
    EVAL_WORKDIR="$RUN_DIR/work" \
    EVAL_STUB_KUBECTL_LOG="$RUN_DIR/kubectl.log" \
    GITHUB_STEP_SUMMARY="$RUN_DIR/summary.md" \
    bash "$EVAL_SCRIPT" --dry-run "$@" > "$RUN_DIR/out.log" 2>&1
  RUN_RC=$?
  RUN_OUT="$(cat "$RUN_DIR/out.log")"
}

echo "═══ Budget/rate-limit eval harness tests ═══"
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
name "the tag guard refuses an admin write that escaped the run tag"
# -----------------------------------------------------------------------------
# THE most important test in this file. Every entity id this eval writes must
# carry eval-bgt-<run_id>; an untagged write against shared dev would cap or
# throttle a real tenant. The guard must DIE, not record a failure and continue.
#
# Tested by extracting assert_tagged() and driving it directly, so the test
# exercises the real function rather than a re-description of it. `die` is
# stubbed to exit non-zero, which is the contract the guard relies on.
GUARD_FN="$(sed -n '/^assert_tagged() {/,/^}/p' "$EVAL_SCRIPT")"
if [ -z "$GUARD_FN" ]; then
  bad "could not extract assert_tagged() from run-eval.sh — was it renamed?"
else
  GUARD_OUT="$(env -i PATH="$PATH" bash -c "
    set -uo pipefail
    EVAL_TAG='eval-bgt-test'
    die() { echo \"DIED: \$*\"; exit 9; }
    $GUARD_FN
    assert_tagged 'budget entity' 'eval-bgt-test-t1' && echo 'TAGGED-OK'
    assert_tagged 'budget entity' 'acme-corp-prod-team'
    echo 'REACHED-AFTER-UNTAGGED'
  " 2>&1)"
  GUARD_RC=$?
  assert_contains "$GUARD_OUT" "TAGGED-OK" "a correctly-tagged entity id passes the guard"
  assert_contains "$GUARD_OUT" "DIED:" "an untagged entity id makes the guard die"
  assert_contains "$GUARD_OUT" "acme-corp-prod-team" "the refusal names the offending value"
  assert_not_contains "$GUARD_OUT" "REACHED-AFTER-UNTAGGED" \
    "the guard aborts rather than continuing past an untagged write"
  assert_eq "$GUARD_RC" "9" "the guard exits non-zero (it dies, it does not just log)"
fi

# Every admin write in the real run must be guarded. Asserted structurally: each
# call to set_budget/set_ratelimit ultimately reaches assert_tagged, so the two
# writer functions are required to call it. A new writer that forgot to would be
# the exact regression this catches.
for writer in set_budget set_ratelimit; do
  FN="$(sed -n "/^${writer}() {/,/^}/p" "$EVAL_SCRIPT")"
  if [ -z "$FN" ]; then
    bad "could not extract ${writer}() — was it renamed?"
  else
    assert_contains "$FN" "assert_tagged" "${writer}() asserts the run tag before writing"
  fi
done

# -----------------------------------------------------------------------------
name "clean room: passes in a pristine environment"
# -----------------------------------------------------------------------------
run_eval clean-pass --assert-clean-room; OUT="$RUN_OUT"
assert_eq "$RUN_RC" "0" "exit 0 in a pristine HOME"
assert_contains "$OUT" "clean room verified" "reports the clean room verified"

# -----------------------------------------------------------------------------
name "clean room: trips on each contaminant independently"
# -----------------------------------------------------------------------------
# Each contaminant is tested ALONE: a detector that only fires on a combination
# would pass a partially-contaminated container, which is the realistic case.
for contaminant in .codex .claude .bedrock-gateway .claude.json; do
  new_env "dirty-$contaminant"
  if [ "$contaminant" = ".claude.json" ]; then
    touch "$RUN_DIR/home/$contaminant"
  else
    mkdir -p "$RUN_DIR/home/$contaminant"
  fi
  OUT="$(env -i PATH="$PATH" HOME="$RUN_DIR/home" EVAL_WORKDIR="$RUN_DIR/work" \
    "${UNCONTAMINATE[@]}" \
    bash "$EVAL_SCRIPT" --dry-run --assert-clean-room 2>&1)"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    assert_contains "$OUT" "$contaminant" "detects ~/$contaminant (exit $RC)"
  else
    bad "did NOT detect ~/$contaminant — a contaminated container would run the matrix"
  fi
done

# For THIS eval these env vars are not just "a CLI might be redirected": a
# request that goes out through the agent-worker's sigv4-proxy authenticates as
# an agent, so it is enforced against a different budget entity than the seeded
# human and every cascading-cap case passes for the wrong reason.
for var in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ADP_GATEWAY_URL CLAUDE_CODE_USE_BEDROCK; do
  new_env "dirtyvar-$var"
  OUT="$(env -i PATH="$PATH" HOME="$RUN_DIR/home" EVAL_WORKDIR="$RUN_DIR/work" \
    "${UNCONTAMINATE[@]}" \
    "$var=contaminated" bash "$EVAL_SCRIPT" --dry-run --assert-clean-room 2>&1)"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    assert_contains "$OUT" "$var" "detects \$$var (exit $RC)"
  else
    bad "did NOT detect \$$var — traffic could be billed to an agent identity instead"
  fi
done

# Note this case does NOT use $UNCONTAMINATE — it is the check being tested.
new_env "dirty-path"
mkdir -p "$RUN_DIR/fakebin"
printf '#!/bin/sh\nexit 0\n' > "$RUN_DIR/fakebin/claude"
chmod +x "$RUN_DIR/fakebin/claude"
OUT="$(env -i PATH="$RUN_DIR/fakebin:$PATH" HOME="$RUN_DIR/home" EVAL_WORKDIR="$RUN_DIR/work" \
  "EVAL_FORBIDDEN_PORTS=$FREE_PORT" \
  bash "$EVAL_SCRIPT" --dry-run --assert-clean-room 2>&1)"
RC=$?
if [ "$RC" -ne 0 ]; then
  assert_contains "$OUT" "'claude' is already on PATH" "detects a pre-installed 'claude' on PATH (exit $RC)"
else
  bad "did NOT detect a pre-installed claude on PATH"
fi

# A listener on the proxy port means platform-internal auth is reachable from
# the pod. Exercised for real by binding $FREE_PORT and pointing the check at it.
new_env "dirty-port"
python3 -c "
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(4)
time.sleep(60)
" "$FREE_PORT" >/dev/null 2>&1 &
LISTENER=$!
sleep 1
OUT="$(env -i PATH="$PATH" HOME="$RUN_DIR/home" EVAL_WORKDIR="$RUN_DIR/work" \
  EVAL_SKIP_CLI_PATH_CHECK=true "EVAL_FORBIDDEN_PORTS=$FREE_PORT" \
  bash "$EVAL_SCRIPT" --dry-run --assert-clean-room 2>&1)"
RC=$?
kill "$LISTENER" 2>/dev/null || true
wait "$LISTENER" 2>/dev/null || true
if [ "$RC" -ne 0 ]; then
  assert_contains "$OUT" "listening on 127.0.0.1:${FREE_PORT}" "detects a listener on the proxy port (exit $RC)"
else
  bad "did NOT detect a listener on the proxy port — traffic could be answered by another proxy"
fi

# -----------------------------------------------------------------------------
name "the workflow does not use the test-only escape hatches"
# -----------------------------------------------------------------------------
WORKFLOW="$SCRIPT_DIR/../../../../.github/workflows/eval-budget-ratelimit.yml"
if [ -f "$WORKFLOW" ]; then
  WF="$(cat "$WORKFLOW")"
  # The must-NOT-contain checks below read the workflow's CODE, with whole-line
  # comments stripped. Checked against the raw file they would match the header
  # comment that explains why each thing is absent — the assertion would fail on
  # the documentation of the property it is asserting, which is both confusing
  # and a standing incentive to delete the explanation.
  WF_CODE="$(grep -v '^[[:space:]]*#' "$WORKFLOW")"
  for hatch in EVAL_SKIP_CLI_PATH_CHECK EVAL_FORBIDDEN_PORTS; do
    assert_not_contains "$WF_CODE" "$hatch" "eval-budget-ratelimit.yml never sets \$$hatch"
  done
  # The ARC scale set has no Docker daemon, so a job-level `container:` cannot
  # start at all (#4171). The clean room is a pod the harness spawns instead.
  assert_not_contains "$WF_CODE" "container:" \
    "the eval job does NOT use container: — ARC runners have no Docker daemon"
  # This eval writes budget and rate-limit config to a shared dev account, so it
  # must never fire from an untrusted PR. Its schedule belongs to the combined
  # nightly parent; standalone dispatch remains available for diagnosis.
  assert_not_contains "$WF_CODE" "schedule:" \
    "the reusable child has no cron — the nightly parent owns scheduling"
  assert_not_contains "$WF_CODE" "cron:" "no cron expression is configured"
  assert_contains "$WF" "workflow_dispatch:" "the workflow is dispatch-driven"
  assert_contains "$WF" "eval-pod leftover check" \
    "the workflow checks for leaked clean-room pods"
  # The dry-run tests are only a gate if the lint job actually runs them, and the
  # shared lib is only guarded if the trigger covers it.
  assert_contains "$WF" "platform/evals/lib/**" \
    "the PR trigger covers the shared harness, not just this eval"
  assert_contains "$WF" "tests/test-run-eval-dry-run.sh" \
    "the lint job runs this test suite"
else
  bad "could not find eval-budget-ratelimit.yml at $WORKFLOW"
fi

# -----------------------------------------------------------------------------
name "phases run in order, and cleanup always closes the trace"
# -----------------------------------------------------------------------------
run_eval order; OUT="$RUN_OUT"
TRACE_FILE="$RUN_DIR/work/trace.log"
if [ -f "$TRACE_FILE" ]; then
  TRACE="$(tr -d ' \n' < "$TRACE_FILE")"
  assert_eq "$TRACE" \
    "phase:configphase:seedphase:1phase:2phase:3phase:4phase:5phase:6phase:7phase:8phase:9phase:10phase:11phase:12phase:Hphase:cleanup" \
    "trace is config, seed, cases 1-12, H, cleanup — in that order"
else
  bad "no trace file was written"
fi

# -----------------------------------------------------------------------------
name "--phases selects a subset (and cleanup still runs)"
# -----------------------------------------------------------------------------
run_eval subset --phases 1,8; OUT="$RUN_OUT"
TRACE="$(tr -d ' \n' < "$RUN_DIR/work/trace.log" 2>/dev/null)"
assert_eq "$TRACE" "phase:configphase:seedphase:1phase:8phase:cleanup" \
  "only the requested cases run, cleanup still does"

# -----------------------------------------------------------------------------
name "cleanup runs when a case explodes mid-run (--fail-phase 3)"
# -----------------------------------------------------------------------------
# The guarantee that a crashed run does not leave a $1 cap in dev. Cases 1-2
# have already WRITTEN budget config by the time 3 dies, so this is the real
# scenario, not a no-op teardown.
run_eval failmid --fail-phase 3; OUT="$RUN_OUT"
assert_contains "$OUT" "--fail-phase 3" "the injected failure fired"
assert_contains "$OUT" "Cleanup" "cleanup ran despite the mid-run death"
assert_contains "$OUT" "removed budget and rate-limit configs" \
  "the configs written before the death were removed"
assert_contains "$OUT" "deleted Cognito user" "the seeded identities were deleted"
assert_contains "$OUT" "swept tag-scoped rows" "the tag-scoped DB sweep ran"
if [ "$RUN_RC" -ne 0 ]; then
  ok "exits non-zero after an injected failure (rc=$RUN_RC)"
else
  bad "exited 0 after an injected failure — CI would report green"
fi

# Every config the run created must be deleted, and the deletes must be
# tag-scoped. Read from the stub's own state directory: an entry left behind
# there is a config that would have been left behind in dev.
LEFTOVER=$(find "$RUN_DIR/work/stub-state/budgets" "$RUN_DIR/work/stub-state/ratelimits" \
  -type f 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$LEFTOVER" "0" "no budget or rate-limit config survives cleanup"

# -----------------------------------------------------------------------------
name "traffic is driven from the pod, never from the runner"
# -----------------------------------------------------------------------------
# For this eval the property is load-bearing in a specific way: a request issued
# on the RUNNER would carry the runner's IRSA and be enforced against a
# different budget entity than the seeded human, so a cap case would pass while
# testing nothing.
run_eval podroute --phases 1; OUT="$RUN_OUT"
KLOG="$RUN_DIR/kubectl.log"
if [ ! -f "$KLOG" ]; then
  bad "no kubectl invocations were logged — the eval never reached the pod"
else
  EXECS="$(grep -c '^exec ' "$KLOG" || echo 0)"
  if [ "$EXECS" -gt 0 ]; then
    ok "the run routed $EXECS command(s) through kubectl exec"
  else
    bad "no 'kubectl exec' was issued — the traffic ran on the runner"
  fi
  STRAY="$(grep '^exec ' "$KLOG" | grep -vc 'eval-bgt' || true)"
  assert_eq "$STRAY" "0" "every exec targets the clean-room pod"
  # Every exec carrying the laptop env (i.e. that came through laptop()) must
  # carry -i: stdin is the only channel a secret may travel on, because anything
  # in an exec'd argv is visible in the exec API and the runner's process table.
  NO_STDIN="$(grep '^exec ' "$KLOG" | grep 'AWS_EC2_METADATA_DISABLED' | grep -vc -- '^exec -i ' || true)"
  assert_eq "$NO_STDIN" "0" "every laptop() exec passes -i (stdin stays the secret channel)"
  assert_contains "$(grep '^exec ' "$KLOG" | head -1)" "AWS_EC2_METADATA_DISABLED=true" \
    "the pod env disables IMDS"
fi

# The org-admin token is the one credential that must NOT enter the clean room:
# the admin writes are harness actions, and an org-admin token in the pod would
# let the emulated laptop raise its own cap — defeating the whole eval.
assert_not_contains "$(grep '^exec ' "$KLOG" 2>/dev/null || true)" "A1.curlrc" \
  "the org-admin credential is never used inside the pod"
if [ -f "$RUN_DIR/work/A1.curlrc" ]; then
  ok "the org-admin curl config stays on the harness side"
else
  bad "no A1 curl config was written — the admin writes did not authenticate as a1"
fi

# -----------------------------------------------------------------------------
name "the clean-room pod is created hardened, and the gate runs inside it"
# -----------------------------------------------------------------------------
RUN_CMD="$(grep '^run ' "$KLOG" 2>/dev/null | head -1)"
if [ -z "$RUN_CMD" ]; then
  bad "no 'kubectl run' was logged — the clean room was never created"
else
  assert_contains "$RUN_CMD" '"automountServiceAccountToken":false' \
    "the pod gets no service-account token"
  assert_contains "$RUN_CMD" '"enableServiceLinks":false' \
    "the pod gets no service-link env for in-cluster services"
  assert_contains "$RUN_CMD" '"app":"eval-bgt"' "the pod is labelled for the leftover sweep"
  assert_contains "$RUN_CMD" '"karpenter.sh/do-not-disrupt"' \
    "the pod is annotated do-not-disrupt so consolidation cannot kill it mid-run"
  assert_contains "$RUN_CMD" 'command -- sleep' \
    "the pod sleeps rather than running the image entrypoint and exiting"
fi
assert_contains "$OUT" "clean room is a fresh pod" "the clean-room gate ran inside the pod"
GATE_EXEC="$(grep -n '^exec ' "$KLOG" 2>/dev/null | grep 'assert-clean-room' | head -1)"
if [ -n "$GATE_EXEC" ]; then
  ok "--assert-clean-room is exec'd in the pod (kubectl log line ${GATE_EXEC%%:*})"
else
  bad "--assert-clean-room was never exec'd in the pod — the clean room is unverified"
fi
# The gate must run before the first admin write, or a contaminated run would
# already have mutated dev by the time it aborts.
GATE_LINE="$(grep -n 'assert-clean-room' "$KLOG" 2>/dev/null | head -1 | cut -d: -f1)"
WRITE_LINE="$(grep -n 'budgets' "$KLOG" 2>/dev/null | head -1 | cut -d: -f1)"
if [ -n "$GATE_LINE" ] && { [ -z "$WRITE_LINE" ] || [ "$GATE_LINE" -lt "$WRITE_LINE" ]; }; then
  ok "the gate runs before any admin write, so a contaminated run changes nothing in dev"
else
  bad "an admin write was issued before the clean-room gate passed"
fi

# -----------------------------------------------------------------------------
name "the clean-room pod is deleted, including after a failure"
# -----------------------------------------------------------------------------
# A pod outlives the job that made it, so a leak here is a leaked node. Deletion
# is by LABEL, not name: a pod orphaned by a crashed run is swept by a later run
# that never learned the old run id.
for scenario in "" "--fail-phase 3"; do
  label="${scenario:-full run}"
  # shellcheck disable=SC2086  # deliberate word-split of the scenario flags
  run_eval "poddel-${scenario// /-}" $scenario; OUT="$RUN_OUT"
  DEL="$(grep '^delete .*pod' "$RUN_DIR/kubectl.log" 2>/dev/null | tail -1)"
  if [ -z "$DEL" ]; then
    bad "no pod deletion was logged ($label) — the clean room leaked"
  else
    assert_contains "$DEL" "-l app=eval-bgt" "the pod is swept by label ($label)"
    assert_contains "$DEL" "--ignore-not-found" \
      "the sweep tolerates an already-gone pod, so cleanup stays idempotent ($label)"
  fi
done

# --cleanup-only must sweep pods too: it is what an operator runs after a killed
# run, when no state file records the pod's name.
run_eval podsweep --cleanup-only; OUT="$RUN_OUT"
assert_contains "$(grep '^delete .*pod' "$RUN_DIR/kubectl.log" 2>/dev/null || true)" \
  "-l app=eval-bgt" "--cleanup-only sweeps leaked pods by label"

# -----------------------------------------------------------------------------
name "--inject-failure wrong-entity makes the run fail loudly"
# -----------------------------------------------------------------------------
# THE acceptance check on the eval itself. The dry-run stubs re-implement the
# real cascade and the real bucket arithmetic rather than being handed the
# expected answer per case, so when the injection flips case 1 to expect
# entity_type=team the stub still computes 'user' and the two genuinely
# disagree. If this test ever passes clean, the eval proves nothing.
run_eval inject --phases 1,8 --inject-failure wrong-entity; OUT="$RUN_OUT"
assert_contains "$OUT" "expected to FAIL" "announces that the run is deliberately broken"
assert_contains "$OUT" "case 1" "the failure is attributed to a named case"
if [ "$RUN_RC" -ne 0 ]; then
  ok "the deliberately-broken run exits non-zero (rc=$RUN_RC)"
else
  bad "the deliberately-broken run PASSED — the eval is not asserting anything"
fi
assert_contains "$(cat "$RUN_DIR/summary.md" 2>/dev/null)" "EXPECTED to fail" \
  "the job summary flags the injected failure"
# Cleanup is not optional on the acceptance run either.
assert_contains "$OUT" "swept tag-scoped rows" "the broken run still cleans up after itself"

# -----------------------------------------------------------------------------
name "the same cases WITHOUT the injection pass clean"
# -----------------------------------------------------------------------------
# The control for the case above: identical cases, no injection, zero failures.
# Together they show the eval discriminates rather than always-passing or
# always-failing.
run_eval control --phases 1,8; OUT="$RUN_OUT"
assert_eq "$RUN_RC" "0" "the un-injected control run exits 0"
assert_contains "$OUT" "eval passed: 0 failures" "control run records no failures"

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
name "both wire paths are exercised by every enforcement case"
# -----------------------------------------------------------------------------
# #4163 requires Claude Code (/v1/messages, Anthropic format) AND Codex
# (/openai/v1/responses). A case that silently stopped driving one wire would
# leave half the surface untested while still reporting a pass.
run_eval wires --phases 1,2,3,4,5,6; OUT="$RUN_OUT"
for wire in claude codex; do
  N="$(grep -c "\[${wire}\]" <<< "$OUT" || echo 0)"
  if [ "$N" -ge 6 ]; then
    ok "the ${wire} wire is asserted in $N places"
  else
    bad "the ${wire} wire appears in only $N assertion(s) — a wire path stopped being driven"
  fi
done
# And the two wires must actually hit different URLs, not the same one twice.
assert_contains "$(cat "$KLOG" 2>/dev/null || true)" "v1/messages" \
  "the Anthropic wire posts to /v1/messages"
run_eval wireurls --phases 1; OUT="$RUN_OUT"
URLS="$(grep -o 'v1/messages\|openai/v1/responses' "$RUN_DIR/work/req.json" \
  "$RUN_DIR/kubectl.log" 2>/dev/null | sort -u | wc -l | tr -d ' ')"
if [ "$URLS" -ge 2 ]; then
  ok "the two wires target two distinct endpoints"
else
  bad "only $URLS distinct inference endpoint(s) were used — the wires are not really split"
fi

# -----------------------------------------------------------------------------
name "the findings are reported rather than silently dropped"
# -----------------------------------------------------------------------------
# Preserve real coverage gaps without pinning obsolete implementation defects.
run_eval findings; OUT="$RUN_OUT"
assert_contains "$OUT" "agent-triggered budget exhaustion is NOT TESTED" \
  "Phase H states its observational coverage limit"
assert_not_contains "$OUT" "does NOT land under the triggering human's budget" \
  "historical direct billing cannot disprove root_user accounting"
assert_contains "$OUT" "TPM rate limiting is effectively unenforceable" \
  "case 9 pins the TPM finding"
assert_contains "$OUT" "case 11 org RPM: HTTP 429" \
  "case 11 requires the now-supported org rate limit"
assert_contains "$OUT" "settled organization usage uses the enforceable 'org' ledger key" \
  "case 7 verifies the current org ledger key"
SUMMARY="$RUN_DIR/summary.md"
if [ -f "$SUMMARY" ]; then
  assert_contains "$(cat "$SUMMARY")" "| Phase | Result | Assertion |" \
    "summary has the per-phase table header"
  assert_contains "$(cat "$SUMMARY")" "Findings (pinned current behaviour" \
    "findings are rendered in their own summary block, not as passes"
  ROWS="$(grep -c '^| [0-9cHs]' "$SUMMARY" || echo 0)"
  if [ "$ROWS" -gt 20 ]; then
    ok "summary has $ROWS assertion rows"
  else
    bad "summary has only $ROWS assertion rows — the table is not being populated"
  fi
else
  bad "no \$GITHUB_STEP_SUMMARY was written"
fi
# A finding must NOT be counted as a failure: the eval's job is to report what
# is true today, and a red run for known behaviour would train people to ignore
# it. Four findings and still exit 0 is the property.
assert_eq "$RUN_RC" "0" "a run that emits findings still exits 0"
if [ "$RUN_RC" -ne 0 ]; then
  # Dry-run output contains only test fixtures. Preserve the failed assertion's
  # context in CI instead of reporting an unexplained exit code.
  printf '%s\n' "$OUT"
fi

# -----------------------------------------------------------------------------
name "--cleanup-only is standalone and idempotent"
# -----------------------------------------------------------------------------
run_eval cleanonly --cleanup-only; OUT="$RUN_OUT"
assert_contains "$OUT" "Cleanup" "runs cleanup on its own"
assert_eq "$RUN_RC" "0" "exits 0 with nothing to clean up"
if env -i PATH="$PATH" HOME="$RUN_DIR/home" ENVIRONMENT=dev AWS_REGION=us-east-1 ADP_DB_USER=eval_test_user \
     EVAL_WORKDIR="$RUN_DIR/work" bash "$EVAL_SCRIPT" --dry-run --cleanup-only \
     > "$RUN_DIR/cleanup2.log" 2>&1; then
  assert_contains "$(cat "$RUN_DIR/cleanup2.log")" "Cleanup" \
    "a second --cleanup-only re-runs teardown and still exits 0"
else
  bad "a second --cleanup-only failed (rc=$?) — teardown is not idempotent"
fi
# The standalone sweep must be tag-scoped: it runs with no state file, so a
# broad DELETE here would hit real tenants' config.
assert_contains "$(cat "$RUN_DIR/cleanup2.log")" "swept tag-scoped rows for eval-bgt-" \
  "the stateless sweep is scoped to the eval's tag prefix"

# -----------------------------------------------------------------------------
name "no secret material is echoed to the log"
# -----------------------------------------------------------------------------
run_eval nosecrets; OUT="$RUN_OUT"
assert_not_contains "$OUT" "stub.access.token" "the access token is never printed"
assert_not_contains "$OUT" "stub-refresh-token" "the refresh token is never printed"
assert_not_contains "$OUT" "stubpassword" "the DB password is never printed"
assert_not_contains "$OUT" "stub-iam-auth-token" "the RDS IAM auth token is never printed"

# -----------------------------------------------------------------------------
name "the shared harness is sourced, not re-implemented"
# -----------------------------------------------------------------------------
# #4163's premise: both evals use ONE clean-room boundary, so a hardening fix
# lands in both. A local copy of laptop() here would silently fork the boundary
# and could be weaker than the one cli-onboarding's tests guard.
EVAL_SRC="$(cat "$EVAL_SCRIPT")"
assert_contains "$EVAL_SRC" 'EVAL_LIB_DIR' "the eval resolves the shared lib directory"
for libfile in log.sh state.sh aws.sh clean-room.sh http.sh pod.sh cognito.sh; do
  assert_contains "$EVAL_SRC" "\$EVAL_LIB_DIR/$libfile" "sources lib/$libfile"
done
# The boundary functions must come from the lib, not be redefined here.
for fn in 'laptop()' 'h_kubectl()' 'assert_clean_room()'; do
  if grep -q "^${fn} {" "$EVAL_SCRIPT"; then
    bad "${fn} is redefined in run-eval.sh — the clean-room boundary is forked"
  else
    ok "${fn} comes from the shared lib, not a local copy"
  fi
done
if [ -f "$LIB_DIR/pod.sh" ]; then
  ok "the shared lib is where this eval expects it ($LIB_DIR)"
else
  bad "no shared lib at $LIB_DIR — the refactor is incomplete"
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

#!/usr/bin/env bash
#
# =============================================================================
# test-run-eval-dry-run.sh — tests for the CLI-onboarding eval harness itself
# =============================================================================
# Issue #4157. These tests run in CI on every PR that touches the eval, with no
# AWS, no cluster and no network: run-eval.sh --dry-run stubs every external CLI.
#
# WHAT THEY PROTECT
# An eval is only worth what its harness guarantees. Four of those guarantees
# are load-bearing and silent when broken, so each gets a test:
#
#   1. The clean-room detector actually trips. If it stopped detecting
#      contamination, every future run would report green from inside the
#      agent-worker image while proving nothing.
#   2. Cleanup runs even when a phase explodes. If it stopped, a crashed run
#      would leave the dev gateway with the approval gate flipped.
#   3. The flag is restored to the value READ AT START, not a hardcoded false.
#      Hardcoding false silently disables the gate in an env that had it on.
#   4. laptop() really executes in the clean-room pod. If it stopped — if a
#      laptop command ever ran bare on the runner — the laptop phase would ride
#      the runner's IRSA and the eval would pass on the wrong auth.
#   5. The clean-room pod is deleted in phase D, on every exit path. A leaked
#      pod is a leaked node, and the next run's label sweep is what catches it.
#
# Follows the mock-CLI pattern of platform/scripts/tests/test-flip-gate-check.sh.
#
# Usage: ./platform/evals/cli-onboarding/tests/test-run-eval-dry-run.sh
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_SCRIPT="$SCRIPT_DIR/../run-eval.sh"

TESTS_RUN=0
TESTS_FAILED=0
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'

# These tests necessarily run in the agent/dev container, which is itself
# contaminated by the eval's definition: `codex` is on PATH and something may be
# listening on 9090. Two narrow escape hatches let the tests exercise everything
# else; each is still asserted directly in its own case below, so bypassing them
# here cannot hide a broken detector.
#   EVAL_SKIP_CLI_PATH_CHECK — skip the "claude/codex on PATH" check
#   EVAL_FORBIDDEN_PORTS     — check a known-free port instead of 9191/9090
# The workflow sets NEITHER. A test asserts that.
FREE_PORT=19387
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
# never reach the caller, which silently broke every exit-code assertion.
run_eval() {
  local n="$1"; shift
  new_env "$n"
  env -i \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    HOME="$RUN_DIR/home" \
    "${UNCONTAMINATE[@]}" \
    ENVIRONMENT=dev \
    AWS_REGION=us-east-1 \
    EVAL_RUN_ID="test-$n" \
    EVAL_WORKDIR="$RUN_DIR/work" \
    EVAL_STUB_KUBECTL_LOG="$RUN_DIR/kubectl.log" \
    EVAL_STUB_FLAG="${STUB_FLAG:-false}" \
    EVAL_PROXY_PORT="${STUB_PORT:-9191}" \
    GITHUB_STEP_SUMMARY="$RUN_DIR/summary.md" \
    bash "$EVAL_SCRIPT" --dry-run "$@" > "$RUN_DIR/out.log" 2>&1
  RUN_RC=$?
  RUN_OUT="$(cat "$RUN_DIR/out.log")"
}

echo "═══ CLI-onboarding eval harness tests ═══"
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
name "clean room: passes in a pristine environment"
# -----------------------------------------------------------------------------
run_eval clean-pass --assert-clean-room; OUT="$RUN_OUT"
assert_eq "$RUN_RC" "0" "exit 0 in a pristine HOME"
assert_contains "$OUT" "clean room verified" "reports the clean room verified"

# -----------------------------------------------------------------------------
name "clean room: trips on each contaminant independently"
# -----------------------------------------------------------------------------
# Each contaminant is tested ALONE. A detector that only fires on a combination
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

for var in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ADP_GATEWAY_URL CLAUDE_CODE_USE_BEDROCK; do
  new_env "dirtyvar-$var"
  OUT="$(env -i PATH="$PATH" HOME="$RUN_DIR/home" EVAL_WORKDIR="$RUN_DIR/work" \
    "${UNCONTAMINATE[@]}" \
    "$var=contaminated" bash "$EVAL_SCRIPT" --dry-run --assert-clean-room 2>&1)"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    assert_contains "$OUT" "$var" "detects \$$var (exit $RC)"
  else
    bad "did NOT detect \$$var — a CLI could be redirected at another endpoint"
  fi
done

# A pre-installed CLI on PATH is contamination: the journey includes installing
# it, and in the images we care about "installed" implies "pre-configured".
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

# A listener on the sigv4-proxy port means platform-internal auth is available
# to the CLI — the single most dangerous false-green. This case exercises the
# port check for real by binding $FREE_PORT and pointing the check at it.
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
  bad "did NOT detect a listener on the proxy port — the CLI could be answered by another proxy"
fi

# -----------------------------------------------------------------------------
name "the workflow does not use the test-only escape hatches"
# -----------------------------------------------------------------------------
# EVAL_SKIP_CLI_PATH_CHECK / EVAL_FORBIDDEN_PORTS make the clean room a fiction
# if they ever reach the real run, so the workflow is asserted free of them.
WORKFLOW="$SCRIPT_DIR/../../../../.github/workflows/eval-cli-onboarding.yml"
if [ -f "$WORKFLOW" ]; then
  for hatch in EVAL_SKIP_CLI_PATH_CHECK EVAL_FORBIDDEN_PORTS; do
    assert_not_contains "$(cat "$WORKFLOW")" "$hatch" "eval-cli-onboarding.yml never sets \$$hatch"
  done
  # The ARC scale set has no Docker daemon, so a job-level `container:` cannot
  # start at all (#4171). The clean room is a pod the harness spawns instead;
  # a `container:` reappearing here means the job dies before its first step.
  assert_not_contains "$(cat "$WORKFLOW")" "container:" \
    "the eval job does NOT use container: — ARC runners have no Docker daemon"
  assert_contains "$(cat "$WORKFLOW")" "eval-pod leftover check" \
    "the workflow checks for leaked clean-room pods"
else
  bad "could not find eval-cli-onboarding.yml at $WORKFLOW"
fi

# -----------------------------------------------------------------------------
name "phases run in order A → B → C → D"
# -----------------------------------------------------------------------------
run_eval order; OUT="$RUN_OUT"
TRACE_FILE="$RUN_DIR/work/trace.log"
if [ -f "$TRACE_FILE" ]; then
  TRACE="$(tr '\n' ' ' < "$TRACE_FILE")"
  assert_eq "$(tr -d ' ' <<< "$TRACE")" "phase:0phase:Aphase:Bphase:Cphase:D" \
    "trace is exactly phase 0,A,B,C,D"
else
  bad "no trace file was written"
fi

# -----------------------------------------------------------------------------
name "--phases selects a subset (and D still runs)"
# -----------------------------------------------------------------------------
run_eval subset --phases A; OUT="$RUN_OUT"
TRACE="$(tr -d ' \n' < "$RUN_DIR/work/trace.log" 2>/dev/null)"
assert_eq "$TRACE" "phase:0phase:Aphase:D" "only phase A runs, cleanup still does"

# -----------------------------------------------------------------------------
name "cleanup runs when a phase explodes mid-run (--fail-phase B)"
# -----------------------------------------------------------------------------
# This is the guarantee that a crashed run does not leave dev mis-flagged.
run_eval failmid --fail-phase B; OUT="$RUN_OUT"
assert_contains "$OUT" "injected failure in phase B" "the injected failure fired"
assert_contains "$OUT" "Phase D" "phase D ran despite the mid-run death"
assert_contains "$OUT" "restored the flag" "the flag was restored on the way out"
if [ "$RUN_RC" -ne 0 ]; then
  ok "exits non-zero after an injected failure (rc=$RUN_RC)"
else
  bad "exited 0 after an injected failure — CI would report green"
fi

# -----------------------------------------------------------------------------
name "flag restore uses the value read at start, not a hardcoded false"
# -----------------------------------------------------------------------------
# The regression this guards: an env with the gate already ON is silently left
# OFF by the eval, i.e. the eval itself becomes a security incident.
STUB_FLAG=true run_eval flagtrue --phases A; OUT="$RUN_OUT"
KLOG="$RUN_DIR/kubectl.log"
if [ -f "$KLOG" ]; then
  # The eval sets the flag false for its Phase-A baseline, so the LAST set env
  # must put back true — the value the stub reported at start.
  LAST_SET="$(grep 'set env' "$KLOG" | tail -1)"
  assert_contains "$LAST_SET" "BG_ENFORCE_ORG_ASSIGNMENT=true" \
    "final 'set env' restores =true (the value found at start)"
  FIRST_READ="$(grep -c 'jsonpath' "$KLOG")"
  if [ "$FIRST_READ" -gt 0 ]; then
    ok "the flag was READ before being mutated ($FIRST_READ read(s))"
  else
    bad "the flag was never read — restore cannot know what to put back"
  fi
else
  bad "no kubectl invocations were logged"
fi

# The mirror case: no deployment-level override at start means restore must
# REMOVE the var (kubectl's NAME- syntax), not pin a literal that was never there.
STUB_FLAG="unset" run_eval flagunset --phases A; OUT="$RUN_OUT"
LAST_SET="$(grep 'set env' "$RUN_DIR/kubectl.log" 2>/dev/null | tail -1)"
assert_contains "$LAST_SET" "BG_ENFORCE_ORG_ASSIGNMENT-" \
  "with no override at start, restore deletes the var rather than pinning a value"

# -----------------------------------------------------------------------------
name "laptop commands execute in the pod, never on the runner"
# -----------------------------------------------------------------------------
# The guarantee that replaced env-scrubbing (#4171): the runner is the harness
# and holds credentials; the laptop is a separate pod reached only by
# `kubectl exec`. `kubectl exec` forwards none of the runner's environment, so
# there is no IRSA to borrow rather than one that gets stripped. The property is
# therefore "every laptop command left the runner", asserted from the stub log.
run_eval laptoproute --phases C; OUT="$RUN_OUT"
KLOG="$RUN_DIR/kubectl.log"
if [ ! -f "$KLOG" ]; then
  bad "no kubectl invocations were logged — phase C did not reach the pod"
else
  EXECS="$(grep -c '^exec ' "$KLOG" || echo 0)"
  if [ "$EXECS" -gt 0 ]; then
    ok "phase C routed $EXECS command(s) through kubectl exec"
  else
    bad "phase C issued no 'kubectl exec' — the laptop journey ran on the runner"
  fi
  # Every exec must target the clean-room pod — an exec at anything else would
  # be a command running somewhere that holds credentials.
  STRAY="$(grep '^exec ' "$KLOG" | grep -vc 'eval-cli-onboarding' || true)"
  assert_eq "$STRAY" "0" "every exec targets the clean-room pod"
  # Every exec that carries the laptop env (i.e. came through laptop()) must
  # carry -i: stdin is the only channel a secret is allowed to travel on, since
  # anything in an exec'd argv is visible in the exec API.
  NO_STDIN="$(grep '^exec ' "$KLOG" | grep 'AWS_EC2_METADATA_DISABLED' | grep -vc -- '^exec -i ' || true)"
  assert_eq "$NO_STDIN" "0" "every laptop() exec passes -i (stdin stays the secret channel)"
  assert_contains "$(grep '^exec ' "$KLOG" | head -1)" "AWS_EC2_METADATA_DISABLED=true" \
    "the pod env disables IMDS"
fi

# laptop() itself must contain no path that runs on the runner. Extracted
# verbatim so the test can never drift from the implementation.
LAPTOP_FN="$(sed -n '/^laptop() {/,/^}/p' "$EVAL_SCRIPT")"
if [ -z "$LAPTOP_FN" ]; then
  bad "could not extract laptop() from run-eval.sh — did it get renamed?"
else
  # shellcheck disable=SC2016  # matching laptop()'s source text, so $LAPTOP_POD must stay literal
  assert_contains "$LAPTOP_FN" 'h_kubectl exec -i "$LAPTOP_POD"' \
    "laptop() is a kubectl-exec wrapper onto the clean-room pod"
  assert_contains "$LAPTOP_FN" 'AWS_EC2_METADATA_DISABLED=true' "laptop() disables IMDS in the pod"
  # A credential the harness holds must never be named in the exec'd argv:
  # anything in an exec's arguments is visible in the exec API and the runner's
  # process table. Tokens reach the pod on stdin instead.
  for forbidden in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
                   AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE \
                   AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_PROFILE; do
    assert_not_contains "$LAPTOP_FN" "$forbidden" "laptop() never forwards \$$forbidden into the pod"
  done
fi

# -----------------------------------------------------------------------------
name "the clean-room pod is created hardened, and the gate runs inside it"
# -----------------------------------------------------------------------------
# The pod IS the clean room, so its spec is the boundary. No service-account
# token and no service-link env means there is no platform credential in reach
# — not a scrubbed one, an absent one.
RUN_CMD="$(grep '^run ' "$RUN_DIR/kubectl.log" 2>/dev/null | head -1)"
if [ -z "$RUN_CMD" ]; then
  bad "no 'kubectl run' was logged — the clean room was never created"
else
  assert_contains "$RUN_CMD" '"automountServiceAccountToken":false' \
    "the pod gets no service-account token"
  assert_contains "$RUN_CMD" '"enableServiceLinks":false' \
    "the pod gets no service-link env for in-cluster services"
  assert_contains "$RUN_CMD" '"app":"eval-cli-onboarding"' \
    "the pod is labelled for the leftover sweep"
  assert_contains "$RUN_CMD" '"karpenter.sh/do-not-disrupt"' \
    "the pod is annotated do-not-disrupt so a consolidation cannot kill it mid-run"
  assert_contains "$RUN_CMD" 'command -- sleep' \
    "the pod sleeps rather than running the image entrypoint and exiting"
  assert_contains "$RUN_CMD" '"limits"' "the pod declares resource limits"
fi
# The gate moved into the pod, and it is the FIRST thing exec'd there — before
# any provisioning, so "nothing pre-installed" is still an honest claim.
assert_contains "$OUT" "clean room is a fresh pod" \
  "the clean-room gate ran inside the pod"
FIRST_EXEC_LINE="$(grep -n '^exec ' "$RUN_DIR/kubectl.log" 2>/dev/null | grep 'assert-clean-room' | head -1)"
if [ -n "$FIRST_EXEC_LINE" ]; then
  ok "--assert-clean-room is exec'd in the pod (kubectl log line ${FIRST_EXEC_LINE%%:*})"
else
  bad "--assert-clean-room was never exec'd in the pod — the clean room is unverified"
fi
assert_not_contains "$(grep '^exec ' "$RUN_DIR/kubectl.log" 2>/dev/null | grep 'assert-clean-room' || true)" \
  "npm install" "the gate runs before anything is installed in the pod"

# -----------------------------------------------------------------------------
name "the clean-room pod is deleted in phase D, including after a failure"
# -----------------------------------------------------------------------------
# A pod outlives the job that made it, so a leak here is a leaked node. Deletion
# is by LABEL, not name: a pod orphaned by a crashed run is swept by a later run
# that never learned the old run id.
for scenario in "" "--fail-phase B"; do
  label="${scenario:-full run}"
  # shellcheck disable=SC2086  # deliberate word-split of the scenario flags
  run_eval "poddel-${scenario// /-}" $scenario; OUT="$RUN_OUT"
  DEL="$(grep '^delete .*pod' "$RUN_DIR/kubectl.log" 2>/dev/null | tail -1)"
  if [ -z "$DEL" ]; then
    bad "no pod deletion was logged ($label) — the clean room leaked"
  else
    assert_contains "$DEL" "-l app=eval-cli-onboarding" \
      "phase D sweeps the pod by label ($label)"
    assert_contains "$DEL" "--ignore-not-found" \
      "the sweep tolerates an already-gone pod, so cleanup stays idempotent ($label)"
  fi
done

# --cleanup-only must sweep pods too: it is what an operator runs after a killed
# run, when no state file records the pod's name.
run_eval podsweep --cleanup-only; OUT="$RUN_OUT"
assert_contains "$(grep '^delete .*pod' "$RUN_DIR/kubectl.log" 2>/dev/null || true)" \
  "-l app=eval-cli-onboarding" "--cleanup-only sweeps leaked pods by label"

# -----------------------------------------------------------------------------
name "--inject-failure wrong-org makes the run fail loudly"
# -----------------------------------------------------------------------------
# The acceptance check on the eval itself: if a deliberately-broken approval
# still passes, the eval is asserting nothing.
# This is the acceptance check ON THE EVAL. The dry-run stubs re-implement the
# middleware's exemption order rather than being handed the expected answer per
# phase, so an approval with an empty org really does produce a 409 and B3
# really does fail. If this test ever passes clean, the eval proves nothing.
run_eval inject --phases A,B --inject-failure wrong-org; OUT="$RUN_OUT"
assert_contains "$OUT" "expected to FAIL" "announces that the run is deliberately broken"
assert_contains "$OUT" "B3" "the failure is attributed to B3, the DB-fallback assertion"
if [ "$RUN_RC" -ne 0 ]; then
  ok "the deliberately-broken run exits non-zero (rc=$RUN_RC)"
else
  bad "the deliberately-broken run PASSED — the eval is not asserting anything"
fi
# The note lands in the job summary, not on stdout — that is where a reviewer
# checking "did the acceptance run really fail on purpose?" will look.
assert_contains "$(cat "$RUN_DIR/summary.md" 2>/dev/null)" "EXPECTED to fail" \
  "the job summary flags the injected failure"

# -----------------------------------------------------------------------------
name "the same run WITHOUT the injection passes clean"
# -----------------------------------------------------------------------------
# The control for the case above: identical phases, no injection, zero failures.
# Together they show the eval discriminates rather than always-passing or
# always-failing.
run_eval control --phases A,B; OUT="$RUN_OUT"
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
name "--cleanup-only is standalone and idempotent"
# -----------------------------------------------------------------------------
run_eval cleanonly --cleanup-only; OUT="$RUN_OUT"
assert_contains "$OUT" "Phase D" "runs phase D on its own"
assert_eq "$RUN_RC" "0" "exits 0 with nothing to clean up"
# Second run over the same (now-empty) state must behave the same. Teardown is
# the step that runs after a killed run, so "safe to run twice" is the property.
if env -i PATH="$PATH" HOME="$RUN_DIR/home" ENVIRONMENT=dev AWS_REGION=us-east-1 \
     EVAL_WORKDIR="$RUN_DIR/work" bash "$EVAL_SCRIPT" --dry-run --cleanup-only \
     > "$RUN_DIR/cleanup2.log" 2>&1; then
  assert_contains "$(cat "$RUN_DIR/cleanup2.log")" "Phase D" \
    "a second --cleanup-only re-runs teardown and still exits 0"
else
  bad "a second --cleanup-only failed (rc=$?) — teardown is not idempotent"
fi

# -----------------------------------------------------------------------------
name "the summary table renders with a row per assertion"
# -----------------------------------------------------------------------------
run_eval summary; OUT="$RUN_OUT"
SUMMARY="$RUN_DIR/summary.md"
if [ -f "$SUMMARY" ]; then
  assert_contains "$(cat "$SUMMARY")" "| Phase | Result | Assertion |" "summary has the per-phase table header"
  assert_contains "$(cat "$SUMMARY")" "Failures:" "summary reports a failure count"
  ROWS="$(grep -c '^| [0ABCD]' "$SUMMARY" || echo 0)"
  if [ "$ROWS" -gt 5 ]; then
    ok "summary has $ROWS assertion rows"
  else
    bad "summary has only $ROWS assertion rows — the table is not being populated"
  fi
else
  bad "no \$GITHUB_STEP_SUMMARY was written"
fi

# -----------------------------------------------------------------------------
name "no secret material is echoed to the log"
# -----------------------------------------------------------------------------
# The stubs hand out recognisable fake secrets; none may appear in the output.
run_eval nosecrets; OUT="$RUN_OUT"
assert_not_contains "$OUT" "stub.access.token" "the access token is never printed"
assert_not_contains "$OUT" "stub-refresh-token" "the refresh token is never printed"
assert_not_contains "$OUT" "stubpassword" "the DB password is never printed"

# -----------------------------------------------------------------------------
echo ""
echo "═══════════════════════════════════════"
if [ "$TESTS_FAILED" -eq 0 ]; then
  echo "${GREEN}All $TESTS_RUN test groups passed.${NC}"
  exit 0
fi
echo "${RED}$TESTS_FAILED assertion(s) failed across $TESTS_RUN test groups.${NC}"
exit 1

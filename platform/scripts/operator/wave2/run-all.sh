#!/usr/bin/env bash
# Wave 2 orchestration — run the fixture evaluation end to end, in order.
#
# Issue #3968, root's blocker 7: "... and an executable orchestration path."
#
# The README's runbook was a copy-paste sequence of eight commands. That is not an
# orchestration path: it puts the ordering, the run id threading, the
# stop-on-failure decision and -- critically -- the cleanup guarantee on whoever is
# pasting. This script owns all four.
#
# WHAT THIS DOES NOT DO
# ---------------------
# It does not lower any gate. Every step is the same script with the same
# refusals; this only sequences them and guarantees the parts a human operator
# forgets under pressure.
#
# THE THREE PROPERTIES THAT MATTER
# --------------------------------
# 1. CLEANUP ALWAYS RUNS. On failure, on a refusal, and on Ctrl-C -- via a trap,
#    after evidence is preserved. A fixture left running is a flag-ON gateway and a
#    live queue; the failure mode of "the operator will remember" is a resource
#    nobody owns, which is exactly what root found already present in the account.
#
# 2. NOTHING IS CREATED WITHOUT --apply. The default is a validate-only pass:
#    every step that can dry-run does, and the steps that cannot are skipped with a
#    named reason. So the default invocation is safe to run while reviewing, and
#    creation is a deliberate act.
#
# 3. A SKIPPED STEP IS REPORTED AS SKIPPED, NEVER AS PASSED. The summary counts
#    skips separately and the exit status is non-zero if any step did not pass.
#    This is the defect class running through this whole PR: an observation that
#    was never made must not grade as a satisfied one.
#
# Isolation is verified BEFORE any experiment runs (step 15). A control measurement
# taken through an unproven boundary cannot be attributed to the software, so
# proving isolation first is the difference between evidence and a number.
#
# THE STAGED SEQUENCE (--stage)
# ----------------------------
# The protected worker cannot be created until #5836's fixture edge publishes a
# control endpoint, and that edge cannot be built until the fixture Service exists.
# So a full run is not one invocation of this script -- it is two, with a Terraform
# apply that only root performs in between:
#
#   ./run-all.sh --apply --stage gateway --run-id w2-X
#       creates the fixture gateway, proves its isolation, and STOPS -- leaving the
#       fixture running on purpose (see below) with a handoff document naming the
#       next commands.
#   ... root runs #5836's create-fixture-alb.sh / terraform apply / handoff, and
#       exports `terraform output -json` -- ALL outputs, since the bindings are in
#       `ownership` and the endpoint is a separate top-level output ...
#   ./run-all.sh --apply --stage worker --run-id w2-X \
#       --evidence-dir <the same dir> --edge-receipt <that outputs json>
#       creates the protected worker against the now-real endpoint, runs the
#       experiments, and tears BOTH stages down from the shared ledger.
#
# `--stage gateway` FORCES --keep-fixture: tearing the fixture down at the end of
# that stage would make the next stage impossible (the edge would front a deleted
# Service). That is stated loudly because it means a control-flag-ON gateway and a
# live queue persist between the two invocations, and it exits non-zero -- a
# mid-sequence run has established nothing about the checks and must not read green.
#
# `--stage all` (the default) is the one-invocation path, and creates a worker only
# when an --edge-receipt from an ALREADY-EXISTING edge is supplied. Without one it is
# a gateway-only fixture: the worker-dependent steps are skipped, not faked.
#
# Usage:
#   ./run-all.sh                          # validate-only; creates nothing
#   ./run-all.sh --apply --run-id w2-...  # full run (creates the fixture)
#   ./run-all.sh --apply --no-paid        # full run, skipping the paid model step
#   ./run-all.sh --apply --stage gateway --run-id w2-...      # stage 1 of 2
#   ./run-all.sh --apply --stage worker  --run-id w2-... \
#       --evidence-dir DIR --edge-receipt PATH                # stage 2 of 2
#   ./run-all.sh --from 40 --apply        # resume from a step
#   ./run-all.sh --keep-fixture --apply   # skip the cleanup trap (must be explicit)
#
# Token env vars (values supplied by root, never by this script):
#   W2_OWNER  W2_NONOWNER  W2_OTHER_TENANT  W2_ADMIN

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/session.sh
. "$HERE/lib/session.sh"

APPLY=0
NO_PAID=0
KEEP_FIXTURE=0
RUN_ID=""
EVIDENCE_DIR=""
FROM=""
GATEWAY_URL=""
STAGE="all"
EDGE_RECEIPT=""
EDGE_ALB_ARN=""
GATEWAY_IMAGE=""
WORKER_IMAGE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)         APPLY=1; shift ;;
    --no-paid)       NO_PAID=1; shift ;;
    --keep-fixture)  KEEP_FIXTURE=1; shift ;;
    --run-id)        RUN_ID="${2:?}"; shift 2 ;;
    --evidence-dir)  EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --from)          FROM="${2:?}"; shift 2 ;;
    --gateway-url)   GATEWAY_URL="${2:?}"; shift 2 ;;
    --stage)         STAGE="${2:?}"; shift 2 ;;
    --edge-receipt)  EDGE_RECEIPT="${2:?}"; shift 2 ;;
    --edge-alb-arn)  EDGE_ALB_ARN="${2:?}"; shift 2 ;;
    --gateway-image) GATEWAY_IMAGE="${2:?}"; shift 2 ;;
    --worker-image) WORKER_IMAGE="${2:?}"; shift 2 ;;
    -h|--help)       sed -n '1,78p' "$0"; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

case "$STAGE" in
  gateway|worker|all) ;;
  *) printf 'FAIL: --stage %s is not a stage. Valid: gateway, worker, all.\n' "$STAGE" >&2
     printf '  Refused rather than defaulted: a typo falling through to "all" would try to\n' >&2
     printf '  create a protected worker in an invocation the operator meant to stop at the\n' >&2
     printf '  gateway.\n' >&2
     exit 2 ;;
esac

# The worker stage JOINS an existing run, so it cannot mint either of the two values
# that identify one. A generated run id would name a fixture that does not exist, and
# a fresh evidence directory would carry no ledger -- so 10- would be refused for a
# confusing reason instead of this one.
if [ "$STAGE" = worker ]; then
  [ -n "$RUN_ID" ] || { printf 'FAIL: --stage worker requires --run-id: it continues the run the gateway stage created,\n' >&2
    printf '  and a generated id would name a fixture that does not exist.\n' >&2; exit 2; }
  [ -n "$EVIDENCE_DIR" ] || { printf 'FAIL: --stage worker requires --evidence-dir -- the same one the gateway stage used.\n' >&2
    printf '  The shared ledger lives there; without it there is no fixture to join and no\n' >&2
    printf '  record authorising teardown of what the gateway stage created.\n' >&2; exit 2; }
  [ -n "$EDGE_RECEIPT" ] || { printf 'FAIL: --stage worker requires --edge-receipt.\n' >&2
    printf '  It is the whole reason this stage is separate: the endpoint must come from\n' >&2
    printf "  #5836's run-bound ownership output, not from an arbitrary https URL.\n" >&2
    exit 2; }
fi

# `--stage gateway` must NOT tear down what it built: the next stage needs it. Forced
# rather than required of the operator, because the failure mode of forgetting is a
# sequence that cannot continue -- and forced rather than silent, because the cost is
# a flag-ON gateway and a live queue running between two invocations.
if [ "$STAGE" = gateway ] && [ "$APPLY" -eq 1 ]; then
  KEEP_FIXTURE=1
fi

# A run id must be deterministic for the whole run and must not be minted twice.
# `date` is called exactly once, here, because two call sites would produce two ids
# and the ledger would then name resources the cleanup does not know about.
if [ -z "$RUN_ID" ]; then
  RUN_ID="w2-$(date -u +%Y%m%d-%H%M%S)"
fi
EVIDENCE_DIR="${EVIDENCE_DIR:-$PWD/w2-evidence-$RUN_ID}"
LEDGER="$EVIDENCE_DIR/cleanup-ledger.json"
mkdir -p "$EVIDENCE_DIR/artifacts"

SUMMARY="$EVIDENCE_DIR/orchestration-summary.txt"
: > "$SUMMARY"

PASSED=0; FAILED=0; SKIPPED=0
FAILED_STEPS=""; SKIPPED_STEPS=""

# Every step number this orchestrator knows about. `--from` must name one of them:
# `--from 61` previously matched nothing, ran zero steps, and exited 0 -- a green
# result over an empty run, which is the vacuous pass this script exists to remove.
readonly KNOWN_STEPS="0 10 15 20 22 30 40 60"
ATTEMPTED=0
RESUMED_PAST=""
# Declared HERE, above the EXIT trap, not next to the code that populates them.
# `on_exit` reads all three, and the script runs under `set -u`: when the account
# gate refuses (the #5195 path) the trap fired before their original declaration
# site, so print_summary died on an unbound RESUMED_PAST instead of printing the
# refusal. The summary must survive the earliest possible exit.

record() {  # record <status> <step> <detail>
  printf '%-8s %-28s %s\n' "$1" "$2" "$3" >> "$SUMMARY"
}

step_skip() {  # step_skip <step> <reason>
  SKIPPED=$((SKIPPED + 1)); SKIPPED_STEPS="$SKIPPED_STEPS $1"
  printf '\n\033[1m-- %s: SKIPPED\033[0m\n' "$1"
  w2_note "$2"
  record SKIP "$1" "$2"
}

# Runs one step. A non-zero exit is recorded and the run CONTINUES only for steps
# that are not prerequisites of what follows; prerequisites call step_gate instead.
step_run() {  # step_run <step> <description> -- <command...>
  local step="$1" desc="$2"; shift 3
  printf '\n\033[1m-- %s: %s\033[0m\n' "$step" "$desc"
  # rc is captured INSIDE the else branch. `if ... fi; rc=$?` reads the exit
  # status of the COMPLETED IF STATEMENT, which is 0 whenever the failing branch
  # has no else -- so the recorded evidence said "exit 0" for a step that failed.
  local rc=0
  if "$@"; then
    PASSED=$((PASSED + 1)); record PASS "$step" "$desc"
    return 0
  else
    rc=$?
  fi
  FAILED=$((FAILED + 1)); FAILED_STEPS="$FAILED_STEPS $step"
  record FAIL "$step" "$desc (exit $rc)"
  printf '   step %s FAILED (exit %s) -- continuing; see the summary\n' "$step" "$rc" >&2
  return 0
}

# A gate: if this fails the run stops. Used where continuing would measure nothing
# meaningful -- there is no point collecting control evidence from a fixture that
# was never created, and filing the resulting errors would be worse than stopping.
step_gate() {  # step_gate <step> <description> -- <command...>
  local step="$1" desc="$2"; shift 3
  printf '\n\033[1m-- %s: %s\033[0m\n' "$step" "$desc"
  local rc=0
  if "$@"; then
    PASSED=$((PASSED + 1)); record PASS "$step" "$desc"
    return 0
  else
    rc=$?
  fi
  FAILED=$((FAILED + 1)); FAILED_STEPS="$FAILED_STEPS $step"
  record GATE-FAIL "$step" "$desc (exit $rc) -- run stopped here"
  printf '\n\033[1mGATE FAILED at %s (exit %s). Stopping.\033[0m\n' "$step" "$rc" >&2
  w2_note "Continuing past this point would collect evidence about a fixture that is"
  w2_note "not in the expected state, and report the resulting errors as measurements."
  return 1
}

# ---------------------------------------------------------------------------
# cleanup trap
# ---------------------------------------------------------------------------
# Registered BEFORE anything is created, so an interrupt during creation is still
# covered. Ordering matters: the ledger is written by 10-create-fixture.sh before
# each resource is created, so even a partial creation leaves a cleanable record.
cleanup_ran=0
run_cleanup() {
  local signal="${1:-EXIT}"
  [ "$cleanup_ran" -eq 0 ] || return 0
  cleanup_ran=1

  # Cleanup outcomes are counted like any other step. The previous revision wrote
  # a FAIL line to the summary but never incremented FAILED, so a cleanup that
  # exited 19 -- leaving a control-flag-ON gateway live -- produced an overall
  # exit 0. A teardown that did not happen is the single most consequential thing
  # this orchestrator can get wrong, so it is the last thing that should be able
  # to fail silently.
  if [ "$KEEP_FIXTURE" -eq 1 ]; then
    printf '\n\033[1m-- cleanup: SKIPPED (--keep-fixture)\033[0m\n'
    w2_note "The fixture is STILL RUNNING: a control-flag-ON gateway and a live queue."
    w2_note "Tear it down with: ./90-cleanup-ledger.sh '$LEDGER' '$EVIDENCE_DIR'"
    record SKIP cleanup "--keep-fixture was passed; fixture left running"
    SKIPPED=$((SKIPPED + 1)); SKIPPED_STEPS="$SKIPPED_STEPS cleanup"
    return 0
  fi
  if [ ! -f "$LEDGER" ]; then
    # No ledger means nothing was RECORDED as created. That is not the same as
    # nothing having been created: a crash between creating a resource and writing
    # its ledger entry produces exactly this state. Counted as skipped so the run
    # cannot exit green while that possibility is open.
    record SKIP cleanup "no ledger written; nothing is RECORDED as created, but an
     interrupted creation would look identical -- verify by hand before closing the run"
    SKIPPED=$((SKIPPED + 1)); SKIPPED_STEPS="$SKIPPED_STEPS cleanup"
    return 0
  fi

  printf '\n\033[1m-- cleanup (%s): tearing down the fixture\033[0m\n' "$signal"
  # Deliberately not under `set -e`: cleanup must attempt every entry even if one
  # fails, and its own result file records what did and did not go.
  local rc=0
  if "$HERE/90-cleanup-ledger.sh" "$LEDGER" "$EVIDENCE_DIR"; then
    record PASS cleanup "fixture torn down"
    PASSED=$((PASSED + 1))
  else
    rc=$?
    record FAIL cleanup "cleanup reported failures (exit $rc) -- SEE cleanup-ledger-result.json"
    FAILED=$((FAILED + 1)); FAILED_STEPS="$FAILED_STEPS cleanup"
    printf '\n\033[1mCLEANUP DID NOT FULLY SUCCEED.\033[0m Resources may still exist.\n' >&2
    w2_note "Inspect $EVIDENCE_DIR/cleanup-ledger-result.json and finish by hand."
    w2_note "Do NOT assume absence: confirm each resource is gone before closing the run."
  fi
}

print_summary() {
  printf '\n=========================== summary ===========================\n'
  cat "$SUMMARY"
  printf '\npassed %s | failed %s | skipped %s\n' "$PASSED" "$FAILED" "$SKIPPED"
  [ -z "$FAILED_STEPS" ]  || printf 'failed steps: %s\n' "$FAILED_STEPS"
  [ -z "$SKIPPED_STEPS" ] || printf 'skipped steps:%s\n' "$SKIPPED_STEPS"
  if [ -n "$RESUMED_PAST" ]; then
    printf 'NOT RUN in this invocation (--from %s):%s\n' "$FROM" "$RESUMED_PAST"
    printf '  Their results are NOT carried forward. This run establishes nothing about\n'
    printf '  them; cite the earlier run'"'"'s evidence explicitly or re-run from 0.\n'
  fi
  printf 'run id:   %s\nevidence: %s\n' "$RUN_ID" "$EVIDENCE_DIR"
}

on_exit() {
  local rc=$?
  set +e
  run_cleanup EXIT
  print_summary
  # A skip is not a pass. If anything failed or was skipped the run did not
  # establish what it set out to, and the exit status has to say so -- a green
  # exit over a partially-skipped run is the vacuous pass this PR exists to remove.
  if [ "$FAILED" -gt 0 ] || [ "$SKIPPED" -gt 0 ]; then
    exit $(( rc != 0 ? rc : 1 ))
  fi
  # A run that reached NO step cannot have established anything either. Without
  # this, `--from 61` (and any resume past the last step) exited 0 having graded
  # nothing at all: no step ran, so nothing failed and nothing was skipped, and
  # both counters above stayed at zero. An empty run is not a passing run.
  if [ "${ATTEMPTED:-0}" -eq 0 ] || [ "$PASSED" -eq 0 ]; then
    printf '\nFAIL: this invocation reached no step, so it establishes nothing.\n' >&2
    printf '  Valid --from resume points: %s\n' "${KNOWN_STEPS:-<unset>}" >&2
    exit $(( rc != 0 ? rc : 1 ))
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'printf "\ninterrupted\n" >&2; exit 130' INT TERM

# ---------------------------------------------------------------------------
# preamble
# ---------------------------------------------------------------------------
printf '== wave 2 orchestration ==\n'
w2_report_mode
w2_require_account
w2_ok "account $W2_ACCOUNT"
w2_note "identity: $W2_ARN"
w2_note "run id:   $RUN_ID"
w2_note "evidence: $EVIDENCE_DIR"
if [ "$APPLY" -eq 1 ]; then
  w2_note "mode:     APPLY -- the fixture WILL be created"
else
  w2_note "mode:     validate-only -- nothing will be created (pass --apply to create)"
fi

case " $KNOWN_STEPS " in
  *" ${FROM:-0} "*) ;;
  *) w2_fail "--from '$FROM' is not a step. Valid resume points: $KNOWN_STEPS" ;;
esac

# A resume assumes earlier steps already succeeded, and this run has no evidence
# of that: their results live in a previous run's evidence directory. Resuming is
# supported, but the summary must not present unverified earlier steps as passed.
if [ -n "$FROM" ] && [ "$FROM" -gt 0 ]; then
  for s in $KNOWN_STEPS; do
    [ "$s" -lt "$FROM" ] && RESUMED_PAST="$RESUMED_PAST $s"
  done
fi

want() {  # want <step-number> -- should this step run, given --from?
  if [ -z "$FROM" ] || [ "$1" -ge "$FROM" ]; then
    ATTEMPTED=$((ATTEMPTED + 1))
    return 0
  fi
  return 1
}

# ---------------------------------------------------------------------------
# 00 — target
# ---------------------------------------------------------------------------
if want 0; then
  step_gate "00-target" "verify target account, schema, DP-INV-1 flag state" -- \
    "$HERE/00-verify-target.sh"
fi

# ---------------------------------------------------------------------------
# 10 — fixture
# ---------------------------------------------------------------------------
if want 10; then
  # The worker flags, assembled once. The worker stage always carries them (they are
  # required above); `--stage all` carries them only if a receipt from an existing edge
  # was supplied, and otherwise builds a gateway-only fixture rather than pretending.
  FIXTURE_ARGS=(--run-id "$RUN_ID" --ledger "$LEDGER" --evidence-dir "$EVIDENCE_DIR"
                --stage "$STAGE")
  if [ -n "$EDGE_RECEIPT" ] && [ "$STAGE" != gateway ]; then
    FIXTURE_ARGS+=(--worker-job --edge-receipt "$EDGE_RECEIPT")
    if [ -n "$EDGE_ALB_ARN" ]; then
      FIXTURE_ARGS+=(--edge-alb-arn "$EDGE_ALB_ARN")
    fi
  fi

  if [ -n "$GATEWAY_IMAGE$WORKER_IMAGE" ]; then
    FIXTURE_ARGS+=(--gateway-image "$GATEWAY_IMAGE" --worker-image "$WORKER_IMAGE")
  fi

  # The dry run is skipped on the worker stage, not silently dropped. Its purpose is
  # to prove nothing collides before creating anything, and on this stage that is the
  # stage gate's job instead: --check-only would have to observe the gateway stage's
  # own objects, and a server dry-run of the Job against them establishes nothing the
  # gate has not already established against the recorded uids.
  if [ "$STAGE" = worker ]; then
    w2_note "10-fixture-check: not applicable to the worker stage -- the stage gate checks"
    w2_note "  this run's recorded uids, which is a stronger statement than a dry run."
  else
    step_gate "10-fixture-check" "dry-run the fixture (creates nothing)" -- \
      "$HERE/10-create-fixture.sh" "${FIXTURE_ARGS[@]}" --check-only
  fi

  if [ "$APPLY" -eq 1 ]; then
    step_gate "10-fixture-apply" "create the run-bound fixture (stage: $STAGE)" -- \
      "$HERE/10-create-fixture.sh" "${FIXTURE_ARGS[@]}"
  else
    step_skip "10-fixture-apply" "no --apply, so no fixture exists. Every step below that
     needs one is skipped rather than run against the ordinary deployment."
  fi
fi

# ---------------------------------------------------------------------------
# 15 — isolation, BEFORE any experiment
# ---------------------------------------------------------------------------
if want 15; then
  if [ "$APPLY" -eq 1 ]; then
    step_gate "15-isolation" "prove fixture isolation with real socket probes" -- \
      "$HERE/15-verify-isolation.sh" --run-id "$RUN_ID" --evidence-dir "$EVIDENCE_DIR"
  else
    step_skip "15-isolation" "needs a live fixture to probe from. Probing nothing would
     produce all-error results, and an all-error run must not be filed as isolation proof."
  fi
fi

# ---------------------------------------------------------------------------
# the gateway stage ends here — deliberately, and not as a pass
# ---------------------------------------------------------------------------
# Everything below needs the protected worker, which cannot exist until #5836's edge
# does. The remaining steps are therefore SKIPPED with that reason rather than run:
# 20/40/60 against a workerless fixture would produce real errors about a fixture that
# is merely incomplete, and the harness would file them as failed checks of the
# software under review.
#
# The exit status is non-zero because of those skips (and the forced --keep-fixture),
# which is correct and load-bearing: a mid-sequence invocation established nothing
# about W2-03/04/05, and an operator or CI job reading exit 0 here would conclude the
# evaluation had run.
if [ "$STAGE" = gateway ]; then
  for pending in 22 20 40 30 60; do
    if want "$pending"; then
      step_skip "$pending-gateway-stage" "the gateway stage stops before the experiments:
     they need the protected worker, which needs #5836's edge, which needs the Service
     this stage just created. Continue with --stage worker once the edge exists."
    fi
  done
  printf '\n\033[1m-- gateway stage complete: HANDOFF --\033[0m\n'
  w2_note "The fixture is STILL RUNNING and was NOT torn down -- the next stage needs it."
  w2_note "  That means a control-flag-ON fixture gateway and a live fixture queue persist"
  w2_note "  until teardown. This invocation is NOT a passing evaluation run."
  w2_note "Next commands, with this run's nonce and uids already filled in:"
  w2_note "  $EVIDENCE_DIR/stage-handoff.json"
  w2_note "Then:  ./run-all.sh --apply --stage worker --run-id $RUN_ID \\"
  w2_note "         --evidence-dir $EVIDENCE_DIR --edge-receipt <ownership json>"
  w2_note "ALWAYS, whether or not the worker stage runs:"
  w2_note "  ./90-cleanup-ledger.sh '$LEDGER' '$EVIDENCE_DIR'"
  exit 0   # the EXIT trap downgrades this: skips and --keep-fixture make it non-zero
fi

# ---------------------------------------------------------------------------
# 22 — suites (no cloud needed)
# ---------------------------------------------------------------------------
if want 22; then
  step_run "22-suites" "neutral-contract + vocabulary suites, stats schema" -- \
    "$HERE/22-collect-suite-evidence.sh" --evidence-dir "$EVIDENCE_DIR" \
      --ledger "$LEDGER" --expected-identity "$EVIDENCE_DIR/expected-identity.json" \
      --gateway-image "$GATEWAY_IMAGE" --worker-image "$WORKER_IMAGE"
fi

# ---------------------------------------------------------------------------
# 20 — paid SDK pause evidence
# ---------------------------------------------------------------------------
if want 20; then
  if [ "$NO_PAID" -eq 1 ]; then
    step_skip "20-pause" "--no-paid. W2-03/04/05 have NO evidence; the harness will fail
     them, which is correct -- unit mocks are not acceptable for these checks."
  elif [ "$APPLY" -eq 0 ]; then
    step_skip "20-pause" "validate-only: this step spends real money, so it is never run
     without --apply."
  elif [ ! -f "$EVIDENCE_DIR/expected-identity.json" ]; then
    # The document 10- writes when it BINDS the worker pod. Its absence means the
    # fixture has no bound workload -- either the worker was not created, or it was
    # created and refused. Step 20 requires it on the live path and would exit 1, so
    # this is a skip with the real reason rather than a FAIL whose message says only
    # "--expected-identity is required".
    step_skip "20-pause" "no bound worker: $EVIDENCE_DIR/expected-identity.json was not
     written by 10-fixture-apply, so there is no workload this evidence could be about.
     Spending model calls would produce a measurement of an unidentified process."
  else
    step_run "20-pause" "real-SDK pause experiments (PAID model calls)" -- \
      "$HERE/20-collect-pause-evidence.sh" --evidence-dir "$EVIDENCE_DIR" \
      --expected-identity "$EVIDENCE_DIR/expected-identity.json" \
      --ledger "$LEDGER"
  fi
fi

# ---------------------------------------------------------------------------
# 40 — identities
# ---------------------------------------------------------------------------
if want 40; then
  MISSING=""
  for var in W2_OWNER W2_NONOWNER W2_OTHER_TENANT; do
    [ -n "${!var:-}" ] || MISSING="$MISSING $var"
  done
  if [ -n "$MISSING" ]; then
    step_skip "40-identities" "missing token env vars:$MISSING. Minting them needs an
     interactive GitHub OAuth login per identity; root supplies them at execution time."
  else
    # Discover the fixture pod so the sessions are genuinely fixture-scoped. Without
    # it 40- falls back to a routable URL, which terminates at the ORDINARY
    # deployment -- a real result, but not a fixture measurement.
    FIXTURE_POD=""
    if [ "$APPLY" -eq 1 ]; then
      FIXTURE_POD="$(w2_kubectl get pods -n "${W2_GW_NS:-adp-gateway}" \
        -l "adp.io/w2-fixture=$RUN_ID" --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    fi
    if [ -n "$FIXTURE_POD" ]; then
      step_run "40-identities" "verify three real sessions against the FIXTURE" -- \
        "$HERE/40-verify-edge-sessions.sh" --evidence-dir "$EVIDENCE_DIR" \
        --run-id "$RUN_ID" --fixture-pod "$FIXTURE_POD" --gateway-url "${GATEWAY_URL:-}" \
        --owner-token-env W2_OWNER --nonowner-token-env W2_NONOWNER \
        --other-tenant-token-env W2_OTHER_TENANT
    elif [ -n "$GATEWAY_URL" ]; then
      w2_note "no fixture pod found; sessions will be verified against the ordinary"
      w2_note "deployment and recorded as fixture_scoped: false"
      step_run "40-identities" "verify three real sessions (NOT fixture-scoped)" -- \
        "$HERE/40-verify-edge-sessions.sh" --evidence-dir "$EVIDENCE_DIR" \
        --gateway-url "$GATEWAY_URL" \
        --owner-token-env W2_OWNER --nonowner-token-env W2_NONOWNER \
        --other-tenant-token-env W2_OTHER_TENANT
    else
      step_skip "40-identities" "no fixture pod and no --gateway-url, so there is nothing to
     verify the sessions against."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 30 — seed + count
# ---------------------------------------------------------------------------
if want 30; then
  if [ -z "${W2_ADMIN:-}" ]; then
    step_skip "30-seed-check" "W2_ADMIN (platform-admin bearer) is unset. An org-scoped
     token silently reports every delta as 0, so this refuses rather than guessing."
  elif [ -z "$GATEWAY_URL" ]; then
    step_skip "30-seed-check" "--gateway-url is required to read counters back from the
     deployed reader."
  else
    step_run "30-seed-check" "validate seeding without writing anything" -- \
      "$HERE/30-seed-and-count.sh" --run-id "$RUN_ID" --ledger "$LEDGER" \
      --evidence-dir "$EVIDENCE_DIR" --gateway-url "$GATEWAY_URL" \
      --admin-token-env W2_ADMIN --check-only
    # The real seed writes synthetic rows, so it is gated on --apply like creation.
    if [ "$APPLY" -eq 1 ]; then
      # The ids are NOT inferred: they are read back from step 40's session artifact,
      # where each one is the gateway's own verdict about a real token (never a
      # locally decoded JWT). The previous revision skipped this unconditionally even
      # under --apply, so the advertised "full --apply path" never seeded, and W2-06 --
      # which needs an aborted row to read back -- had no data on every full run.
      #
      # Fails CLOSED. Seeding under a guessed owner writes a synthetic row into a real
      # tenant that nothing in the ledger can attribute, which is worse than not seeding.
      SESSIONS="$EVIDENCE_DIR/artifacts/identity_sessions.json"
      SEED_IDS=""
      SEED_WHY=""
      if [ -f "$SESSIONS" ]; then
        SEED_IDS="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print("unreadable identity_sessions.json: %s" % exc, file=sys.stderr); raise SystemExit(1)
# Only a session set that AUTHENTICATED and resolved to DISTINCT principals is an
# identity source. If owner and nonowner collapsed to one principal, the "owner" id
# is not established and seeding under it would attribute a row to the wrong party.
if d.get("all_authenticated") is not True:
    print("step 40 did not authenticate every session", file=sys.stderr); raise SystemExit(1)
if d.get("distinct_principals") is not True:
    print("step 40 did not establish distinct principals: %s" % (d.get("problems") or []),
          file=sys.stderr); raise SystemExit(1)
owner = (d.get("sessions") or {}).get("owner") or {}
if owner.get("status") != 200:
    print("owner session status is %r, not 200" % (owner.get("status"),), file=sys.stderr)
    raise SystemExit(1)
user = owner.get("user_id")
tenant = owner.get("org_id") or owner.get("tenant_id")
if not user or not tenant:
    print("owner session carried user_id=%r tenant=%r; both are required" % (user, tenant),
          file=sys.stderr)
    raise SystemExit(1)
for value in (user, tenant):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        print("refusing a non-string or whitespace-bearing id: %r" % (value,), file=sys.stderr)
        raise SystemExit(1)
print(user); print(tenant)
' "$SESSIONS" 2>&1)" || { SEED_WHY="$SEED_IDS"; SEED_IDS=""; }
      else
        SEED_WHY="step 40 wrote no identity_sessions.json (it was skipped or failed)"
      fi
      if [ -n "$SEED_IDS" ]; then
        SEED_USER="$(printf '%s\n' "$SEED_IDS" | sed -n 1p)"
        SEED_TENANT="$(printf '%s\n' "$SEED_IDS" | sed -n 2p)"
        w2_note "seeding as step 40's verified owner principal (tenant $SEED_TENANT)"
        step_run "30-seed-apply" "seed the W2-06 read-back row as the verified owner" -- \
          "$HERE/30-seed-and-count.sh" --run-id "$RUN_ID" --ledger "$LEDGER" \
          --evidence-dir "$EVIDENCE_DIR" --gateway-url "$GATEWAY_URL" \
          --admin-token-env W2_ADMIN \
          --owner-user-id "$SEED_USER" --owner-tenant-id "$SEED_TENANT"
      else
        step_skip "30-seed-apply" "no verified owner identity to seed under: $SEED_WHY.
     Seeding needs --owner-user-id/--owner-tenant-id, and guessing them would write a
     synthetic row into a real tenant under an identity nothing verified. W2-06 has no
     read-back row on this run; the harness will report it unsatisfied, which is correct."
      fi
    else
      step_skip "30-seed-apply" "validate-only."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 60 — harness
# ---------------------------------------------------------------------------
if want 60; then
  CONFIG="$EVIDENCE_DIR/fixture-config.json"
  if [ -f "$CONFIG" ]; then
    # --evidence-dir is where result.json and the raw observation artifacts land.
    # Omitting it does NOT fail: the evaluator defaults to ./test-results/agent-control,
    # so the grade was written outside this run's evidence directory, beside whatever a
    # previous invocation left there. Every other step here is passed --evidence-dir;
    # the one that produces the verdict was the one not threading it through.
    step_run "60-harness" "run the Wave 2 evaluation harness" -- \
      python3 "$HERE/../../agent-control-eval.py" --wave 2 --config "$CONFIG" \
      --evidence-dir "$EVIDENCE_DIR"
  else
    step_skip "60-harness" "no fixture-config.json was produced (step 10 did not create a
     fixture), so the harness has nothing to read. Running it would grade absent artifacts."
  fi
fi

# Cleanup and the summary run from the EXIT trap, so they happen on every path
# including an early gate failure or Ctrl-C.
exit 0

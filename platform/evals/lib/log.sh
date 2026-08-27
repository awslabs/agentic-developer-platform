# shellcheck shell=bash
# =============================================================================
# lib/log.sh — output, result recording and the job-summary table
# =============================================================================
# Sourced by every eval harness under platform/evals/. Factored out of the
# cli-onboarding eval (#4157) when the budget/rate-limit eval (#4163) needed the
# same reporting contract: one shared table format means the triage agent reads
# every eval the same way.
#
# CONTRACT WITH THE CALLER
# The caller must define, before calling anything here:
#   TRACE_FILE    — phase/step ordering log (the dry-run tests assert on it)
#   RESULTS_FILE  — TSV of phase/result/assertion, rendered into the summary
# and may set:
#   EVAL_SUMMARY_TITLE — heading for the job summary (default: "eval")
#
# All assert_*/fail() helpers RECORD and return, never abort: an eval must run
# its whole matrix, because fail-fast hides every later regression behind the
# first one and the triage agent needs the complete table to know how many
# fix-issues to file. $FAILURES carries the verdict.
# =============================================================================

if [ -t 1 ]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
fi

# Set with :=-style defaults rather than plain assignment so sourcing this file
# twice (or after the caller has already counted a failure) cannot reset the
# verdict to zero.
: "${FAILURES:=0}"
: "${CURRENT_PHASE:=init}"

log()   { echo "${BLUE}[eval]${NC} $*"; }
pass()  { echo "${GREEN}[PASS]${NC} $*"; record_result PASS "$*"; }
fail()  { echo "${RED}[FAIL]${NC} $*" >&2; record_result FAIL "$*"; FAILURES=$((FAILURES + 1)); }
skip()  { echo "${YELLOW}[SKIP]${NC} $*"; record_result SKIP "$*"; }
die()   { echo "${RED}[FATAL]${NC} $*" >&2; exit 1; }

# trace() records phase/step ordering. The dry-run unit tests assert against it,
# and on a real failure it tells the triage agent which phase died.
trace() { echo "$1" >> "$TRACE_FILE"; }

record_result() {
  printf '%s\t%s\t%s\n' "$CURRENT_PHASE" "$1" "$2" >> "$RESULTS_FILE"
}

phase() {
  CURRENT_PHASE="$1"
  trace "phase:$1"
  echo ""
  log "═══ Phase $1 — $2 ═══"
}

# mask() hides a secret from the Actions log the moment it exists. Guarded so a
# local run does not print the token it is trying to protect.
mask() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::add-mask::$1"
  fi
}

# after_phase <label> <rc> <failures-before> — reconcile a finished phase.
#
# A phase gives up early (`return 1`) when a prerequisite is missing: no point
# asserting a limit trips if the config write never landed. That must NOT end the
# run — the remaining phases still have to execute so the summary table is
# complete and the triage agent sees every regression, not just the first.
# `die()` (a genuinely unrecoverable setup error) still exits, because it exits
# the shell rather than returning.
#
# Invoking a phase with `|| rc=$?` also suspends `set -e` for its dynamic extent,
# so an unguarded command failure inside a phase surfaces as a non-zero return
# rather than killing the script. Either way it is recorded: a phase that returns
# non-zero without having called fail() would otherwise pass silently, so the
# count is checked here and a failure synthesised.
after_phase() {
  local label="$1" rc="$2" before="$3"
  [ "$rc" -eq 0 ] && return 0
  if [ "$FAILURES" -eq "$before" ]; then
    CURRENT_PHASE="$label"
    fail "phase $label aborted early with no recorded assertion failure — a command failed unguarded"
  else
    log "phase $label stopped early after a failed assertion (remaining phases still run)"
  fi
}

# A per-phase table in the job summary is what the triage agent reads to decide
# which phase to file a fix-issue against, so it must render even on a crash.
write_summary() {
  local out="${GITHUB_STEP_SUMMARY:-/dev/null}"
  {
    echo "## ${EVAL_SUMMARY_TITLE:-eval} — ${ENVIRONMENT} — run ${EVAL_RUN_ID}"
    echo ""
    echo "| Phase | Result | Assertion |"
    echo "|-------|--------|-----------|"
    if [ -s "$RESULTS_FILE" ]; then
      awk -F'\t' '{
        r = ($2=="PASS") ? "✅ pass" \
          : ($2=="FAIL") ? "❌ FAIL" \
          : ($2=="FINDING") ? "🔎 finding" \
          : "⚪ skip"
        printf "| %s | %s | %s |\n", $1, r, $3
      }' "$RESULTS_FILE"
    else
      echo "| — | ❌ FAIL | eval produced no assertions (died during setup) |"
    fi
    echo ""
    echo "**Failures: ${FAILURES}**"
    if [ -n "${INJECT_FAILURE:-}" ]; then
      echo ""
      echo "> \`--inject-failure ${INJECT_FAILURE}\` was set: this run is EXPECTED to fail."
    fi
    # Findings are observations the eval PINS rather than pass/fail assertions —
    # the #4163 cases where the honest answer is "here is what the platform does
    # today". Rendered as a separate block so a reader does not mistake a pinned
    # reality for a passing assertion.
    if [ -n "${FINDINGS_FILE:-}" ] && [ -s "${FINDINGS_FILE:-/dev/null}" ]; then
      echo ""
      echo "### Findings (pinned current behaviour, not pass/fail)"
      echo ""
      while IFS= read -r line; do
        echo "- ${line}"
      done < "$FINDINGS_FILE"
    fi
  } >> "$out"

  echo ""
  if [ "$FAILURES" -eq 0 ]; then
    echo "${GREEN}=== eval passed: 0 failures ===${NC}"
  else
    echo "${RED}=== eval FAILED: ${FAILURES} failure(s) ===${NC}"
  fi
}

# finding() records an observation for the summary's Findings block AND prints it.
# Used where the issue asks the eval to "assert whatever is true today" — the
# result is a pinned fact, so counting it as a pass would overstate it and
# counting it as a failure would make the eval red for reporting reality.
finding() {
  echo "${YELLOW}[FINDING]${NC} $*"
  if [ -n "${FINDINGS_FILE:-}" ]; then
    printf '%s\n' "$*" >> "$FINDINGS_FILE"
  fi
  record_result FINDING "$*"
}

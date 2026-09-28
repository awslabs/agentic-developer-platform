#!/usr/bin/env bash
# Wave-2 evaluation for the delivery-loop graph (issue #4239, EPIC #4191).
#
# Deterministic re-runnable form of the 9 checks in #4239. Stories under test:
#   #4199 — loop proposal schema + advisory CLI validator + in-transaction compile
#   #4200 — plan amendment as a first-class attributed engine operation
#
# Deploy target: adp-dev-embark1 (see ../deploy-target.md). Account is asserted,
# never assumed — this exits non-zero rather than evaluating the wrong account.
#
# Usage:
#   ./evaluate-wave-2.sh              # run every check
#   ./evaluate-wave-2.sh --no-live    # skip the account/cluster binding checks
#
# Exit 0 only when every check passes.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
GATEWAY="${REPO_ROOT}/modules/gateway"
FIXTURES="${REPO_ROOT}/aidlc/spaces/issue-4120/construction/loop-proposal"
DESIGN_DOC="aidlc/spaces/issue-4120/design-overview.md"

# Pinned at wave-2 dispatch. Check 9 asserts the component-change map moved off
# this commit, i.e. that it was updated for THIS wave rather than left stale.
WAVE1_BASELINE="7b967d497a6eedbd48e643ac522f016dd9a54d1f"

# Wave-2 story merge commits. NOTE: #4199 merged as 30c2ef5 (PR #4283) and #4200
# as 685f0a4 (PR #4285). 3b3dd81 (PR #4286) is the docs-only D-R21 map update and
# is deliberately NOT in this list — gateway-ci.yml does not include
# aidlc/**/*.md in its path filters, so it legitimately runs no Lint/Test jobs.
WAVE2_STORY_COMMITS=("30c2ef5" "685f0a4")
REQUIRED_CI_JOBS=("Lint" "Test" "Frontend Unit Tests")

TARGET_ACCOUNT="879318057152"

LIVE=1
[[ "${1:-}" == "--no-live" ]] && LIVE=0

PASS=0; FAIL=0
declare -a RESULTS

ok()   { echo "  ✅ PASS — $1"; RESULTS+=("PASS|$2|$1"); PASS=$((PASS+1)); }
bad()  { echo "  ❌ FAIL — $1"; RESULTS+=("FAIL|$2|$1"); FAIL=$((FAIL+1)); }
head_() { echo; echo "── Check $1: $2"; }

# Quiet pytest: we assert on the exit code, not on the warning spam.
pyt() { (cd "$GATEWAY" && python3 -m pytest "$@" -q >/dev/null 2>&1); }

# ── Check 0 (precondition): right account, and wave 2 actually DEPLOYED ────────
if [[ $LIVE -eq 1 ]]; then
  head_ 0 "precondition — on target account and wave 2 is deployed"
  acct="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
  if [[ "$acct" == "$TARGET_ACCOUNT" ]]; then
    ok "on target account (adp-dev-embark1)" 0
  else
    bad "expected account $TARGET_ACCOUNT (adp-dev-embark1), got '${acct:-none}'" 0
    echo; echo "Refusing to evaluate against the wrong account."; exit 1
  fi
  img="$(kubectl get deploy bedrockgateway -n adp-gateway \
         -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)"
  # The deployed tag must be one of the wave-2 story commits (or later), and
  # amend.py must exist in the image — merged-but-not-deployed is the failure
  # mode this guards against.
  if kubectl exec -n adp-gateway deploy/bedrockgateway -c bedrockgateway -- \
       test -f /app/src/orchestration/amend.py 2>/dev/null; then
    ok "wave-2 code is live in the pod (amend.py present; image ${img##*:})" 0
  else
    bad "amend.py not present in the running image (${img:-unknown}) — wave 2 is merged but not deployed" 0
  fi
fi

# ── Check 1: advisory CLI validator, valid → 0 and invalid → non-zero ──────────
head_ 1 "CLI validator exits 0 on a valid fixture, non-zero on an invalid one"
python3 "${REPO_ROOT}/.github/scripts/validate_loop_proposal.py" \
  "${FIXTURES}/example-proposal.json" >/dev/null 2>&1
[[ $? -eq 0 ]] && ok "valid fixture exits 0" 1 || bad "valid fixture did not exit 0" 1

python3 "${REPO_ROOT}/.github/scripts/validate_loop_proposal.py" \
  "${FIXTURES}/cyclic-proposal.json" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "invalid (cyclic) fixture exits non-zero" 1 \
               || bad "invalid fixture exited 0 — the validator is not rejecting it" 1

# ── Check 2: proposal model suite ─────────────────────────────────────────────
head_ 2 "pytest tests/orchestration/test_proposal.py"
pyt tests/orchestration/test_proposal.py \
  && ok "test_proposal.py green" 2 || bad "test_proposal.py failed" 2

# ── Check 3: compile is authoritative — invalid doc raises AND writes 0 rows ──
head_ 3 "compile_proposal rejects an invalid document atomically (AC-29)"
pyt tests/orchestration/test_compile.py -k invalid \
  && ok "invalid document raises and leaves zero rows (CLI bypass cannot write)" 3 \
  || bad "test_compile.py -k invalid failed" 3

# ── Check 4: cross-org amendment is 404, not 403 (no existence disclosure) ────
# The issue designates this test-level assertion authoritative when no live
# token with a real org_id + seeded flow is available.
head_ 4 "cross-org amendment returns 404 (not 403)"
pyt tests/orchestration/test_amend.py -k "another_org_gets_404 or cross_org" \
  && ok "cross-org attempt yields 404 and writes nothing" 4 \
  || bad "cross-org isolation assertions failed" 4

# ── Check 5: one plan in force, superseded version still queryable ───────────
# Field is nullable `superseded_at` (null = in force), NOT a boolean `superseded`.
head_ 5 "after one amendment: exactly one current plan, superseded still queryable"
pyt tests/orchestration/test_amend.py -k "TestAC28HappyPath" \
  && ok "v1 superseded_at set, v2 superseded_at null, both coexist" 5 \
  || bad "AC-28 happy-path (plan versioning) assertions failed" 5

# Guard the literal itself: the corrected jq filter must accept the real
# response shape. This is what silently mismatched before the dispatch fix.
python3 - <<'PY' >/dev/null 2>&1
import json, subprocess, sys
payload = [{"version":1,"superseded_at":"2026-01-01T00:00:00+00:00"},
           {"version":2,"superseded_at":None}]
f = '([.[]|select(.superseded_at!=null)]|length>=1) and ([.[]|select(.superseded_at==null)]|length==1)'
sys.exit(subprocess.run(["jq","-e",f], input=json.dumps(payload),
                        capture_output=True, text=True).returncode)
PY
[[ $? -eq 0 ]] && ok "corrected superseded_at jq filter matches PlanVersionResponse" 5 \
              || bad "jq filter does not match the deployed response shape" 5

# ── Check 6: amendment attributed to a human, role at the time, own field ─────
# HTTP half (GET /flows/{id}/decisions) is a forward reference to S13 (#4213,
# wave 6) and is NOT owed by wave 2. Decision kind is `plan_amended`, not `amend`.
head_ 6 "amendment attribution (human actor, role, distinct decision kind)"
pyt tests/orchestration/test_amend.py \
  -k "decision_carries_full_attribution or decision_kind_is_distinct" \
  && ok "attribution recorded with human/service discriminator and plan_amended kind" 6 \
  || bad "attribution assertions failed" 6

# ── Check 7: amendment permission is org-scoped by equality assertion ────────
head_ 7 "amendment permission registered in _ORG_SCOPED_PERMISSIONS (AC-17)"
pyt tests/orchestration/ -k "org_scoped or permission" \
  && ok "org-scoped permission registration + frontend mirror green" 7 \
  || bad "org-scoped permission assertions failed" 7

# ── Check 8: CI green on each wave-2 STORY merge commit ──────────────────────
head_ 8 "CI jobs ${REQUIRED_CI_JOBS[*]} green on each wave-2 story merge commit"
for c in "${WAVE2_STORY_COMMITS[@]}"; do
  runs="$(gh api "repos/aws-e/adp/commits/${c}/check-runs" \
          --jq '.check_runs[] | "\(.name)=\(.conclusion)"' 2>/dev/null)"
  for job in "${REQUIRED_CI_JOBS[@]}"; do
    if grep -qx "${job}=success" <<<"$runs"; then
      ok "${c}: ${job} success" 8
    else
      bad "${c}: ${job} not success (got: $(grep -F "${job}=" <<<"$runs" | head -1 || echo missing))" 8
    fi
  done
done

# ── Check 9: component-change map was updated FOR THIS WAVE ─────────────────
head_ 9 "design-overview.md moved off the wave-1 baseline and names #4199/#4200"
cur="$(cd "$REPO_ROOT" && git log -1 --format=%H -- "$DESIGN_DOC")"
if [[ "$cur" != "$WAVE1_BASELINE" ]]; then
  ok "last-touch commit ${cur:0:7} differs from wave-1 baseline ${WAVE1_BASELINE:0:7}" 9
else
  bad "design-overview.md still at the wave-1 baseline — not updated for wave 2" 9
fi
if grep -q "4199" "${REPO_ROOT}/${DESIGN_DOC}" && grep -q "4200" "${REPO_ROOT}/${DESIGN_DOC}"; then
  ok "map names both #4199 and #4200" 9
else
  bad "map does not name both wave-2 stories" 9
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════"
printf 'Wave-2 evaluation: %d passed, %d failed\n' "$PASS" "$FAIL"
echo "══════════════════════════════════════════════"
if [[ $FAIL -ne 0 ]]; then
  echo "Failing checks:"
  for r in "${RESULTS[@]}"; do
    [[ "$r" == FAIL* ]] && echo "  - check ${r#FAIL|}"
  done
  exit 1
fi
echo "All checks passed."

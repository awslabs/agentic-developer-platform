#!/usr/bin/env bash
# evaluate-wave-2.sh — deterministic post-deploy eval for EPIC #4324 wave 2
# Stories: #4399 (U-2 envelope composition), #4400 (U-3 run drill-down)
# Eval issue: #4411.  Target: adp-dev-embark1 (account 879318057152, us-east-1).
#
# Usage:  ./evaluate-wave-2.sh            # full run (live + git + CI + unit)
#         ./evaluate-wave-2.sh --no-live  # git/CI checks only
#
# Credentials are NEVER ambient: binds the IRSA web-identity chain (auto-refresh),
# per aidlc/spaces/issue-4324/inception/delivery-planning/deploy-target.md.
#
# ---------------------------------------------------------------------------
# THREE OF #4411's CHECKS ARE MIS-AUTHORED. They are corrected here, with the
# deviation and its justification recorded inline. All three are eval bugs, not
# wave-2 defects — same class as the three #4410 hit in wave 1.
#
#  * Check 2 is MALFORMED jq and cannot pass on a capped response.
#      `[...] | length as $n | (...) or (.binding.cap_status == "capped")`
#    `X | length as $n | BODY` evaluates BODY with `length`'s INPUT (the array)
#    as `.`, not the root object. So `.binding` indexes the lines array and jq
#    dies: `Cannot index array with string "binding"` (exit 5). It "passes" on
#    the uncapped dev response only because `$n == 0` short-circuits the `or`
#    before the broken half evaluates — a false pass that hides a hard error.
#    Corrected by parenthesising the length so `.` stays the root object.
#
#  * Checks 1, 3 and 4 assume a CAPPED, TWO-LINE response. The dev member caller
#    is uncapped with zero spend (`budget_configs` is empty in dev), so
#    `binding` and `combined_informational` are both `null` BY DESIGN
#    (me_routes.py: an uncapped line cannot bind — rule 6 — and a "combined"
#    total over one line is that line restated). `null | tonumber` is a hard jq
#    error, so check 3 as written cannot pass in dev regardless of correctness.
#    Corrected two ways: (a) null-tolerant guards so the live call asserts the
#    invariant where it is observable, and (b) the SAME literal filters are
#    additionally run against a real capped two-line response body captured from
#    the #4399 worked fixture, which is where the invariant actually has teeth.
# ---------------------------------------------------------------------------
set -uo pipefail

TARGET_ACCOUNT=879318057152
REGION=us-east-1
NS=adp-gateway
DEPLOY=bedrockgateway            # NOT deploy/adp-gateway — that does not exist
BASE=${BASE:-fe72cdc}            # wave-2 base = parent of #4399's merge commit
HEAD_SHA=${HEAD_SHA:-1ce5e538bd49e8289a4ec2454c92c789f92f7eeb}
PR_4399=4436; PR_4400=4453
MC_4400=1ce5e538bd49e8289a4ec2454c92c789f92f7eeb
LIVE=1; [[ "${1:-}" == "--no-live" ]] && LIVE=0
REPO_ROOT="$(git rev-parse --show-toplevel)"
GW="$REPO_ROOT/modules/gateway"

PASS=0; FAIL=0
ok()  { echo "  PASS - $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL - $1"; FAIL=$((FAIL+1)); }
head_() { echo; echo "-- Check $1: $2"; }

bind_creds() {
  export AWS_ROLE_ARN="arn:aws:iam::${TARGET_ACCOUNT}:role/adp-dev-agent-scaledjob-role"
  export AWS_WEB_IDENTITY_TOKEN_FILE="/var/run/secrets/eks.amazonaws.com/serviceaccount/token"
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}

# ---------------------------------------------------------------- live checks
if [[ $LIVE -eq 1 ]]; then
  bind_creds
  head_ 0 "precondition - correct account, and wave 2 actually DEPLOYED"
  acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
  [[ "$acct" == "$TARGET_ACCOUNT" ]] && ok "on target account $acct" \
    || { bad "expected $TARGET_ACCOUNT, got '${acct:-none}'"; echo "Refusing to evaluate the wrong account."; exit 1; }
  aws eks update-kubeconfig --name adp-dev-eks-cluster --region "$REGION" >/dev/null 2>&1
  img=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  # merged-but-not-deployed is the failure mode this guards
  [[ "$img" == *"$MC_4400"* ]] && ok "running image is the #4400 merge commit (${img##*:})" \
    || bad "running image (${img:-unknown}) is not the wave-2 merge commit - merged but not deployed"

  # Setup: an empty CF or TOKEN turns every live check into a false pass.
  CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text 2>/dev/null)
  aws secretsmanager get-secret-value --secret-id adp/dev/gateway/test-user-credentials \
    --query SecretString --output text > /tmp/w2_sec.json 2>/dev/null
  # password contains CLI-special chars -> pass auth params via a JSON file, never inline
  python3 -c 'import json;d=json.load(open("/tmp/w2_sec.json"));json.dump({"USERNAME":d["username"],"PASSWORD":d["password"]},open("/tmp/w2_ap.json","w"));open("/tmp/w2_cid","w").write(d["cognito_client_id"])'
  aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id "$(cat /tmp/w2_cid)" --auth-parameters file:///tmp/w2_ap.json \
    --region "$REGION" > /tmp/w2_auth.json 2>/dev/null
  TOKEN=$(python3 -c 'import json;print(json.load(open("/tmp/w2_auth.json"))["AuthenticationResult"]["IdToken"])' 2>/dev/null)
  rm -f /tmp/w2_sec.json /tmp/w2_ap.json /tmp/w2_auth.json /tmp/w2_cid
  if [[ -z "$CF" || -z "$TOKEN" ]]; then
    echo "STOP: CF or TOKEN empty - every live check would be a false pass."; exit 1
  fi
  echo "  setup: CF=$CF (${#CF} chars), member TOKEN ${#TOKEN} chars - both non-empty"

  B="https://$CF/api/me/budget"
  AUTH=(-H "Authorization: Bearer $TOKEN")
  curl -s "${AUTH[@]}" "$B?period_type=monthly"      > /tmp/w2_env.json
  curl -s "${AUTH[@]}" "$B/runs?period_type=monthly" > /tmp/w2_runs.json

  # DEVIATION (see header): null-tolerant. `binding` is null when nothing in the
  # caller's hierarchy is capped; the invariant is "the headline never names a
  # synthesised entity", asserted on the flat headline too, which is always present.
  head_ 1 "headline names one of the returned lines, not a synthesised entity (FR-2.2)"
  jq -e '((.binding == null) or (.binding.entity_type as $b | ([.lines[].entity_type] | index($b)) != null))
         and (.entity_type as $e | ([.lines[].entity_type] | index($e)) != null)' /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "binding (and the flat headline) name a real line" || bad "headline names an entity absent from lines[]"

  # DEVIATION (see header): parenthesised length so `.` stays the root object.
  head_ 2 "an uncapped line is never selected as binding (#4399 check 5)"
  jq -e '([.lines[] | select(.cap_status == "capped")] | length) as $n | ($n == 0) or (.binding.cap_status == "capped")' \
    /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "no capped lines, or binding is itself capped" || bad "an uncapped line was selected as binding"

  # DEVIATION (see header): disjuncts reordered so the single-line case
  # short-circuits BEFORE `null | tonumber` raises. Same assertion, evaluable.
  head_ 3 "R3 GATE - the headline is not the sum of the lines (FR-2.3)"
  jq -e '([.lines[]] | length == 1) or ((.binding.spend_usd | tonumber) != ([.lines[].spend_usd | tonumber] | add))' \
    /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "headline is a selected line, not the total" || bad "headline equals the sum of lines[].spend_usd"

  # DEVIATION (see header): null-tolerant. CombinedInformational is None for a
  # single-line envelope; when present, is_budget is Literal[False] and the model
  # has no cap field at all (schemas.py), so the rule is carried by the type.
  head_ 4 "the combined figure carries no cap denominator (FR-2.4)"
  jq -e '(.combined_informational == null)
         or (.combined_informational.is_budget == false and (.combined_informational | has("cap_usd") | not))' \
    /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "combined absent, or present with is_budget=false and no cap_usd" || bad "combined figure presented as a budget"

  head_ 5 "every line declares a principal kind (FR-2.5)"
  jq -e '[.lines[].principal_kind] | all(. == "human" or . == "service")' /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "all lines are human or service" || bad "a line has no/invalid principal_kind"

  head_ 6 "bands come from the server threshold set (FR-5.3)"
  jq -e '[.lines[] | select(.cap_status == "capped") | .band] | all(. == "none" or . == "warning" or . == "critical" or . == "exceeded")' \
    /tmp/w2_env.json >/dev/null 2>&1 \
    && ok "all capped-line bands are in the server vocabulary" || bad "a band is outside the server threshold set"

  head_ 7 "GET /api/me/budget/runs smoke (#4400)"
  jq -e '.items and .subtotal.status' /tmp/w2_runs.json >/dev/null 2>&1 \
    && ok "items + subtotal.status present" || bad "runs smoke shape missing"

  head_ 8 "every run cost is one of the three contract values (FR-3.4/3.5)"
  jq -e '[.items[].cost.status] | all(. == "known" or . == "none_incurred" or . == "unknown")' /tmp/w2_runs.json >/dev/null 2>&1 \
    && ok "no bare numeric cost" || bad "a run cost is not a three-valued status"

  head_ 9 "runs?period_type=run is 422, never 500 (FR-3.6)"
  c=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$B/runs?period_type=run")
  [[ "$c" == "422" ]] && ok "422" || bad "expected 422, got $c"

  head_ 10 "injected user_id is ignored - own scope only (FR-3.3)"
  INJ=$(curl -s "${AUTH[@]}" "$B/runs?period_type=monthly&user_id=00000000-0000-0000-0000-000000000000")
  CLEAN=$(cat /tmp/w2_runs.json)
  if echo "$INJ" | jq -e '.items | type == "array"' >/dev/null 2>&1 \
     && [[ "$(echo "$INJ" | grep -c '00000000-0000-0000-0000-000000000000')" == "0" ]] \
     && [[ "$INJ" == "$CLEAN" ]]; then
    ok "items is an array, injected id absent, output identical to un-injected call"
  else
    bad "injected user_id influenced the response - IDOR"
  fi
else
  echo "(--no-live: skipping account/deploy/API checks)"
fi

# --------------------------------------------------- capped-state reassertion
# Checks 1-6 above are weakest exactly where they matter most: the dev caller is
# uncapped, so binding/combined are null. Re-run the LITERAL #4411 filters (only
# check 2's malformed-jq bug corrected) against a real capped two-line response
# built from #4399's worked fixture, where the R3 gate has teeth.
head_ "1-6 (capped)" "same invariants on a real CAPPED two-line response"
if [[ -x /tmp/venv/bin/python ]]; then PY=/tmp/venv/bin/python; else PY=python3; fi
cat > /tmp/w2_dump_test.py <<'PYEOF'
import json, pytest
# Star-import is REQUIRED, not sloppiness: `session` and `caller_user_row` are
# module-local fixtures of test_envelope_composition.py, not conftest fixtures.
# Importing only the helpers yields "fixture 'session' not found".
from tests.budget.test_envelope_composition import *  # noqa: F401,F403
from tests.budget.test_envelope_composition import seed_worked_example, build_app, caller_context, client_for

@pytest.mark.asyncio
async def test_dump(session, caller_user_row):
    await seed_worked_example(session)
    app = build_app(session, caller_context())
    async with client_for(app) as client:
        body = (await client.get("/me/budget")).json()
    open("/tmp/w2_capped.json", "w").write(json.dumps(body, indent=2))
PYEOF
cp /tmp/w2_dump_test.py "$GW/tests/budget/test_zz_w2_dump.py"
( cd "$GW" && timeout 600 $PY -m pytest tests/budget/test_zz_w2_dump.py -q -p no:warnings >/dev/null 2>&1 )
rm -f "$GW/tests/budget/test_zz_w2_dump.py"
if [[ -s /tmp/w2_capped.json ]]; then
  n=0
  jq -e '.binding.entity_type as $b | ([.lines[].entity_type] | index($b)) != null' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 1 failed"
  jq -e '([.lines[] | select(.cap_status == "capped")] | length) as $q | ($q == 0) or (.binding.cap_status == "capped")' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 2 failed"
  jq -e '(.binding.spend_usd | tonumber) != ([.lines[].spend_usd | tonumber] | add) or ([.lines[]] | length == 1)' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 3 failed"
  jq -e '.combined_informational.is_budget == false and (.combined_informational | has("cap_usd") | not)' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 4 failed"
  jq -e '[.lines[].principal_kind] | all(. == "human" or . == "service")' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 5 failed"
  jq -e '[.lines[] | select(.cap_status == "capped") | .band] | all(. == "none" or . == "warning" or . == "critical" or . == "exceeded")' /tmp/w2_capped.json >/dev/null 2>&1 && n=$((n+1)) || echo "     capped check 6 failed"
  # the R3 gate, positively: headline is one line's figure and NOT the forbidden total
  jq -e '(.binding.spend_usd | tonumber) == 264.6 and (([.lines[].spend_usd | tonumber] | add) == 412.8)' /tmp/w2_capped.json >/dev/null 2>&1 \
    && echo "     R3 witness: headline=264.60, sum=412.80 - distinct, as required" \
    || echo "     R3 witness could not be established"
  [[ $n -eq 6 ]] && ok "all 6 envelope invariants hold on a capped two-line response" || bad "only $n/6 capped-state invariants held"
else
  bad "could not capture a capped-state response body"
fi

# ------------------------------------------------------- unit tests + coverage
head_ 3b "the 412.80 negative-gate assertion is present"
cnt=$(grep -c '412.80' "$GW/tests/budget/test_envelope_composition.py")
[[ "$cnt" -ge 1 ]] && ok "grep -c '412.80' = $cnt (>= 1)" || bad "the forbidden-total assertion is absent"

head_ 11 "CI 'lint' and 'test' green on the merge commit of BOTH #4399 and #4400"
for PR in $PR_4399 $PR_4400; do
  # `--required` reports "no required checks" on this repo (no branch protection),
  # so assert on the named Lint/Test contexts directly.
  out=$(gh pr checks "$PR" 2>/dev/null | grep -E '^(Lint|Test)\s')
  n_ok=$(echo "$out" | grep -cE '\spass\s')
  [[ "$n_ok" -eq 2 ]] && ok "PR #$PR: Lint + Test both pass" || bad "PR #$PR: Lint/Test not both green ($n_ok/2)"
done

head_ 12 "coverage gates - composition >= 95% (#4399), drill-down >= 85% (#4400)"
covout=$( cd "$GW" && timeout 1200 $PY -m pytest tests/budget/test_envelope_composition.py tests/budget/test_me_budget_runs.py \
  --cov=src.budget.me_routes --cov-report=term -q -p no:warnings 2>&1 | grep -E 'me_routes\.py' )
pct=$(echo "$covout" | grep -oE '[0-9]+%' | tr -d '%' | head -1)
if [[ -n "$pct" && "$pct" -ge 95 ]]; then
  ok "me_routes.py (both wave-2 modules live here) at ${pct}% - clears 95% and 85%"
else
  bad "coverage ${pct:-unknown}% below the 95% gate"
fi

head_ "unit" "all wave-2 unit tests pass"
tout=$( cd "$GW" && timeout 1200 $PY -m pytest tests/budget/test_envelope_composition.py tests/budget/test_me_budget_runs.py -q -p no:warnings 2>&1 | tail -1 )
echo "     $tout"
echo "$tout" | grep -qE '^[0-9]+ passed' && ok "wave-2 suites green ($tout)" || bad "wave-2 unit tests not green"

# ----------------------------------------------- cumulative source constraints
head_ 13 "enforcement untouched (NFR-2 / frozen E-1 option a)"
for f in modules/gateway/src/budget/enforcement_service.py modules/gateway/src/budget/routes.py; do
  d=$( cd "$REPO_ROOT" && git diff --stat "$BASE".."$HEAD_SHA" -- "$f" )
  [[ -z "$d" ]] && ok "empty diff: $(basename $f)" || bad "$(basename $f) changed: $d"
done

head_ 14 "no write-path change to the root_user ledger (frozen E-1 option a)"
d=$( cd "$REPO_ROOT" && git diff --stat "$BASE".."$HEAD_SHA" -- modules/gateway/lambda/budget-usage-tracker/ )
[[ -z "$d" ]] && ok "budget-usage-tracker/ diff is empty - reads only" || bad "usage-tracker changed: $d"

head_ 15 "no unfiltered rollup reintroduced (FR-1.6)"
n=$( cd "$REPO_ROOT" && git diff "$BASE".."$HEAD_SHA" | grep -c 'get_organization_budget_overview' )
[[ "$n" -eq 0 ]] && ok "grep -c get_organization_budget_overview = 0" || bad "unfiltered rollup referenced $n time(s)"

echo
echo "=================================================="
echo " PASS=$PASS  FAIL=$FAIL"
echo "=================================================="
[[ $FAIL -eq 0 ]] || exit 1

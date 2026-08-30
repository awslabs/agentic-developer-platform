#!/usr/bin/env bash
# evaluate-wave-3.sh — deterministic post-deploy eval for EPIC #4324 wave 3
# Stories: #4401 (U-4 managed-scope read API), #4402 (U-5 Budget & Spend dashboard)
# Eval issue: #4412.  Target: adp-dev-embark1 (account 879318057152, us-east-1).
#
# Usage:  ./evaluate-wave-3.sh            # full run (live + git + CI + unit)
#         ./evaluate-wave-3.sh --no-live  # git/CI/unit checks only
#
# Credentials are NEVER ambient: binds the IRSA web-identity chain (auto-refresh),
# per aidlc/spaces/issue-4120/deploy-target.md (ruling D-R11, adopted by #4324).
#
# NOTE ON THE DEPLOY-TARGET PATH: #4412 cites
# `inception/delivery-planning/deploy-target.md` on branch `agent/issue-4324`.
# That path does not exist on that branch. The file D-R11 actually governs is
# `aidlc/spaces/issue-4120/deploy-target.md`, which #4324's delivery-planning
# adopts BY REFERENCE rather than retyping (that is the entire point of D-R11).
# Literals below are verified against live STS, not copied from the issue.
#
# ---------------------------------------------------------------------------
# FOUR OF #4412's CHECKS ARE MIS-AUTHORED. They are corrected here, with the
# deviation and its justification recorded inline. All four are EVAL-AUTHORING
# bugs, not wave-3 defects — the same class wave 1 hit on 3 checks (#4410) and
# wave 2 on 3 checks (#4411) — so per the defect protocol no defect issue is
# filed and no developer is dispatched. Each is grounded in the shipped
# `schemas.py` / test suite, which are the contracts of record.
#
#  * Check 6 asserts `.binding and .lines` on a MANAGED-SCOPE response.
#    `ManagedScopeBudgetResponse` (schemas.py) has fields
#    `period, entity_type, entity_id, line, binding, rollup` — there is NO
#    `lines` field, so `.lines` is always `null` and the filter can NEVER exit 0.
#    `lines[]` belongs to `MyBudgetResponse` (the /me/ surface, U-2), a different
#    model. Additionally `binding` is `null` for an UNCAPPED target by contract
#    rule 6 ("an uncapped line cannot bind"), which is the dev state — so even
#    `.binding` alone cannot pass here. Corrected to the field that actually
#    carries the figures (`.line`) plus the echoed target, and the binding rule is
#    asserted in its real, null-tolerant form. Story test T7 asserts exactly this
#    shape (`body["line"]["spend_usd"]`, `body["rollup"] == []`).
#
#  * Check 7 is a FALSE PASS as written. `[.. | objects | select(has(
#    "principal_kind"))]` recurses the WHOLE document, so it matches the single
#    `.line` object even when `rollup` is EMPTY — and in dev `rollup` IS empty
#    (one org member, no ledger rows). The check reports PASS while proving
#    nothing about rollup rows, which is what FR-2.5 is about. Corrected two
#    ways: (a) the live call additionally asserts on `.rollup[]` specifically,
#    and (b) the same filter is re-run against a real NON-EMPTY 4-row rollup
#    captured from #4401's seeded fixture, where the assertion has teeth.
#
#  * Check 3 expects `403` for a `dept_admin`/`org_admin` reading another org.
#    The only admin identity provisioned in dev is a PLATFORM admin
#    (`custom:role=platform_admin`, `cognito:groups=[platform_admin,...]`), and a
#    platform admin is scoped to EVERY org BY DESIGN — `access_control.is_org_admin`
#    returns True unconditionally for `PLATFORM_ADMIN`, and story test T7d
#    ("platform admin reads across tenants") asserts 200 for precisely this call.
#    So the live 200 is CORRECT behaviour, not a leak. The scoped identities the
#    check needs (`org_admin_a`/`org_admin_b`, deliberately in different orgs)
#    are defined in #4444's actor matrix but are NOT YET PROVISIONED in the dev
#    pool — the token lambda fails closed with UserNotFoundException. Corrected
#    by asserting the property where scoped identities exist: story tests
#    T3/T3b-e (dept_admin cross-department) and T4/T4b-c (org_admin cross-org),
#    all of which must pass. Recorded as an eval/environment gap, NOT a defect.
#
#  * Checks 12/13 UNDER-SPECIFY the frontend type. Their stated rule is "every
#    field in the frontend type MUST exist in the live response", but the
#    enumerated key lists are SHORTER than what `types/budget.ts` declares:
#    `BudgetEnvelopeResponse` also declares `entity_type, cap_usd, spend_usd,
#    remaining_usd, utilization_pct, band, cap_status, identity_status`, and
#    `BudgetLine` also declares `enforcement_mode`. Asserting only the short list
#    would leave 9 fields unvalidated — exactly the #3675 hole these checks exist
#    to close. Corrected to assert the FULL declared field set (a strict superset
#    of the issue's list), parsed from the .ts file rather than retyped.
# ---------------------------------------------------------------------------
set -uo pipefail

TARGET_ACCOUNT=879318057152
REGION=us-east-1
NS=adp-gateway
DEPLOY=bedrockgateway            # NOT deploy/adp-gateway — that does not exist
CLUSTER=adp-dev-eks-cluster      # NOT adp-dev-cyber-eks, also in this account
BASE=${BASE:-07a80f41}           # wave-3 base = parent of #4485's merge commit
HEAD_SHA=${HEAD_SHA:-86d13d28fff83b2d0a016ff3587fa61a5b6bd163}
PR_4401=4485; PR_4401_FIX=4491; PR_4402=4494
MC_4402=98af98d1                 # #4402 merge commit
MERGE_COMMITS="4bdc9c77 a85e79ec 98af98d1"
LIVE=1; [[ "${1:-}" == "--no-live" ]] && LIVE=0
REPO_ROOT="$(git rev-parse --show-toplevel)"
GW="$REPO_ROOT/modules/gateway"
FE="$GW/frontend"
if [[ -x /tmp/venv/bin/python ]]; then PY=/tmp/venv/bin/python; else PY=python3; fi

PASS=0; FAIL=0
ok()  { echo "  PASS - $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL - $1"; FAIL=$((FAIL+1)); }
head_() { echo; echo "-- Check $1: $2"; }

bind_creds() {
  export AWS_ROLE_ARN="arn:aws:iam::${TARGET_ACCOUNT}:role/adp-dev-agent-scaledjob-role"
  export AWS_WEB_IDENTITY_TOKEN_FILE="/var/run/secrets/eks.amazonaws.com/serviceaccount/token"
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}

# Mint a real Cognito IdToken. The password contains CLI-special characters, so
# auth parameters go via a JSON file — never inline on the command line.
mint_token() {
  local secret=$1 out=$2
  aws secretsmanager get-secret-value --secret-id "$secret" \
    --query SecretString --output text > /tmp/w3_sec.json 2>/dev/null
  $PY -c 'import json,sys;d=json.load(open("/tmp/w3_sec.json"));json.dump({"USERNAME":d["username"],"PASSWORD":d["password"]},open("/tmp/w3_ap.json","w"));open("/tmp/w3_cid","w").write(d["cognito_client_id"])' 2>/dev/null
  aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id "$(cat /tmp/w3_cid)" --auth-parameters file:///tmp/w3_ap.json \
    --region "$REGION" > /tmp/w3_auth.json 2>/dev/null
  $PY -c 'import json;print(json.load(open("/tmp/w3_auth.json"))["AuthenticationResult"]["IdToken"])' > "$out" 2>/dev/null
  rm -f /tmp/w3_sec.json /tmp/w3_ap.json /tmp/w3_auth.json /tmp/w3_cid
}

# ---------------------------------------------------------------- live checks
if [[ $LIVE -eq 1 ]]; then
  bind_creds
  head_ 0 "precondition - correct account, and wave 3 actually DEPLOYED"
  acct=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
  [[ "$acct" == "$TARGET_ACCOUNT" ]] && ok "on target account $acct" \
    || { bad "expected $TARGET_ACCOUNT, got '${acct:-none}'"; echo "Refusing to evaluate the wrong account."; exit 1; }
  aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" >/dev/null 2>&1
  img=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  running_sha="${img##*:}"
  # merged-but-not-deployed is the failure mode this guards. The running image
  # need not BE a wave-3 merge commit (later commits deploy over it) — it must
  # CONTAIN them, which is an ancestry question, not a string match.
  missing=""
  for c in $MERGE_COMMITS; do
    git merge-base --is-ancestor "$c" "$running_sha" 2>/dev/null || missing="$missing $c"
  done
  [[ -z "$missing" ]] && ok "running image ${running_sha:0:8} contains all wave-3 merges (4bdc9c77, a85e79ec, 98af98d1)" \
    || bad "running image ${running_sha:-unknown} is missing:$missing - merged but not deployed"

  # Setup: an empty CF or TOKEN turns every live check below into a false pass.
  CF=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-domain --query Parameter.Value --output text 2>/dev/null)
  CF_ID=$(aws ssm get-parameter --name /adp/dev/gateway/cloudfront-id --query Parameter.Value --output text 2>/dev/null)
  mint_token adp/dev/gateway/test-user-credentials  /tmp/w3_tok_member
  mint_token adp/dev/gateway/test-admin-credentials /tmp/w3_tok_admin
  TOKEN=$(cat /tmp/w3_tok_member 2>/dev/null)
  ADMIN_TOKEN=$(cat /tmp/w3_tok_admin 2>/dev/null)
  if [[ -z "$CF" || -z "$CF_ID" || -z "$TOKEN" || -z "$ADMIN_TOKEN" ]]; then
    echo "STOP: CF/CF_ID/TOKEN/ADMIN_TOKEN empty - every live check would be a false pass."; exit 1
  fi
  echo "  setup: CF=$CF, dist=$CF_ID, member TOKEN ${#TOKEN} chars, ADMIN_TOKEN ${#ADMIN_TOKEN} chars - all non-empty"

  AUTH=(-H "Authorization: Bearer $TOKEN")
  AADMIN=(-H "Authorization: Bearer $ADMIN_TOKEN")
  SCOPE="https://$CF/api/budget/scope"

  # Resolve REAL subjects. #4412 leaves these as <other-member-id> placeholders;
  # a denial against a nonexistent id proves nothing (it would 403 either way),
  # so every id below is a row that actually exists and is printed for audit.
  CALLER_SUB=$($PY -c 'import base64,json;t=open("/tmp/w3_tok_member").read().strip();p=t.split(".")[1];p+="="*(-len(p)%4);print(json.loads(base64.urlsafe_b64decode(p))["sub"])')
  IN_ORG=adp-platform
  OTHER_MEMBER=$(curl -s "${AADMIN[@]}" "https://$CF/api/admin/organizations/$IN_ORG/users" | jq -r '.items[0].id')
  OTHER_ORG=iankoulski
  XTENANT_MEMBER=$(curl -s "${AADMIN[@]}" "https://$CF/api/admin/organizations/$OTHER_ORG/users" | jq -r '.items[0].id')
  if [[ -z "$OTHER_MEMBER" || "$OTHER_MEMBER" == "null" || -z "$XTENANT_MEMBER" || "$XTENANT_MEMBER" == "null" ]]; then
    echo "STOP: could not resolve real target ids - a denial against a nonexistent id proves nothing."; exit 1
  fi
  echo "  subjects: caller=${CALLER_SUB:0:8}.. other-member=${OTHER_MEMBER:0:8}.. (org $IN_ORG)  cross-tenant=${XTENANT_MEMBER:0:8}.. (org $OTHER_ORG)"

  head_ 1 "cross-member denial is 403 AND carries no entity metadata (FR-4.2)"
  c=$(curl -s -o /tmp/w3_c1.json -w '%{http_code}' "${AUTH[@]}" "$SCOPE/user/$OTHER_MEMBER?period_type=monthly")
  if [[ "$c" == "403" ]] && ! jq -e 'has("cap_usd") or has("spend_usd") or has("label")' /tmp/w3_c1.json >/dev/null 2>&1; then
    ok "403 with no cap_usd/spend_usd/label - not even existence leaks"
  else
    bad "expected metadata-free 403, got $c body=$(head -c 200 /tmp/w3_c1.json)"
  fi

  head_ 2 "cross-tenant denial is 403 (#4401 check 2)"
  c=$(curl -s -o /tmp/w3_c2.json -w '%{http_code}' "${AUTH[@]}" "$SCOPE/user/$XTENANT_MEMBER?period_type=monthly")
  # Same uniform body as check 1: a denial that differs for another TENANT versus
  # another MEMBER is itself an information leak.
  if [[ "$c" == "403" ]] && diff -q /tmp/w3_c1.json /tmp/w3_c2.json >/dev/null 2>&1; then
    ok "403, byte-identical to the cross-member denial - uniform"
  else
    bad "expected 403 identical to check 1, got $c body=$(head -c 200 /tmp/w3_c2.json)"
  fi

  # DEVIATION (see header): the only dev admin is a PLATFORM admin, which is
  # scoped to every org BY DESIGN (T7d asserts 200 for this exact call). The
  # scoped org_admin/dept_admin identities this check needs are unprovisioned.
  # The 200 is therefore recorded as EXPECTED, and the cross-scope property is
  # asserted below (check 3-unit) where scoped identities exist.
  head_ 3 "cross-scope admin read - platform admin is org-unscoped BY DESIGN"
  c=$(curl -s -o /tmp/w3_c3.json -w '%{http_code}' "${AADMIN[@]}" "$SCOPE/org/$OTHER_ORG?period_type=monthly")
  role=$($PY -c 'import base64,json;t=open("/tmp/w3_tok_admin").read().strip();p=t.split(".")[1];p+="="*(-len(p)%4);d=json.loads(base64.urlsafe_b64decode(p));print(d.get("custom:role",""))')
  if [[ "$role" == "platform_admin" && "$c" == "200" ]]; then
    ok "200 for a platform_admin reading org '$OTHER_ORG' - correct (T7d); scoped-admin denial asserted in check 3-unit"
  elif [[ "$c" == "403" ]]; then
    ok "403 - caller is scoped (role='$role') and was denied"
  else
    bad "role='$role' got $c - a NON-platform admin returning 200 cross-scope is a leak"
  fi

  head_ 4 "falsy target path denies rather than skips (FR-4.3)"
  c=$(curl -s -o /dev/null -w '%{http_code}' "${AADMIN[@]}" "$SCOPE/org/?period_type=monthly")
  [[ "$c" == "403" || "$c" == "404" || "$c" == "422" ]] && ok "$c (never 200)" \
    || bad "expected 403/404/422, got $c - a falsy id must not fall through to allow"

  head_ 5 "entity-type allow-list rejects 'run' with 422 (#4401 check 8)"
  c=$(curl -s -o /dev/null -w '%{http_code}' "${AADMIN[@]}" "$SCOPE/run/abc?period_type=monthly")
  [[ "$c" == "422" ]] && ok "422 at the HTTP boundary, before any handler code" || bad "expected 422, got $c"

  # DEVIATION (see header): `.lines` does not exist on ManagedScopeBudgetResponse
  # (that is the /me/ model). Asserting the real fields: `.line` carries the
  # figures, `.entity_id` echoes the target, and `binding` is null-tolerant
  # because an uncapped target cannot bind (contract rule 6). Matches test T7.
  head_ 6 "in-scope happy path returns the target's figures (FR-4.1)"
  c=$(curl -s -o /tmp/w3_c6.json -w '%{http_code}' "${AADMIN[@]}" "$SCOPE/user/$OTHER_MEMBER?period_type=monthly")
  if [[ "$c" == "200" ]] && jq -e --arg id "$OTHER_MEMBER" \
       '.entity_id == $id and .entity_type == "user" and (.line.spend_usd | type == "string")
        and ((.line.cap_status == "uncapped" and .binding == null) or (.binding.cap_status == "capped"))
        and (.rollup | type == "array")' /tmp/w3_c6.json >/dev/null 2>&1; then
    ok "200, echoes target, line.spend_usd is a string, binding obeys rule 6 ($(jq -rc '{cap_status:.line.cap_status,spend:.line.spend_usd,binding:(.binding!=null)}' /tmp/w3_c6.json))"
  else
    bad "in-scope read wrong: code=$c body=$(head -c 250 /tmp/w3_c6.json)"
  fi

  # DEVIATION (see header): the issue's recursive filter matches `.line` and so
  # passes on an EMPTY rollup. Asserting on .rollup[] specifically; the non-empty
  # case is re-asserted below against a seeded fixture.
  head_ 7 "rollup rows declare a principal kind (FR-2.5)"
  c=$(curl -s -o /tmp/w3_c7.json -w '%{http_code}' "${AADMIN[@]}" "$SCOPE/org/$IN_ORG?period_type=monthly")
  n_roll=$(jq '.rollup | length' /tmp/w3_c7.json 2>/dev/null)
  if [[ "$c" == "200" ]] && jq -e '(.rollup | type == "array") and ([.rollup[].principal_kind] | all(. == "human" or . == "service"))' /tmp/w3_c7.json >/dev/null 2>&1; then
    if [[ "${n_roll:-0}" -gt 0 ]]; then
      ok "$n_roll rollup row(s), every principal_kind in {human,service}"
    else
      ok "rollup is empty in dev (no ledger rows) - vacuously true here; teeth are in check 7-seeded below"
    fi
  else
    bad "rollup principal_kind invalid: code=$c body=$(head -c 250 /tmp/w3_c7.json)"
  fi

  head_ 8 "the Budget & Spend screen is served (#4402 smoke)"
  c=$(curl -s -o /dev/null -w '%{http_code}' "https://$CF/budget")
  n_root=$(curl -s "https://$CF/budget" | grep -c '<div id="root"')
  [[ "$c" == "200" && "$n_root" == "1" ]] && ok "HTTP 200 with exactly one '<div id=\"root\"' - SPA shell, not an S3 error page" \
    || bad "expected 200 + 1 root div, got code=$c roots=$n_root"

  head_ 9 "frontend was actually republished (the CloudFront staleness trap)"
  read -r inv_status inv_time < <(aws cloudfront list-invalidations --distribution-id "$CF_ID" \
    --query 'InvalidationList.Items[0].[Status,CreateTime]' --output text 2>/dev/null)
  merge_time=$(git show -s --format=%cI "$MC_4402" 2>/dev/null)
  inv_epoch=$($PY -c "import datetime;print(int(datetime.datetime.fromisoformat('${inv_time/Z/+00:00}').timestamp()))" 2>/dev/null)
  mrg_epoch=$($PY -c "import datetime;print(int(datetime.datetime.fromisoformat('${merge_time/Z/+00:00}').timestamp()))" 2>/dev/null)
  if [[ "$inv_status" == "Completed" && -n "$inv_epoch" && "$inv_epoch" -gt "$mrg_epoch" ]]; then
    ok "invalidation Completed at $inv_time, after #4402's merge at $merge_time"
  else
    bad "status=$inv_status created=$inv_time vs #4402 merge=$merge_time - a frontend change without an invalidation looks unshipped"
  fi

  # ---------------------------------------------- live API-contract (Rule 5)
  curl -s "${AUTH[@]}" "https://$CF/api/me/budget?period_type=monthly"      > /tmp/w3_env.json
  curl -s "${AUTH[@]}" "https://$CF/api/me/budget/runs?period_type=monthly" > /tmp/w3_runs.json

  # DEVIATION (see header): assert the FULL declared field set, parsed out of the
  # .ts file, not the issue's shorter hand-typed list. The rule is "every field in
  # the frontend type", so the type file is the authority on what that set is.
  head_ 12 "live API-contract - every BudgetEnvelopeResponse field exists live (Rule 5)"
  $PY - <<'PYEOF'
import json, re, sys
src = open("modules/gateway/frontend/src/types/budget.ts").read()
def fields(iface):
    m = re.search(r"export interface " + iface + r"\s*\{(.*?)\n\}", src, re.S)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    return sorted(set(re.findall(r"^\s{2}(\w+)\??\s*:", body, re.M)))
declared = fields("BudgetEnvelopeResponse")
live = sorted(json.load(open("/tmp/w3_env.json")).keys())
missing = [f for f in declared if f not in live]
print(f"     declared in .ts ({len(declared)}): {','.join(declared)}")
print(f"     live keys      ({len(live)}): {','.join(live)}")
# The issue's own (shorter) list must also hold — assert it explicitly too.
issue_list = ["binding","combined_informational","enforcement_mode","freshness","lines","period"]
issue_missing = [f for f in issue_list if f not in live]
print(f"     issue's 6-key subset missing: {issue_missing or 'none'}")
sys.exit(1 if (missing or issue_missing) else 0)
PYEOF
  [[ $? -eq 0 ]] && ok "all declared BudgetEnvelopeResponse fields present live" \
    || bad "a frontend-declared envelope field is missing/renamed on the wire"

  head_ 13 "live API-contract - every BudgetLine field exists on live lines[] (Rule 5, nested)"
  $PY - <<'PYEOF'
import json, re, sys
src = open("modules/gateway/frontend/src/types/budget.ts").read()
m = re.search(r"export interface BudgetLine\s*\{(.*?)\n\}", src, re.S)
body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
body = re.sub(r"//.*", "", body)
declared = sorted(set(re.findall(r"^\s{2}(\w+)\??\s*:", body, re.M)))
lines = json.load(open("/tmp/w3_env.json"))["lines"]
if not lines:
    print("     live lines[] is EMPTY - cannot validate nested keys"); sys.exit(1)
live = sorted({k for l in lines for k in l})
missing = [f for f in declared if f not in live]
print(f"     declared in .ts ({len(declared)}): {','.join(declared)}")
print(f"     live line keys  ({len(live)}): {','.join(live)}")
issue_list = ["band","cap_status","cap_usd","entity_type","label","principal_kind","remaining_usd","source","spend_usd","utilization_pct"]
issue_missing = [f for f in issue_list if f not in live]
print(f"     issue's 10-key subset missing: {issue_missing or 'none'}")
sys.exit(1 if (missing or issue_missing) else 0)
PYEOF
  [[ $? -eq 0 ]] && ok "all declared BudgetLine fields present on live lines[]" \
    || bad "a frontend-declared line field is missing/renamed on the wire"

  head_ 14 "fixture provenance - fixture keys are a SUBSET of live keys (#3675 guard)"
  # `vite-node -` does NOT read a script from stdin (it prints its usage and
  # exits 1), so the extractor is written to a temp file inside the frontend tree
  # — it must live there to resolve the `@/` alias and the TS transform.
  cat > "$FE/w3-extract-fixtures.mjs" <<'JSEOF'
// Import the real fixture module rather than regex-parsing it, so what is
// validated is exactly what the MSW handlers and unit tests consume.
import * as m from './src/mocks/data/budgetSpend.ts';
const uniq = (a) => [...new Set(a)].sort();
console.log(JSON.stringify({
  envelope_top:       Object.keys(m.mockBudgetEnvelope).sort(),
  envelope_line:      uniq(m.mockBudgetEnvelope.lines.flatMap(Object.keys)),
  envelope_period:    Object.keys(m.mockBudgetEnvelope.period).sort(),
  envelope_freshness: Object.keys(m.mockBudgetEnvelope.freshness).sort(),
  envelope_combined:  Object.keys(m.mockBudgetEnvelope.combined_informational).sort(),
  runs_top:           Object.keys(m.mockBudgetRuns).sort(),
  runs_subtotal:      Object.keys(m.mockBudgetRuns.subtotal).sort(),
  runs_item:          uniq(m.mockBudgetRuns.items.flatMap(Object.keys)),
  runs_cost:          uniq(m.mockBudgetRuns.items.flatMap(i => Object.keys(i.cost))),
}));
JSEOF
  ( cd "$FE" && NODE_ENV=test npx --no-install vite-node w3-extract-fixtures.mjs > /tmp/w3_fixt.json 2>/dev/null )
  rm -f "$FE/w3-extract-fixtures.mjs"
  if [[ -s /tmp/w3_fixt.json ]]; then
    BG_TOKEN_SECRET_KEY=eval-only-not-a-secret $PY - <<'PYEOF'
import importlib.util, json, sys
fx = json.load(open("/tmp/w3_fixt.json"))
env = json.load(open("/tmp/w3_env.json")); runs = json.load(open("/tmp/w3_runs.json"))
bad = []
def sub(name, fixture, live):
    extra = sorted(set(fixture) - set(live))
    print(f"     {'ok  ' if not extra else 'FAIL'} {name}: {len(fixture)} fixture keys vs {len(live)} live" + (f"  INVENTED={extra}" if extra else ""))
    if extra: bad.append(name)
sub("envelope top",       fx["envelope_top"],       env.keys())
sub("envelope period",    fx["envelope_period"],    env["period"].keys())
sub("envelope freshness", fx["envelope_freshness"], env["freshness"].keys())
sub("envelope line",      fx["envelope_line"],      {k for l in env["lines"] for k in l})
sub("runs top",           fx["runs_top"],           runs.keys())
sub("runs subtotal",      fx["runs_subtotal"],      runs["subtotal"].keys())
# Live `items[]` is empty whenever the caller's canonical id is unresolved, so the
# nested item/cost shapes are validated against schemas.py — which the issue names
# as the fixture's source of provenance anyway ("or from a captured real response").
spec = importlib.util.spec_from_file_location("bsch", "modules/gateway/src/budget/schemas.py")
mod = importlib.util.module_from_spec(spec); sys.path.insert(0, "modules/gateway")
spec.loader.exec_module(mod)
if runs["items"]:
    sub("runs item", fx["runs_item"], {k for i in runs["items"] for k in i})
    sub("runs cost", fx["runs_cost"], {k for i in runs["items"] for k in i["cost"]})
else:
    print("     note: live items[] empty (identity_status=%s) -> nested shapes checked against schemas.py" % runs.get("identity_status"))
    sub("runs item (schemas.py)", fx["runs_item"], mod.BudgetRunItem.model_fields.keys())
    sub("runs cost (schemas.py)", fx["runs_cost"], mod.CostFigure.model_fields.keys())
sub("combined (schemas.py)", fx["envelope_combined"], mod.CombinedInformational.model_fields.keys())
sys.exit(1 if bad else 0)
PYEOF
    [[ $? -eq 0 ]] && ok "no invented fixture fields - every fixture key exists on the wire/schema" \
      || bad "a fixture declares a field the backend never sends (#3675 class)"
  else
    bad "could not extract fixture keys (is $FE/node_modules installed with devDependencies?)"
  fi
else
  echo "(--no-live: skipping account/deploy/API/CloudFront checks)"
fi

# ------------------------------------------- seeded reassertions (real teeth)
# Checks 6 and 7 are weakest exactly where they matter: the dev org has one
# member and no ledger rows, so `rollup` is empty and every line is uncapped.
# Re-run the same invariants against #4401's seeded fixture, where a 4-row
# rollup with a mixed human/service population actually exists.
head_ "7-seeded" "principal_kind on a real NON-EMPTY rollup (FR-2.5, with teeth)"
cat > "$GW/tests/budget/test_zz_w3_dump.py" <<'PYEOF'
import json, pytest
# Star-import is REQUIRED, not sloppiness: `session` and `seeded` are
# module-local fixtures of test_managed_scope_budget.py, not conftest fixtures.
# Importing only the helpers yields "fixture 'session' not found".
from tests.budget.test_managed_scope_budget import *  # noqa: F401,F403
from tests.budget.test_managed_scope_budget import ORG_ADMIN_SUB, ORG_ID, client_for, context_for

@pytest.mark.asyncio
async def test_dump_org_rollup(session, seeded):
    async with client_for(session, context_for(ORG_ADMIN_SUB)) as client:
        r = await client.get(f"/budget/scope/org/{ORG_ID}?period_type=monthly")
    open("/tmp/w3_rollup.json", "w").write(json.dumps(r.json(), indent=2))
PYEOF
( cd "$GW" && timeout 600 $PY -m pytest tests/budget/test_zz_w3_dump.py -q -p no:warnings >/dev/null 2>&1 )
rm -f "$GW/tests/budget/test_zz_w3_dump.py"
if [[ -s /tmp/w3_rollup.json ]]; then
  n=$(jq '.rollup | length' /tmp/w3_rollup.json)
  if jq -e '(.rollup | length) > 0 and ([.rollup[].principal_kind] | all(. == "human" or . == "service"))
            and ([.rollup[].principal_kind] | index("service")) != null' /tmp/w3_rollup.json >/dev/null 2>&1; then
    ok "$n rollup rows, all human|service, including a 'service' row: $(jq -c '[.rollup[]|{entity_id,principal_kind}]' /tmp/w3_rollup.json)"
  else
    bad "seeded rollup violates FR-2.5: $(jq -c '.rollup' /tmp/w3_rollup.json)"
  fi
  # The literal #4412 filter, for the record: it passes here too, but note it
  # ALSO passes on an empty rollup, which is why it is not the assertion above.
  jq -e '[.. | objects | select(has("principal_kind")) | .principal_kind] | length > 0 and all(. == "human" or . == "service")' \
    /tmp/w3_rollup.json >/dev/null 2>&1 \
    && echo "     (issue's literal recursive filter also passes on this body)" \
    || echo "     (issue's literal recursive filter FAILS on this body)"
else
  bad "could not capture a seeded rollup body"
fi

# DEVIATION (see header): check 3's scoped-admin denial, asserted where scoped
# identities exist. T3* = dept_admin cross-department, T4* = org_admin cross-org.
head_ "3-unit" "scoped-admin cross-scope denial (dept_admin/org_admin) - #4401 checks 3-4"
tout=$( cd "$GW" && timeout 900 $PY -m pytest tests/budget/test_managed_scope_budget.py -q -p no:warnings \
        -k "t3 or t3b or t3c or t3d or t3e or t4 or t4b or t4c" 2>&1 | tail -1 )
echo "     $tout"
echo "$tout" | grep -qE '^8 passed' && ok "all 8 scoped-admin denial tests pass ($tout)" \
  || bad "scoped-admin cross-scope denial not proven: $tout"

# ------------------------------------------------------- unit tests + coverage
head_ 10 "CI green on the merge commits of #4401 (x2) and #4402"
# `gh pr checks --required` reports "no required checks" on this repo (no branch
# protection), so the named contexts are asserted directly. Frontend Test is
# named "Frontend Unit Tests" and build is "Build Container" in this repo.
for PR in $PR_4401 $PR_4401_FIX $PR_4402; do
  out=$(gh pr checks "$PR" 2>/dev/null)
  n_ok=0
  for ctx in "Lint" "Test" "Frontend Unit Tests" "Build Container"; do
    echo "$out" | grep -E "^${ctx}\s" | grep -qE '\spass\s' && n_ok=$((n_ok+1))
  done
  [[ "$n_ok" -eq 4 ]] && ok "PR #$PR: Lint + Test + Frontend Unit Tests + Build Container all pass" \
    || bad "PR #$PR: only $n_ok/4 required contexts green"
done

head_ "11a" "backend coverage - managed-scope module >= 90% AND every 403 branch covered"
covout=$( cd "$GW" && timeout 1800 $PY -m pytest tests/budget/test_managed_scope_budget.py \
  --cov=src.budget.managed_scope_routes --cov-branch \
  --cov-report=json:/tmp/w3_cov.json --cov-report=term -q -p no:warnings 2>&1 | grep -E 'managed_scope_routes\.py' )
pct=$(echo "$covout" | grep -oE '[0-9]+%' | tr -d '%' | head -1)
if [[ -n "$pct" && "$pct" -ge 90 ]]; then
  ok "managed_scope_routes.py at ${pct}% (branch) - clears the 90% gate"
else
  bad "coverage ${pct:-unknown}% below the 90% gate"
fi
# The HARD gate: an uncovered denial branch is the data-leak class this unit
# exists to prevent, so each 403-raising line is checked individually.
$PY - <<'PYEOF'
import ast, json, sys
# Find the 403-raising lines by walking the AST, NOT by grepping text: the
# module's docstrings quote ``raise _deny(...)`` in prose (explaining why _deny
# RETURNS rather than raises), and a text grep counts that comment line as an
# uncovered denial branch — a false failure against documentation.
tree = ast.parse(open("modules/gateway/src/budget/managed_scope_routes.py").read())
deny = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        f = node.exc.func
        if getattr(f, "id", None) == "_deny":
            deny.add(node.lineno)
        elif getattr(f, "id", None) == "HTTPException" or getattr(f, "attr", None) == "HTTPException":
            if any(k.arg == "status_code" and getattr(k.value, "value", None) == 403 for k in node.exc.keywords):
                deny.add(node.lineno)
    # `_deny` itself builds the uniform 403 and returns it.
    if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
        if getattr(node.value.func, "id", None) == "HTTPException":
            if any(k.arg == "status_code" and getattr(k.value, "value", None) == 403 for k in node.value.keywords):
                deny.add(node.lineno)
deny = sorted(deny)
cov = json.load(open("/tmp/w3_cov.json"))["files"]["src/budget/managed_scope_routes.py"]
ex = set(cov["executed_lines"])
uncovered = [l for l in deny if l not in ex]
print(f"     403-producing lines {deny}: {'ALL COVERED' if not uncovered else 'UNCOVERED ' + str(uncovered)}")
print(f"     missing lines overall: {cov['missing_lines']}")
sys.exit(1 if uncovered else 0)
PYEOF
[[ $? -eq 0 ]] && ok "every 403 branch is covered by a test (hard gate)" \
  || bad "a 403 denial branch is uncovered - the data-leak class this unit prevents"

head_ "11b" "frontend coverage - new Budget & Spend components >= 85%"
# Scoped to the files #4402 ADDED. BudgetFormModal.tsx also lives under
# components/budget/ but predates this wave (0% here, covered by its own suite);
# including it would fail the gate on code the story never touched.
covout=$( cd "$FE" && NODE_ENV=test timeout 1200 npx --no-install vitest run --coverage \
  --coverage.reporter=text \
  --coverage.include='src/pages/BudgetSpend.tsx' \
  --coverage.include='src/components/budget/BudgetLines.tsx' \
  --coverage.include='src/components/budget/BudgetRunsTable.tsx' \
  --coverage.include='src/services/budgetSpend.ts' \
  --coverage.include='src/utils/budgetBand.ts' \
  src/__tests__/components/BudgetSpend.test.tsx \
  src/__tests__/components/BudgetLines.test.tsx \
  src/__tests__/services/budgetSpend.test.ts 2>&1 )
echo "$covout" | grep -E '^(Statements|Branches|Functions|Lines)\s+:' | sed 's/^/     /'
fpct=$(echo "$covout" | grep -E '^Statements\s+:' | grep -oE '[0-9]+\.[0-9]+' | head -1)
ftests=$(echo "$covout" | grep -oE 'Tests +[0-9]+ passed' | head -1)
if [[ -n "$fpct" ]] && $PY -c "import sys; sys.exit(0 if float('$fpct') >= 85 else 1)"; then
  ok "wave-3 frontend files at ${fpct}% statements - clears the 85% gate ($ftests)"
else
  bad "frontend coverage ${fpct:-unknown}% below the 85% gate"
fi

head_ "unit" "all wave-3 test suites pass"
tout=$( cd "$GW" && timeout 1800 $PY -m pytest tests/budget/test_managed_scope_budget.py -q -p no:warnings 2>&1 | tail -1 )
echo "     backend:  $tout"
echo "$tout" | grep -qE '^[0-9]+ passed' && ok "backend wave-3 suite green ($tout)" || bad "backend wave-3 tests not green"
echo "$covout" | grep -qE 'Tests +[0-9]+ passed' && ok "frontend wave-3 suites green ($ftests)" || bad "frontend wave-3 tests not green"

# ----------------------------------------------- cumulative source constraints
head_ 15 "forbidden surfaces untouched (NFR-1 / NFR-2)"
for f in modules/gateway/src/budget/routes.py modules/gateway/src/budget/enforcement_service.py; do
  d=$( cd "$REPO_ROOT" && git diff --stat "$BASE".."$HEAD_SHA" -- "$f" )
  [[ -z "$d" ]] && ok "empty diff: $(basename $f)" || bad "$(basename $f) changed: $d"
done

head_ 16 "the stale 50/80 frontend band is not copied (FR-5.3)"
n=$( cd "$REPO_ROOT" && git diff "$BASE".."$HEAD_SHA" -- modules/gateway/frontend/ | grep -E '^\+' | grep -cE 'THRESHOLD[^=]*=\s*(50|80)\b' )
[[ "$n" -eq 0 ]] && ok "no new 50/80 THRESHOLD constant - bands arrive from the API's band field" \
  || bad "a 50/80 band constant was added $n time(s); the screen would disagree with the enforcer"

head_ 17 "no unfiltered rollup reintroduced (FR-1.6)"
# The issue greps the whole diff for the symbol name. That counts PROSE as well
# as code: managed_scope_routes.py's docstring cites the symbol to explain why it
# is NOT used ("nothing here aggregates ... the way get_organization_budget_overview
# does (#4328)"). A documentation citation is not a reintroduction, so the count
# is taken over CODE lines only. The raw count is printed for transparency.
raw=$( cd "$REPO_ROOT" && git diff "$BASE".."$HEAD_SHA" | grep -c 'get_organization_budget_overview' )
code=$( cd "$REPO_ROOT" && git diff "$BASE".."$HEAD_SHA" | grep -E '^\+' | grep 'get_organization_budget_overview' \
        | grep -vE '^\+\s*(#|\*|"""|``)' | grep -vE '``get_organization_budget_overview``' | wc -l )
echo "     raw diff mentions: $raw (prose+code)   call sites: $code"
if [[ "$code" -eq 0 ]]; then
  ok "0 call sites; the $raw mention(s) are docstring prose explaining why it is avoided"
else
  bad "get_organization_budget_overview referenced from code $code time(s)"
fi

echo
echo "=================================================="
echo " PASS=$PASS  FAIL=$FAIL"
echo "=================================================="
[[ $FAIL -eq 0 ]] || exit 1

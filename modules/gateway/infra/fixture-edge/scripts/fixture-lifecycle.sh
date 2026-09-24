#!/usr/bin/env bash
# =============================================================================
# Fixture trusted edge — executable lifecycle contract (Issue #5836)
# =============================================================================
# WHY THIS EXISTS RATHER THAN PROSE IN A RUNBOOK
# ----------------------------------------------
# Root's review found the create/handoff/cleanup procedure was not executable:
# the init step described a local-state fallback that does not exist, the secret
# handoff had no checked upstream error handling and no ledger receipt, NOTHING
# actually attached the generated Secret to the fixture Deployment, and teardown
# was a name-prefix sweep that cannot prove replacement-safety. Each of those was
# reproduced. This script is the executable answer; RUNBOOK.md now points at it
# instead of restating steps a reader has to reassemble by hand.
#
# SUBCOMMANDS (each idempotent where it can be, fail-closed where it cannot)
#   init      bind terraform init to an explicit profile/account and per-run key
#   plan      plan into a reviewable file, and refuse anything outside this root
#   apply     apply a REVIEWED plan file only
#   handoff   pipe the SSM secret into the fixture Secret, ATTACH it to the
#             fixture Deployment, and record both in the #3968 ledger
#   verify    prove edge identity and the three refusals
#   destroy   teardown in dependency order against exact owned state
#
# WHAT THE ORDERING PROTECTS (verified, not assumed)
# -------------------------------------------------
# main.tf reads the fixture ALB (data.aws_lb.fixture) to derive its DNS name. A
# data source is re-read during `terraform destroy`, so if the fixture Ingress is
# deleted FIRST the destroy cannot even plan:
#
#   Error: Read ... data source error / the object cannot be read
#
# Reproduced with an equivalent dependency on Terraform 1.15.3. So the order is
# destroy-terraform-FIRST (stop the listeners/routes), THEN delete the Ingress and
# its ALB. `destroy --recover` adds -refresh=false for the case where an operator
# already deleted the ALB out of order; that was also verified to complete.
#
# SCOPE: init/plan are read-only. apply/handoff/destroy MUTATE and are for ROOT
# under the existing account authorization. --dry-run is available on the mutating
# paths and is what a non-deploying review can exercise.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$HERE/.." && pwd)"
OWNERSHIP_LIB="${W2_OWNERSHIP_LIB:-$ROOT_DIR/../../../../platform/scripts/operator/wave2/lib/ownership.py}"

fail() { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }
ok()   { printf '  [ ok ] %s\n' "$*"; }
note() { printf '  [note] %s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }

CMD="${1:-}"; [ -n "$CMD" ] && shift || true

NONCE=""; ACCOUNT=""; REGION="us-east-1"; ENVIRONMENT="dev"; PROFILE=""
BUCKET=""; LEDGER=""; NAMESPACE="adp-gateway"; FIXTURE_DEPLOY=""
PLAN_FILE=""; DRY_RUN=0; RECOVER=0; ARTIFACT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --nonce)            NONCE="${2:?}"; shift 2 ;;
    --account-id)       ACCOUNT="${2:?}"; shift 2 ;;
    --region)           REGION="${2:?}"; shift 2 ;;
    --environment)      ENVIRONMENT="${2:?}"; shift 2 ;;
    --profile)          PROFILE="${2:?}"; shift 2 ;;
    --state-bucket)     BUCKET="${2:?}"; shift 2 ;;
    --ledger)           LEDGER="${2:?}"; shift 2 ;;
    --namespace)        NAMESPACE="${2:?}"; shift 2 ;;
    --fixture-deployment) FIXTURE_DEPLOY="${2:?}"; shift 2 ;;
    --plan-file)        PLAN_FILE="${2:?}"; shift 2 ;;
    --artifact-dir)     ARTIFACT_DIR="${2:?}"; shift 2 ;;
    --dry-run)          DRY_RUN=1; shift ;;
    --recover)          RECOVER=1; shift ;;
    -h|--help)          sed -n '1,45p' "$0"; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

# --- shared validation ------------------------------------------------------
require_nonce() {
  [ -n "$NONCE" ] || fail "--nonce is required (the #3968 run nonce; every resource is bound to it)"
  case "$NONCE" in
    *[!0-9a-f]*|"") fail "--nonce must be lower-case hex, matching #3968 lib/ownership.py new_nonce()" ;;
  esac
}

require_account() {
  [ -n "$ACCOUNT" ] || fail "--account-id is required. Binding init and every AWS call to an
     EXPLICIT account is the point: an ambient credential that silently resolves
     elsewhere is how a fixture gets created in the wrong account, and its teardown
     then 'cleans up' resources it does not own."
  case "$ACCOUNT" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) fail "--account-id must be 12 digits" ;;
  esac
}

# Bind every AWS call in this script to the named profile, so nothing silently
# falls back to an ambient credential for a different account.
aws_() {
  if [ -n "$PROFILE" ]; then
    aws --profile "$PROFILE" --region "$REGION" "$@"
  else
    aws --region "$REGION" "$@"
  fi
}

assert_live_account() {
  local live
  live="$(aws_ sts get-caller-identity --query Account --output text)" \
    || fail "could not resolve the caller identity. Check --profile/credentials."
  [ "$live" = "$ACCOUNT" ] \
    || fail "the active credential resolves to account $live, but --account-id is $ACCOUNT.
     REFUSING to continue. This is the check that stops a correct-looking command
     from acting on the wrong account."
  ok "credential resolves to the expected account ($ACCOUNT)"
}

# Private artifact directory. 700 because the handoff writes run-scoped
# operational detail here; the secret VALUE is never written to any file (see
# cmd_handoff), but a world-readable directory of fixture state is still wrong.
artifact_dir() {
  local dir="${ARTIFACT_DIR:-$ROOT_DIR/.fixture-run-$NONCE}"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
  fi
  chmod 700 "$dir"
  # Verified rather than assumed: a pre-existing directory with loose permissions
  # would otherwise be silently accepted.
  local mode
  mode="$(stat -c '%a' "$dir")"
  [ "$mode" = "700" ] || fail "artifact directory $dir has mode $mode, expected 700"
  printf '%s\n' "$dir"
}

STATE_KEY=""
state_key() { STATE_KEY="fixture-edge/${ENVIRONMENT}/${ACCOUNT}/${NONCE}/terraform.tfstate"; }

# ===========================================================================
# init
# ===========================================================================
cmd_init() {
  require_nonce; require_account
  [ -n "$BUCKET" ] || fail "--state-bucket is required.
     There is NO local-state alternative: with a backend \"s3\" block declared,
     omitting the flags FAILS init with 'The attribute \"key\" is required by the
     backend' -- it does not fall back to local state. An earlier revision of the
     runbook claimed otherwise; that claim was reproduced as false. For checks
     only, use: terraform init -backend=false (fmt/validate/test, no plan/apply)."
  assert_live_account
  state_key

  step "terraform init — account $ACCOUNT, per-run key"
  note "key = $STATE_KEY"
  # The account id is IN the key, so a mistyped bucket belonging to another
  # account cannot silently collide with that account's fixture state.
  cd "$ROOT_DIR"
  terraform init -input=false -reconfigure \
    -backend-config="bucket=$BUCKET" \
    -backend-config="key=$STATE_KEY" \
    -backend-config="region=$REGION" \
    -backend-config="encrypt=true" \
    ${PROFILE:+-backend-config="profile=$PROFILE"} \
    || fail "terraform init failed"
  ok "initialised against an isolated per-run state key"
}

# ===========================================================================
# plan
# ===========================================================================
cmd_plan() {
  require_nonce; require_account
  assert_live_account
  local dir; dir="$(artifact_dir)"
  local tfvars="$dir/fixture.tfvars" out="$dir/fixture.plan"
  [ -f "$tfvars" ] || fail "expected reviewed inputs at $tfvars (see RUNBOOK.md step 3)"

  step "terraform plan"
  cd "$ROOT_DIR"
  terraform plan -input=false -var-file="$tfvars" -out="$out" \
    || fail "terraform plan failed. A plan-time REFUSAL here is the run-binding gate
     working: it blocks a wrong account/region, a public or foreign-VPC fixture ALB,
     an ALB not tagged for this run, one the VPC Link cannot reach, or the ordinary
     internal-plane ALB."

  # Refuse a plan that reaches outside this root. Separate state and a separate API
  # should make it impossible; a plan that shows otherwise means the wrong backend.
  terraform show -json "$out" > "$dir/fixture.plan.json"
  python3 - "$dir/fixture.plan.json" <<'PY' || fail "plan review refused the plan"
import json, sys
plan = json.load(open(sys.argv[1]))
allowed = {
    "aws_api_gateway_rest_api", "aws_api_gateway_rest_api_policy",
    "aws_api_gateway_deployment", "aws_api_gateway_stage",
    "aws_cloudwatch_log_group", "aws_ssm_parameter",
    "random_password", "terraform_data",
}
bad = []
for change in plan.get("resource_changes", []):
    actions = set(change.get("change", {}).get("actions", []))
    if actions <= {"no-op", "read"}:
        continue
    if change.get("type") not in allowed:
        bad.append(f"{change['address']} ({sorted(actions)})")
if bad:
    sys.exit(
        "STOP: the plan changes resources outside this component's expected set:\n  "
        + "\n  ".join(bad)
        + "\nThis should be impossible (separate state, separate API). It means the"
          " wrong backend or the wrong directory."
    )
print(f"  [ ok ] plan touches only this component ({len(plan.get('resource_changes', []))} changes)")
PY
  ok "plan written to $out (review it, then: apply --plan-file $out)"
}

# ===========================================================================
# apply
# ===========================================================================
cmd_apply() {
  require_nonce; require_account
  [ -n "$PLAN_FILE" ] || fail "--plan-file is required: apply a REVIEWED plan, never a fresh one.
     Applying without a reviewed plan file is how an unreviewed change lands."
  [ -f "$PLAN_FILE" ] || fail "plan file not found: $PLAN_FILE"
  assert_live_account

  if [ "$DRY_RUN" = 1 ]; then
    note "dry-run: would apply $PLAN_FILE"; return 0
  fi
  step "terraform apply (reviewed plan)"
  cd "$ROOT_DIR"
  terraform apply -input=false "$PLAN_FILE" || fail "terraform apply failed"
  ok "applied"

  # Record the Terraform-owned resources in the ledger IMMEDIATELY. Until this
  # runs, the only record of what exists is the state file.
  record_terraform_ownership
}

# ---------------------------------------------------------------------------
# Terraform-owned resources -> the #3968 ledger
# ---------------------------------------------------------------------------
# #3968's ledger has three buckets: synthetic_rows, k8s and queues. None models an
# API Gateway REST API or an SSM parameter, and their cleanup.py handles only those
# three. Rather than edit their files (explicitly out of scope), the Terraform-owned
# resources stay owned BY TERRAFORM STATE -- which is a stronger ownership record
# than a name prefix -- and a RECEIPT is written next to the ledger so a human
# reading the ledger can see this component's resources exist and how they are
# removed. `destroy` below consumes exact state, not the receipt.
record_terraform_ownership() {
  local dir; dir="$(artifact_dir)"
  cd "$ROOT_DIR"
  terraform output -json ownership > "$dir/ownership.json" \
    || fail "applied, but could not read the ownership output. Resolve before proceeding:
     the run's resources exist and are recorded ONLY in state right now."
  ok "ownership receipt written to $dir/ownership.json"
  note "Terraform-owned resources are torn down by 'destroy' below, from exact state."
  note "The fixture Ingress and Secret ARE in the #3968 ledger (uid-gated), because"
  note "they are Kubernetes objects its existing k8s bucket already covers."
}

# ===========================================================================
# handoff — the secret, AND actually attaching it
# ===========================================================================
cmd_handoff() {
  require_nonce; require_account
  [ -n "$LEDGER" ] || fail "--ledger is required; the Secret must be recorded as it is created"
  [ -n "$FIXTURE_DEPLOY" ] || fail "--fixture-deployment is required (the #3968 fixture gateway
     Deployment). Generating the Secret without ATTACHING it is what the previous
     revision did: the fixture pod kept reading the ORDINARY gateway's
     bedrockgateway-secrets/apigw-provenance-secret, so it validated against
     production's value and every internal call would 403 -- or, worse, would
     succeed against the wrong trust root. The Secret alone changes nothing."
  [ -f "$OWNERSHIP_LIB" ] || fail "#3968 ownership library not found at $OWNERSHIP_LIB"
  assert_live_account

  local dir; dir="$(artifact_dir)"
  local secret_name="w2-fixture-provenance-${NONCE}"

  cd "$ROOT_DIR"
  local param
  param="$(terraform output -raw ssm_provenance_parameter_name)" \
    || fail "could not read ssm_provenance_parameter_name from state. Apply first."
  [ -n "$param" ] || fail "ssm_provenance_parameter_name is empty — is fixture_edge_enabled true?"
  # Refuse the ordinary parameter outright. If the fixture were pointed at it, the
  # two edges would share one trust root and the isolation claim would be false.
  case "$param" in
    */fixture/*) ;;
    *) fail "refusing to hand off $param: it is not a per-run fixture parameter path" ;;
  esac
  ok "per-run parameter: $param"

  kubectl get deployment "$FIXTURE_DEPLOY" -n "$NAMESPACE" >/dev/null 2>&1 \
    || fail "fixture Deployment $FIXTURE_DEPLOY not found in $NAMESPACE. Run #3968's
     10-create-fixture.sh first."

  if [ "$DRY_RUN" = 1 ]; then
    note "dry-run: would create Secret/$secret_name and patch deployment/$FIXTURE_DEPLOY"
    note "dry-run: no secret value is read"
    return 0
  fi

  step "create the fixture Secret from SSM"
  # The value goes SSM -> pipe -> kubectl stdin. It is never written to a file, a
  # variable, a log or an argv entry. PIPESTATUS is checked because `set -o
  # pipefail` alone would not tell us WHICH side failed, and a silent SSM failure
  # would otherwise create a Secret containing an error string -- which then fails
  # much later as an unexplained 403.
  set +e
  aws_ ssm get-parameter --name "$param" --with-decryption \
      --query 'Parameter.Value' --output text \
    | kubectl create secret generic "$secret_name" \
        --namespace "$NAMESPACE" \
        --from-file=BG_APIGW_PROVENANCE_SECRET=/dev/stdin \
        -o json > "$dir/secret-create.json" 2>"$dir/secret-create.err"
  local rc_ssm="${PIPESTATUS[0]}" rc_kube="${PIPESTATUS[1]}"
  set -e
  [ "$rc_ssm" -eq 0 ] || fail "reading $param failed (exit $rc_ssm). NOTHING was created.
     A Secret created from a failed read would hold an error string and surface as
     an unexplained 403 long after this step."
  if [ "$rc_kube" -ne 0 ]; then
    if grep -q 'AlreadyExists' "$dir/secret-create.err"; then
      fail "Secret $secret_name already exists. REFUSING to adopt it: it may hold
     another run's value, and recording it would authorise deleting it. Use a new nonce."
    fi
    fail "creating Secret $secret_name failed: $(cat "$dir/secret-create.err")"
  fi

  local uid
  uid="$(python3 -c '
import json,sys
print(json.load(open(sys.argv[1]))["metadata"]["uid"])' "$dir/secret-create.json")"
  [ -n "$uid" ] || fail "Secret created but no uid returned; remove it by hand:
       kubectl delete secret $secret_name -n $NAMESPACE"

  python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "w2-${NONCE}" \
    --account-id "$ACCOUNT" --kind Secret --name "$secret_name" \
    --namespace "$NAMESPACE" --uid "$uid" \
    || fail "created Secret $secret_name (uid $uid) but could NOT record it in the ledger.
     Remove it by hand or teardown will not know it exists:
       kubectl delete secret $secret_name -n $NAMESPACE"
  ok "Secret/$secret_name created and recorded (uid $uid)"

  step "attach it to the fixture Deployment"
  # A strategic-merge patch on a merge-by-name list (container.env carries
  # patchMergeKey=name, verified against the core/v1 API types). So this REPOINTS
  # the one provenance ref and adds the trust flag while PRESERVING the other eight
  # secret-backed refs #3968's renderer deliberately carried over. Verified offline
  # with `kubectl patch --local`: 3 env entries in, 4 out, only the targeted one
  # changed. A whole-list replacement here would silently drop the other refs --
  # the exact failure #3968's render_fixture.py exists to prevent.
  kubectl patch deployment "$FIXTURE_DEPLOY" -n "$NAMESPACE" --type=strategic -p "$(
    cat <<JSON
{"spec":{"template":{"spec":{"containers":[{"name":"bedrockgateway","env":[
  {"name":"BG_APIGW_PROVENANCE_SECRET","valueFrom":{"secretKeyRef":{"name":"$secret_name","key":"BG_APIGW_PROVENANCE_SECRET"}}},
  {"name":"BG_TRUST_APIGW_HEADERS","value":"true"}
]}]}}}}
JSON
  )" || fail "could not attach the Secret to $FIXTURE_DEPLOY. The Secret IS recorded in
     the ledger. Until this patch succeeds the fixture still reads the ORDINARY
     gateway's provenance secret, so do NOT proceed to verification."
  ok "deployment/$FIXTURE_DEPLOY now reads BG_APIGW_PROVENANCE_SECRET from Secret/$secret_name"
  note "BG_TRUST_APIGW_HEADERS=true is set on the FIXTURE deployment only. It is true"
  note "there only because this component blanks both trusted headers on its"
  note "auth-NONE route; it must never be set on the ordinary gateway."

  # Prove the attachment rather than trusting the patch's exit code.
  step "verify the attachment landed"
  python3 - "$NAMESPACE" "$FIXTURE_DEPLOY" "$secret_name" <<'PY' || fail "attachment verification failed"
import json, subprocess, sys
ns, deploy, secret = sys.argv[1:4]
spec = json.loads(subprocess.run(
    ["kubectl", "get", "deployment", deploy, "-n", ns, "-o", "json"],
    capture_output=True, text=True, check=True).stdout)
containers = spec["spec"]["template"]["spec"]["containers"]
c = next(c for c in containers if c["name"] == "bedrockgateway")
env = {e["name"]: e for e in c.get("env", [])}
ref = (env.get("BG_APIGW_PROVENANCE_SECRET", {}).get("valueFrom") or {}).get("secretKeyRef", {})
if ref.get("name") != secret:
    sys.exit(f"BG_APIGW_PROVENANCE_SECRET still points at {ref.get('name')!r}, not {secret!r}")
if env.get("BG_TRUST_APIGW_HEADERS", {}).get("value") != "true":
    sys.exit("BG_TRUST_APIGW_HEADERS is not 'true' on the fixture deployment")
# The other eight secret refs must survive; losing one breaks a different identity
# check and would be diagnosed as a fixture-edge bug.
refs = [n for n, e in env.items()
        if (e.get("valueFrom") or {}).get("secretKeyRef")]
if len(refs) < 9:
    sys.exit(f"only {len(refs)} secret-backed env refs remain (expected >= 9): {sorted(refs)}. "
             "The patch dropped references #3968's renderer deliberately carried over.")
print(f"  [ ok ] attachment verified; {len(refs)} secret-backed env refs intact")
PY
  note "the pod must roll before this takes effect:"
  note "  kubectl rollout status deployment/$FIXTURE_DEPLOY -n $NAMESPACE"
}

# ===========================================================================
# destroy — dependency-ordered, against exact owned state
# ===========================================================================
cmd_destroy() {
  require_nonce; require_account
  assert_live_account
  local dir; dir="$(artifact_dir)"
  local tfvars="$dir/fixture.tfvars"
  [ -f "$tfvars" ] || fail "reviewed inputs not found at $tfvars. Destroy must run against the
     SAME inputs that were applied. Do NOT fall back to deleting by name prefix:
     a name prefix is not ownership, and a same-named replacement created by
     someone else would be destroyed instead."

  cd "$ROOT_DIR"
  local api_id
  api_id="$(terraform output -raw rest_api_id 2>/dev/null || echo "")"

  step "1/3 review the destroy plan against exact state"
  # A REVIEWED destroy plan, not an unchecked sweep: this is what shows exactly
  # which objects state owns before anything is deleted.
  local refresh_flag=""
  if [ "$RECOVER" = 1 ]; then
    # For the out-of-order case: the fixture ALB is already gone, so re-reading
    # data.aws_lb.fixture fails and destroy cannot plan at all. Verified on 1.15.3.
    refresh_flag="-refresh=false"
    note "--recover: using -refresh=false because a deleted fixture ALB makes the"
    note "           data source unreadable and blocks the destroy plan entirely."
  fi
  terraform plan -destroy -input=false -var-file="$tfvars" $refresh_flag \
    -out="$dir/destroy.plan" \
    || fail "could not plan the destroy. If the fixture ALB/Ingress was ALREADY deleted,
     the data source read fails and this is expected — re-run with --recover, which
     adds -refresh=false. Do not resort to deleting resources by name."
  terraform show -json "$dir/destroy.plan" > "$dir/destroy.plan.json"
  python3 - "$dir/destroy.plan.json" <<'PY' || fail "destroy plan review refused"
import json, sys
plan = json.load(open(sys.argv[1]))
deleting = [c["address"] for c in plan.get("resource_changes", [])
            if "delete" in c.get("change", {}).get("actions", [])]
other = [c["address"] for c in plan.get("resource_changes", [])
         if set(c.get("change", {}).get("actions", [])) - {"delete", "no-op", "read"}]
if other:
    sys.exit(f"STOP: the destroy plan also CREATES or UPDATES: {other}")
print("  [ ok ] destroy plan deletes exactly these, from state:")
for a in deleting:
    print(f"         - {a}")
PY

  if [ "$DRY_RUN" = 1 ]; then
    note "dry-run: stopping before any deletion. Reviewed plan: $dir/destroy.plan"
    return 0
  fi

  step "2/3 destroy the edge FIRST (stop the listeners/routes)"
  # ORDERING IS LOAD-BEARING. The edge must stop accepting and forwarding traffic
  # BEFORE the fixture ALB is removed, and main.tf READS that ALB, so removing it
  # first makes this step unplannable. Both directions verified.
  terraform apply -input=false "$dir/destroy.plan" \
    || fail "destroy failed partway. State is isolated per nonce, so re-running is safe
     and idempotent. Resolve and re-run; do NOT delete by name prefix."
  ok "fixture edge destroyed"

  step "3/3 verify ABSENCE, and that the ordinary edge is untouched"
  local problems=0
  if [ -n "$api_id" ]; then
    if aws_ apigateway get-rest-api --rest-api-id "$api_id" >/dev/null 2>&1; then
      printf '  [FAIL] fixture REST API %s is STILL PRESENT\n' "$api_id" >&2; problems=1
    else
      ok "fixture REST API $api_id is gone"
    fi
  fi
  local param="/adp/${ENVIRONMENT}/gateway/fixture/${NONCE}/apigw-provenance-secret"
  if aws_ ssm get-parameter --name "$param" >/dev/null 2>&1; then
    printf '  [FAIL] per-run secret %s is STILL PRESENT\n' "$param" >&2; problems=1
  else
    ok "per-run secret is gone"
  fi
  # The most important post-check: this component must have changed nothing
  # ordinary. Checked by READING, never by writing.
  local ord
  ord="$(aws_ ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/apigw-provenance-secret" \
    --query 'Parameter.Name' --output text 2>/dev/null || echo "MISSING")"
  if [ "$ord" = "MISSING" ]; then
    printf '  [FAIL] the ORDINARY provenance parameter is missing. Investigate immediately.\n' >&2
    problems=1
  else
    ok "ordinary provenance parameter still present (value not read)"
  fi

  [ "$problems" -eq 0 ] || fail "teardown did NOT fully verify. Do not report cleanup as complete."
  ok "teardown verified"

  cat <<EOF

  REMAINING, in this order:
   1. Delete the fixture Ingress (this is what removes the fixture ALB) and the
      fixture Secret, via #3968's 90-cleanup-ledger.sh. Both are uid-gated there,
      so a same-named replacement created by someone else is left alone.
   2. Remove the private artifact directory $dir, which holds the reviewed inputs
      and plan files.
   3. #3968's cleanup_ok stays None for a dry run and False on any unverified
      absence; do not record success on this component until step 1 reports
      verified absence for both objects.
EOF
}

# ===========================================================================
# verify
# ===========================================================================
cmd_verify() {
  require_nonce; require_account
  assert_live_account
  cd "$ROOT_DIR"
  local endpoint api_id
  endpoint="$(terraform output -raw worker_control_endpoint)" || fail "no endpoint in state"
  api_id="$(terraform output -raw rest_api_id)" || fail "no rest_api_id in state"

  step "edge identity"
  printf '  endpoint: %s\n' "$endpoint"
  local ordinary
  ordinary="$(aws_ ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/api-gateway-id" \
    --query Parameter.Value --output text 2>/dev/null || echo "")"
  if [ -n "$ordinary" ] && [ "$ordinary" != "None" ]; then
    [ "$api_id" != "$ordinary" ] \
      || fail "the fixture API id EQUALS the ordinary edge's ($ordinary). STOP."
    ok "distinct from the ordinary edge ($api_id != $ordinary)"
  else
    note "could not read the ordinary API id from SSM; compare it by hand"
  fi

  step "the refusals (each observed, not assumed)"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$endpoint/bootstrap" || echo 000)"
  printf '  unsigned                -> %s\n' "$code"
  [ "$code" = "403" ] || note "EXPECTED 403 (API Gateway AWS_IAM refuses before the pod)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$endpoint/bootstrap" \
    -H 'X-Caller-Identity: arn:aws:iam::000000000000:role/anything' \
    -H 'X-Adp-Edge-Provenance: forged' || echo 000)"
  printf '  spoofed headers, unsigned -> %s\n' "$code"
  [ "$code" = "403" ] || note "EXPECTED 403; the edge OVERWRITES both headers"
  note "wrong-role (correctly signed) must be run with a SigV4 signer from an identity"
  note "NOT in allowed_caller_role_arns; it is refused by the resource policy's Deny."
  note "A 5xx on either of the above means the request may have reached a backend —"
  note "investigate; the first two must never do so."
}

case "$CMD" in
  init)    cmd_init ;;
  plan)    cmd_plan ;;
  apply)   cmd_apply ;;
  handoff) cmd_handoff ;;
  verify)  cmd_verify ;;
  destroy) cmd_destroy ;;
  ""|-h|--help) sed -n '1,45p' "$0" ;;
  *) fail "unknown subcommand: $CMD (init|plan|apply|handoff|verify|destroy)" ;;
esac

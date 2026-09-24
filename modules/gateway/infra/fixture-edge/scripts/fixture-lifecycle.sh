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
# Security-control inputs for `verify`. Both default to ABSENT-IS-A-FAILURE rather
# than absent-is-a-note, so a control cannot go silently unrun.
WRONG_ROLE_PROFILE=""; SKIP_WRONG_ROLE=0; HUMAN_PROBE_PATH=""
EXPECT_CLUSTER=""

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
    --expect-cluster)   EXPECT_CLUSTER="${2:?}"; shift 2 ;;
    --wrong-role-profile) WRONG_ROLE_PROFILE="${2:?}"; shift 2 ;;
    --human-probe-path) HUMAN_PROBE_PATH="${2:?}"; shift 2 ;;
    --skip-wrong-role)  SKIP_WRONG_ROLE=1; shift ;;
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

# ---------------------------------------------------------------------------
# `--profile` DOES NOT BIND THE TERRAFORM PROVIDER.
#
# aws_() passes --profile to the AWS CLI. The AWS provider is a SEPARATE
# credential consumer: it runs inside the terraform process and resolves its own
# chain (env vars, then AWS_PROFILE, then the default profile, then IMDS). So
# every `aws_` call could be correctly bound to adp-embark1 while terraform
# planned and APPLIED against whatever ambient credential the shell happened to
# carry -- a different account, with only main.tf's account precondition standing
# between that and a fixture edge created in the wrong place. A CLI flag that the
# provider never sees is not a binding.
#
# `-backend-config=profile=` (set in init) binds only the S3 BACKEND, not the
# provider, which is why that was not sufficient either.
#
# Terraform has no --profile flag, so the provider is bound the only way it can
# be: through the environment of the terraform process itself. AWS_PROFILE and
# AWS_REGION are set, and the lower-precedence static/ambient variables are
# CLEARED so they cannot win over the named profile. main.tf's account/region
# preconditions then re-verify the result rather than trusting this.
# ---------------------------------------------------------------------------
terraform_() {
  if [ -n "$PROFILE" ]; then
    # AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN outrank AWS_PROFILE in the provider's
    # chain, so leaving them set would silently override the named profile.
    env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
        -u AWS_DEFAULT_PROFILE -u AWS_CREDENTIAL_PROFILES_FILE \
        AWS_PROFILE="$PROFILE" AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" \
        terraform "$@"
  else
    env AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" terraform "$@"
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

# ---------------------------------------------------------------------------
# AWS CLI --profile does NOT bind kubectl.
#
# assert_live_account proves the AWS CLI is pointed at the right account. It says
# nothing about which Kubernetes cluster kubectl will mutate: the current context
# comes from KUBECONFIG and can easily be another cluster, or another ACCOUNT's
# cluster, while every aws_ call in the same run is correctly bound. Every
# mutating subcommand must therefore assert the cluster too, BEFORE mutating.
# ---------------------------------------------------------------------------
# A CONTEXT NAME IS A LOCAL LABEL, NOT THE CLUSTER'S IDENTITY.
#
# The previous revision decided this by PARSING the context name: an EKS-ARN-shaped
# name had its account/region compared, and anything else was accepted once the
# operator passed --expect-cluster with the same string. Both halves are weak in the
# same way -- the name is an arbitrary local alias chosen by whoever wrote the
# kubeconfig, and it can say `arn:aws:eks:us-east-1:<right account>:cluster/adp-dev-eks`
# while pointing at ANY server. So --expect-cluster was a free-form bypass: repeating
# the current context name always satisfied it, which made it a typing exercise
# rather than a check.
#
# What is actually authoritative is the API SERVER ENDPOINT kubectl will connect to
# (kubeconfig `cluster.server`) compared against the endpoint AWS reports for the
# named cluster (`eks describe-cluster`, read through the run's own bound profile).
# That resolves the cluster's true account and region from the AWS side, and pins the
# connection kubectl will actually make. --expect-cluster is now the CLUSTER NAME to
# verify against AWS, not a string to echo back.
assert_kube_context() {
  local ctx server
  ctx="$(kubectl config current-context 2>/dev/null)" \
    || fail "no current kubectl context. Refusing to mutate an unknown cluster."
  [ -n "$ctx" ] || fail "kubectl current-context is empty. Refusing to continue."

  # The endpoint kubectl will really talk to, from the resolved (--minify) context.
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || printf '')"
  [ -n "$server" ] || fail "could not read the API server endpoint for context '$ctx'. Without it
     the cluster kubectl would mutate cannot be identified, and a context NAME is only
     a local label. Refusing to mutate an unidentifiable cluster."

  # The cluster NAME to verify. Derived from an EKS-ARN context when available (a
  # convenience, not the proof -- the ARN is still just a local string), otherwise it
  # must be stated, because there is nothing to look up.
  local cluster_name="$EXPECT_CLUSTER"
  if [ -z "$cluster_name" ]; then
    case "$ctx" in
      arn:aws:eks:*:cluster/*) cluster_name="${ctx##*/}" ;;
      *) fail "the kubectl context '$ctx' is a local alias, so the cluster it points at cannot
     be named from it. Pass --expect-cluster <EKS cluster name> -- the NAME of the
     cluster, which is then verified against AWS via eks describe-cluster. Refusing
     to mutate an unverified cluster." ;;
    esac
  fi

  # THE AUTHORITATIVE READ. Through aws_, so it uses this run's bound profile and
  # region: the cluster is therefore confirmed to exist in the account every other
  # check in this run was made against.
  local aws_endpoint
  aws_endpoint="$(aws_ eks describe-cluster --name "$cluster_name" \
    --query cluster.endpoint --output text 2>/dev/null || printf '')"
  [ -n "$aws_endpoint" ] && [ "$aws_endpoint" != "None" ] \
    || fail "EKS cluster '$cluster_name' does not exist in account $ACCOUNT / $REGION (as
     resolved through --profile), so kubectl's target cannot be confirmed to be the
     cluster this run is authorised for. Refusing to mutate. This is the check a
     context-name comparison could not make: the name is a local label and can claim
     any account."

  # Compare on host, since kubeconfig and the API may differ in scheme/trailing slash
  # while naming the same endpoint.
  local want_host have_host
  want_host="$(printf '%s' "$aws_endpoint" | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')"
  have_host="$(printf '%s' "$server" | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')"
  [ "$have_host" = "$want_host" ] || fail "kubectl would connect to a DIFFERENT cluster than the one verified.
       kubeconfig server   : $have_host
       AWS says $cluster_name is : $want_host
     The context name ('$ctx') is only a local label and can name any cluster, so this
     endpoint comparison is what actually pins the target. Refusing to mutate."

  ok "cluster verified against AWS: $cluster_name ($want_host) in $ACCOUNT/$REGION"
}

# ---------------------------------------------------------------------------
# A SigV4-SIGNED probe from a DIFFERENT profile, for the wrong-role control.
#
# This is the only probe that reaches this component's resource-policy Deny: an
# UNSIGNED request is refused earlier, by API Gateway's AWS_IAM check, so it can
# never demonstrate that the policy denies a non-allowlisted role.
#
# THREE DEFECTS ROOT FOUND IN THE PREVIOUS VERSION OF THIS FUNCTION:
#
#  1. `aws configure get aws_access_key_id` reads STATIC KEYS OUT OF A CONFIG FILE.
#     It returns nothing for an assumed-role, SSO or credential-process profile --
#     which is what the authorized profiles here actually are. So the probe emitted
#     000 for a perfectly usable credential and the control could never pass.
#  2. The secret key was passed via `curl --user`, i.e. as an ARGV ENTRY, readable
#     by any process listing on the host for the lifetime of the call.
#  3. Nothing checked WHO the probe signed as. A profile that happens to be an
#     ALLOWLISTED role would produce a 403-or-not result that says nothing about the
#     Deny -- and a 403 from the wrong identity would be recorded as the Deny
#     working. A wrong-role control that does not verify the role is not a control.
#
# Fixed by resolving credentials through the SDK's OWN provider chain (botocore,
# which handles assume-role/SSO/credential_process/IMDS exactly as the CLI does),
# signing IN MEMORY with SigV4, and requiring the caller identity to be verified
# separately by sigv4_probe_identity() below.
#
# Emits ONLY an HTTP status code on stdout. Returns 000 when it could not sign,
# which the caller treats as a FAILURE ("the refusal was not observed").
# ---------------------------------------------------------------------------
aws_sigv4_probe() {
  local profile="$1" url="$2"
  python3 - "$profile" "$url" "$REGION" <<'PY' 2>/dev/null || printf '000\n'
import sys
profile, url, region = sys.argv[1:4]
try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import urllib.error
    import urllib.request
except ImportError:
    print("000"); raise SystemExit(0)
try:
    session = boto3.Session(profile_name=profile)
    creds = session.get_credentials()
    if creds is None:
        print("000"); raise SystemExit(0)
    creds = creds.get_frozen_credentials()
    # Signed in memory. Nothing reaches argv, a file or the log.
    req = AWSRequest(method="POST", url=url, data=b"")
    SigV4Auth(creds, "execute-api", region).add_auth(req)
    request = urllib.request.Request(url, data=b"", method="POST")
    for key, value in req.headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            print(resp.status)
    except urllib.error.HTTPError as exc:
        # A 403 is the EXPECTED outcome here, and urllib raises on it.
        print(exc.code)
except Exception:                                  # noqa: BLE001
    print("000")
PY
}

# ---------------------------------------------------------------------------
# WHO the wrong-role probe actually signs as.
#
# Prints the caller ARN on stdout, empty when it cannot be resolved. The verify
# step REQUIRES this to be a real identity that is NOT in allowed_caller_role_arns:
# a 403 observed while signing as an allowlisted role, or as an unresolvable
# identity, proves nothing about the Deny and must not be recorded as proving it.
# ---------------------------------------------------------------------------
sigv4_probe_identity() {
  local profile="$1"
  aws --profile "$profile" --region "$REGION" sts get-caller-identity \
    --query Arn --output text 2>/dev/null || printf ''
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

# ---------------------------------------------------------------------------
# The run id is the LEDGER'S, never one this script invents.
#
# The previous revision passed `--run-id "w2-${NONCE}"`. That is a guess at
# #3968's internal identifier format. If it does not match the run id #3968
# actually opened, the row lands under an id its cleanup never looks up: the
# object is "recorded" and still orphaned, which is the failure recording exists
# to prevent. Read it from the ledger #3968 wrote, and refuse rather than fall
# back to a guess.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# NOTE ON THE CALLING CONVENTION, which was a real bug.
#
# This was first used as `--run-id "$RUN_ID"`. Command substitution runs
# in a SUBSHELL, so the `fail` inside this function exited THAT subshell -- the
# parent kept going with an EMPTY run id and recorded the object as
# `--run-id ""`. Worse, `record-k8s` then consumed the NEXT flag as the run id in
# the fake, and in the real CLI an empty run id is a row no cleanup looks up. The
# refusal this function exists to perform was silently converted into the exact
# outcome it was meant to prevent.
#
# So callers must assign it to a variable on its own line -- `RUN_ID="$(...)" ||
# fail` -- because `set -e` does propagate a failed command substitution when it
# is the whole right-hand side of an assignment. resolve_run_id below does that
# once, so no caller has to remember.
# ---------------------------------------------------------------------------
ledger_run_id() {
  [ -n "$LEDGER" ] || fail "--ledger is required to resolve the run id"
  [ -s "$LEDGER" ] || fail "ledger $LEDGER is missing or empty. #3968's session must have
     opened this run before the fixture edge records anything into it."
  local rid
  rid="$(python3 - "$LEDGER" "$NONCE" <<'PY'
import json, sys
path, nonce = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(path))
except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
    sys.exit(f"could not parse ledger {path}: {exc}")
if not isinstance(doc, dict):
    sys.exit("ledger root is not an object; cannot resolve run_id")
rid = doc.get("run_id") or doc.get("runId")
if not rid:
    sys.exit("ledger has no run_id field; #3968 must open the run first")
# The ledger must be THIS run's. Recording into another run's ledger would hand
# this fixture's objects to a cleanup that deletes on a different schedule.
led_nonce = doc.get("run_nonce") or doc.get("nonce")
if led_nonce and led_nonce != nonce:
    sys.exit(f"ledger is for run nonce {led_nonce}, not {nonce}; refusing to record")
print(rid)
PY
  )" || fail "could not resolve the #3968 run id from $LEDGER: see the error above.
     Refusing to invent one -- a guessed run id records the object under an id
     #3968's cleanup never looks up, which orphans it while looking recorded."
  printf '%s\n' "$rid"
}

# Resolve ONCE into a global, failing the whole script (not a subshell) if the
# ledger cannot vouch for a run id.
RUN_ID=""
resolve_run_id() {
  RUN_ID="$(ledger_run_id)" || fail "could not resolve the #3968 run id; refusing to record."
  [ -n "$RUN_ID" ] || fail "resolved an EMPTY run id from $LEDGER. Refusing to record: a row
     with no run id is a row #3968's cleanup never looks up, which orphans the
     object while making it look recorded."
}

# ---------------------------------------------------------------------------
# Absence must mean NotFound, never "the call failed".
#
# Root EXECUTED the previous revision's destroy verification with the fake AWS
# returning AccessDeniedException instead of NotFound: it exited 0 and reported
# "teardown verified". Every `if aws ... >/dev/null 2>&1; then present; else
# absent; fi` reads an expired credential, a network blip, a throttle and a
# permission denial as proof of deletion. That is the most dangerous possible
# direction for this error to point, because the operator then records cleanup as
# complete and stops looking.
#
# Returns: 0 = confirmed absent, 1 = confirmed present, 2 = UNKNOWN (must fail).
# ---------------------------------------------------------------------------
probe_absent() {
  local not_found_pattern="$1"; shift
  local out rc=0
  # `if`, NOT `set +e` ... `set -e`. A command in an `if` condition is exempt from
  # errexit by the shell's own rules, so nothing here has to toggle a global flag.
  #
  # The save/restore version of this was WRONG and the fake caught it: this
  # function ran inside its caller's `set +e` region, and the trailing `set -e`
  # re-enabled errexit for the CALLER. So the moment probe_absent returned 1 --
  # "the resource is STILL PRESENT", the single most important thing teardown can
  # discover -- errexit killed the script before the [FAIL] was printed. Exit
  # status 1 with an EMPTY stderr, which reads as a silent crash rather than as a
  # surviving resource. Toggling errexit inside a helper is not composable; not
  # toggling it is.
  if out="$("$@" 2>&1)"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    return 1                      # the call succeeded: the resource is PRESENT
  fi
  # Only the service's own not-found error proves absence.
  if printf '%s' "$out" | grep -Eq "$not_found_pattern"; then
    return 0
  fi
  PROBE_ERROR="$(printf '%s' "$out" | tr -d '\n' | cut -c1-400)"
  return 2                        # UNKNOWN -- caller must treat as failure
}
PROBE_ERROR=""

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
  terraform_ init -input=false -reconfigure \
    -backend-config="bucket=$BUCKET" \
    -backend-config="key=$STATE_KEY" \
    -backend-config="region=$REGION" \
    -backend-config="encrypt=true" \
    ${PROFILE:+-backend-config="profile=$PROFILE"} \
    || fail "terraform init failed"
  ok "initialised against an isolated per-run state key"
}

# ---------------------------------------------------------------------------
# REFUSE nonce B against a backend initialised for nonce A.
#
# terraform init writes the resolved backend into .terraform/terraform.tfstate.
# Nothing in the previous revision compared it to the --nonce/--account-id on the
# CURRENT command line, so `init --nonce A` followed by `plan --nonce B` planned
# run B's resources into run A's state file. Both runs then believe they own the
# same objects, and either teardown destroys the other's edge.
#
# Every command that touches state calls this BEFORE reading or writing any.
# ---------------------------------------------------------------------------
assert_backend_binding() {
  # TF_DATA_DIR is terraform's own override for where `.terraform` lives, so
  # honouring it here keeps this check reading the SAME record the terraform
  # invocations below write. Hardcoding $ROOT_DIR/.terraform would silently read a
  # stale record whenever an operator (or CI) sets TF_DATA_DIR.
  local cfg="${TF_DATA_DIR:-$ROOT_DIR/.terraform}/terraform.tfstate"
  [ -f "$cfg" ] || fail "no initialised backend found ($cfg missing). Run 'init' first with
     this run's --nonce and --account-id. This is deliberately fatal: without the
     record there is nothing to compare the nonce/account on this command line
     against, so proceeding would mean planning or destroying through whatever
     state happened to be configured."
  state_key
  # The BUCKET, TYPE and PROFILE are checked as well as the key.
  #
  # Validating only the key was insufficient in three distinct ways, all of which
  # leave the run acting on state it did not intend:
  #   * the same key in a DIFFERENT BUCKET is a different state file entirely, so a
  #     re-init against another bucket (a typo, or another account's state bucket
  #     this credential can reach) passed the check while reading foreign state;
  #   * a backend of another TYPE (local, or an s3 record replaced by one) has no
  #     per-run isolation at all, and "the key matches" says nothing about it;
  #   * the backend's PROFILE is the credential that reads and writes the state. If
  #     it differs from --profile, this command reads state through one identity
  #     while terraform_/aws_ act through another -- so the state the plan is built
  #     from is not the state the account checks were run against.
  python3 - "$cfg" "$STATE_KEY" "$ACCOUNT" "$REGION" "${BUCKET:-}" "${PROFILE:-}" <<'PY' \
    || fail "backend binding check refused this command; see above."
import json, sys
cfg, expect_key, account, region, expect_bucket, expect_profile = sys.argv[1:7]
try:
    doc = json.load(open(cfg))
except Exception as exc:                      # noqa: BLE001
    sys.exit(f"could not read the initialised backend record {cfg}: {exc}")
backend = doc.get("backend") or {}
conf = backend.get("config") or {}

# --- the backend must be the isolated remote one this component requires -----
actual_type = backend.get("type")
if actual_type != "s3":
    sys.exit(
        f"BACKEND MISMATCH -- the initialised backend type is {actual_type!r}, not 's3'.\n"
        "This component's whole ownership story rests on an isolated per-run S3 state\n"
        "key: it is what lets teardown prove which objects this run created. A local\n"
        "(or otherwise substituted) backend has no such isolation, so refusing."
    )

actual_key = conf.get("key")
if not actual_key:
    sys.exit("the initialised backend records no state key; re-run init")
if actual_key != expect_key:
    sys.exit(
        "BACKEND MISMATCH -- refusing to touch another run's state.\n"
        f"  initialised for : {actual_key}\n"
        f"  this command    : {expect_key}\n"
        "The arguments on this command line do not match the backend that was\n"
        "initialised. Planning or destroying through a mismatched state file would\n"
        "make two runs believe they own the same resources. Re-run 'init' with these\n"
        "exact arguments, or re-issue this command with the nonce/account the\n"
        "backend was initialised for."
    )

# --- the bucket: the same key in another bucket is another state file --------
actual_bucket = conf.get("bucket")
if not actual_bucket:
    sys.exit("the initialised backend records no bucket; re-run init")
if expect_bucket and actual_bucket != expect_bucket:
    sys.exit(
        "BACKEND MISMATCH -- same key, DIFFERENT BUCKET.\n"
        f"  initialised in : {actual_bucket}\n"
        f"  this command   : {expect_bucket}\n"
        "A matching key in another bucket is a different state file, so this command\n"
        "would act on resources recorded somewhere other than where it believes. Re-run\n"
        "'init' with the intended bucket."
    )

actual_region = conf.get("region")
if actual_region and actual_region != region:
    sys.exit(f"backend region {actual_region} != --region {region}; re-run init")

# --- the profile: the identity that reads/writes the state -------------------
actual_profile = conf.get("profile") or ""
if expect_profile and actual_profile != expect_profile:
    sys.exit(
        "BACKEND MISMATCH -- the state is read through a DIFFERENT credential than this\n"
        "command is bound to.\n"
        f"  backend profile : {actual_profile or '<none: ambient credential>'}\n"
        f"  --profile       : {expect_profile}\n"
        "Every account check in this run was made against --profile, so a backend on\n"
        "another identity means the state the plan is built from was never the state\n"
        "those checks applied to. Re-run 'init' with this --profile."
    )
print(f"  [ ok ] backend binding verified "
      f"(s3://{actual_bucket}/{actual_key}, profile {actual_profile or '<ambient>'})")
PY
}

# ===========================================================================
# plan
# ===========================================================================
cmd_plan() {
  require_nonce; require_account
  assert_live_account
  assert_backend_binding
  local dir; dir="$(artifact_dir)"
  local tfvars="$dir/fixture.tfvars" out="$dir/fixture.plan"
  [ -f "$tfvars" ] || fail "expected reviewed inputs at $tfvars (see RUNBOOK.md step 3)"

  step "terraform plan"
  cd "$ROOT_DIR"
  terraform_ plan -input=false -var-file="$tfvars" -out="$out" \
    || fail "terraform plan failed. A plan-time REFUSAL here is the run-binding gate
     working: it blocks a wrong account/region, a public or foreign-VPC fixture ALB,
     an ALB not tagged for this run, one the VPC Link cannot reach, or the ordinary
     internal-plane ALB."

  # Refuse a plan that reaches outside this root. Separate state and a separate API
  # should make it impossible; a plan that shows otherwise means the wrong backend.
  terraform_ show -json "$out" > "$dir/fixture.plan.json"
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
  assert_backend_binding

  if [ "$DRY_RUN" = 1 ]; then
    note "dry-run: would apply $PLAN_FILE"; return 0
  fi
  step "terraform apply (reviewed plan)"
  cd "$ROOT_DIR"
  terraform_ apply -input=false "$PLAN_FILE" || fail "terraform apply failed"
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
  terraform_ output -json ownership > "$dir/ownership.json" \
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
  # BOTH identities, before anything is created. assert_live_account binds the AWS
  # CLI; assert_kube_context binds kubectl, which --profile does NOT.
  assert_live_account
  assert_backend_binding
  assert_kube_context
  # Resolve the run id UP FRONT, before the secret is read or anything is created.
  # If the ledger cannot vouch for a run id, the object must never come into
  # existence -- discovering that only at record time leaves a live Secret that
  # nothing can look up.
  resolve_run_id

  local dir; dir="$(artifact_dir)"
  local secret_name="w2-fixture-provenance-${NONCE}"

  cd "$ROOT_DIR"
  local param
  param="$(terraform_ output -raw ssm_provenance_parameter_name)" \
    || fail "could not read ssm_provenance_parameter_name from state. Apply first."
  [ -n "$param" ] || fail "ssm_provenance_parameter_name is empty — is fixture_edge_enabled true?"
  # Refuse the ordinary parameter outright. If the fixture were pointed at it, the
  # two edges would share one trust root and the isolation claim would be false.
  #
  # `*/fixture/*` alone is NOT sufficient, and accepting it was a real hole: it
  # matches ANOTHER RUN's fixture parameter just as happily as this one's. Handing
  # run B's trust root to run A's fixture would make both runs' isolation claims
  # false while every check here still passed. The path must contain THIS nonce.
  case "$param" in
    */fixture/*) ;;
    *) fail "refusing to hand off $param: it is not a per-run fixture parameter path" ;;
  esac
  case "$param" in
    *"$NONCE"*) ;;
    *) fail "refusing to hand off $param: it is a fixture path but does NOT carry this
     run's nonce ($NONCE). It likely belongs to a DIFFERENT run, and handing its
     trust root to this fixture would break both runs' isolation." ;;
  esac
  ok "per-run parameter: $param"

  # -------------------------------------------------------------------------
  # EXISTENCE IS NOT OWNERSHIP.
  #
  # The previous revision ran `kubectl get deployment >/dev/null` and proceeded.
  # That accepts ANY Deployment of that name -- including the ORDINARY gateway if
  # someone passes its name, and including a replacement created after #3968's
  # fixture was deleted. It would then attach a fixture trust root to a production
  # workload and set BG_TRUST_APIGW_HEADERS=true on it, which is precisely the
  # "trusts forgeable headers" outcome #5836 exists to avoid.
  #
  # So: read the object, and verify against the LEDGER that it is the fixture this
  # run owns. The uid and resourceVersion captured here also bind the patch below,
  # so a Deployment replaced between this check and the patch cannot be mutated.
  # -------------------------------------------------------------------------
  step "verify the target Deployment is THIS run's fixture"
  local deploy_json="$dir/fixture-deployment.json"
  kubectl get deployment "$FIXTURE_DEPLOY" -n "$NAMESPACE" -o json > "$deploy_json" 2>"$dir/deploy-get.err" \
    || fail "fixture Deployment $FIXTURE_DEPLOY not found in $NAMESPACE: $(tr -d '\n' < "$dir/deploy-get.err")
     Run #3968's 10-create-fixture.sh first."

  local deploy_uid deploy_rv
  # Reads the ledger #3968 wrote and REFUSES anything it does not vouch for.
  if ! python3 - "$deploy_json" "$LEDGER" "$NONCE" "$NAMESPACE" "$dir/deploy-identity" <<'PY'
import json, sys
dj, ledger_path, nonce, ns, out = sys.argv[1:6]
doc = json.load(open(dj))
md = doc.get("metadata", {})
uid, rv, name = md.get("uid"), md.get("resourceVersion"), md.get("name")
if not uid or not rv:
    sys.exit("the Deployment has no uid/resourceVersion; refusing to patch it")
if md.get("namespace") != ns:
    sys.exit(f"Deployment is in namespace {md.get('namespace')}, expected {ns}")

labels = md.get("labels") or {}
# ---------------------------------------------------------------------------
# THE REAL LABEL KEYS, read from #3968's renderer -- not guessed.
#
# The previous revision looked for "adp.fixture/run", "adp.fixture/run-nonce" and
# "adp-fixture-run". NONE of those exist. lib/render_fixture.py (branch
# agent/issue-3968) labels the fixture Deployment/Service with:
#
#     app                        = <fixture name>
#     app.kubernetes.io/part-of  = bedrock-gateway
#     adp.io/w2-fixture          = <run id>
#     adp.io/w2-nonce            = <run nonce>
#
# So every label check here was inert: the ordinary-gateway refusal could not fire
# (no invented key is ever present, so the `not any(...)` was always true -- it
# would in fact have refused a LEGITIMATE fixture), and the nonce comparison never
# ran. Verified against the renderer source rather than assumed.
# ---------------------------------------------------------------------------
W2_FIXTURE_LABEL = "adp.io/w2-fixture"
W2_NONCE_LABEL = "adp.io/w2-nonce"

# Refuse the ORDINARY gateway outright, whatever it is called. #3968's renderer
# deep-copies the live gateway pod spec, so a fixture and the ordinary workload
# look alike apart from their ownership markers -- which is exactly why the
# markers, not the shape, must decide.
if not labels.get(W2_FIXTURE_LABEL) or not labels.get(W2_NONCE_LABEL):
    sys.exit(
        f"{name} does not carry BOTH of #3968's fixture run labels "
        f"({W2_FIXTURE_LABEL}, {W2_NONCE_LABEL}). Present labels: {sorted(labels)}. "
        "This is how the ORDINARY gateway Deployment is refused: it carries "
        "app=bedrockgateway and neither run label. REFUSING to attach a fixture trust "
        "root or set BG_TRUST_APIGW_HEADERS on it."
    )

label_nonce = labels.get(W2_NONCE_LABEL)

# The ledger is authoritative. A label alone is caller-writable; a ledger row was
# recorded by #3968 at creation time against a server-assigned uid.
try:
    led = json.load(open(ledger_path))
except Exception as exc:                       # noqa: BLE001
    sys.exit(f"could not read the ledger {ledger_path}: {exc}")

rows = []
if isinstance(led, dict):
    k8s = led.get("k8s")
    if isinstance(k8s, list):
        rows = k8s
    elif isinstance(k8s, dict):
        rows = list(k8s.values())
matched = [r for r in rows
           if isinstance(r, dict)
           and str(r.get("kind", "")).lower() == "deployment"
           and r.get("uid") == uid]
if not matched:
    recorded = [(r.get("kind"), r.get("name"), r.get("uid")) for r in rows if isinstance(r, dict)]
    sys.exit(
        f"the live Deployment {name} (uid {uid}) is NOT recorded as a Deployment in "
        f"{ledger_path}. Existence is not ownership: this may be the ordinary gateway "
        f"or a replacement created after this run's fixture was deleted. "
        f"Ledger k8s rows: {recorded}"
    )
if label_nonce and label_nonce != nonce:
    sys.exit(f"Deployment run label is {label_nonce}, not this run's nonce {nonce}")

with open(out + ".uid", "w") as fh:
    fh.write(uid)
with open(out + ".rv", "w") as fh:
    fh.write(rv)
print(f"  [ ok ] {name} uid {uid} is recorded in the ledger for this run")
PY
  then
    fail "refusing to patch $FIXTURE_DEPLOY: see the ownership error above."
  fi
  deploy_uid="$(cat "$dir/deploy-identity.uid")"
  deploy_rv="$(cat "$dir/deploy-identity.rv")"

  if [ "$DRY_RUN" = 1 ]; then
    note "dry-run: would create Secret/$secret_name and patch deployment/$FIXTURE_DEPLOY"
    note "dry-run: no secret value is read"
    return 0
  fi

  step "read and validate the secret BEFORE creating anything"
  # ---------------------------------------------------------------------------
  # WHY THIS IS NOT A PIPELINE ANY MORE.
  #
  # The previous revision piped `aws ssm get-parameter | kubectl create secret`
  # and then checked PIPESTATUS. Root EXECUTED that path and it is broken: the two
  # sides of a pipe run CONCURRENTLY. When the SSM read fails, kubectl has already
  # seen EOF on stdin, created an EMPTY Secret, and exited 0. The script then read
  # rc_ssm != 0 and reported "NOTHING was created" -- which was FALSE. The Secret
  # existed, the ledger was empty because the failure path returns before recording,
  # and so the one object nothing could later find was also the one object teardown
  # would never delete. A confident false claim is worse than a crash.
  #
  # PIPESTATUS was never capable of preventing this. It reports what happened; it
  # cannot un-create what the downstream process already did. The fix has to be
  # ordering: get the value, validate it, and only then mutate the cluster.
  # ---------------------------------------------------------------------------
  local secret_value rc_ssm
  set +e
  # Command substitution, so a failing read yields a nonzero status and no cluster
  # call has happened yet. The value stays in a shell variable: never an argv entry
  # (visible in ps), never a file, never the log.
  secret_value="$(aws_ ssm get-parameter --name "$param" --with-decryption \
    --query 'Parameter.Value' --output text 2>"$dir/ssm-read.err")"
  rc_ssm=$?
  set -e
  if [ "$rc_ssm" -ne 0 ]; then
    secret_value=""
    fail "reading $param failed (exit $rc_ssm). NOTHING was created -- this is now
     accurate rather than aspirational: the cluster has not been contacted at this
     point. Upstream error: $(tr -d '\n' < "$dir/ssm-read.err")"
  fi
  # Validate IN MEMORY. A Secret holding an error string, an empty value or the
  # literal "None" that the AWS CLI prints for a missing field would surface much
  # later as an unexplained 403, far from its cause.
  [ -n "$secret_value" ] || fail "the value read from $param is EMPTY. Refusing to create
     a Secret from it: the fixture would validate every request against an empty
     trust root."
  case "$secret_value" in
    None|null) fail "read the literal '$secret_value' from $param -- that is the CLI's
     rendering of an absent value, not a secret. Nothing was created." ;;
    *[Ee]rror*|*xception*|*"not authorized"*)
      fail "the value read from $param looks like an ERROR MESSAGE, not a secret.
     Refusing to create a Secret from it. Nothing was created." ;;
  esac
  # Length only -- never the value, and never a prefix of it, since a prefix of a
  # provenance secret is still secret material.
  ok "secret read and validated in memory (${#secret_value} bytes); nothing created yet"

  step "create the fixture Secret"
  # INTENT BEFORE MUTATION. Written BEFORE the create so that if this process dies
  # between the create and the ledger record, the operator has a uid-recoverable
  # trail instead of an orphan. `recover-secret` below consumes it.
  local intent="$dir/secret-intent.json"
  python3 - "$intent" "$secret_name" "$NAMESPACE" "$NONCE" "$ACCOUNT" <<'PY'
import json, sys
path, name, ns, nonce, account = sys.argv[1:6]
# Deliberately NO secret value and no `data` block: an intent record is metadata.
# run_nonce/account_id/name/namespace are all re-validated by recover-secret, so a
# stale directory from another run cannot satisfy its existence check.
json.dump({"intent": "create-secret", "name": name, "namespace": ns,
           "run_nonce": nonce, "account_id": account, "state": "pending"},
          open(path, "w"), indent=2)
PY
  chmod 600 "$intent"

  # --from-file=...=/dev/stdin with a HERE-STRING rather than a pipe: the value
  # still never becomes an argv entry, but this process controls the ordering, so
  # there is no concurrent consumer to create an empty Secret behind our back.
  #
  # ---------------------------------------------------------------------------
  # THE RESPONSE IS NEVER WRITTEN TO DISK -- NOT EVEN BRIEFLY.
  #
  # `kubectl create secret -o json` returns the object INCLUDING its base64 `data`
  # block, i.e. the secret itself. The previous revision redirected that response to
  # secret-create.raw.json, parsed it, and THEN shredded it. Root was right that the
  # window is real and not theoretical: a crash, a SIGKILL, an out-of-space error or
  # a parse failure between the write and the shred leaves the trust root sitting in
  # the artifact directory, and the parse-failure branch is the one that fails most
  # plausibly. "Sanitised immediately after" is still "written".
  #
  # So the response is captured into a SHELL VARIABLE and parsed from the parser's
  # STDIN. Only sanitised metadata is ever written. `-o jsonpath` was the other
  # option, but a single jsonpath cannot report a MISSING uid distinguishably from
  # an empty one, and that distinction drives the recovery path below.
  # ---------------------------------------------------------------------------
  local rc_kube created
  set +e
  created="$(kubectl create secret generic "$secret_name" \
    --namespace "$NAMESPACE" \
    --from-file=BG_APIGW_PROVENANCE_SECRET=/dev/stdin \
    -o json 2>"$dir/secret-create.err" <<<"$secret_value")"
  rc_kube=$?
  set -e
  if [ "$rc_kube" -ne 0 ]; then
    created=""
    if grep -q 'AlreadyExists' "$dir/secret-create.err"; then
      fail "Secret $secret_name already exists. REFUSING to adopt it: it may hold
     another run's value, and recording it would authorise deleting it. Use a new nonce."
    fi
    fail "creating Secret $secret_name failed: $(cat "$dir/secret-create.err")"
  fi

  # Parse from STDIN and emit ONLY metadata. The receipt deliberately carries no
  # data/stringData rather than copying the document and hoping no future kubectl
  # version adds another secret-bearing field.
  #
  # THE PROGRAM ARRIVES ON FD 3, NOT ON STDIN.
  #
  # `python3 - <<'PY'` puts the PROGRAM on stdin, so `json.load(sys.stdin)` reads
  # the already-consumed script and sees an empty string -- every create failed
  # with "could not parse its metadata" after the Secret existed. stdin has to
  # carry the RESPONSE, so the program is fed through a separate descriptor. The
  # response still never becomes an argv entry or a file.
  local uid
  set +e
  uid="$(printf '%s' "$created" | python3 /dev/fd/3 "$dir/secret-receipt.json" \
           "$NONCE" "$ACCOUNT" "$secret_name" "$NAMESPACE" 3<<'PY'
import json, sys
receipt, nonce, account, expect_name, expect_ns = sys.argv[1:6]
doc = json.load(sys.stdin)
md = doc.get("metadata", {})
uid = md.get("uid") or ""
# The receipt is the ONLY ownership evidence recover-secret may act on, so it
# records the run identity too: recovery must be able to prove the intent and the
# receipt belong to THIS run before it records a live object as deletable.
json.dump({"kind": doc.get("kind"), "name": md.get("name"),
           "namespace": md.get("namespace"), "uid": uid,
           "resourceVersion": md.get("resourceVersion"),
           "run_nonce": nonce, "account_id": account,
           "expected_name": expect_name, "expected_namespace": expect_ns,
           "note": "metadata only; the secret value is deliberately absent"},
          open(receipt, "w"), indent=2)
print(uid)
PY
      )"
  local rc_parse=$?
  set -e
  created=""   # drop the response (which holds the secret) from this shell
  if [ "$rc_parse" -ne 0 ]; then
    fail "created Secret $secret_name but could not parse its metadata. The response is
     NOT on disk (it was never written there), so there is no secret material to
     clean up. Recover with:
       $0 recover-secret --nonce $NONCE --account-id $ACCOUNT --ledger $LEDGER"
  fi
  chmod 600 "$dir/secret-receipt.json"
  secret_value=""   # drop the value from this shell's memory as soon as it is unused

  [ -n "$uid" ] || fail "Secret created but the server returned NO uid. Do NOT delete by
     name -- a same-named object may be another run's. Recover with:
       $0 recover-secret --nonce $NONCE --account-id $ACCOUNT --ledger $LEDGER
     which re-reads the live object and records it by its ACTUAL uid."

  python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
    --account-id "$ACCOUNT" --kind Secret --name "$secret_name" \
    --namespace "$NAMESPACE" --uid "$uid" \
    || fail "created Secret $secret_name (uid $uid) but could NOT record it in the ledger.
     The object EXISTS and is currently unrecorded, so teardown would not know about
     it. Recover with:
       $0 recover-secret --nonce $NONCE --account-id $ACCOUNT --ledger $LEDGER
     Do NOT 'kubectl delete secret' by name. The uid above identifies the exact
     object this run created; a same-named object may belong to another run, and
     deleting it by name would destroy that run's trust root. recover-secret
     re-reads the live object and refuses unless its uid still matches $uid."
  ok "Secret/$secret_name created and recorded (uid $uid)"

  step "attach it to the fixture Deployment"
  # ---------------------------------------------------------------------------
  # OPTIMISTIC CONCURRENCY, VIA A MECHANISM kubectl ACTUALLY HAS.
  #
  # The previous revision passed `kubectl patch --resource-version=...`. THAT FLAG
  # DOES NOT EXIST. Root executed it; so did I, against the real client:
  #
  #   $ kubectl patch --local --resource-version=1 -f /dev/null -p '{}'
  #   error: unknown flag: --resource-version        (exit 1, no cluster needed)
  #
  # So the attach ALWAYS failed, after the Secret had already been created and
  # recorded -- leaving a fixture that still reads the ORDINARY gateway's provenance
  # secret while the run looked one step from done. The suite missed it because the
  # test double PARSED the invented flag, so the tests proved the script agreed with
  # the fake rather than with kubectl. tests/ now runs real `kubectl --local` for the
  # interface, and the double rejects unknown flags the way kubectl does.
  #
  # JSON Patch `test` operations are the supported equivalent and are strictly
  # STRONGER here: the API server evaluates every `test` op against the live object
  # and applies NOTHING unless all pass, so this closes the same window (the verified
  # fixture being replaced between the ownership check and the patch) AND pins the
  # uid, which --resource-version could not have done at all. Verified both ways on
  # the real client: matching uid+resourceVersion exits 0 and changes only the
  # targeted entry; a wrong uid prints
  #   error: testing value /metadata/uid failed: test failed
  # and exits 1.
  #
  # WHY THE ENV EDIT IS AN INDEXED REPLACE. JSON Patch has no merge-by-name, so the
  # exact index of BG_APIGW_PROVENANCE_SECRET is resolved from the object we just
  # read and pinned with its own `test` op. That keeps the other eight secret-backed
  # refs #3968's renderer deliberately carried over: a whole-list write is the exact
  # failure its render_fixture.py exists to prevent. The trust flag is appended with
  # `-` when absent, or replaced at its index when present.
  # ---------------------------------------------------------------------------
  local patch_body
  patch_body="$(python3 - "$deploy_json" "$deploy_uid" "$deploy_rv" "$secret_name" <<'PY'
import json, sys
deploy_json, uid, rv, secret = sys.argv[1:5]
doc = json.load(open(deploy_json))
containers = doc["spec"]["template"]["spec"]["containers"]
ci = next((i for i, c in enumerate(containers) if c.get("name") == "bedrockgateway"), None)
if ci is None:
    sys.exit("the fixture Deployment has no container named 'bedrockgateway'; "
             "refusing to guess which one carries the gateway.")
env = containers[ci].get("env") or []
base = f"/spec/template/spec/containers/{ci}"

# Every `test` op is a precondition the SERVER checks before applying anything.
ops = [
    {"op": "test", "path": "/metadata/uid", "value": uid},
    {"op": "test", "path": "/metadata/resourceVersion", "value": rv},
    {"op": "test", "path": f"{base}/name", "value": "bedrockgateway"},
]

def index_of(name):
    return next((i for i, e in enumerate(env) if e.get("name") == name), None)

prov = index_of("BG_APIGW_PROVENANCE_SECRET")
ref = {"name": secret, "key": "BG_APIGW_PROVENANCE_SECRET"}
if prov is None:
    # #3968's renderer carries this ref over from the live gateway, so its absence
    # means the target is not the composition this component was designed against.
    sys.exit("BG_APIGW_PROVENANCE_SECRET is not present on the fixture container. "
             "#3968's renderer carries it over from the live gateway, so this is not "
             "the expected fixture composition; refusing to patch.")
# Pin the entry being replaced by NAME as well, so an env list reordered between the
# read and the patch cannot cause a different variable to be overwritten.
ops.append({"op": "test", "path": f"{base}/env/{prov}/name",
            "value": "BG_APIGW_PROVENANCE_SECRET"})
ops.append({"op": "replace", "path": f"{base}/env/{prov}/valueFrom/secretKeyRef",
            "value": ref})

trust = index_of("BG_TRUST_APIGW_HEADERS")
if trust is None:
    ops.append({"op": "add", "path": f"{base}/env/-",
                "value": {"name": "BG_TRUST_APIGW_HEADERS", "value": "true"}})
else:
    ops.append({"op": "test", "path": f"{base}/env/{trust}/name",
                "value": "BG_TRUST_APIGW_HEADERS"})
    ops.append({"op": "replace", "path": f"{base}/env/{trust}",
                "value": {"name": "BG_TRUST_APIGW_HEADERS", "value": "true"}})

print(json.dumps(ops))
PY
  )" || fail "could not build the attach patch; see above. NOTHING was patched."

  kubectl patch deployment "$FIXTURE_DEPLOY" -n "$NAMESPACE" --type=json \
    -p "$patch_body" \
    || fail "could not attach the Secret to $FIXTURE_DEPLOY. If the error mentions a
     failed 'test' operation, the Deployment CHANGED since it was verified as this
     run's fixture -- re-run handoff rather than forcing the patch; nothing was
     applied. The Secret IS recorded in the ledger (uid $uid). Until this patch
     succeeds the fixture still reads the ORDINARY gateway's provenance secret, so
     do NOT proceed to verification."
  ok "deployment/$FIXTURE_DEPLOY now reads BG_APIGW_PROVENANCE_SECRET from Secret/$secret_name"
  note "BG_TRUST_APIGW_HEADERS=true is set on the FIXTURE deployment only. It is true"
  note "there only because this component blanks both trusted headers on its"
  note "auth-NONE route; it must never be set on the ordinary gateway."

  # Prove the attachment rather than trusting the patch's exit code.
  step "verify the attachment landed"
  python3 - "$NAMESPACE" "$FIXTURE_DEPLOY" "$secret_name" "$deploy_uid" <<'PY' || fail "attachment verification failed"
import json, subprocess, sys
ns, deploy, secret, expect_uid = sys.argv[1:5]
spec = json.loads(subprocess.run(
    ["kubectl", "get", "deployment", deploy, "-n", ns, "-o", "json"],
    capture_output=True, text=True, check=True).stdout)
# The object we just patched must still be the object we verified. resourceVersion
# guards the patch; only the uid can prove no delete-and-recreate happened around
# it -- and if one did, the env below belongs to a workload nobody vouched for.
live_uid = spec.get("metadata", {}).get("uid")
if live_uid != expect_uid:
    sys.exit(
        f"the Deployment uid changed from {expect_uid} to {live_uid}: it was replaced "
        "during handoff. The patch may have landed on a DIFFERENT workload. Investigate "
        "before proceeding; do not treat this as attached."
    )
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
# recover-secret — UID-BOUND recovery for a partially-created handoff
# ===========================================================================
# Exists because the alternative advice was "kubectl delete secret <name>", and
# delete-by-name is exactly the unsafe operation this component refuses elsewhere:
# a same-named Secret may be another run's trust root, and deleting it would break
# that run while looking like tidy-up.
#
# The recoverable state is: the Secret was CREATED, its uid WAS captured in the
# receipt, but the ledger write failed -- so an object exists that teardown does not
# know about. Recovery re-reads the LIVE object and records it ONLY if it is still
# the exact object the receipt names.
#
# ---------------------------------------------------------------------------
# WHY INTENT ALONE IS NOT OWNERSHIP (root executed this).
#
# The previous revision checked only that the intent FILE EXISTED, then recorded
# whatever uid was live. Root ran it with a receipt naming one object and a
# different live uid: it exited 0 and recorded the REPLACEMENT. Recording is what
# authorises deletion, so that path could hand another run's trust root -- or an
# unrelated same-named Secret -- to this run's teardown.
#
# An intent record proves an attempt was MADE. It cannot prove which object now
# holds that name: between the attempt and recovery the object may have been
# deleted and recreated by another run, or the create may have hit AlreadyExists
# against an object this run never made. Only a server-assigned uid captured at
# creation can distinguish those, so recovery REQUIRES the receipt's uid and
# refuses when there is none. An unverifiable partial creation is escalated, not
# adopted -- the safe failure is "a human looks at it", never "assume it is ours".
# ---------------------------------------------------------------------------
cmd_recover_secret() {
  require_nonce; require_account
  [ -n "$LEDGER" ] || fail "--ledger is required"
  [ -f "$OWNERSHIP_LIB" ] || fail "#3968 ownership library not found at $OWNERSHIP_LIB"
  assert_live_account
  assert_kube_context
  resolve_run_id

  local dir; dir="$(artifact_dir)"
  local secret_name="w2-fixture-provenance-${NONCE}"
  local intent="$dir/secret-intent.json"
  local receipt="$dir/secret-receipt.json"

  # Without the intent record there is no evidence THIS run created the object, and
  # recording someone else's Secret would authorise deleting it at teardown.
  [ -f "$intent" ] || fail "no creation intent at $intent, so there is no evidence this run
     created Secret/$secret_name. REFUSING to record it: recording implies authority
     to delete, and a same-named Secret may belong to another run. Investigate by
     hand:
       kubectl get secret $secret_name -n $NAMESPACE -o jsonpath='{.metadata.uid}'"

  # The intent must belong to THIS run. A stale directory from another nonce or
  # account would otherwise satisfy the existence check above.
  python3 - "$intent" "$NONCE" "$ACCOUNT" "$secret_name" "$NAMESPACE" <<'PY' \
    || fail "the creation intent does not match this command; see above."
import json, sys
path, nonce, account, name, ns = sys.argv[1:6]
try:
    doc = json.load(open(path))
except Exception as exc:                       # noqa: BLE001
    sys.exit(f"could not read the creation intent {path}: {exc}")
for field, want, got in (("run_nonce", nonce, doc.get("run_nonce")),
                         ("account_id", account, doc.get("account_id")),
                         ("name", name, doc.get("name")),
                         ("namespace", ns, doc.get("namespace"))):
    if got != want:
        sys.exit(
            f"the creation intent's {field} is {got!r}, but this command is for {want!r}. "
            "REFUSING: this artifact directory belongs to a different run/account/object, "
            "and recording from it would authorise deleting something this run did not "
            "create."
        )
print("  [ ok ] creation intent matches this run")
PY

  # THE UID RECEIPT IS MANDATORY. Without it there is no server-observed creation
  # marker, so nothing can distinguish our object from a same-named replacement.
  [ -f "$receipt" ] || fail "no uid receipt at $receipt. The Secret's server-assigned uid was
     never captured, so there is NO evidence identifying which object this run
     created -- only that it tried. REFUSING to record any live Secret as ours:
     adopting an AlreadyExists or foreign object is exactly the failure this path
     exists to prevent. Escalate and inspect by hand:
       kubectl get secret $secret_name -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'
     Compare that uid against the audit trail before deleting anything."
  local expect_uid
  expect_uid="$(python3 - "$receipt" "$NONCE" "$ACCOUNT" "$secret_name" "$NAMESPACE" <<'PY'
import json, sys
path, nonce, account, name, ns = sys.argv[1:6]
doc = json.load(open(path))
for field, want, got in (("run_nonce", nonce, doc.get("run_nonce")),
                         ("account_id", account, doc.get("account_id")),
                         ("name", name, doc.get("name")),
                         ("namespace", ns, doc.get("namespace"))):
    if got != want:
        sys.exit(f"the uid receipt's {field} is {got!r}, expected {want!r}; it belongs to a "
                 "different run/account/object. Refusing.")
uid = doc.get("uid") or ""
if not uid:
    sys.exit("the uid receipt records NO uid, so the object this run created cannot be "
             "identified. Refusing to adopt whatever is live.")
print(uid)
PY
  )" || fail "the uid receipt is unusable; see above. Nothing was recorded."

  local live_json="$dir/secret-recover.json"
  kubectl get secret "$secret_name" -n "$NAMESPACE" \
    -o 'jsonpath={.metadata.uid}{"\n"}{.metadata.resourceVersion}' \
    > "$live_json" 2>"$dir/secret-recover.err" \
    || fail "Secret/$secret_name does not exist in $NAMESPACE, so there is nothing to
     recover. If the create genuinely failed, no object leaked and you can re-run
     handoff with the same nonce. Error: $(tr -d '\n' < "$dir/secret-recover.err")"
  # jsonpath, not -o json: the full object carries the base64 secret data, and this
  # recovery path must not write it to disk any more than handoff does.
  local uid; uid="$(sed -n 1p "$live_json")"
  [ -n "$uid" ] || fail "Secret/$secret_name exists but returned no uid. Do NOT delete by
     name. Escalate: an object with no uid cannot be safely recorded or removed."
  rm -f "$live_json"

  # THE REFUSAL ROOT'S REPRODUCTION NEEDED. A live uid that differs from the one
  # creation recorded means the object under this name is NOT the one this run
  # created -- it was deleted and recreated, or the name was taken by another run.
  [ "$uid" = "$expect_uid" ] || fail "Secret/$secret_name is NOT the object this run created.
       recorded at creation : $expect_uid
       live now             : $uid
     A uid changes only on delete-and-recreate, so the live object belongs to
     something else. REFUSING to record it: that would authorise teardown to delete
     another run's trust root. Do NOT delete it by name. The object this run created
     no longer exists, so nothing of ours is leaking here; escalate the mismatch."
  ok "live uid matches the uid recorded at creation ($uid)"

  python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
    --account-id "$ACCOUNT" --kind Secret --name "$secret_name" \
    --namespace "$NAMESPACE" --uid "$uid" \
    || fail "could not record Secret/$secret_name (uid $uid) in the ledger. The object
     STILL EXISTS and is STILL unrecorded. Do not delete it by name; resolve the
     ledger problem and re-run this command."
  python3 - "$intent" <<'PY'
import json, sys
p = sys.argv[1]
doc = json.load(open(p))
doc["state"] = "recovered"
json.dump(doc, open(p, "w"), indent=2)
PY
  ok "Secret/$secret_name recorded by its actual uid ($uid); teardown will remove it"
  note "handoff did NOT complete: the Secret is recorded but may not be ATTACHED."
  note "Re-run 'handoff' to attach it, or tear down if you are abandoning this run."
}

# ===========================================================================
# destroy — dependency-ordered, against exact owned state
# ===========================================================================
cmd_destroy() {
  require_nonce; require_account
  assert_live_account
  assert_backend_binding
  local dir; dir="$(artifact_dir)"
  local tfvars="$dir/fixture.tfvars"
  [ -f "$tfvars" ] || fail "reviewed inputs not found at $tfvars. Destroy must run against the
     SAME inputs that were applied. Do NOT fall back to deleting by name prefix:
     a name prefix is not ownership, and a same-named replacement created by
     someone else would be destroyed instead."

  cd "$ROOT_DIR"
  local api_id
  api_id="$(terraform_ output -raw rest_api_id 2>/dev/null || echo "")"

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
  terraform_ plan -destroy -input=false -var-file="$tfvars" $refresh_flag \
    -out="$dir/destroy.plan" \
    || fail "could not plan the destroy. If the fixture ALB/Ingress was ALREADY deleted,
     the data source read fails and this is expected — re-run with --recover, which
     adds -refresh=false. Do not resort to deleting resources by name."
  terraform_ show -json "$dir/destroy.plan" > "$dir/destroy.plan.json"
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
  terraform_ apply -input=false "$dir/destroy.plan" \
    || fail "destroy failed partway. State is isolated per nonce, so re-running is safe
     and idempotent. Resolve and re-run; do NOT delete by name prefix."
  ok "fixture edge destroyed"

  step "3/3 verify ABSENCE of the COMPLETE owned set, and that the ordinary edge is untouched"
  # ---------------------------------------------------------------------------
  # Every probe below goes through probe_absent, which distinguishes "the service
  # said NotFound" from "the call failed". See its definition for the executed
  # failure this replaces: AccessDenied previously read as proof of deletion and
  # this function exited 0 claiming "teardown verified".
  #
  # It also verifies the COMPLETE owned set. The previous revision checked the API
  # and the per-run parameter only, so a surviving stage or log group was reported
  # as a verified teardown.
  # ---------------------------------------------------------------------------
  local problems=0 unknowns=0

  # A swallowed rest_api_id read is itself a verification gap: with api_id empty
  # the whole API probe was silently skipped and the run still reported success.
  if [ -z "$api_id" ]; then
    printf '  [FAIL] could not read rest_api_id from state, so the fixture API CANNOT be\n' >&2
    printf '         verified absent. This is not the same as it being gone.\n' >&2
    problems=1
  fi

  _check_absent() {   # <label> <not-found-pattern> <command...>
    local label="$1" pat="$2"; shift 2
    local rc=0
    # Same reason as in probe_absent: the `if` form is errexit-exempt, so a
    # "STILL PRESENT" (rc 1) or "UNKNOWN" (rc 2) reaches the case below instead of
    # terminating the script before it can be reported.
    if probe_absent "$pat" "$@"; then rc=0; else rc=$?; fi
    case "$rc" in
      0) ok "$label is gone (service reported not-found)" ;;
      1) printf '  [FAIL] %s is STILL PRESENT\n' "$label" >&2; problems=1 ;;
      2) printf '  [UNKNOWN] %s could not be verified: %s\n' "$label" "$PROBE_ERROR" >&2
         printf '            This is NOT absence. Fix the credential/permission and re-run.\n' >&2
         unknowns=1 ;;
    esac
  }

  if [ -n "$api_id" ]; then
    _check_absent "fixture REST API $api_id" 'NotFoundException|does not exist' \
      aws_ apigateway get-rest-api --rest-api-id "$api_id"
    _check_absent "fixture stage ${ENVIRONMENT} on $api_id" 'NotFoundException|does not exist' \
      aws_ apigateway get-stage --rest-api-id "$api_id" --stage-name "$ENVIRONMENT"
  fi

  local param="/adp/${ENVIRONMENT}/gateway/fixture/${NONCE}/apigw-provenance-secret"
  _check_absent "per-run secret $param" 'ParameterNotFound' \
    aws_ ssm get-parameter --name "$param"

  # Previously unchecked entirely, so a surviving log group (which keeps the access
  # logs, and costs) counted as a verified teardown.
  local lg="/aws/apigateway/w2-fixture-edge-${NONCE}"
  local lg_out lg_rc
  set +e
  lg_out="$(aws_ logs describe-log-groups --log-group-name-prefix "$lg" \
    --query 'logGroups[].logGroupName' --output text 2>&1)"
  lg_rc=$?
  set -e
  if [ "$lg_rc" -ne 0 ]; then
    printf '  [UNKNOWN] log group %s could not be verified: %s\n' "$lg" \
      "$(printf '%s' "$lg_out" | tr -d '\n' | cut -c1-200)" >&2
    unknowns=1
  elif [ -n "$lg_out" ] && [ "$lg_out" != "None" ]; then
    printf '  [FAIL] log group %s is STILL PRESENT\n' "$lg" >&2; problems=1
  else
    # describe-* returns an empty LIST rather than an error, so empty IS absence
    # here. Noted because it is the one probe where a 0 exit proves absence.
    ok "log group $lg is gone (empty result set, not a failed call)"
  fi

  # The most important post-check: this component must have changed nothing
  # ordinary. Checked by READING, never by writing. Note the inverted polarity --
  # here a NotFound is the ALARM, so an unknown error must not be read as presence
  # either.
  local ord_out ord_rc
  set +e
  ord_out="$(aws_ ssm get-parameter \
    --name "/adp/${ENVIRONMENT}/gateway/apigw-provenance-secret" \
    --query 'Parameter.Name' --output text 2>&1)"
  ord_rc=$?
  set -e
  if [ "$ord_rc" -eq 0 ]; then
    ok "ordinary provenance parameter still present (value not read)"
  elif printf '%s' "$ord_out" | grep -q 'ParameterNotFound'; then
    printf '  [FAIL] the ORDINARY provenance parameter is MISSING. Investigate immediately:\n' >&2
    printf '         this component must never touch it.\n' >&2
    problems=1
  else
    printf '  [UNKNOWN] could not confirm the ordinary parameter survived: %s\n' \
      "$(printf '%s' "$ord_out" | tr -d '\n' | cut -c1-200)" >&2
    unknowns=1
  fi

  [ "$unknowns" -eq 0 ] || fail "one or more resources could NOT be verified (see [UNKNOWN] above).
     An unverifiable resource is NOT a deleted resource. Report cleanup as
     UNVERIFIED -- #3968's cleanup_ok must stay False -- fix the access problem and
     re-run this step."
  [ "$problems" -eq 0 ] || fail "teardown did NOT fully verify. Do not report cleanup as complete."
  ok "teardown verified: complete owned set confirmed absent by service not-found"

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
  assert_backend_binding
  cd "$ROOT_DIR"
  local endpoint api_id
  endpoint="$(terraform_ output -raw worker_control_endpoint)" || fail "no endpoint in state"
  api_id="$(terraform_ output -raw rest_api_id)" || fail "no rest_api_id in state"

  step "edge identity"
  printf '  endpoint: %s\n' "$endpoint"
  # The ordinary API id must be resolved AUTHORITATIVELY. Previously an unreadable
  # parameter downgraded to "compare it by hand" and the run continued, so the one
  # check that proves the fixture is not the production edge could be skipped by a
  # transient SSM failure.
  local ordinary ord_rc
  set +e
  ordinary="$(aws_ ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/api-gateway-id" \
    --query Parameter.Value --output text 2>&1)"
  ord_rc=$?
  set -e
  [ "$ord_rc" -eq 0 ] || fail "could not read the ORDINARY api-gateway-id from SSM:
     $(printf '%s' "$ordinary" | tr -d '\n' | cut -c1-200)
     Refusing to continue: without it this cannot prove the fixture edge is not the
     production edge, and 'compare it by hand' is not a check."
  [ -n "$ordinary" ] && [ "$ordinary" != "None" ] \
    || fail "the ordinary api-gateway-id resolved EMPTY. Refusing to continue."
  [ "$api_id" != "$ordinary" ] \
    || fail "the fixture API id EQUALS the ordinary edge's ($ordinary). STOP."
  ok "distinct from the ordinary edge ($api_id != $ordinary)"

  # ---------------------------------------------------------------------------
  # THE REFUSALS ARE ASSERTIONS, NOT OBSERVATIONS.
  #
  # Root EXECUTED the previous revision with a curl returning 200 for both the
  # unsigned and the spoofed probe: verify printed "EXPECTED 403" notes and EXITED
  # 0. A security control that reports success when the edge answered 200 to an
  # unsigned request is worse than no control, because the operator then has a
  # green verification to point at.
  #
  # Every probe below sets a failure flag. A `note` cannot fail a run.
  # ---------------------------------------------------------------------------
  step "the refusals (each observed AND asserted)"
  local failures=0

  _expect_refused() {   # <label> <code>
    local label="$1" code="$2"
    case "$code" in
      403)
        ok "$label -> 403 (refused)" ;;
      000)
        printf '  [FAIL] %s -> no response (000). The refusal was NOT observed; an\n' "$label" >&2
        printf '         unreachable endpoint is not a refusal.\n' >&2
        failures=1 ;;
      5*)
        printf '  [FAIL] %s -> %s. A 5xx means the request may have REACHED A BACKEND.\n' \
          "$label" "$code" >&2
        failures=1 ;;
      *)
        printf '  [FAIL] %s -> %s. NOT REFUSED. The fixture edge is accepting requests it\n' \
          "$label" "$code" >&2
        printf '         must reject. Do not point a worker at this endpoint.\n' >&2
        failures=1 ;;
    esac
  }

  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$endpoint/bootstrap" || echo 000)"
  _expect_refused "unsigned" "$code"

  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$endpoint/bootstrap" \
    -H 'X-Caller-Identity: arn:aws:iam::000000000000:role/anything' \
    -H 'X-Adp-Edge-Provenance: forged' || echo 000)"
  _expect_refused "spoofed headers, unsigned" "$code"

  # WRONG ROLE, CORRECTLY SIGNED. This is the one control that exercises this
  # component's resource-policy Deny rather than API Gateway's AWS_IAM check, so
  # skipping it leaves the policy itself unverified. It needs a SigV4 signer, so it
  # is EXECUTED when a signer profile is supplied and an explicit FAILURE otherwise
  # -- not a note, which is how it previously went permanently unrun.
  if [ -n "$WRONG_ROLE_PROFILE" ]; then
    # PROVE THE IDENTITY FIRST. A 403 observed while signing as an ALLOWLISTED role
    # (or as an identity that cannot be resolved at all) says nothing about the Deny,
    # and recording it as the wrong-role refusal would be a false proof. The previous
    # revision never checked who it signed as.
    local probe_arn
    probe_arn="$(sigv4_probe_identity "$WRONG_ROLE_PROFILE")"
    if [ -z "$probe_arn" ] || [ "$probe_arn" = "None" ]; then
      printf '  [FAIL] wrong-role control: could not resolve the identity of profile %s via\n' \
        "$WRONG_ROLE_PROFILE" >&2
      printf '         sts get-caller-identity, so what it signs as is UNKNOWN. An unverified\n' >&2
      printf '         signer cannot prove the Deny; refusing to record this control.\n' >&2
      failures=1
    else
      # An assumed-role request presents sts::assumed-role/NAME/SESSION, and
      # aws:PrincipalArn in the policy is the iam::role form. Compare on the ROLE
      # NAME so an allowlisted role is recognised in either representation.
      local probe_role
      probe_role="$(printf '%s' "$probe_arn" | awk -F/ '{print $2}')"
      local allowlisted=0 allowed_arns
      allowed_arns="$(terraform_ output -json allowed_caller_role_arns 2>/dev/null \
        | python3 -c 'import json,sys
try:
    print("\n".join(json.load(sys.stdin)))
except Exception:
    pass' || true)"
      if [ -z "$allowed_arns" ]; then
        printf '  [FAIL] wrong-role control: could not read allowed_caller_role_arns from state,\n' >&2
        printf '         so this cannot confirm profile %s is NOT allowlisted. A probe that\n' "$WRONG_ROLE_PROFILE" >&2
        printf '         might be signing as a PERMITTED role cannot prove the Deny.\n' >&2
        failures=1
      else
        while IFS= read -r arn; do
          [ -n "$arn" ] || continue
          [ "$(printf '%s' "$arn" | awk -F/ '{print $NF}')" = "$probe_role" ] && allowlisted=1
        done <<<"$allowed_arns"
        if [ "$allowlisted" = 1 ]; then
          printf '  [FAIL] wrong-role control: profile %s signs as %s, whose role IS in\n' \
            "$WRONG_ROLE_PROFILE" "$probe_arn" >&2
          printf '         allowed_caller_role_arns. A refusal (or acceptance) from an\n' >&2
          printf '         ALLOWLISTED identity proves nothing about the Deny. Use a profile\n' >&2
          printf '         for a role that is genuinely NOT allowlisted.\n' >&2
          failures=1
        else
          ok "wrong-role probe signs as $probe_arn (verified NOT allowlisted)"
          code="$(aws_sigv4_probe "$WRONG_ROLE_PROFILE" "$endpoint/bootstrap" || echo 000)"
          _expect_refused "wrong role (correctly signed as $probe_role)" "$code"
        fi
      fi
    fi
  else
    # -----------------------------------------------------------------------
    # A SKIPPED REQUIRED CONTROL MAKES ACCEPTANCE NONZERO.
    #
    # Root executed the previous revision with --skip-wrong-role: it exited 0 and
    # printed "all refusals observed AND asserted". The skip suppressed the failure
    # flag entirely, so the one control that exercises this component's own resource
    # policy could be turned off and the run still reported full verification. A
    # green exit that coexists with an unrun security control is a false acceptance.
    #
    # --skip-wrong-role is now a DOCUMENTATION flag, not a waiver: it records the
    # control as deliberately unrun and still exits nonzero, because acceptance was
    # not established either way.
    # -----------------------------------------------------------------------
    printf '  [FAIL] wrong-role control NOT RUN: pass --wrong-role-profile <profile> for an\n' >&2
    printf '         identity NOT in allowed_caller_role_arns. This is the only probe that\n' >&2
    printf '         exercises THIS component resource-policy Deny; without it the Deny is\n' >&2
    printf '         unverified.\n' >&2
    if [ "$SKIP_WRONG_ROLE" = 1 ]; then
      printf '         --skip-wrong-role recorded: the control is EXPLICITLY SKIPPED and the\n' >&2
      printf '         Deny remains UNVERIFIED. This still fails: skipping a required control\n' >&2
      printf '         records why acceptance is incomplete, it does not grant acceptance.\n' >&2
    fi
    failures=1
  fi

  # POSITIVE CONTROL. Refusals alone cannot distinguish a correctly-restricted edge
  # from a totally broken one: an endpoint that 403s everything passes every check
  # above. The human JWT route must still be reachable and must NOT be 403ed by the
  # resource policy, which is the defect area 2 fixed.
  if [ -n "$HUMAN_PROBE_PATH" ]; then
    local base="${endpoint%/internal/v1/agent}"
    code="$(curl -s -o /dev/null -w '%{http_code}' "${base}${HUMAN_PROBE_PATH}" || echo 000)"
    case "$code" in
      200|401)
        # 401 is a PASS: it means the request reached the gateway and was rejected
        # by the application's JWT check -- the layer that should decide -- rather
        # than being blocked at the edge.
        ok "human route ${HUMAN_PROBE_PATH} -> $code (reached the gateway; app-layer auth decided)" ;;
      403)
        printf '  [FAIL] human route %s -> 403. The resource policy is refusing an\n' "$HUMAN_PROBE_PATH" >&2
        printf '         auth-NONE human route: an unsigned human request has no\n' >&2
        printf '         aws:PrincipalArn, so a broad Deny blocks it before the gateway can\n' >&2
        printf '         authenticate. The Deny must stay scoped to /internal.\n' >&2
        failures=1 ;;
      404)
        printf '  [FAIL] human route %s -> 404. The fixture ALB does not publish this path,\n' "$HUMAN_PROBE_PATH" >&2
        printf '         so human session/control traffic hits the default action.\n' >&2
        failures=1 ;;
      *)
        printf '  [FAIL] human route %s -> %s (expected 200 or 401)\n' "$HUMAN_PROBE_PATH" "$code" >&2
        failures=1 ;;
    esac
  else
    printf '  [FAIL] human positive control NOT RUN: pass --human-probe-path (e.g.\n' >&2
    printf '         /%s/api/health). Refusals alone cannot tell a correctly restricted\n' "$ENVIRONMENT" >&2
    printf '         edge from one that refuses everything.\n' >&2
    failures=1
  fi

  [ "$failures" -eq 0 ] || fail "one or more security controls FAILED or was NOT RUN (see
     [FAIL] above). Do NOT point a protected worker at this endpoint and do not
     record live acceptance. This exits nonzero deliberately: the previous revision
     printed notes and exited 0 even when the edge answered 200 to an unsigned
     request, and again when --skip-wrong-role turned the Deny control off."
  # Claimed ONLY on the path where every control ran and passed. --skip-wrong-role
  # can no longer reach this line.
  ok "all refusals observed AND asserted (unsigned, spoofed, wrong-role); human route reachable"
}

case "$CMD" in
  init)    cmd_init ;;
  plan)    cmd_plan ;;
  apply)   cmd_apply ;;
  handoff) cmd_handoff ;;
  recover-secret) cmd_recover_secret ;;
  verify)  cmd_verify ;;
  destroy) cmd_destroy ;;
  ""|-h|--help) sed -n '1,45p' "$0" ;;
  *) fail "unknown subcommand: $CMD
     (init|plan|apply|handoff|recover-secret|verify|destroy)" ;;
esac

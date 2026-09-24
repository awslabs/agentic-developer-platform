#!/usr/bin/env bash
# =============================================================================
# Create the fixture-owned internal ALB — Issue #5836
# =============================================================================
# WHAT THIS IS FOR
# ----------------
# The fixture trusted edge (../main.tf) forwards to a FIXTURE-OWNED internal ALB.
# Nothing created that ALB: #3968's renderer emits a Deployment, a ClusterIP
# Service and NetworkPolicies, and NO Ingress (verified on branch
# agent/issue-3968). This script creates the missing piece from
# ../fixture-alb.yaml.tmpl and records it in #3968's EXISTING ledger, so the
# ordering is create-ALB -> terraform apply -> worker bootstrap, with no circular
# prerequisite.
#
# It does not modify anything under platform/scripts/operator/wave2/. It calls
# that tooling's supported `lib/ownership.py record-k8s` interface, which already
# stores a server-assigned uid per object and is already consumed by
# `90-cleanup-ledger.sh`. An Ingress is an ordinary uid-bearing Kubernetes object,
# so it needs NO new ledger type and teardown reaches it through the existing k8s
# bucket.
#
# OWNERSHIP AND WHY `create` RATHER THAN `apply`
# ----------------------------------------------
# `kubectl apply` ADOPTS a pre-existing object of the same name and mutates it,
# and the ledger would then authorise deleting something this run did not create.
# `create` fails on AlreadyExists instead. This mirrors the rule #3968's
# 10-create-fixture.sh already follows, deliberately, for the same reason.
#
# WHAT IS PROVEN *BEFORE* THE CLUSTER IS TOUCHED
# ----------------------------------------------
# Four defects root found in the previous revision, all of them ordering or
# evidence problems rather than missing features:
#
#  1. THE ACCOUNT WAS RESOLVED AFTER THE CREATE. `aws sts get-caller-identity`
#     ran only to label the ledger row, four steps past `kubectl create`. So the
#     account this run acts on was unknown at the moment it mutated, and a
#     credential pointing somewhere else produced a real Ingress and then failed
#     at the record step. Account and cluster identity are now asserted first,
#     and the account is REQUIRED as an argument so there is something to compare
#     against -- an ambient value agrees with itself.
#
#  2. THE SERVICE CHECK WAS EXISTENCE ONLY. `kubectl get service` proves a name
#     is taken, not that the object behind it is THIS run's fixture. An ordinary
#     or another run's Service of the same name would pass, and the Ingress would
#     then route the fixture edge's trusted-header traffic into it. The Service's
#     run labels (adp.io/w2-fixture, adp.io/w2-nonce -- set by #3968's renderer),
#     its ClusterIP type and the port the Ingress backend actually references are
#     all verified against this run's values.
#
#  3. NOTHING RECORDED THE INTENT TO CREATE. A create that succeeded server-side
#     but whose response was lost (killed process, dropped connection) left an
#     Ingress -- and therefore an ALB -- with no trace on disk and no ledger row.
#     An intent record is now written BEFORE the create, and `--recover` consumes
#     it. Recovery REQUIRES the server-assigned uid: an intent proves an attempt
#     was made, it cannot prove which object now holds the name, so recovery
#     refuses rather than adopting whatever is live.
#
#  4. THE FAILURE PATHS ADVISED DELETING BY NAME, and the closing note had the
#     teardown ORDER BACKWARDS. Both are corrected below; see the teardown note
#     at the end of this file and RUNBOOK.md section 8.
#
# SCOPE: this script MUTATES the cluster. It is for ROOT's authorized execution,
# not for the developer run that wrote it. --check-only is read-only (server
# dry-run) and is what the developer can legitimately exercise.
#
# Usage:
#   ./create-fixture-alb.sh --run-id w2-... --run-nonce <hex> --ledger <path> \
#       --account-id 879318057152 [--region us-east-1] [--profile adp-embark1] \
#       [--expect-cluster adp-dev-eks] \
#       --namespace adp-gateway --service <fixture-service> \
#       --alb-security-groups sg-aaa,sg-bbb [--port 80] [--check-only]
#
#   ./create-fixture-alb.sh --recover ...same bindings...
#       Records an Ingress this run created but did not manage to record, by its
#       ACTUAL uid, refusing unless the uid still matches the one captured at
#       creation.
#
# Discover --alb-security-groups read-only. It must be a group the VPC Link ALREADY
# egresses to on the listener port, and which itself admits the link -- see
# ../fixture-alb.yaml.tmpl for why. Reading rules is the authoritative way to see
# that: describe-security-groups returns no rule detail worth matching on, and an
# id list alone says nothing about port, protocol or direction.
#   aws apigatewayv2 get-vpc-links --query 'Items[].SecurityGroupIds'
#   aws ec2 describe-security-group-rules \
#     --filters Name=group-id,Values=<link-sg> \
#     --query 'SecurityGroupRules[?IsEgress==`true`].{To:ReferencedGroupInfo.GroupId,Proto:IpProtocol,From:FromPort,Until:ToPort}'
#
# This script does NOT verify reachability, and must not be read as doing so: the
# id passed here is what the ALB is TOLD to reuse. main.tf's run_binding_gate reads
# the live rules on both sides and refuses unless each direction is carried by a
# real rule on the fixture port.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$HERE/.." && pwd)"
TEMPLATE="$ROOT_DIR/fixture-alb.yaml.tmpl"

RUN_ID=""; RUN_NONCE=""; LEDGER=""; NAMESPACE=""; SERVICE=""
ALB_SGS=""; PORT="80"; CHECK_ONLY=0; RECOVER=0
ACCOUNT=""; REGION="us-east-1"; PROFILE=""; EXPECT_CLUSTER=""; ARTIFACT_DIR=""
# Defaults to the #3968 tooling location; overridable for a non-standard checkout.
OWNERSHIP_LIB="${W2_OWNERSHIP_LIB:-$ROOT_DIR/../../../../platform/scripts/operator/wave2/lib/ownership.py}"

fail() { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }
ok()   { printf '  [ ok ] %s\n' "$*"; }
note() { printf '  [note] %s\n' "$*"; }
step() { printf '\n=== %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)              RUN_ID="${2:?}"; shift 2 ;;
    --run-nonce)           RUN_NONCE="${2:?}"; shift 2 ;;
    --ledger)              LEDGER="${2:?}"; shift 2 ;;
    --namespace)           NAMESPACE="${2:?}"; shift 2 ;;
    --service)             SERVICE="${2:?}"; shift 2 ;;
    --alb-security-groups) ALB_SGS="${2:?}"; shift 2 ;;
    --port)                PORT="${2:?}"; shift 2 ;;
    --account-id)          ACCOUNT="${2:?}"; shift 2 ;;
    --region)              REGION="${2:?}"; shift 2 ;;
    --profile)             PROFILE="${2:?}"; shift 2 ;;
    --expect-cluster)      EXPECT_CLUSTER="${2:?}"; shift 2 ;;
    --artifact-dir)        ARTIFACT_DIR="${2:?}"; shift 2 ;;
    --check-only)          CHECK_ONLY=1; shift ;;
    --recover)             RECOVER=1; shift ;;
    -h|--help)             sed -n '1,82p' "$0"; exit 0 ;;
    *)                     fail "unknown argument: $1" ;;
  esac
done

# Bind every AWS call to the named profile, so nothing silently falls back to an
# ambient credential for a different account. Same helper as
# fixture-lifecycle.sh, for the same reason.
aws_() {
  if [ -n "$PROFILE" ]; then
    aws --profile "$PROFILE" --region "$REGION" "$@"
  else
    aws --region "$REGION" "$@"
  fi
}

# --- input validation: refuse before touching the cluster -------------------
[ -n "$RUN_ID" ]    || fail "--run-id is required (the #3968 ledger run id)"
[ -n "$RUN_NONCE" ] || fail "--run-nonce is required; it becomes the AdpFixtureRun tag the Terraform gate verifies"
[ -n "$NAMESPACE" ] || fail "--namespace is required"
[ -n "$SERVICE" ]   || fail "--service is required (the fixture ClusterIP Service created by #3968's renderer)"
# REQUIRED, and required before anything is created. An ambient account resolves
# to itself and therefore cannot disagree with itself; only an explicit expected
# value makes `sts get-caller-identity` a check rather than a label.
[ -n "$ACCOUNT" ]   || fail "--account-id is required. The previous revision resolved the account
     AFTER creating the Ingress, purely to label the ledger row, so a credential
     pointing at another account created a real ALB and only then failed. Naming the
     expected account is what turns the identity read into a refusal."
case "$ACCOUNT" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) fail "--account-id must be 12 digits" ;;
esac
# --ledger is required even in check-only mode, so the happy path cannot create a
# real object without a place to record it.
[ -n "$LEDGER" ]    || fail "--ledger is required; resources must be recorded as they are created"

case "$RUN_NONCE" in
  *[!0-9a-f]*|"") fail "--run-nonce must be lower-case hex (see #3968 lib/ownership.py new_nonce())" ;;
esac
case "$RUN_ID" in
  *[!a-zA-Z0-9-]*) fail "--run-id must be alphanumeric with dashes only (it becomes a k8s object name)" ;;
esac
case "$PORT" in
  *[!0-9]*|"") fail "--port must be numeric" ;;
esac
if [ "$PORT" != "80" ]; then
  fail "--port $PORT would require opening the SHARED VPC Link security group, which is an
     ordinary-infrastructure change #5836 forbids. The link permits tcp/80 only."
fi

if [ "$RECOVER" = 0 ]; then
  [ -n "$ALB_SGS" ] || fail "--alb-security-groups is required. A fresh controller-created group would be UNREACHABLE from the reused VPC Link (its egress is restricted to specific groups), and every request would time out."
  for sg in ${ALB_SGS//,/ }; do
    case "$sg" in
      sg-*) ;;
      *) fail "--alb-security-groups entry '$sg' is not a security group id" ;;
    esac
  done
  [ -f "$TEMPLATE" ] || fail "template not found: $TEMPLATE"
fi

if [ "$RECOVER" = 1 ] && [ "$CHECK_ONLY" = 1 ]; then
  fail "--recover and --check-only are mutually exclusive: recovery records a live object
     in the ledger, which is a mutation."
fi

[ -f "$OWNERSHIP_LIB" ] || fail "#3968 ownership library not found at $OWNERSHIP_LIB.
     Set W2_OWNERSHIP_LIB to its path. The ALB must be recorded in the ledger at
     creation time, or teardown cannot prove it is ours."

NAME="bedrockgw-w2fx-${RUN_ID}"
# Kubernetes object names are limited to 63 characters; a silent truncation by the
# server would break the name<->ledger correspondence teardown depends on.
[ "${#NAME}" -le 63 ] || fail "derived Ingress name '$NAME' exceeds 63 characters; use a shorter --run-id"

# The artifact directory is shared with fixture-lifecycle.sh, so one run's intent
# records, receipts and reviewed inputs live together under one 700 directory.
artifact_dir() {
  local dir="${ARTIFACT_DIR:-$ROOT_DIR/.fixture-run-$RUN_NONCE}"
  [ -d "$dir" ] || mkdir -p "$dir"
  chmod 700 "$dir"
  local mode; mode="$(stat -c '%a' "$dir")"
  [ "$mode" = "700" ] || fail "artifact directory $dir has mode $mode, expected 700"
  printf '%s\n' "$dir"
}

# ---------------------------------------------------------------------------
# IDENTITY, ASSERTED BEFORE ANY MUTATION
# ---------------------------------------------------------------------------
assert_live_account() {
  local live
  live="$(aws_ sts get-caller-identity --query Account --output text 2>/dev/null || printf '')"
  [ -n "$live" ] || fail "could not resolve the caller identity. Check --profile/credentials.
     Refusing to create anything while the acting account is UNKNOWN."
  [ "$live" = "$ACCOUNT" ] || fail "the active credential resolves to account $live, but --account-id
     is $ACCOUNT. REFUSING to create the fixture ALB. This runs BEFORE the create
     precisely because the previous revision discovered the mismatch afterwards, with
     the Ingress already live."
  ok "credential resolves to the expected account ($ACCOUNT)"
}

# ---------------------------------------------------------------------------
# AWS CLI --profile DOES NOT BIND kubectl, AND A CONTEXT NAME IS NOT AN IDENTITY.
#
# assert_live_account pins the AWS CLI. kubectl's target comes from KUBECONFIG and
# can be a different cluster -- or a different account's cluster -- while every
# aws_ call in the same run is correctly bound. A context NAME cannot settle it
# either: it is an arbitrary local alias and can read
# `arn:aws:eks:us-east-1:<right account>:cluster/adp-dev-eks` while pointing at any
# server. What is authoritative is the API server ENDPOINT kubectl will connect to,
# compared against the endpoint AWS reports for the named cluster, read through
# this run's own bound profile. Same check, and the same reasoning, as
# fixture-lifecycle.sh's assert_kube_context.
# ---------------------------------------------------------------------------
assert_kube_context() {
  local ctx server
  ctx="$(kubectl config current-context 2>/dev/null || printf '')"
  [ -n "$ctx" ] || fail "no current kubectl context. Refusing to mutate an unknown cluster."

  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || printf '')"
  [ -n "$server" ] || fail "could not read the API server endpoint for context '$ctx'. Without it
     the cluster kubectl would mutate cannot be identified, and a context NAME is only
     a local label. Refusing to mutate an unidentifiable cluster."

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

  local aws_endpoint
  aws_endpoint="$(aws_ eks describe-cluster --name "$cluster_name" \
    --query cluster.endpoint --output text 2>/dev/null || printf '')"
  [ -n "$aws_endpoint" ] && [ "$aws_endpoint" != "None" ] \
    || fail "EKS cluster '$cluster_name' does not exist in account $ACCOUNT / $REGION (as
     resolved through --profile), so kubectl's target cannot be confirmed to be the
     cluster this run is authorised for. Refusing to mutate."

  local want_host have_host
  want_host="$(printf '%s' "$aws_endpoint" | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')"
  have_host="$(printf '%s' "$server" | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')"
  [ "$have_host" = "$want_host" ] || fail "kubectl would connect to a DIFFERENT cluster than the one verified.
       kubeconfig server        : $have_host
       AWS says $cluster_name is : $want_host
     The context name ('$ctx') is only a local label and can name any cluster, so this
     endpoint comparison is what actually pins the target. Refusing to mutate."

  ok "cluster verified against AWS: $cluster_name ($want_host) in $ACCOUNT/$REGION"
}

# ---------------------------------------------------------------------------
# THE SERVICE MUST BE THIS RUN'S FIXTURE, NOT MERELY A NAME THAT EXISTS.
#
# `kubectl get service <name>` proves the name is taken. The previous revision
# stopped there, so an ordinary Service, or another run's fixture, satisfied it --
# and the Ingress would then have routed the trusted edge's traffic into whatever
# that was. The run labels are #3968's renderer's own (render_fixture.py sets
# adp.io/w2-fixture=<run id> and adp.io/w2-nonce=<nonce> on the Deployment AND the
# Service), so comparing them against this run's values is a real ownership test.
#
# Type and port are checked for a different reason: they are what makes the
# Ingress functional. The rendered backend references port __PORT__ by NUMBER, so
# a Service that does not expose it reconciles to an ALB with an empty target
# group -- a fixture that looks created and answers nothing.
# ---------------------------------------------------------------------------
assert_fixture_service_is_ours() {
  local svc_json
  svc_json="$(kubectl get service "$SERVICE" -n "$NAMESPACE" -o json 2>/dev/null || printf '')"
  [ -n "$svc_json" ] || fail "Service $SERVICE not found in namespace $NAMESPACE.
     Run #3968's 10-create-fixture.sh FIRST: it creates the fixture Deployment and
     Service that this ALB targets. Order is: fixture pods -> this ALB -> terraform
     apply of the edge -> worker bootstrap."

  # THE PROGRAM ARRIVES ON FD 3, NOT ON STDIN. `python3 - <<'PY'` puts the PROGRAM
  # on stdin, so json.load(sys.stdin) would read the already-consumed script and see
  # an empty string -- the exact failure fixture-lifecycle.sh hit on its Secret
  # response. stdin has to carry the DOCUMENT, so the program comes in on its own
  # descriptor.
  printf '%s' "$svc_json" | python3 /dev/fd/3 \
      "$RUN_ID" "$RUN_NONCE" "$NAMESPACE" "$PORT" "$SERVICE" 3<<'PY'
import json, sys
run_id, nonce, namespace, port, name = sys.argv[1:6]
try:
    doc = json.load(sys.stdin)
except Exception as exc:                       # noqa: BLE001
    sys.exit(f"could not parse the Service document for {name}: {exc}")

md = doc.get("metadata") or {}
labels = md.get("labels") or {}
spec = doc.get("spec") or {}

# Namespace from the SERVER, not from the flag: -n could be satisfied while the
# returned object names another namespace only if kubectl were lying, but reading
# it back costs nothing and makes the recorded ledger row self-consistent.
if md.get("namespace") != namespace:
    sys.exit(f"the Service the server returned is in namespace {md.get('namespace')!r}, "
             f"not {namespace!r}. Refusing.")

# THE OWNERSHIP TEST. Both labels, both exact: the run id alone would accept an
# object from an earlier attempt of the same run id under a different nonce, and
# the nonce is what every other gate in this component binds to.
for key, want in (("adp.io/w2-fixture", run_id), ("adp.io/w2-nonce", nonce)):
    got = labels.get(key)
    if got is None:
        sys.exit(
            f"Service {name} carries no {key} label, so it is NOT a fixture Service "
            f"created by #3968's renderer for this run. REFUSING to point the fixture "
            f"ALB at it: the edge injects verified-caller and provenance headers, and "
            f"an ordinary Service behind this Ingress would receive them. Existence of "
            f"the name is not ownership."
        )
    if got != want:
        sys.exit(
            f"Service {name}'s {key} is {got!r}, but this run is {want!r}. The Service "
            f"belongs to a DIFFERENT run. REFUSING: creating an Ingress for it would "
            f"put this run's ALB, tag and ledger row in front of another run's pods."
        )

if spec.get("type") != "ClusterIP":
    sys.exit(
        f"Service {name} is type {spec.get('type')!r}, expected ClusterIP. #3968's "
        f"renderer emits ClusterIP; anything else is already externally reachable, "
        f"which defeats the point of fronting it with an INTERNAL ALB."
    )

ports = [p.get("port") for p in (spec.get("ports") or [])]
if int(port) not in [p for p in ports if isinstance(p, int)]:
    sys.exit(
        f"Service {name} exposes ports {ports}, not {port}. The rendered Ingress "
        f"references port {port} by number, so the ALB would reconcile with an EMPTY "
        f"target group and every request would fail as if the fixture were broken."
    )

print(f"  [ ok ] Service {name} is this run's fixture "
      f"(adp.io/w2-fixture={run_id}, adp.io/w2-nonce={nonce}, ClusterIP:{port})")
PY
}

# ---------------------------------------------------------------------------
# RECOVERY: record an Ingress this run created but did not manage to record.
#
# An intent record proves an attempt was MADE. It cannot prove which object now
# holds the name -- between the attempt and recovery the name may have been taken
# by another run, or the create may have hit AlreadyExists against an object this
# run never made. Only a server-assigned uid captured at creation distinguishes
# those, so recovery REQUIRES the uid receipt and REFUSES when there is none.
# Recording is what authorises deletion, so an unverifiable partial creation is
# escalated to a human, never adopted.
#
# This mirrors fixture-lifecycle.sh's recover-secret deliberately: the failure it
# prevents is identical (a same-named replacement being recorded, and therefore
# deleted, by this run's teardown).
# ---------------------------------------------------------------------------
cmd_recover() {
  step "recover a partially-created fixture Ingress"
  assert_live_account
  assert_kube_context

  local dir; dir="$(artifact_dir)"
  local intent="$dir/alb-intent.json"
  local receipt="$dir/alb-receipt.json"

  [ -f "$intent" ] || fail "no creation intent at $intent, so there is no evidence this run
     created Ingress/$NAME. REFUSING to record it: recording implies authority to
     delete, and a same-named Ingress may belong to another run -- deleting it would
     take down that run's ALB. Investigate by hand:
       kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'"

  python3 - "$intent" "$RUN_NONCE" "$ACCOUNT" "$NAME" "$NAMESPACE" <<'PY' \
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

  [ -f "$receipt" ] || fail "no uid receipt at $receipt. The Ingress's server-assigned uid was
     never captured, so there is NO evidence identifying which object this run
     created -- only that it tried. REFUSING to record any live Ingress as ours.
     Escalate and inspect by hand:
       kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'
     Compare that uid against the audit trail before deleting anything. Deleting
     this Ingress deletes its ALB, so a wrong guess here takes down another run."

  local expect_uid
  expect_uid="$(python3 - "$receipt" "$RUN_NONCE" "$ACCOUNT" "$NAME" "$NAMESPACE" <<'PY'
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

  local live_uid
  live_uid="$(kubectl get ingress "$NAME" -n "$NAMESPACE" \
    -o jsonpath='{.metadata.uid}' 2>/dev/null || printf '')"
  [ -n "$live_uid" ] || fail "Ingress/$NAME does not exist in $NAMESPACE, so there is nothing to
     recover. If the create genuinely failed, no object leaked and you can re-run this
     script with the same --run-id."

  # A uid changes only on delete-and-recreate, so a differing live uid means the
  # object under this name is NOT the one this run created.
  [ "$live_uid" = "$expect_uid" ] || fail "Ingress/$NAME is NOT the object this run created.
       recorded at creation : $expect_uid
       live now             : $live_uid
     REFUSING to record it: that would authorise teardown to delete another run's
     Ingress, and deleting an Ingress deletes its ALB. Do NOT delete it by name. The
     object this run created no longer exists, so nothing of ours is leaking here;
     escalate the mismatch."
  ok "live uid matches the uid recorded at creation ($live_uid)"

  python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
    --account-id "$ACCOUNT" --kind Ingress --name "$NAME" --namespace "$NAMESPACE" \
    --uid "$live_uid" \
    || fail "could not record Ingress/$NAME (uid $live_uid) in the ledger. The object STILL
     EXISTS and is STILL unrecorded. Do not delete it by name; resolve the ledger
     problem and re-run this command."
  python3 - "$intent" <<'PY'
import json, sys
p = sys.argv[1]
doc = json.load(open(p))
doc["state"] = "recovered"
json.dump(doc, open(p, "w"), indent=2)
PY
  ok "Ingress/$NAME recorded by its actual uid ($live_uid); teardown will remove it"
  note "the ALB facts for terraform were NOT printed by this path. Re-read them with:"
  note "  kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'"
  exit 0
}

# ---------------------------------------------------------------------------
# Order: identity -> ownership -> render -> intent -> create. Every read that can
# refuse happens before the one call that cannot be undone.
# ---------------------------------------------------------------------------
step "verify the account and cluster this run will mutate"
assert_live_account
assert_kube_context

# An `[ ... ] && cmd_recover` one-liner would exit 1 under errexit on the ordinary
# path, so the branch is written as an `if`.
if [ "$RECOVER" = 1 ]; then
  cmd_recover
fi

step "verify the fixture Service belongs to this run"
assert_fixture_service_is_ours

# --- render (plain substitution; no shell eval of template content) --------
step "render the fixture Ingress"
MANIFEST_DIR="$(mktemp -d)"
# 700: the manifest is run-scoped operational detail. It holds no secret, but the
# artifact directory convention for this run is private.
chmod 700 "$MANIFEST_DIR"
MANIFEST="$MANIFEST_DIR/fixture-alb.yaml"

python3 - "$TEMPLATE" "$MANIFEST" \
  "$NAME" "$NAMESPACE" "$SERVICE" "$RUN_ID" "$RUN_NONCE" "$ALB_SGS" "$PORT" <<'PY'
import sys
tmpl, out, name, ns, svc, run_id, nonce, sgs, port = sys.argv[1:10]
text = open(tmpl).read()
for token, value in {
    "__NAME__": name, "__NAMESPACE__": ns, "__SERVICE__": svc,
    "__RUN_ID__": run_id, "__RUN_NONCE__": nonce,
    "__ALB_SG_IDS__": sgs, "__PORT__": port,
}.items():
    text = text.replace(token, value)
# Fail loudly rather than creating an object with a literal __TOKEN__ in it.
leftover = [t for t in ("__NAME__", "__NAMESPACE__", "__SERVICE__", "__RUN_ID__",
                        "__RUN_NONCE__", "__ALB_SG_IDS__", "__PORT__") if t in text]
if leftover:
    sys.exit(f"unsubstituted placeholders remain: {leftover}")
open(out, "w").write(text)
PY
ok "rendered $MANIFEST"

# --- check-only: server-side validation, creates nothing -------------------
if [ "$CHECK_ONLY" = 1 ]; then
  out="$(kubectl create -f "$MANIFEST" --dry-run=server -o name 2>&1)" \
    || fail "server dry-run rejected the fixture Ingress: $out"
  ok "check-only: server accepted the fixture Ingress ($out). Nothing was created."
  note "manifest retained at $MANIFEST for review"
  exit 0
fi

# --- intent BEFORE the create ----------------------------------------------
# Written before the mutation so that a process that dies between the create and
# the ledger record leaves a uid-recoverable trail rather than an orphan ALB.
# --recover consumes it. Deliberately metadata only.
step "record the intent to create, then create"
DIR="$(artifact_dir)"
INTENT="$DIR/alb-intent.json"
RECEIPT="$DIR/alb-receipt.json"
python3 - "$INTENT" "$NAME" "$NAMESPACE" "$RUN_NONCE" "$ACCOUNT" "$RUN_ID" "$SERVICE" <<'PY'
import json, sys
path, name, ns, nonce, account, run_id, svc = sys.argv[1:8]
# run_nonce/account_id/name/namespace are all re-validated by --recover, so a
# stale directory from another run cannot satisfy its existence check.
json.dump({"intent": "create-ingress", "name": name, "namespace": ns,
           "run_id": run_id, "run_nonce": nonce, "account_id": account,
           "service": svc, "state": "pending"},
          open(path, "w"), indent=2)
PY
chmod 600 "$INTENT"
ok "intent recorded at $INTENT"

# --- create, then record the server-assigned uid ---------------------------
set +e
created="$(kubectl create -f "$MANIFEST" -o json 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  case "$created" in
    *AlreadyExists*)
      fail "Ingress $NAME already exists in $NAMESPACE. REFUSING to adopt it: it may
     belong to another run, and recording it would authorise deleting something
     this run did not create. Use a new --run-id.
     If this run created it and only the RECORD is missing, recover it uid-safely:
       $0 --recover --run-id $RUN_ID --run-nonce $RUN_NONCE --account-id $ACCOUNT \\
          --ledger $LEDGER --namespace $NAMESPACE --service $SERVICE
     That path refuses unless the live uid still matches the one captured at
     creation, so it cannot adopt a same-named object from elsewhere." ;;
    *) fail "could not create the fixture Ingress: $created
     THE OBJECT MAY NEVERTHELESS EXIST: a create can succeed server-side and still
     report a failure here (dropped connection, timeout). The intent is recorded at
     $INTENT. Check, and do NOT delete by name:
       kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'
     If it exists, recover it with '$0 --recover ...same bindings...'." ;;
  esac
fi

# The uid receipt is written from the response IMMEDIATELY, before the ledger call,
# so the window in which a live object has no on-disk identity is one write wide.
# An Ingress carries no secret material (ours declares no TLS), so unlike the
# provenance Secret its response can be parsed and its metadata persisted.
UID_VALUE="$(printf '%s' "$created" | python3 /dev/fd/3 "$RECEIPT" \
    "$RUN_NONCE" "$ACCOUNT" "$NAME" "$NAMESPACE" 3<<'PY'
import json, sys
receipt, nonce, account, expect_name, expect_ns = sys.argv[1:6]
doc = json.load(sys.stdin)
md = doc.get("metadata", {})
uid = md.get("uid") or ""
json.dump({"kind": doc.get("kind"), "name": md.get("name"),
           "namespace": md.get("namespace"), "uid": uid,
           "resourceVersion": md.get("resourceVersion"),
           "run_nonce": nonce, "account_id": account,
           "expected_name": expect_name, "expected_namespace": expect_ns},
          open(receipt, "w"), indent=2)
print(uid)
PY
  )" || fail "created Ingress $NAME but could not parse the server's response. The intent is
     at $INTENT. Do NOT delete by name; read the uid and recover:
       kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'"
chmod 600 "$RECEIPT"

[ -n "$UID_VALUE" ] || fail "created Ingress $NAME but the server returned no metadata.uid, so it
     cannot be proven ours at teardown. Do NOT delete by name -- a same-named object
     may be another run's, and deleting an Ingress deletes its ALB. Re-read the live
     uid and recover:
       kubectl get ingress $NAME -n $NAMESPACE -o jsonpath='{.metadata.uid}{\"\\n\"}'
       $0 --recover --run-id $RUN_ID --run-nonce $RUN_NONCE --account-id $ACCOUNT \\
          --ledger $LEDGER --namespace $NAMESPACE --service $SERVICE"

# Recorded through #3968's SUPPORTED interface. An Ingress is a uid-bearing k8s
# object, so its existing k8s bucket and uid-gated delete path already cover it.
# $ACCOUNT is the VERIFIED account, asserted before the create rather than read
# afterwards to fill in this argument.
python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
  --account-id "$ACCOUNT" --kind Ingress --name "$NAME" --namespace "$NAMESPACE" \
  --uid "$UID_VALUE" \
  || fail "created Ingress $NAME (uid $UID_VALUE) but could NOT record it in the ledger.
     The object EXISTS and is currently unrecorded, so teardown would not know about
     it. Its uid is captured at $RECEIPT, so recovery is uid-safe:
       $0 --recover --run-id $RUN_ID --run-nonce $RUN_NONCE --account-id $ACCOUNT \\
          --ledger $LEDGER --namespace $NAMESPACE --service $SERVICE
     Do NOT 'kubectl delete ingress' by name: a same-named object may belong to
     another run, and deleting an Ingress deletes its ALB."
ok "created Ingress/$NAME uid=$UID_VALUE (recorded in $LEDGER)"

# --- wait for the ALB, then emit the facts terraform needs -----------------
# The poll shape is overridable ONLY so the timeout branch is reachable in tests
# (tests/test_fixture_alb_composition.py). Defaults are the real operational ones;
# an untested timeout branch is how "created but never recorded as failed" happens.
WAIT_ATTEMPTS="${W2_ALB_WAIT_ATTEMPTS:-60}"
WAIT_INTERVAL="${W2_ALB_WAIT_INTERVAL:-10}"

note "waiting for the ALB to be provisioned (EKS Auto Mode typically 2-4 minutes)"
ALB_DNS=""
for _ in $(seq 1 "$WAIT_ATTEMPTS"); do
  ALB_DNS="$(kubectl get ingress "$NAME" -n "$NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [ -n "$ALB_DNS" ] && break
  sleep "$WAIT_INTERVAL"
done
if [ -z "$ALB_DNS" ]; then
  fail "the Ingress was created and recorded, but no ALB hostname appeared within
     $((WAIT_ATTEMPTS * WAIT_INTERVAL))s.
     It is IN THE LEDGER, so teardown will remove it. Diagnose with:
       kubectl describe ingress $NAME -n $NAMESPACE"
fi
ok "ALB provisioned: $ALB_DNS"

ALB_ARN="$(aws_ elbv2 describe-load-balancers \
  --query "LoadBalancers[?DNSName=='$ALB_DNS'].LoadBalancerArn | [0]" --output text)"
[ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] \
  || fail "could not resolve the ALB ARN for $ALB_DNS. The Ingress is recorded; teardown will remove it."

# Verified from the live resource, not assumed from the annotation: a tag that did
# not land would make the Terraform gate refuse later, and it is better to learn
# that here than after the operator has moved on.
ALB_TAG="$(aws_ elbv2 describe-tags --resource-arns "$ALB_ARN" \
  --query "TagDescriptions[0].Tags[?Key=='AdpFixtureRun'].Value | [0]" --output text 2>/dev/null || echo "")"
if [ "$ALB_TAG" != "$RUN_NONCE" ]; then
  fail "the ALB's AdpFixtureRun tag is '${ALB_TAG}', expected '$RUN_NONCE'.
     The Terraform gate REFUSES to plan without it, so stop here. The Ingress is
     recorded in the ledger and teardown will remove it."
fi
ok "ownership tag verified on the live ALB (AdpFixtureRun=$RUN_NONCE)"

ALB_SG_LIVE="$(aws_ elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].SecurityGroups' --output json)"
ALB_SCHEME="$(aws_ elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].Scheme' --output text)"
[ "$ALB_SCHEME" = "internal" ] \
  || fail "the ALB resolved as '$ALB_SCHEME', not internal. Stop: a public backend behind a
     trusted-header-injecting edge would let the fixture gateway be addressed directly.
     The Ingress is recorded; teardown will remove it."
ok "scheme verified internal"

cat <<EOF

  Fixture ALB ready. Terraform inputs for this run:

    fixture_alb_arn = "$ALB_ARN"
    expected_vpc_id = "$(aws_ elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --query 'LoadBalancers[0].VpcId' --output text)"

  Live security groups: $ALB_SG_LIVE
  These are NOT a Terraform input. The gate READS the rules on every group attached
  to this ALB and to the VPC Link, and refuses unless a live rule carries BOTH
  directions on the listener port -- link egress TO one of these groups, and ingress
  on one of these groups FROM the link's group. Egress alone is a timeout, not a
  refusal, so a plan that refuses here has saved you a silent one. Do NOT add a rule:
  these groups are shared with ordinary traffic.

  fixture_alb_dns is NOT an input: main.tf reads it from this ALB so the two
  cannot disagree.

  Recorded in the ledger as Ingress/$NAME (uid $UID_VALUE).

  TEARDOWN ORDER -- DESTROY THE EDGE FIRST, THEN THIS INGRESS.
  An earlier revision of this note had it backwards. ../main.tf READS this ALB
  (data.aws_lb.fixture) to derive its DNS name, and Terraform RE-READS data sources
  during destroy, so deleting the Ingress first makes the edge's destroy unplannable
  and the only apparent way out is deleting cloud objects by hand -- the one thing
  the ownership gates exist to prevent. So:

    1. ./scripts/fixture-lifecycle.sh destroy --nonce $RUN_NONCE \\
         --account-id $ACCOUNT --profile <profile>        # the edge
    2. platform/scripts/operator/wave2/90-cleanup-ledger.sh --ledger $LEDGER
                                                          # this Ingress, uid-gated;
                                                          # deleting it removes the ALB

  RUNBOOK.md section 8 is the full version, including 'destroy --recover' for the
  case where the Ingress was already deleted out of order.
EOF

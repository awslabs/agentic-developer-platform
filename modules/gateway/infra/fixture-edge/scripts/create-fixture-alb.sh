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
# SCOPE: this script MUTATES the cluster. It is for ROOT's authorized execution,
# not for the developer run that wrote it. --check-only is read-only (server
# dry-run) and is what the developer can legitimately exercise.
#
# Usage:
#   ./create-fixture-alb.sh --run-id w2-... --run-nonce <hex> --ledger <path> \
#       --namespace adp-gateway --service <fixture-service> \
#       --alb-security-groups sg-aaa,sg-bbb [--port 80] [--check-only]
#
# Discover --alb-security-groups read-only (must be groups the VPC Link may
# already egress to; see ../fixture-alb.yaml.tmpl for why this matters):
#   aws apigatewayv2 get-vpc-links --query 'Items[].SecurityGroupIds'
#   aws ec2 describe-security-groups --group-ids <link-sg> \
#     --query 'SecurityGroups[0].IpPermissionsEgress[].UserIdGroupPairs[].GroupId'
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/../fixture-alb.yaml.tmpl"

RUN_ID=""; RUN_NONCE=""; LEDGER=""; NAMESPACE=""; SERVICE=""
ALB_SGS=""; PORT="80"; CHECK_ONLY=0
# Defaults to the #3968 tooling location; overridable for a non-standard checkout.
OWNERSHIP_LIB="${W2_OWNERSHIP_LIB:-$HERE/../../../../../platform/scripts/operator/wave2/lib/ownership.py}"

fail() { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }
ok()   { printf '  [ ok ] %s\n' "$*"; }
note() { printf '  [note] %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)              RUN_ID="${2:?}"; shift 2 ;;
    --run-nonce)           RUN_NONCE="${2:?}"; shift 2 ;;
    --ledger)              LEDGER="${2:?}"; shift 2 ;;
    --namespace)           NAMESPACE="${2:?}"; shift 2 ;;
    --service)             SERVICE="${2:?}"; shift 2 ;;
    --alb-security-groups) ALB_SGS="${2:?}"; shift 2 ;;
    --port)                PORT="${2:?}"; shift 2 ;;
    --check-only)          CHECK_ONLY=1; shift ;;
    -h|--help)             sed -n '1,45p' "$0"; exit 0 ;;
    *)                     fail "unknown argument: $1" ;;
  esac
done

# --- input validation: refuse before touching the cluster -------------------
[ -n "$RUN_ID" ]    || fail "--run-id is required (the #3968 ledger run id)"
[ -n "$RUN_NONCE" ] || fail "--run-nonce is required; it becomes the AdpFixtureRun tag the Terraform gate verifies"
[ -n "$NAMESPACE" ] || fail "--namespace is required"
[ -n "$SERVICE" ]   || fail "--service is required (the fixture ClusterIP Service created by #3968's renderer)"
[ -n "$ALB_SGS" ]   || fail "--alb-security-groups is required. A fresh controller-created group would be UNREACHABLE from the reused VPC Link (its egress is restricted to specific groups), and every request would time out."
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
for sg in ${ALB_SGS//,/ }; do
  case "$sg" in
    sg-*) ;;
    *) fail "--alb-security-groups entry '$sg' is not a security group id" ;;
  esac
done

[ -f "$TEMPLATE" ]      || fail "template not found: $TEMPLATE"
[ -f "$OWNERSHIP_LIB" ] || fail "#3968 ownership library not found at $OWNERSHIP_LIB.
     Set W2_OWNERSHIP_LIB to its path. The ALB must be recorded in the ledger at
     creation time, or teardown cannot prove it is ours."

NAME="bedrockgw-w2fx-${RUN_ID}"
# Kubernetes object names are limited to 63 characters; a silent truncation by the
# server would break the name<->ledger correspondence teardown depends on.
[ "${#NAME}" -le 63 ] || fail "derived Ingress name '$NAME' exceeds 63 characters; use a shorter --run-id"

# --- the fixture Service must already exist --------------------------------
# The Ingress would otherwise reconcile to an ALB with no healthy target, which
# looks like a broken fixture rather than a missing prerequisite.
if ! kubectl get service "$SERVICE" -n "$NAMESPACE" >/dev/null 2>&1; then
  fail "Service $SERVICE not found in namespace $NAMESPACE.
     Run #3968's 10-create-fixture.sh FIRST: it creates the fixture Deployment and
     Service that this ALB targets. Order is: fixture pods -> this ALB -> terraform
     apply of the edge -> worker bootstrap."
fi
ok "fixture Service $SERVICE exists in $NAMESPACE"

# --- render (plain substitution; no shell eval of template content) --------
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

# --- create, then record the server-assigned uid ---------------------------
set +e
created="$(kubectl create -f "$MANIFEST" -o json 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  case "$created" in
    *AlreadyExists*)
      fail "Ingress $NAME already exists in $NAMESPACE. REFUSING to adopt it: it may
     belong to another run, and recording it would authorise deleting something
     this run did not create. Use a new --run-id." ;;
    *) fail "could not create the fixture Ingress: $created" ;;
  esac
fi

UID_VALUE="$(printf '%s' "$created" | python3 -c '
import json,sys
print(json.load(sys.stdin).get("metadata", {}).get("uid", ""))
')"
[ -n "$UID_VALUE" ] || fail "created Ingress $NAME but the server returned no metadata.uid;
     it cannot be proven ours at teardown. Remove it by hand: kubectl delete ingress $NAME -n $NAMESPACE"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" \
  || fail "created Ingress $NAME (uid $UID_VALUE) but could not resolve the account to record it; remove it by hand"

# Recorded through #3968's SUPPORTED interface. An Ingress is a uid-bearing k8s
# object, so its existing k8s bucket and uid-gated delete path already cover it.
python3 "$OWNERSHIP_LIB" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
  --account-id "$ACCOUNT" --kind Ingress --name "$NAME" --namespace "$NAMESPACE" \
  --uid "$UID_VALUE" \
  || fail "created Ingress $NAME (uid $UID_VALUE) but could NOT record it in the ledger.
     Remove it by hand, or teardown will not know it exists:
       kubectl delete ingress $NAME -n $NAMESPACE"
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

ALB_ARN="$(aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?DNSName=='$ALB_DNS'].LoadBalancerArn | [0]" --output text)"
[ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] \
  || fail "could not resolve the ALB ARN for $ALB_DNS. The Ingress is recorded; teardown will remove it."

# Verified from the live resource, not assumed from the annotation: a tag that did
# not land would make the Terraform gate refuse later, and it is better to learn
# that here than after the operator has moved on.
ALB_TAG="$(aws elbv2 describe-tags --resource-arns "$ALB_ARN" \
  --query "TagDescriptions[0].Tags[?Key=='AdpFixtureRun'].Value | [0]" --output text 2>/dev/null || echo "")"
if [ "$ALB_TAG" != "$RUN_NONCE" ]; then
  fail "the ALB's AdpFixtureRun tag is '${ALB_TAG}', expected '$RUN_NONCE'.
     The Terraform gate REFUSES to plan without it, so stop here. The Ingress is
     recorded in the ledger and teardown will remove it."
fi
ok "ownership tag verified on the live ALB (AdpFixtureRun=$RUN_NONCE)"

ALB_SG_LIVE="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].SecurityGroups' --output json)"
ALB_SCHEME="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].Scheme' --output text)"
[ "$ALB_SCHEME" = "internal" ] \
  || fail "the ALB resolved as '$ALB_SCHEME', not internal. Stop: a public backend behind a
     trusted-header-injecting edge would let the fixture gateway be addressed directly.
     The Ingress is recorded; teardown will remove it."
ok "scheme verified internal"

cat <<EOF

  Fixture ALB ready. Terraform inputs for this run:

    fixture_alb_arn = "$ALB_ARN"
    expected_vpc_id = "$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --query 'LoadBalancers[0].VpcId' --output text)"

  Live security groups: $ALB_SG_LIVE
  (at least one must appear in vpc_link_egress_target_security_group_ids, or the
   Terraform gate refuses -- that is the reachability check, not a formality)

  fixture_alb_dns is NOT an input: main.tf reads it from this ALB so the two
  cannot disagree.

  Recorded in the ledger as Ingress/$NAME (uid $UID_VALUE). Teardown:
  #3968's 90-cleanup-ledger.sh deletes it uid-gated; deleting the Ingress is what
  removes the ALB. Delete the Ingress BEFORE destroying the edge's Terraform, per
  ../RUNBOOK.md teardown ordering.
EOF

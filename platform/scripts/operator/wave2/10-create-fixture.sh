#!/usr/bin/env bash
# Wave 2 fixture step 1 — create the isolated, run-bound fixture.
#
# Issue #3968 / epic #3959.
#
# WHAT CHANGED AND WHY
# --------------------
# Root rejected the previous revision of this script on four counts. Each is
# addressed by a mechanism rather than by a comment:
#
# 1. COMPOSITION. The fixture gateway was hand-written: two configMapRefs and two
#    env vars. That silently dropped all NINE secret-backed env references the
#    real gateway carries, so the fixture could not verify a session, validate
#    edge provenance or sign a control envelope -- every identity check would
#    have failed for reasons unrelated to the software under review. It also
#    inherited AGENT_AUTHORITY_ENABLED=false and ADP_RUN_TASKS_ENABLED=false from
#    the shared ConfigMap, and never set ADP_RUN_TASK_QUEUE_URL (Terraform-only),
#    so the task routes would have 503'd.
#    Now: lib/render_fixture.py DEEP-COPIES the live pod spec and overrides a
#    small explicit set of keys. Hand-listing is how the nine were lost; a copy
#    cannot lose them, and the copy is ASSERTED to contain them before anything
#    is created.
#
# 2. ISOLATION. The old policy allowed egress on 53 and 443 only -- blocking
#    8770 (the control flow under test) and 5432 (so the gateway could not even
#    start). A policy that blocks the behaviour under test produces a meaningless
#    check, not a failed one. And the fixture's unique label did not match the
#    ordinary worker's ingress allowlist, so the dial could never connect.
#    Now: fixture-scoped policies carry the real flows, and 15-verify-isolation.sh
#    PROBES them -- an applied NetworkPolicy is not isolation proof.
#
# 3. OWNERSHIP. `kubectl apply` ADOPTS a pre-existing object of the same name and
#    `sqs create-queue` returns an EXISTING queue on a name match; both were then
#    recorded `run_bound: true` BEFORE creation. A collision would have authorised
#    cleanup to delete somebody else's resource.
#    Now: `kubectl create` (exclusive, refuses AlreadyExists), the server-assigned
#    metadata.uid is captured and recorded AFTER creation, and the queue is probed
#    first, tagged with this run's nonce, and the tag read back.
#
# 4. FAIL CLOSED. The old script continued past an unusable protected bootstrap
#    with "continuing: the fixture is still valid without it", creating a worker
#    Job that could not possibly authenticate.
#    Now: missing control-critical prerequisites stop the run BEFORE anything is
#    created, and the protected worker is composed against every condition the
#    gateway's own verifier decides (lib/render_fixture.py) rather than against
#    what seems reasonable.
#
# THE PROTECTED WORKER
# --------------------
# This script used to refuse --worker-job outright, and that refusal was correct
# when written: lib/run_identity.py requires an https control endpoint, and every
# such endpoint resolved through API Gateway to the ORDINARY gateway's pods by
# label -- so a fixture worker would have bootstrapped against live traffic.
# #5836 built a fixture-only edge whose `worker_control_endpoint` terminates at
# the FIXTURE gateway, which removes the constraint. So the worker is creatable
# now, with the endpoint as a REQUIRED input and no default: a defaulted endpoint
# would silently reintroduce exactly the defect the refusal prevented, and the
# supplied one is compared against the ordinary API id from SSM before use.
#
# Identity is split, because the protected service account can neither `get` nor
# `list` pods and that boundary is preserved rather than widened:
#   * the CONTAINER gets its own metadata.uid through a downwardAPI volume -- the
#     one identity a process cannot rewrite for itself;
#   * the OPERATOR (this script) reads the pod from the API server, binds it to
#     the Job's server-assigned uid via ownerReferences, and writes the
#     expected-identity document the experiment is measured against.
# No RBAC is granted and no in-worker kubectl is introduced.
#
# DP-INV-1: the control flag is ON only on this disposable fixture. This script
# never edits the ordinary gateway, never mutates a ConfigMap the ordinary
# gateway consumes, and never flips an SSM flag.
#
# Ordering is load-bearing: NetworkPolicies are created BEFORE anything can
# listen. There must never be a window where the fixture is reachable but
# unprotected.
#
# THE STAGED LIFECYCLE (--stage)
# ------------------------------
# The worker needs a control endpoint before anything is created. #5836's edge
# cannot publish one until the fixture Service exists, because its ALB is put in
# FRONT of that Service. So the only workable order is gateway -> edge -> worker,
# and this script was all-or-nothing: the first invocation created the gateway and
# stopped, and the second -- now able to supply the endpoint -- hit the absence
# check and refused every object the first had just created. The sequence the
# runbook described was not executable.
#
# `--stage gateway` creates the queue, policies, Deployment and Service and needs
# NO endpoint, then writes a handoff document carrying this run's nonce, uids,
# queue url and the exact next commands. `--stage worker` requires those objects to
# be present AND to be the instances THIS RUN'S LEDGER RECORDED -- by uid, so a
# same-named replacement is refused rather than adopted. lib/stage_gate.py owns
# that decision; the absence check was moved, not relaxed.
#
# Usage:
#   ./10-create-fixture.sh --run-id w2-YYYYMMDD-HHMMSS --ledger <ledger.json> \
#       [--stage gateway|worker|all] [--evidence-dir DIR] [--check-only] \
#       [--resume-nonce HEX] \
#       [--gateway-image ECR_DIGEST_PIN --worker-image ECR_DIGEST_PIN] \
#       [--worker-job --edge-receipt FILE --edge-alb-arn ARN [--worker-ready-timeout S]]
#
#   --stage gateway  queue + policies + gateway only. No endpoint needed, so it can
#                    run BEFORE #5836's edge exists. Leaves the fixture RUNNING
#                    (deliberately: the next stage needs it) and writes
#                    stage-handoff.json with the next commands.
#   --stage worker   the protected worker only, against the now-existing edge. Reads
#                    the nonce and queue url from the shared ledger rather than
#                    asking for them back, and refuses unless the gateway objects
#                    are the uids this run created.
#   --stage all      both, in one invocation (the default, and what every existing
#                    caller means). Only possible when an endpoint already exists.
#   --gateway-image / --worker-image
#                    Optional paired digest pins from reviewed builds in this account.
#                    Verify build source revision, successful control CI, and ECR
#                    digest provenance before use. Supply the same pair at both stages.
#                    Approval is literal env only in the disposable gateway; shared
#                    ConfigMaps stay unchanged. These inputs are not acceptance proof.
#   --prepare-worker-only
#                    With --stage worker, admit the verified edge and render the
#                    protected worker without creating its Job. Use this to verify
#                    edge connectivity and queue the authenticated task first; an
#                    empty-queue worker exits and cannot be reused for experiments.
#   --check-only     render + server-side dry-run everything, create nothing.
#   --resume-nonce   cross-check against the ledger's nonce (it is READ from the
#                    ledger; a disagreeing value refuses both rather than winning).
#   --worker-job     also create the PROTECTED worker Job. Requires --edge-receipt;
#                    without it, the fixture is gateway-only and W2-03/04/05 report
#                    not_run.
#   --edge-receipt   #5836's run-bound outputs document -- ALL outputs, because the
#                    bindings are in `ownership` and the endpoint is a separate
#                    top-level output:
#                      cd modules/gateway/infra/fixture-edge \
#                        && terraform output -json > edge-outputs.json
#   --edge-alb-arn   the fixture ALB that receipt's addresses were read from. Required
#                    with --edge-receipt: the fixture gateway policy admits callers by
#                    namespaceSelector, and an ALB with `target-type: ip` connects from
#                    its own network interfaces, which belong to no pod and no
#                    namespace -- so the worker stage narrows the policy to admit this
#                    run's ALB addresses, and this ARN is what says they are this run's.
#
# Credentials: vault, AWS_PROFILE or ambient env (lib/session.sh); all three are
# subject to the same target-account assertion. Nothing is echoed or written.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/session.sh
. "$HERE/lib/session.sh"

readonly GW_NS="${W2_GW_NS:-adp-gateway}"
readonly AGENT_NS="${W2_AGENT_NS:-adp-agents}"
readonly GATEWAY_REPO_NAME="adp-gateway"

RUN_ID=""
LEDGER=""
EVIDENCE_DIR=""
CHECK_ONLY=0
PREPARE_WORKER_ONLY=0
RESUME_NONCE=""
WANT_WORKER_JOB=0
WORKER_ENDPOINT=""
EDGE_RECEIPT=""
EDGE_ALB_ARN=""
REVIEWED_GATEWAY_IMAGE=""
REVIEWED_WORKER_IMAGE=""
FIXTURE_APPROVAL_ARGS=()
WORKER_READY_TIMEOUT=300
STAGE="all"

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)        RUN_ID="${2:?}"; shift 2 ;;
    --ledger)        LEDGER="${2:?}"; shift 2 ;;
    --evidence-dir)  EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --resume-nonce)  RESUME_NONCE="${2:?}"; shift 2 ;;
    # gateway -> edge -> worker. See THE STAGED LIFECYCLE above.
    --stage)         STAGE="${2:?}"; shift 2 ;;
    --check-only)    CHECK_ONLY=1; shift ;;
    --prepare-worker-only) PREPARE_WORKER_ONLY=1; shift ;;
    --worker-job)    WANT_WORKER_JOB=1; shift ;;
    # #5836's `worker_control_endpoint` output. Required WITH --worker-job and
    # never defaulted: see the refusal below.
    --worker-control-endpoint) WORKER_ENDPOINT="${2:?}"; shift 2 ;;
    # #5836's `terraform output -json` (ALL outputs), bound to
    # run_nonce/account/region/environment via its `ownership` output.
    # The endpoint is READ FROM this; --worker-control-endpoint becomes a cross-check.
    --edge-receipt)            EDGE_RECEIPT="${2:?}"; shift 2 ;;
    # The fixture ALB the receipt's addresses must have been read from. Required WITH
    # --edge-receipt and never derived from the receipt itself: the receipt is the thing
    # being checked, so taking the expectation from it would make the check compare the
    # document against itself. A matching nonce alone would admit addresses read from
    # ANY load balancer in the account.
    --edge-alb-arn)            EDGE_ALB_ARN="${2:?}"; shift 2 ;;
    --gateway-image) REVIEWED_GATEWAY_IMAGE="${2:?}"; shift 2 ;;
    --worker-image) REVIEWED_WORKER_IMAGE="${2:?}"; shift 2 ;;
    --worker-ready-timeout)    WORKER_READY_TIMEOUT="${2:?}"; shift 2 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[ -n "$RUN_ID" ] || w2_fail "--run-id is required and must be unique per run (e.g. w2-20260924-0130)"
[ -n "$LEDGER" ] || w2_fail "--ledger is required; resources are recorded as they are created"
case "$STAGE" in
  gateway|worker|all) : ;;
  *) w2_fail "--stage '$STAGE' is not a stage. Valid: gateway, worker, all.
     Refusing rather than defaulting: 'all' creates a protected worker, so a typo that
     fell through to the default would create authority nobody asked for." ;;
esac
if [ "$PREPARE_WORKER_ONLY" = 1 ] && [ "$STAGE" != worker ]; then
  w2_fail "--prepare-worker-only requires --stage worker"
fi
# The worker stage exists ONLY to create the worker. Without --worker-job it would
# create nothing at all and exit 0, which reads as a completed stage.
if [ "$STAGE" = worker ] && [ "$WANT_WORKER_JOB" != 1 ]; then
  w2_fail "--stage worker without --worker-job would create nothing and exit 0, which
     reads as a stage that ran. Pass --worker-job (with --edge-receipt), or use
     --stage gateway if the gateway is what you meant to create."
fi
# The reverse contradiction, refused rather than silently resolved either way. If
# --worker-job won, the gateway stage would need the endpoint it exists to run
# without; if --stage won, the operator asked for protected authority and got a
# success exit without it.
if [ "$STAGE" = gateway ] && [ "$WANT_WORKER_JOB" = 1 ]; then
  w2_fail "--stage gateway and --worker-job contradict each other: the gateway stage runs
     BEFORE #5836's edge exists, which is why it needs no control endpoint, and a worker
     cannot be created without one. Run --stage gateway now, then --stage worker once the
     edge publishes its ownership receipt."
fi
# The receipt and the ALB ARN are ALL-OR-NOTHING, refused rather than resolved in
# either direction. A receipt without the ARN is a document whose only established
# property is that SOME run's edge produced it, and an ARN without the receipt has no
# addresses to check against it. The renderer refuses the same combination across its
# five --edge-* flags; refusing here as well means the operator is told at argument
# time, before the queue and the gateway exist, rather than after.
if [ -n "$EDGE_RECEIPT" ] && [ -z "$EDGE_ALB_ARN" ]; then
  w2_fail "--edge-receipt was given without --edge-alb-arn. The ARN is the only thing that
     says WHICH load balancer the receipt's addresses were read from: with a matching
     nonce alone, a receipt naming any ALB in the account would satisfy the binding, and
     the fixture gateway would then admit addresses belonging to something else. Pass the
     ARN this run's ledger records for #5836's edge. A missing expectation must not waive
     the check it exists for."
fi
if [ -n "$EDGE_ALB_ARN" ] && [ -z "$EDGE_RECEIPT" ]; then
  w2_fail "--edge-alb-arn was given without --edge-receipt. There is nothing to check it
     against: the addresses come from the receipt, not from this flag, precisely so they
     cannot be typed in by hand."
fi
case "$RUN_ID" in
  w2-*) : ;;
  *) w2_fail "--run-id must start with 'w2-' so fixture resources are unmistakably this evaluation's" ;;
esac
case "$RUN_ID" in
  *[!a-zA-Z0-9-]*) w2_fail "--run-id must be alphanumeric with dashes only (it becomes a k8s object name)" ;;
esac
EVIDENCE_DIR="${EVIDENCE_DIR:-$PWD/w2-evidence-$RUN_ID}"
mkdir -p "$EVIDENCE_DIR"

readonly GW_NAME="w2-fixture-gateway-${RUN_ID#w2-}"
readonly POLICY_NAME="w2-fixture-policy-${RUN_ID#w2-}"
# The fixture has TWO NetworkPolicies and render_fixture.py derives the second one's
# name from the first. Named here as its own variable so the stage gate can be told
# about it: while it was only a derived string inside the renderer, the gate had no way
# to be handed it, and the worker stage proceeded without ever checking the policy that
# confines the protected worker (5810412904).
readonly WORKER_POLICY_NAME="${POLICY_NAME}-worker"
readonly JOB_NAME="w2-fixture-worker-${RUN_ID#w2-}"
readonly QUEUE_NAME="adp-dev-w2-fixture-${RUN_ID#w2-}.fifo"

# Explicitly protected: unknown ownership per the assignment. Belt-and-braces --
# the real protection is that ownership is proven per-resource, not that these
# two names are blocklisted. A blocklist only ever covers what someone
# remembered; the nonce/uid checks cover everything.
readonly FORBIDDEN_DEPLOY="authority-probe-gateway-20260920"
readonly FORBIDDEN_QUEUE="adp-dev-authority-probe-20260920.fifo"
for n in "$GW_NAME" "$JOB_NAME" "$POLICY_NAME" "$WORKER_POLICY_NAME"; do
  [ "$n" = "$FORBIDDEN_DEPLOY" ] && w2_fail "refusing to touch the protected probe deployment"
done
[ "$QUEUE_NAME" = "$FORBIDDEN_QUEUE" ] && w2_fail "refusing to touch the protected probe queue"

OWNERSHIP="$HERE/lib/ownership.py"
RENDER="$HERE/lib/render_fixture.py"
OBSERVE="$HERE/lib/worker_observation.py"
EDGE_LIB="$HERE/lib/edge_receipt.py"
STAGE_LIB="$HERE/lib/stage_gate.py"
POLICY_LIB="$HERE/lib/alb_policy_observation.py"
[ -f "$OWNERSHIP" ] || w2_fail "missing $OWNERSHIP"
[ -f "$RENDER" ]    || w2_fail "missing $RENDER"
[ -f "$OBSERVE" ]   || w2_fail "missing $OBSERVE"
[ -f "$EDGE_LIB" ]  || w2_fail "missing $EDGE_LIB"
[ -f "$STAGE_LIB" ] || w2_fail "missing $STAGE_LIB"
[ -f "$POLICY_LIB" ] || w2_fail "missing $POLICY_LIB"

# ---------------------------------------------------------------------------
# session + account assertion (all three credential modes)
# ---------------------------------------------------------------------------
printf '\n== session ==\n'
w2_report_mode
w2_require_account   # sets W2_ACCOUNT / W2_ARN; exits here, not in a subshell
ACCOUNT="$W2_ACCOUNT"
if [ -n "$REVIEWED_GATEWAY_IMAGE$REVIEWED_WORKER_IMAGE" ]; then
  python3 - "$ACCOUNT" "$W2_REGION" "$REVIEWED_GATEWAY_IMAGE" "$REVIEWED_WORKER_IMAGE" <<'PY_IMAGES'
import re, sys
account, region, gateway, worker = sys.argv[1:]
for image, repository in [(gateway, "adp-gateway"), (worker, "adp-agent-runtime")]:
    prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/{repository}@"
    if not image.startswith(prefix) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image[len(prefix):]):
        sys.exit("reviewed gateway and worker images must both be exact digest pins in the target account")
PY_IMAGES
  FIXTURE_APPROVAL_ARGS=(--fixture-worker-digest "${REVIEWED_WORKER_IMAGE##*@}")
fi

w2_ok "account $ACCOUNT"
w2_note "identity: $W2_ARN"

# The nonce is this run's ownership evidence for resources that have no uid.
#
# A LATER STAGE READS IT FROM THE SHARED LEDGER. It must not be retyped: its whole
# purpose is to bind the stages (and #5836's state key, and the queue tag) to one
# run, so a transcription error produces a *plausible* nonce and the stages then
# tag their resources so the other's teardown never finds them. --resume-nonce is
# accepted as a CROSS-CHECK that must agree, on the same reasoning as
# --worker-control-endpoint against the edge receipt.
if [ -f "$LEDGER" ]; then
  set +e
  NONCE="$(python3 "$STAGE_LIB" nonce --ledger "$LEDGER" \
    ${RESUME_NONCE:+--supplied "$RESUME_NONCE"})"; nonce_rc=$?
  set -e
  [ "$nonce_rc" -eq 0 ] || w2_fail "could not recover this run's nonce from the shared ledger
     $LEDGER (see the refusal above). A later stage cannot be bound to the earlier one
     without it."
  w2_ok "nonce recovered from the shared ledger (stage continuation)"
elif [ "$STAGE" = worker ]; then
  w2_fail "--stage worker needs the shared ledger the gateway stage wrote, and there is
     no file at $LEDGER. There is nothing for the worker to join: run
     --stage gateway first, with this same --ledger path."
elif [ -n "$RESUME_NONCE" ]; then
  # There is nothing to resume, so this is a FRESH run with a pinned nonce -- which is
  # legitimate (reproducible dry runs need one) but is not what the flag's name says.
  # Said out loud rather than refused, because the dangerous version of this mistake --
  # meaning to resume and mistyping --ledger -- is caught where it does damage: the
  # stage gate below refuses objects that exist but this ledger does not record.
  NONCE="$RESUME_NONCE"
  w2_note "there is no ledger at $LEDGER, so nothing is being resumed: the supplied nonce"
  w2_note "  is being used as THIS run's nonce. If you meant to continue an earlier run,"
  w2_note "  stop and check --ledger -- its objects would be refused as unrecorded."
else
  NONCE="$(python3 "$OWNERSHIP" nonce)"
fi
python3 "$OWNERSHIP" init --ledger "$LEDGER" --run-id "$RUN_ID" \
  --account-id "$ACCOUNT" --region "$W2_REGION" --nonce "$NONCE" \
  || w2_fail "could not initialise the ledger (see the refusal above)"
w2_ok "ledger ready ($LEDGER)"

KUBECONFIG_PATH="$(w2_kubeconfig)"
export KUBECONFIG="$KUBECONFIG_PATH"

# ---------------------------------------------------------------------------
# stage gate — BEFORE anything is created
# ---------------------------------------------------------------------------
# Every object this stage would create must be ABSENT, and every object it depends
# on must be PRESENT *and be the uid this run's ledger recorded*. The second half is
# what makes a staged lifecycle possible without turning it into adoption: a name
# match admits anything wearing the name, and the ledger would then license deleting
# it. lib/stage_gate.py decides; this script only observes.
printf '\n== stage gate (%s) ==\n' "$STAGE"

# One observation per object, written to a file the gate reads. An object with NO
# observation is a refusal there, not a pass -- "did not look" must never read as
# "absent".
STAGE_OBS="$EVIDENCE_DIR/stage-observations.json"
: > "$STAGE_OBS.raw"
w2_observe() { # kind name namespace
  local out rc uid
  set +e
  out="$(w2_kubectl get "$1" "$2" -n "$3" -o jsonpath='{.metadata.uid}' 2>&1)"; rc=$?
  set -e
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    uid="$out"
    printf '%s\t%s\t%s\t%s\t%s\t\n' "$1" "$2" "$3" present "$uid" >> "$STAGE_OBS.raw"
    return 0
  fi
  case "$out" in
    *NotFound*|*"not found"*)
      printf '%s\t%s\t%s\t%s\t\t\n' "$1" "$2" "$3" absent >> "$STAGE_OBS.raw" ;;
    *)
      # Includes rc=0 with an EMPTY uid: the object answered but identified nothing,
      # which cannot be treated as either present-and-ours or absent.
      printf '%s\t%s\t%s\t%s\t\t%s\n' "$1" "$2" "$3" unreadable \
        "$(printf '%s' "$out" | tr '\t\n' '  ' | cut -c1-200)" >> "$STAGE_OBS.raw" ;;
  esac
}
w2_observe Deployment    "$GW_NAME"            "$GW_NS"
w2_observe Service       "$GW_NAME"            "$GW_NS"
w2_observe NetworkPolicy "$POLICY_NAME"        "$GW_NS"
# BOTH policies. The worker-side one lives in the agent namespace and is what confines
# the pod holding protected authority; it was never observed before, so the worker stage
# could not have refused a deleted or replaced one.
w2_observe NetworkPolicy "$WORKER_POLICY_NAME" "$AGENT_NS"
w2_observe Job           "$JOB_NAME"           "$AGENT_NS"

python3 - "$STAGE_OBS.raw" "$STAGE_OBS" <<'PY' || w2_fail "could not record the stage observations"
import json, sys
src, dest = sys.argv[1], sys.argv[2]
out = {}
with open(src, encoding="utf-8") as fh:
    for line in fh:
        if not line.strip():
            continue
        kind, name, ns, status, uid, detail = line.rstrip("\n").split("\t")
        entry = {"status": status}
        if uid:
            entry["uid"] = uid
        if detail:
            entry["detail"] = detail
        out[f"{kind}/{ns}/{name}"] = entry
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
rm -f "$STAGE_OBS.raw"

# The Job is only a creation target when a worker was actually requested. Naming it
# otherwise would make `--stage gateway` refuse to run beside a worker from an
# earlier stage of the SAME run, which is the normal end state of a full sequence.
# Keyed by ROLE, and each object identified by its full kind/namespace/name. Keyed by
# kind, the two NetworkPolicies collided and only the gateway's was ever passed -- so
# the worker stage skipped creating the worker policy while claiming the gate had
# verified it. A role cannot collide, and an unnamed required role is now a refusal.
STAGE_EXPECT=(
  "--expect=deployment=Deployment/$GW_NS/$GW_NAME"
  "--expect=service=Service/$GW_NS/$GW_NAME"
  "--expect=gateway_policy=NetworkPolicy/$GW_NS/$POLICY_NAME"
  "--expect=worker_policy=NetworkPolicy/$AGENT_NS/$WORKER_POLICY_NAME"
)
if [ "$WANT_WORKER_JOB" = 1 ]; then
  STAGE_EXPECT+=("--expect=worker_job=Job/$AGENT_NS/$JOB_NAME")
fi

python3 "$STAGE_LIB" gate --stage "$STAGE" --ledger "$LEDGER" \
  --observations "$STAGE_OBS" "${STAGE_EXPECT[@]}" \
  || w2_fail "the $STAGE stage may not proceed (see the refusal above). Nothing was
     created by this invocation. Observations: $STAGE_OBS"

# ---------------------------------------------------------------------------
# resolve the digest to pin
# ---------------------------------------------------------------------------
printf '\n== resolving the gateway image digest (pin, never a tag) ==\n'
LIVE_DEPLOY_JSON="$EVIDENCE_DIR/live-gateway-deployment.json"
w2_kubectl get deploy bedrockgateway -n "$GW_NS" -o json > "$LIVE_DEPLOY_JSON" \
  || w2_fail "could not read the live gateway deployment. The fixture is a COPY of the
     reviewed composition; without the live spec there is nothing to copy and
     hand-assembling a lookalike is the defect this replaces."

GW_IMAGE="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
cs=d["spec"]["template"]["spec"]["containers"]
print(next(c["image"] for c in cs if c["name"]=="bedrockgateway"))
' "$LIVE_DEPLOY_JSON")"

if [ -n "$REVIEWED_GATEWAY_IMAGE" ]; then
  FIXTURE_IMAGE="$REVIEWED_GATEWAY_IMAGE"
  GATEWAY_DIGEST="${FIXTURE_IMAGE##*@}"
else
# Prefer the digest the live pods actually run (imageID), not a tag lookup: a tag
# can have moved since the rollout, and the evaluated revision must be the one
# serving.
LIVE_DIGEST="$(w2_kubectl get pods -n "$GW_NS" -l app=bedrockgateway \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="bedrockgateway")].imageID}{"\n"}{end}' \
  2>/dev/null | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' | sort -u | head -1 || true)"

if [ -n "$LIVE_DIGEST" ]; then
  GATEWAY_DIGEST="$LIVE_DIGEST"
  w2_ok "gateway digest $GATEWAY_DIGEST (from the running pods' imageID)"
else
  case "$GW_IMAGE" in
    *@sha256:*) GATEWAY_DIGEST="${GW_IMAGE##*@}" ;;
    *) w2_fail "could not determine the running gateway digest, and the deployment image
     ($GW_IMAGE) is tag-referenced. Refusing to evaluate an unpinned revision: the
     evidence would not identify what was actually tested." ;;
  esac
  w2_ok "gateway digest $GATEWAY_DIGEST (from the deployment spec)"
fi
FIXTURE_IMAGE="${GW_IMAGE%%:*}"
case "$GW_IMAGE" in
  *@*) FIXTURE_IMAGE="${GW_IMAGE%@*}@$GATEWAY_DIGEST" ;;
  *)   FIXTURE_IMAGE="${GW_IMAGE%:*}@$GATEWAY_DIGEST" ;;
esac
fi
w2_note "fixture image: $FIXTURE_IMAGE"

# ---------------------------------------------------------------------------
# control-critical prerequisites — FAIL CLOSED, before creating anything
# ---------------------------------------------------------------------------
# The published script noted these and continued, producing a fixture whose
# control path could not work. A fixture that cannot exercise the feature is not
# a cheaper evaluation, it is a misleading one.
printf '\n== control-critical prerequisites ==\n'
# The list comes FROM the renderer, not from a copy of it here. Re-listing the
# composition in a second place is the defect that lost the nine secret refs;
# duplicating it for the precondition check would reintroduce the same drift.
CONTROL_SECRET_REFS="$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import render_fixture
for secret, key in render_fixture.CONTROL_CRITICAL_SECRETS:
    print(f"{secret}:{key}")
' "$HERE/lib")" || w2_fail "could not read CONTROL_CRITICAL_SECRETS from lib/render_fixture.py"
[ -n "$CONTROL_SECRET_REFS" ] || w2_fail "the control-critical secret list is empty; refusing to
     report preconditions met against an empty list"

PREREQ_OK=1
while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  secret="${ref%%:*}"; key="${ref##*:}"
  set +e
  # Only whether the KEY EXISTS. Never reads or prints the value.
  present="$(w2_kubectl get secret "$secret" -n "$GW_NS" \
    -o "jsonpath={.data.$key}" 2>/dev/null | head -c 1)"
  set -e
  if [ -n "$present" ]; then
    w2_ok "secret $secret/$key is present"
  else
    PREREQ_OK=0
    w2_note "MISSING: secret $secret/$key"
  fi
done <<<"$CONTROL_SECRET_REFS"

if [ "$PREREQ_OK" != 1 ]; then
  printf '\n' >&2
  printf 'FAIL: control-critical secret material is missing, so the fixture gateway could\n' >&2
  printf '  not sign or verify a control envelope, validate a session, or authenticate the\n' >&2
  printf '  protected worker. Every control check would fail for a reason unrelated to the\n' >&2
  printf '  software under review, and a report built from that would be worthless.\n' >&2
  printf '\n' >&2
  printf '  Nothing has been created. Stopping BEFORE creation is deliberate: the previous\n' >&2
  printf '  revision continued here and produced a known-nonfunctional fixture.\n' >&2
  printf '\n' >&2
  printf '  Provisioning this material is Terraform-owned (agent-authority-bootstrap.tf)\n' >&2
  printf '  and is root'"'"'s change to make, not this script'"'"'s.\n' >&2
  exit 1
fi

# The ordinary gateway role is scoped to its ordinary queues. A policy on this
# disposable queue grants only its real IRSA principal access to this exact ARN;
# deleting the queue removes the grant without editing shared IAM policies.
QUEUE_ATTRIBUTES="FifoQueue=true,ContentBasedDeduplication=true,MessageRetentionPeriod=3600"
if [ -n "$REVIEWED_GATEWAY_IMAGE" ]; then
  GATEWAY_SA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["spec"]["template"]["spec"]["serviceAccountName"])' "$LIVE_DEPLOY_JSON")"
  w2_kubectl get serviceaccount "$GATEWAY_SA" -n "$GW_NS" -o json > "$EVIDENCE_DIR/gateway-serviceaccount.json"
  python3 - "$EVIDENCE_DIR/gateway-serviceaccount.json" "$ACCOUNT" "$W2_REGION" "$QUEUE_NAME" "$NONCE" "$EVIDENCE_DIR/queue-attributes.json" <<'PY_QUEUE_POLICY'
import json, re, sys
source, account, region, name, nonce, output = sys.argv[1:]
role = json.load(open(source)).get("metadata", {}).get("annotations", {}).get("eks.amazonaws.com/role-arn", "")
if not re.fullmatch(r"arn:aws:iam::" + re.escape(account) + r":role/[A-Za-z0-9_+=,.@/-]+", role):
    sys.exit("gateway service account has no target-account IRSA role")
policy = {"Version": "2012-10-17", "Statement": [{
    "Sid": "FixtureGateway" + nonce,
    "Effect": "Allow", "Principal": {"AWS": role},
    "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
    "Resource": f"arn:aws:sqs:{region}:{account}:{name}",
}]}
with open(output, "w") as stream:
    json.dump({"FifoQueue": "true", "ContentBasedDeduplication": "true", "MessageRetentionPeriod": "3600", "Policy": json.dumps(policy)}, stream)
PY_QUEUE_POLICY
  QUEUE_ATTRIBUTES="file://$EVIDENCE_DIR/queue-attributes.json"
fi

# ---------------------------------------------------------------------------
# dedicated queue — probe, create, tag, verify the tag
# ---------------------------------------------------------------------------
# The fixture must never publish to the shared submit queue, so it gets its own.
printf '\n== dedicated fixture queue ==\n'
QUEUE_URL=""
if [ "$STAGE" = worker ]; then
  # The worker stage JOINS the fixture the gateway stage built, so it must use that
  # stage's queue. Read from the ledger, not re-derived from the name: re-deriving
  # would work right up until it named a queue from another run with the same run id
  # prefix, and the worker would then publish where nothing is reading.
  set +e
  QUEUE_URL="$(python3 "$STAGE_LIB" queue-url --ledger "$LEDGER")"; q_rc=$?
  set -e
  [ "$q_rc" -eq 0 ] && [ -n "$QUEUE_URL" ] || w2_fail "the worker stage could not recover
     this run's fixture queue from the shared ledger (see the refusal above). The gateway
     stage creates it; without it the worker would have no queue to join."
  w2_ok "reusing the gateway stage's queue (from the ledger)"
  w2_note "queue: $QUEUE_URL"
else
  # Absence is still REQUIRED of every stage that creates the queue. SQS has no uid,
  # so unlike the k8s objects there is no way to tell a same-named queue apart from
  # the one this run made -- which is exactly why CreateQueue's name-match idempotence
  # is dangerous here and why the worker stage reads the URL instead of re-creating.
  set +e
  EXISTING_URL="$(w2_aws sqs get-queue-url --queue-name "$QUEUE_NAME" \
    --query QueueUrl --output text 2>&1)"; probe_rc=$?
  set -e
  if [ "$probe_rc" -eq 0 ]; then
    w2_fail "queue $QUEUE_NAME already exists ($EXISTING_URL). Refusing: CreateQueue is
     idempotent on a name match, so creating it would ADOPT a queue this run did
     not create and the ledger would then authorise deleting it. Use a new --run-id.
     (If the gateway stage of THIS run created it, you want --stage worker, which
     reuses the recorded queue instead of creating one.)"
  fi
  case "$EXISTING_URL" in
    *NonExistentQueue*|*"does not exist"*) w2_ok "queue $QUEUE_NAME is absent" ;;
    *) w2_fail "could not determine whether queue $QUEUE_NAME exists: $EXISTING_URL
     Refusing to create: a failed lookup is not an absent queue." ;;
  esac
fi

if [ "$STAGE" = worker ]; then
  : # already resolved above
elif [ "$CHECK_ONLY" = 1 ]; then
  w2_note "check-only: would create $QUEUE_NAME tagged adp-w2-nonce=<nonce>"
  QUEUE_URL="https://sqs.$W2_REGION.amazonaws.com/$ACCOUNT/$QUEUE_NAME"
else
  # The nonce tag is applied AT CREATION so there is no window in which the
  # queue exists without its ownership evidence.
  QUEUE_URL="$(w2_aws sqs create-queue --queue-name "$QUEUE_NAME" \
    --attributes "$QUEUE_ATTRIBUTES" \
    --tags "adp-w2-nonce=$NONCE,adp-w2-run=$RUN_ID,adp-w2-issue=3968" \
    --query QueueUrl --output text)" \
    || w2_fail "could not create the dedicated fixture queue"

  # Read the tag back: proof from the server, not from our own intent.
  LIVE_NONCE="$(w2_aws sqs list-queue-tags --queue-url "$QUEUE_URL" \
    --query 'Tags."adp-w2-nonce"' --output text 2>/dev/null || echo "")"
  [ "$LIVE_NONCE" = "$NONCE" ] || w2_fail "queue $QUEUE_NAME does not carry this run's nonce
     tag (read back: '$LIVE_NONCE'). Refusing to record it as ours; it may be a
     pre-existing queue. Remove it by hand if this run did create it."

  python3 "$OWNERSHIP" record-queue --ledger "$LEDGER" --run-id "$RUN_ID" \
    --account-id "$ACCOUNT" --name "$QUEUE_NAME" --url "$QUEUE_URL" --nonce "$NONCE" \
    || w2_fail "created queue $QUEUE_NAME but could not record it; remove it by hand"
  w2_ok "created $QUEUE_NAME (nonce tag verified, recorded in ledger)"
fi

if [ -n "$REVIEWED_GATEWAY_IMAGE" ] && [ "$CHECK_ONLY" != 1 ]; then
  w2_aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names Policy \
    --output json > "$EVIDENCE_DIR/queue-policy-readback.json"
  python3 - "$EVIDENCE_DIR/queue-attributes.json" "$EVIDENCE_DIR/queue-policy-readback.json" <<'PY_QUEUE_READBACK'
import json, sys
expected = json.loads(json.load(open(sys.argv[1]))["Policy"])
actual = json.loads(json.load(open(sys.argv[2])).get("Attributes", {}).get("Policy", "{}"))
if actual != expected:
    sys.exit("fixture queue policy does not match the exact gateway principal and queue grant")
PY_QUEUE_READBACK
fi

# ---------------------------------------------------------------------------
# render from the LIVE composition
# ---------------------------------------------------------------------------
printf '\n== rendering the fixture from the live composition ==\n'
MANIFEST_DIR="$EVIDENCE_DIR/manifests"
# Rendered WITHOUT the ALB's addresses, on every stage, including --stage all where the
# edge already exists and they could have been rendered in. Deliberate: the ALB rule
# arrives by exactly ONE mechanism -- the uid-bound, resourceVersion-preconditioned
# mutation in the worker section below -- rather than by rendering in one stage layout
# and by mutation in the other. Two paths to the same admission means the one that runs
# less often is the one that drifts, and this is the admission whose failure looks like
# the finding under test.
python3 "$RENDER" \
  --live-deployment "$LIVE_DEPLOY_JSON" \
  --run-id "$RUN_ID" --nonce "$NONCE" \
  --name "$GW_NAME" --namespace "$GW_NS" --agent-namespace "$AGENT_NS" \
  --image "$FIXTURE_IMAGE" --queue-url "$QUEUE_URL" "${FIXTURE_APPROVAL_ARGS[@]}" \
  --policy-name "$POLICY_NAME" --out-dir "$MANIFEST_DIR" \
  || w2_fail "rendering refused (see above). Nothing further was created."

# ---------------------------------------------------------------------------
# create — policies FIRST, exclusively, recording each uid
# ---------------------------------------------------------------------------
w2_create_and_record() { # file kind name namespace
  local file="$1" kind="$2" name="$3" ns="$4" uid out rc
  if [ "$CHECK_ONLY" = 1 ]; then
    set +e
    out="$(w2_kubectl create -f "$file" --dry-run=server -o name 2>&1)"; rc=$?
    set -e
    [ "$rc" -eq 0 ] || w2_fail "server dry-run rejected $kind/$name: $out"
    w2_ok "check-only: server accepted $kind/$name"
    return 0
  fi

  # `create`, NOT `apply`: apply would ADOPT a pre-existing object of this name
  # and mutate it, and the ledger would then authorise deleting it.
  set +e
  out="$(w2_kubectl create -f "$file" -o json 2>&1)"; rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *AlreadyExists*) w2_fail "$kind/$name already exists. Refusing to adopt it." ;;
      *) w2_fail "could not create $kind/$name: $out" ;;
    esac
  fi

  # The server-assigned uid is the ownership proof. Recorded AFTER creation,
  # from the server's own response.
  uid="$(printf '%s' "$out" | python3 -c '
import json,sys
doc = json.load(sys.stdin)
items = doc.get("items", [doc])
print(items[0].get("metadata", {}).get("uid", ""))
')"
  [ -n "$uid" ] || w2_fail "created $kind/$name but the server returned no metadata.uid;
     it cannot be proven ours at teardown. Remove it by hand."

  python3 "$OWNERSHIP" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
    --account-id "$ACCOUNT" --kind "$kind" --name "$name" --namespace "$ns" --uid "$uid" \
    || w2_fail "created $kind/$name (uid $uid) but could not record it; remove it by hand"
  w2_ok "created $kind/$name uid=$uid (recorded)"
}

# The renderer emits v1/List documents. Split them into one file per object so
# each object is created by its own call and its own server-assigned uid is
# captured -- a single `create -f` on a List returns them batched, which is how
# the previous revision ended up recording no uid at all.
w2_split_list() { # listfile prefix  -> prints "file<TAB>kind<TAB>name<TAB>namespace"
  python3 - "$1" "$MANIFEST_DIR" "$2" <<'PY'
import json, sys
src, outdir, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
doc = json.load(open(src))
for index, item in enumerate(doc["items"]):
    meta = item["metadata"]
    dest = f"{outdir}/{prefix}-{index}-{item['kind'].lower()}-{meta['name']}.json"
    with open(dest, "w") as fh:
        json.dump(item, fh, indent=2)
    print("\t".join([dest, item["kind"], meta["name"], meta["namespace"]]))
PY
}

w2_create_each() { # listfile prefix
  local list; list="$(w2_split_list "$1" "$2")" \
    || w2_fail "could not split $1 into individual objects"
  [ -n "$list" ] || w2_fail "$1 rendered no objects; refusing to report a fixture that is empty"
  while IFS=$'\t' read -r file kind name ns; do
    [ -n "$file" ] || continue
    w2_create_and_record "$file" "$kind" "$name" "$ns"
  done <<<"$list"
}

if [ "$STAGE" = worker ]; then
  # Already created by the gateway stage, and the stage gate above proved each one is
  # PRESENT with the uid this run recorded -- not merely that something of that name
  # is standing there. Re-creating them is not a resume: it would either fail or
  # adopt, and the ledger's uid would stop matching either way.
  w2_note "skipping the policies and the gateway: the gateway stage created them and the"
  w2_note "  stage gate confirmed each is the uid this run's ledger records -- BOTH"
  w2_note "  NetworkPolicies ($POLICY_NAME in $GW_NS and $WORKER_POLICY_NAME in $AGENT_NS),"
  w2_note "  the Deployment and the Service. The worker-side policy is the one that confines"
  w2_note "  the protected worker about to be created, so skipping its creation is only sound"
  w2_note "  because it was verified by identity, not because it was assumed still there."
else
  printf '\n== creating isolation policies FIRST ==\n'
  # Before anything can listen. There must never be a window in which the fixture
  # is reachable but unprotected.
  w2_create_each "$MANIFEST_DIR/00-policies.json" 00-policy

  printf '\n== creating the fixture gateway ==\n'
  w2_create_each "$MANIFEST_DIR/10-gateway.json" 10-gateway
fi

# ---------------------------------------------------------------------------
# the protected worker Job — opt-in, and only against the FIXTURE edge
# ---------------------------------------------------------------------------
# It is opt-in because it is the one part of this fixture that holds PROTECTED
# AUTHORITY, and because it requires an input this script cannot derive: the
# fixture-only control endpoint from #5836. Not creating it leaves a gateway-only
# fixture, which is a smaller but still honest fixture.
printf '\n== protected worker Job ==\n'
WORKER_CREATED=0
WORKER_JOB_UID=""
WORKER_POD_NAME=""
WORKER_POD_UID=""
WORKER_DIGEST=""
WORKER_REASON=""
EXPECTED_IDENTITY_DOC=""

if [ "$WANT_WORKER_JOB" != 1 ] && [ "$STAGE" = gateway ]; then
  # Distinguished from the line below deliberately. Both leave worker_job_created
  # false, but they mean opposite things: this one is a sequence MID-FLIGHT, where the
  # worker is expected to arrive in a later invocation, and the other is a deliberately
  # gateway-only fixture that is finished. A reader of the record who cannot tell them
  # apart would read a half-run staged sequence as a completed gateway-only one.
  WORKER_REASON="the gateway stage does not create the worker; --stage worker does, once #5836's edge exists"
  w2_note "no worker Job: this is the GATEWAY stage, and the worker cannot be created until"
  w2_note "  #5836's edge publishes a control endpoint in front of the Service just created."
  w2_note "  W2-03/04/05 are not_run until --stage worker has run -- that is mid-sequence,"
  w2_note "  not a finished gateway-only fixture. See the handoff document below."
elif [ "$WANT_WORKER_JOB" != 1 ]; then
  WORKER_REASON="not requested (--worker-job was not passed)"
  w2_note "not creating a fixture worker Job (pass --worker-job to create one)."
  w2_note "  It is opt-in because it is the only part of this fixture that holds"
  w2_note "  protected authority, and because its control endpoint must come from"
  w2_note "  #5836's fixture edge -- a value this script must be GIVEN, never guessed."
  w2_note "  Without it the fixture is gateway-only: W2-03/04/05 have no worker to"
  w2_note "  pause, and the harness will report those as not_run rather than passed."
else
  # -------------------------------------------------------------------------
  # the endpoint. Required, never defaulted.
  # -------------------------------------------------------------------------
  # Historically this script REFUSED --worker-job outright, because every https
  # control endpoint resolved through API Gateway to the ORDINARY gateway's pods
  # by label -- so a fixture worker would have bootstrapped against live traffic.
  # #5836 built a fixture-only edge whose worker_control_endpoint terminates at
  # the FIXTURE gateway, which is what makes this creatable at all. The endpoint
  # is therefore the load-bearing input, and a default would silently reintroduce
  # exactly the defect the refusal existed to prevent.
  # A RECEIPT, not a URL. Root: "Consume account/run/nonce-bound #5836 output, not
  # arbitrary HTTPS."
  #
  # The two checks below this (https, and not-the-ordinary-API) are both kept and
  # both real, but together they establish only that the value is not one specific
  # known-bad edge. In this account, in this region, EVERY fixture edge has a
  # plausible execute-api hostname and none of them contains the ordinary API id --
  # so a typo, a stale shell variable or a URL from a colleague's run all passed,
  # and the worker then bootstrapped against an edge this run neither owns nor can
  # tear down. Everything else in this tooling binds evidence to identities; this
  # was the one load-bearing value taken on the operator's word.
  [ -n "$EDGE_RECEIPT" ] || w2_fail "--worker-job requires --edge-receipt.
     Pass #5836's WHOLE outputs document. Its \`ownership\` output carries the
     bindings (run_nonce/account_id/region/environment); the endpoint is a SEPARATE
     top-level output, so \`terraform output -json ownership\` alone can never supply
     one -- and neither can the ownership.json their apply writes:
       cd modules/gateway/infra/fixture-edge
       terraform output -json > \$EVIDENCE_DIR/edge-outputs.json
     The endpoint is READ FROM that document rather than supplied, so it cannot be a
     URL belonging to another run's edge. --worker-control-endpoint remains accepted
     as a CROSS-CHECK and must agree; on its own it is an unverified string."

  # The ordinary API id is authoritative in SSM, so it is compared rather than
  # pattern-matched, and an unreadable parameter is a refusal: "could not check" must
  # not read as "checked and fine". Resolved BEFORE the receipt is consulted so the
  # production-edge refusal applies even to a receipt that names it.
  set +e
  ORDINARY_API_ID="$(w2_aws ssm get-parameter \
    --name "/adp/$W2_ENVIRONMENT/gateway/apigw-invoke-url" \
    --query Parameter.Value --output text 2>&1)"; ord_rc=$?
  set -e
  if [ "$ord_rc" -ne 0 ]; then
    w2_fail "could not read the ORDINARY gateway's API id from SSM
     (/adp/$W2_ENVIRONMENT/gateway/apigw-invoke-url): $(printf '%s' "$ORDINARY_API_ID" | tr -d '\n' | cut -c1-160)
     Refusing to create a control-enabled worker: without it this cannot prove the
     receipt's endpoint is the FIXTURE edge and not production's. An unreadable check
     is not a passed check."
  fi

  # Derive the ID only from the canonical HTTPS invoke URL for this region/stage.
  ORDINARY_INVOKE_URL="$ORDINARY_API_ID"
  [[ "$ORDINARY_INVOKE_URL" =~ ^https://([a-z0-9]+)\.execute-api\. ]] \
    || w2_fail "ORDINARY gateway apigw-invoke-url is invalid; refusing worker creation."
  ORDINARY_API_ID="${BASH_REMATCH[1]}"
  [ "$ORDINARY_INVOKE_URL" = "https://${ORDINARY_API_ID}.execute-api.${W2_REGION}.amazonaws.com/${W2_ENVIRONMENT}" ] \
    || w2_fail "ORDINARY gateway apigw-invoke-url has an unexpected region, stage or URL component."

  # NONCE comes from THIS run's ledger (or --resume-nonce), so the receipt is bound
  # to the same run whose resources the ledger authorises tearing down. stdout is the
  # endpoint alone; every explanatory line goes to stderr, which the operator sees.
  EDGE_PROVENANCE="$EVIDENCE_DIR/edge-endpoint-provenance.json"
  set +e
  RESOLVED_ENDPOINT="$(python3 "$EDGE_LIB" \
    --receipt "$EDGE_RECEIPT" \
    --run-nonce "$NONCE" \
    --account-id "$ACCOUNT" \
    --region "$W2_REGION" \
    --environment "$W2_ENVIRONMENT" \
    --ordinary-api-id "$ORDINARY_API_ID" \
    ${WORKER_ENDPOINT:+--supplied-endpoint "$WORKER_ENDPOINT"} \
    --provenance-out "$EDGE_PROVENANCE")"; edge_rc=$?
  set -e
  [ "$edge_rc" -eq 0 ] || w2_fail "the control endpoint could not be bound to this run's
     fixture edge (see the refusal above). Nothing has been created that depends on it.
     A worker pointed at an edge outside this run's ledger would hold protected
     authority against infrastructure this run cannot tear down."
  [ -n "$RESOLVED_ENDPOINT" ] || w2_fail "the edge receipt resolved an EMPTY endpoint
     while exiting 0. Refusing: an empty control endpoint is not a usable one."
  WORKER_ENDPOINT="$RESOLVED_ENDPOINT"
  w2_ok "control endpoint bound to this run's edge receipt (nonce/account/region match)"
  w2_note "endpoint: $WORKER_ENDPOINT"
  w2_note "provenance: $EDGE_PROVENANCE"
  w2_ok "control endpoint is not the ordinary API ($ORDINARY_API_ID)"

  # The Job's absence was established by the stage gate above, against the same
  # observation set and to the same standard as the gateway's objects -- BEFORE the
  # endpoint was resolved and before anything was created. It is not re-checked here:
  # a second lookup would be a second answer, and the one that mattered was the one
  # taken before creation began.

  # -------------------------------------------------------------------------
  # admit the fixture edge's ALB to the fixture gateway — BEFORE the worker
  # -------------------------------------------------------------------------
  # WHY A MUTATION HERE AND NOT ONLY AT RENDER TIME. The fixture gateway policy admits
  # callers by namespaceSelector. With `target-type: ip` the edge's ALB connects from
  # its OWN elastic network interfaces, which belong to the load balancer and to no
  # pod and no namespace -- so no selector matches it at any width. In the STAGED
  # lifecycle the gateway is created before #5836's edge exists, so its addresses
  # cannot be rendered into the policy: they are not knowable yet. The rule therefore
  # has to arrive by a later, narrowed mutation, on this run's policy only.
  # --stage all also renders without ALB addresses and uses this same mutation
  # path. Only a readback proving the existing rule can make this a no-op.
  #
  # AND WHY BEFORE THE WORKER. If the policy does not admit the ALB, the worker's
  # bootstrap handshake never completes and the run reports "the protected worker
  # failed its bootstrap" -- the exact conclusion Wave 2 exists to establish or
  # refute. A fixture that can produce the result it is measuring is not a fixture,
  # so this is a gate on worker creation rather than a diagnosis afterwards.
  printf '\n-- admitting the fixture edge ALB to the fixture gateway policy --\n'

  # The ALB source comes from the receipt, bound to this run and cross-checked against
  # the ARN this run's ledger records. `resolve-alb-source` refuses a document belonging
  # to another run, a wider-than-/32 entry, a bare address, and a port that is not the
  # one the fixture pod serves.
  ALB_SOURCE_JSON="$EVIDENCE_DIR/alb-policy-source.json"
  set +e
  python3 "$EDGE_LIB" resolve-alb-source \
    --receipt "$EDGE_RECEIPT" \
    --run-nonce "$NONCE" \
    --account-id "$ACCOUNT" \
    --region "$W2_REGION" \
    --environment "$W2_ENVIRONMENT" \
    --expected-alb-arn "$EDGE_ALB_ARN" \
    --out "$ALB_SOURCE_JSON" >/dev/null; alb_rc=$?
  set -e
  [ "$alb_rc" -eq 0 ] || w2_fail "the fixture ALB's policy source could not be bound to this
     run (see the refusal above). Nothing has been mutated and no worker was created."

  ALB_CIDRS="$(python3 -c '
import json,sys
print(",".join(json.load(open(sys.argv[1]))["cidrs"]))' "$ALB_SOURCE_JSON")"
  ALB_PORT="$(python3 -c '
import json,sys
print(json.load(open(sys.argv[1]))["container_port"])' "$ALB_SOURCE_JSON")"
  [ -n "$ALB_CIDRS" ] || w2_fail "the bound ALB source carries no addresses while exiting 0.
     Refusing: an empty allowance denies the edge, and that denial is indistinguishable
     from the protected worker failing its bootstrap."
  [ -n "$ALB_PORT" ] || w2_fail "the bound ALB source names no container port while exiting 0.
     Refusing rather than defaulting one: a rule on the wrong port admits the edge to
     nothing, while reading in the manifest as though it admits it."

  # A FRESH observation of the load balancer's interfaces, then a comparison against the
  # receipt. A Terraform output records what was true WHEN IT RAN: between that apply and
  # now an interface can be replaced, and a released address can be REASSIGNED in this
  # same VPC -- so a stale allowance is a live admission of something else, not a
  # harmless leftover. The AWS call is made HERE; the decision is made by a pure function
  # that makes none, so every refusal is testable without an account.
  ALB_ARN_SUFFIX="${EDGE_ALB_ARN##*:loadbalancer/}"
  set +e
  FRESH_IPS="$(w2_aws ec2 describe-network-interfaces \
    --filters "Name=description,Values=ELB $ALB_ARN_SUFFIX" \
    --query 'NetworkInterfaces[].PrivateIpAddress' --output text 2>&1)"; eni_rc=$?
  set -e
  [ "$eni_rc" -eq 0 ] || w2_fail "could not re-observe the fixture ALB's network interfaces:
     $(printf '%s' "$FRESH_IPS" | tr -d '\n' | cut -c1-200)
     Refusing to admit an address set nothing corroborates. 'Could not observe' is not
     'unchanged', and the addresses in the receipt may since have been reassigned."
  [ -n "$FRESH_IPS" ] || w2_fail "the fixture ALB has no network interfaces in this account.
     #5836's own precondition treats an empty result as a refusal and it means the same
     here: either the load balancer is gone or the query is wrong."
  FRESH_CIDRS="$(printf '%s' "$FRESH_IPS" | tr '\t' '\n' | sed '/^$/d;s|$|/32|' | paste -sd,)"

  set +e
  python3 "$EDGE_LIB" check-alb-current \
    --rendered "$ALB_SOURCE_JSON" \
    --observed-cidrs "$FRESH_CIDRS" \
    --observed-arn "$EDGE_ALB_ARN" \
    --expected-alb-arn "$EDGE_ALB_ARN" >/dev/null; fresh_rc=$?
  set -e
  [ "$fresh_rc" -eq 0 ] || w2_fail "the receipt's ALB addresses no longer match the load
     balancer (see the refusal above). Re-read #5836's outputs and retry; nothing was
     mutated and no worker was created."
  w2_ok "the receipt's ALB addresses still match the live load balancer"

  # The uid comes from the LEDGER, never from the cluster: a live uid always matches
  # itself, so re-reading it would let a replacement policy pass the check that exists
  # to catch it.
  #
  # Under --check-only there may be NO ledger record and no live policy, because a
  # check-only run created neither. That case is reported as an unmade check, not
  # skipped quietly and not failed: the remaining steps operate on an object that does
  # not exist, and saying "would replace it" about a policy nobody created would be the
  # same vacuous pass this tooling exists to avoid. (--stage worker --check-only against
  # a gateway an earlier real run created DOES have both, and runs the whole composition
  # below, sending nothing.)
  ALB_COMPOSED=0
  set +e
  GW_POLICY_UID="$(python3 "$STAGE_LIB" k8s-uid --ledger "$LEDGER" \
    --kind NetworkPolicy --name "$POLICY_NAME" --namespace "$GW_NS" 2>&1)"; uid_rc=$?
  set -e
  if [ "$uid_rc" -ne 0 ] && [ "$CHECK_ONLY" = 1 ]; then
    w2_note "check-only: this run's ledger records no NetworkPolicy/$POLICY_NAME, because"
    w2_note "  check-only created none. The ALB admission CANNOT be checked here: there is no"
    w2_note "  live policy to compose against. This is an unmade check, not a passed one --"
    w2_note "  run --stage gateway for real, then --stage worker --check-only to exercise it."
    GW_POLICY_UID=""
  elif [ "$uid_rc" -ne 0 ]; then
    w2_fail "could not establish this run's uid for NetworkPolicy/$POLICY_NAME:
     $(printf '%s' "$GW_POLICY_UID" | tr -d '\n' | cut -c1-200)
     Without it the mutation cannot be shown to target this run's own policy."
  fi

  if [ -n "$GW_POLICY_UID" ]; then
  LIVE_POLICY_JSON="$EVIDENCE_DIR/live-gateway-policy.json"
  w2_kubectl get networkpolicy "$POLICY_NAME" -n "$GW_NS" -o json > "$LIVE_POLICY_JSON" \
    || w2_fail "could not read back NetworkPolicy/$POLICY_NAME in $GW_NS. The mutation is
     composed from the LIVE object -- composing it from the rendered manifest would only
     confirm this tooling agrees with itself."

  ALB_UPDATE_JSON="$EVIDENCE_DIR/alb-policy-update.json"
  set +e
  python3 "$POLICY_LIB" compose-update \
    --live-policy "$LIVE_POLICY_JSON" \
    --uid "$GW_POLICY_UID" --name "$POLICY_NAME" --namespace "$GW_NS" \
    --nonce "$NONCE" --cidrs "$ALB_CIDRS" --port "$ALB_PORT" \
    --out "$ALB_UPDATE_JSON" >/dev/null; compose_rc=$?
  set -e
  [ "$compose_rc" -eq 0 ] || w2_fail "refusing to add the ALB rule to NetworkPolicy/$POLICY_NAME
     (see the refusal above). Nothing was mutated and no worker was created. The ordinary
     gateway's policy is never touched by this tooling."
  ALB_COMPOSED=1

  ALB_ACTION="$(python3 -c '
import json,sys
print(json.load(open(sys.argv[1]))["action"])' "$ALB_UPDATE_JSON")"
  if [ "$ALB_ACTION" = none ]; then
    # Idempotent re-run. Reported as "already" rather than as an update: "the rule was
    # applied" and "the rule was already there" are different evidence about whether
    # the mutation path works at all.
    w2_ok "the fixture gateway policy already admits this run's ALB (no change made)"
  elif [ "$CHECK_ONLY" = 1 ]; then
    w2_note "check-only: would replace NetworkPolicy/$POLICY_NAME to admit $ALB_CIDRS on"
    w2_note "  TCP/$ALB_PORT, conditional on resourceVersion. Not sent."
  else
    # `replace`, NOT `apply`: apply would ADOPT whatever object of this name is
    # present. The body carries the live metadata.resourceVersion, so the API server
    # rejects it with 409 Conflict if the policy changed at all since the read back --
    # the precondition is enforced by the server, atomically with the write, rather
    # than by a comparison here followed by a hope.
    ALB_BODY_JSON="$EVIDENCE_DIR/alb-policy-body.json"
    python3 -c '
import json,sys
json.dump(json.load(open(sys.argv[1]))["body"], open(sys.argv[2], "w"), indent=2)' \
      "$ALB_UPDATE_JSON" "$ALB_BODY_JSON" \
      || w2_fail "could not extract the composed policy body"
    set +e
    replace_out="$(w2_kubectl replace -f "$ALB_BODY_JSON" 2>&1)"; replace_rc=$?
    set -e
    if [ "$replace_rc" -ne 0 ]; then
      case "$replace_out" in
        *Conflict*|*"object has been modified"*)
          w2_fail "NetworkPolicy/$POLICY_NAME changed between the read and this write, so the
     resourceVersion precondition rejected it. That is the guard working: writing anyway
     would have overwritten whatever the policy has become, possibly a replacement this
     run does not own. Re-run this stage to observe it afresh." ;;
        *) w2_fail "could not apply the ALB rule to NetworkPolicy/$POLICY_NAME: $replace_out" ;;
      esac
    fi
    w2_ok "added the ALB ingress rule to NetworkPolicy/$POLICY_NAME (uid-bound, precondition held)"
  fi

  # RE-OBSERVE. The mutation reporting success is not evidence that the live object
  # admits the ALB: `kubectl` reports success for a write that changed nothing, and the
  # object could have been replaced immediately after. This reads the policy AGAIN and
  # decides from what the API server actually holds -- the gate on worker creation.
  if [ "$CHECK_ONLY" = 1 ]; then
    w2_note "check-only: the post-mutation readback is NOT run, because no mutation was sent."
    w2_note "  The gate on worker creation is therefore unmade, not satisfied -- and no worker"
    w2_note "  is created under --check-only either, so nothing proceeds on it."
  else
    ALB_OBSERVED_JSON="$EVIDENCE_DIR/alb-policy-observed.json"
    w2_kubectl get networkpolicy "$POLICY_NAME" -n "$GW_NS" -o json > "$LIVE_POLICY_JSON.after" \
      || w2_fail "could not re-read NetworkPolicy/$POLICY_NAME after the mutation. Refusing to
     create the worker: an unverified admission is not an admission, and a denied edge
     reads as the protected worker failing its bootstrap."
    set +e
    python3 "$POLICY_LIB" admits \
      --live-policy "$LIVE_POLICY_JSON.after" \
      --uid "$GW_POLICY_UID" --name "$POLICY_NAME" --namespace "$GW_NS" \
      --nonce "$NONCE" --cidrs "$ALB_CIDRS" --port "$ALB_PORT" \
      --out "$ALB_OBSERVED_JSON" >/dev/null; admits_rc=$?
    set -e
    [ "$admits_rc" -eq 0 ] || w2_fail "the live fixture gateway policy does not admit this run's
     ALB (see the refusal above). NOT creating the worker: its bootstrap would never
     complete and the run would report the protected worker as having failed, which is
     the conclusion this fixture exists to establish or refute."
    w2_ok "verified from the live policy: the fixture edge ALB is admitted on TCP/$ALB_PORT"
    w2_note "observation: $ALB_OBSERVED_JSON"
  fi
  fi  # GW_POLICY_UID

  # A real (not check-only) run that reached here without composing anything would be
  # creating the worker with the edge unadmitted -- the one outcome this whole block
  # exists to prevent. Guarded explicitly rather than left to the branches above,
  # because the failure mode of a skipped gate is silence.
  #
  # NO TEST REACHES THIS, and that is stated rather than left implied: on today's code
  # the only way to arrive with ALB_COMPOSED=0 is the ledger-lookup failure above, which
  # already w2_fails unless --check-only. Neutralising this line therefore breaks
  # nothing, which was verified rather than assumed. It is kept as a backstop against a
  # future edit adding a path that skips the composition -- the one defect here whose
  # symptom is a worker that looks created and then times out.
  if [ "$CHECK_ONLY" != 1 ] && [ "$ALB_COMPOSED" != 1 ]; then
    w2_fail "reached the worker stage without deciding the fixture gateway's ALB admission.
     Refusing to create the worker: if the policy does not admit the edge, the bootstrap
     never completes and the run reports the protected worker as having failed -- which is
     the conclusion this fixture exists to establish or refute, so it must not be produced
     by the fixture's own networking."
  fi

  # -------------------------------------------------------------------------
  # the approved digest list, read from the gateway's OWN configuration
  # -------------------------------------------------------------------------
  # AGENT_WORKER_IMAGE_DIGESTS is what the verifying gateway compares the running
  # image against. Read from the Terraform-owned ConfigMap the gateway itself
  # consumes, so the check is made against what the gateway WILL apply rather than
  # against a list maintained here. A second copy would drift, and drift in this
  # particular list means a worker that deploys and then cannot authenticate.
  printf '\n-- approved worker image digests --\n'
  if [ -n "$REVIEWED_WORKER_IMAGE" ]; then
    APPROVED_DIGESTS="${REVIEWED_WORKER_IMAGE##*@}"
    if [ "$CHECK_ONLY" != 1 ]; then
      w2_kubectl get deployment "$GW_NAME" -n "$GW_NS" -o json > "$EVIDENCE_DIR/fixture-gateway-approval.json"
      python3 - "$EVIDENCE_DIR/fixture-gateway-approval.json" "$APPROVED_DIGESTS" "$REVIEWED_GATEWAY_IMAGE" <<'PY_APPROVAL'
import json, sys
pod = json.load(open(sys.argv[1]))["spec"]["template"]["spec"]
container = next(c for c in pod["containers"] if c["name"] == "bedrockgateway")
if container.get("image") != sys.argv[3]:
    sys.exit("fixture gateway does not run the reviewed gateway image")
values = [e.get("value") for e in container.get("env", []) if e["name"] == "AGENT_WORKER_IMAGE_DIGESTS"]
if values != [sys.argv[2]]:
    sys.exit("fixture gateway does not approve exactly the reviewed worker digest")
PY_APPROVAL
    fi
  else
    set +e
    APPROVED_DIGESTS="$(w2_kubectl get configmap adp-worker-authority-config -n "$GW_NS" \
      -o "jsonpath={.data.AGENT_WORKER_IMAGE_DIGESTS}" 2>&1)"; cm_rc=$?
    set -e
    if [ "$cm_rc" -ne 0 ] || [ -z "$APPROVED_DIGESTS" ]; then
      w2_fail "could not read AGENT_WORKER_IMAGE_DIGESTS from configmap
       adp-worker-authority-config in $GW_NS: $(printf '%s' "$APPROVED_DIGESTS" | tr -d '\n' | cut -c1-160)
       This is the list the gateway checks the running worker image against. An empty or
       unreadable list admits NOTHING, so a worker created now would be refused at
       bootstrap; it must not be read as admitting anything. The list is Terraform-owned
       (agent_authority_worker_image_digests in webhook-ingress) and widening it is
       root's change, not this script's."
    fi
  fi
  w2_ok "verified the worker digest approval for this gateway"

  # -------------------------------------------------------------------------
  # the live worker template — copied, never hand-written
  # -------------------------------------------------------------------------
  # Same reasoning as the gateway: hand-listing a pod spec is how the previous
  # revision lost nine secret env references. The ScaledJob's jobTargetRef.template
  # is the reviewed composition, including the projected bootstrap token whose
  # audience the gateway reviews.
  LIVE_WORKER_JSON="$EVIDENCE_DIR/live-worker-template.json"
  set +e
  w2_kubectl get scaledjob agent-scaledjob -n "$AGENT_NS" \
    -o "jsonpath={.spec.jobTargetRef.template}" > "$LIVE_WORKER_JSON" 2>/dev/null
  sj_rc=$?
  set -e
  if [ "$sj_rc" -ne 0 ] || [ ! -s "$LIVE_WORKER_JSON" ]; then
    w2_fail "could not read the live worker pod template from scaledjob/agent-scaledjob in
     $AGENT_NS. The fixture worker is a COPY of the reviewed composition -- that is where
     the projected bootstrap token, its audience and the verification-keys mount come
     from. Hand-assembling a lookalike is the defect this replaces, so there is no
     fallback: without the template there is nothing to copy."
  fi

  # The image comes FROM that template, so the fixture runs the same program the
  # ordinary worker does. Resolved to a digest below and checked against the
  # approved list by the renderer.
  WORKER_IMAGE="$(python3 -c '
import json, sys
tpl = json.load(open(sys.argv[1]))
containers = tpl.get("spec", {}).get("containers", [])
matches = [c for c in containers if c.get("name") == "agent-worker"]
if len(matches) != 1:
    sys.exit("expected exactly one agent-worker container in the live template, "
             f"found {len(matches)}")
print(matches[0].get("image", ""))
' "$LIVE_WORKER_JSON")" || w2_fail "could not read the worker image from the live template"

  # A tag names no particular bytes, and the gateway compares a DIGEST. If the
  # template is tag-referenced, resolve it from what the ordinary worker pods are
  # actually running -- the same reasoning as the gateway digest above.
  if [ -n "$REVIEWED_WORKER_IMAGE" ]; then
    WORKER_IMAGE="$REVIEWED_WORKER_IMAGE"
  fi
  case "$WORKER_IMAGE" in
    *@sha256:*) w2_ok "worker image is digest-pinned" ;;
    *)
      RESOLVED="$(w2_kubectl get pods -n "$AGENT_NS" -l app.kubernetes.io/name=agent-scaledjob \
        -o jsonpath='{range .items[*]}{.status.containerStatuses[?(@.name=="agent-worker")].imageID}{"\n"}{end}' \
        2>/dev/null | sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' | sort -u | head -1 || true)"
      [ -n "$RESOLVED" ] || w2_fail "the live worker template references image $WORKER_IMAGE by TAG,
     and no running ordinary worker pod was available to resolve it to a digest. A tag is
     mutable, so it does not identify the code under test, and the gateway compares a
     digest against its approved list. Refusing to create an unpinned protected worker."
      WORKER_IMAGE="${WORKER_IMAGE%:*}@$RESOLVED"
      w2_ok "resolved the tag-referenced worker image to $RESOLVED"
      ;;
  esac
  w2_note "worker image: $WORKER_IMAGE"

  # -------------------------------------------------------------------------
  # render + create
  # -------------------------------------------------------------------------
  # Re-invoked with the worker arguments so 20-worker.json is produced by the SAME
  # renderer that already asserted the gateway's composition. The renderer refuses
  # every condition the gateway's verifier decides from a spec, before anything is
  # created.
  printf '\n-- rendering the protected worker --\n'
  python3 "$RENDER" \
    --live-deployment "$LIVE_DEPLOY_JSON" \
    --run-id "$RUN_ID" --nonce "$NONCE" \
    --name "$GW_NAME" --namespace "$GW_NS" --agent-namespace "$AGENT_NS" \
    --image "$FIXTURE_IMAGE" --queue-url "$QUEUE_URL" "${FIXTURE_APPROVAL_ARGS[@]}" \
    --policy-name "$POLICY_NAME" --out-dir "$MANIFEST_DIR" \
    --worker-template "$LIVE_WORKER_JSON" \
    --worker-name "$JOB_NAME" \
    --worker-image "$WORKER_IMAGE" \
    --worker-control-endpoint "$WORKER_ENDPOINT" \
    --approved-worker-digests "$APPROVED_DIGESTS" \
    || w2_fail "rendering the protected worker refused (see above). The gateway and its
     policies already exist and are recorded in the ledger; no worker was created.
     Run 90-cleanup-ledger.sh when you are done."

  printf '\n-- creating the protected worker Job --\n'
  if [ "$PREPARE_WORKER_ONLY" != 1 ]; then
    w2_create_each "$MANIFEST_DIR/20-worker.json" 20-worker
  fi

  if [ "$PREPARE_WORKER_ONLY" = 1 ]; then
    WORKER_REASON="prepared only: edge admission and manifest ready; no worker created"
    w2_note "prepared only: verify the edge and enqueue an authenticated task before creating the worker"
  elif [ "$CHECK_ONLY" = 1 ]; then
    WORKER_REASON="check-only: the Job was server-side validated, not created"
    w2_note "check-only: no Job exists, so there is no pod to bind"
  else
    # The Job's server-assigned uid, read back from the API server. It is what the
    # pod is bound to below -- a label match would not establish that this run
    # created the pod, since anything can wear a label.
    WORKER_JOB_UID="$(w2_kubectl get job "$JOB_NAME" -n "$AGENT_NS" \
      -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    [ -n "$WORKER_JOB_UID" ] || w2_fail "created Job $JOB_NAME but could not read its uid back.
     The pod cannot be bound to an unidentified Job, and the Job cannot be safely
     deleted at teardown. Remove it by hand: kubectl delete job $JOB_NAME -n $AGENT_NS"
    w2_ok "worker Job uid=$WORKER_JOB_UID"

    # ---------------------------------------------------------------------
    # wait for the pod, then BIND it
    # ---------------------------------------------------------------------
    # The verifier's remaining conditions are runtime facts -- phase, resolved
    # imageID, podIP, exactly one container status -- so they are observed here
    # rather than asserted from the template. The pod is found via the Job's
    # own label and then CHECKED against the Job uid; the label locates a
    # candidate, the uid is what makes it ours.
    printf '\n-- binding the worker pod (operator-side; the SA cannot read pods) --\n'
    WORKER_POD_NAME=""
    waited=0
    while [ "$waited" -lt "$WORKER_READY_TIMEOUT" ]; do
      CANDIDATE="$(w2_kubectl get pods -n "$AGENT_NS" \
        -l "adp.io/w2-fixture=$RUN_ID" -o jsonpath='{.items[0].metadata.name}' \
        2>/dev/null || true)"
      if [ -n "$CANDIDATE" ]; then
        PHASE="$(w2_kubectl get pod "$CANDIDATE" -n "$AGENT_NS" \
          -o jsonpath='{.status.phase}' 2>/dev/null || true)"
        if [ "$PHASE" = "Running" ]; then
          WORKER_POD_NAME="$CANDIDATE"
          break
        fi
        # A pod that will never run must not be waited out to the timeout: the
        # Job has backoffLimit 0, so there is no second attempt coming.
        case "$PHASE" in
          Failed|Succeeded)
            w2_fail "the worker pod $CANDIDATE reached phase $PHASE without ever running.
     The Job is created with backoffLimit 0 (a retry would be a DIFFERENT pod uid, and
     the identity binding is to one uid), so no replacement is coming. Diagnose with:
       kubectl describe pod $CANDIDATE -n $AGENT_NS
     Then run 90-cleanup-ledger.sh; the Job is recorded and will be removed." ;;
        esac
      fi
      sleep 5
      waited=$((waited + 5))
    done
    [ -n "$WORKER_POD_NAME" ] || w2_fail "no worker pod reached Running within
     ${WORKER_READY_TIMEOUT}s. The Job is recorded in the ledger, so 90-cleanup-ledger.sh
     will remove it. Diagnose with:
       kubectl get pods -n $AGENT_NS -l adp.io/w2-fixture=$RUN_ID
       kubectl describe job $JOB_NAME -n $AGENT_NS"

    WORKER_POD_JSON="$EVIDENCE_DIR/worker-pod.json"
    w2_kubectl get pod "$WORKER_POD_NAME" -n "$AGENT_NS" -o json > "$WORKER_POD_JSON" \
      || w2_fail "could not read pod $WORKER_POD_NAME back for binding"

    # The scoped role the pod will actually assume, read from the service
    # account's own annotation rather than named by hand. The blast radius of an
    # unsupervised run IS this role, so it is resolved, not assumed.
    WORKER_ROLE_ARN="$(w2_kubectl get sa agent-authority-worker-sa -n "$AGENT_NS" \
      -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null || true)"

    EXPECTED_IDENTITY_DOC="$EVIDENCE_DIR/expected-identity.json"
    python3 "$OBSERVE" \
      --pod-json "$WORKER_POD_JSON" \
      --run-id "$RUN_ID" --nonce "$NONCE" \
      --job-uid "$WORKER_JOB_UID" \
      --service-account agent-authority-worker-sa \
      --approved-digests "$APPROVED_DIGESTS" \
      --account-id "$ACCOUNT" --region "$W2_REGION" \
      --aws-role-arn "$WORKER_ROLE_ARN" \
      --ledger "$LEDGER" \
      --control-endpoint "$WORKER_ENDPOINT" \
      --out "$EXPECTED_IDENTITY_DOC" \
      || w2_fail "the worker pod could not be bound (see the refusal above). The Job and
     pod exist and the Job is recorded, so 90-cleanup-ledger.sh will remove them. Nothing
     downstream may use this fixture: without a bound identity there is no reference for
     the experiment to be measured against."

    WORKER_POD_UID="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["expected_identity"]["pod_uid"])
' "$EXPECTED_IDENTITY_DOC")"
    WORKER_DIGEST="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["expected_identity"]["runtime_image_digest"])
' "$EXPECTED_IDENTITY_DOC")"

    # The POD is recorded separately from the Job. Deleting the Job cascades to it,
    # but the ledger is also the record of what was OBSERVED, and the pod uid is
    # what the experiment proves itself against.
    python3 "$OWNERSHIP" record-k8s --ledger "$LEDGER" --run-id "$RUN_ID" \
      --account-id "$ACCOUNT" --kind Pod --name "$WORKER_POD_NAME" \
      --namespace "$AGENT_NS" --uid "$WORKER_POD_UID" \
      || w2_fail "bound pod $WORKER_POD_NAME but could not record it; remove the Job by hand"

    WORKER_CREATED=1
    WORKER_REASON="created and bound"
    w2_ok "worker pod $WORKER_POD_NAME uid=$WORKER_POD_UID bound and recorded"
    w2_note "expected-identity document: $EXPECTED_IDENTITY_DOC"
    w2_note "  The pod proves it is this pod by reading its own projected metadata.uid;"
    w2_note "  the protected SA was never granted pod read access to make this work."
  fi
fi

# ---------------------------------------------------------------------------
# record what was built
# ---------------------------------------------------------------------------
printf '\n== fixture summary ==\n'
# Values are passed as ARGUMENTS, not interpolated into the Python source. Shell
# interpolation into a heredoc is how the unreviewable quoting in the published
# revision arose, and `$(... && echo true)` would inject a bare `true` that
# Python rejects. Arguments also mean a value containing a quote cannot break
# the program.
python3 - \
  "$EVIDENCE_DIR/fixture-created.json" \
  "$RUN_ID" "$ACCOUNT" "$W2_REGION" "$CHECK_ONLY" \
  "$GW_NAME" "$GW_NS" "$FIXTURE_IMAGE" "$GATEWAY_DIGEST" \
  "$POLICY_NAME" "$QUEUE_NAME" "$QUEUE_URL" "$LEDGER" \
  "$WORKER_CREATED" "$WORKER_REASON" "$JOB_NAME" "$AGENT_NS" \
  "$WORKER_JOB_UID" "$WORKER_POD_NAME" "$WORKER_POD_UID" "$WORKER_DIGEST" \
  "$WORKER_ENDPOINT" "$EXPECTED_IDENTITY_DOC" "$STAGE" <<'PY'
import json, sys
(out, run_id, account, region, check_only, gw_name, gw_ns, image, digest,
 policy, queue_name, queue_url, ledger, worker_created, worker_reason, job_name,
 agent_ns, job_uid, pod_name, pod_uid, worker_digest, worker_endpoint,
 identity_doc) = sys.argv[1:24]

# worker_job_created is DERIVED, never a constant. The previous revision hardcoded
# False, which stayed false after the worker became creatable -- a report that
# disagrees with the cluster is worse than no report, because everything downstream
# believes it.
created = worker_created == "1"
worker = {
    "created": created,
    "reason": worker_reason,
    "job_name": job_name,
    "namespace": agent_ns,
}
if created:
    # Identities, not names: a name is reusable, a server-assigned uid is not.
    worker.update({
        "job_uid": job_uid,
        "pod_name": pod_name,
        "pod_uid": pod_uid,
        "runtime_image_digest": worker_digest,
        "control_endpoint": worker_endpoint,
        "expected_identity_document": identity_doc,
        "identity_binding": (
            "the operator read the pod from the API server and bound it to the Job uid via "
            "ownerReferences; the pod proves it is this pod by reading its own projected "
            "metadata.uid. The protected service account was NOT granted pod read access."
        ),
    })

record = {
    "run_id": run_id,
    "account_id": account,
    "region": region,
    "check_only": check_only == "1",
    # WHICH STAGE this report is about. Without it, a gateway-stage report and an
    # all-stage report are byte-identical apart from the worker block -- and a reader
    # cannot tell "no worker was requested" from "the worker stage has not run yet".
    "stage": sys.argv[24],
    "gateway": {"name": gw_name, "namespace": gw_ns, "image": image, "digest": digest},
    "policy_name": policy,
    "queue": {"name": queue_name, "url": queue_url},
    "worker_job_created": created,
    "worker": worker,
    "ledger": ledger,
}
with open(out, "w") as fh:
    json.dump(record, fh, indent=2, sort_keys=True)
print(json.dumps(record, indent=2, sort_keys=True))
PY

w2_ok "fixture created; composition report at $MANIFEST_DIR/composition-report.json"

# ---------------------------------------------------------------------------
# the handoff — what makes the staged sequence executable rather than prose
# ---------------------------------------------------------------------------
if [ "$STAGE" = gateway ] && [ "$CHECK_ONLY" != 1 ]; then
  printf '\n== gateway -> edge handoff ==\n'
  # Written from the LEDGER, not from this process's variables: the nonce, uids and
  # queue url are the values whose only purpose is to bind #5836's run to this one, so
  # the document must describe what was actually recorded. The runbook's version of
  # this asked the operator to transcribe four of them into four more commands.
  HANDOFF="$EVIDENCE_DIR/stage-handoff.json"
  python3 "$STAGE_LIB" handoff --ledger "$LEDGER" --run-id "$RUN_ID" \
    --namespace "$GW_NS" --agent-namespace "$AGENT_NS" \
    --service "$GW_NAME" --deployment "$GW_NAME" \
    --evidence-dir "$EVIDENCE_DIR" --out "$HANDOFF" >/dev/null \
    || w2_fail "the gateway stage created its resources but the handoff document could not
     be written (see above). The fixture IS RUNNING and recorded in $LEDGER; tear it down
     with ./90-cleanup-ledger.sh, or write the next stage's inputs by hand from the ledger."
  w2_ok "handoff written: $HANDOFF"
  w2_note "The fixture gateway is STILL RUNNING -- deliberately: #5836's edge is built in"
  w2_note "  front of its Service, so tearing it down now would make the worker stage"
  w2_note "  impossible. That means a control-flag-ON gateway and a live queue persist"
  w2_note "  until you run 90-cleanup-ledger.sh. The handoff document lists the exact"
  w2_note "  next commands, including the nonce -- read them from it rather than retyping."
else
  w2_note "NEXT: ./15-verify-isolation.sh --run-id $RUN_ID --evidence-dir $EVIDENCE_DIR"
  w2_note "      an applied NetworkPolicy is not isolation proof; probe it."
fi
w2_note "ALWAYS, even on failure: ./90-cleanup-ledger.sh $LEDGER $EVIDENCE_DIR"

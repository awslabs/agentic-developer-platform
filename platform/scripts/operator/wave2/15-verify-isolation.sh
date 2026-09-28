#!/usr/bin/env bash
# Wave 2 step 15 — PROVE the fixture's isolation by probing it.
#
# Issue #3968, root's blocker 2: "an applied NetworkPolicy is not isolation
# proof." Applying a policy and reading it back only proves the API server
# accepted the YAML. This step opens real sockets from inside the fixture pods
# and classifies what came back.
#
# Run this after 10-create-fixture.sh and BEFORE any control experiment. If
# isolation is not proven, the experiment must not run: a control result measured
# through an unverified boundary cannot be attributed to the software.
#
# WHY A NAIVE PROBE IS WORSE THAN NONE
# ------------------------------------
# `nc -z host port || echo isolated` fails in the safe-LOOKING direction. A typo
# in the hostname, an absent target, or a dead listener all produce "could not
# connect", which then gets filed as proof of isolation. lib/probes.py separates
# REFUSED (packet arrived, nothing listening -> NOT blocked) from TIMEOUT
# (silently dropped -> the NetworkPolicy signature), and requires every
# deny-probe to carry a positive control on the same port before a timeout counts
# as a pass. See the module docstring.
#
# This script is a thin wrapper: it discovers pod names and the ordinary-worker
# target, then hands off. All classification logic is in lib/probes.py, where it
# is unit-tested (tests/test_probes.py) without needing a cluster.
#
# READ-ONLY on the cluster: `kubectl get` to discover, `kubectl exec` to probe.
# It creates nothing, mutates nothing, and sends no queue messages.
#
# WHY A TIMEOUT NEEDS A SAME-ENDPOINT CONTROL
# -------------------------------------------
# A deny-probe that times out is only evidence of a policy drop if the SAME
# observed endpoint -- host, port and pod uid -- was reachable from a source the
# policy PERMITS, contemporaneously. Pairing it with a control against a different
# pod on the same port number (which this script used to do) leaves the timeout
# equally consistent with a stale pod IP, an IP recycled to another pod, or an
# ordinary worker that never binds a control listener. Pass --allowed-source-pod to
# supply that control; without it the deny-probe grades inconclusive and isolation
# is NOT reported as proven.
#
# Usage:
#   ./15-verify-isolation.sh --run-id w2-... [--evidence-dir DIR]
#                            [--ordinary-worker-pod NAME]
#                            [--allowed-source-pod NAME]
#                            [--allowed-source-namespace NS]
#
# Exit: 0 isolation proven | 1 refused before probing | 5 not proven.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/session.sh
. "$HERE/lib/session.sh"

readonly GW_NS="${W2_GW_NS:-adp-gateway}"
readonly AGENT_NS="${W2_AGENT_NS:-adp-agents}"
readonly CONTROL_PORT="${W2_CONTROL_PORT:-8770}"

RUN_ID=""
EVIDENCE_DIR=""
ORDINARY_WORKER_POD=""
ALLOWED_SOURCE_POD=""
ALLOWED_SOURCE_NS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)                   RUN_ID="${2:?}"; shift 2 ;;
    --evidence-dir)             EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --ordinary-worker-pod)      ORDINARY_WORKER_POD="${2:?}"; shift 2 ;;
    --allowed-source-pod)       ALLOWED_SOURCE_POD="${2:?}"; shift 2 ;;
    --allowed-source-namespace) ALLOWED_SOURCE_NS="${2:?}"; shift 2 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[ -n "$RUN_ID" ] || w2_fail "--run-id is required"
EVIDENCE_DIR="${EVIDENCE_DIR:-$PWD/w2-evidence-$RUN_ID}"
mkdir -p "$EVIDENCE_DIR"

readonly GW_NAME="w2-fixture-gateway-${RUN_ID#w2-}"

printf '\n== session ==\n'
w2_report_mode
w2_require_account
w2_ok "account $W2_ACCOUNT"
w2_note "identity: $W2_ARN"

KUBECONFIG_PATH="$(w2_kubeconfig)"
export KUBECONFIG="$KUBECONFIG_PATH"

# ---------------------------------------------------------------------------
# discover the fixture gateway pod
# ---------------------------------------------------------------------------
printf '\n== discovering probe endpoints ==\n'
GW_POD="$(w2_kubectl get pods -n "$GW_NS" -l "adp.io/w2-fixture=$RUN_ID" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$GW_POD" ] || w2_fail "no Running fixture gateway pod labelled adp.io/w2-fixture=$RUN_ID in
     $GW_NS. Probing cannot proceed: with no pod to probe FROM, every result would
     be an error, and an all-error run must not be filed as isolation proof.
     Check: kubectl get pods -n $GW_NS -l adp.io/w2-fixture=$RUN_ID"
w2_ok "fixture gateway pod: $GW_POD"

# ---------------------------------------------------------------------------
# the fixture worker, if one exists
# ---------------------------------------------------------------------------
# Present only when the run was created with `10-create-fixture.sh --worker-job`,
# which needs the #5836 fixture-only control endpoint (FIXTURE-ROUTING-CONSTRAINT.md).
# Without it there is no worker, so the allow-probe on 8770 and the ordinary-worker
# deny-probe that depends on it are both omitted, and the summary says so rather
# than reporting a tidy all-pass.
#
# Discovered rather than assumed either way: this script must not infer what the
# creation step did from its own flags, so an absent worker is reported as an
# omitted probe and never as a passed one.
FIXTURE_WORKER_POD="$(w2_kubectl get pods -n "$AGENT_NS" -l "adp.io/w2-fixture=$RUN_ID" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
FIXTURE_WORKER_IP=""
if [ -n "$FIXTURE_WORKER_POD" ]; then
  FIXTURE_WORKER_IP="$(w2_kubectl get pod "$FIXTURE_WORKER_POD" -n "$AGENT_NS" \
    -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
  w2_ok "fixture worker pod: $FIXTURE_WORKER_POD ($FIXTURE_WORKER_IP)"
else
  w2_note "no fixture worker pod: the worker-dependent probes are OMITTED, not passed.
     A run created with --worker-job has one; see FIXTURE-ROUTING-CONSTRAINT.md"
fi

# ---------------------------------------------------------------------------
# an ORDINARY worker, as the deny target
# ---------------------------------------------------------------------------
# The isolation claim that matters: the fixture must not be able to drive an
# ordinary worker's control port. Discovery is READ-ONLY -- we take its pod IP and
# never exec into it, never send it anything.
ORDINARY_WORKER_IP=""
ORDINARY_WORKER_UID=""
if [ -n "$ORDINARY_WORKER_POD" ]; then
  # IP and uid read in ONE call, so they describe the same observation of the same
  # object. Two calls could straddle a pod replacement and pair an IP with the uid
  # of a pod that no longer holds it.
  ORDINARY_WORKER_OBS="$(w2_kubectl get pod "$ORDINARY_WORKER_POD" -n "$AGENT_NS" \
    -o jsonpath='{.status.podIP}{" "}{.metadata.uid}' 2>/dev/null || true)"
  ORDINARY_WORKER_IP="${ORDINARY_WORKER_OBS%% *}"
  ORDINARY_WORKER_UID="${ORDINARY_WORKER_OBS##* }"
  [ -n "$ORDINARY_WORKER_IP" ] \
    || w2_fail "--ordinary-worker-pod $ORDINARY_WORKER_POD has no podIP; it is not a usable
     deny target. Omit the flag rather than probing an address that routes nowhere:
     that would time out and look like isolation."
else
  # Pick a Running ordinary worker that is NOT part of this fixture. The uid is
  # taken from the SAME list response as the IP, for the reason above.
  ORDINARY_WORKER_OBS="$(w2_kubectl get pods -n "$AGENT_NS" -l app=agent-worker \
    --field-selector=status.phase=Running -o json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
run_id = sys.argv[1]
for item in doc.get("items", []):
    meta = item.get("metadata", {})
    if (meta.get("labels") or {}).get("adp.io/w2-fixture") == run_id:
        continue          # never probe our own fixture as the "ordinary" target
    ip = item.get("status", {}).get("podIP")
    uid = meta.get("uid")
    # Both or neither: an IP without a uid cannot be used as a deny target,
    # because the positive control could not be tied to the same pod.
    if ip and uid:
        print(ip, uid)
        break
' "$RUN_ID" || true)"
  if [ -n "$ORDINARY_WORKER_OBS" ]; then
    ORDINARY_WORKER_IP="${ORDINARY_WORKER_OBS%% *}"
    ORDINARY_WORKER_UID="${ORDINARY_WORKER_OBS##* }"
  fi
fi

if [ -n "$ORDINARY_WORKER_IP" ]; then
  w2_ok "ordinary worker deny-target discovered (read-only)"
  w2_note "  target uid observed: ${ORDINARY_WORKER_UID:-<none>}"
else
  w2_note "no ordinary worker available as a deny target"
  w2_note "  The deny half cannot be exercised, so isolation will NOT be reported as"
  w2_note "  proven. That is deliberate: allow-probes alone say nothing about isolation."
fi

# ---------------------------------------------------------------------------
# the positive control for the deny-probe
# ---------------------------------------------------------------------------
# Root's finding: "A negative timeout only means denied if the SAME observed target
# UID/IP/port is reachable from an allowed source."
#
# So the control must reach the ORDINARY worker's control port -- the identical
# endpoint the deny-probe targets -- from a pod the policy PERMITS to do so. The
# fixture gateway is by definition not such a pod, so this cannot be synthesised
# from the fixture.
#
# This script does NOT pick one itself. Exec'ing into an arbitrary ordinary pod
# means running a command inside a workload of unknown ownership, which is not this
# evaluation's to touch. Root passes --allowed-source-pod when a suitable pod is
# known; without it the deny-probe carries no control and grades INCONCLUSIVE, and
# the run reports isolation as NOT proven. An honest "not established" is the point.
if [ -n "$ORDINARY_WORKER_IP" ] && [ -z "$ALLOWED_SOURCE_POD" ]; then
  w2_note "no --allowed-source-pod given, so the deny-probe has NO positive control."
  w2_note "  Its timeout would be equally consistent with a stale pod IP, a recycled"
  w2_note "  IP, or a worker that never binds a control listener -- so it will grade"
  w2_note "  inconclusive and isolation will NOT be reported as proven."
  w2_note "  Pass --allowed-source-pod NAME [--allowed-source-namespace NS] with a pod"
  w2_note "  the policy permits to reach ordinary workers on $CONTROL_PORT."
fi
if [ -n "$ALLOWED_SOURCE_POD" ]; then
  [ -n "$ORDINARY_WORKER_UID" ] || w2_fail "--allowed-source-pod was given but the deny
     target's uid was not observed. The control could then only be matched to the
     target by IP, and pod IPs are recycled -- so a control proving a DIFFERENT pod
     reachable would vouch for this one. Refusing rather than weakening the match."
  w2_ok "positive control source: $ALLOWED_SOURCE_POD (${ALLOWED_SOURCE_NS:-$AGENT_NS})"
fi

# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------
printf '\n== probing ==\n'
GENERATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OUT="$EVIDENCE_DIR/isolation-probes.json"

set +e
python3 "$HERE/lib/probes.py" \
  --run-id "$RUN_ID" \
  --gateway-pod "$GW_POD" \
  --gateway-namespace "$GW_NS" \
  --agent-namespace "$AGENT_NS" \
  --control-port "$CONTROL_PORT" \
  ${FIXTURE_WORKER_POD:+--fixture-worker-pod "$FIXTURE_WORKER_POD"} \
  ${FIXTURE_WORKER_IP:+--fixture-worker-host "$FIXTURE_WORKER_IP"} \
  ${ORDINARY_WORKER_IP:+--ordinary-worker-host "$ORDINARY_WORKER_IP"} \
  ${ORDINARY_WORKER_UID:+--ordinary-worker-uid "$ORDINARY_WORKER_UID"} \
  ${ALLOWED_SOURCE_POD:+--allowed-source-pod "$ALLOWED_SOURCE_POD"} \
  ${ALLOWED_SOURCE_NS:+--allowed-source-namespace "$ALLOWED_SOURCE_NS"} \
  --out "$OUT" \
  --generated-at "$GENERATED_AT"
rc=$?
set -e

printf '\n'
case "$rc" in
  0) w2_ok "isolation proven: every flow behaved as the policy requires"
     w2_note "evidence: $OUT" ;;
  5) printf 'FAIL: isolation NOT proven. See %s\n' "$OUT" >&2
     printf '  Do not run a control experiment against this fixture: a result measured\n' >&2
     printf '  through an unverified boundary cannot be attributed to the software.\n' >&2 ;;
  *) printf 'FAIL: probing could not be completed (rc=%s)\n' "$rc" >&2 ;;
esac
exit "$rc"

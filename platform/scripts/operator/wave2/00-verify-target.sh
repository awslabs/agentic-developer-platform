#!/usr/bin/env bash
# Wave 2 fixture step 0 — verify the target before anything is created.
#
# Issue #3968 / epic #3959. READ-ONLY: this script creates nothing, mutates
# nothing, and prints no credential material.
#
# Why this exists as its own step: the #5195 run was driven by ambient injected
# credentials that resolved to a DIFFERENT account (605440105851 via
# ADP-Agent-adp-embark2) than the evaluation target. `aws sts get-caller-identity`
# in the bare shell is therefore NOT evidence that you are on the fixture account.
#
# WHY THIS WAS REWRITTEN (root's blocker 5)
# -----------------------------------------
# The previous version hard-required `adp-cred` ("the vault connection is the only
# supported credential path") and refused on line 29 if it was absent. Root has
# valid embark1/instance credentials but NO adp-cred binary, so this script -- the
# gate every later step depends on -- could not run at all on the host that has to
# run it. A guard that cannot execute in its intended environment protects nothing.
#
# The account assertion is what actually matters, and it is mode-independent: all
# three credential modes (vault / AWS_PROFILE / ambient env) must resolve to
# 879318057152 or the run refuses. lib/session.sh owns that single check, so the
# #5195 protection is strictly stronger than before -- it now also covers the two
# modes this script previously could not express.
#
# The role-ARN check is likewise relaxed to a RECORDED OBSERVATION rather than a
# gate: `ADP-Agent-adp-embark1` is one of several legitimate ways to reach the
# target account, and hard-failing on the role name rejects a correct credential
# for the right account. The account is the security property; the role is context.
#
# Also removed: the nested single-quoted heredoc (`--exec bash -c '...'"$VAR"'...'`)
# that this file used to interpolate shell variables into an inner script. It
# breaks on any value containing a quote and is unreviewable. lib/session.sh's
# w2_session passes a body without interpolation; callers export what they need.
#
# Usage:
#   ./00-verify-target.sh
# Exit 0 = target verified. Nonzero = do NOT proceed to fixture creation.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/session.sh
. "$HERE/lib/session.sh"

readonly EXPECT_ROLE="${W2_EXPECT_ROLE:-ADP-Agent-adp-embark1}"
readonly TABLE="${W2_TABLE:-adp-dev-webhook-events}"
readonly GW_NS="${W2_GW_NS:-adp-gateway}"
readonly AGENT_NS="${W2_AGENT_NS:-adp-agents}"

printf '== session ==\n'
w2_report_mode

# --- 1. Identity -------------------------------------------------------------
# THE load-bearing check, and it applies to every credential mode. Assigns in this
# shell rather than via `read <<<"$(...)"`: inside a command substitution the
# refusal's `exit 1` would kill only the subshell and the run would carry on with
# an empty account.
w2_require_account
w2_ok "account $W2_ACCOUNT (asserted, all modes)"

# Recorded, not gated -- see the header. The account is the security property.
case "$W2_ARN" in
  *"$EXPECT_ROLE"*) w2_ok "identity $EXPECT_ROLE" ;;
  *)
    w2_ok "identity: $W2_ARN"
    w2_note "NOTE: this is not $EXPECT_ROLE. Recorded, not refused: the account"
    w2_note "  assertion above is the check that matters, and several legitimate"
    w2_note "  credentials reach $W2_EXPECT_ACCOUNT. Confirm this identity is the one"
    w2_note "  you intended before creating anything."
    ;;
esac

# --- 2. Invocation table key schema -----------------------------------------
# The harness refuses any write until this is event_id HASH + arrived_at RANGE
# (exit 3). It is also the reason cleanup can be keyed exactly, on BOTH keys.
printf '\n== invocation table ==\n'
SCHEMA="$(w2_aws dynamodb describe-table --table-name "$TABLE" \
  --query 'Table.KeySchema[].[AttributeName,KeyType]' --output text 2>/dev/null \
  | tr '\n' ';')" \
  || w2_fail "could not describe $TABLE. A table whose schema cannot be read is not a
       table whose schema is correct; do not proceed."
EXPECTED_SCHEMA="$(printf 'event_id\tHASH;arrived_at\tRANGE;')"
[ "$SCHEMA" = "$EXPECTED_SCHEMA" ] \
  || w2_fail "key schema is [$SCHEMA], expected event_id HASH + arrived_at RANGE.
       Cleanup deletes synthetic rows by BOTH keys; a different schema means the
       delete would not be uniquely targeted."
w2_ok "table $TABLE key schema event_id HASH + arrived_at RANGE"

# The table is SHARED and large. Recorded so the W2-07 plan is forced to use
# isolated before/after deltas rather than absolute totals.
ITEMS="$(w2_aws dynamodb describe-table --table-name "$TABLE" \
  --query 'Table.ItemCount' --output text 2>/dev/null || echo unknown)"
w2_ok "table is shared: ~$ITEMS items -- W2-07 MUST assert deltas, never shared totals"

# --- 3. DP-INV-1: ordinary control flag must be OFF -------------------------
printf '\n== DP-INV-1 ==\n'
KUBECONFIG_PATH="$(w2_kubeconfig)"
export KUBECONFIG="$KUBECONFIG_PATH"

GW_JSON="$(w2_kubectl get deploy bedrockgateway -n "$GW_NS" -o json 2>/dev/null)" \
  || w2_fail "could not read deploy/bedrockgateway in $GW_NS. DP-INV-1 cannot be confirmed,
       and an unverifiable invariant must not be assumed intact."

# Passed on stdin, not interpolated into the program text: a deployment JSON can
# contain anything, and substituting it into source would be both fragile and a
# code-execution path.
FLAG="$(printf '%s' "$GW_JSON" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for container in doc["spec"]["template"]["spec"]["containers"]:
    for env in container.get("env", []):
        if env.get("name") == "FEATURE_AGENT_CONTROL_ENABLED":
            # valueFrom (secret/configmap ref) has no inline value. Report it as
            # indirect rather than as absent: "absent" would pass the gate below.
            print(env.get("value") if "value" in env else "valueFrom")
            raise SystemExit
print("absent")
')" || w2_fail "could not parse the gateway deployment"

case "$FLAG" in
  false|absent) w2_ok "ordinary gateway control flag is $FLAG (DP-INV-1 intact)" ;;
  *) w2_fail "ordinary gateway has FEATURE_AGENT_CONTROL_ENABLED=$FLAG. DP-INV-1 requires the
       control flag ON ONLY in the disposable fixture. Do not proceed; do not 'fix'
       this by widening the flag. A valueFrom reference is reported here rather than
       treated as absent, because its resolved value is unknown to this check." ;;
esac

# --- 4. Network policy controller actually enforcing ------------------------
# W1-04/W2-10 need a real connection result, not policy YAML that does not apply.
printf '\n== network policy ==\n'
NPC="$(w2_kubectl get networkpolicy -n "$AGENT_NS" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
[ "${NPC:-0}" -gt 0 ] \
  || w2_fail "no NetworkPolicy objects in $AGENT_NS; cannot claim ingress isolation"
w2_ok "$NPC NetworkPolicy objects present in $AGENT_NS"
w2_note "presence is not enforcement -- 15-verify-isolation.sh probes real sockets"

# --- 5. Ordinary gateway digests: INFORMATIONAL, not a gate ----------------
# W2-01 requires the digests of the revisions UNDER EVALUATION -- the fixture
# gateway and fixture worker, both pinned by digest at creation time by
# 10-create-fixture.sh. The ordinary gateway is a different workload and may be
# mid-rollout for unrelated reasons; gating on it would couple this evaluation to
# every unrelated deploy.
printf '\n== ordinary gateway digests (informational) ==\n'
DIGESTS="$(w2_kubectl get pods -n "$GW_NS" -l app=bedrockgateway \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' 2>/dev/null \
  | grep -o 'sha256:[0-9a-f]*' | sort -u || true)"
if [ -z "$DIGESTS" ]; then
  w2_note "none observed (informational only; the fixture pins its own digest)"
else
  printf '%s\n' "$DIGESTS" | sed 's/^/       /'
  N="$(printf '%s\n' "$DIGESTS" | wc -l | tr -d ' ')"
  if [ "$N" -ne 1 ]; then
    w2_note "NOTE: serving $N digests (mid-rollout). This does NOT block the fixture,"
    w2_note "      which pins its own immutable digest."
  else
    w2_ok "serving one digest (context only)"
  fi
fi

# --- 6. Do-not-touch inventory ---------------------------------------------
# Unknown ownership per the assignment: never reuse, mutate or remove.
printf '\n== do-not-touch inventory ==\n'
FOUND=0
if w2_kubectl get deploy authority-probe-gateway-20260920 -n "$GW_NS" >/dev/null 2>&1; then
  w2_ok "PRE-EXISTING authority-probe-gateway-20260920 -- DO NOT reuse/mutate/delete"
  FOUND=1
fi
if w2_aws sqs get-queue-url --queue-name adp-dev-authority-probe-20260920.fifo \
     >/dev/null 2>&1; then
  w2_ok "PRE-EXISTING adp-dev-authority-probe-20260920.fifo -- DO NOT reuse/purge/delete"
  FOUND=1
fi
[ "$FOUND" -eq 1 ] || w2_note "neither pre-existing probe resource found in this account"

printf '\nTARGET VERIFIED for account %s.\n' "$W2_ACCOUNT"

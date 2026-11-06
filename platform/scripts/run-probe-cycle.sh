#!/usr/bin/env bash
# run-probe-cycle.sh — run one model invocability probe cycle now (#7967).
#
# Delegated chat enforces model policy and the gateway refuses every
# /v1/chat/model/decision without fresh invocability evidence. Only the probe
# records that evidence, and its CronJob (webhook-ingress persona-model-probe.tf,
# cronjob/persona-model-invocability-probe in adp-agents) ticks once a day. This
# helper creates a Job from the CronJob's own template so a fresh or updated
# environment holds evidence before the first chat turn, waits for it (bounded),
# prints the per-slot probe log lines and the cycle summary, and exits:
#   0  the cycle recorded at least one `proven` outcome
#   1  the Job failed, timed out, the gateway refused the cycle for configuration
#      (disabled / zero_slots / zero_budget / platform_account_unconfigured), or
#      every claimed slot ended refused/error (nothing proven)
#   2  usage / prerequisite error (no cluster access, CronJob missing)
#   3  nothing to claim (`no_candidates`: every catalogue model already holds fresh
#      evidence or no verified platform destination is registered;
#      `cycle_budget_exhausted`: today's cycle is spent) — existing evidence stands
#   4  the CronJob is suspended and its ADP_PERSONA_MODEL_PROBE_ENABLED is not true
#      (persona_model_probe_enabled = false in webhook-ingress tfvars)
#
# Env: ENVIRONMENT (default dev), NAMESPACE (adp-agents), CRONJOB
# (persona-model-invocability-probe), TIMEOUT_SECONDS (900), POLL_SECONDS (10),
# KEEP_JOB (true; set false to delete the Job after reading its log).
# Non-interactive; needs kubectl against the target cluster (and python3).
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
NAMESPACE="${NAMESPACE:-adp-agents}"
CRONJOB="${CRONJOB:-persona-model-invocability-probe}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-10}"
KEEP_JOB="${KEEP_JOB:-true}"

usage_error() { echo "run-probe-cycle.sh: $*" >&2; exit 2; }
[[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && [ "$TIMEOUT_SECONDS" -gt 0 ] || usage_error "TIMEOUT_SECONDS must be a positive integer"
[[ "$POLL_SECONDS" =~ ^[0-9]+$ ]] && [ "$POLL_SECONDS" -gt 0 ] || usage_error "POLL_SECONDS must be a positive integer"
[[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || usage_error "NAMESPACE is not a valid Kubernetes name"
[[ "$CRONJOB" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || usage_error "CRONJOB is not a valid Kubernetes name"
command -v kubectl >/dev/null || usage_error "kubectl is required"
command -v python3 >/dev/null || usage_error "python3 is required"

TFVARS_HINT="environments/${ENVIRONMENT}/modules/webhook-ingress.tfvars"
GATEWAY_HINT="environments/${ENVIRONMENT}/modules/gateway.tfvars"

CRONJOB_JSON=$(kubectl -n "$NAMESPACE" get cronjob "$CRONJOB" -o json 2>/dev/null) \
  || usage_error "cronjob/${CRONJOB} not found in namespace ${NAMESPACE}; apply webhook-ingress (persona-model-probe.tf) first"

JOB_NAME="probe-cycle-$(date -u +%Y%m%d%H%M%S)"

# Render a Job from the CronJob's jobTemplate (the same image, service account,
# security context, NetworkPolicy labels and deadline the scheduled tick uses).
# ADP_PERSONA_MODEL_PROBE_ENABLED is the pod's local defence-in-depth switch; it is
# set to true only when the CronJob does not already carry true, and never when
# the CronJob is suspended with it false (that is the operator saying "off").
JOB_MANIFEST=$(printf '%s' "$CRONJOB_JSON" | JOB_NAME="$JOB_NAME" NAMESPACE="$NAMESPACE" CRONJOB="$CRONJOB" \
  TFVARS_HINT="$TFVARS_HINT" python3 -c '
import json, os, sys

cronjob = json.load(sys.stdin)
job_name, namespace, cronjob_name, tfvars_hint = (os.environ[k] for k in ("JOB_NAME", "NAMESPACE", "CRONJOB", "TFVARS_HINT"))
spec = cronjob.get("spec", {})
template = spec.get("jobTemplate", {})
containers = template.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
if not containers:
    sys.exit("run-probe-cycle.sh: cronjob has no containers")
local_switch = next((e.get("value") for e in containers[0].get("env", []) if e.get("name") == "ADP_PERSONA_MODEL_PROBE_ENABLED"), None)
if spec.get("suspend") is True and local_switch != "true":
    print(f"run-probe-cycle.sh: cronjob/{cronjob_name} is suspended and ADP_PERSONA_MODEL_PROBE_ENABLED={local_switch!r}; "
          f"set persona_model_probe_enabled = true in {tfvars_hint} and apply webhook-ingress "
          f"(kubernetes_cron_job_v1.persona_model_probe), then re-run", file=sys.stderr)
    sys.exit(4)
if local_switch != "true":
    env = [e for e in containers[0].get("env", []) if e.get("name") != "ADP_PERSONA_MODEL_PROBE_ENABLED"]
    env.append({"name": "ADP_PERSONA_MODEL_PROBE_ENABLED", "value": "true"})
    containers[0]["env"] = env
labels = dict(template.get("metadata", {}).get("labels", {}))
labels["adp.aws-e.io/probe-trigger"] = "run-probe-cycle"
job = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": job_name,
        "namespace": namespace,
        "labels": labels,
        "annotations": {"cronjob.kubernetes.io/instantiate": "manual", "adp.aws-e.io/source-cronjob": cronjob_name},
    },
    "spec": template.get("spec", {}),
}
job["spec"].setdefault("ttlSecondsAfterFinished", 86400)
print(json.dumps(job))
') || { status=$?; [ "$status" -eq 4 ] && exit 4; usage_error "could not render a Job from cronjob/${CRONJOB}"; }

printf '%s' "$JOB_MANIFEST" | kubectl apply -f - >/dev/null \
  || usage_error "could not create job/${JOB_NAME} in ${NAMESPACE}"
echo "run-probe-cycle.sh: created job/${JOB_NAME} from cronjob/${CRONJOB} in ${NAMESPACE}; waiting up to ${TIMEOUT_SECONDS}s"

cleanup() {
  if [ "$KEEP_JOB" != "true" ]; then
    kubectl -n "$NAMESPACE" delete job "$JOB_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Poll both terminal conditions. `kubectl wait --for=condition=complete` alone
# would sit out the whole timeout on a Job that failed in its first minute.
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
JOB_STATE=pending
while [ "$(date +%s)" -lt "$deadline" ]; do
  JOB_STATE=$(kubectl -n "$NAMESPACE" get job "$JOB_NAME" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    conditions = json.load(sys.stdin).get("status", {}).get("conditions", []) or []
except ValueError:
    conditions = []
state = "pending"
for condition in conditions:
    if condition.get("status") != "True":
        continue
    if condition.get("type") in ("Complete", "SuccessCriteriaMet"):
        state = "complete"
    elif condition.get("type") in ("Failed", "FailureTarget"):
        state = "failed"
print(state)
' || echo pending)
  [ "$JOB_STATE" = pending ] || break
  sleep "$POLL_SECONDS"
done

JOB_LOG=$(kubectl -n "$NAMESPACE" logs "job/${JOB_NAME}" --all-containers=true 2>/dev/null || true)
echo "----- probe log (job/${JOB_NAME}) -----"
# Per-slot harness lines and the final cycle summary; the probe prints the
# summary JSON ({"completed":N,"outcomes":{"proven":..,"refused":..,"error":..},
# "stoppedReason":...}) to stdout and fatal errors as "[invocability-probe] fatal:".
printf '%s\n' "$JOB_LOG" | grep -E '^\[invocability-probe\]|^\{' || echo "(no probe output captured)"
echo "----- end probe log -----"

if [ "$JOB_STATE" = pending ]; then
  echo "run-probe-cycle.sh: job/${JOB_NAME} did not finish within ${TIMEOUT_SECONDS}s (kubectl -n ${NAMESPACE} describe job ${JOB_NAME})" >&2
  exit 1
fi

printf '%s\n' "$JOB_LOG" | JOB_STATE="$JOB_STATE" GATEWAY_HINT="$GATEWAY_HINT" python3 -c '
import json, os, sys

job_state, gateway_hint = os.environ["JOB_STATE"], os.environ["GATEWAY_HINT"]
summary = None
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        candidate = json.loads(line)
    except ValueError:
        continue
    if isinstance(candidate, dict) and isinstance(candidate.get("outcomes"), dict):
        summary = candidate
if job_state != "complete":
    print("run-probe-cycle.sh: probe job failed (see the log above)", file=sys.stderr)
    sys.exit(1)
if summary is None:
    print("run-probe-cycle.sh: probe job completed without a cycle summary; nothing was proven", file=sys.stderr)
    sys.exit(1)
outcomes = summary["outcomes"]
completed = summary.get("completed", 0)
reason = summary.get("stoppedReason", "") or "-"
proven, refused, errors = (outcomes.get(k, 0) for k in ("proven", "refused", "error"))
print(f"run-probe-cycle.sh: cycle summary completed={completed} proven={proven} refused={refused} error={errors} stopped={reason}")
if proven > 0:
    sys.exit(0)
if reason in ("disabled", "zero_slots", "zero_budget", "platform_account_unconfigured"):
    print(f"run-probe-cycle.sh: the gateway refused the cycle ({reason}); set model_probe_enabled, "
          f"model_probe_max_slots_per_cycle, model_probe_budget_usd_per_cycle and model_probe_max_budget_usd_per_attempt "
          f"in {gateway_hint}, apply gateway infra and redeploy the gateway, then re-run", file=sys.stderr)
    sys.exit(1)
if completed == 0 and reason in ("no_candidates", "cycle_budget_exhausted"):
    print(f"run-probe-cycle.sh: nothing to claim ({reason}); existing evidence stands. If chat still answers "
          f"probing_disabled, check that a verified platform Bedrock destination is registered and that the "
          f"evidence TTL (model_probe_evidence_ttl_hours) has not lapsed", file=sys.stderr)
    sys.exit(3)
print(f"run-probe-cycle.sh: {completed} slot(s) claimed but none proven (stopped={reason}); "
      f"inspect the refused/error outcomes above", file=sys.stderr)
sys.exit(1)
'

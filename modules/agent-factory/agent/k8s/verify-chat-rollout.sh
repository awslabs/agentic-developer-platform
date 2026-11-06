#!/usr/bin/env bash
# Independent drift check for the delegated chat rollout (#8058, #8059, #8086). Compares the
# LIVE ScaledJob and pre-pull DaemonSet against the release the caller built and against SSM,
# and asserts that the digests the gateway verifies chat workloads against — SSM
# chat-authority-worker-images / chat-sandbox-image-digests / chat-crew-image-digests and the
# live bedrockgateway-config ADP_CHAT_*_DIGESTS — equal the digests the ScaledJob carries,
# without trusting what deploy-chat-scaledjob.sh resolved or published. Run by
# chat-agent-deploy.yml after the apply; operators can run it by hand (deployment-manifest.md).
#
#   ENVIRONMENT, AWS_REGION, NAMESPACE  as for deploy-chat-scaledjob.sh
#   SUPERVISOR_IMAGE, SANDBOX_IMAGE     the release digests (<registry>/adp-chat-agent@sha256:...)
#   ADP_CREW_DASHBOARD_ORIGIN           optional: expected value when the deploy overrode SSM
#   ADP_CHAT_SANDBOX_START_DEADLINE_MS  optional: expected value when the deploy overrode the default
#   ADP_CHAT_PERSISTENT_EXECUTOR        optional: expected value when the deploy overrode SSM (#8115)
set -euo pipefail
NAMESPACE="${NAMESPACE:-adp-gateway-agents}"
ENVIRONMENT="${ENVIRONMENT:?environment required}"
AWS_REGION="${AWS_REGION:-us-east-1}"
: "${SUPERVISOR_IMAGE:?supervisor release digest required}"
: "${SANDBOX_IMAGE:?sandbox release digest required}"
[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{1,20}$ && "$NAMESPACE" == adp-gateway-agents ]] || { echo 'verify-chat-rollout requires a valid environment name and the adp-gateway-agents namespace' >&2; exit 1; }
[[ "$SUPERVISOR_IMAGE" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/adp-chat-agent@sha256:[a-f0-9]{64}$ ]] || { echo 'SUPERVISOR_IMAGE must be a pinned adp-chat-agent digest' >&2; exit 1; }
[[ "$SANDBOX_IMAGE" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/adp-chat-agent@sha256:[a-f0-9]{64}$ ]] || { echo 'SANDBOX_IMAGE must be a pinned adp-chat-agent digest' >&2; exit 1; }
[[ "$SUPERVISOR_IMAGE" != "$SANDBOX_IMAGE" ]] || { echo 'Supervisor and sandbox digests must differ' >&2; exit 1; }

_normalise() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ "$value" == None ]] && value=""
  printf '%s' "$value"
}
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
EXPECTED_CREW_ORIGIN="$(_normalise "${ADP_CREW_DASHBOARD_ORIGIN:-}")"
CREW_PARAMETER="/adp/${ENVIRONMENT}/gateway/crew-dashboard-origin"
if [[ -n "$EXPECTED_CREW_ORIGIN" ]]; then
  CREW_ORIGIN_SOURCE="env"
elif RAW="$(aws ssm get-parameter --name "$CREW_PARAMETER" --query Parameter.Value --output text --region "$AWS_REGION" 2>"$WORK/ssm.err")"; then
  EXPECTED_CREW_ORIGIN="$(_normalise "$RAW")"
  CREW_ORIGIN_SOURCE="ssm ${CREW_PARAMETER}"
else
  # Only ParameterNotFound is a legitimate "empty" (Crew edge off). A failed read is not an
  # expectation: comparing against it would pass vacuously (dev 2026-10-17, #8058).
  CODE="$(sed -nE 's/.*An error occurred \(([A-Za-z0-9.]+)\).*/\1/p' "$WORK/ssm.err" | head -1)"
  if [[ "$CODE" == ParameterNotFound ]]; then
    EXPECTED_CREW_ORIGIN=""
    CREW_ORIGIN_SOURCE="ssm ${CREW_PARAMETER} (ParameterNotFound)"
  else
    echo "verify-chat-rollout: SSM read of ${CREW_PARAMETER} failed (${CODE:-no AWS error code}); refusing to compare against a failed read: $(tr '\n' ' ' < "$WORK/ssm.err")" >&2
    exit 1
  fi
fi
echo "[verify-chat] expected ADP_CREW_DASHBOARD_ORIGIN from ${CREW_ORIGIN_SOURCE}: ${EXPECTED_CREW_ORIGIN:-<empty>}"

# The published workload identities (#8086). ParameterNotFound is a legitimate read ("never
# published", reported below as drift against the live digest); any other failure is not a
# value to compare against.
read_published_digest() {
  local name="$1" parameter="/adp/${ENVIRONMENT}/gateway/$2" raw code value
  if raw="$(aws ssm get-parameter --name "$parameter" --query Parameter.Value --output text --region "$AWS_REGION" 2>"$WORK/ssm.err")"; then
    value="$(_normalise "$raw")"
    echo "[verify-chat] published ${parameter}: ${value:-<empty>}"
  else
    code="$(sed -nE 's/.*An error occurred \(([A-Za-z0-9.]+)\).*/\1/p' "$WORK/ssm.err" | head -1)"
    if [[ "$code" == ParameterNotFound ]]; then
      value=""
      echo "[verify-chat] published ${parameter} (ParameterNotFound): <empty>"
    else
      echo "verify-chat-rollout: SSM read of ${parameter} failed (${code:-no AWS error code}); refusing to compare against a failed read: $(tr '\n' ' ' < "$WORK/ssm.err")" >&2
      exit 1
    fi
  fi
  printf -v "$name" '%s' "$value"
  export "${name?}"
}
read_published_digest SSM_WORKER_DIGESTS chat-authority-worker-images
read_published_digest SSM_SANDBOX_DIGESTS chat-sandbox-image-digests
read_published_digest SSM_CREW_DIGESTS chat-crew-image-digests

# The persistent-executor default (#8115): env override, else SSM chat-persistent-executor;
# ParameterNotFound or an empty value means the renderer's sandbox-loop. Same fail-closed read.
EXPECTED_EXECUTOR="$(_normalise "${ADP_CHAT_PERSISTENT_EXECUTOR:-}")"
EXECUTOR_PARAMETER="/adp/${ENVIRONMENT}/gateway/chat-persistent-executor"
if [[ -n "$EXPECTED_EXECUTOR" ]]; then
  EXECUTOR_SOURCE="env"
elif RAW="$(aws ssm get-parameter --name "$EXECUTOR_PARAMETER" --query Parameter.Value --output text --region "$AWS_REGION" 2>"$WORK/ssm.err")"; then
  EXPECTED_EXECUTOR="$(_normalise "$RAW")"
  EXECUTOR_SOURCE="ssm ${EXECUTOR_PARAMETER}"
else
  CODE="$(sed -nE 's/.*An error occurred \(([A-Za-z0-9.]+)\).*/\1/p' "$WORK/ssm.err" | head -1)"
  if [[ "$CODE" == ParameterNotFound ]]; then
    EXPECTED_EXECUTOR=""
    EXECUTOR_SOURCE="ssm ${EXECUTOR_PARAMETER} (ParameterNotFound)"
  else
    echo "verify-chat-rollout: SSM read of ${EXECUTOR_PARAMETER} failed (${CODE:-no AWS error code}); refusing to compare against a failed read: $(tr '\n' ' ' < "$WORK/ssm.err")" >&2
    exit 1
  fi
fi
EXPECTED_EXECUTOR="${EXPECTED_EXECUTOR:-sandbox-loop}"
echo "[verify-chat] expected ADP_CHAT_PERSISTENT_EXECUTOR from ${EXECUTOR_SOURCE}: ${EXPECTED_EXECUTOR}"

kubectl get scaledjob chat-agent-worker -n "$NAMESPACE" -o json > "$WORK/scaledjob.json"
kubectl get daemonset chat-agent-image-prepull -n "$NAMESPACE" -o json > "$WORK/daemonset.json"
kubectl get configmap bedrockgateway-config -n adp-gateway -o json > "$WORK/gateway-config.json" \
  || { echo 'verify-chat-rollout: cannot read configmap/bedrockgateway-config in adp-gateway; refusing to assume the gateway converged' >&2; exit 1; }
EXPECTED_CREW_ORIGIN="$EXPECTED_CREW_ORIGIN" EXPECTED_DEADLINE_MS="${ADP_CHAT_SANDBOX_START_DEADLINE_MS:-}" EXPECTED_EXECUTOR="$EXPECTED_EXECUTOR" \
  python3 - "$WORK/scaledjob.json" "$WORK/daemonset.json" "$WORK/gateway-config.json" "$SUPERVISOR_IMAGE" "$SANDBOX_IMAGE" <<'PYCODE'
import json, os, re, sys
from pathlib import Path
scaledjob = json.loads(Path(sys.argv[1]).read_text())
daemonset = json.loads(Path(sys.argv[2]).read_text())
gateway_config = json.loads(Path(sys.argv[3]).read_text()).get("data") or {}
supervisor, sandbox = sys.argv[4:]
drift = []
ok = []

def check(label, live, expected):
    (ok if live == expected else drift).append(f"{label}: live={live!r} expected={expected!r}")

template = scaledjob["spec"]["jobTargetRef"]["template"]["spec"]
container = template["containers"][0]
env = {item.get("name"): item.get("value", "") for item in container.get("env", [])}
check("ScaledJob supervisor image", container.get("image"), supervisor)
check("ScaledJob serviceAccountName", template.get("serviceAccountName"), "adp-chat-supervisor")
check("ScaledJob env ADP_CHAT_SANDBOX_IMAGE", env.get("ADP_CHAT_SANDBOX_IMAGE"), sandbox)
expected_origin = os.environ["EXPECTED_CREW_ORIGIN"]
live_origin = env.get("ADP_CREW_DASHBOARD_ORIGIN", "")
if expected_origin and not live_origin:
    drift.append(f"ScaledJob env ADP_CREW_DASHBOARD_ORIGIN is empty while SSM holds {expected_origin!r} (the rollout blanked the Crew dashboard origin)")
else:
    check("ScaledJob env ADP_CREW_DASHBOARD_ORIGIN", live_origin, expected_origin)
deadline = env.get("ADP_CHAT_SANDBOX_START_DEADLINE_MS", "")
expected_deadline = os.environ["EXPECTED_DEADLINE_MS"]
if expected_deadline:
    check("ScaledJob env ADP_CHAT_SANDBOX_START_DEADLINE_MS", deadline, expected_deadline)
elif re.fullmatch(r"[0-9]{4,6}", deadline) and 5000 <= int(deadline) <= 300000:
    ok.append(f"ScaledJob env ADP_CHAT_SANDBOX_START_DEADLINE_MS: live={deadline!r} within 5000..300000")
else:
    drift.append(f"ScaledJob env ADP_CHAT_SANDBOX_START_DEADLINE_MS: live={deadline!r} expected 5000..300000")
ready = [c for c in scaledjob.get("status", {}).get("conditions", []) if c.get("type") == "Ready"]
# The persistent-executor default the supervisor applies when a session has not chosen (#8115).
check("ScaledJob env ADP_CHAT_PERSISTENT_EXECUTOR", env.get("ADP_CHAT_PERSISTENT_EXECUTOR", ""), os.environ["EXPECTED_EXECUTOR"])
check("ScaledJob Ready", ready[0].get("status") if ready else None, "True")

images = {c.get("name"): c.get("image") for c in daemonset["spec"]["template"]["spec"]["containers"]}
check("DaemonSet container prepull image (supervisor)", images.get("prepull"), supervisor)
check("DaemonSet container prepull-sandbox image (== ScaledJob ADP_CHAT_SANDBOX_IMAGE)", images.get("prepull-sandbox"), env.get("ADP_CHAT_SANDBOX_IMAGE"))
status = daemonset.get("status", {})
desired = status.get("desiredNumberScheduled", 0)
if desired <= 0:
    drift.append("DaemonSet desiredNumberScheduled: live=0 expected >0")
else:
    check("DaemonSet updatedNumberScheduled", status.get("updatedNumberScheduled", 0), desired)
    if status.get("numberReady", 0) != desired:
        print(f"[verify-chat] warning: DaemonSet numberReady={status.get('numberReady', 0)} desired={desired} (nodes still pulling)")

# SSM == ScaledJob == ConfigMap (#8086): the gateway admits sandbox/Crew pods and the
# supervisor only by these digests, so a ScaledJob pinned to digests that SSM or the live
# ConfigMap do not carry refuses every turn ("chat creation image refused").
live_supervisor_digest = (container.get("image") or "").rpartition("@")[2]
live_sandbox_digest = (env.get("ADP_CHAT_SANDBOX_IMAGE") or "").rpartition("@")[2]
for label, published, live in (
    ("SSM chat-authority-worker-images (== ScaledJob supervisor digest)", os.environ["SSM_WORKER_DIGESTS"], live_supervisor_digest),
    ("SSM chat-sandbox-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", os.environ["SSM_SANDBOX_DIGESTS"], live_sandbox_digest),
    ("SSM chat-crew-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", os.environ["SSM_CREW_DIGESTS"], live_sandbox_digest),
    ("ConfigMap bedrockgateway-config ADP_CHAT_WORKER_IMAGE_DIGESTS (== ScaledJob supervisor digest)", gateway_config.get("ADP_CHAT_WORKER_IMAGE_DIGESTS", ""), live_supervisor_digest),
    ("ConfigMap bedrockgateway-config ADP_CHAT_SANDBOX_IMAGE_DIGESTS (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", gateway_config.get("ADP_CHAT_SANDBOX_IMAGE_DIGESTS", ""), live_sandbox_digest),
    ("ConfigMap bedrockgateway-config ADP_CHAT_CREW_IMAGE_DIGESTS (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", gateway_config.get("ADP_CHAT_CREW_IMAGE_DIGESTS", ""), live_sandbox_digest),
):
    if not published:
        drift.append(f"{label}: published=<empty> while the ScaledJob carries {live!r} (the rollout never published the digest; the gateway refuses it)")
    else:
        check(label, published, live)
check("ConfigMap bedrockgateway-config ADP_CHAT_WORKER_SERVICE_ACCOUNT", gateway_config.get("ADP_CHAT_WORKER_SERVICE_ACCOUNT"), template.get("serviceAccountName"))

for line in ok:
    print(f"[verify-chat] ok {line}")
for line in drift:
    print(f"[verify-chat] DRIFT {line}", file=sys.stderr)
if drift:
    sys.exit("verify-chat-rollout: live chat rollout drifts from SSM, the gateway ConfigMap or the release")
print("[verify-chat] live ScaledJob, pre-pull DaemonSet, SSM digests and gateway ConfigMap match the release.")
PYCODE

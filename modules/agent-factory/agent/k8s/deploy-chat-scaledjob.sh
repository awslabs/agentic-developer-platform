#!/usr/bin/env bash
set -euo pipefail

# SSM-backed Crew values (#8058). deploy-all.sh --chat delegated reads these from SSM
# before calling this script; chat-agent-deploy.yml does not, and every ADP-managed
# image deploy used to re-apply the ScaledJob with ADP_CREW_DASHBOARD_ORIGIN blank.
# Both deploy paths reach the same ScaledJob, so the script resolves each value
# itself when the caller leaves the variable unset or empty; a caller-provided value
# wins. `None` (SSM text output for a null value) and blank values mean "not set".
#   ADP_CREW_DASHBOARD_ORIGIN  <- /adp/<env>/gateway/crew-dashboard-origin  (https origin, or empty = Crew edge off)
#   ADP_CREW_S3_INTERFACE_IP   <- /adp/<env>/gateway/crew-s3-interface-ip   (RFC 1918 IPv4, or empty = no S3 egress rule)
#   ADP_CHAT_PERSISTENT_EXECUTOR <- /adp/<env>/gateway/chat-persistent-executor (sandbox-loop | claude-code |
#                                   kirocrew, or empty = sandbox-loop; the deployment default a persistent
#                                   session's pod runs when the session has not chosen one, #8115)
# GATEWAY_HTTPS_ORIGIN is the only other value deploy-all.sh derives from SSM
# (apigw-invoke-url); it stays a required explicit input on both paths (the
# workflow passes vars.GATEWAY_HTTPS_ORIGIN) and is validated by the renderer.
#
# After the ScaledJob apply this script is also the single owner of the release
# identities the gateway verifies chat workloads against (#8086): it publishes the
# supervisor and sandbox digests to SSM and converges the live gateway ConfigMap
# (see publish_chat_value and the "Publish the release identities" block below).
_chat_normalise() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ "$value" == None ]] && value=""
  printf '%s' "$value"
}
# _aws_error_code STDERR_FILE: the code the AWS CLI reports ("An error occurred (<Code>) ...").
_aws_error_code() {
  sed -nE 's/.*An error occurred \(([A-Za-z0-9.]+)\).*/\1/p' "$1" | head -1
}
# resolve_chat_value VARIABLE PARAMETER_SUFFIX: env wins when set; otherwise SSM. Always logs the source.
# Only ParameterNotFound means "empty": the parameter is seeded by gateway Terraform and absent
# only while the Crew feature is off. Any other failure (AccessDeniedException, throttling,
# network) stops the rollout — a value silently treated as empty blanked the Crew dashboard
# origin on the ADP-managed path even though SSM held it (dev 2026-10-17, #8058).
resolve_chat_value() {
  local name="$1" parameter="/adp/${ENVIRONMENT}/gateway/$2" value source raw errors code
  value="$(_chat_normalise "${!name:-}")"
  if [[ -n "$value" ]]; then
    source="env"
  else
    errors="$(mktemp)"
    if raw="$(aws ssm get-parameter --name "$parameter" --query Parameter.Value --output text --region "$AWS_REGION" 2>"$errors")"; then
      value="$(_chat_normalise "$raw")"
      source="ssm ${parameter}"
    else
      code="$(_aws_error_code "$errors")"
      if [[ "$code" == ParameterNotFound ]]; then
        value=""
        source="ssm ${parameter} (ParameterNotFound)"
      else
        echo "SSM read of ${parameter} for ${name} failed (${code:-no AWS error code}); refusing to treat the value as empty: $(tr '\n' ' ' < "$errors")" >&2
        rm -f "$errors"
        exit 1
      fi
    fi
    rm -f "$errors"
  fi
  printf -v "$name" '%s' "$value"
  export "${name?}"
  echo "[deploy-chat] ${name} from ${source}: ${value:-<empty>}"
}
# publish_chat_value PARAMETER_SUFFIX VALUE: put the gateway parameter only when SSM does not
# already hold VALUE (idempotent re-runs publish nothing). The read fails closed like
# resolve_chat_value — only ParameterNotFound means "publish now" (gateway Terraform seeds
# every parameter; it is absent only before the gateway stack exists) — and a failed put
# stops the rollout: a ScaledJob pinned to digests the gateway does not know refuses every
# turn with "chat creation image refused" (dev 2026-10-17, #8086).
publish_chat_value() {
  local parameter="/adp/${ENVIRONMENT}/gateway/$1" value="$2" errors live code
  errors="$(mktemp)"
  if live="$(aws ssm get-parameter --name "$parameter" --query Parameter.Value --output text --region "$AWS_REGION" 2>"$errors")"; then
    live="$(_chat_normalise "$live")"
  else
    code="$(_aws_error_code "$errors")"
    if [[ "$code" != ParameterNotFound ]]; then
      echo "SSM read of ${parameter} failed (${code:-no AWS error code}); refusing to publish over an unknown value: $(tr '\n' ' ' < "$errors")" >&2
      rm -f "$errors"
      exit 1
    fi
    live=""
  fi
  if [[ "$live" == "$value" ]]; then
    echo "[deploy-chat] ${parameter} already holds ${value}"
  elif aws ssm put-parameter --name "$parameter" --type String --value "$value" --overwrite --region "$AWS_REGION" > /dev/null 2>"$errors"; then
    echo "[deploy-chat] published ${parameter}=${value} (was ${live:-<empty>})"
  else
    code="$(_aws_error_code "$errors")"
    echo "SSM publication of ${parameter}=${value} failed (${code:-no AWS error code}); the gateway would keep refusing the chat workloads this rollout pinned: $(tr '\n' ' ' < "$errors")" >&2
    rm -f "$errors"
    exit 1
  fi
  rm -f "$errors"
}
validate_crew_dashboard_origin() {
  [[ -z "$ADP_CREW_DASHBOARD_ORIGIN" || "$ADP_CREW_DASHBOARD_ORIGIN" =~ ^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
    || { echo "Crew dashboard origin must be an https origin with no path, port or query (ADP_CREW_DASHBOARD_ORIGIN / SSM crew-dashboard-origin): ${ADP_CREW_DASHBOARD_ORIGIN}" >&2; exit 1; }
}
# The deployment default for the persistent-session executor (#8115). Empty renders the
# supervisor's built-in sandbox-loop; anything but the three executors stops the rollout
# rather than shipping a ScaledJob every persistent dispatch would refuse.
validate_chat_persistent_executor() {
  [[ -z "$ADP_CHAT_PERSISTENT_EXECUTOR" || "$ADP_CHAT_PERSISTENT_EXECUTOR" =~ ^(sandbox-loop|claude-code|kirocrew)$ ]] \
    || { echo "Persistent chat executor must be sandbox-loop, claude-code or kirocrew (ADP_CHAT_PERSISTENT_EXECUTOR / SSM chat-persistent-executor): ${ADP_CHAT_PERSISTENT_EXECUTOR}" >&2; exit 1; }
}
validate_crew_s3_interface_ip() {
  [[ -n "$ADP_CREW_S3_INTERFACE_IP" ]] || return 0
  python3 - "$ADP_CREW_S3_INTERFACE_IP" <<'PYTHON'
import ipaddress
import sys
try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit('Crew S3 interface IP must be a private IPv4 address')
if address.version != 4 or not (
    address in ipaddress.ip_network('10.0.0.0/8') or
    address in ipaddress.ip_network('172.16.0.0/12') or
    address in ipaddress.ip_network('192.168.0.0/16')
):
    raise SystemExit('Crew S3 interface IP must be a private IPv4 address')
PYTHON
}

if [[ "${ADP_CHAT_CREW_CONTROLS_ONLY:-false}" == true ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ENVIRONMENT="${ENVIRONMENT:?environment required}"
  AWS_REGION="${AWS_REGION:-us-east-1}"
  [[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{1,20}$ ]] || { echo 'Crew controls require a valid environment name' >&2; exit 1; }
  resolve_chat_value ADP_CREW_S3_INTERFACE_IP crew-s3-interface-ip
  validate_crew_s3_interface_ip
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-ebs-storage.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-service-account-admission.yaml"
  kubectl delete networkpolicy chat-crew-helper-s3-egress -n adp-gateway-agents --ignore-not-found
  kubectl delete networkpolicy chat-crew-restore-s3-egress -n adp-gateway-agents --ignore-not-found
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-service-accounts.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-pod-egress.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-pod-dashboard-ingress.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-helper-egress.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-restore-egress.yaml"
  if [[ -n "${ADP_CREW_S3_INTERFACE_IP:-}" ]]; then
    sed "s/REPLACE_WITH_APPROVED_S3_INTERFACE_IP/${ADP_CREW_S3_INTERFACE_IP}/" \
      "${SCRIPT_DIR}/chat-crew-helper-s3-egress.yaml" | kubectl apply -f -
    sed "s/REPLACE_WITH_APPROVED_S3_INTERFACE_IP/${ADP_CREW_S3_INTERFACE_IP}/" \
      "${SCRIPT_DIR}/chat-crew-restore-s3-egress.yaml" | kubectl apply -f -
  fi
  kubectl apply -f "${SCRIPT_DIR}/chat-sandbox-admission.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-pod-admission.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-helper-admission.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-restore-admission.yaml"
  exit 0
fi

NAMESPACE="${NAMESPACE:-adp-gateway-agents}"
ENVIRONMENT="${ENVIRONMENT:?environment required}"
AWS_REGION="${AWS_REGION:-us-east-1}"
# Any ADP environment name may host delegated chat (deploy-all.sh --chat delegated,
# #7899); the namespace stays fixed because every admission, network and RBAC
# manifest below names adp-gateway-agents.
[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{1,20}$ && "$NAMESPACE" == adp-gateway-agents ]] || { echo 'Chat rollout requires a valid environment name and the reviewed adp-gateway-agents namespace' >&2; exit 1; }
[[ "${ADP_CHAT_EXISTING_NAMESPACE_ONLY:-false}" == true ]] || { echo 'Existing namespace is required' >&2; exit 1; }
# ADP_CHAT_MODEL_POLICY_ENABLED is the delegated-chat plumbing switch (model roots,
# /v1/chat/model/decision, sandbox admission), not the evidence posture: whether a
# decision requires invocability evidence is the gateway's BG_CHAT_MODEL_POLICY_POSTURE
# (SSM /adp/<env>/gateway/chat-model-policy-posture; off | advisory | enforced, #7997).
[[ "${ADP_CHAT_MODEL_POLICY_ENABLED:-false}" == true ]] || { echo 'Chat sandbox model policy plumbing must be enabled (ADP_CHAT_MODEL_POLICY_ENABLED=true); the evidence posture is the gateway setting chat_model_policy_posture' >&2; exit 1; }
: "${SUPERVISOR_IMAGE:?distinct supervisor image required}"
: "${SANDBOX_IMAGE:?restricted sandbox image required}"
: "${GATEWAY_HTTPS_ORIGIN:?gateway HTTPS URL required (API Gateway stage URL https://<id>.execute-api.<region>.amazonaws.com/<stage>, or an https origin)}"
[[ "$SUPERVISOR_IMAGE" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/adp-chat-agent@sha256:[a-f0-9]{64}$ ]]
[[ "$SANDBOX_IMAGE" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/adp-chat-agent@sha256:[a-f0-9]{64}$ ]]
[[ "$SUPERVISOR_IMAGE" != "$SANDBOX_IMAGE" ]]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${SCRIPT_DIR}/../../infra"

kubectl get namespace "$NAMESPACE" -o name > /dev/null
bash "${SCRIPT_DIR}/../../../../platform/scripts/enable-bedrock-models.sh" --verify --region "$AWS_REGION"
STATE_BUCKET="${STATE_BUCKET:-adp-terraform-state-$(aws sts get-caller-identity --query Account --output text)}"
resolve_chat_value ADP_CREW_DASHBOARD_ORIGIN crew-dashboard-origin
validate_crew_dashboard_origin
# Remember whether the caller named the executor default: only a caller-provided value is
# published back to SSM after the apply (deploy-all.sh --chat delegated / an operator export);
# the ADP-managed workflow passes none and holds no put on the parameter.
PERSISTENT_EXECUTOR_REQUESTED="$(_chat_normalise "${ADP_CHAT_PERSISTENT_EXECUTOR:-}")"
resolve_chat_value ADP_CHAT_PERSISTENT_EXECUTOR chat-persistent-executor
validate_chat_persistent_executor
# Rendered into the ScaledJob so the value is visible and parity-tested (#8059); the
# renderer bounds it to the launcher's 5 s..300 s.
export ADP_CHAT_SANDBOX_START_DEADLINE_MS="${ADP_CHAT_SANDBOX_START_DEADLINE_MS:-120000}"
echo "[deploy-chat] ADP_CHAT_SANDBOX_START_DEADLINE_MS: ${ADP_CHAT_SANDBOX_START_DEADLINE_MS}"
pushd "$INFRA_DIR" > /dev/null
terraform init -backend-config="../../../environments/${ENVIRONMENT}/modules/agent-factory-backend.tfvars" \
  -backend-config="bucket=${STATE_BUCKET}" -input=false -reconfigure > /dev/null
CHAT_FIFO_URL="$(terraform output -raw chat_tasks_fifo_queue_url)"
SUPERVISOR_ROLE_ARN="$(terraform output -raw chat_supervisor_role_arn)"
SCALER_ROLE_ARN="$(terraform output -raw chat_scaler_role_arn)"
popd > /dev/null
[[ "$SCALER_ROLE_ARN" == "arn:aws:iam::${SUPERVISOR_IMAGE%%.*}:role/adp-${ENVIRONMENT}-chat-worker-role" ]] || { echo 'Unexpected chat queue polling role' >&2; exit 1; }
SERVICE_IP="$(kubectl get service chat-sandbox-gateway -n adp-gateway -o jsonpath='{.spec.clusterIP}')"
[[ "$SERVICE_IP" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9.]+$ ]] || { echo 'Sandbox gateway service is unavailable' >&2; exit 1; }
SANDBOX_CA_CONFIGMAP="$(kubectl get configmaps -n "$NAMESPACE" -o json | python3 -c '
import json, re, sys
matches = [item["metadata"]["name"] for item in json.load(sys.stdin)["items"]
           if re.fullmatch(r"chat-sandbox-gateway-ca-[a-f0-9]{16}", item["metadata"]["name"])
           and item.get("data", {}).get("ca.crt")]
if len(matches) != 1: sys.exit("Expected one published sandbox gateway CA")
print(matches[0])')"
export SUPERVISOR_IMAGE SUPERVISOR_ROLE_ARN AWS_REGION CHAT_FIFO_URL
export SANDBOX_IMAGE_DIGEST="$SANDBOX_IMAGE" GATEWAY_HTTPS_ORIGIN SANDBOX_CA_CONFIGMAP ADP_CREW_DASHBOARD_ORIGIN ADP_CHAT_PERSISTENT_EXECUTOR
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
node "${SCRIPT_DIR}/render-chat-supervisor-job.mjs" > "$WORK/supervisor.yaml"
kubectl create --dry-run=client --validate=false -f "$WORK/supervisor.yaml" -o json > "$WORK/supervisor.json"
python3 - "$WORK/supervisor.json" "$SANDBOX_IMAGE" "$CHAT_FIFO_URL" "$AWS_REGION" "$SCALER_ROLE_ARN" > "$WORK/objects.json" <<'PYCODE'
import json, sys
from pathlib import Path
job = json.loads(Path(sys.argv[1]).read_text())
image, queue, region, scaler_role = sys.argv[2:]
template = {"metadata": {"labels": {"adp.io/chat-sandbox": "true"}},
            "spec": {"serviceAccountName": "adp-chat-sandbox", "containers": [{"image": image}]}}
items = [
    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "chat-crew-pod-template", "namespace": "adp-gateway-agents"},
     "data": {"pod.json": json.dumps(template, sort_keys=True)}},
    {"apiVersion": "keda.sh/v1alpha1", "kind": "TriggerAuthentication", "metadata": {"name": "chat-supervisor-aws-auth", "namespace": "adp-gateway-agents"},
     "spec": {"podIdentity": {"provider": "aws", "identityOwner": "keda", "roleArn": scaler_role}}},
    {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledJob", "metadata": {"name": "chat-agent-worker", "namespace": "adp-gateway-agents"},
     "spec": {"jobTargetRef": {"parallelism": 1, "completions": 1, "backoffLimit": 0, "activeDeadlineSeconds": 900,
                                "template": job["spec"]["template"]},
              "pollingInterval": 1, "minReplicaCount": 0, "maxReplicaCount": 10,
              # scaleOnInFlight false: an in-flight delivery (held by the running supervisor) must not
              # spawn idle supervisors; only visible messages count (#8047).
              "triggers": [{"type": "aws-sqs-queue", "authenticationRef": {"name": "chat-supervisor-aws-auth"},
                            "metadata": {"queueURL": queue, "queueLength": "1", "awsRegion": region, "scaleOnInFlight": "false"}}]}},
]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
PYCODE

if [[ "${ADP_CHAT_VERIFY_EXISTING_CONTROLS:-false}" == true ]]; then
  python3 "${SCRIPT_DIR}/../../../../platform/scripts/verify-chat-release-controls.py" \
    "${SCRIPT_DIR}/chat-supervisor-pod-policy.yaml" \
    "${SCRIPT_DIR}/chat-sandbox-admission.yaml" \
    "${SCRIPT_DIR}/chat-crew-pod-admission.yaml" \
    "${SCRIPT_DIR}/chat-supervisor-rbac.yaml"
else
  kubectl apply -f "${SCRIPT_DIR}/chat-supervisor-pod-policy.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-sandbox-admission.yaml"
  kubectl apply -f "${SCRIPT_DIR}/chat-crew-pod-admission.yaml"
fi
kubectl apply -f "${SCRIPT_DIR}/chat-sandbox-network-policy.yaml"
kubectl apply -f "${SCRIPT_DIR}/chat-sandbox-gateway-egress.yaml"
kubectl apply -f "${SCRIPT_DIR}/chat-crew-pod-dashboard-ingress.yaml"
if [[ "${ADP_CHAT_VERIFY_EXISTING_CONTROLS:-false}" != true ]]; then
  kubectl apply -f "${SCRIPT_DIR}/chat-supervisor-rbac.yaml"
  # The gateway verifies sandbox and Crew pods by reading them (and their Jobs)
  # from the API server before it admits a workload (src/agentauth/workload.py);
  # without this Role every admit answers 404 "not admitted" (dev 2026-10-17).
  kubectl apply -f "${SCRIPT_DIR}/chat-model-rbac.yaml"
fi
# Pre-pull both release digests on every node BEFORE the ScaledJob can schedule a turn
# (#8059): the sandbox image is large, and a cold node pulling the new digest (1 m 49 s
# on dev) ran past the supervisor's start deadline, killing the first turn per node.
# image-prepull-daemonset.yaml is the single source of the pod shape; this script pins
# the supervisor digest, appends the `prepull-sandbox` container with the sandbox
# digest and waits for every node, on both deploy paths. deploy-all.sh does not apply
# the DaemonSet itself in delegated mode.
PREPULL_TIMEOUT="${ADP_CHAT_PREPULL_TIMEOUT:-600s}"
[[ "$PREPULL_TIMEOUT" =~ ^[0-9]{1,4}s$ ]] || { echo 'ADP_CHAT_PREPULL_TIMEOUT must be <seconds>s' >&2; exit 1; }
# Bound for the gateway restart after the ConfigMap converge below (#8086); validated here
# so a bad value stops the rollout before anything is applied.
GATEWAY_ROLLOUT_TIMEOUT="${ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT:-600s}"
[[ "$GATEWAY_ROLLOUT_TIMEOUT" =~ ^[0-9]{1,4}s$ ]] || { echo 'ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT must be <seconds>s' >&2; exit 1; }
sed -e "s|REPLACE_WITH_AGENT_IMAGE|${SUPERVISOR_IMAGE}|g" "${SCRIPT_DIR}/image-prepull-daemonset.yaml" > "$WORK/prepull.yaml"
kubectl create --dry-run=client --validate=false -f "$WORK/prepull.yaml" -o json > "$WORK/prepull-base.json"
python3 - "$WORK/prepull-base.json" "$SUPERVISOR_IMAGE" "$SANDBOX_IMAGE" > "$WORK/prepull.json" <<'PYCODE'
import copy, json, sys
from pathlib import Path
daemonset = json.loads(Path(sys.argv[1]).read_text())
supervisor, sandbox = sys.argv[2:]
if daemonset.get("kind") != "DaemonSet" or daemonset["metadata"]["name"] != "chat-agent-image-prepull":
    sys.exit("image-prepull-daemonset.yaml must render the chat-agent-image-prepull DaemonSet")
containers = daemonset["spec"]["template"]["spec"]["containers"]
if [c["name"] for c in containers] != ["prepull"] or containers[0]["image"] != supervisor:
    sys.exit("image-prepull-daemonset.yaml must declare exactly one pre-pull container pinned to the supervisor digest")
restricted = copy.deepcopy(containers[0])
restricted.update({"name": "prepull-sandbox", "image": sandbox})
containers.append(restricted)
print(json.dumps(daemonset))
PYCODE
kubectl apply -f "$WORK/prepull.json"
kubectl rollout status daemonset/chat-agent-image-prepull -n "$NAMESPACE" --timeout="$PREPULL_TIMEOUT"
PREPULL_IMAGES="$(kubectl get daemonset chat-agent-image-prepull -n "$NAMESPACE" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}={.image}{"\n"}{end}')"
[[ "$PREPULL_IMAGES" == *"prepull=${SUPERVISOR_IMAGE}"* && "$PREPULL_IMAGES" == *"prepull-sandbox=${SANDBOX_IMAGE}"* ]] \
  || { echo 'Pre-pull DaemonSet does not carry the release digests' >&2; exit 1; }
echo '[deploy-chat] chat-agent-image-prepull caches the supervisor and sandbox digests on every node.'
if kubectl get job adp-chat-supervisor-once -n "$NAMESPACE" -o json > "$WORK/existing.json" 2>/dev/null; then
  python3 - "$WORK/existing.json" <<'PYCODE'
import json, sys
job = json.load(open(sys.argv[1]))
if job["spec"].get("suspend") is not True or job.get("status", {}).get("active", 0):
    sys.exit("Refusing to replace an active supervisor job")
PYCODE
  kubectl delete job adp-chat-supervisor-once -n "$NAMESPACE" --wait=true
fi
kubectl apply -f "$WORK/supervisor.yaml"
kubectl apply -f "$WORK/objects.json"
kubectl wait --for=condition=Ready scaledjob/chat-agent-worker -n "$NAMESPACE" --timeout=300s
LIVE_SUPERVISOR="$(kubectl get scaledjob chat-agent-worker -n "$NAMESPACE" -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].image}')"
[[ "$LIVE_SUPERVISOR" == "$SUPERVISOR_IMAGE" ]] || { echo 'Supervisor rollout digest mismatch' >&2; exit 1; }
echo '[deploy-chat] Pinned supervisor and restricted crew template applied; D3 verifies runtime and network enforcement.'
# Publish the release identities the gateway verifies chat workloads against
# (src/agentauth/workload.py): the supervisor by ADP_CHAT_WORKER_IMAGE_DIGESTS +
# ADP_CHAT_WORKER_SERVICE_ACCOUNT, sandbox pods (adp-chat-sandbox) and Crew pods
# (adp-crew-pod) by image digest. The Crew pod template above runs the sandbox image,
# so both get its digest. The parameters are seeded by gateway Terraform (lifecycle
# ignore_changes) and script-owned here, on both deploy paths: chat-agent-deploy.yml
# used to re-pin the ScaledJob without publishing them, so every ADP-managed image
# deploy ended in "chat creation image refused" until an operator published by hand
# (#8086). deploy-all.sh Step 4 renders the same parameters on every later run.
# The ADP-managed identity may put only the three digest parameters; the two
# activation values below are owned by deploy-all.sh --chat delegated and are only
# confirmed here (a differing value stops the rollout at the put).
SUPERVISOR_DIGEST="${SUPERVISOR_IMAGE##*@}"
SANDBOX_DIGEST="${SANDBOX_IMAGE##*@}"
publish_chat_value chat-authority-worker-images "$SUPERVISOR_DIGEST"
publish_chat_value chat-sandbox-image-digests "$SANDBOX_DIGEST"
publish_chat_value chat-crew-image-digests "$SANDBOX_DIGEST"
publish_chat_value chat-worker-service-account adp-chat-supervisor
publish_chat_value chat-data-enabled true
# The persistent-executor default the ScaledJob now carries (#8115): published only when the
# caller named it, so the self-managed path records the operator's choice (dev: claude-code)
# and the ADP-managed path, which passes no value, re-renders what SSM already holds.
if [[ -n "$PERSISTENT_EXECUTOR_REQUESTED" ]]; then
  publish_chat_value chat-persistent-executor "$PERSISTENT_EXECUTOR_REQUESTED"
fi
# The ordinary gateway needs the same values now rather than at the next ConfigMap
# render (parity: modules/gateway/k8s/configmap.yaml placeholders), so the supervisor,
# sandbox and Crew pods this ScaledJob launches are admitted from the first turn. Same
# converge-now pattern as deploy-dev-box.sh step 5: patch only on difference, restart,
# wait. A failed read or patch stops the rollout; it must never be mistaken for "converged".
CHAT_GATEWAY_DESIRED="$(CA_NAME="$SANDBOX_CA_CONFIGMAP" SUPERVISOR_DIGEST="$SUPERVISOR_DIGEST" SANDBOX_DIGEST="$SANDBOX_DIGEST" python3 -c 'import json, os
print(json.dumps({
    "ADP_CHAT_SANDBOX_CA_CONFIGMAP": os.environ["CA_NAME"],
    "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
    "ADP_CHAT_WORKER_IMAGE_DIGESTS": os.environ["SUPERVISOR_DIGEST"],
    "ADP_CHAT_WORKER_SERVICE_ACCOUNT": "adp-chat-supervisor",
    "ADP_CHAT_SANDBOX_IMAGE_DIGESTS": os.environ["SANDBOX_DIGEST"],
    "ADP_CHAT_CREW_IMAGE_DIGESTS": os.environ["SANDBOX_DIGEST"],
    "ADP_CHAT_DATA_ENABLED": "true",
}, sort_keys=True))')"
kubectl get configmap bedrockgateway-config -n adp-gateway -o json > "$WORK/gateway-config.json" \
  || { echo 'Cannot read configmap/bedrockgateway-config in adp-gateway; refusing to leave the gateway on the previous chat digests' >&2; exit 1; }
if CHAT_GATEWAY_DESIRED="$CHAT_GATEWAY_DESIRED" python3 - "$WORK/gateway-config.json" <<'PYCODE'
import json, os, sys
from pathlib import Path
desired = json.loads(os.environ["CHAT_GATEWAY_DESIRED"])
try:
    data = json.loads(Path(sys.argv[1]).read_text()).get("data") or {}
except ValueError:
    sys.exit("configmap/bedrockgateway-config did not read as JSON")
stale = sorted(key for key, value in desired.items() if data.get(key) != value)
if stale:
    print("[deploy-chat] bedrockgateway-config differs on: " + " ".join(stale))
    sys.exit(1)
PYCODE
then
  echo "[deploy-chat] bedrockgateway-config already carries the release digests (supervisor ${SUPERVISOR_DIGEST}, sandbox/Crew ${SANDBOX_DIGEST}); no gateway restart needed."
else
  kubectl patch configmap bedrockgateway-config -n adp-gateway --type merge --patch "{\"data\": ${CHAT_GATEWAY_DESIRED}}" \
    || { echo 'Cannot patch configmap/bedrockgateway-config; the gateway would keep refusing the chat workloads this rollout pinned' >&2; exit 1; }
  kubectl rollout restart deployment/bedrockgateway -n adp-gateway \
    || { echo 'Cannot restart deployment/bedrockgateway after patching its ConfigMap' >&2; exit 1; }
  kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout="$GATEWAY_ROLLOUT_TIMEOUT" \
    || { echo "Gateway rollout did not complete within ${GATEWAY_ROLLOUT_TIMEOUT} after publishing the chat release digests (kubectl -n adp-gateway rollout status deployment/bedrockgateway)" >&2; exit 1; }
  echo "[deploy-chat] bedrockgateway-config patched (supervisor ${SUPERVISOR_DIGEST}, sandbox/Crew ${SANDBOX_DIGEST}, CA ${SANDBOX_CA_CONFIGMAP}) and deployment/bedrockgateway restarted; the gateway admits the release digests."
fi
echo '[deploy-chat] Release digests published to SSM and the gateway ConfigMap; the gateway verifies chat workloads against this rollout.'

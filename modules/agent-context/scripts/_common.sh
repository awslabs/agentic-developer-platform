#!/usr/bin/env bash
# Common helper functions for Agent Context Platform scripts

# Portable envsubst replacement using sed
# Replaces ${VAR_NAME} patterns with exported environment variable values
# Usage: template_file <input_file> | kubectl apply -f -
template_file() {
  local input="$1"
  local result
  result="$(cat "$input")"

  # Extract all ${VAR_NAME} patterns, substitute each
  local vars
  vars=$(echo "$result" | grep -oE '\$\{[A-Z_][A-Z_0-9]*\}' | sort -u || true)

  for pattern in $vars; do
    local varname="${pattern:2:${#pattern}-3}"  # strip ${ and }
    local varval="${!varname:-}"
    # Escape sed special chars in value (delimiter is |, also escape \ and &)
    local escaped_val
    escaped_val=$(printf '%s' "$varval" | sed -e 's/[\\|&]/\\&/g')
    result=$(echo "$result" | sed "s|\${${varname}}|${escaped_val}|g")
  done

  echo "$result"
}

# Source configuration
resolve_ingestion_authority_config() {
  if [[ -z "${INGESTION_RECEIVER_ROLE_ARN:-}" ]]; then
    local parameter_name="/adp/${ENVIRONMENT:-dev}/agent-context/ingestion-receiver-role-arn"
    local lookup_result
    if lookup_result=$(aws ssm get-parameter --name "$parameter_name" \
      --region "${AWS_REGION:-us-east-1}" --query 'Parameter.Value' --output text 2>&1); then
      INGESTION_RECEIVER_ROLE_ARN="$lookup_result"
    else
      case "$lookup_result" in
        *"(AccessDeniedException)"*|*"(AccessDenied)"*)
          echo "ERROR: Access denied reading ${parameter_name}. Grant the deployment identity ssm:GetParameter for this exact parameter; infrastructure presence is unknown." >&2
          ;;
        *"(ParameterNotFound)"*)
          echo "ERROR: Ingestion receiver parameter ${parameter_name} was not found. Apply the reviewed ingestion isolation infrastructure before deploying ingestion workers." >&2
          ;;
        *)
          echo "ERROR: Cannot read ${parameter_name} in ${AWS_REGION:-us-east-1}. Check AWS credentials and SSM availability; infrastructure presence is unknown." >&2
          ;;
      esac
      return 1
    fi
  fi
  if [[ ! "$INGESTION_RECEIVER_ROLE_ARN" =~ ^arn:aws[a-z-]*:iam::[0-9]{12}:role/ ]]; then
    echo "ERROR: Ingestion receiver role is missing or invalid." >&2
    return 1
  fi
  # GATEWAY_CALLBACK_URL is the gateway origin (status_callback.py appends its
  # route). Also accept an explicit callback endpoint during older rollouts.
  local callback_base="${GATEWAY_CALLBACK_URL:-http://bedrockgateway.adp-gateway.svc.cluster.local:8080}"
  callback_base="${callback_base%/}"
  local authority_base="${callback_base%/internal/v1/knowledge-assets/status-callback}/internal/v1/knowledge-assets"
  INGESTION_CONTROL_URL="${INGESTION_CONTROL_URL:-${authority_base}/ingestion-control}"
  INGESTION_REFRESH_URL="${INGESTION_REFRESH_URL:-${authority_base}/refresh}"
  export INGESTION_RECEIVER_ROLE_ARN INGESTION_CONTROL_URL INGESTION_REFRESH_URL
}

verify_ingestion_control_ready() {
  # A queued worker cannot fall back to the old shared role. Require the new
  # gateway image AND its applied infrastructure configuration before cutover.
  kubectl -n adp-gateway rollout status deployment/bedrockgateway --timeout=120s || return 1
  kubectl -n adp-gateway exec deployment/bedrockgateway -- python -c '
import importlib.util, os
assert importlib.util.find_spec("src.internal.ingestion_control_routes"), "Deploy the ingestion-control gateway image first"
assert os.environ.get("INGESTION_ISOLATION_VERSION") == "1", "Apply ingestion infrastructure and restart gateway first"
assert all(os.environ.get(k) for k in ("AGENT_CONTEXT_S3_BUCKET", "AGENT_CONTEXT_VECTORS_BUCKET", "INGESTION_QUEUE_URL", "AGENT_RUN_CREDENTIAL_KEY")), "Ingestion control is not configured"
' || return 1
}

load_config() {
  local root_dir="$1"
  # shellcheck disable=SC1091
  source "${root_dir}/config.env"
  # shellcheck disable=SC1091
  [[ -f "${root_dir}/config.local.env" ]] && source "${root_dir}/config.local.env"
  return 0
}

# Resolve the existing ACL database before rendering the shared ConfigMap.
# Both CLI and Actions deployment paths must supply the same connection settings.
resolve_acl_config() {
  : "${AC_DB_NAME:=agent_context}"
  : "${AC_DB_USERNAME:=agent_context_svc}"
  if [[ -z "${AC_RDS_HOST:-}" || "${AC_RDS_HOST}" == "None" ]]; then
    AC_RDS_HOST=$(aws ssm get-parameter \
      --name "/adp/${ENVIRONMENT:-dev}/rds/endpoint" \
      --region "${AWS_REGION:-us-east-1}" \
      --query 'Parameter.Value' --output text) || {
      echo "ERROR: Cannot resolve the existing ACL database endpoint." >&2
      return 1
    }
  fi
  if [[ -z "$AC_RDS_HOST" || "$AC_RDS_HOST" == "None" || "$AC_RDS_HOST" == "null" ]]; then
    echo "ERROR: ACL database endpoint is empty; refusing an unready deployment." >&2
    return 1
  fi
  export AC_RDS_HOST AC_DB_NAME AC_DB_USERNAME
}

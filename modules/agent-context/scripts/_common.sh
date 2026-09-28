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

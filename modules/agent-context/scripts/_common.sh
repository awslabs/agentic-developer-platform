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

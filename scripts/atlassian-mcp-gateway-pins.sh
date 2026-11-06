#!/usr/bin/env bash
set -euo pipefail

mode="${1:-reuse}"
if [[ "$mode" != "reuse" && "$mode" != "publish" ]]; then
  echo 'Invalid provider-plan mode' >&2
  exit 2
fi
core='aws_iam_role.atlassian_setup_observer["core"]'
adapter='aws_iam_role.atlassian_setup_observer["adapter"]'
core_present=false
adapter_present=false
if terraform state show "$core" >/dev/null 2>&1; then core_present=true; fi
if terraform state show "$adapter" >/dev/null 2>&1; then adapter_present=true; fi
if [[ "$core_present" != "$adapter_present" ]]; then
  echo 'The protected observer roles have incomplete Terraform state; refusing an unreviewed replacement.' >&2
  exit 2
fi
if [[ "${ENABLE_ATLASSIAN_SETUP_OBSERVER_ROLES:-false}" != false && "${ENABLE_ATLASSIAN_SETUP_OBSERVER_ROLES:-false}" != true ]]; then
  echo 'Invalid protected observer role opt-in.' >&2
  exit 2
fi
if [[ "$core_present" == true || "${ENABLE_ATLASSIAN_SETUP_OBSERVER_ROLES:-false}" == true ]]; then
  printf 'TF_VAR_atlassian_mcp_setup_observer_roles_enabled=true\n' >> "$GITHUB_ENV"
fi
if ! terraform state show aws_secretsmanager_secret.atlassian_mcp_config >/dev/null 2>&1; then
  if [[ "$mode" == "publish" ]]; then
    echo 'Apply the reviewed Terraform secret containers first; then run a separate manual gateway apply to publish the provider plan.' >&2
    exit 2
  fi
  echo 'Atlassian MCP secret containers not yet in Terraform state; no pins applied.'
  exit 0
fi

umask 077
gateway_url="${GATEWAY_URL:-}"
if [[ -z "$gateway_url" ]]; then
  domain="$(terraform output -raw frontend_cloudfront_domain_name)"
  if [[ ! "$domain" =~ ^[a-z0-9-]+[.]cloudfront[.]net$ ]]; then
    echo 'The target Terraform state has no valid CloudFront frontend domain; refusing provider plan preparation.' >&2
    exit 2
  fi
  gateway_url="https://$domain"
fi
directory="$RUNNER_TEMP/gateway-atlassian-mcp-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
options=(--use-retained)
if [[ "$mode" == "publish" ]]; then
  options=(--publish)
fi
python3 ../../../scripts/prepare-atlassian-mcp.py \
  --account-id "$ACCOUNT_ID" --region "$AWS_REGION" \
  --environment "$TF_VAR_environment" --gateway-url "$gateway_url" \
  --contract-revision "$GITHUB_SHA" --output-dir "$directory" "${options[@]}"
pins="$directory/atlassian-mcp.tfvars.json"
if [[ -f "$pins" ]]; then
  printf 'ADP_ATLASSIAN_PINS=%s\n' "$pins" >> "$GITHUB_ENV"
fi

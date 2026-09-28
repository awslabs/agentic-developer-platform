#!/usr/bin/env bash
# A missing optional URL may fall back to the domain; denied reads must fail.
set -euo pipefail
read_parameter() {
  local value error_file
  error_file=$(mktemp)
  if value=$(aws ssm get-parameter --name "$1" --query Parameter.Value --output text 2>"$error_file"); then
    rm -f "$error_file"
    [[ "$value" == None ]] || printf '%s' "$value"
  elif grep -q 'ParameterNotFound' "$error_file"; then
    rm -f "$error_file"
  else
    cat "$error_file" >&2
    rm -f "$error_file"
    return 1
  fi
}
URL=${INPUT_CLOUDFRONT_URL:-}
if [[ -z "$URL" ]]; then
  URL=$(read_parameter "/adp/${ENVIRONMENT}/gateway/frontend-url")
  if [[ -z "$URL" ]]; then
    DOMAIN=$(read_parameter "/adp/${ENVIRONMENT}/gateway/cloudfront-domain")
    [[ -z "$DOMAIN" ]] || URL="https://${DOMAIN}"
  fi
fi
if [[ "$URL" != https://* || "$URL" == *$'\n'* || "$URL" == *$'\r'* ]]; then
  echo '::error::Dashboard URL must be a nonempty, single-line HTTPS URL.' >&2
  exit 1
fi
printf 'E2E_CLOUDFRONT_URL=%s\n' "$URL" >> "$GITHUB_ENV"

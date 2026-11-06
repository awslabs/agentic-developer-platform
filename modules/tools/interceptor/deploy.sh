#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" != 1 || "$1" != --interceptor ]]; then
  echo 'Only --interceptor is available; remaining tools stages belong to TLS-C08.' >&2
  exit 2
fi

: "${TOOLS_INTERCEPTOR_FUNCTION_NAME:?Set the reviewed Lambda function name}"
: "${ACCOUNT_ID:?Set the confirmed target account}"
: "${ADP_TOOLS_RELEASE_DIR:?Set the release receipt directory}"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]
[[ "$TOOLS_INTERCEPTOR_FUNCTION_NAME" =~ ^[a-zA-Z0-9_-]{1,64}$ ]]
identity_account=$(aws sts get-caller-identity --query Account --output text)
[[ "$identity_account" == "$ACCOUNT_ID" ]] || { echo 'Deployment identity does not match confirmed account' >&2; exit 1; }

checked_alias_fields() {
  python3 -c '
import json
import sys

snapshot = json.load(sys.stdin)
if (snapshot.get("RoutingConfig") or {}).get("AdditionalVersionWeights"):
    raise SystemExit("Weighted alias cannot represent one published version")
print(snapshot["FunctionVersion"], snapshot["RevisionId"])
'
}

if [[ -n "${ROLLBACK_TO_VERSION:-}" ]]; then
  [[ "$ROLLBACK_TO_VERSION" =~ ^[1-9][0-9]*$ ]] || { echo 'Rollback requires a numbered published version' >&2; exit 2; }
  alias_snapshot=$(aws lambda get-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --output json)
  alias_fields=$(checked_alias_fields <<< "$alias_snapshot")
  read -r current_version revision <<< "$alias_fields"
  [[ "$current_version" != "$ROLLBACK_TO_VERSION" ]] || { echo 'Alias already points to requested version' >&2; exit 2; }
  version=$(aws lambda get-function --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --qualifier "$ROLLBACK_TO_VERSION" --query Configuration.Version --output text)
  [[ "$version" == "$ROLLBACK_TO_VERSION" ]] || { echo 'Rollback version not found' >&2; exit 1; }
  code_sha256=$(aws lambda get-function --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --qualifier "$version" --query Configuration.CodeSha256 --output text)
  [[ "$code_sha256" =~ ^[a-zA-Z0-9+/]{43}=$ ]] || { echo 'Rollback version hash missing' >&2; exit 1; }
  aws lambda update-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --revision-id "$revision" --function-version "$version" >/dev/null
else
  configuration=$(aws lambda get-function-configuration --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --query Environment --output json)
  INTERCEPTOR_CONFIGURATION="$configuration" python3 - "$ACCOUNT_ID" "${AWS_REGION:?Set the confirmed region}" <<'PYCONFIG'
import json
import os
import re
import sys

environment = json.loads(os.environ["INTERCEPTOR_CONFIGURATION"])
if isinstance(environment, dict) and environment.get("Error") is not None:
    raise SystemExit("Interceptor environment cannot be read; check Lambda KMS decrypt authorization for the deployment role")
variables = environment.get("Variables") if isinstance(environment, dict) else None
account, region = sys.argv[1:]
role = variables.get("ADP_TOOLS_FACADE_ROLE_ARN", "") if isinstance(variables, dict) else ""
key = variables.get("ADP_TOOLS_ASSERTION_KEY_ARN", "") if isinstance(variables, dict) else ""
if not re.fullmatch(rf"arn:aws:iam::{account}:role/adp-(dev|prod)-role-tools-facade", role):
    raise SystemExit("Interceptor façade role is not configured in the target account")
if not re.fullmatch(rf"arn:aws:kms:{region}:{account}:key/[a-fA-F0-9-]+", key):
    raise SystemExit("Interceptor assertion verification key is not configured in the target region")
PYCONFIG
  temp_dir=$(mktemp -d)
  archive="$temp_dir/interceptor.zip"
  trap 'rm -f "$archive"; rmdir "$temp_dir"' EXIT
  python3 modules/tools/interceptor/package.py "$archive"
  code_sha256=$(openssl dgst -sha256 -binary "$archive" | openssl base64 -A)
  version=$(aws lambda update-function-code --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --zip-file "fileb://$archive" --publish --query Version --output text)
  [[ "$version" =~ ^[1-9][0-9]*$ ]] || { echo 'Lambda did not publish a numbered version' >&2; exit 1; }
  aws lambda wait function-updated-v2 --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME"
  published_sha256=$(aws lambda get-function --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --qualifier "$version" --query Configuration.CodeSha256 --output text)
  [[ "$published_sha256" == "$code_sha256" ]] || { echo 'Published Lambda does not match the packaged code' >&2; exit 1; }

  if alias_snapshot=$(aws lambda get-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --output json 2>/dev/null); then
    alias_fields=$(checked_alias_fields <<< "$alias_snapshot")
    read -r current_version revision <<< "$alias_fields"
    aws lambda update-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --revision-id "$revision" --function-version "$version" >/dev/null
  else
    aws lambda create-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --function-version "$version" >/dev/null
  fi
fi
alias_snapshot=$(aws lambda get-alias --function-name "$TOOLS_INTERCEPTOR_FUNCTION_NAME" --name live --output json)
alias_fields=$(checked_alias_fields <<< "$alias_snapshot")
read -r actual_version revision <<< "$alias_fields"
[[ "$actual_version" == "$version" ]] || { echo 'Live alias does not point to the published version' >&2; exit 1; }
mkdir -p "$ADP_TOOLS_RELEASE_DIR"
python3 - "$ADP_TOOLS_RELEASE_DIR/release.json" "$version" "$code_sha256" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({"version": sys.argv[2], "code_sha256": sys.argv[3]}, sort_keys=True) + "\n")
PY

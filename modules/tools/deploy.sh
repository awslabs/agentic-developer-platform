#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 || " ${1:-} " == " --help " || " $* " == *" --plan "* || " $* " == *" --resume "* || " $* " == *" --facade "* || " $* " == *" --tool-lambdas "* || " $* " == *" --verify "* || " $* " == *" --bootstrap-ci "* || ( $# -eq 1 && ( "$1" == --infra || "$1" == --targets ) ) ]]; then
  exec bash "$(dirname "$0")/orchestrate.sh" "$@"
fi

if [[ $# -eq 1 && "$1" == --interceptor ]]; then
  exec bash "$(dirname "$0")/interceptor/deploy.sh" "$@"
fi

[[ $# -eq 2 && "$1" == --infra && "$2" == --targets ]] || exit 2
mode=${ADP_TOOLS_MODE:-plan}
[[ "$mode" == plan || "$mode" == apply || "$mode" == drift ]] || exit 2
environment=${ADP_TOOLS_ENVIRONMENT:?Set dev or prod}
[[ "$environment" == dev || "$environment" == prod ]] || exit 2
region=${ADP_TOOLS_AWS_REGION:?Set the approved region}
[[ "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]] || exit 2
expected_account=${ADP_TOOLS_ACCOUNT_ID:?Set the confirmed target account}
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || exit 2
[[ "${ADP_TOOLS_STATE_BUCKET:-}" =~ ^[a-z0-9.-]+$ ]] || exit 2

if [[ "$mode" != plan ]]; then
  [[ "${GITHUB_ACTIONS:-}" == true && "${GITHUB_REF:-}" == refs/heads/main && "${GITHUB_WORKFLOW:-}" == 'Deploy tools platform' ]] || exit 2
  if [[ "$mode" == apply ]]; then
    [[ "${GITHUB_EVENT_NAME:-}" == workflow_dispatch && "${GITHUB_RUN_ATTEMPT:-}" == 1 ]] || exit 2
  else
    [[ "${GITHUB_EVENT_NAME:-}" == schedule || "${GITHUB_EVENT_NAME:-}" == workflow_dispatch ]] || exit 2
  fi
  [[ "${ADP_SOURCE_REVISION:-}" =~ ^[0-9a-f]{40}$ && "${ADP_DEFINITION_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] || exit 2
  [[ "$(git rev-parse HEAD)" == "$ADP_SOURCE_REVISION" ]] || exit 2
  git merge-base --is-ancestor "$ADP_SOURCE_REVISION" refs/remotes/origin/main
  if [[ "$mode" == drift ]]; then
    [[ "$(git rev-parse refs/remotes/origin/main)" == "$ADP_SOURCE_REVISION" ]] || { echo 'Drift check must use latest main revision' >&2; exit 2; }
  fi
fi

identity=$(aws sts get-caller-identity --region "$region" --output json)
[[ "$(jq -r '.Account' <<<"$identity")" == "$expected_account" ]] || { echo 'Deployment account mismatch' >&2; exit 2; }
if [[ "$mode" != plan ]]; then
  expected_role=${ADP_TOOLS_DEPLOY_ROLE_ARN:?Set the environment deployment role}
  [[ "$expected_role" =~ ^arn:aws:iam::${expected_account}:role/adp-[a-z0-9-]+-trusted-deployment$ ]] || exit 2
  role_name=${expected_role##*/}
  [[ "$(jq -r '.Arn' <<<"$identity")" == "arn:aws:sts::${expected_account}:assumed-role/${role_name}/"* ]] || { echo 'Deployment role mismatch' >&2; exit 2; }
fi

umask 077
workdir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/adp-tools.XXXXXX")
trap 'rm -rf "$workdir"' EXIT
if [[ "$mode" == apply ]]; then
  python3 -m modules.tools.platform.scripts.native_release preflight
fi
# Committed non-secret inputs are the base; ADP_TOOLS_TFVARS_JSON is an optional
# overlay for private values and wins on conflict (#7457).
committed="$(cd "$(dirname "$0")/../.." && pwd)/environments/${environment}/modules/tools-platform.tfvars.json"
[[ -f "$committed" ]] || { echo "Missing committed tools inputs: $committed" >&2; exit 2; }
jq -s '.[0] * .[1]' "$committed" <(printf '%s' "${ADP_TOOLS_TFVARS_JSON:-{\}}") > "$workdir/variables.tfvars.json"
python3 -m modules.tools.platform.scripts.knowledge_overlay \
  "$workdir/variables.tfvars.json" "$workdir/knowledge.tfvars.json"
mv "$workdir/knowledge.tfvars.json" "$workdir/variables.tfvars.json"
jq -e --arg account "$expected_account" 'type == "object" and .aws_account_id == $account and (.tools_facade_role_arn | type == "string")' "$workdir/variables.tfvars.json" >/dev/null
jq -e '.interceptor_lambda_arn | type == "string" and length > 0' "$workdir/variables.tfvars.json" >/dev/null || { echo "Tools targets require a configured interceptor before release" >&2; exit 2; }
function_name="adp-tools-${environment}-interceptor"
function_arn="arn:aws:lambda:${region}:${expected_account}:function:${function_name}"
interceptor_arn=$(jq -r '.interceptor_lambda_arn' "$workdir/variables.tfvars.json")
interceptor_role=$(jq -r '.interceptor_execution_role_arn // ""' "$workdir/variables.tfvars.json")
facade_role=$(jq -r '.tools_facade_role_arn' "$workdir/variables.tfvars.json")
assertion_key=$(jq -r '.assertion_signing_key_arn // ""' "$workdir/variables.tfvars.json")
[[ "$interceptor_arn" == "${function_arn}:live" &&
   "$interceptor_role" == "arn:aws:iam::${expected_account}:role/${function_name}" &&
   "$facade_role" == "arn:aws:iam::${expected_account}:role/adp-${environment}-role-tools-facade" &&
   "$assertion_key" =~ ^arn:aws:kms:${region}:${expected_account}:key/[0-9a-f-]+$ ]] || {
  echo 'Tools targets require the reviewed interceptor alias, role and assertion key' >&2
  exit 2
}
python3 "$(dirname "$0")/interceptor/package.py" "$workdir/interceptor.zip"
interceptor_code_sha256=$(openssl dgst -sha256 -binary "$workdir/interceptor.zip" | openssl base64 -A)
verify_interceptor() {
  local snapshot version configuration
  snapshot=$(aws lambda get-alias --region "$region" --function-name "$function_name" --name live --output json)
  version=$(jq -er --arg arn "$interceptor_arn" '
    select(.AliasArn == $arn and ((.RoutingConfig.AdditionalVersionWeights // {}) | length == 0))
    | .FunctionVersion | select(type == "string" and test("^[1-9][0-9]*$"))
  ' <<< "$snapshot") || { echo 'Tools interceptor live alias must point to one numbered version' >&2; return 2; }
  [[ -z "${interceptor_version:-}" || "$version" == "$interceptor_version" ]] || {
    echo 'Tools interceptor alias changed during platform release' >&2; return 2;
  }
  configuration=$(aws lambda get-function --region "$region" --function-name "$function_name" --qualifier "$version" --query Configuration --output json)
  if jq -e '.Environment.Error != null' <<< "$configuration" >/dev/null; then
    echo 'Tools interceptor environment cannot be read; check Lambda KMS decrypt authorization for the deployment role' >&2
    return 2
  fi
  jq -e --arg arn "${function_arn}:${version}" --arg role "$interceptor_role" \
    --arg facade "$facade_role" --arg key "$assertion_key" --arg hash "$interceptor_code_sha256" '
    .FunctionArn == $arn and .Version == ($arn | split(":")[-1]) and
    .CodeSha256 == $hash and .Role == $role and
    .Runtime == "python3.12" and .Handler == "tools_interceptor.handler.handler" and
    .State == "Active" and .LastUpdateStatus == "Successful" and
    .Environment.Variables.ADP_TOOLS_FACADE_ROLE_ARN == $facade and
    .Environment.Variables.ADP_TOOLS_ASSERTION_KEY_ARN == $key
  ' <<< "$configuration" >/dev/null || {
    echo 'Tools interceptor version, hash or verification configuration does not match reviewed source' >&2
    return 2
  }
  interceptor_version=$version
}
verify_interceptor
infra_dir=$(cd "$(dirname "$0")/platform/infra" && pwd)
[[ ! -e "$infra_dir/adp_teardown_target_override.tf.json" ]] || { echo "Remove the tools teardown provider/KMS override before deploying" >&2; exit 2; }
terraform -chdir="$infra_dir" init -input=false \
  -backend-config="bucket=$ADP_TOOLS_STATE_BUCKET" \
  -backend-config="key=tools/platform/${environment}.tfstate" \
  -backend-config="region=$region" -backend-config='encrypt=true' -backend-config='use_lockfile=true'
terraform -chdir="$infra_dir" validate -no-color
plan_options=()
if [[ "$mode" == drift ]]; then
  plan_options=(-detailed-exitcode -lock=false)
fi
plan_status=0
terraform -chdir="$infra_dir" plan -input=false -no-color "${plan_options[@]}" \
  -var-file="$workdir/variables.tfvars.json" -var="environment=$environment" \
  -var="aws_region=$region" -out="$workdir/platform.tfplan" || plan_status=$?
if [[ "$plan_status" -eq 0 || ( "$mode" == drift && "$plan_status" -eq 2 ) ]]; then
  terraform -chdir="$infra_dir" show -json "$workdir/platform.tfplan" > "$workdir/platform-plan.json"
  python3 "$(dirname "$0")/platform/scripts/release_plan.py" "$workdir/platform-plan.json" \
    --account "$expected_account" --region "$region" --environment "$environment" || {
    echo 'Tools release plan changes bootstrap-owned or destructive resources; reconcile state ownership before release' >&2
    exit 2
  }
fi
if [[ "$mode" == drift ]]; then
  if [[ "$plan_status" -eq 2 ]]; then
    changes=$(jq -r '[.resource_changes[]? | select(.change.actions != ["no-op"]) | .address] | if length > 0 then join(", ") else "state differs from main" end' "$workdir/platform-plan.json")
    echo "::error::Tools platform drift: $changes" >&2
    exit 1
  fi
  exit "$plan_status"
fi
[[ "$plan_status" -eq 0 ]] || exit "$plan_status"
[[ "$mode" == apply ]] || exit 0
verify_interceptor

# Terraform output additions/updates also need apply to persist in state, even
# when every managed resource is already unchanged. Use the validated plan.
changes=$(jq '([.resource_changes[]? | select(.change.actions != ["no-op"])] +
  [(.output_changes // {})[] | select(.actions != ["no-op"])]) | length' "$workdir/platform-plan.json")
if [[ "$changes" -eq 0 ]]; then
  echo 'Tools platform Terraform: 0 to add, 0 to change, 0 to destroy'
else
  terraform -chdir="$infra_dir" apply -input=false -no-color "$workdir/platform.tfplan"
fi
verify_interceptor
terraform -chdir="$infra_dir" output -json > "$workdir/outputs.json"
gateway_id=$(jq -er '.gateway_id.value | select(type == "string" and length > 0)' "$workdir/outputs.json")
jq -e '.gateway_arn.value | type == "string" and length > 0' "$workdir/outputs.json" >/dev/null
jq -e '.gateway_url.value | type == "string" and length > 0' "$workdir/outputs.json" >/dev/null
jq -e '.kms_key_arn.value | type == "string" and length > 0' "$workdir/outputs.json" >/dev/null
jq -e --arg caller "$(jq -r '.tools_facade_role_arn' "$workdir/variables.tfvars.json")"   '.caller_role_arn.value == $caller' "$workdir/outputs.json" >/dev/null

ready=false
for attempt in {1..30}; do
  aws bedrock-agentcore-control get-gateway --gateway-identifier "$gateway_id" --region "$region" --output json > "$workdir/gateway.json"
  if jq -e --slurpfile outputs "$workdir/outputs.json" \
    '.status == "READY" and .gatewayId == $outputs[0].gateway_id.value and .gatewayArn == $outputs[0].gateway_arn.value and .gatewayUrl == $outputs[0].gateway_url.value and .kmsKeyArn == $outputs[0].kms_key_arn.value' "$workdir/gateway.json" >/dev/null; then
    ready=true
    break
  fi
  sleep 10
done
[[ "$ready" == true ]] || { echo 'Gateway did not become READY with the planned KMS key and URL' >&2; exit 1; }

target_ids=$(jq -c '.target_ids.value | values | select(type == "object") | [.[]] | select(length > 0 and all(.[]; type == "string" and length > 0)) | sort' "$workdir/outputs.json")
[[ -n "$target_ids" ]] || { echo 'No valid gateway target IDs in Terraform outputs' >&2; exit 1; }
mapfile -t target_list < <(jq -r '.[]' <<<"$target_ids")
# Only DEFAULT MCP-server and OpenAPI targets synchronize their cached schemas.
# DYNAMIC MCP targets discover capabilities on tools/list; Lambda targets also
# become READY without a synchronization. Do not require cached-schema evidence
# from these targets; the authenticated discovery smoke still covers them.
# Record which targets were synchronized so readiness can require a
# synchronization timestamp only where one can exist.
: > "$workdir/synchronized-target-ids"
for target_id in "${target_list[@]}"; do
  aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$gateway_id" \
    --target-id "$target_id" --region "$region" --output json > "$workdir/target.json"
  if jq -e '.targetConfiguration.mcp.lambda? != null or .targetConfiguration.mcp.mcpServer.listingMode? == "DYNAMIC"' "$workdir/target.json" >/dev/null; then
    continue
  fi
  aws bedrock-agentcore-control synchronize-gateway-targets --gateway-identifier "$gateway_id" \
    --target-id-list "$target_id" --region "$region" --output json > "$workdir/sync.json"
  printf '%s\n' "$target_id" >> "$workdir/synchronized-target-ids"
done
synced=false
for attempt in {1..30}; do
  aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gateway_id" --region "$region" --output json > "$workdir/targets.json"
  : > "$workdir/synchronized-targets.jsonl"
  if jq -e --argjson expected "$target_ids" '[.items[]?.targetId] | sort == $expected' "$workdir/targets.json" >/dev/null; then
    synced=true
    for target_id in "${target_list[@]}"; do
      aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$gateway_id" \
        --target-id "$target_id" --region "$region" --output json > "$workdir/target.json"
      if grep -qx "$target_id" "$workdir/synchronized-target-ids"; then
        sync_filter='(.lastSynchronizedAt | type == "string" and length > 0)'
      else
        sync_filter='true'
      fi
      if ! jq -e --arg id "$target_id" --arg arn "$(jq -r '.gateway_arn.value' "$workdir/outputs.json")" \
        ".targetId == \$id and .gatewayArn == \$arn and .status == \"READY\" and $sync_filter" "$workdir/target.json" >/dev/null; then
        synced=false
        break
      fi
      jq -c . "$workdir/target.json" >> "$workdir/synchronized-targets.jsonl"
    done
    [[ "$synced" == false ]] || break
  fi
  sleep 10
done
[[ "$synced" == true ]] || { echo 'Gateway targets did not synchronize and become READY' >&2; exit 1; }

python3 -m modules.tools.platform.scripts.native_release smoke --outputs "$workdir/outputs.json" \
  --targets "$workdir/synchronized-targets.jsonl" > "$workdir/smoke.json"
jq -e '.behavioral_status == "passed" and .cleanup_status == "confirmed" and (.governance_complete | type == "boolean") and (.tools_list_sha256 | test("^[0-9a-f]{64}$")) and (.target_sync_revision | test("^[0-9a-f]{64}$"))' "$workdir/smoke.json" >/dev/null
verify_interceptor

release_dir=${ADP_TOOLS_RELEASE_DIR:?Set the release artifact directory}
mkdir -p "$release_dir"
jq -n --slurpfile outputs "$workdir/outputs.json" --slurpfile smoke "$workdir/smoke.json" \
  --arg interceptor_arn "$interceptor_arn" --arg interceptor_version "$interceptor_version" \
  --arg interceptor_code_sha256 "$interceptor_code_sha256" \
  --arg environment "$environment" --arg source_revision "$ADP_SOURCE_REVISION" \
  --arg definition_revision "$ADP_DEFINITION_REVISION" --arg correlation "${ADP_CORRELATION:-}" \
  '{component:"tools-platform",qualification_status:"smoke_passed_evidence_pending",environment:$environment,source_revision:$source_revision,definition_revision:$definition_revision,adp_correlation:$correlation,interceptor_lambda_arn:$interceptor_arn,interceptor_version:$interceptor_version,interceptor_code_sha256:$interceptor_code_sha256,gateway_id:$outputs[0].gateway_id.value,gateway_arn:$outputs[0].gateway_arn.value,gateway_url:$outputs[0].gateway_url.value,kms_key_arn:$outputs[0].kms_key_arn.value,caller_role_arn:$outputs[0].caller_role_arn.value,target_role_arn:($outputs[0].target_role_arn.value // ""),targets:$outputs[0].target_ids.value,assertion_signing_key_arn:($outputs[0].assertion_signing_key_arn.value // ""),execution_table_name:($outputs[0].execution_table_name.value // ""),shared_tools_lambda_arn:($outputs[0].shared_tools_lambda_arn.value // ""),target_sync_revision:$smoke[0].target_sync_revision,tools_list_sha256:$smoke[0].tools_list_sha256} + (if $smoke[0].qualification_mode == "operational" then {operational_smoke:$smoke[0]} else {native_smoke:$smoke[0]} end)' > "$release_dir/release.json"

for field in gateway_id gateway_arn gateway_url kms_key_arn caller_role_arn target_role_arn targets assertion_signing_key_arn execution_table_name shared_tools_lambda_arn; do
  if [[ "$field" == targets ]]; then
    value=$(jq -c '.targets' "$release_dir/release.json")
  else
    value=$(jq -r --arg field "$field" '.[$field]' "$release_dir/release.json")
  fi
  [[ -n "$value" && "$value" != null ]] || continue
  aws ssm put-parameter --name "/adp/${environment}/tools/${field//_/-}" \
    --value "$value" --type String --overwrite --region "$region" >/dev/null
done
aws ssm put-parameter --name "/adp/${environment}/tools/release" \
  --value "$(jq -c . "$release_dir/release.json")" --type String --overwrite --region "$region" >/dev/null

#!/usr/bin/env bash
# Tools façade release stage (TLS-S10 / #7513): publish the reviewed source as an
# immutable image through the tools-owned CodeBuild project, then roll the façade
# Deployment in the gateway namespace from the platform release parameters.
# Invoked only by `modules/tools/deploy.sh --facade` under the protected tools
# deployment identity; every input is an environment variable the workflow sets.
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
environment=${ADP_TOOLS_ENVIRONMENT:?Set dev or prod}
region=${ADP_TOOLS_AWS_REGION:?Set the approved region}
account=${ADP_TOOLS_ACCOUNT_ID:?Set the confirmed target account}
cluster=${ADP_TOOLS_EKS_CLUSTER_NAME:?Set the EKS cluster}
namespace=${ADP_TOOLS_NAMESPACE:-adp-gateway}
bucket=${ADP_TOOLS_STATE_BUCKET:?Set the source staging bucket}
source_sha=${ADP_SOURCE_REVISION:?Set the reviewed source revision}
[[ "$environment" =~ ^(dev|prod)$ && "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ && "$account" =~ ^[0-9]{12}$ ]] || exit 2
[[ "$cluster" =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,99}$ && "$namespace" == adp-gateway && "$bucket" =~ ^[a-z0-9.-]{3,63}$ ]] || exit 2
[[ "$source_sha" =~ ^[0-9a-f]{40}$ && "$(git -C "$root" rev-parse HEAD)" == "$source_sha" ]] || exit 2

repository=adp-tools-facade
project="adp-tools-${environment}-facade-build"
registry="${account}.dkr.ecr.${region}.amazonaws.com"
umask 077
workdir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/adp-tools-facade.XXXXXX")
trap 'rm -rf "$workdir"' EXIT

export KUBECONFIG="$workdir/kubeconfig"
aws eks update-kubeconfig --region "$region" --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null
for object in deployment/tools-facade service/tools-facade \
  serviceaccount/tools-facade serviceaccount/tools-facade-reconciler \
  cronjob/tools-facade-reconciler; do
  kubectl get "$object" -n "$namespace" -o name >/dev/null || { echo "Bootstrap must pre-create $object in $namespace" >&2; exit 2; }
done

# Runtime configuration comes from the platform bootstrap and the gateway.
# The shared Lambda identity is fixed by the owning stack. Its SSM release output
# is published by the later targets stage, so it cannot be a façade prerequisite.
param() { aws ssm get-parameter --region "$region" --name "$1" --query Parameter.Value --output text; }
gateway_url=$(param "/adp/${environment}/tools/gateway-url")
assertion_key=$(param "/adp/${environment}/tools/assertion-signing-key-arn")
execution_table=$(param "/adp/${environment}/tools/execution-table-name")
facade_role=$(param "/adp/${environment}/tools/caller-role-arn")
reconciler_key=$(param "/adp/${environment}/tools/reconciler-signing-key-arn")
cleanup_lambda="arn:aws:lambda:${region}:${account}:function:adp-${environment}-shared-tools-shared"
provider_role="arn:aws:iam::${account}:role/adp-${environment}-shared-tools-shared-lambda"
reconciler_role="arn:aws:iam::${account}:role/adp-${environment}-role-tools-reconciler"
auth_base=$(param "/adp/${environment}/gateway/apigw-invoke-url")
[[ "$gateway_url" =~ ^https://[a-z0-9.-]+\.gateway\.bedrock-agentcore\.${region}\.amazonaws\.com/mcp$ ]] || { echo 'Unexpected gateway URL' >&2; exit 1; }
[[ "$assertion_key" =~ ^arn:aws:kms:${region}:${account}:key/[0-9a-f-]+$ ]] || { echo 'Missing assertion signing key' >&2; exit 1; }
[[ "$execution_table" =~ ^adp-tools-${environment}-executions$ ]] || { echo 'Unexpected execution table' >&2; exit 1; }
[[ "$facade_role" == "arn:aws:iam::${account}:role/adp-${environment}-role-tools-facade" ]] || { echo 'Unexpected façade role' >&2; exit 1; }
[[ "$reconciler_key" =~ ^arn:aws:kms:${region}:${account}:key/[0-9a-f-]+$ &&
   "$reconciler_role" != "$facade_role" ]] || { echo 'Cleanup identity or provider binding unavailable' >&2; exit 1; }
verify_cleanup_provider() {
  provider_configuration=$(aws lambda get-function-configuration --function-name "$cleanup_lambda" --region "$region" --output json)
  jq -e '.Environment.Error == null and (.Environment.Variables | type == "object")' \
    <<< "$provider_configuration" >/dev/null || { echo 'Cleanup provider environment cannot be read; verify Lambda KMS decrypt authorization' >&2; exit 1; }
  jq -e --arg function "$cleanup_lambda" --arg provider_role "$provider_role" \
    --arg key "$reconciler_key" --arg role "$reconciler_role" '
    .FunctionArn == $function and .Role == $provider_role and
    .State == "Active" and .LastUpdateStatus == "Successful" and
    .Environment.Variables.ADP_TOOLS_RECONCILER_KEY_ARN == $key and
    .Environment.Variables.ADP_TOOLS_RECONCILER_ROLE_ARN == $role
  ' <<< "$provider_configuration" >/dev/null || { echo 'Cleanup provider release is not ready' >&2; exit 1; }
}
verify_cleanup_provider
[[ "$auth_base" =~ ^https://[a-z0-9]+\.execute-api\.${region}\.amazonaws\.com/[a-z0-9]+$ ]] || { echo 'Unexpected gateway invoke URL' >&2; exit 1; }
auth_url="${auth_base}/internal/v1/tools/authorize"

image_digest() {
  aws ecr describe-images --region "$region" --repository-name "$repository" \
    --image-ids "imageTag=$source_sha" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true
}

# Image: an immutable tag per reviewed source; rebuilding an existing tag is refused
# by the repository, so an existing digest is reused as-is.
digest=$(image_digest)
if [[ -n "${ADP_TOOLS_FACADE_ROLLBACK_IMAGE:-}" ]]; then
  [[ "$ADP_TOOLS_FACADE_ROLLBACK_IMAGE" =~ ^${registry}/${repository}@sha256:[0-9a-f]{64}$ ]] || exit 2
  image=$ADP_TOOLS_FACADE_ROLLBACK_IMAGE
elif [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Reusing published façade image $registry/$repository@$digest"
  image="$registry/$repository@$digest"
else
  key="codebuild/src/${project}/${source_sha}-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}.zip"
  git -C "$root" archive --format=zip -o "$workdir/source.zip" "$source_sha"
  aws s3 cp --region "$region" --only-show-errors "$workdir/source.zip" "s3://${bucket}/${key}"
  build_id=$(aws codebuild start-build --region "$region" --project-name "$project" \
    --source-location-override "${bucket}/${key}" --source-type-override S3 \
    --environment-variables-override "name=ADP_SOURCE_SHA,value=${source_sha},type=PLAINTEXT" \
    --query 'build.id' --output text)
  [[ -n "$build_id" && "$build_id" != None ]] || { echo 'Façade build did not start' >&2; exit 1; }
  echo "Façade build $build_id started"
  for _ in $(seq 1 120); do
    status=$(aws codebuild batch-get-builds --region "$region" --ids "$build_id" --query 'builds[0].buildStatus' --output text)
    case "$status" in
      SUCCEEDED) break ;;
      FAILED|FAULT|STOPPED|TIMED_OUT) echo "Façade build $build_id ended $status" >&2; exit 1 ;;
      *) sleep 15 ;;
    esac
  done
  [[ "${status:-}" == SUCCEEDED ]] || { echo 'Façade build did not finish in time' >&2; exit 1; }
  digest=$(image_digest)
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Published façade image has no digest' >&2; exit 1; }
  image="$registry/$repository@$digest"
fi

mkdir -p "$workdir/k8s"
for manifest in serviceaccount reconciler-serviceaccount service deployment reconciler-cronjob; do
  sed -e "s|REPLACE_WITH_TOOLS_FACADE_IMAGE|${image}|g" \
      -e "s|__AWS_REGION__|${region}|g" \
      -e "s|__TOOLS_FACADE_IRSA_ROLE_ARN__|${facade_role}|g" \
      -e "s|__TOOLS_RECONCILER_IRSA_ROLE_ARN__|${reconciler_role}|g" \
      -e "s|__TOOLS_RECONCILER_SIGNING_KEY_ARN__|${reconciler_key}|g" \
      -e "s|__TOOLS_CLEANUP_LAMBDA_ARN__|${cleanup_lambda}|g" \
      -e "s|__TOOLS_ACCOUNT_ID__|${account}|g" \
      -e "s|__TOOLS_ENVIRONMENT__|${environment}|g" \
      -e "s|__TOOLS_INTERNAL_AUTH_URL__|${auth_url}|g" \
      -e "s|__TOOLS_NATIVE_CLEANUP_URL__|${auth_base}/internal/v1/tools/native-cleanup|g" \
      -e "s|__TOOLS_OUTCOME_REPAIR_URL__|${auth_base}/internal/v1/tools/outcome-repair|g" \
      -e "s|__TOOLS_ATLASSIAN_READBACK_URL__|${auth_base}/internal/v1/tools/atlassian/reconcile|g" \
      -e "s|__TOOLS_AGENTCORE_URL__|${gateway_url}|g" \
      -e "s|__TOOLS_ASSERTION_KEY_ARN__|${assertion_key}|g" \
      -e "s|__TOOLS_EXECUTION_TABLE__|${execution_table}|g" \
      "$root/modules/tools/facade/k8s/${manifest}.yaml" > "$workdir/k8s/${manifest}.yaml"
  grep -q "__[A-Z_]*__\|REPLACE_WITH_" "$workdir/k8s/${manifest}.yaml" && { echo "Unrendered placeholder in ${manifest}.yaml" >&2; exit 1; }
done
# Recheck after a potentially long image build before changing the workload.
verify_cleanup_provider
kubectl apply -n "$namespace" -f "$workdir/k8s/"
kubectl rollout status deployment/tools-facade -n "$namespace" --timeout=180s
echo "Rolled tools façade to $image in $cluster/$namespace"

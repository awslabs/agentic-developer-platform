#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
stages=(infra tool-lambdas interceptor facade targets verify)
selected=()
plan=false
resume=false
for argument in "$@"; do
  case "$argument" in
    --infra|--tool-lambdas|--interceptor|--facade|--targets|--verify)
      selected+=("${argument#--}") ;;
    --plan) plan=true ;;
    --resume) resume=true ;;
    --bootstrap-ci)
      echo 'CI bootstrap requires a platform administrator and reviewed IAM policy; no policy is bundled' >&2
      exit 2 ;;
    --help)
      echo 'Usage: modules/tools/deploy.sh [--infra] [--tool-lambdas] [--interceptor] [--facade] [--targets] [--verify] [--plan] [--resume]'
      echo 'Default: all stages; --plan checks prerequisites and makes no cloud changes.'
      exit 0 ;;
    *) echo "Unknown tools deploy option: $argument" >&2; exit 2 ;;
  esac
done
if [[ ${#selected[@]} -eq 0 ]]; then selected=("${stages[@]}"); fi
python3 "$root/modules/tools/scripts/release_journal.py" prerequisites "$plan" "${selected[@]}"
: "${ADP_TOOLS_ACCOUNT_ID:?Set the confirmed target account}"
: "${ADP_TOOLS_AWS_REGION:?Set the approved region}"
: "${ADP_TOOLS_ENVIRONMENT:?Set dev or prod}"
[[ "$ADP_TOOLS_ACCOUNT_ID" =~ ^[0-9]{12}$ && "$ADP_TOOLS_AWS_REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ && "$ADP_TOOLS_ENVIRONMENT" =~ ^(dev|prod)$ ]] || exit 2
account=$(aws sts get-caller-identity --region "$ADP_TOOLS_AWS_REGION" --query Account --output text)
[[ "$account" == "$ADP_TOOLS_ACCOUNT_ID" ]] || { echo 'Tools deployment account mismatch' >&2; exit 2; }
if [[ "$plan" == false ]]; then
  [[ "${GITHUB_ACTIONS:-}" == true && "${GITHUB_REF:-}" == refs/heads/main ]] || { echo 'Apply requires a protected main-branch tools workflow' >&2; exit 2; }
  [[ "${GITHUB_WORKFLOW:-}" == 'Deploy tools platform' || "${GITHUB_WORKFLOW:-}" == 'Deploy tools façade' || "${GITHUB_WORKFLOW:-}" == 'Deploy tools interceptor' ]] || exit 2
  [[ "${ADP_SOURCE_REVISION:-}" =~ ^[0-9a-f]{40}$ && "${ADP_DEFINITION_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] || exit 2
  [[ "$(git -C "$root" rev-parse HEAD)" == "$ADP_SOURCE_REVISION" ]] || exit 2
  git -C "$root" merge-base --is-ancestor "$ADP_SOURCE_REVISION" refs/remotes/origin/main
  role=${ADP_TOOLS_DEPLOY_ROLE_ARN:?Set the protected deployment role}
  [[ "$role" =~ ^arn:aws:iam::${account}:role/adp-[a-z0-9-]+-trusted-deployment$ ]] || exit 2
  arn=$(aws sts get-caller-identity --region "$ADP_TOOLS_AWS_REGION" --query Arn --output text)
  [[ "$arn" == "arn:aws:sts::${account}:assumed-role/${role##*/}/"* ]] || { echo 'Tools deployment role mismatch' >&2; exit 2; }
fi

# All prerequisite checks precede every mutating stage, including on resume.
for stage in "${selected[@]}"; do
  case "$stage" in
    infra|targets)
      [[ -n "${ADP_TOOLS_STATE_BUCKET:-}" && -n "${ADP_TOOLS_TFVARS_JSON:-}" ]] || { echo 'Missing tools Terraform backend or variables' >&2; exit 2; } ;;
    facade|verify)
      [[ -n "${ADP_TOOLS_EKS_CLUSTER_NAME:-}" ]] || { echo 'Missing EKS cluster for tools façade' >&2; exit 2; }
      command -v kubectl >/dev/null || { echo 'Missing kubectl' >&2; exit 2; }
      # The stage writes its own kubeconfig for the named cluster; a pre-existing
      # context is not required on a fresh runner.
      aws ssm get-parameter --name "/adp/${ADP_TOOLS_ENVIRONMENT}/gateway/apigw-invoke-url" --region "$ADP_TOOLS_AWS_REGION" --query Parameter.Value --output text >/dev/null || { echo 'Missing gateway invoke URL SSM parameter' >&2; exit 2; } ;;
  esac
  if [[ "$stage" == facade ]]; then
    [[ -n "${ADP_TOOLS_STATE_BUCKET:-}" ]] || { echo 'Missing tools source staging bucket for the façade build' >&2; exit 2; }
    aws ecr describe-repositories --repository-names adp-tools-facade --region "$ADP_TOOLS_AWS_REGION" >/dev/null || { echo 'Missing tools façade ECR repository' >&2; exit 2; }
    [[ -f "$root/modules/tools/facade/Dockerfile" ]] || { echo 'Missing tools façade Dockerfile (TLS-S10)' >&2; exit 2; }
  fi
done

if [[ "$plan" == false ]]; then
  for requested in "${selected[@]}"; do
    if [[ "$requested" == tool-lambdas ]]; then
      [[ -f "${ADP_SHARED_TOOLS_RELEASE_RECEIPT_FILE:-}" ]] || { echo 'Coordinator: run protected shared-agentcore-tools-deploy.yml with mode=apply and supply its independent runtime receipt' >&2; exit 2; }
      python3 "$root/modules/tools/scripts/verify_release.py" shared "$ADP_SHARED_TOOLS_RELEASE_RECEIPT_FILE" "$ADP_SOURCE_REVISION" "$ADP_TOOLS_ENVIRONMENT" "$account"
    fi
  done
fi

if [[ "$plan" == true ]]; then
  for stage in "${stages[@]}"; do
    for requested in "${selected[@]}"; do
      [[ "$stage" != "$requested" ]] || echo "Plan: $stage"
    done
  done
  if [[ -n "${ADP_TOOLS_RELEASE_BASE_SHA:-}" ]]; then
    [[ "${ADP_SOURCE_REVISION:-}" =~ ^[a-f0-9]{40}$ ]] || { echo 'Reviewed tools source required for release scope' >&2; exit 2; }
    python3 "$root/modules/tools/scripts/release_scope.py" --base "$ADP_TOOLS_RELEASE_BASE_SHA" --source "$ADP_SOURCE_REVISION"
  fi
  if [[ -n "${ADP_TOOLS_RELEASE_MANIFEST_FILE:-}${ADP_TOOLS_RELEASE_RECEIPTS_FILE:-}" ]]; then
    [[ -n "${ADP_TOOLS_RELEASE_BASE_SHA:-}" && -f "${ADP_TOOLS_RELEASE_MANIFEST_FILE:-}" &&
       -f "${ADP_TOOLS_RELEASE_RECEIPTS_FILE:-}" && -n "${ADP_TOOLS_REPOSITORY:-${GITHUB_REPOSITORY:-}}" ]] || {
      echo 'Coordinator: supply the reviewed base, private component manifest, runtime receipts and repository before reconciliation' >&2
      exit 2
    }
    python3 "$root/modules/tools/scripts/reconcile_release.py" --repo "${ADP_TOOLS_REPOSITORY:-$GITHUB_REPOSITORY}" \
      --base "$ADP_TOOLS_RELEASE_BASE_SHA" --source "$ADP_SOURCE_REVISION" \
      --manifest "$ADP_TOOLS_RELEASE_MANIFEST_FILE" --receipts "$ADP_TOOLS_RELEASE_RECEIPTS_FILE" \
      --environment "$ADP_TOOLS_ENVIRONMENT" --account "$account"
  fi
  exit 0
fi

umask 077
# The tools stage ledger is private to this module. It must not share a path with
# the platform deployment agent's `.adp-deploy-state.json`, which is tracked at the
# repository root with a different schema and would be read as a foreign target.
state=${ADP_TOOLS_DEPLOY_STATE_FILE:-"$root/.adp-tools-deploy-state.json"}
exec {state_lock}>"${state}.lock"
flock -n "$state_lock" || { echo "Another tools deployment holds this state lock" >&2; exit 2; }
source_sha=$ADP_SOURCE_REVISION
python3 "$root/modules/tools/scripts/release_journal.py" init "$state" "$account" \
  "$ADP_TOOLS_AWS_REGION" "$ADP_TOOLS_ENVIRONMENT" "$source_sha" "$resume"
for stage in "${stages[@]}"; do
  wanted=false
  for requested in "${selected[@]}"; do [[ "$stage" != "$requested" ]] || wanted=true; done
  [[ "$wanted" == true ]] || continue
  stage_action=$(python3 "$root/modules/tools/scripts/release_journal.py" decision "$state" "$stage" "$resume" \
    "${ADP_TOOLS_STAGE_RECEIPTS_DIR:-}")
  if [[ "$stage_action" == skip ]]; then
    echo "Resuming: $stage independently verified"
    continue
  fi
  python3 "$root/modules/tools/scripts/release_journal.py" start "$state" "$stage" \
    "${GITHUB_RUN_ID:-}" "${GITHUB_RUN_ATTEMPT:-}"
  case "$stage" in
    infra)
      if [[ " ${selected[*]} " == *" targets "* ]]; then
        ADP_TOOLS_TFVARS_JSON=$(jq -c '.targets = []' <<< "${ADP_TOOLS_TFVARS_JSON:-{\}}") \
          bash "$root/modules/tools/deploy.sh" --infra --targets
      else
        bash "$root/modules/tools/deploy.sh" --infra --targets
      fi ;;
    targets) bash "$root/modules/tools/deploy.sh" --infra --targets ;;
    interceptor) bash "$root/modules/tools/deploy.sh" --interceptor ;;
    tool-lambdas) python3 "$root/modules/tools/scripts/verify_release.py" shared "$ADP_SHARED_TOOLS_RELEASE_RECEIPT_FILE" "$source_sha" "$ADP_TOOLS_ENVIRONMENT" "$account" ;;
    facade) bash "$root/modules/tools/facade/deploy.sh" ;;
    verify)
      [[ -f "${ADP_TOOLS_EVAL_RECEIPT_FILE:-}" ]] || { echo 'Coordinator: run protected tools-live-evaluation.yml and supply SCN-8068-04 evidence' >&2; exit 2; }
      python3 "$root/modules/tools/scripts/verify_release.py" evaluation "$ADP_TOOLS_EVAL_RECEIPT_FILE" "$source_sha" "$ADP_TOOLS_ENVIRONMENT" "$account" ;;
  esac
  python3 "$root/modules/tools/scripts/release_journal.py" finish "$state" "$stage"
done

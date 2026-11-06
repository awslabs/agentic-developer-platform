#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

run_case() {
  local label="$1" expected_status="$2" expected_text="$3"
  shift 3
  local status=0
  mkdir -p "$scratch/$label"
  EVAL_WORKDIR="$scratch/$label" bash "$script_dir/run-eval.sh" --environment dev --dry-run "$@" > "$scratch/$label/output" 2>&1 || status=$?
  if [ "$expected_status" = success ]; then
    [ "$status" -eq 0 ] || { cat "$scratch/$label/output"; return 1; }
  else
    [ "$status" -ne 0 ] || { echo "Unexpected green eval: $label"; return 1; }
  fi
  grep -Fq "$expected_text" "$scratch/$label/output" || { cat "$scratch/$label/output"; return 1; }
  printf 'PASS %s\n' "$label"
}

run_case control success 'eval passed: 0 failures'
for number in 0 1 2 3 4 5 6 7 8 9; do
  grep -Fq "Phase $number —" "$scratch/control/output"
done
grep -Fq 'client: reconstructed add input JSON and executed the local tool' "$scratch/control/output"
grep -Fq 'Bedrock: final answer contains local add result' "$scratch/control/output"
jq -e '.id == "toolu_fixture_add" and .result == 5' < <(python3 "$script_dir/conversation.py" tool "$script_dir/fixtures/tool.stream.sse") >/dev/null
python3 "$script_dir/conversation.py" request global.anthropic.claude-sonnet-4-6 true toolu_fixture_add 7 \
  | jq -e '.messages[-1].content[0].content == "7"' >/dev/null
grep -Fq '[SKIP] AWS identity, deployed image digest and gateway preflight need dev' "$scratch/control/output"
grep -Fq '[SKIP] CLI: deployed installer, smoke-user refresh and setup --check require dev' "$scratch/control/output"
run_case missing_delta failure 'client: incomplete input_json_delta' --inject-failure missing-tool-delta
run_case wrong_id failure 'client: wrong tool id in tool_result' --inject-failure wrong-tool-id
run_case denied failure 'gateway: disallowed control model returned 200 instead of 403' --inject-failure denied-model
run_case refresh failure 'CLI: refresh after token-cache invalidation returned 401' --inject-failure refresh-401
run_case leftover failure 'CLI: cleanup-leftover: clean-room pod still present' --inject-failure cleanup-leftover
EVAL_TEST_WRONG_USAGE=true run_case wrong_usage failure 'usage: request IDs attributed to a different smoke user or org'
EVAL_TEST_COUNT_MODE=estimate run_case estimated success 'count_tokens mode estimate; direct comparison unavailable'
for case_name in missing_delta wrong_id denied refresh leftover wrong_usage; do
  [ "$(grep -c '^\[FAIL\]' "$scratch/$case_name/output")" -eq 1 ] || {
    echo "Expected exactly one layer-named FAIL in $case_name" >&2; exit 1
  }
done

GITHUB_STEP_SUMMARY="$scratch/safe-summary.md" run_case summary success 'eval passed: 0 failures'
grep -Fq 'tool_use.id stable across stream and tool_result: true' "$scratch/safe-summary.md"
grep -Fq 'final reply contains deterministic tool result: true (stream=true and stream=false)' "$scratch/safe-summary.md"
grep -Fq 'count_tokens mode: exact; gateway == direct: true' "$scratch/safe-summary.md"
grep -Fq 'Usage attribution: fixture: 4 inference rows attributed to smoke user and org; cleanup: clean' "$scratch/safe-summary.md"
if grep -Ei '(eyJ[A-Za-z0-9_-]{20,}|Use add to calculate|Count this sample|refresh_token)' "$scratch/safe-summary.md" "$scratch/summary/output"; then
  echo 'Token or prompt leaked to summary or logs' >&2; exit 1
fi
printf 'PASS summary names checked layers without logging tokens or prompts\n'

cp "$scratch/control/results.tsv" "$scratch/control-results-before.tsv"
EVAL_WORKDIR="$scratch/control" bash "$script_dir/run-eval.sh" --dry-run --cleanup-only > "$scratch/cleanup-only.log" 2>&1
grep -Fq 'Phase 9 —' "$scratch/cleanup-only.log"
if grep -Fq 'Phase 0 —' "$scratch/cleanup-only.log"; then
  echo 'Cleanup-only entered a live phase' >&2; exit 1
fi
cmp -s "$scratch/control/results.tsv" "$scratch/control-results-before.tsv"
printf 'PASS cleanup-only is standalone and preserves evaluation evidence\n'

mkdir -p "$scratch/dirty-home/.codex"
status=0
env -i PATH="$PATH" HOME="$scratch/dirty-home" EVAL_WORKDIR="$scratch/dirty" \
  EVAL_SKIP_CLI_PATH_CHECK=true EVAL_FORBIDDEN_PORTS=1 \
  bash "$script_dir/run-eval.sh" --assert-clean-room > "$scratch/dirty.log" 2>&1 || status=$?
[ "$status" -ne 0 ]
grep -Fq 'contaminated:' "$scratch/dirty.log"
printf 'PASS contaminated clean room rejected by shared gate\n'

status=0
env -i PATH="$PATH" HOME="$scratch/dirty-home" AWS_ACCESS_KEY_ID=fixture \
  bash "$script_dir/run-eval.sh" --assert-clean-room > "$scratch/aws.log" 2>&1 || status=$?
[ "$status" -ne 0 ]
grep -Fq 'inherited credential variable AWS_ACCESS_KEY_ID' "$scratch/aws.log"
printf 'PASS inherited AWS credentials rejected before running\n'

EVAL_TEST_CONTAMINATED=true run_case contaminated failure 'CLI: clean-room contamination: inherited AWS or ARC credentials' --phases 1

workflow="$script_dir/../../../.github/workflows/eval-claude-desktop-gateway.yml"
command -v yq >/dev/null
yq -e '.jobs.lint.if == "github.event_name == '\''pull_request'\''"' "$workflow" >/dev/null
yq -e '.jobs.eval.if == "github.event_name == '\''workflow_dispatch'\'' && github.ref == '\''refs/heads/main'\''"' "$workflow" >/dev/null
if yq -r '.jobs.lint.steps[].uses // ""' "$workflow" | grep -Fq trusted-deployment; then
  echo 'PR lint job is not credential-free' >&2; exit 1
fi
yq -r '.jobs.lint.steps[].name' "$workflow" | grep -Fxq 'Dry-run tests only (no AWS or Kubernetes)'
yq -e '.jobs.eval.permissions."id-token" == "write" and .jobs.lint.permissions."id-token" == null' "$workflow" >/dev/null
yq -e '.jobs.eval."timeout-minutes" == 60 and .jobs.eval.environment == "adp-gateway-deploy-dev"' "$workflow" >/dev/null
printf 'PASS PR workflow is lint/dry-run-only; live job requires trusted dispatch\n'

inputs="$(yq -r '.on.workflow_dispatch.inputs | keys | sort | join(",")' "$workflow")"
[ "$inputs" = adp_correlation,adp_definition_revision,adp_source_revision ]
if grep -Fq adp_approved_revision "$workflow"; then
  echo 'Unexpected approval input in dispatch workflow' >&2; exit 1
fi
artifact="$(yq -r '.jobs.eval.steps[] | select(.name == "Upload engine dispatch context") | .with.name' "$workflow")"
# shellcheck disable=SC2016
[ "$artifact" = 'adp-deployment-context-eval-claude-desktop-gateway.yml-${{ github.run_attempt }}' ]
# shellcheck disable=SC2016
[ "$(yq -r '.jobs.eval.steps[] | select(.name == "Upload engine dispatch context") | .with.path' "$workflow")" = '${{ runner.temp }}/desktop-eval-context/deployment-context.json' ]
printf 'PASS engine dispatch accepts only three transport inputs and uploads one context file\n'

revision="$(git rev-parse HEAD)"
context_args=(PATH="$PATH" GITHUB_REF=refs/heads/main GITHUB_SHA="$revision" GITHUB_WORKFLOW_SHA="$revision"
  GITHUB_REPOSITORY_ID=123 GITHUB_RUN_ID=456 GITHUB_RUN_ATTEMPT=1
  EXPECTED_DEFINITION_REVISION="$revision" EXPECTED_SOURCE_REVISION="$revision"
  ADP_CORRELATION=fixture EVAL_CONTEXT_ACCOUNT=123456789012 EVAL_EXPECTED_ACCOUNT=123456789012 AWS_REGION=us-east-1)
env -i "${context_args[@]}" python3 "$script_dir/context.py" "$scratch/deployment-context.json"
jq -e --arg revision "$revision" '
  (keys | sort) == (["schema_version", "repository_id", "run_id", "run_attempt", "workflow_path",
    "workflow_revision", "source_revision", "account_id", "region", "resource_kind", "resource_id",
    "inputs", "correlation"] | sort) and
  .schema_version == 1 and .repository_id == 123 and .run_id == 456 and .run_attempt == 1 and
  .workflow_path == ".github/workflows/eval-claude-desktop-gateway.yml" and
  .workflow_revision == $revision and .source_revision == $revision and
  .account_id == "123456789012" and .region == "us-east-1" and
  .resource_kind == "cli-evaluation" and .resource_id == "dev" and
  .inputs == {} and .correlation == "fixture"' "$scratch/deployment-context.json" >/dev/null
if env -i "${context_args[@]}" GITHUB_WORKFLOW_SHA=0000000000000000000000000000000000000000 \
  python3 "$script_dir/context.py" "$scratch/invalid-context.json" > "$scratch/context-error" 2>&1; then
  echo 'Incorrect workflow revision produced a context artifact' >&2; exit 1
fi
grep -Fq 'running workflow definition differs' "$scratch/context-error"
printf 'PASS context schema matches WorkflowProvider and rejects revision drift\n'

python3 "$script_dir/tests/test-live-contracts.py"

#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
set -uo pipefail

EVAL_SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run-eval.sh"
EVAL_LIB_DIR="$(cd "$(dirname "$EVAL_SCRIPT_PATH")/../lib" && pwd)"
# shellcheck source=../lib/log.sh
. "$EVAL_LIB_DIR/log.sh"
# shellcheck source=../lib/aws.sh
. "$EVAL_LIB_DIR/aws.sh"
# shellcheck source=../lib/clean-room.sh
. "$EVAL_LIB_DIR/clean-room.sh"
# shellcheck source=../lib/pod.sh
. "$EVAL_LIB_DIR/pod.sh"
# shellcheck source=../lib/state.sh
. "$EVAL_LIB_DIR/state.sh"

ENVIRONMENT=dev
AWS_REGION="${AWS_REGION:-us-east-1}"
EVAL_RUN_ID="${EVAL_RUN_ID:-local-$$}"
WORKDIR="${EVAL_WORKDIR:-$(mktemp -d)}"
STATE_FILE="$WORKDIR/state.env"
TRACE_FILE="$WORKDIR/trace.log"
RESULTS_FILE="$WORKDIR/results.tsv"
EVAL_SUMMARY_TITLE="Claude Desktop gateway eval"
FIXTURES="$(dirname "$EVAL_SCRIPT_PATH")/fixtures"
CONVERSATION="$(dirname "$EVAL_SCRIPT_PATH")/conversation.py"
DRY_RUN=false
MODE=run
INJECT_FAILURE=""
PHASES=1,2,3,4,5,6,7,8
PROXY_PORT=9191
POD_NAMESPACE="${EVAL_POD_NAMESPACE:-adp-gateway-evals}"
POD_IMAGE="${EVAL_POD_IMAGE:-node:20-bookworm}"
POD_LABEL_APP=eval-claude-desktop-gateway
POD_RUN_LABEL="$(printf '%s' "$EVAL_RUN_ID" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-63)"
LAPTOP_POD="eval-desktop-$(printf '%s' "$EVAL_RUN_ID" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-30 | sed 's/-*$//')"
POD_WORKDIR=/tmp/desktop-eval
POD_HOME="$POD_WORKDIR/home"
POD_PATH="$POD_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
POD_ASSERT_ENV=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --environment|--phases|--inject-failure)
      [ "$#" -ge 2 ] || die "$1 requires a value"
      case "$1" in
        --environment) ENVIRONMENT="$2" ;;
        --phases) PHASES="$2" ;;
        --inject-failure) INJECT_FAILURE="$2" ;;
      esac
      shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --cleanup-only) MODE=cleanup; shift ;;
    --assert-clean-room) MODE=clean-room; shift ;;
    *) die "Unknown option: $1" ;;
  esac
done
[ "$ENVIRONMENT" = dev ] || die "Only dev is an authorized evaluation target"
case "$INJECT_FAILURE" in
  ''|missing-tool-delta|wrong-tool-id|refresh-401|denied-model|cleanup-leftover) ;;
  *) die "Unknown injected failure: $INJECT_FAILURE" ;;
esac
[[ "$PHASES" =~ ^[1-8](,[1-8])*$ ]] || die "Unsupported phase selection: $PHASES"

if [ "$MODE" = cleanup ]; then
  WORKDIR="$WORKDIR/cleanup-only"
  STATE_FILE="$WORKDIR/state.env"
  TRACE_FILE="$WORKDIR/trace.log"
  RESULTS_FILE="$WORKDIR/results.tsv"
fi
mkdir -p "$WORKDIR"
chmod 700 "$WORKDIR"
: > "$TRACE_FILE"
: > "$RESULTS_FILE"
phase_enabled() { case ",$PHASES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }

cleanup() {
  phase 9 "delete the evaluation pod and check leftovers"
  if [ "$DRY_RUN" = true ]; then
    if [ "$INJECT_FAILURE" = cleanup-leftover ]; then
      CLEANUP_STATUS=leftover
      fail "CLI: cleanup-leftover: clean-room pod still present"
    else
      CLEANUP_STATUS=clean
      pass "CLI: no clean-room pod or temporary fixture remains"
    fi
    return
  fi
  if [ "$MODE" = cleanup ]; then
    local expected account
    expected="$(eval_ssm /adp/dev/platform/account-id)"
    account="$(h_aws sts get-caller-identity --query Account --output text 2>/dev/null)"
    if [ -z "$expected" ] || [ "$account" != "$expected" ] || [ "$AWS_REGION" != us-east-1 ]; then
      CLEANUP_STATUS=unverified
      fail "AWS: cleanup refused without verified dev account and region"
      return 1
    fi
  elif [ -z "$(state_get LAPTOP_POD)" ]; then
    CLEANUP_STATUS=clean
    skip "CLI: no evaluation pod was created; cleanup has nothing to delete"
    return
  fi
  local pods
  if ! laptop_pod_delete; then
    CLEANUP_STATUS=leftover
    fail "CLI: could not delete evaluation pods; run --cleanup-only"
    return 1
  fi
  pods="$(h_kubectl get pods -n "$POD_NAMESPACE" -l "app=$POD_LABEL_APP" -o json 2>/dev/null)" || {
    CLEANUP_STATUS=unverified
    fail "CLI: cannot verify evaluation pod cleanup; run --cleanup-only"
    return 1
  }
  if jq -e '.items | type == "array"' <<< "$pods" >/dev/null && [ "$(jq -r '.items | length' <<< "$pods")" = 0 ]; then
    CLEANUP_STATUS=clean
    pass "CLI: no pod with the evaluation label remains"
  else
    CLEANUP_STATUS=leftover
    fail "CLI: cleanup left a labeled evaluation pod; run --cleanup-only"
  fi
}

preflight() {
  phase 0 "verify dev identity, deployed image and fixtures"
  if [ ! -f "$CONVERSATION" ] || [ ! -s "$FIXTURES/tool.stream.sse" ] || [ ! -s "$FIXTURES/final.stream.sse" ] || [ ! -s "$FIXTURES/tool.json" ] || [ ! -s "$FIXTURES/final.json" ]; then
    fail "client: FIXTURE DRIFT — missing Desktop-shaped response fixtures"; return 1
  fi
  for file in tool.stream.sse tool.json; do
    python3 "$CONVERSATION" tool "$FIXTURES/$file" >/dev/null 2>&1 || {
      fail "client: FIXTURE DRIFT — $file does not contain a valid add call"; return 1;
    }
  done
  for file in final.stream.sse final.json; do
    python3 "$CONVERSATION" finish "$FIXTURES/$file" toolu_fixture_add 5 >/dev/null 2>&1 || {
      fail "client: FIXTURE DRIFT — $file does not finish the add call"; return 1;
    }
  done
  pass "client: Desktop-shaped streaming and non-streaming fixtures validated"
  if [ "$DRY_RUN" = true ]; then
    GATEWAY_URL=https://gateway.example.test/api
    EVAL_ACCOUNT=123456789012
    EVAL_IMAGE_DIGEST=sha256:fixture
    skip "AWS identity, deployed image digest and gateway preflight need dev; dry-run uses fixture data"
    return
  fi
  local expected actual image
  expected="$(eval_ssm /adp/dev/platform/account-id)"
  actual="$(h_aws sts get-caller-identity --query Account --output text 2>/dev/null)"
  if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
    fail "AWS: dev account does not match the SSM-held expectation"; return 1
  fi
  [ "$AWS_REGION" = us-east-1 ] || { fail "AWS: dev region is not us-east-1"; return 1; }
  image="$(h_kubectl get pods -n adp-gateway -l app=bedrockgateway -o json | jq -r '[.items[] | select(.status.phase == "Running") | .status.containerStatuses[]? | select(.ready) | .imageID] | unique | if length == 1 then .[0] else empty end' 2>/dev/null)" || true
  [[ "$image" == *@sha256:* ]] || { fail "gateway: ready pods do not share one verifiable deployed image digest"; return 1; }
  EVAL_ACCOUNT="$expected"
  EVAL_IMAGE_DIGEST="${image##*@}"
  GATEWAY_URL="https://$(eval_ssm /adp/dev/gateway/cloudfront-domain)/api"
  [ "$GATEWAY_URL" != https:///api ] || { fail "gateway: no dev domain in SSM"; return 1; }
  pass "AWS: expected dev account and region; gateway image digest verified"
}

clean_room() {
  phase 1 "start the credential-free laptop pod and reject contamination"
  if [ "$DRY_RUN" = true ]; then
    if [ "${EVAL_TEST_CONTAMINATED:-false}" = true ]; then
      fail "CLI: clean-room contamination: inherited AWS or ARC credentials"; return 1
    fi
    skip "CLI: no Kubernetes pod in dry run; shared pod isolation is checked by source and the contamination fixture"
    return
  fi
  laptop_pod_create || { fail "CLI: clean-room pod or contamination gate failed"; return 1; }
  pass "CLI: pod creation and contamination gate passed"
}

setup_cli() {
  phase 2 "import smoke-user login and verify the deployed CLI helper"
  if [ "$DRY_RUN" = true ]; then
    skip "CLI: deployed installer, smoke-user refresh and setup --check require dev"
    return
  fi
  [ -s "${EVAL_REFRESH_TOKEN_FILE:-}" ] || { fail "CLI: smoke-user refresh token file not configured"; return 1; }
  local installer="$POD_WORKDIR/install.sh" helper="$POD_HOME/.adp/bin/adp-claude-desktop-token" identity
  laptop_provision
  laptop_put_file "$CONVERSATION" "$POD_WORKDIR/conversation.py" 644 || return 1
  if ! laptop curl -fsS -o "$installer" "$GATEWAY_URL/cli/install.sh" \
    || ! laptop sh "$installer" --gateway-url "$GATEWAY_URL" --prefix "$POD_HOME/bin" >"$WORKDIR/install.log" 2>&1 \
    || ! laptop adp import --gateway-url "$GATEWAY_URL" < "$EVAL_REFRESH_TOKEN_FILE" >"$WORKDIR/import.log" 2>&1; then
    fail "CLI: installer or smoke-user import failed"; return 1
  fi
  if ! laptop adp update >"$WORKDIR/update.log" 2>&1 \
    || ! laptop adp claude-desktop setup --check >"$WORKDIR/setup.log" 2>&1; then
    fail "CLI: update or Desktop setup --check failed"; return 1
  fi
  # shellcheck disable=SC2016
  if ! laptop sh -c 'umask 077; "$1" > "$2"' _ "$helper" "$POD_WORKDIR/token"; then
    fail "CLI: credential helper failed"; return 1
  fi
  laptop_write_curl_auth_config "$POD_WORKDIR/token" "$POD_WORKDIR/auth.curlrc" || return 1
  identity="$(laptop node -e '
    const fs = require("fs");
    const parts = fs.readFileSync(process.argv[1], "utf8").trim().split("~");
    const scoped = parts[0] === "adpctx1";
    if (parts.length !== (scoped ? 3 : 1)) throw new Error("Invalid credential envelope");
    const decode = token => JSON.parse(Buffer.from(token.split(".")[1], "base64url"));
    const claims = decode(parts.at(-1));
    const org = scoped ? decode(parts[1]).tenant : claims["custom:org_id"] || claims.org_id;
    if (scoped && (typeof org !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(org))) {
      throw new Error("Invalid scoped tenant");
    }
    console.log(JSON.stringify({sub: claims.sub, org}));
  ' "$POD_WORKDIR/token")" || {
    fail "CLI: credential helper did not produce a decodable smoke-user identity"; return 1
  }
  SMOKE_USER="$(jq -r '.sub // empty' <<< "$identity")"
  SMOKE_ORG="$(jq -r '.org // empty' <<< "$identity")"
  [[ "$SMOKE_USER" =~ ^[A-Za-z0-9_-]{1,128}$ && "$SMOKE_ORG" =~ ^[A-Za-z0-9_-]{1,128}$ ]] || {
    fail "CLI: smoke-user token lacks a safe user and org attribution"; return 1
  }
  if ! laptop test ! -e "$POD_HOME/.config/Claude/claude_desktop_config.json"; then
    fail "CLI: Desktop settings were modified by setup --check"; return 1
  fi
  pass "CLI: deployed update and Desktop setup --check succeeded; helper stdout token-only and Desktop settings preserved"
}

gateway_http() {
  local method="$1" url="$2" body="$3" output="$4" headers="${5:-/dev/null}"
  if [ "$method" = GET ]; then
    laptop curl -sS -o "$output" -D "$headers" -w '%{http_code}' -K "$POD_WORKDIR/auth.curlrc" --max-time 120 "$url" || echo 000
  else
    laptop curl -sSN -o "$output" -D "$headers" -w '%{http_code}' -X POST -K "$POD_WORKDIR/auth.curlrc" \
      -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
      --data-binary "@$POD_WORKDIR/$body" --max-time 120 "$url" || echo 000
  fi
}

models() {
  phase 3 "check Desktop-entitled model and denied control"
  if [ "$DRY_RUN" = true ]; then
    MODEL=global.anthropic.claude-sonnet-4-6
    pass "gateway: Desktop model fixture selected"
  else
    local status
    status="$(gateway_http GET "$GATEWAY_URL/v1/models?client=claude-desktop" '' "$POD_WORKDIR/models.json")"
    [ "$status" = 200 ] || { fail "gateway: Desktop model discovery returned $status"; return 1; }
    laptop_get_file "$POD_WORKDIR/models.json" "$WORKDIR/models.json" || return 1
    MODEL="$(jq -r '.data[] | select(.id | startswith("global.anthropic.claude")) | .id' "$WORKDIR/models.json" | head -1)"
    [ -n "$MODEL" ] || { fail "gateway: no entitled Claude model in Desktop list"; return 1; }
    pass "gateway: Desktop model available"
  fi
  local status control_model=adp-eval.denied-control
  jq -n --arg model "$control_model" '{model:$model,max_tokens:256,messages:[{role:"user",content:"hello"}]}' > "$WORKDIR/denied-request.json"
  if [ "$DRY_RUN" = true ]; then
    if [ "$INJECT_FAILURE" = denied-model ]; then status=200; else status=403; fi
    jq -n --arg model "$control_model" '{detail:{error:"model_not_allowed",model:$model,allowed_models:["anthropic.claude-*"]}}' > "$WORKDIR/denied.json"
  else
    laptop_put_file "$WORKDIR/denied-request.json" "$POD_WORKDIR/denied-request.json" || return 1
    status="$(gateway_http POST "$GATEWAY_URL/v1/messages" denied-request.json "$POD_WORKDIR/denied.json")"
    laptop_get_file "$POD_WORKDIR/denied.json" "$WORKDIR/denied.json" || return 1
  fi
  [ "$status" = 403 ] || { fail "gateway: disallowed control model returned $status instead of 403"; return 1; }
  if ! jq -e --arg model "$control_model" '.detail.error == "model_not_allowed" and .detail.model == $model and (.detail.allowed_models | type == "array")' "$WORKDIR/denied.json" >/dev/null 2>&1; then
    fail "gateway: control response does not prove model admission denial"; return 1
  fi
  pass "gateway: disallowed model rejected with 403 model_not_allowed"
}

record_inference_request() {
  local headers="$1" number="$2" request_id
  if [ "$DRY_RUN" = true ]; then
    printf -v request_id '00000000-0000-4000-8000-%012d' "$number"
  else
    laptop_get_file "$headers" "$WORKDIR/headers" || return 1
    request_id="$(awk 'tolower($1)=="x-request-id:" {print $2}' "$WORKDIR/headers" | tr -d '\r' | tail -1)"
  fi
  [[ "$request_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
    fail "gateway: inference response lacks a valid request ID"; return 1
  }
  printf '%s\n' "$request_id" >> "$WORKDIR/inference-request-ids"
}

exchange() {
  local streaming="$1" phase_number="$2" response tool_id tool_value tool_data status suffix
  if [ "$streaming" = true ]; then suffix=stream.sse; else suffix=json; fi
  phase "$phase_number" "two-turn ${streaming} tool exchange"
  python3 "$CONVERSATION" request "$MODEL" "$streaming" > "$WORKDIR/first.json" || return 1
  if [ "$DRY_RUN" = true ]; then
    response="$WORKDIR/first.$suffix"
    cp "$FIXTURES/tool.$suffix" "$response"
    if [ "$INJECT_FAILURE" = missing-tool-delta ] && [ "$streaming" = true ]; then
      sed -i '/input_json_delta/d' "$response"
    fi
  else
    laptop_put_file "$WORKDIR/first.json" "$POD_WORKDIR/first.json" || return 1
    response="$POD_WORKDIR/first.$suffix"
    status="$(gateway_http POST "$GATEWAY_URL/v1/messages" first.json "$response" "$POD_WORKDIR/first.headers")"
    [ "$status" = 200 ] || { fail "gateway: first message returned $status"; return 1; }
    laptop_get_file "$response" "$WORKDIR/first.$suffix" || return 1
    response="$WORKDIR/first.$suffix"
  fi
  record_inference_request "$POD_WORKDIR/first.headers" "$((phase_number * 10 + 1))" || return 1
  if [ "$DRY_RUN" = true ]; then
    tool_data="$(python3 "$CONVERSATION" tool "$response" 2>"$WORKDIR/parser.err")" || {
      fail "$(sed 's/^\[FAIL\] //' "$WORKDIR/parser.err")"; return 1;
    }
  else
    tool_data="$(laptop python3 "$POD_WORKDIR/conversation.py" tool "$POD_WORKDIR/first.$suffix" 2>"$WORKDIR/parser.err")" || {
      fail "$(sed 's/^\[FAIL\] //' "$WORKDIR/parser.err")"; return 1;
    }
  fi
  tool_id="$(jq -r '.id' <<< "$tool_data")"
  tool_value="$(jq -r '.result' <<< "$tool_data")"
  pass "client: reconstructed add input JSON and executed the local tool"
  python3 "$CONVERSATION" request "$MODEL" "$streaming" "$tool_id" "$tool_value" > "$WORKDIR/second.json" || return 1
  if [ "$INJECT_FAILURE" = wrong-tool-id ] && [ "$streaming" = true ]; then
    python3 - "$WORKDIR/second.json" <<'PY'
import json
import sys
path = sys.argv[1]
with open(path) as source:
    request = json.load(source)
request["messages"][-1]["content"][0]["tool_use_id"] = "toolu_wrong"
with open(path, "w") as destination:
    json.dump(request, destination)
PY
  fi
  if ! jq -e --arg id "$tool_id" '.messages[-1].content[0].tool_use_id == $id and .messages[-2].content[0].id == $id' "$WORKDIR/second.json" >/dev/null; then
    fail "client: wrong tool id in tool_result"; return 1
  fi
  if [ "$streaming" = true ]; then STREAM_STATUS=true; fi
  if [ "$DRY_RUN" = true ]; then
    cp "$FIXTURES/final.$suffix" "$WORKDIR/second.$suffix"
  else
    laptop_put_file "$WORKDIR/second.json" "$POD_WORKDIR/second.json" || return 1
    status="$(gateway_http POST "$GATEWAY_URL/v1/messages" second.json "$POD_WORKDIR/second.$suffix" "$POD_WORKDIR/second.headers")"
    [ "$status" = 200 ] || { fail "gateway: second message returned $status"; return 1; }
    laptop_get_file "$POD_WORKDIR/second.$suffix" "$WORKDIR/second.$suffix" || return 1
  fi
  record_inference_request "$POD_WORKDIR/second.headers" "$((phase_number * 10 + 2))" || return 1
  if [ "$DRY_RUN" = true ]; then
    python3 "$CONVERSATION" finish "$WORKDIR/second.$suffix" "$tool_id" "$tool_value" 2>"$WORKDIR/parser.err" || {
      fail "$(sed 's/^\[FAIL\] //' "$WORKDIR/parser.err")"; return 1;
    }
  else
    laptop python3 "$POD_WORKDIR/conversation.py" finish "$POD_WORKDIR/second.$suffix" "$tool_id" "$tool_value" 2>"$WORKDIR/parser.err" || {
      fail "$(sed 's/^\[FAIL\] //' "$WORKDIR/parser.err")"; return 1;
    }
  fi
  FINAL_STATUS="${FINAL_STATUS:+$FINAL_STATUS,}stream=$streaming:true"
  pass "Bedrock: final answer contains local add result"
}

count_tokens() {
  phase 6 "identify token-count mode and compare direct Bedrock when exact"
  [ -n "${MODEL:-}" ] || { fail "gateway: model discovery is required before token counting"; return 1; }
  local mode status count direct model_id
  jq -n --arg model "$MODEL" '{model:$model,max_tokens:256,messages:[{role:"user",content:"Count this sample."}]}' > "$WORKDIR/count.json"
  if [ "$DRY_RUN" = true ]; then
    mode="${EVAL_TEST_COUNT_MODE:-exact}"
    count=42
  else
    laptop_put_file "$WORKDIR/count.json" "$POD_WORKDIR/count.json" || return 1
    status="$(gateway_http POST "$GATEWAY_URL/v1/messages/count_tokens" count.json "$POD_WORKDIR/count-response.json" "$POD_WORKDIR/count.headers")"
    [ "$status" = 200 ] || { fail "gateway: count_tokens returned $status"; return 1; }
    laptop_get_file "$POD_WORKDIR/count-response.json" "$WORKDIR/count-response.json" || return 1
    laptop_get_file "$POD_WORKDIR/count.headers" "$WORKDIR/count.headers" || return 1
    mode="$(awk 'tolower($1)=="x-adp-token-count-mode:" {print $2}' "$WORKDIR/count.headers" | tr -d '\r' | tail -1)"
    count="$(jq -r '.input_tokens // empty' "$WORKDIR/count-response.json")"
  fi
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || { fail "gateway: count_tokens returned no positive input count"; return 1; }
  COUNT_MODE="$mode"
  case "$mode" in
    exact)
      if [ "$DRY_RUN" = true ]; then
        direct=42
      else
        jq -c '{anthropic_version:"bedrock-2023-05-31",max_tokens:.max_tokens,
          messages:[.messages[] | {role,content:[{type:"text",text:.content}]}]}' "$WORKDIR/count.json" > "$WORKDIR/bedrock-count.json"
        jq -n --rawfile body "$WORKDIR/bedrock-count.json" '{invokeModel:{body:$body}}' > "$WORKDIR/direct-count.json"
        model_id="$MODEL"
        case "$model_id" in global.anthropic.*|us.anthropic.*|eu.anthropic.*) model_id="${model_id#*.}" ;; esac
        direct="$(h_aws bedrock-runtime count-tokens --cli-binary-format raw-in-base64-out --model-id "$model_id" --input "file://$WORKDIR/direct-count.json" --query inputTokens --output text 2>"$WORKDIR/direct-count.err")" || {
          fail "Bedrock: direct CountTokens request failed for exact mode"; return 1;
        }
      fi
      [ "$direct" = "$count" ] || { fail "Bedrock: gateway exact count differs from direct CountTokens"; return 1; }
      DIRECT_STATUS=true
      pass "Bedrock: count_tokens mode exact; gateway == direct: true" ;;
    estimate)
      DIRECT_STATUS="not compared (estimate)"
      skip "Bedrock: count_tokens mode estimate; direct comparison unavailable because gateway reported a denied or unsupported CountTokens operation" ;;
    *) fail "gateway: count_tokens mode missing or unknown"; return 1 ;;
  esac
}

refresh_session() {
  phase 7 "invalidate only pod access-token cache and verify scoped refresh"
  if [ "$DRY_RUN" = true ]; then
    if [ "$INJECT_FAILURE" = refresh-401 ]; then
      fail "CLI: refresh after token-cache invalidation returned 401"; return 1
    fi
    pass "CLI: refresh fixture succeeded without runner credentials"
    return
  fi
  local cache="$POD_HOME/.bedrock-gateway/tokens.json" helper="$POD_HOME/.adp/bin/adp-claude-desktop-token" status
  # shellcheck disable=SC2016
  if ! laptop sh -c 'set -eu; test -s "$1"; tmp="$1.tmp"; jq -e ".refresh_token | type == \"string\" and length > 0" "$1" >/dev/null; jq ".expires_at = 0 | .access_token = \"\" | .id_token = \"\"" "$1" > "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$1"' _ "$cache" \
    || ! laptop sh -c 'umask 077; "$1" > "$2"' _ "$helper" "$POD_WORKDIR/refreshed-token" \
    || ! laptop_write_curl_auth_config "$POD_WORKDIR/refreshed-token" "$POD_WORKDIR/auth.curlrc"; then
    fail "CLI: helper failed to refresh after pod access-token cache invalidation"; return 1
  fi
  status="$(gateway_http GET "$GATEWAY_URL/v1/models?client=claude-desktop" '' "$POD_WORKDIR/refreshed-models.json")"
  [ "$status" = 200 ] || { fail "CLI: refreshed scoped token returned $status from gateway"; return 1; }
  pass "CLI: refresh after cache expiry: true; helper token still authorizes Desktop models"
}

usage_rows() {
  phase 8 "attribute this run's inference request IDs to the smoke user and org"
  local expected rows matched count_for_identity query ids request_id attempt
  expected="$(wc -l < "$WORKDIR/inference-request-ids" 2>/dev/null || echo 0)"
  if [ "$expected" -eq 0 ]; then
    skip "usage: no inference requests selected; no attribution claim made"; return
  fi
  if [ "$DRY_RUN" = true ]; then
    if [ "${EVAL_TEST_WRONG_USAGE:-false}" = true ]; then
      fail "usage: request IDs attributed to a different smoke user or org"; return 1
    fi
    USAGE_STATUS="fixture: $expected inference rows attributed to smoke user and org"
    pass "usage: fixture rows match all $expected inference request IDs and the smoke user and org"
    return
  fi
  if [ -z "${SMOKE_USER:-}" ] || [ -z "${SMOKE_ORG:-}" ]; then
    fail "usage: smoke user or org missing; cannot prove attribution"; return 1
  fi
  resolve_db_creds
  ids=""
  while IFS= read -r request_id; do
    [[ "$request_id" =~ ^[0-9a-f-]{36}$ ]] || { fail "usage: malformed inference request ID"; return 1; }
    ids="${ids:+$ids,}'${request_id}'"
  done < "$WORKDIR/inference-request-ids"
  query="SELECT count(*), count(*) FILTER (WHERE user_id = '${SMOKE_USER}' AND org_id = '${SMOKE_ORG}') FROM usage_logs WHERE request_id IN (${ids});"
  for ((attempt = 1; attempt <= 5; attempt++)); do
    rows="$(h_psql -c "$query" 2>/dev/null | head -1)" || {
      fail "usage: could not read scoped usage rows"; return 1
    }
    IFS='|' read -r matched count_for_identity <<< "$rows"
    if [ "$matched" = "$expected" ] && [ "$count_for_identity" = "$expected" ]; then
      USAGE_STATUS="$matched/$expected rows attributed to smoke user and org"
      pass "usage: all $expected request IDs attributed to the smoke user and org"
      return
    fi
    sleep 2
  done
  fail "usage: inference request IDs missing or attributed to another user or org"; return 1
}

write_report() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  {
    echo "### Dev gateway evidence"
    echo "- Account: ${EVAL_ACCOUNT:-unverified}; region: ${AWS_REGION}"
    echo "- Model: ${MODEL:-unverified}; image digest: ${EVAL_IMAGE_DIGEST:-unverified}"
    echo "- Inference request IDs: $(paste -sd, "$WORKDIR/inference-request-ids" 2>/dev/null || echo none)"
    echo "- Smoke user / org: ${SMOKE_USER:-fixture} / ${SMOKE_ORG:-fixture}"
    echo "- Event checklist: tool_use.id stable across stream and tool_result: ${STREAM_STATUS:-not-run}; reconstructed input JSON valid: ${STREAM_STATUS:-not-run}"
    if [ "${FINAL_STATUS:-}" = 'stream=true:true,stream=false:true' ]; then
      echo "- final reply contains deterministic tool result: true (stream=true and stream=false)"
    else
      echo "- final reply contains deterministic tool result: ${FINAL_STATUS:-not-run}"
    fi
    echo "- count_tokens mode: ${COUNT_MODE:-not-run}; gateway == direct: ${DIRECT_STATUS:-not-run}"
    echo "- Usage attribution: ${USAGE_STATUS:-not-run}; cleanup: ${CLEANUP_STATUS:-not-run}"
  } >> "$GITHUB_STEP_SUMMARY"
}

pod_clean_room_gate() {
  local name
  for name in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_ARN AWS_PROFILE GITHUB_TOKEN GH_TOKEN; do
    if [ -n "${!name:-}" ]; then
      fail "CLI: clean-room contamination: inherited credential variable $name"; return 1
    fi
  done
  assert_clean_room
}

on_exit() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$FAILURES" -eq 0 ]; then
    fail "client: evaluation stopped without a recorded assertion"
  fi
  cleanup
  write_summary
  write_report
  if [ "$DRY_RUN" = false ] && [ "${CLEANUP_STATUS:-}" = clean ]; then
    find "$WORKDIR" -mindepth 1 -delete || fail "CLI: could not remove runner scratch after cleanup"
  fi
  if [ "$FAILURES" -ne 0 ]; then status=1; fi
  exit "$status"
}

main() {
  if [ "$MODE" = clean-room ]; then
    pod_clean_room_gate; return $?
  fi
  trap on_exit EXIT
  if [ "$MODE" = cleanup ]; then return; fi
  preflight || return 1
  if phase_enabled 1; then clean_room || return 1; fi
  if phase_enabled 2; then setup_cli || return 1; fi
  if phase_enabled 3; then models || return 1; fi
  if phase_enabled 4 || phase_enabled 5; then
    [ -n "${MODEL:-}" ] || { fail "gateway: select a model in phase 3 before exchanging messages"; return 1; }
    if phase_enabled 4; then exchange true 4 || return 1; fi
    if phase_enabled 5; then exchange false 5 || return 1; fi
  fi
  if phase_enabled 6; then count_tokens || return 1; fi
  if phase_enabled 7; then refresh_session || return 1; fi
  if phase_enabled 8; then usage_rows || return 1; fi
  [ "$FAILURES" -eq 0 ]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main
fi

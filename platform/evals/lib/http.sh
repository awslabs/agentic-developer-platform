# shellcheck shell=bash
# =============================================================================
# lib/http.sh — HTTP helpers with a retry policy that cannot mask a real denial
# =============================================================================
# Tokens travel to curl in a 0600 config file, never in argv (/proc/<pid>/cmdline
# is world-readable) and never in an exported variable a child could inherit.
# =============================================================================

write_curl_auth_config() {
  local token_file="$1" cfg="$2" header_name="${3:-Authorization}" prefix="${4:-Bearer }"
  local token
  token="$(cat "$token_file")"
  umask 077
  printf 'header = "%s: %s%s"\n' "$header_name" "$prefix" "$token" > "$cfg"
  chmod 600 "$cfg"
}

# -----------------------------------------------------------------------------
# The retry policy
# -----------------------------------------------------------------------------
# Retry transient EDGE errors (502/503/504) and connection failures (000). A flag
# flip or a rollout makes the ALB target group flap (ready/draining targets churn)
# and successive requests can each hit a bad target for a few seconds even after
# `kubectl rollout status` returns. Retrying rides out that churn so the assertion
# sees the true status.
#
# BUT: some 5xx are the APPLICATION'S OWN VERDICT, not edge churn, and retrying
# those eight times would silently convert a real fail-closed denial into whatever
# the last attempt happened to return.
#
# The budget middleware's check-unavailable path (#4075) returns **HTTP 503** with
# a body of {"error":"budget_check_unavailable",...}. That is a deliberate
# fail-closed decision the #4163 eval exists to observe. So the rule is:
#
#   a 5xx whose JSON body carries a known application-level .error is FINAL.
#
# Anything else 502/503/504/000 is edge churn and is retried. A genuine upstream
# 502 (e.g. a retired model) simply exhausts the retries and is still reported.
APP_LEVEL_5XX_ERRORS="${APP_LEVEL_5XX_ERRORS:-budget_check_unavailable ratelimit_check_unavailable}"

# is_app_level_5xx <body-file> — true when the body names an application-level
# error code, i.e. the status is the app's verdict and must NOT be retried.
is_app_level_5xx() {
  local body_file="$1" err known
  [ -f "$body_file" ] || return 1
  # `.error` at the top level, or nested under `.detail` (FastAPI HTTPException).
  err="$(jq -r '(.error // .detail.error // empty)' "$body_file" 2>/dev/null || true)"
  [ -n "$err" ] || return 1
  for known in $APP_LEVEL_5XX_ERRORS; do
    [ "$err" = "$known" ] && return 0
  done
  return 1
}

# http_post_json <curl-cfg> <url> <body-file> <out-body-file> -> echoes status
http_post_json() {
  local cfg="$1" url="$2" body_file="$3" out="$4"
  local status attempt=0 max_attempts=8
  while :; do
    status="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
      -K "$cfg" \
      -H 'content-type: application/json' \
      --data-binary "@${body_file}" \
      --max-time 120 \
      "$url" || echo "000")"
    case "$status" in
      502|503|504|000)
        # An application-level denial is the answer, not a flake. Stop.
        if is_app_level_5xx "$out"; then break; fi
        attempt=$((attempt + 1))
        [ "$attempt" -ge "$max_attempts" ] && break
        sleep 3
        ;;
      *) break ;;
    esac
  done
  printf '%s' "$status"
}

# http_get <curl-cfg> <url> <out-body-file> -> echoes status
http_get() {
  local cfg="$1" url="$2" out="$3"
  local status attempt=0 max_attempts=8
  while :; do
    status="$(curl -sS -o "$out" -w '%{http_code}' -K "$cfg" --max-time 60 "$url" || echo "000")"
    case "$status" in
      502|503|504|000)
        if is_app_level_5xx "$out"; then break; fi
        attempt=$((attempt + 1))
        [ "$attempt" -ge "$max_attempts" ] && break
        sleep 3
        ;;
      *) break ;;
    esac
  done
  printf '%s' "$status"
}

# http_delete / http_put — the admin API surface the budget eval configures
# through. Same 0600-config discipline; same retry policy.
http_delete() {
  local cfg="$1" url="$2" out="$3"
  local status attempt=0 max_attempts=8
  while :; do
    status="$(curl -sS -o "$out" -w '%{http_code}' -X DELETE -K "$cfg" --max-time 60 "$url" || echo "000")"
    case "$status" in
      502|503|504|000)
        if is_app_level_5xx "$out"; then break; fi
        attempt=$((attempt + 1))
        [ "$attempt" -ge "$max_attempts" ] && break
        sleep 3
        ;;
      *) break ;;
    esac
  done
  printf '%s' "$status"
}

# -----------------------------------------------------------------------------
# Request bodies
# -----------------------------------------------------------------------------
# EVAL_MODEL / EVAL_CODEX_MODEL are the caller's; both are in
# enable-bedrock-models.sh's REQUIRED_MODELS, so a deploy that passed cannot lack
# access to them.
anthropic_body() {
  local out="$1" prompt="$2"
  jq -n --arg m "$EVAL_MODEL" --arg p "$prompt" \
    '{model:$m, max_tokens:64, messages:[{role:"user",content:$p}]}' > "$out"
}

openai_body() {
  local out="$1" prompt="$2"
  jq -n --arg m "$EVAL_MODEL" --arg p "$prompt" \
    '{model:$m, max_tokens:64, messages:[{role:"user",content:$p}]}' > "$out"
}

responses_body() {
  local out="$1" prompt="$2"
  jq -n --arg m "$EVAL_CODEX_MODEL" --arg p "$prompt" \
    '{model:$m, input:$p}' > "$out"
}

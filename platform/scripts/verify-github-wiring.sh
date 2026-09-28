#!/usr/bin/env bash
# =============================================================================
# verify-github-wiring.sh — Issue #4032
# =============================================================================
# Post-deploy verification that the GitHub App wiring can actually dispatch an
# agent. This is the Phase 9 "Verify" gate of
# docs/adp-platform-deployment/deploy-quickstart.md.
#
# WHY THIS EXISTS: the previous Phase 9 check asserted that one secret
# (`adp/gh-app-id`) was non-empty — a secret ID that exists nowhere in this
# codebase — and the end-to-end check only asserted that a pod spawned. The
# Acme PoV-2 deployment PASSED that gate with dispatch 100% broken: the
# per-tenant secret was missing, so worker pods spawned, crash-looped in
# bootstrap step 2, and never replied. Every check here is one that failure
# would have tripped.
#
# Usage:
#   ./platform/scripts/verify-github-wiring.sh --installation-id <id> [options]
#
# Options:
#   --env <env>              Environment (default: dev). See the ADP_ENV note below.
#   --installation-id <id>   GitHub App installation ID (required). Find it at
#                            https://github.com/settings/installations or in the
#                            install URL after completing Phase 9.
#   --repo <owner/name>      Enable the round-trip check (step 5) on this repo.
#   --issue <n>              Enable the round-trip check (step 5) on this issue.
#   --timeout <seconds>      Round-trip poll timeout (default: 600).
#   -h, --help               Show this help.
#
# Steps 1-4 are read-only and always run. Step 5 runs ONLY when both --repo and
# --issue are supplied, because it posts a real comment and burns real Bedrock
# tokens.
#
# Exit codes: 0 = all HARD checks passed, 1 = a HARD check failed, 2 = usage error.
#
# Requires: awscli (secretsmanager:GetSecretValue, dynamodb:GetItem), jq,
#           and for step 5 `gh` authenticated against --repo.
#
# NOTE ON --env: this script defaults to `dev` deliberately. The agent worker's
# vault client keys the secret path off ADP_ENV (lib/vault_client.py), which is
# injected nowhere, so it resolves `dev` regardless of the deployment's actual
# environment. On a non-dev deploy, verify against the path the worker ACTUALLY
# reads, not the one the writers use. Tracked as a separate code issue (#4042).
# =============================================================================
set -euo pipefail

ENV="dev"
INSTALLATION_ID=""
REPO=""
ISSUE=""
TIMEOUT=600
REGION="${AWS_REGION:-us-east-1}"

FAILURES=0
WARNINGS=0

# -----------------------------------------------------------------------------
# Pure predicates — no AWS calls, unit-tested by tests/test-verify-github-wiring.sh
# -----------------------------------------------------------------------------

# A GitHub App ID is an all-digits string. Rejects empty and placeholder values.
is_valid_app_id() {
  printf '%s' "${1:-}" | grep -qE '^[0-9]+$'
}

# A real private key is PEM-armoured. This is what distinguishes a genuine key
# from a placeholder or a truncated write — mint_installation_token() is the next
# thing that blows up on a malformed one.
is_valid_private_key() {
  printf '%s' "${1:-}" | grep -q '^-----BEGIN'
}

# Assert the tenant secret JSON has a non-empty app_id AND a PEM-shaped
# private_key. Reads the secret JSON on stdin.
#
# The obvious `jq -e '.app_id and .private_key'` is WRONG twice over: it passes
# on empty strings ({"app_id":"","private_key":""} exits 0), and it explodes on
# a healthy secret if the caller forgot `--output text` (--query SecretString
# alone emits a JSON-quoted *string*, not an object).
#
# Two further traps, both found by the unit tests and both false-PASSES:
#   * `.app_id | tostring` on a MISSING key yields the string "null" (length 4),
#     so a length>0 test passes on a secret with no app_id at all. Hence `// ""`.
#   * `jq -e` on EMPTY stdin emits nothing and exits 0 — so an unreadable secret
#     would pass. Hence we compare the emitted verdict explicitly rather than
#     trusting jq's exit code.
tenant_secret_is_wellformed() {
  local verdict
  verdict=$(jq -r 'if (((.app_id // "") | tostring | length) > 0)
                      and (((.private_key // "") | tostring) | startswith("-----BEGIN"))
                   then "true" else "false" end' 2>/dev/null) || return 1
  [ "$verdict" = "true" ]
}

# Count genuine agent replies in a `gh api .../comments` JSON payload on stdin.
#
# "Reply" means the agent produced a real answer. Three rules, and each one is
# load-bearing — a green step 5 on a broken deploy is the bug this whole script
# exists to prevent, so do not widen these without re-reading the tests.
#
# (1) Exclude `<!-- adp-run:` — the worker posts a "🤖 Agent started working"
#     comment at bootstrap step 9. Counting it false-passes every failure
#     downstream of step 9, notably the Phase 8 Bedrock gotcha, where the
#     started comment posts and nothing else ever does.
#
# (2) Exclude `<!-- adp-failed:` — the worker's failure paths post a Bot comment
#     with that marker (entrypoint.py `_post_comment(..., "failed", ...)`; the
#     marker is assembled by f-string, so grepping the literal finds nothing).
#     One of those paths is the zero-token diagnostic — "the model call never
#     succeeded (0 tokens)" — i.e. exactly the Bedrock failure rule (1) is
#     guarding against. Without this exclusion the failure comment itself
#     satisfies the gate and the script reports dispatch as healthy on a run
#     that demonstrably failed.
#
# (3) Filter on .user.type == "Bot" rather than a hardcoded login, because the
#     App name is chosen per-deployment at registration time.
count_agent_replies() {
  jq '[ .[]
        | select(.user.type == "Bot")
        | select((.body // "") | contains("<!-- adp-run:") | not)
        | select((.body // "") | contains("<!-- adp-failed:") | not)
      ] | length' 2>/dev/null || echo 0
}

# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------
ok()   { echo "   OK: $*"; }
skip() { echo "   SKIP: $*"; }
warn() { echo "   WARN: $*"; WARNINGS=$((WARNINGS + 1)); }
fail() { echo "   FAIL: $*"; FAILURES=$((FAILURES + 1)); }

usage() {
  sed -n '/^# Usage:/,/^# ====/p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# -----------------------------------------------------------------------------
# Arg parsing
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --env)             ENV="${2:?--env needs a value}"; shift 2 ;;
    --installation-id) INSTALLATION_ID="${2:?--installation-id needs a value}"; shift 2 ;;
    --repo)            REPO="${2:?--repo needs a value}"; shift 2 ;;
    --issue)           ISSUE="${2:?--issue needs a value}"; shift 2 ;;
    --timeout)         TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    -h|--help)         usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

if [ -z "$INSTALLATION_ID" ]; then
  echo "ERROR: --installation-id is required." >&2
  echo "Find it at https://github.com/settings/installations (or the install URL)." >&2
  exit 2
fi

TABLE_NAME="adp-${ENV}-identity-index"
WEBHOOK_LOG_GROUP="/aws/lambda/adp-${ENV}-github-webhook"

echo "=== GitHub App wiring verification (env=${ENV}, installation_id=${INSTALLATION_ID}) ==="
echo ""

# -----------------------------------------------------------------------------
# Step 1 (HARD) — platform App secrets.
#
# Assert these FIRST: auto-provision composes every per-tenant secret by copying
# these two (webhook-ingress lambda/github/handler.py). If they are missing or
# malformed, every tenant seed fails downstream, so a step-4 failure here would
# be a symptom, not the cause.
# -----------------------------------------------------------------------------
echo "1. Platform App secrets (adp/${ENV}/github-app/adp-agent-platform-*)..."
PLATFORM_APP_ID=$(aws secretsmanager get-secret-value \
  --secret-id "adp/${ENV}/github-app/adp-agent-platform-id" \
  --region "$REGION" --query SecretString --output text 2>/dev/null || echo "")
if is_valid_app_id "$PLATFORM_APP_ID"; then
  ok "platform App ID present (${PLATFORM_APP_ID})"
else
  fail "adp/${ENV}/github-app/adp-agent-platform-id missing or not numeric"
  echo "         → Phase 9 never completed. Re-run the UI flow (Settings →"
  echo "           Connections → 'Set up GitHub App') or register-github-app.sh."
fi

PLATFORM_APP_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "adp/${ENV}/github-app/adp-agent-platform-key" \
  --region "$REGION" --query SecretString --output text 2>/dev/null || echo "")
if is_valid_private_key "$PLATFORM_APP_KEY"; then
  ok "platform App private key present and PEM-armoured"
else
  fail "adp/${ENV}/github-app/adp-agent-platform-key missing or not a PEM key"
fi
echo ""

# -----------------------------------------------------------------------------
# Step 2 (HARD) — forward identity row, and the ORG_ID capture.
#
# Sequenced before the tenant-secret check on purpose: ORG_ID is not knowable a
# priori (it is the Postgres tenant_id when the gateway resolves one, else a
# fallback to the org login). Telling an operator to "substitute <org_id>"
# invites a wrong guess and then a ResourceNotFoundException misread as a broken
# deploy. Derive it from the row instead.
# -----------------------------------------------------------------------------
echo "2. Forward identity row (github_installation_id → org_id) in ${TABLE_NAME}..."
ORG_ID=$(aws dynamodb get-item \
  --table-name "$TABLE_NAME" --region "$REGION" \
  --key "{\"identity_type\":{\"S\":\"github_installation_id\"},\"identity_value\":{\"S\":\"${INSTALLATION_ID}\"}}" \
  --query 'Item.org_id.S' --output text 2>/dev/null || echo "")

if [ -n "$ORG_ID" ] && [ "$ORG_ID" != "None" ]; then
  ok "forward row present → tenant=${ORG_ID}"
else
  ORG_ID=""
  fail "no forward row for installation_id=${INSTALLATION_ID} in ${TABLE_NAME}"
  echo "         → The App is registered but its 'installation' webhook never"
  echo "           landed, so nothing mapped it to a tenant. Re-deliver the"
  echo "           install event from the App's Advanced → Recent Deliveries"
  echo "           page, then check the discriminators printed below."
fi
echo ""

# -----------------------------------------------------------------------------
# Step 3 (SOFT / WARN) — reverse identity row.
#
# Deliberately NOT a hard failure. The reverse row has exactly one writer (the
# webhook Lambda's auto-register); the gateway never writes it — `org_installation`
# is not even in the gateway's IdentityType literal. So a healthy UI-installed
# tenant whose `@agent-developer` mention round-trips perfectly can legitimately
# be missing it (that gap is #3860). It is consumed only by the EventBridge and
# agent-trigger handlers, i.e. agent→agent chaining and scheduled triggers —
# never by the human-mention path this script smoke-tests. Hard-failing on it
# would block a working deploy.
#
# Also note the type asymmetry: the forward row stores installation_id as a
# STRING (.S), the reverse row as a NUMBER (.N). Don't "tidy" these to match.
# -----------------------------------------------------------------------------
echo "3. Reverse identity row (org_installation → installation_id) [SOFT]..."
if [ -z "$ORG_ID" ]; then
  skip "no tenant resolved in step 2"
else
  REVERSE_ID=$(aws dynamodb get-item \
    --table-name "$TABLE_NAME" --region "$REGION" \
    --key "{\"identity_type\":{\"S\":\"org_installation\"},\"identity_value\":{\"S\":\"${ORG_ID}\"}}" \
    --query 'Item.installation_id.N' --output text 2>/dev/null || echo "")
  if [ -n "$REVERSE_ID" ] && [ "$REVERSE_ID" != "None" ]; then
    ok "reverse row present → installation_id=${REVERSE_ID}"
  else
    warn "no reverse row for tenant=${ORG_ID} (not fatal)"
    echo "         Absent on UI-installed tenants (#3860). Required for"
    echo "         agent→agent chaining and scheduled triggers, NOT for a human"
    echo "         @agent-developer mention. Repair with:"
    echo "           ENVIRONMENT=${ENV} ./platform/scripts/backfill-org-installation-index.sh"
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# Step 4 (HARD) — the per-tenant secret. THE Acme PoV-2 FAILURE.
#
# The worker hard-requires this at bootstrap step 2 with no fallback: it fetches
# only tenants/<tenant_id>/github-app and re-raises on any exception. There is no
# global-secret fallback. Missing → the pod spawns, dies in bootstrap, and
# crash-loops. That is why "a pod spawned" was never a sufficient check.
# -----------------------------------------------------------------------------
echo "4. Per-tenant App secret (adp/${ENV}/tenants/<tenant>/github-app)..."
if [ -z "$ORG_ID" ]; then
  skip "no tenant resolved in step 2"
  echo "         Orientation — which tenants did get seeded:"
  echo "           aws secretsmanager list-secrets \\"
  echo "             --filters Key=name,Values=\"adp/${ENV}/tenants/\" \\"
  echo "             --query 'SecretList[].Name' --output text"
else
  TENANT_SECRET_PATH="adp/${ENV}/tenants/${ORG_ID}/github-app"
  TENANT_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id "$TENANT_SECRET_PATH" \
    --region "$REGION" --query SecretString --output text 2>/dev/null || echo "")

  if [ -z "$TENANT_SECRET" ]; then
    fail "${TENANT_SECRET_PATH} does not exist"
    echo "         → THIS IS THE DISPATCH-BREAKING FAILURE. Worker pods will"
    echo "           spawn and crash-loop in bootstrap step 2 (vault_fetch)."
    echo "           The seed is attempted only when an 'installation' webhook"
    echo "           is processed, and only CreateSecret is permitted (never"
    echo "           PutSecretValue), so the repair is to re-deliver the install"
    echo "           event from the App's Recent Deliveries page. Then confirm"
    echo "           with the Auto-provision: search below."
  elif printf '%s' "$TENANT_SECRET" | tenant_secret_is_wellformed; then
    ok "tenant secret present and well-formed (app_id + PEM private_key)"
  else
    fail "${TENANT_SECRET_PATH} exists but is malformed"
    echo "         → app_id is empty, or private_key is not PEM-armoured. The"
    echo "           worker will fail at bootstrap step 3 (mint_token) instead"
    echo "           of step 2. Delete the secret and re-deliver the install"
    echo "           event so auto-provision recreates it."
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# Step 5 (opt-in, HARD when run) — the real round trip.
#
# "A pod spawned" is not a passing deployment; a reply comment is. Opt-in
# because it posts a real comment and spends real Bedrock tokens.
# -----------------------------------------------------------------------------
echo "5. End-to-end comment round-trip [opt-in]..."
ROUND_TRIP_RAN=0
if [ -z "$REPO" ] || [ -z "$ISSUE" ]; then
  skip "pass --repo <owner/name> --issue <n> to run the round-trip"
  echo "         Steps 1-4 prove the wiring EXISTS; only this step proves it WORKS."
else
  COMMENTS_API="repos/${REPO}/issues/${ISSUE}/comments"

  # Fetch the comment list and print the reply count, or print nothing if the
  # API call itself failed. Callers distinguish "0 replies" from "no answer",
  # which matters: conflating them is how a transient network blip turns into a
  # verdict. `if !` keeps a non-zero exit from tripping `set -e`.
  fetch_reply_count() {
    local payload
    if ! payload=$(gh api "$COMMENTS_API" --paginate 2>/dev/null); then
      return 1
    fi
    printf '%s' "$payload" | count_agent_replies
  }

  # Baseline first. On a re-run of the smoke test, comments from a previous run
  # are already present and would make any "did a bot comment appear?" poll pass
  # spuriously. Compare against the baseline, or use a fresh issue.
  #
  # A failed baseline is NOT tolerated. Defaulting it to 0 when the API is
  # unreachable would make every pre-existing reply look new, so the first poll
  # would pass instantly without dispatch having done anything — a false pass,
  # the exact failure mode this script exists to remove. Better to run no
  # round-trip than to report an unearned one.
  if ! BASELINE=$(fetch_reply_count); then
    ROUND_TRIP_RAN=1
    fail "could not read existing comments on ${REPO}#${ISSUE} (gh api failed)"
    echo "         Round-trip not attempted: without a trustworthy baseline a"
    echo "         pre-existing reply would be counted as a new one. Check"
    echo "         'gh auth status' and repo access, then re-run."
  elif ! gh issue comment "$ISSUE" -R "$REPO" \
       --body "@agent-developer say hello" >/dev/null 2>&1; then
    ROUND_TRIP_RAN=1
    fail "could not post the trigger comment on ${REPO}#${ISSUE} (gh failed)"
    echo "         This is a local gh/auth problem, not a dispatch failure —"
    echo "         nothing was dispatched. Check 'gh auth status', then re-run."
  else
    echo "   Baseline agent replies on ${REPO}#${ISSUE}: ${BASELINE}"
    echo "   Posted trigger comment; polling up to ${TIMEOUT}s for a reply..."

    ELAPSED=0
    REPLIES="$BASELINE"
    POLL_ERRORS=0
    while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
      sleep 15
      ELAPSED=$((ELAPSED + 15))
      # A transient failure here degrades one iteration, it does not end the
      # run: aborting mid-poll would skip the summary and the discriminators,
      # which are the only diagnostic output this script produces.
      if CURRENT=$(fetch_reply_count); then
        REPLIES="$CURRENT"
        if [ "${REPLIES:-0}" -gt "${BASELINE:-0}" ]; then
          break
        fi
        echo "   ... ${ELAPSED}s elapsed (replies=${REPLIES})"
      else
        POLL_ERRORS=$((POLL_ERRORS + 1))
        echo "   ... ${ELAPSED}s elapsed (gh api failed, retrying)"
      fi
    done

    ROUND_TRIP_RAN=1
    if [ "${REPLIES:-0}" -gt "${BASELINE:-0}" ]; then
      ok "agent replied after ~${ELAPSED}s — dispatch is working end to end"
    elif [ "$POLL_ERRORS" -gt 0 ]; then
      # Every poll may have errored, in which case we never actually observed
      # the issue state and cannot call this a dispatch failure.
      fail "no agent reply after ${TIMEOUT}s (${POLL_ERRORS} poll(s) hit gh api errors — result may be unreliable)"
    else
      fail "no agent reply after ${TIMEOUT}s"
    fi
  fi
fi
echo ""

# -----------------------------------------------------------------------------
# Discriminators — printed whenever something failed. These separate the known
# failure modes from each other, which is the difference between a one-command
# diagnosis and a multi-hour cross-service trace.
# -----------------------------------------------------------------------------
if [ "$FAILURES" -gt 0 ]; then
  cat <<EOF
--- Discriminators: which failure mode is this? ---

A) Webhook Lambda (durable, Terraform-managed, 14-day retention):

   aws logs filter-log-events --log-group-name "${WEBHOOK_LOG_GROUP}" \\
     --filter-pattern '"Auto-registered installation_id="' \\
     --query 'events[-5:].message' --output text

   aws logs filter-log-events --log-group-name "${WEBHOOK_LOG_GROUP}" \\
     --filter-pattern '"Auto-provision:"' \\
     --query 'events[-5:].message' --output text

   Reading these two:
     * 'Auto-provision:' present  ⇒ the tenant-secret SEED FAILED. All three
       call sites are logger.error, so any occurrence is a failure, not progress.
       The message names the cause (e.g. platform secrets unreadable).
     * 'Auto-registered' present + 'Auto-provision:' ABSENT ⇒ the #4030
       signature: the identity mapping was written but the seed was never
       attempted. Matches a step-2 PASS with a step-4 FAIL above.
     * Neither present ⇒ the install webhook never reached the Lambda at all.
       Check the App's Advanced → Recent Deliveries page for non-2xx responses.

   Caveat: the 'Auto-registered ... (forward + reverse)' line is logged
   unconditionally, including when the reverse write was skipped by its
   non-clobber guard. Treat it as proof of the FORWARD row only — step 3 above
   is the authority on the reverse row.

B) Worker bootstrap (stdout only — see the caveat):

   kubectl get pods -n adp-agents
   kubectl logs -n adp-agents -l app.kubernetes.io/name=agent-scaledjob \\
     --tail=200 --prefix=true 2>/dev/null | grep -E 'name=vault_fetch|bootstrap_logger'

   'name=vault_fetch] FAILED exception=ResourceNotFoundException' is the exact
   missing-tenant-secret signature. 'vault_fetch] OK' present with no reply
   comment means the secret resolved and the failure is DOWNSTREAM — Bedrock
   model access (Phase 8) or mint_token — not the tenant secret.

   ⚠️  Run this WHILE the round-trip poll is still waiting. These lines do not
   reach CloudWatch: the bootstrap logger targets /adp/${ENV}/agent-factory/bootstrap,
   but the ScaledJob pod role has no logs permission on it, so the logger
   fail-softs to stdout and the crash-looping pod is GC'd shortly after. Do not
   query that log group for webhook-path workers — it will be empty and read as
   "no failure occurred". Tracked as a separate IAM issue (#4041).

   Crash-looping or Error pods in adp-agents = dispatch DID reach a worker, so
   the break is in the worker's bootstrap, not in webhook → SQS → KEDA.
EOF
  echo ""
fi

# -----------------------------------------------------------------------------
# Verdict
# -----------------------------------------------------------------------------
echo "=== Summary ==="
if [ "$ROUND_TRIP_RAN" -eq 0 ]; then
  echo "Round-trip (step 5) NOT run — wiring checked, dispatch UNPROVEN."
fi
echo "Hard failures: ${FAILURES}   Warnings: ${WARNINGS}"

if [ "$FAILURES" -gt 0 ]; then
  echo "RESULT: FAIL — Phase 9 is not complete."
  exit 1
fi
echo "RESULT: PASS"
exit 0

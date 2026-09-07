#!/usr/bin/env bash
# bedrock-routing-validate.sh — Operations gates for the Bedrock per-principal
# account routing admin surface (issue #4745, parent #4692).
#
# WHY THIS FILE EXISTS
#   Issue #4745 comments 3 and 6 (mine, @agent-operations) instructed the
#   implementing agent to re-verify a destination with:
#       ./platform/scripts/bedrock-routing-validate.sh --check destination ...
#   That script had never been written. Anyone following the instruction got
#   "No such file or directory". This is that script, shipped to make the
#   referenced command real rather than aspirational.
#
#   It is DISTINCT from validate-bedrock-routing-shadow.sh (#4743), which
#   validates shadow-mode resolution and proxy latency and knows nothing about
#   destinations or the admin surface.
#
# CHECKS
#   destination  Prove a routing destination role is actually usable:
#                  (a) assume WITH the ExternalId succeeds
#                  (b) assume WITHOUT the ExternalId is DENIED  <- the load-bearing
#                      negative; without it a green (a) proves nothing, because a
#                      role trusting the whole account would also pass (a)
#                  (c) a REAL bedrock:InvokeModel succeeds on the assumed session
#                Together these are exactly what a save-time test-assume must
#                establish before a mapping may be stored (#4692 ruling: reject,
#                never store inert — the #4511 class).
#   authz        Prove the admin routes are platform-admin-only. A member identity
#                must receive 401/403 on every route. See the LIMITATION note.
#   panel        Prove the admin surface is actually deployed (routes not 404).
#
# LIMITATION, STATED UP FRONT (do not let a green run overstate this)
#   dev seeds only two identities: adp/<env>/gateway/test-admin-credentials
#   (is_admin=True, platform admin) and .../test-user-credentials (plain member).
#   There is NO org_admin identity, so --check authz demonstrates the 403 against
#   a MEMBER, not against an org admin. A member is denied by any authz check at
#   all, so this is the weaker assertion. The org_admin case is provable cheaply
#   only in the backend test suite with a synthetic org_admin context, and that
#   test is required at PR time. This script prints that caveat on every run so a
#   PASS is never mistaken for the stronger claim.
#
#   For the record, the platform DOES reject org_admin here: all three copies of
#   the predicate (auth/auth_service.py:301, auth/dependencies.py:82,
#   auth/middleware.py:673) are role in {platform_admin, admin} or group in
#   {admins, platform-admins} — org_admin is deliberately excluded, with the
#   rationale in the comment at dependencies.py:75-81. The residual risk is
#   DRIFT between those three duplicated copies, which --check authz cannot see.
#
# SECRETS
#   The destination ExternalId is read from SSM SecureString at runtime and is
#   never echoed, never passed on a command line, and never written to a file.
#   Bearer tokens are handed to curl via a file, never argv (argv is readable
#   through /proc on a shared host). This matches the shadow script's handling.
#
# IDEMPOTENT: read-only. Authors no mappings, registers no destinations, mutates
# no AWS or database state. Safe to re-run any number of times.
#
# Usage:
#   ./platform/scripts/bedrock-routing-validate.sh                      # all checks
#   ./platform/scripts/bedrock-routing-validate.sh --check destination
#   ./platform/scripts/bedrock-routing-validate.sh --check destination \
#       --destination-account 938500344975
#   ./platform/scripts/bedrock-routing-validate.sh --check authz -e dev
#
# Exit codes: 0 = every selected check passed, 1 = a check FAILED, 2 = setup error.
set -euo pipefail

ENVIRONMENT="dev"
CHECK="all"
DESTINATION_ACCOUNT=""
DESTINATION_ROLE_ARN=""
EXTERNAL_ID_PARAM=""
ROLE_ARN_PARAM=""
MODEL_ID="us.anthropic.claude-sonnet-4-5-20250929-v1:0"
REGION="us-east-1"

usage() {
  cat <<'USAGE'
Usage: bedrock-routing-validate.sh [options]

Options:
  --check              destination | authz | panel | all   (default: all)
  --environment, -e    Target environment (default: dev)
  --destination-account
                       Account id of the destination to test. If given without
                       --destination-role-arn, the role ARN is assumed to be
                       arn:aws:iam::<acct>:role/OrganizationAccountAccessRole.
  --destination-role-arn
                       Explicit destination role ARN (overrides the above).
  --external-id-param  SSM param holding the ExternalId (SecureString).
                       Default: /adp/<env>/bedrock-routing/validation-destination/external-id
  --role-arn-param     SSM param holding the destination role ARN.
                       Default: /adp/<env>/bedrock-routing/validation-destination/role-arn
  --model-id           Inference profile for the invoke probe.
                       Default: us.anthropic.claude-sonnet-4-5-20250929-v1:0
                       NOTE: us.anthropic.claude-3-5-haiku-20241022-v1:0 is
                       END-OF-LIFE in this org and returns ResourceNotFoundException,
                       which looks exactly like a permissions failure but is not.
                       Invocation goes through us.anthropic.* INFERENCE PROFILES,
                       not bare foundation-model ARNs.
  --region             AWS region (default: us-east-1)
  --help, -h           Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)                 CHECK="$2"; shift 2 ;;
    --environment|-e)        ENVIRONMENT="$2"; shift 2 ;;
    --destination-account)   DESTINATION_ACCOUNT="$2"; shift 2 ;;
    --destination-role-arn)  DESTINATION_ROLE_ARN="$2"; shift 2 ;;
    --external-id-param)     EXTERNAL_ID_PARAM="$2"; shift 2 ;;
    --role-arn-param)        ROLE_ARN_PARAM="$2"; shift 2 ;;
    --model-id)              MODEL_ID="$2"; shift 2 ;;
    --region)                REGION="$2"; shift 2 ;;
    --help|-h)               usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

case "${CHECK}" in
  destination|authz|panel|all) ;;
  *) echo "ERROR: --check must be one of: destination, authz, panel, all" >&2; exit 2 ;;
esac

for bin in aws curl python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found in PATH" >&2; exit 2; }
done

: "${EXTERNAL_ID_PARAM:=/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/external-id}"
: "${ROLE_ARN_PARAM:=/adp/${ENVIRONMENT}/bedrock-routing/validation-destination/role-arn}"

FAILURES=0
PASSES=0
SKIPS=0

pass() { echo "    PASS: $1"; PASSES=$((PASSES + 1)); }
fail() { echo "    FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }
skip() { echo "    SKIP: $1"; SKIPS=$((SKIPS + 1)); }

echo "=== Bedrock routing admin-surface validation (#4745) ==="
echo "Environment: ${ENVIRONMENT}    Checks: ${CHECK}"
aws sts get-caller-identity --query Arn --output text 2>/dev/null | sed 's/^/Caller: /' || {
  echo "ERROR: no usable AWS credentials. Connect an account or refresh the session." >&2
  exit 2
}
echo

# ===========================================================================
# CHECK: destination
# ===========================================================================
if [[ "${CHECK}" == "destination" || "${CHECK}" == "all" ]]; then
  echo "--> [destination] Proving a routing destination is genuinely usable"

  if [[ -z "${DESTINATION_ROLE_ARN}" && -n "${DESTINATION_ACCOUNT}" ]]; then
    DESTINATION_ROLE_ARN="arn:aws:iam::${DESTINATION_ACCOUNT}:role/OrganizationAccountAccessRole"
    echo "    derived role ARN from --destination-account"
  fi
  if [[ -z "${DESTINATION_ROLE_ARN}" ]]; then
    DESTINATION_ROLE_ARN="$(aws ssm get-parameter --name "${ROLE_ARN_PARAM}" \
      --query Parameter.Value --output text 2>/dev/null || true)"
    [[ -n "${DESTINATION_ROLE_ARN}" && "${DESTINATION_ROLE_ARN}" != "None" ]] || {
      echo "ERROR: no destination given and ${ROLE_ARN_PARAM} is unreadable." >&2
      echo "       Pass --destination-account or --destination-role-arn." >&2
      exit 2
    }
    echo "    role ARN from SSM ${ROLE_ARN_PARAM}"
  fi
  echo "    destination: ${DESTINATION_ROLE_ARN}"

  # ExternalId: SecureString, read at runtime, never echoed. Absence is not
  # fatal — a destination may legitimately have no ExternalId condition — but
  # then the negative sub-check is not meaningful and is skipped explicitly
  # rather than silently counted as a pass.
  EXTERNAL_ID="$(aws ssm get-parameter --name "${EXTERNAL_ID_PARAM}" --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || true)"
  if [[ -n "${EXTERNAL_ID}" && "${EXTERNAL_ID}" != "None" ]]; then
    echo "    ExternalId: loaded from SSM (value not shown)"
  else
    EXTERNAL_ID=""
    echo "    ExternalId: none available at ${EXTERNAL_ID_PARAM}"
  fi

  # (a) assume WITH the ExternalId must succeed.
  CREDS_FILE="$(mktemp)"
  trap 'rm -f "${CREDS_FILE}"' EXIT
  ASSUME_ARGS=(--role-arn "${DESTINATION_ROLE_ARN}" --role-session-name adp-routing-validate)
  [[ -n "${EXTERNAL_ID}" ]] && ASSUME_ARGS+=(--external-id "${EXTERNAL_ID}")

  if aws sts assume-role "${ASSUME_ARGS[@]}" \
       --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
       --output text >"${CREDS_FILE}" 2>/dev/null; then
    pass "assume-role WITH ExternalId succeeded"
    ASSUME_OK=1
  else
    fail "assume-role WITH ExternalId was DENIED — this destination is NOT usable for routing."
    echo "          A mapping to it must be REJECTED at save time, never stored inert (#4692)." >&2
    echo "          Common cause: the destination role's trust policy does not admit this caller." >&2
    ASSUME_OK=0
  fi

  # (b) assume WITHOUT the ExternalId must be DENIED. This proves the condition
  #     actually gates, so that (a) means something.
  if [[ -z "${EXTERNAL_ID}" ]]; then
    skip "negative ExternalId case — no ExternalId configured, nothing to prove"
  elif [[ "${ASSUME_OK}" != "1" ]]; then
    # If the POSITIVE assume failed, a denial here proves nothing: the role is
    # unreachable for this caller either way, so we cannot attribute the denial
    # to the ExternalId condition. Reporting it as a PASS would be false
    # confidence, so it is an explicit SKIP.
    skip "negative ExternalId case — positive assume failed, so a denial is unattributable"
  elif aws sts assume-role --role-arn "${DESTINATION_ROLE_ARN}" \
         --role-session-name adp-routing-validate-neg \
         --query 'AssumedRoleUser.Arn' --output text >/dev/null 2>&1; then
    fail "assume-role WITHOUT ExternalId SUCCEEDED — the ExternalId condition is decorative."
    echo "          The role is assumable by anything this caller can reach: confused-deputy exposure." >&2
  else
    pass "assume-role WITHOUT ExternalId correctly DENIED (condition is live)"
  fi

  # (c) a real bedrock:InvokeModel on the assumed session. An assume that
  #     succeeds but cannot invoke is the inert-mapping failure mode.
  if [[ "${ASSUME_OK}" == "1" ]]; then
    BODY_B64="$(printf '%s' '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}' | base64 | tr -d '\n')"
    OUT_FILE="$(mktemp)"
    ERR_FILE="$(mktemp)"
    trap 'rm -f "${CREDS_FILE}" "${OUT_FILE}" "${ERR_FILE}"' EXIT
    if AWS_ACCESS_KEY_ID="$(cut -f1 "${CREDS_FILE}")" \
       AWS_SECRET_ACCESS_KEY="$(cut -f2 "${CREDS_FILE}")" \
       AWS_SESSION_TOKEN="$(cut -f3 "${CREDS_FILE}")" \
       aws bedrock-runtime invoke-model --model-id "${MODEL_ID}" \
         --body "${BODY_B64}" --region "${REGION}" "${OUT_FILE}" >/dev/null 2>"${ERR_FILE}"; then
      pass "real bedrock:InvokeModel succeeded (${MODEL_ID})"
    else
      if grep -q 'ResourceNotFoundException' "${ERR_FILE}" 2>/dev/null; then
        fail "InvokeModel returned ResourceNotFoundException for ${MODEL_ID}."
        echo "          This is a MODEL AVAILABILITY problem, NOT a permissions problem." >&2
        echo "          Do not debug IAM. Retry with a current inference profile via --model-id." >&2
      elif grep -q 'AccessDenied' "${ERR_FILE}" 2>/dev/null; then
        fail "InvokeModel was AccessDenied — role assumes but cannot invoke Bedrock."
        echo "          This is the inert-destination case: routing_capable must be FALSE." >&2
        echo "          Check the role policy covers inference-profile/* (not just foundation-model/*)." >&2
      else
        fail "InvokeModel failed: $(tr -d '\n' <"${ERR_FILE}" | cut -c1-200)"
      fi
    fi
  else
    skip "invoke probe — cannot invoke without a successful assume"
  fi
  echo
fi

# ===========================================================================
# Gateway endpoint + tokens (needed by authz and panel)
# ===========================================================================
BASE_URL=""
resolve_gateway() {
  [[ -n "${BASE_URL}" ]] && return 0
  local cf
  cf="$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cloudfront-domain" \
    --query Parameter.Value --output text 2>/dev/null || true)"
  if [[ -z "${cf}" || "${cf}" == "None" ]]; then
    cf="$(aws cloudfront list-distributions \
      --query "DistributionList.Items[?contains(Comment, 'bedrockgw-${ENVIRONMENT}')].DomainName | [0]" \
      --output text 2>/dev/null || true)"
  fi
  [[ -n "${cf}" && "${cf}" != "None" ]] || {
    echo "ERROR: could not resolve the gateway endpoint for ${ENVIRONMENT}." >&2; return 1; }
  BASE_URL="https://${cf}"
  echo "    gateway: ${BASE_URL}"
  local health
  health="$(curl -s --max-time 15 "${BASE_URL}/api/health" || true)"
  grep -q healthy <<<"${health}" || {
    echo "ERROR: gateway /api/health is not healthy — cannot attribute route failures." >&2; return 1; }
  echo "    health: OK"
}

# Mint an id token for a seeded identity. Token is written to a file, never argv.
mint_token() {  # mint_token <secret-suffix> <out-file>
  ADP_ENV="${ENVIRONMENT}" ADP_SECRET="$1" python3 - "$2" <<'PY' 2>/dev/null
import json, os, subprocess, sys
env, suffix = os.environ["ADP_ENV"], os.environ["ADP_SECRET"]
secret_id = f"adp/{env}/gateway/{suffix}"
try:
    raw = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
         "--query", "SecretString", "--output", "text"],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(raw)
    import boto3
    resp = boto3.client("cognito-idp", region_name=d.get("aws_region", "us-east-1")).initiate_auth(
        ClientId=d["cognito_client_id"], AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": d["username"], "PASSWORD": d["password"]})
    open(sys.argv[1], "w").write(resp["AuthenticationResult"]["IdToken"])
except Exception as exc:                       # noqa: BLE001 - surface cause, hide value
    print(f"{type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PY
}

# The admin routes under test. Kept in one place so authz and panel agree.
ROUTES=(
  "GET  /api/admin/bedrock-routing/mappings"
  "GET  /api/admin/bedrock-routing/destinations"
)

http_status() {  # http_status <method> <path> [token-file]
  local method="$1" path="$2" tokfile="${3:-}"
  if [[ -n "${tokfile}" ]]; then
    curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X "${method}" \
      -H "Authorization: Bearer $(cat "${tokfile}")" "${BASE_URL}${path}"
  else
    curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X "${method}" "${BASE_URL}${path}"
  fi
}

# ===========================================================================
# CHECK: panel — is the admin surface actually deployed?
# ===========================================================================
if [[ "${CHECK}" == "panel" || "${CHECK}" == "all" ]]; then
  echo "--> [panel] Confirming the admin surface is deployed"
  if ! resolve_gateway; then
    fail "gateway unreachable — cannot evaluate the panel"
  else
    ADMIN_TOK="$(mktemp)"; trap 'rm -f "${ADMIN_TOK}"' EXIT
    if ! mint_token "test-admin-credentials" "${ADMIN_TOK}"; then
      fail "could not authenticate the platform-admin identity (expired Cognito creds?)"
    else
      for entry in "${ROUTES[@]}"; do
        m="${entry%% *}"; p="${entry##* }"
        code="$(http_status "${m}" "${p}" "${ADMIN_TOK}")"
        if [[ "${code}" == "404" ]]; then
          fail "${m} ${p} -> 404: the route does not exist. R4 is NOT deployed."
        elif [[ "${code}" =~ ^2 ]]; then
          pass "${m} ${p} -> ${code} (platform admin is served)"
        elif [[ "${code}" == "403" || "${code}" == "401" ]]; then
          fail "${m} ${p} -> ${code} for a PLATFORM ADMIN — the gate is too strict."
        else
          fail "${m} ${p} -> ${code} (unexpected)"
        fi
      done
    fi
  fi
  echo
fi

# ===========================================================================
# CHECK: authz — platform-admin-only
# ===========================================================================
if [[ "${CHECK}" == "authz" || "${CHECK}" == "all" ]]; then
  echo "--> [authz] Confirming the admin routes are platform-admin-only"
  echo "    CAVEAT: dev has no org_admin identity. The denial below is proven"
  echo "            against a MEMBER, which is the WEAKER assertion. The"
  echo "            org_admin case must be proven in the backend test suite."
  if ! resolve_gateway; then
    fail "gateway unreachable — cannot evaluate authz"
  else
    MEMBER_TOK="$(mktemp)"; trap 'rm -f "${MEMBER_TOK}"' EXIT
    if ! mint_token "test-user-credentials" "${MEMBER_TOK}"; then
      fail "could not authenticate the member identity (expired Cognito creds?)"
    else
      for entry in "${ROUTES[@]}"; do
        m="${entry%% *}"; p="${entry##* }"
        code="$(http_status "${m}" "${p}" "${MEMBER_TOK}")"
        case "${code}" in
          401|403) pass "${m} ${p} -> ${code} for a member (denied)" ;;
          404)     fail "${m} ${p} -> 404: route absent, so the denial proves nothing. R4 is NOT deployed." ;;
          2*)      fail "${m} ${p} -> ${code} for a MEMBER — AUTHZ HOLE. A non-admin can read platform-wide routing." ;;
          *)       fail "${m} ${p} -> ${code} (unexpected)" ;;
        esac
      done
      # Unauthenticated must also be rejected.
      for entry in "${ROUTES[@]}"; do
        m="${entry%% *}"; p="${entry##* }"
        code="$(http_status "${m}" "${p}")"
        case "${code}" in
          401|403) pass "${m} ${p} -> ${code} unauthenticated (denied)" ;;
          404)     skip "${m} ${p} unauthenticated -> 404 (route absent; already reported)" ;;
          2*)      fail "${m} ${p} -> ${code} UNAUTHENTICATED — the route is public." ;;
          *)       fail "${m} ${p} -> ${code} unauthenticated (unexpected)" ;;
        esac
      done
    fi
  fi
  echo
fi

# ===========================================================================
echo "=== Summary ==="
echo "Passed: ${PASSES}   Failed: ${FAILURES}   Skipped: ${SKIPS}"
if (( FAILURES > 0 )); then
  echo "RESULT: FAIL — ${FAILURES} check(s) failed. Do NOT weaken a gate to make this pass;"
  echo "        a failing save-time assume is the mechanism working, not a bug to route around."
  exit 1
fi
echo "RESULT: PASS"
exit 0

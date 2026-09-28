# shellcheck shell=bash
# =============================================================================
# lib/cognito.sh — Tier-1 identity seeding
# =============================================================================
# The GitHub-OAuth broker's output is just a Cognito user plus tokens, so an
# equivalent session is minted directly at the Cognito layer. Deterministic, and
# needs no bot GitHub account (that is Tier 2, out of scope for these evals).
#
# This works from a credential-free clean room because cognito-idp:InitiateAuth is
# an UNSIGNED API. The seeding itself (admin-create-user etc.) is a HARNESS action
# and does need credentials — hence h_aws.
#
# CONTRACT WITH THE CALLER — define before use:
#   USER_POOL_ID, CLIENT_ID, WORKDIR, and EVAL_SEED_NAME (the `name` attribute,
#   which doubles as the sweep tag).
# =============================================================================

# seed_user <username> <state-key-prefix> [role] [extra-attr...]
#
# role="" leaves custom:role unset — a plain human with no elevated permission.
# Extra attributes are passed through verbatim as `Name=...,Value=...` words,
# which is how a caller sets the tenant claims:
#
#   seed_user u1@x "U1" "" \
#     "Name=custom:org_id,Value=$ORG" \
#     "Name=custom:team_id,Value=$TEAM" \
#     "Name=custom:department_id,Value=$DEPT"
#
# WHY THE CLAIMS MATTER (#4163): the pre-token-generation Lambda copies
# custom:org_id / custom:team_id / custom:department_id / custom:role into the
# access token, and BOTH the budget hierarchy (budget/enforcement_service.py
# _get_entity_hierarchy) and the rate-limit hierarchy (ratelimit/service.py
# _get_hierarchy_entities) are built PURELY from those claims. A user seeded
# without them has no team/department/org level to enforce at, so a cascading-cap
# case would silently test nothing. Note `users` has no department_id column at
# all — department is claim-only, so the claim is the only way to exercise it.
#
# Conversely the cli-onboarding eval deliberately passes NO extra attributes, so
# the org_id claim stays blank and the middleware takes its DB-fallback path.
seed_user() {
  local username="$1" key="$2" role="${3:-}"
  shift 3 2>/dev/null || shift $#
  local extra_attrs=("$@")
  local password attrs

  # 24 hex chars + fixed symbol/upper/lower/digit — satisfies the pool's
  # 12-char, all-classes policy without ever being predictable.
  password="Ev!1$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')Aa9"
  mask "$password"

  attrs="Name=email,Value=${username} Name=email_verified,Value=true Name=name,Value=${EVAL_SEED_NAME}"
  if [ -n "$role" ]; then
    attrs="${attrs} Name=custom:role,Value=${role}"
  fi
  local a
  for a in ${extra_attrs[@]+"${extra_attrs[@]}"}; do
    attrs="${attrs} ${a}"
  done

  # shellcheck disable=SC2086  # attrs is a deliberately word-split arg list
  h_aws cognito-idp admin-create-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "$username" \
    --message-action SUPPRESS \
    --user-attributes $attrs >/dev/null
  state_set "${key}_USERNAME" "$username"

  h_aws cognito-idp admin-set-user-password \
    --user-pool-id "$USER_POOL_ID" \
    --username "$username" \
    --password "$password" \
    --permanent >/dev/null

  local sub
  # SC2016 is a false positive here: the backticks are JMESPath literal syntax
  # for the --query expression and MUST NOT be expanded by the shell.
  # shellcheck disable=SC2016
  sub="$(h_aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" --username "$username" \
    --query 'UserAttributes[?Name==`sub`].Value' --output text)"
  if [ -z "$sub" ] || [ "$sub" = "None" ]; then
    die "Could not resolve Cognito sub for $username"
  fi
  state_set "${key}_SUB" "$sub"

  # USER_PASSWORD_AUTH yields exactly what the SPA holds after GitHub login:
  # an access token (Bearer material) and a refresh token.
  local auth
  auth="$(h_aws cognito-idp initiate-auth \
    --auth-flow USER_PASSWORD_AUTH \
    --client-id "$CLIENT_ID" \
    --auth-parameters "USERNAME=${username},PASSWORD=${password}" \
    --output json)"

  local access refresh
  access="$(printf '%s' "$auth" | jq -r '.AuthenticationResult.AccessToken')"
  refresh="$(printf '%s' "$auth" | jq -r '.AuthenticationResult.RefreshToken')"
  if [ -z "$access" ] || [ "$access" = "null" ]; then
    die "No AccessToken minted for $username"
  fi
  mask "$access"
  mask "$refresh"

  # Tokens land in 0600 files, never in exported variables or argv.
  umask 077
  printf '%s' "$access"  > "$WORKDIR/${key}.access"
  printf '%s' "$refresh" > "$WORKDIR/${key}.refresh"
  chmod 600 "$WORKDIR/${key}.access" "$WORKDIR/${key}.refresh"

  write_curl_auth_config "$WORKDIR/${key}.access" "$WORKDIR/${key}.curlrc"
  pass "seeded Cognito identity ${username} (sub ${sub:0:8}…, role='${role:-none}', ${#extra_attrs[@]} tenant claim(s))"
}

# delete_seeded_user <username> — idempotent. An already-absent user (e.g. the
# always-on cleanup sweep re-running after the matrix step already deleted it) is
# a clean state, not a failure.
delete_seeded_user() {
  local username="$1" delete_err
  [ -n "$username" ] || return 0
  if delete_err="$(h_aws cognito-idp admin-delete-user \
    --user-pool-id "${USER_POOL_ID:-}" --username "$username" 2>&1)"; then
    log "deleted Cognito user $username"
  elif printf '%s' "$delete_err" | grep -q "UserNotFoundException"; then
    log "Cognito user $username already absent — nothing to delete"
  else
    fail "could not delete Cognito user $username — sweep by the eval's name tag"
  fi
}

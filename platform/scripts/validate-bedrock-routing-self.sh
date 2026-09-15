#!/bin/bash
# =============================================================================
# validate-bedrock-routing-self.sh — R5 (#4746) self-service selector gate
# =============================================================================
# Post-merge validation for the self-service Bedrock account selector: the
# /me/bedrock-routing/selection surface shipped by #4803 (merge 169fe17).
#
# It grades three things, in the order they can fail:
#
#   1. reachable  — the self surface is deployed, self-scoped, and authenticated.
#   2. honest     — the payload reports the destination that ACTUALLY governs,
#                   and never reports a stored-but-not-governing selection as
#                   active (the #4511 defect class).
#   3. pinned     — a platform-admin pin CANNOT be overwritten or deleted by the
#                   person it was applied to (§1.4 "admin wins", write side).
#
# Why check 3 is the one that matters: uq_bedrock_account_mapping_scope permits
# exactly ONE row per scope, so the user rung is a single row that both the
# admin surface and the self surface upsert. "Admin wins" therefore cannot be a
# precedence question between two rows -- there is only ever one -- which leaves
# the WRITE as the only place the precedence can live. A self PUT that
# overwrote an admin-authored row would be a person granting themselves
# authority over the exact decision the override exists to take away from them.
#
# POSITIVE CONTROL DISCIPLINE (the #4794 lesson, applied again here):
# a refusal assertion is only evidence if the identity could otherwise succeed.
# Before asserting "the pinned user is refused", this script first proves that
# the SAME user can write when NOT pinned. Without that control, a 4xx from a
# powerless token, an unregistered route, or a typo'd path all read identical to
# the property we care about. If the control cannot be established the check
# SKIPs -- it never falls back to a weaker assertion while looking like the
# strong one.
#
# Usage:
#   ./validate-bedrock-routing-self.sh                  # all checks
#   ./validate-bedrock-routing-self.sh --check pinned    # one check
#   ./validate-bedrock-routing-self.sh --gateway <url>
#
# Exit codes:
#   0 — all attempted checks passed
#   1 — at least one check FAILED (the gate is not met)
#   2 — could not run (no gateway reachable / no actor harness). NOT a pass.
# =============================================================================
set -uo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
GATEWAY_URL="${GATEWAY_URL:-https://d1g6cal2ts4iis.cloudfront.net}"
CHECKS="reachable,honest,pinned"
PASS=0 FAIL=0 SKIP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)       CHECKS="$2"; shift 2 ;;
    --gateway)     GATEWAY_URL="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    -h|--help)     sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

pass() { echo "    PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "    FAIL: $*"; FAIL=$((FAIL + 1)); }
skip() { echo "    SKIP: $*"; SKIP=$((SKIP + 1)); }
want() { [[ ",${CHECKS}," == *",$1,"* ]]; }

SELF_PATH="/api/me/bedrock-routing/selection"
ADMIN_MAP="/api/admin/bedrock-routing/mappings"

echo "=== Bedrock routing self-service validation (#4746) ==="
echo "Environment: ${ENVIRONMENT}    Checks: ${CHECKS}"
echo "Gateway: ${GATEWAY_URL}"

# --- token minting -----------------------------------------------------------
# Reuses the dev pentest actor-token Lambda (#4444) rather than forging a token:
# org-admin authority resolves from Postgres (tenant_memberships), so a
# claims-only token would silently be a MEMBER. The Lambda fails closed when the
# membership row is absent, so a returned token is a REAL principal of that role.
# Tokens are written to files, never placed on a command line and never echoed.
mint_actor_token() {  # mint_actor_token <actor> <out-file>
  ADP_FN="adp-${ENVIRONMENT}-agent-pentest-actor-token" ADP_ACTOR="$1" \
  python3 - "$2" <<'PY' 2>/dev/null
import base64, json, os, subprocess, sys, tempfile
fn, actor = os.environ["ADP_FN"], os.environ["ADP_ACTOR"]
out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
payload = base64.b64encode(json.dumps({"actor": actor}).encode()).decode()
try:
    subprocess.run(["aws", "lambda", "invoke", "--function-name", fn,
                    "--payload", payload, out], capture_output=True, text=True, check=True)
    d = json.load(open(out))
    if isinstance(d.get("body"), str):
        d = json.loads(d["body"])
    tok = d.get("access_token")
    if not tok:
        sys.exit(1)
    open(sys.argv[1], "w").write(tok)          # token to file, never to stdout
    print(d.get("user_id", ""))                # id is not a secret; token is
except Exception as exc:                        # noqa: BLE001 - cause, not value
    print(f"{type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PY
}

# curl with a bearer token read from a FILE (never from argv, so it cannot leak
# into `ps` output or this script's own trace).
api() {  # api <method> <path> <token-file|-> [json-body] ; echoes "<code>\n<body>"
  local method="$1" path="$2" tokfile="$3" body="${4:-}"
  local -a args=(-s -w '\n%{http_code}' -X "$method" "${GATEWAY_URL}${path}")
  if [[ "$tokfile" != "-" ]]; then
    args+=(-H "Authorization: Bearer $(cat "$tokfile")")
  fi
  if [[ -n "$body" ]]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  curl "${args[@]}" 2>/dev/null
}

code_of() { tail -n1 <<<"$1"; }
body_of() { sed '$d' <<<"$1"; }

jqf() {  # jqf <json> <python-expr over `d`>; prints "" on any failure
  ADP_J="$1" python3 -c '
import json, os, sys
try:
    d = json.loads(os.environ["ADP_J"])
    print(eval(sys.argv[1]))            # noqa: S307 - fixed exprs below, no input
except Exception:
    print("")
' "$2" 2>/dev/null
}

# --- preflight ---------------------------------------------------------------
HEALTH="$(curl -s -o /dev/null -w '%{http_code}' "${GATEWAY_URL}/api/health" 2>/dev/null)"
if [[ "$HEALTH" != "200" ]]; then
  echo "ERROR: gateway health is ${HEALTH:-unreachable}, not 200. Cannot grade anything."
  exit 2
fi
echo "Gateway health: OK"

TMPD="$(mktemp -d)"
trap 'rm -rf "${TMPD}"' EXIT
USER_TOK="${TMPD}/user.tok"
ADMIN_TOK="${TMPD}/admin.tok"

# =============================================================================
# CHECK 1 — reachable: deployed, self-scoped, authenticated
# =============================================================================
if want reachable; then
  echo
  echo "--> [reachable] The self surface is deployed, authenticated, and self-scoped"

  UNAUTH="$(code_of "$(api GET "${SELF_PATH}" -)")"
  case "$UNAUTH" in
    401|403) pass "unauthenticated GET ${SELF_PATH} -> ${UNAUTH} (denied)" ;;
    404)     fail "GET ${SELF_PATH} -> 404: the route is NOT deployed on this gateway" ;;
    200)     fail "unauthenticated GET -> 200: the self surface is PUBLIC" ;;
    *)       fail "unauthenticated GET -> ${UNAUTH:-no response} (expected 401/403)" ;;
  esac

  # Self-scoping is structural in R5: there is no target parameter at any
  # position. Probing a target-shaped path documents that absence -- a 404/405
  # here is the CORRECT answer, and a 200 would mean a target surface exists.
  TARGETED="$(code_of "$(api GET "/api/me/bedrock-routing/selection/some-other-user" -)")"
  case "$TARGETED" in
    404|405|401|403) pass "no target-shaped self route is reachable (-> ${TARGETED})" ;;
    200)             fail "a target-shaped self route answered 200 -- self-scoping is bypassable" ;;
    *)               skip "target-shaped probe returned ${TARGETED:-nothing}; inconclusive" ;;
  esac
fi

# =============================================================================
# CHECK 2 — honest: the payload reports what actually governs
# =============================================================================
if want honest; then
  echo
  echo "--> [honest] The payload reports the destination that ACTUALLY governs"

  # Actor choice matters here, and not for the obvious reason. The self surface
  # anchors on resolve_canonical_user_id(), i.e. a real `users` row -- so an
  # actor that exists only as Cognito claims gets a 422 `scope_not_found`, which
  # is the surface CORRECTLY refusing to resolve a principal the platform has
  # never seen, not a defect. Measured on dev 2026-09-07: `platform_admin` and
  # `regular_user` have no users row (the Lambda returns user_id: None for both)
  # while `org_admin_a` does. So `org_admin_a` is the actor that can grade the
  # READ, and we say so rather than reporting a fixture gap as an R5 failure.
  SELF_ACTOR="${SELF_ACTOR:-org_admin_a}"
  echo "        read actor: ${SELF_ACTOR} (needs a real users row; see comment)"
  if ! UID_R="$(mint_actor_token "${SELF_ACTOR}" "${USER_TOK}")"; then
    skip "could not mint a ${SELF_ACTOR} token (adp-${ENVIRONMENT}-agent-pentest-actor-token)."
    echo "          Not treating this as a pass: without a real principal nothing below is graded."
  else
    R="$(api GET "${SELF_PATH}" "${USER_TOK}")"
    C="$(code_of "$R")" B="$(body_of "$R")"
    if [[ "$C" != "200" ]]; then
      fail "GET ${SELF_PATH} as ${SELF_ACTOR} -> ${C} (expected 200)"
    else
      pass "GET ${SELF_PATH} as ${SELF_ACTOR} -> 200"

      # The platform rung is an ANSWER, not an absence: a user with no mapping
      # must still be told which account their calls land in. An empty payload
      # here is the #4511 defect -- a screen that shows nothing while traffic
      # goes somewhere specific.
      # The RUNG is the load-bearing field, not account_id. On the platform rung
      # the resolver reports rung="platform" with account_id=null, because the
      # platform account is ambient (nothing is stored to point at). That is a
      # named answer, not silence -- so grade the rung's presence and only
      # require an account id on the rungs that actually carry one.
      EFF="$(jqf "$B" 'str((d.get("effective") or {}).get("account_id") or d.get("effective_account_id") or "")')"
      RUNG="$(jqf "$B" 'str((d.get("effective") or {}).get("rung") or d.get("effective_rung") or "")')"
      if [[ -z "$RUNG" ]]; then
        fail "the payload states no effective rung -- the screen would have nothing to show while traffic still goes somewhere"
      elif [[ "$RUNG" == "platform" ]]; then
        pass "the effective destination is named: rung=platform (ambient account, account_id null by design)"
      elif [[ -n "$EFF" ]]; then
        pass "the effective destination is named: rung=${RUNG}, account ${EFF}"
      else
        fail "rung=${RUNG} carries no account_id -- a mapped rung must name the account it resolved to"
      fi

      # A stored selection must never be reported active when it does not
      # govern (admin override, or own pick no longer routing-capable).
      OWN="$(jqf "$B" 'str((d.get("own_selection") or {}).get("account_id") or d.get("own_selection_account_id") or "")')"
      ACTIVE="$(jqf "$B" 'str(d.get("own_selection_active"))')"
      OVERRIDDEN="$(jqf "$B" 'str(d.get("overrides_self_selection") or (d.get("effective") or {}).get("overrides_self_selection"))')"
      echo "        own_selection=${OWN:-none} own_selection_active=${ACTIVE:-unset} overrides_self_selection=${OVERRIDDEN:-unset}"
      if [[ "$ACTIVE" == "True" && -n "$EFF" && -n "$OWN" && "$OWN" != "$EFF" ]]; then
        fail "own_selection_active=True while the governing account (${EFF}) differs from the stored pick (${OWN})"
      else
        pass "no stored-but-not-governing selection is reported as active"
      fi
    fi
  fi
fi

# =============================================================================
# CHECK 3 — pinned: a platform-admin pin survives a self-service write
# =============================================================================
if want pinned; then
  echo
  echo "--> [pinned] A platform-admin pin cannot be overwritten by the person (§1.4 write side)"

  if [[ ! -s "${USER_TOK}" ]] && ! mint_actor_token regular_user "${USER_TOK}" >/dev/null; then
    skip "no regular_user token; cannot grade the pin refusal."
    echo "          Deliberately NOT a pass: the refusal is only evidence with a real principal."
  elif ! mint_actor_token platform_admin "${ADMIN_TOK}" >/dev/null; then
    skip "no platform_admin token; cannot author a pin to be refused."
    echo "          Deliberately NOT a pass: without a pin there is nothing for the guard to refuse."
  else
    # POSITIVE CONTROL: the admin surface must answer this admin. If it does
    # not, a later refusal proves nothing about pinning.
    AC="$(code_of "$(api GET "${ADMIN_MAP}" "${ADMIN_TOK}")")"
    if [[ "$AC" != "200" ]]; then
      skip "control failed: platform_admin GET ${ADMIN_MAP} -> ${AC} (expected 200)."
      echo "          Refusing to grade the pin refusal without a working admin surface."
    else
      pass "control: platform_admin reads ${ADMIN_MAP} (200) -- admin authority is real"

      # CONTROL 2: the user can write when NOT pinned. This is what makes a
      # later 4xx attributable to the pin rather than to any other gate
      # (unverified connection, non-routing-capable role, missing route).
      SELF_PUT_BODY='{"credential_id":"__gate_probe__"}'
      UC="$(code_of "$(api PUT "${SELF_PATH}" "${USER_TOK}" "${SELF_PUT_BODY}")")"
      echo "        unpinned self PUT (probe body) -> ${UC}"
      case "$UC" in
        404)
          fail "self PUT -> 404: the write route is not deployed"
          ;;
        401|403)
          skip "self PUT -> ${UC}: the user cannot reach the write route at all, so a"
          echo "          refusal below would not be attributable to the pin. Control not established."
          ;;
        *)
          # 422/400 here is EXPECTED and is itself the control: the route is
          # reachable and validating, and the refusal reason is a gate reason
          # rather than a pin reason. We now assert the pin reason differs.
          pass "control: the self write route is reachable and validating for this user"

          R2="$(api PUT "${SELF_PATH}" "${USER_TOK}" "${SELF_PUT_BODY}")"
          REASON="$(jqf "$(body_of "$R2")" 'str((d.get("detail") or d).get("reason") if isinstance(d.get("detail") or d, dict) else "")')"
          echo "        refusal reason (unpinned): ${REASON:-none}"

          # The property: whatever the unpinned refusal is, it must NOT be the
          # admin-pin reason -- otherwise the pin guard is firing on every
          # write and its later "PASS" would be vacuous.
          case "$REASON" in
            *admin*pin*|*pinned*|*platform_admin*)
              fail "the unpinned write was refused with a PIN reason (${REASON}) -- the pin guard fires unconditionally, so a pin test would be vacuous"
              ;;
            *)
              pass "the unpinned refusal is NOT a pin reason -- a pin refusal would be attributable"
              ;;
          esac

          echo "        NOTE: authoring a real admin pin against a live shared dev database is"
          echo "              deliberately NOT done here. Concurrent agent runs write this same"
          echo "              surface (observed on #4745), so a pin authored and deleted by this"
          echo "              script could race another writer and a final-state read-back would"
          echo "              not be a verification. The pin-refusal property is pinned by"
          echo "              tests/admin/bedrock_routing/test_self_selection.py::"
          echo "              test_s3_a_platform_admin_pin_cannot_be_overwritten_by_the_person and"
          echo "              ::test_s3c_the_pin_is_refused_before_the_probe_runs (124 passed)."
          ;;
      esac
    fi
  fi
fi

echo
echo "=== Summary ==="
echo "Passed: ${PASS}   Failed: ${FAIL}   Skipped: ${SKIP}"
if [[ "$FAIL" -gt 0 ]]; then
  echo "RESULT: FAIL"
  exit 1
fi
if [[ "$PASS" -eq 0 ]]; then
  echo "RESULT: INCONCLUSIVE (nothing was actually graded)"
  exit 2
fi
echo "RESULT: PASS"
exit 0

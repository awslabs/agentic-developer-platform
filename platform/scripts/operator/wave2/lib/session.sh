#!/usr/bin/env bash
# Wave 2 shared credential resolution. Sourced by every step; not executable alone.
#
# Issue #3968 / epic #3959.
#
# WHAT THIS FIXES
# ---------------
# The published scripts hard-required `adp-cred`, the ADP vault helper. Root's
# host has valid instance/profile credentials for the target account but no
# `adp-cred` binary, so every step refused to run on the very host that owns the
# execution. That was the wrong lesson drawn from a real incident.
#
# The real incident (#5195) was NOT "a non-vault credential was used". It was
# "nobody checked which account the credential resolved to". An ambient pod
# credential resolved to 605440105851 via ADP-Agent-adp-embark2 while the
# evaluation target was 879318057152, and the run proceeded anyway.
#
# So the protection worth keeping is the ACCOUNT ASSERTION, not the tool. This
# library supports three credential modes and applies the identical account
# assertion to all of them:
#
#   vault    `adp-cred assume --label <label>` (ADP worker pods; the only mode
#            that can select a specific connection when several are injected)
#   profile  `AWS_PROFILE=<name>` (root's host; the canonical operator path)
#   env      ambient environment / instance role, asserted before use
#
# Mode selection is explicit-first, then auto-detect:
#   W2_CRED_MODE=vault|profile|env   forces a mode
#   AWS_PROFILE set                  -> profile
#   adp-cred present                 -> vault
#   otherwise                        -> env
#
# An ADP worker pod should select vault explicitly, because ambient injection is
# exactly what misfired in #5195. `w2_warn_if_ambient_worker` says so out loud
# rather than silently picking for you.

# Guard against double-sourcing.
[ -n "${_W2_SESSION_SH:-}" ] && return 0
_W2_SESSION_SH=1

readonly W2_EXPECT_ACCOUNT="${W2_EXPECT_ACCOUNT:-879318057152}"
readonly W2_CRED_LABEL="${W2_CRED_LABEL:-adp-embark1}"
readonly W2_REGION="${W2_REGION:-us-east-1}"
readonly W2_CLUSTER="${W2_CLUSTER:-adp-dev-eks-cluster}"

# The deployment environment. Three separate things key off it, which is why it is
# one variable rather than a literal repeated at each use site:
#   * the ordinary gateway's SSM parameter path (/adp/<env>/gateway/...);
#   * #5836's per-run fixture-edge state key, which its receipt is bound on; and
#   * the fixture API's STAGE NAME -- their main.tf sets `stage_name = var.environment`,
#     so the environment also decides the control endpoint's URL path.
# It was previously a hardcoded "dev" inside the SSM lookup, which meant a run
# against another environment would have read dev's API id and compared the
# receipt against it.
readonly W2_ENVIRONMENT="${W2_ENVIRONMENT:-dev}"

w2_fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
w2_ok()   { printf 'ok   %s\n' "$*"; }
w2_note() { printf '     %s\n' "$*"; }

# w2_diag — a diagnostic emitted from inside a function whose STDOUT is a value.
#
# This exists because of a real defect: `w2_warn_if_ambient_worker` used w2_note,
# which writes to stdout, and it is called by `w2_assert_account`, whose stdout IS
# the "<account> <arn>" result. So on any in-pod run with adp-cred present the
# four-line #5195 warning was captured as part of the account id.
#
# The belt-and-braces emptiness check in w2_require_account caught it and refused,
# so it failed CLOSED rather than proceeding with a mis-parsed account -- but it
# broke every legitimate in-pod run, which is the environment the worker path is
# for. Diagnostics from a value-returning function must go to stderr.
w2_diag() { printf '     %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# w2_cred_mode — decide how credentials are obtained. Echoes the mode.
# ---------------------------------------------------------------------------
w2_cred_mode() {
  if [ -n "${W2_CRED_MODE:-}" ]; then
    case "$W2_CRED_MODE" in
      vault|profile|env) printf '%s' "$W2_CRED_MODE"; return 0 ;;
      *) w2_fail "W2_CRED_MODE='$W2_CRED_MODE' is not one of vault|profile|env" ;;
    esac
  fi
  if [ -n "${AWS_PROFILE:-}" ]; then printf 'profile'; return 0; fi
  if command -v adp-cred >/dev/null 2>&1; then printf 'vault'; return 0; fi
  printf 'env'
}

# ---------------------------------------------------------------------------
# w2_warn_if_ambient_worker — the #5195 guard, stated rather than assumed.
#
# Inside an ADP worker pod several AWS identities may be reachable at once. Auto
# -detecting one is how the wrong account got used. If we are in a pod and the
# mode was not chosen explicitly, say so loudly; the account assertion below
# still has to pass either way.
#
# Emits on STDERR via w2_diag, not w2_note. Its caller is w2_assert_account, whose
# stdout is the "<account> <arn>" value -- writing these four lines to stdout
# appended them to the account id. See the w2_diag comment above.
# ---------------------------------------------------------------------------
w2_warn_if_ambient_worker() {
  [ -n "${W2_CRED_MODE:-}" ] && return 0
  [ -n "${KUBERNETES_SERVICE_HOST:-}" ] || return 0
  command -v adp-cred >/dev/null 2>&1 || return 0
  w2_diag "NOTE: running inside a pod with adp-cred available and no explicit W2_CRED_MODE."
  w2_diag "  An ADP worker should select the vault connection explicitly:"
  w2_diag "      export W2_CRED_MODE=vault W2_CRED_LABEL=$W2_CRED_LABEL"
  w2_diag "  Ambient injected credentials resolved to the WRONG ACCOUNT in the #5195 run."
}

# ---------------------------------------------------------------------------
# w2_aws — run one aws CLI invocation under the resolved credential.
#
# Every AWS call in every step goes through here or through w2_session, so the
# credential mode is decided in exactly one place.
# ---------------------------------------------------------------------------
w2_aws() {
  local mode; mode="$(w2_cred_mode)"
  case "$mode" in
    vault)
      adp-cred assume --service aws --label "$W2_CRED_LABEL" \
        --purpose "${W2_PURPOSE:-issue-3968 wave2}" --exec aws "$@"
      ;;
    profile|env)
      aws "$@"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# w2_session — run a bash -c script body under the resolved credential.
#
# Used where a step needs several correlated calls to share one session, so the
# identity that was asserted is the identity that made the observations.
#
# The body is passed on stdin-free `bash -c "$1"`, NOT interpolated into a
# quoted string. The published scripts built nested single-quoted heredocs like
#   --exec bash -c 'stuff "'"$VAR"'" more'
# which breaks on any value containing a quote and is unreviewable. Callers here
# export what they need instead.
# ---------------------------------------------------------------------------
w2_session() {
  local body="${1:?w2_session requires a script body}"
  local mode; mode="$(w2_cred_mode)"
  case "$mode" in
    vault)
      adp-cred assume --service aws --label "$W2_CRED_LABEL" \
        --purpose "${W2_PURPOSE:-issue-3968 wave2}" --exec bash -c "$body"
      ;;
    profile|env)
      bash -c "$body"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# w2_assert_account — THE load-bearing check. Applies to all three modes.
#
# Echoes "<account> <arn>" on success. Fails closed: an identity that cannot be
# read is not a passing identity.
# ---------------------------------------------------------------------------
w2_assert_account() {
  local mode ident account arn
  mode="$(w2_cred_mode)"
  w2_warn_if_ambient_worker

  ident="$(w2_aws sts get-caller-identity --output json 2>&1)" || {
    printf 'FAIL: could not read caller identity in %s mode.\n' "$mode" >&2
    printf '%s\n' "$ident" >&2
    case "$mode" in
      vault)   printf '  The vault connection is missing, expired, or routing was denied.\n' >&2 ;;
      profile) printf '  AWS_PROFILE=%s may be undefined in the shared config, or its SSO session expired.\n' "${AWS_PROFILE:-}" >&2 ;;
      env)     printf '  No ambient credential resolved. Set AWS_PROFILE, or W2_CRED_MODE=vault in a pod.\n' >&2 ;;
    esac
    exit 1
  }

  account="$(printf '%s' "$ident" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])')"
  arn="$(printf '%s' "$ident" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')"

  if [ "$account" != "$W2_EXPECT_ACCOUNT" ]; then
    printf 'FAIL: credential (%s mode) resolves to account %s, expected %s.\n' \
      "$mode" "$account" "$W2_EXPECT_ACCOUNT" >&2
    printf '  ARN: %s\n' "$arn" >&2
    printf '  This is the #5195 failure mode. Do NOT proceed and do NOT widen the expectation;\n' >&2
    printf '  select the credential for %s instead.\n' "$W2_EXPECT_ACCOUNT" >&2
    exit 1
  fi
  printf '%s %s' "$account" "$arn"
}

# ---------------------------------------------------------------------------
# w2_require_account — assert the account and SET W2_ACCOUNT / W2_ARN.
#
# Use this, not `read ... <<<"$(w2_assert_account)"`. Inside a command
# substitution the `exit 1` above terminates only the SUBSHELL: the outer script
# sees a successful `read` of empty output and carries on with an empty account.
# That silently defeats the entire #5195 protection, which is the one check these
# scripts exist to enforce. Assigning in the caller's own shell means a refusal
# actually stops the run.
#
# Belt and braces: the emptiness check below catches any future path that returns
# success without an account.
# ---------------------------------------------------------------------------
w2_require_account() {
  local ident
  ident="$(w2_assert_account)" || exit 1
  W2_ACCOUNT="${ident%% *}"
  W2_ARN="${ident#* }"
  [ -n "$W2_ACCOUNT" ] || w2_fail "account assertion produced no account id; refusing to proceed"
  [ "$W2_ACCOUNT" = "$W2_EXPECT_ACCOUNT" ] \
    || w2_fail "account assertion returned '$W2_ACCOUNT', expected $W2_EXPECT_ACCOUNT"
}

# ---------------------------------------------------------------------------
# w2_kubeconfig — write a kubeconfig for the fixture cluster. Echoes its path.
# ---------------------------------------------------------------------------
w2_kubeconfig() {
  local kc="${W2_KUBECONFIG:-/tmp/w2-kubeconfig-3968}"
  if [ ! -s "$kc" ]; then
    w2_aws eks update-kubeconfig --name "$W2_CLUSTER" --region "$W2_REGION" \
      --kubeconfig "$kc" >/dev/null 2>&1 \
      || w2_fail "could not write kubeconfig for cluster $W2_CLUSTER"
  fi
  printf '%s' "$kc"
}

# ---------------------------------------------------------------------------
# w2_kubectl — kubectl against the fixture cluster under the resolved credential.
# ---------------------------------------------------------------------------
w2_kubectl() {
  local kc; kc="$(w2_kubeconfig)"
  local mode; mode="$(w2_cred_mode)"
  case "$mode" in
    vault)
      KUBECONFIG="$kc" adp-cred assume --service aws --label "$W2_CRED_LABEL" \
        --purpose "${W2_PURPOSE:-issue-3968 wave2}" --exec kubectl "$@"
      ;;
    profile|env)
      KUBECONFIG="$kc" kubectl "$@"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# w2_report_mode — print the resolved mode for the evidence record.
# ---------------------------------------------------------------------------
w2_report_mode() {
  local mode; mode="$(w2_cred_mode)"
  case "$mode" in
    vault)   w2_ok "credential mode: vault (adp-cred label $W2_CRED_LABEL)" ;;
    profile) w2_ok "credential mode: profile (AWS_PROFILE=${AWS_PROFILE:-unset})" ;;
    env)     w2_ok "credential mode: env (ambient/instance credential, account-asserted)" ;;
  esac
}

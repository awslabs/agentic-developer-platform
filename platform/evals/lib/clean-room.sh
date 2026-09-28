# shellcheck shell=bash
# =============================================================================
# lib/clean-room.sh — the contamination gate
# =============================================================================
# Runs INSIDE the laptop pod (the harness execs it there as the pod's first
# command). Fails fast, before anything mutates the target environment, if the pod
# is not a plausible fresh developer laptop. Each check maps to a way the
# agent-worker image (or an inherited shell) would silently substitute platform
# auth for the flow under test — and it stays in place as the guard against
# someone later pointing laptop() at a dirty target.
#
# WHY THIS IS NOT OPTIONAL
# A contaminated run reports FALSE GREEN on exactly the auth path under test,
# which is worse than having no eval at all. That is the whole reason this file
# exists as a shared, single implementation: two evals with two hand-rolled
# gates would drift, and the weaker one would be the one that lies.
#
# The caller must define PROXY_PORT before calling assert_clean_room.
# =============================================================================

# /dev/tcp in a subshell, so a refused connection cannot trip `set -e` in the
# caller and cannot leak fd 3 into the rest of the script.
port_is_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

assert_clean_room() {
  local violations=0
  log "Asserting clean room (HOME=$HOME)"

  local d
  for d in "$HOME/.codex" "$HOME/.claude" "$HOME/.bedrock-gateway"; do
    if [ -e "$d" ]; then
      echo "${RED}[FAIL]${NC} contaminated: $d exists — this container has pre-wired CLI config" >&2
      violations=$((violations + 1))
    fi
  done
  if [ -e "$HOME/.claude.json" ]; then
    echo "${RED}[FAIL]${NC} contaminated: $HOME/.claude.json exists" >&2
    violations=$((violations + 1))
  fi

  # Any ANTHROPIC_* / ADP_GATEWAY_* / CLAUDE_CODE_* var can redirect a CLI at
  # another endpoint or hand it a credential, so the whole namespace is barred.
  local var
  while IFS= read -r var; do
    case "$var" in
      ANTHROPIC_*|ADP_GATEWAY_*|CLAUDE_CODE_*)
        echo "${RED}[FAIL]${NC} contaminated: \$$var is set" >&2
        violations=$((violations + 1))
        ;;
    esac
  done < <(compgen -e || true)

  # A pre-installed CLI means a pre-configured CLI in the images we care about,
  # and the journey under test includes installing them from npm.
  #
  # EVAL_SKIP_CLI_PATH_CHECK exists ONLY for this repo's own harness tests, which
  # necessarily run inside the agent container where codex IS installed. A
  # workflow must never set it: if it ever appears in an eval workflow, the clean
  # room is a fiction. Every other check still applies when it is set.
  if [ "${EVAL_SKIP_CLI_PATH_CHECK:-false}" != "true" ]; then
    local bin
    for bin in claude codex; do
      if command -v "$bin" >/dev/null 2>&1; then
        echo "${RED}[FAIL]${NC} contaminated: '$bin' is already on PATH" >&2
        violations=$((violations + 1))
      fi
    done
  fi

  # A listener on either port would answer the CLI instead of our own proxy —
  # 9090 specifically is the agent-worker's sigv4-proxy sidecar, i.e. exactly
  # the platform-internal auth this eval must not accidentally ride on.
  # (Overridable only so the harness tests can aim at a known-free port; the
  # workflow always uses the default.)
  local port
  for port in ${EVAL_FORBIDDEN_PORTS:-$PROXY_PORT 9090}; do
    if port_is_open "$port"; then
      echo "${RED}[FAIL]${NC} contaminated: something is listening on 127.0.0.1:${port}" >&2
      violations=$((violations + 1))
    fi
  done

  if [ "$violations" -gt 0 ]; then
    echo "" >&2
    echo "${RED}Clean-room assertion failed with $violations violation(s).${NC}" >&2
    echo "Run this eval in a stock container (see the eval's workflow)," >&2
    echo "never in the agent-worker image — a contaminated run reports false green on the" >&2
    echo "exact auth path under test, which is worse than having no eval at all." >&2
    return 1
  fi

  pass "clean room verified: no pre-wired CLI config, no ANTHROPIC_*/ADP_GATEWAY_*/CLAUDE_CODE_* env, no CLI on PATH, proxy ports free"
  return 0
}

# shellcheck shell=bash
# =============================================================================
# lib/state.sh — key/value state that survives across processes
# =============================================================================
# This is what makes `--cleanup-only` work STANDALONE: the sweep step runs as a
# separate process after a killed job, so everything cleanup needs to undo has to
# have been written down at the moment it was done, not held in a variable.
#
# The caller must define STATE_FILE before calling these.
#
# The discipline that matters: record BEFORE the mutating call, never after. If a
# mutation half-succeeds, cleanup must still know to undo it — recording
# afterwards leaves the environment dirty with nothing pointing at it.
# =============================================================================

state_set() {
  local key="$1" value="$2"
  touch "$STATE_FILE"; chmod 600 "$STATE_FILE"
  grep -v "^${key}=" "$STATE_FILE" > "$STATE_FILE.tmp" 2>/dev/null || true
  mv -f "$STATE_FILE.tmp" "$STATE_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$STATE_FILE"
}

state_get() {
  local key="$1"
  [ -f "$STATE_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$STATE_FILE" | tail -1
}

# state_append <key> <value> — maintain a comma-separated list.
# Cleanup iterates these to undo N-of-a-kind mutations (users rows, budget
# configs, rate-limit configs) without needing a key per item.
state_append() {
  local key="$1" value="$2" existing
  existing="$(state_get "$key")"
  state_set "$key" "${existing:+${existing},}${value}"
}

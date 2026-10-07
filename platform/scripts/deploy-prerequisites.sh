#!/usr/bin/env bash
# Source before argument arrays, config loading, or AWS calls. Bash 3.2 must be
# able to parse this file so macOS gets an actionable error, not an array error.
if [ "${BASH_VERSINFO[0]}" -lt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 4 ]; }; then
  echo 'ADP deployment requires Bash 4.4+. On macOS: brew install bash flock; run with Homebrew bash (and put it first in PATH for child scripts).' >&2
  exit 2
fi
if ! command -v flock >/dev/null 2>&1; then
  echo 'ADP deployment requires flock for checkpoint locking. Install util-linux on Linux or brew install flock on macOS.' >&2
  exit 2
fi

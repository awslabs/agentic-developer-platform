#!/usr/bin/env bash
# Stage the domain auth policy package into this component's Docker build context.
#
# Issue #5055 (U14).
#
# THE PROBLEM THIS SOLVES
# ----------------------
# `app/auth.py` imports `superplane_auth.policy`, the authorization policy owned by
# this module at `modules/domain-apps/superplane/auth/`. That directory is OUTSIDE
# this component's Docker build context: `releases/build-image.sh` pins the context
# to `modules/domain-apps/superplane/src/superplane-api` and verifies that the
# context matches the source path the release lock names, so a `COPY ../../auth`
# cannot work — Docker refuses paths outside the context, by design.
#
# Without this step the code passes CI (where the package is pip-installed from its
# path) and the IMAGE fails at import. That gap is the reason this script exists as
# an explicit, checked step rather than a line in a README: an import that works in
# the test lane and crashes in the container is exactly the class of failure the
# "placeholder artifact" rule warns about.
#
# Nothing has shipped in the gap. `pending_images` in the release lock records that
# no API image has been built yet, so this is closing a hole before first build
# rather than repairing a deployed one.
#
# WHY A COPY, AND WHY IT IS NOT A SECOND SOURCE OF TRUTH
# -----------------------------------------------------
# The copy is build scratch, not a parallel implementation: it is git-ignored (see
# .gitignore), refreshed from the single maintained location on every run, and
# deleted and rewritten rather than merged. The maintained package remains the only
# editable copy. `tests/test_auth.py` asserts the staged directory is not committed,
# so it cannot quietly become a second writable tree.
#
# The alternative — widening the Docker build context to the module root — would
# change `releases/build-image.sh`, which is release-owned (U2/U23) and validates
# that the context equals the locked source path. That is a larger, cross-story
# change than this story should make unilaterally; it is flagged for those owners
# in the PR as the durable fix.
set -euo pipefail

component_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
auth_src="$(cd "$component_dir/../../auth" && pwd)"
staged="$component_dir/vendor/superplane-auth"

[[ -f "$auth_src/superplane_auth/policy.py" ]] || {
  echo "error: the domain auth policy is not at $auth_src/superplane_auth/policy.py" >&2
  exit 1
}

# Refuse to stage from the read-only reference snapshot, mirroring the guard in
# releases/build-image.sh. Evidence must never become a build input.
[[ "$auth_src" != *"ai-super-plane"* ]] || {
  echo "error: refusing to stage from the reference snapshot" >&2
  exit 1
}

rm -rf "$staged"
mkdir -p "$staged"
cp "$auth_src/pyproject.toml" "$staged/"
cp -R "$auth_src/superplane_auth" "$staged/superplane_auth"

echo "Staged superplane_auth from $auth_src into $staged"

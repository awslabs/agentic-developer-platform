#!/usr/bin/env bash
# Stage this component's sibling source packages into its Docker build context.
#
# Issue #5055 (U14) for `superplane_auth`; issue #5053 (U7b) for
# `superplane_contracts`.
#
# THE PROBLEM THIS SOLVES
# ----------------------
# `app/auth.py` imports `superplane_auth.policy` and `app/main.py` imports
# `superplane_contracts.emission`. Both packages are owned by this module —
# `modules/domain-apps/superplane/auth/` and `modules/domain-apps/superplane/contracts/`
# — and both are OUTSIDE this component's Docker build context:
# `releases/build-image.sh` pins the context to
# `modules/domain-apps/superplane/src/superplane-api` and verifies that the context
# matches the source path the release lock names, so a `COPY ../../auth` cannot work —
# Docker refuses paths outside the context, by design.
#
# Without this step the code passes CI (where both packages are pip-installed from
# their paths) and the IMAGE fails at import. That gap is the reason this script
# exists as an explicit, checked step rather than a line in a README: an import that
# works in the test lane and crashes in the container is exactly the class of failure
# the "placeholder artifact" rule warns about.
#
# `superplane_contracts` was the same hole a second time, found while wiring U7b's
# routes: it appears in NO dependency list in `pyproject.toml`, so nothing but the
# developer's pip-installed checkout was supplying it. `app/main.py` imports it
# unconditionally at module scope, so the image would have raised
# ModuleNotFoundError before serving a single request — and the two earlier comments
# claiming it was "not importable at API runtime" (see `app/services/provisioning.py`
# and `app/models/credential.py`) described that broken state as though it were the
# design. Those comments are corrected alongside this change.
#
# Nothing has shipped in either gap. `pending_images` in the release lock records
# that no API image has been built yet, so this closes a hole before first build
# rather than repairing a deployed one.
#
# WHY A COPY, AND WHY IT IS NOT A SECOND SOURCE OF TRUTH
# -----------------------------------------------------
# The copies are build scratch, not parallel implementations: they are git-ignored
# (see .gitignore), refreshed from the single maintained location on every run, and
# deleted and rewritten rather than merged. The maintained packages remain the only
# editable copies. `tests/test_auth.py` asserts the staged directory is not
# committed, so it cannot quietly become a second writable tree.
#
# The alternative — widening the Docker build context to the module root — would
# change `releases/build-image.sh`, which is release-owned (U2/U23) and validates
# that the context equals the locked source path. That is a larger, cross-story
# change than this story should make unilaterally; it is flagged for those owners
# in the PR as the durable fix. It is now TWO stories' worth of evidence that the
# per-package workaround does not scale: each new sibling package repeats it.
set -euo pipefail

component_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vendor_dir="$component_dir/vendor"

# Each entry: <source dir, relative to the module root>:<import package>:<sentinel module>
# The sentinel is a file that must exist for the copy to be worth making — a
# directory that exists but is missing the module the app imports is the failure
# this guard is for, and it is not the same as the directory being absent.
#
# The source is a RELATIVE PATH rather than a bare directory name, which is what
# lets the third entry reach outside this module. `harness_jobs` (#5527) lives at
# `modules/harness/jobs/`, a sibling of `modules/domain-apps/`, not of this
# module's `auth/` and `contracts/`. The path is resolved and then re-checked
# below, so escaping the module root does not escape the repository.
#
# WHY harness_jobs IS STAGED (issue #5535, W6)
# -------------------------------------------
# `app/composition.py` needs it to build three of the four production trust ports:
# `OperationFacadeService` for operation admission and `InventoryAuthority` for
# allocation membership, plus the durable operation and lease rows the provider
# authority adapter resolves against.
#
# It was previously recorded in that module as "not present in this image", with
# the build context named as the blocker. That was accurate about the *symptom*
# and wrong about the *cause*, in the same way the `superplane_contracts` comments
# corrected above were wrong: the package is not unreachable, it was UNSTAGED.
# Nothing about the architecture prevented this entry; no one had added it. The
# consequence was not a crash but something quieter — the packaged capability
# preflight reported three ports permanently absent, and since the installer
# requires all four, no deployment could pass its own gate however it was
# configured.
#
# It is stdlib-only (`modules/harness/jobs/pyproject.toml` declares no
# dependencies, deliberately, so that it cannot read a DSN or hold a credential),
# so staging it adds no transitive package to the image.
packages=(
  "auth:superplane_auth:policy.py"
  "contracts:superplane_contracts:emission.py"
  "../../harness/jobs:harness_jobs:facade.py"
)

stage_one() {
  local src_name="$1" pkg="$2" sentinel="$3"
  local src staged

  src="$(cd "$component_dir/../../$src_name" 2>/dev/null && pwd)" || {
    echo "error: $src_name is not at $component_dir/../../$src_name" >&2
    exit 1
  }

  # `cd` above resolved any `..` in the entry, so `$src` is now absolute and real.
  # Require it to stay inside the repository. An entry may legitimately leave this
  # module (harness_jobs does), but an entry that leaves the repository would stage
  # something no reviewer of this repository has seen, and the copy is a build
  # input. Checked on the RESOLVED path, because checking the unresolved entry
  # string is defeated by any additional `..`.
  repo_root="$(cd "$component_dir/../../../.." && pwd)"
  [[ "$src" == "$repo_root"/* ]] || {
    echo "error: $pkg resolves to $src, outside the repository at $repo_root" >&2
    exit 1
  }

  [[ -f "$src/$pkg/$sentinel" ]] || {
    echo "error: $pkg is not at $src/$pkg/$sentinel" >&2
    exit 1
  }

  # Refuse to stage from the read-only reference snapshot, mirroring the guard in
  # releases/build-image.sh. Evidence must never become a build input.
  [[ "$src" != *"ai-super-plane"* ]] || {
    echo "error: refusing to stage from the reference snapshot" >&2
    exit 1
  }

  # Hyphenated to match the distribution name, which is what `pip install <dir>`
  # reports and what the Dockerfile's COPY paths name.
  staged="$vendor_dir/${pkg//_/-}"
  rm -rf "$staged"
  mkdir -p "$staged"
  cp "$src/pyproject.toml" "$staged/"
  cp -R "$src/$pkg" "$staged/$pkg"

  echo "Staged $pkg from $src into $staged"
}

for entry in "${packages[@]}"; do
  IFS=":" read -r src_name pkg sentinel <<<"$entry"
  stage_one "$src_name" "$pkg" "$sentinel"
done

"""Resolve build inputs from the release lock — Issue #5041 (U2), EPIC #4910.

The three pinned image-build lanes call this instead of parsing YAML inline three
times, so that "the build reads its ref from the lock" is one enforced code path
rather than a convention each workflow reimplements slightly differently. The bug
class it exists to prevent is a build that resolves a floating ref while a lock
file sits next to it looking authoritative: pinning has to be the mechanism, not
a document.

U22 (#5326) changed what this resolves TO. Before the transfer, building any of
the three Superplane images needed read access to a repository ADP does not own,
so every lane stopped here with ``SourceAccessUnresolved``. The source is now
maintained in this repository under ``MAINTAINED_ROOT``, so the resolver hands
back an in-repository build context and the lanes build from a clean ADP checkout
— no upstream access, no PAT, no reference tree.

Two facts are kept deliberately separate, because the issue requires it and
because one field could only carry one of them:

* ``source_path`` / ``SUPERPLANE_SOURCE_DIR`` — where the code being built lives
  now. This is what a build reads.
* ``origin_repository`` / ``origin_revision`` — the historical origin the files
  were transferred from. Provenance for the image label; never a fetch target.

The fail-closed path is retained rather than deleted: a lock that records an
unresolved mechanism still stops with a named cause instead of a checkout error
that reads like a transient CI fault.

Usage from a workflow step::

    python3 modules/domain-apps/superplane/releases/resolve_lock.py superplane-api

Exit codes: 0 resolved (prints ``KEY=value`` lines for $GITHUB_ENV), 78 source
access unresolved (the lane stops deliberately), 1 the lock itself is malformed.
"""

from __future__ import annotations

import sys
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOCK_PATH = Path(__file__).with_name("superplane.lock.yaml")

# Repository-relative module root of the maintained source transferred by U22 (#5326).
# Build contexts are resolved under this, so "which directory does the build read" has one
# answer in code rather than being re-derived in each of the three lanes.
#
# Note this stops at the MODULE root, not at `src/`: the lock's `source_path` values are
# themselves `src/superplane-*`, matching the layout the components were transferred into,
# so appending them here must not repeat the `src/` segment.
MAINTAINED_ROOT = "modules/domain-apps/superplane"

# Exit code for "this lane is correctly configured but is blocked by an unresolved
# access grant". Distinct from 1 so a blocked lane is never mistaken for a broken
# lock file. 78 is EX_CONFIG from sysexits.h.
#
# Retained after U22 resolved the grant for the three transferred components: the
# fail-closed path is still the correct behavior for a lock that records an unresolved
# mechanism, and removing it would mean a future component added in the same
# not-yet-granted state would fail with a confusing error instead of a named one.
EXIT_SOURCE_ACCESS_UNRESOLVED = 78


class LockError(Exception):
    """The lock file is missing, unparseable or internally inconsistent."""


class SourceAccessUnresolved(Exception):
    """The image cannot be built because upstream source access is unresolved."""


@dataclass(frozen=True)
class BuildInputs:
    """Everything a build lane needs, all of it read from the lock.

    After U22 (#5326) the source is maintained in this repository, so ``source_path`` is
    an in-repository build context rather than a path inside a repository ADP cannot read.
    ``origin_repository``/``origin_revision`` are retained as PROVENANCE — the historical
    origin of the transferred files — and are deliberately named ``origin_*`` rather than
    ``upstream_*`` so no caller can mistake them for a place to fetch from.
    """

    image: str
    origin_repository: str
    origin_revision: str
    source_path: str
    ecr_repository: str

    def __post_init__(self) -> None:
        values = {
            "image": (self.image, r"superplane-(api|controller|platform-monitor)"),
            "origin repository": (
                self.origin_repository,
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            ),
            "origin revision": (self.origin_revision, r"[0-9a-f]{40}"),
            "source path": (
                self.source_path,
                r"src/superplane-(api|controller|platform-monitor)",
            ),
            "ECR repository": (self.ecr_repository, r"adp-superplane-[a-z0-9-]+"),
        }
        for name, (value, pattern) in values.items():
            if not re.fullmatch(pattern, value):
                raise LockError(f"invalid build {name}")
        if (
            self.source_path != "src/" + self.image
            or self.ecr_repository != "adp-" + self.image
        ):
            raise LockError(
                "image source and ECR target must match the selected domain component"
            )

    def as_env_lines(self) -> str:
        """Render for ``>> "$GITHUB_ENV"``.

        ``SUPERPLANE_SOURCE_DIR`` is the repository-relative build context, so a lane
        never has to reconstruct it from the module root and the component name.
        """
        return "\n".join(
            [
                f"SUPERPLANE_IMAGE={self.image}",
                f"ORIGIN_REPOSITORY={self.origin_repository}",
                f"ORIGIN_REVISION={self.origin_revision}",
                f"SOURCE_PATH={self.source_path}",
                f"SUPERPLANE_SOURCE_DIR={MAINTAINED_ROOT}/{self.source_path}",
                f"ECR_REPO={self.ecr_repository}",
            ]
        )


def load_lock(path: Path | None = None) -> dict[str, Any]:
    """Read and minimally validate the lock file."""
    lock_path = path or LOCK_PATH
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError as exc:  # missing or unreadable
        raise LockError(f"cannot read lock file {lock_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise LockError(f"{lock_path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise LockError(
            f"{lock_path} must contain a mapping, got {type(data).__name__}"
        )

    upstream = data.get("upstream")
    if not isinstance(upstream, dict) or not upstream.get("revision"):
        raise LockError(
            f"{lock_path} has no upstream.revision — a build has nothing to pin to"
        )

    return data


def resolved_digest(image: str, path: Path | None = None) -> str:
    """Return the pinned digest for an image whose digest is known.

    Raises ``LockError`` if the entry is absent or is not digest-addressed. The
    second check is the point: a tag that appeared in ``images`` would otherwise
    be handed to a deploy as though it were a pin.
    """
    data = load_lock(path)
    images = data.get("images") or {}
    if image not in images:
        pending = data.get("pending_images") or {}
        if image in pending:
            raise LockError(
                f"{image!r} is pending (no digest yet) — blocked by {pending[image].get('blocked_by')!r}"
            )
        raise LockError(f"{image!r} is not in the lock file")

    digest = str(images[image])
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or digest == "sha256:" + "0" * 64
    ):
        raise LockError(
            f"{image!r} is pinned to {digest!r}, which is not a sha256: digest"
        )
    return digest


def resolve_build_inputs(image: str, path: Path | None = None) -> BuildInputs:
    """Resolve initial or subsequent build inputs for a Superplane image.

    First builds use pending metadata. After promotion, the same source metadata
    lives in image_sources alongside the resolved digest's registry provenance.

    Raises ``SourceAccessUnresolved`` when the lock records the upstream
    source-access mechanism as unresolved, which is the current state.
    """
    data = load_lock(path)

    pending = data.get("pending_images") or {}
    images = data.get("images") or {}
    if image in pending and image in images:
        raise LockError(f"{image!r} is both pending and resolved")
    if image in pending:
        entry = pending[image]
    elif image in images:
        resolved_digest(image, path)
        entry = (data.get("image_sources") or {}).get(image)
    else:
        raise LockError(f"{image!r} is not a buildable image in the lock file")
    if not isinstance(entry, dict):
        raise LockError(f"{image!r} has no build source metadata")

    # A pending entry must never carry a digest — see the lock file header. If one
    # appears, the two-map invariant has been broken and the safe move is to stop.
    if image in pending and "digest" in entry:
        raise LockError(
            f"pending image {image!r} carries a digest; pending entries must not have one"
        )

    access = data.get("source_access") or {}
    if access.get("status") != "resolved":
        mechanisms = access.get("candidate_mechanisms") or []
        raise SourceAccessUnresolved(
            f"Cannot build {image}: the source-access mechanism is unresolved.\n"
            f"The lock records origin {data['upstream'].get('repository')} at "
            f"{data['upstream']['revision']}, but records no way for ADP to obtain it.\n"
            "Ruled out: actions/checkout of the upstream repository (the hosted token is "
            "scoped to this repository), and the read-only reference snapshot (evidence, "
            "not a build input).\n"
            "One of these must be granted and recorded in source_access:\n"
            + "\n".join(f"  - {m}" for m in mechanisms)
        )

    origin = data["upstream"]
    return BuildInputs(
        image=image,
        origin_repository=str(origin.get("repository", "")),
        origin_revision=str(origin["revision"]),
        source_path=str(entry.get("source_path", "")),
        ecr_repository=str(entry.get("ecr_repository", "")),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            f"usage: {Path(argv[0]).name if argv else 'resolve_lock.py'} <image>",
            file=sys.stderr,
        )
        return 1

    try:
        inputs = resolve_build_inputs(argv[1])
    except SourceAccessUnresolved as exc:
        # Deliberate stop, not a fault. ::error:: surfaces it in the job log.
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_SOURCE_ACCESS_UNRESOLVED
    except LockError as exc:
        print(f"::error::release lock is unusable: {exc}", file=sys.stderr)
        return 1

    print(inputs.as_env_lines())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))

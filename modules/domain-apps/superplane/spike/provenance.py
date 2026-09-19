"""Provenance for every fact recorded by this harness.

Issue #5040 (U12), EPIC #4910. The scope amendment
`docs/design-notes/4910-skypilot-eks-migration-amendment.md` requires that
fixtures be derived from the named upstream schemas and client responses, and
never from the desired output of the ADP-side adapter that U19 will write.

The pinned reference snapshot lives on the planning branch `agent/issue-4910`
at `modules/domain-apps/ai-super-plane/reference/`. It is deliberately NOT
copied into main: U12's regression check is "the reference snapshot stays
unchanged". So this harness records, for each fact, the upstream path and
revision it was read from, and the tests assert that every claim carries such a
citation. A reader can re-derive any fixture with:

    git fetch origin agent/issue-4910
    git show FETCH_HEAD:modules/domain-apps/ai-super-plane/reference/<path>

That makes the fixtures auditable without vendoring 919 files, and it is why
`EvidenceStatus` below is a closed set rather than a free-text field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# From modules/domain-apps/ai-super-plane/reference-manifest.json on the
# planning branch. The manifest records 919 tracked files verified against
# upstream Git blob SHA-1 ids, with one redaction (a GitHub PAT).
UPSTREAM_REPOSITORY = "https://github.com/aws-innovate/AISuperPlane"
UPSTREAM_REVISION = "5d543c952493f0765133b92e93301b0b24d028ee"

# The ADP commit on agent/issue-4910 carrying that snapshot.
SNAPSHOT_BRANCH = "agent/issue-4910"
SNAPSHOT_PREFIX = "modules/domain-apps/ai-super-plane/reference/"


class EvidenceStatus(str, Enum):
    """How well a behavior is actually attested.

    The amendment requires source-only, stubbed and user-observed behavior to
    be classified separately, and forbids claiming support for a workflow that
    was never present or tested in the baseline. Ordering matters: only
    ``USER_REPORTED`` reflects a human's report of the running system, and even
    that is not the same as ADP-captured live evidence (see
    ``ParityDimension.live_verified``, which no offline run can set true).
    """

    # A human reported this working in a real environment. Still not
    # ADP-captured evidence: no revision, inputs or logs were recorded.
    USER_REPORTED = "user_reported"

    # The upstream source implements it, but this harness has observed no run.
    SOURCE_ONLY = "source_only"

    # A code path or field exists but is not wired to anything that fills it.
    STUBBED = "stubbed"

    # Named in the migration discussion but absent from the baseline source.
    ABSENT = "absent"


@dataclass(frozen=True)
class Citation:
    """A pointer into the pinned upstream snapshot.

    ``path`` is relative to ``SNAPSHOT_PREFIX``. ``detail`` says what the cited
    file actually shows, so a reviewer can check the claim against the file
    rather than trusting this harness's summary of it.
    """

    path: str
    detail: str
    revision: str = UPSTREAM_REVISION

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/"):
            raise ValueError(f"path must be snapshot-relative, got {self.path!r}")
        if not self.detail.strip():
            raise ValueError(f"citation for {self.path!r} has no detail")

    @property
    def snapshot_path(self) -> str:
        """The path as it appears on the planning branch."""
        return SNAPSHOT_PREFIX + self.path

    @property
    def show_command(self) -> str:
        """The exact command a reader runs to see the cited file."""
        return f"git show FETCH_HEAD:{self.snapshot_path}"

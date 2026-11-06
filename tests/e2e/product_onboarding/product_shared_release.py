"""Reuse of the immutable shared Product release across reviewed engine sources.

The release engine publishes one immutable shared release and the regression
engine pins its own reviewed `main` revision, which keeps advancing after that
release is published. Reusing the release is only sound when its source
revision is contained in the engine attempt's own history, so this module
verifies lineage instead of demanding that the two revisions are identical.
"""

from __future__ import annotations

import re
import subprocess

SHA = re.compile(r"[0-9a-f]{40}")


def _commit_exists(run, revision):
    return run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], check=False,
               capture_output=True, text=True, timeout=120).returncode == 0


def verify_shared_source_lineage(manifest, environment, *, run=subprocess.run):
    """Return True when the engine source contains the shared release source.

    Raises ValueError when the shared release was built from a revision that the
    engine attempt does not contain, when either revision is not a full commit
    digest, or when the engine source differs from its checked out revision.
    Returns False when no engine source is transported, which keeps the
    standalone selector unable to claim engine lineage.
    """
    source = manifest["source_revision"]
    engine = environment.get("ADP_SOURCE_REVISION") or ""
    if not engine:
        return False
    if not SHA.fullmatch(source) or not SHA.fullmatch(engine):
        raise ValueError("Shared Product release lineage needs full commit revisions")
    if engine != environment.get("ADP_CHECKOUT_REVISION", engine):
        raise ValueError("Engine source differs from the checked out revision")
    if engine == source:
        return True
    for revision in (source, engine):
        if not _commit_exists(run, revision):
            raise ValueError("A Product source revision is absent from the checked out history")
    if run(["git", "merge-base", "--is-ancestor", source, engine], check=False,
           capture_output=True, text=True, timeout=120).returncode != 0:
        raise ValueError("Selected Product release is not in the engine source's lineage")
    return True

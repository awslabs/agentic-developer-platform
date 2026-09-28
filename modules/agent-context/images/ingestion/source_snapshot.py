"""Private per-attempt Git workspace; never reuse persistent source metadata."""

from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile


@contextmanager
def source_snapshot(scratch_base: str, persistent_roots: tuple[str, ...]):
    root = Path(scratch_base).resolve(strict=True)
    if not root.is_dir() or not root.is_relative_to(Path("/tmp").resolve()):
        raise ValueError("SCRATCH_BASE must resolve beneath bounded /tmp")
    if any(root.is_relative_to(Path(p).resolve()) for p in persistent_roots):
        raise ValueError("Scratch cannot overlap persistent state/source")
    owned = Path(tempfile.mkdtemp(prefix="ingest-source-", dir=root))
    try:
        yield str(owned / "source")
    finally:
        # Delete only the exact private directory allocated by this invocation.
        shutil.rmtree(owned)

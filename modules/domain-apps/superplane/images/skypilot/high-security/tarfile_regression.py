"""Check that extraction retains the target of a relocated hard link."""

import io
import tarfile
import tempfile
from pathlib import Path


def check():
    for filter_name in ("data", "tar"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "dest"
            dest.mkdir()
            (root / "escape").write_bytes(b"outside")
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                entry = tarfile.TarInfo("a/escape")
                entry.size = 5
                archive.addfile(entry, io.BytesIO(b"decoy"))
                entry = tarfile.TarInfo("a/b/s")
                entry.type, entry.linkname = tarfile.SYMTYPE, "../escape"
                archive.addfile(entry)
                entry = tarfile.TarInfo("s")
                entry.type, entry.linkname = tarfile.LNKTYPE, "a/b/s"
                archive.addfile(entry)
            buffer.seek(0)
            with tarfile.open(fileobj=buffer) as archive:
                archive.extractall(dest, filter=filter_name)
            assert not (dest / "s").is_symlink()
            assert (dest / "s").read_bytes() == b"decoy"
            assert (root / "escape").read_bytes() == b"outside"

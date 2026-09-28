"""Offline regressions for the reviewed CPython backports; run inside the image.

Cases follow the upstream security regression tests linked in README.md.
No external requests, credentials, or production data are used.
"""

import io
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path


class SecurityBackports(unittest.TestCase):
    def test_idna_uses_unicode_32_casefolding(self):
        cases = [
            ("\N{CHEROKEE LETTER A}\N{CHEROKEE LETTER A}", b"xn--58da"),
            ("\N{GEORGIAN CAPITAL LETTER AN}.", b"xn--7md."),
            ("\N{CYRILLIC LETTER PALOCHKA}.example", b"xn--d5a.example"),
            ("\N{ROMAN NUMERAL REVERSED ONE HUNDRED}.example.", b"xn--q5g.example."),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(text.encode("idna"), expected)

    def test_passwords_are_scoped_by_scheme(self):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, "https://example.invalid/", "synthetic", "fixture")
        self.assertEqual(manager.find_user_password(None, "https://example.invalid/path"), ("synthetic", "fixture"))
        self.assertEqual(manager.find_user_password(None, "http://example.invalid/path"), (None, None))

    def test_tar_does_not_create_outside_intermediate_directory(self):
        for filter_name in ("tar", "data"):
            with self.subTest(filter=filter_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                dest = root / "dest"
                dest.mkdir()
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    member = tarfile.TarInfo("../escaped/../dest/sub/file")
                    member.size = 7
                    archive.addfile(member, io.BytesIO(b"content"))
                buffer.seek(0)
                with tarfile.open(fileobj=buffer) as archive:
                    archive.extractall(dest, filter=filter_name)
                self.assertFalse((root / "escaped").exists())
                self.assertEqual((dest / "sub/file").read_bytes(), b"content")

    def test_tar_link_fallback_honors_filter_none(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                symlink = tarfile.TarInfo("a/b/s")
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = "../escape"
                archive.addfile(symlink)
                hardlink = tarfile.TarInfo("q")
                hardlink.type = tarfile.LNKTYPE
                hardlink.linkname = "a/b/s"
                archive.addfile(hardlink)

            def filter_unsafe(member, destination):
                try:
                    return tarfile.data_filter(member, destination)
                except tarfile.FilterError:
                    return None

            buffer.seek(0)
            with tarfile.open(fileobj=buffer) as archive:
                archive.extractall(root, filter=filter_unsafe)
            self.assertTrue((root / "a/b/s").is_symlink())
            self.assertFalse((root / "q").is_symlink())
            self.assertFalse((root / "q").exists())

    def test_tar_hardlink_does_not_relocate_symlink(self):
        for filter_name in ("data", "tar"):
            with self.subTest(filter=filter_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                destination = root / "dest"
                destination.mkdir()
                outside = root / "escape"
                outside.write_bytes(b"outside fixture")
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    decoy = tarfile.TarInfo("a/escape")
                    decoy.size = 5
                    archive.addfile(decoy, io.BytesIO(b"decoy"))
                    symlink = tarfile.TarInfo("a/b/s")
                    symlink.type = tarfile.SYMTYPE
                    symlink.linkname = "../escape"
                    archive.addfile(symlink)
                    hardlink = tarfile.TarInfo("s")
                    hardlink.type = tarfile.LNKTYPE
                    hardlink.linkname = "a/b/s"
                    archive.addfile(hardlink)
                buffer.seek(0)
                with tarfile.open(fileobj=buffer) as archive:
                    archive.extractall(destination, filter=filter_name)
                self.assertFalse((destination / "s").is_symlink())
                self.assertEqual((destination / "s").read_bytes(), b"decoy")
                self.assertEqual(outside.read_bytes(), b"outside fixture")


if __name__ == "__main__":
    unittest.main()

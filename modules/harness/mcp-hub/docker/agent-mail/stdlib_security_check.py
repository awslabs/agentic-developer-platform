"""Offline regressions for the reviewed CPython backports; run inside the image.

Cases follow the upstream security regression tests linked in stdlib-security/README.md.
No external requests, credentials, or production data are used.
"""
import io
from pathlib import Path
import poplib
import tarfile
import tempfile
import unittest
import urllib.request
import zipfile


class SecurityBackports(unittest.TestCase):
    def test_idna_uses_unicode_32_casefolding(self):
        cases = [
            ('\N{CHEROKEE LETTER A}\N{CHEROKEE LETTER A}', b'xn--58da'),
            ('\N{GEORGIAN CAPITAL LETTER AN}.', b'xn--7md.'),
            ('\N{CYRILLIC LETTER PALOCHKA}.example', b'xn--d5a.example'),
            ('\N{ROMAN NUMERAL REVERSED ONE HUNDRED}.example.', b'xn--q5g.example.'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(text.encode('idna'), expected)

    def test_passwords_are_scoped_by_scheme(self):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, 'https://example.invalid/', 'synthetic', 'fixture')
        self.assertEqual(manager.find_user_password(None, 'https://example.invalid/path'), ('synthetic', 'fixture'))
        self.assertEqual(manager.find_user_password(None, 'http://example.invalid/path'), (None, None))

    def test_pop_commands_reject_controls_before_write(self):
        client = poplib.POP3.__new__(poplib.POP3)
        client.encoding = 'utf-8'
        client._debugging = 0
        written = []
        client._putline = written.append
        for codepoint in [*range(32), 127]:
            with self.subTest(codepoint=codepoint):
                with self.assertRaises(ValueError):
                    client._putcmd('NOOP' + chr(codepoint) + 'QUIT')
        self.assertEqual(written, [])
        client._putcmd('NOOP')
        self.assertEqual(written, [b'NOOP'])

    def test_tar_does_not_create_outside_intermediate_directory(self):
        for filter_name in ('tar', 'data'):
            with self.subTest(filter=filter_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                dest = root / 'dest'
                dest.mkdir()
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w') as archive:
                    member = tarfile.TarInfo('../escaped/../dest/sub/file')
                    member.size = 7
                    archive.addfile(member, io.BytesIO(b'content'))
                buffer.seek(0)
                with tarfile.open(fileobj=buffer) as archive:
                    archive.extractall(dest, filter=filter_name)
                self.assertFalse((root / 'escaped').exists())
                self.assertEqual((dest / 'sub/file').read_bytes(), b'content')

    def test_tar_link_fallback_honors_filter_none(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                symlink = tarfile.TarInfo('a/b/s')
                symlink.type = tarfile.SYMTYPE
                symlink.linkname = '../escape'
                archive.addfile(symlink)
                hardlink = tarfile.TarInfo('q')
                hardlink.type = tarfile.LNKTYPE
                hardlink.linkname = 'a/b/s'
                archive.addfile(hardlink)
            def filter_unsafe(member, destination):
                try:
                    return tarfile.data_filter(member, destination)
                except tarfile.FilterError:
                    return None
            buffer.seek(0)
            with tarfile.open(fileobj=buffer) as archive:
                archive.extractall(root, filter=filter_unsafe)
            self.assertTrue((root / 'a/b/s').is_symlink())
            self.assertFalse((root / 'q').is_symlink())
            self.assertFalse((root / 'q').exists())

    def test_zip_decompression_is_bounded(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2,
                       zipfile.ZIP_LZMA, zipfile.ZIP_ZSTANDARD):
            with self.subTest(compression=method):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w', compression=method) as archive:
                    archive.writestr('big', b'\0' * (4 * 1024 * 1024))
                buffer.seek(0)
                with zipfile.ZipFile(buffer) as archive, archive.open('big') as member:
                    self.assertLessEqual(len(member._read1(100)), member.MIN_READ_SIZE)


if __name__ == '__main__':
    unittest.main()

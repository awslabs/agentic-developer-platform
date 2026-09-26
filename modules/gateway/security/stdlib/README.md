# CPython 3.13.15 security backports

The gateway stays on Python 3.13. These runtime-library hunks come from merged
upstream **3.13** security backports; upstream test and documentation hunks are
excluded from the installed patch. Exact SHA256 checks guard all three files
before and after patching. A changed base fails closed until the bundle is
reviewed or removed. The runtime verifies its copied sources and removes stale
bytecode. The `patch` executable is installed only in the builder.

| Finding | Merged upstream 3.13 commit | Behavior |
| --- | --- | --- |
| CVE-2026-17084 | [c28b121](https://github.com/python/cpython/commit/c28b121a4f0b975937c8b5a1b4934bb361d84296) | IDNA uses Unicode 3.2 case folding |
| CVE-2026-15806 | [a2773a3](https://github.com/python/cpython/commit/a2773a34183b7d94a243bb98fd658926cc5348ce) | Password-manager credentials are scoped by scheme |
| CVE-2026-19672 | [c7979f3](https://github.com/python/cpython/commit/c7979f3a819011a3222bd16e671264b1e34282cb) | Tar filters normalize paths before directory creation |
| CVE-2026-87910 | [9c17bac](https://github.com/python/cpython/commit/9c17bace90f88dfba6d0e2fe23c8e7ae35f83955) | Tar link fallback honors a filter returning None |
| CVE-2026-82049 | [b8f23e3](https://github.com/python/cpython/commit/b8f23e307097552eaea2604383a12ab280520d0d) | Tar hardlinks resolve symlinks before relocation |

The patches apply with zero fuzz; two tarfile hunks use line offsets. The final
hashes independently verify the resulting source. CPython's license is retained
in `PSF-LICENSE.txt`. `check.py` exercises each security boundary with synthetic
fixtures, valid-operation controls and no external requests. All five methods
fail on the original 3.13.15 files and pass on the patched files.

Two further Python observations remain unresolved: CVE-2026-15310 (bounded ZIP
decompression; upstream 3.13 PR156738 is unmerged at review) and CVE-2025-15367
(POP3 command controls; no merged 3.13 backport identified). They remain owned
under #6112, reviewed 2026-09-26, without risk acceptance or suppression.
The original 214 frozen image occurrences and all 232 observations from the
later raw gateway scan remain retained. Version-only scanner matches may remain
after source backports; source hashes and behavioral evidence establish the
scope of a repair, not report disappearance. No deployment is implied.

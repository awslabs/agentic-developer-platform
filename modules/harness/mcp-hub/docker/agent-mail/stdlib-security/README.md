# CPython 3.14.7 security backports

These patches preserve the stable Python 3.14 runtime while applying reviewed
upstream security fixes that are absent from the pinned base. The build checks
exact SHA256 values before and after patching all five source files and removes
old bytecode. A future Python base update fails closed until these backports are
reviewed or removed. The combined patch contains only runtime-library hunks;
upstream test and documentation changes are not installed into the runtime.

| Finding | Upstream commits | Behavior |
| --- | --- | --- |
| CVE-2026-17084 | [1e54caa](https://github.com/python/cpython/commit/1e54caa096678a38afcabecabb1ff72400dd6bae) (3.14) | IDNA stringprep uses Unicode 3.2 case folding |
| CVE-2026-15806 | [a0d023f](https://github.com/python/cpython/commit/a0d023fbd23773e24b35d8368789470e22cda5d8) (3.14) | Password manager respects URL scheme |
| CVE-2025-15367 | [b234a2b](https://github.com/python/cpython/commit/b234a2b67539f787e191d2ef19a7cbdce32874e7) | POP3 rejects command control characters |
| CVE-2026-19672 | [9768834](https://github.com/python/cpython/commit/97688346ada2df3e5b9c279348862c3d64ab0823) | Tar filters normalize paths before creating directories |
| CVE-2026-87910 | [fb2f0bb](https://github.com/python/cpython/commit/fb2f0bbc3b35264f09cc2cb2934b7987527a6bc2) | Tar link fallback respects a filter returning None |
| CVE-2026-15310 | [31980e8](https://github.com/python/cpython/commit/31980e84b9a708424a0a1dfecde3fc991e313f89), [9d16799](https://github.com/python/cpython/commit/9d167992b59cf5e23c66b9ed742b13f5925f7d70) (3.14) | ZIP output is bounded; custom decompressors retain compatibility |

The tar patches apply with line offsets but no fuzz to Python 3.14.7. The POP3
patch applies without offsets. The other changes are the upstream 3.14 backports.
CPython's license is retained in `PSF-LICENSE.txt`.

Run the six offline security regressions against the built image:

```sh
docker run --rm --workdir /tmp --tmpfs /tmp:uid=10001,gid=10001,mode=1777 \
  -v "$PWD/stdlib_security_check.py:/checks/check.py:ro" \
  --entrypoint python agent-mail:review /checks/check.py -v
```

Run `container_runtime_check.py` as described in the parent validation document
for authenticated MCP messaging/search and native dependency compatibility.

Grype's frozen database still reports the Python version matches after these
source backports. This change neither alters the reported Python version nor
suppresses findings. Source hashes and failing-before/passing-after regressions
are the acceptance evidence for these six Python findings. Five native package
occurrences (zlib, nghttp2-libs and three BusyBox matches) remain for separate
review; the original 362 finding selectors remain intact, and no live rollout
is part of this change.

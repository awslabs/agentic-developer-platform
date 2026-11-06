# Pillow security backports

SkyPilot runs CPython 3.10 and two reviewed provider environments require Pillow
12.2.0 or 12.3.0. Pillow 13.0.0 fixes the five reviewed Critical advisories but
requires Python 3.11. No fixed 12.2.1 or 12.3.1 upstream release exists at review
time. The donor therefore builds 12.2.0+adp1 and 12.3.0+adp1 from the official
sdists, applying only these upstream production-code changes and the local
version identifier:

| Advisory | Upstream change |
| --- | --- |
| [CVE-2026-112393](https://github.com/advisories/GHSA-332p-7hrc-92wf) | Size TIFF memory writes from the seek position and zero new allocation space. |
| [CVE-2026-112403](https://github.com/advisories/GHSA-8g5g-8vmr-9782) | Reject JPEG scanlines that exceed the tile buffer. |
| [CVE-2026-112398](https://github.com/advisories/GHSA-5x8w-8jj5-q52h) | The same JPEG guard also rejects FPX tiles with excess output components. |
| [CVE-2026-96639](https://github.com/advisories/GHSA-v9pv-5gc8-w2m6) | Clear native storage after multi-frame mode/size changes. |
| [CVE-2026-112900](https://github.com/advisories/GHSA-w889-685w-c8fh) | Pass an absolute EPS filename to Ghostscript. |

`source-lock.json` binds the downloaded source, dependency archive, build helper
and complete upstream patches by SHA256. The TIFF patch URL resolves to a patch
whose mail header names a different commit; both the requested reference and
the observed header are recorded rather than treated as interchangeable.
The TIFF backport preserves the 12.x lowercase `min` macro and TRACE
instrumentation; its adapted patch is separately hashed in the lock. `git apply`
applies the production-code changes without fuzzy patching. Upstream
test hunks remain in the retained patches, and `../pillow_regression.py` adapts
them into standalone runtime checks.

The parent Dockerfile uses upstream's manylinux build process and pinned native
dependency versions to preserve the codecs. It bundles the upstream dependency
licenses in each wheel. The build generates `patched-source.json` with before
and after hashes and `pillow-wheel-lock.json` with the actual wheel hashes.
Both are retained in the immutable donor and installed runtime receipt directory.
The installer selects the matching provider line, verifies its wheel hash,
keeps all unrelated Python distributions unchanged and requires `pip check`.

Run `../pillow_regression.py` in the final runtime. The default EPS test checks
the actual subprocess arguments using a synthetic renderer; run it again with
`--case eps --real-ghostscript` in a disposable test image containing Ghostscript.
Keep raw scanner matches for these local-version backports with this evidence.
Python 3.11/3.13 recipes use Pillow 13.0.0+adp1, built from the official
13.0.0 source with no Pillow API patch. Every wheel uses the fixed Brotli 1.2.1
native decoder (CVE-2026-111497), including FreeType WOFF2 font decoding.
The build uses Pillow 13 codec versions plus this bounded Brotli update; the
runtime checks assert the loaded decoder version and exercise all codecs.

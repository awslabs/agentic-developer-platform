# Critical package remediation — 24 October 2026

This change follows the review of security runs `39239212223` and `39755005631`.
Their repeated scanner occurrences are not unique vulnerabilities. The separate
High-finding remediation remains in [PR #8721](https://github.com/aws-e/adp/pull/8721).

The package changes address these actionable advisory records:

| Package | Advisory | Remediation |
| --- | --- | --- |
| Pillow | CVE-2026-112393, CVE-2026-112403, CVE-2026-112398, CVE-2026-96639, CVE-2026-112900 | Pillow 13 for Python 3.11/3.13; four upstream patches backported to provider-compatible 12.2/12.3 on Python 3.10. |
| certifi | GHSA-6q48-6c7w-9gw2 | 2026.10.19 or newer removes the compromised root. Refresh pip and both virtualenv pip seed stores too. |
| uv | GHSA-hq85-5cpj-mr62 | Official 0.13.4 wheel and both canonical/root-local executables. |
| Brotli | CVE-2026-111497 | 1.2.1 for Python, Conda and Pillow's privately bundled FreeType decoder. |
| Next.js / sharp | GHSA-2464-fhr4-w9xw | Upstream-supported sharp 0.35.5 workaround, with its matching native packages, in DeepWiki and its curl overlay. |

The last three Pillow/Brotli records and the Next.js record were reconciled during
fresh candidate scans. The JPEG patch covers both JPEG scanline and FPX component
buffer checks. The multi-frame patch clears stale storage when mode or size changes.

Source and wheel locks live with the maintained recipes. The Pillow donor uses
Pillow 13's native build recipe plus Brotli 1.2.1, preserving JPEG/TIFF/font library
versions. Upstream and adapted patch hashes are recorded separately. Python 3.10
provider constraints remain intact; every unrelated Python distribution retains
its version. The PyTorch update uses Conda for Conda-owned packages and pip for
its pip-owned Pillow; PyTorch/CUDA native extension identity and CPU behavior are
checked during the build. GPU execution requires a separate GPU environment.

The old Pillow runtime fails the TIFF, JPEG width, JPEG component, EPS argument
and multi-frame storage regressions. Candidate acceptance exercises these paths,
codec round trips, actual Brotli decoder identity, trusted/untrusted loopback TLS,
pip/uv wheel installation, virtualenv seeds and requests Brotli responses.
DeepWiki acceptance calls Next's image optimizer with SVG and normal raster input.
The PKCS7 check accounts for unauthenticated CBC: valid padding can occur after
tampering, so it checks that the original plaintext is never recovered and that
observed failures have consistent errors. It does not treat padding as authenticity.

The final library scans have no Critical matches in browser, ingestion or PyTorch.
SkyPilot retains five Pillow version matches; DeepWiki retains one Next.js version
match, with the dispositions below. See `validation.json` for exact image IDs,
checks and scan hashes. The Pillow receipt's `native_dependencies` records the
upstream build recipe; actual enabled codec versions are checked at runtime.

Version-only scanners continue to match Pillow 12.x despite the exact backports,
and Next.js 15.5.24 despite its documented sharp workaround. Preserve these raw
matches with patch and runtime evidence; this change adds no blanket ignores.
Existing curl, OpenSSH, libxml2 and osslsigncode source/backport dispositions remain
separate. This package review does not certify every image or remove unrelated
High/Medium findings.

Candidate publication supplies immutable build inputs and a maintained SkyPilot
release pin. It does not deploy a workload. Raw scans and operational publication
receipts remain private; public evidence retains artifact hashes and sanitized
results only.

The 25 October merge validation re-recorded startup facts and bundled-package
versions from the published immutable SkyPilot image. All 555 SkyPilot Python
files match the previous locked image; see
[startup-source-preservation.json](startup-source-preservation.json). The new
observation preserves historical records and explicitly leaves interpreter CVEs
outside the library-only scan's scope.

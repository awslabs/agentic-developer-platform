# Signed curl repair candidates — 27 September 2026

All three immutable repair overlays are published; `publication.json` verifies
registry root digests against the scanned local roots. None has been rolled out.
The source recipe is `platform/security/curl-8.22.0/`; regular component workflows
do not automatically apply this emergency overlay.

| Candidate | Raw Critical / High | After exact curl review | After existing OpenSSH backport review |
|---|---:|---:|---:|
| Worker | 24 / 106 | 0 / 76 | unchanged |
| DeepWiki | 25 / 123 | 1 / 93 | 0 / 93 |
| SkyPilot | 32 / 184 | 8 / 154 | not applied |

These are native match occurrences per image, not unique cluster CVEs. The
18 curl advisories (8 Critical, 10 High) recur on each of three packages, producing
54 exact fixed dispositions per image. Debian's unfixed package ranges still
flag genuine upstream8.22; raw reports are preserved with receipt hashes and no
suppression. The DeepWiki OpenSSH disposition is independently bound to its
actual patched client binary/source and prior SSH compatibility tests; other
OpenSSH findings remain open. Remaining zlib, OS and application findings have
not been silently excluded from this table.

OpenSSL1835 and GnuTLS1831 upstream tests passed, as did both static HTTP/3
library unit suites. All96 versioned public symbols per TLS library are retained.
Nonroot Git HTTPS/TLS positive and refusal fixtures passed for all three images,
as did DeepWiki API/UI/cache/stdlib/Node, SkyPilot health/restart/status/task
parser and worker packaged tool/shared-contract checks. Original test images
and final licensed images have identical runtime configuration and inherited
layers; only three source/license directories were added, verified in
`licensed-test-binding.json`. Final licensed images were scanned independently.

Static ngtcp2/nghttp3 source hashes and licenses are retained alongside a
supplemental SBOM, zero-match raw Grype scan and upstream advisory responses.
This supplements filesystem scanner coverage; it does not claim undiscovered
vulnerabilities are impossible. Upstream curl removed TLS-SRP and RTMP; repository
search found no callers. These protocol changes still require deployment
compatibility review. Real provider, cluster and task execution acceptance remains
open. Reverting an overlay restores the older image's vulnerability exposure.

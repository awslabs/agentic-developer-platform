# CVE-2026-85091: exact Debian package applicability

**Ten frozen image/package matches are not affected by this specific CVE.**
This is an applicability determination for the exact Debian package and bytes
below, not a scanner suppression, whole-image clearance or deployment result.
Alpine 1.3.2 and Ubuntu packages are outside this determination and remain open.
The advisory still has affected occurrences, so this does not subtract one from
the unique open Critical/High advisory total.

## Why the Debian matches do not apply

The retained CNA record defines the affected range as zlib 1.3.1.2 through 1.3.2,
with default status unaffected. Its prerequisite is the nonblocking gzwrite
path retaining external buffer pointers, followed by `gz_vacate` during
`gzprintf`/`gzvprintf`. The upstream fix is
[df84af25dc1942490e1d1c899a07619152a46148](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148).
It resets `avail_in` and `next_in` on the nonblocking error return.

The examined binary is Debian `zlib1g` **1:1.3.dfsg+really1.3.1-1+b1**, built from
source **1:1.3.dfsg+really1.3.1-1**. This determination does not rely only on that
version string:

1. Debian Trixie's InRelease signatures verify against the archive keyring
   from the digest-pinned official Python 3.10.21/Trixie image
   `sha256:31dd4d9529d02d7436659061cb7564cd4733fc90e5e152709a942d53382ec8d0`.
2. The signed SHA256 entry matches the complete uncompressed Sources index;
   its zlib stanza hashes match the descriptor, original tar and Debian tar.
   The signed Packages.xz hash also matches, and its zlib1g stanza authenticates
   the downloaded binary package.
3. Extracting the authenticated source applies an empty Debian quilt series.
   Its `gzwrite.c` is byte-identical to upstream v1.3.1's file, and contains
   neither `gz_vacate` nor the nonblocking EAGAIN/EWOULDBLOCK implementation.
   The affected implementation is absent, rather than merely unused at runtime.
4. The library extracted from the authenticated binary package has SHA256
   `85590dd58edf5445e18bc7193e5ebc01ac5841f1ae187e97705a662e90c6421e`.
   Each of the ten frozen Syft SBOMs records that exact SHA256 for
   `/usr/lib/x86_64-linux-gnu/libz.so.1.3.1`. Their existing receipt binds the
   SBOM to the captured image/config identity.

`applicability.json` preserves those ten explicit image/package/file joins.
`artifact-hashes.json`, the source and binary stanzas, and signature-verification
log record the authentication chain. Complete raw artifacts are retained at
`/workspaces/projects/security27/zlib-source`; extracted source and binary are
in the sibling `zlib-extracted` and `zlib-binary` directories. The original audit
is `/workspaces/projects/security25/live-audit-20260927`.

The Debian descriptor's direct maintainer signature could not be verified with
the host keyring. It is authenticated through the signed archive Sources index;
this report does not claim that the direct descriptor signature passed.

## Conflicting tracker status and limits

The frozen Debian tracker still marks the source package open (Debian bug
1146895), and Grype retains the original match. That status is preserved. The
narrower code-and-byte evidence above supports **not affected for these exact
artifacts** despite the package-level tracker status. Any changed library hash,
source patch series or newly documented earlier affected path requires review.
No other zlib advisory, other package version, Ubuntu binary, Alpine binary,
statically bundled copy, node or host is covered.

Related to #6512 and #6521 under epic #6492. Preserve raw scans and apply this
explicit disposition only when deriving reviewed totals; do not alter the
frozen baseline or use an unbounded package-name ignore rule.

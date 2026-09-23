# Learnings — issue #5602 (S03: pinned SkyPilot image and its bundled vulnerable packages)

**Deliverable:** branch `agent/issue-5602` — a reproducible replacement-image recipe, exact
digest and provenance, per-finding disposition for five high findings plus the requested
lower-native-severity matches, executable package and live-runtime checks, and an S21 handoff.
**Persona:** `agent-developer` — implementation
**Outcome:** publish the derived linux/amd64 manifest
`sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`.
It preserves SkyPilot 0.12.3 and replaces setuptools 78.1.1 with 81.0.0. All four remediable
story findings are fixed; the disputed Python CPE match remains visible and is dispositioned
not applicable because CPython disputes it, no fix is listed, and the cited API is unused.

---

## 1. Resolve a bundled finding to the distribution that ships it

The pinned image disproved the obvious package-level fix:

```text
site-packages/wheel-0.46.3.dist-info/                          <- patched, NOT reported
site-packages/setuptools/_vendor/wheel-0.45.1.dist-info/       <- reported
```

Top-level `wheel` was already patched while setuptools' private copy remained vulnerable. Four
of the five reported packages were not independently installed components: jaraco-context and
wheel lived under `setuptools/_vendor`, jackson-core lived inside Ray's shaded
`ray/jars/ray_dist.jar`, and only cryptography was a top-level Python package.

**Generalizable:** a scan names a package and version, but the remediable unit may be the
distribution that contains that copy. Resolve the reported location first. For this image, the
setuptools findings required setuptools 81.0.0; upgrading top-level jaraco-context or wheel would
not touch the vulnerable paths.

## 2. A maintained upstream upgrade can be necessary but insufficient

SkyPilot 0.12.3 was the compatible upstream base. It upgrades Ray to 2.55.1, which contains
jackson-core 2.18.6, and upgrades cryptography to 46.0.5. However, both SkyPilot 0.12.3 and 0.13.0
still contain setuptools 78.1.1 with jaraco-context 5.3.0 and wheel 0.45.1 under `_vendor`.
Upstream SkyPilot 0.12.3 alone is therefore not the replacement image.

The complete repair is a reproducible OCI image derived from the immutable 0.12.3 base. The
recipe replaces the whole setuptools-owned tree with setuptools 81.0.0 using opaque whiteouts,
so files from 78.1.1 cannot survive underneath the new layer. It verifies every input hash,
normalizes layer metadata, and refuses output that does not reproduce the expected manifest
digest.

**Generalizable:** when an additive container layer replaces a distribution, copying new files
over old ones is not enough. Remove the old tree with correct OCI whiteout semantics, verify the
merged view, and make the output digest a build invariant.

## 3. Verify vendored code with behavior, inventory, and scan evidence

A version-only reading failed in both directions for jaraco-context. Version 6.1.0 leaves
`strip_first_component` textually unchanged; the fix is its composition with
`tarfile.data_filter`. Conversely, setuptools 81 can omit metadata layouts a scanner previously
used, so a disappearing match alone would not prove that the code changed.

The repair therefore used three independent signals:

- the traversal reproduction shows 5.3.0 escaping the destination while 6.1.0 raises
  `OutsideDestinationError`;
- Syft inventories jaraco-context 6.1.0 and wheel 0.46.3 at their setuptools-private paths,
  jackson-core 2.18.6 inside Ray's JAR, and cryptography 46.0.5 at its top-level path; and
- Grype no longer reports any of the four remediated advisories against the exact derived digest.

The old vendored metadata paths are absent from the merged image. The jaraco implementation also
matches the upstream 6.1.0 content hash, so metadata disappearance cannot masquerade as a fix.

**Generalizable:** for a vendored dependency, combine an exact-path inventory, a vulnerability
rescan, and behavioral or content evidence. Any one signal can be misleading by itself.

## 4. Record the whole rescan, not only the findings that disappeared

The unsuppressed exact-digest scan records every occurrence: 1 critical, 13 high, 33 medium,
11 low, 512 negligible, and 10 unknown. All non-story critical/high occurrences were inherited
from the immutable 0.12.3 base; the setuptools layer introduced none. The repository-configured
scan reports 0 critical and 2 high after existing S21-owned global rules, and neither output
contains the four remediated story advisories.

The disputed Python `CVE-2023-36632` match remains scanner-visible and is not suppressed.
`CVE-2020-15778` remains on OpenSSH with Debian-native severity Negligible and an explicit
reachability/vendor disposition. The earlier jsonwebtoken match is cleared because both `uv`
and `uvx` now contain jsonwebtoken 10.3.0.

**Generalizable:** run and retain an unsuppressed scan to distinguish a clean repair from a quiet
policy configuration. Document inherited findings and ownership rather than implying that a
focused layer refresh repaired unrelated base-image packages.

## 5. Compatibility requires live state and client checks

Static contract checks showed that the derived image preserves the SkyPilot 0.12.3 server
command, ports, health route, environment names, config path, writable-HOME behavior, and image
config. Live validation then exercised the exact OCI image rather than a rebuilt substitute:

- startup as uid 1000 and PostgreSQL persistence against a disposable PostgreSQL 16.15 server;
- creation of the required `clusters`, `storage`, and `users` tables in the expected database
  and schema;
- unauthenticated proxy rejection and authenticated health success;
- authenticated controller connectivity using the repository's maintained Go client;
- creation and retrieval of a user through the authenticated API; and
- SkyPilot process termination, observed 503, restart, health recovery, and retrieval of the
  same PostgreSQL-backed record after restart.

**Generalizable:** a matching command line is not persistence evidence. Verify startup,
authenticated client behavior, database state, failure during process loss, and recovery of a
record after restart against the exact artifact that will be published.

## 6. Ownership boundaries work when the handoff is executable

S21 solely owns the shared release pin and final image references, so S03 supplies the complete
OCI recipe, provenance, manifest digest, replacement facts, and coupled edit list rather than
changing those files independently. S21 must publish the derived manifest without media-type
conversion and atomically update the lock source/digest, manifest provenance, image facts, and
literal lock guard.

Focused tests couple the written findings, recipe provenance, derived facts, live harness, and
learning record to the same digest. A future change cannot quietly restore the incomplete
upstream-only recommendation while the repaired artifacts describe the derived image.

**Generalizable:** when another owner controls the final pin, make the handoff reproducible and
test its coupling constraints. The recommendation must name the publishable derived digest, not
merely the upstream base from which it was built.

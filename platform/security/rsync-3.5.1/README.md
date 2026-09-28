# SkyPilot rsync 3.5.1 repair

Build the `runtime` target with this directory as context. It layers the signed
upstream rsync3.5.1 release onto the immutable reviewed curl-fixed SkyPilot image.
The official GitHub release asset checksum authenticates the downloaded source;
its detached signature is verified against the retained release subkey. The key
is retrieved from OpenPGP's key service; official release NEWS identifies Zen Dodd
as a release maintainer. Both provenance sources are recorded, not inferred from
a keyserver name alone.

The replacement Debian package retains distro service/configuration and
maintainer integration, replaces the complete upstream install payload, records
its truthful3.5.1 version, regenerates checksums and retains source/license records
outside dpkg's excluded doc paths. It uses system zlib and existing shared
compression/checksum libraries; no hidden static dependency is introduced.
IDN support adds an explicit libidn2 dependency. Protocol33 is new; compatibility
with the installed3.4.1/protocol32 peer is tested in both directions.

Build the `build` target to run `verify-upstream.sh` in a disposable container with
`--network none`. Loopback TCP exposes no external port. Both unprivileged and
root/TCP suites were run; skipped sanitizer/platform-specific cases are retained
in the evidence. The version-mix tests cover ACLs, chmod, compression, deletion,
filters, hardlinks, in-place transfers, relative paths and xattrs. Run packaged
SkyPilot API health/restart/task parsing against the final runtime as well.

`review-candidate.py SCAN_DIRECTORY OUTPUT_JSON` preserves raw distro matches
and binds all24 rsync Critical/High findings to actual package/source/binary
identity plus fixed upstream advisory ranges. The existing curl review is rebound
to this exact image too. This is an explicit emergency overlay, not an automatic
change to ordinary component builds. Publication and live rollout acceptance are
separate; a rollback restores the earlier exposure.

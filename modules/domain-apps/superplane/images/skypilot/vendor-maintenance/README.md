# SkyPilot vendor maintenance

This offline layer replaces four Debian packages and four Python distributions
on the exact OS maintenance candidate recorded in `artifact-lock.json`.
It retains SkyPilot 0.12.3, Ray 2.58.0, PyJWT 2.14.0 and all other installed
package versions. The base platform manifest and configuration must be verified
before supplying the BuildKit OCI context; a floating registry tag is not a
substitute.

The Debian packages come from the Debian 13 security repository. Their retained
package-index hashes were checked against the signed InRelease document using
the base image's Debian archive keyring. The Python wheels match the SHA256
hashes published by PyPI for the named releases. The lock retains artifact URLs
and hashes. Supply those retained artifacts to `python3 prepare.py DIRECTORY`.
The artifacts directory is deliberately excluded from Git.

Build using a digest-bound local OCI context for the base platform recorded in
the lock, `--platform linux/amd64`, `--network=none`, and
`--build-arg BASE_IMAGE=skypilot-base`. The Dockerfile runs the installer through
a read-only bind mount, so staging artifacts do not remain in the runtime image.
APT has empty sources and package lists; pip has no index and cannot resolve
additional dependencies. Complete before/after distribution inventories reject
unexpected changes. `python-discovery` 1.6.0 is required by virtualenv 21.7.13.

Run `verify.py` from a read-only bind mount in the resulting image with
`--network none --read-only`, writable `/tmp` and `/home/sky` tmpfs mounts, and
`PYTHONDONTWRITEBYTECODE=1`. It exercises valid ordinary library operations,
local Git commit/read, offline virtualenv creation and activation, cryptographic
sign/verify, PCRE compilation and dependency compatibility. It does not attempt
vulnerability exploitation.

This is a local candidate recipe, not a release qualification or vulnerability
suppression. The composed candidate still needs a fresh scan, exact artifact
review and application verification, including the separately maintained Ray
shaded-Jackson fix.

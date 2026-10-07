# SkyPilot tool and Python high-severity maintenance — 7 October 2026

This package-only image supplies one bounded maintenance operation to the normal,
rsync, OpenSSH and high-security SkyPilot recipes. It is not a runnable release.
The consumer Dockerfiles pin its immutable registry digest; no deployment or
release lock is changed.

The operation replaces the three embedded provider Go helpers with the reviewed
`../go-security` artifacts, removes stale root-local `uv`/`uvx` executables in
favor of the already installed uv 0.12.19 binaries, and installs GitPython
3.1.60, urllib3 2.8.0, virtualenv 21.7.13 and its required python-discovery 1.6.0.
The four wheels come from checksum-pinned official PyPI downloads. `go-lock.json`
records the reviewed donor and exact old/new executable hashes; `uv-lock.json`
records the official wheel and executable hashes. The Go source, license,
upstream-test and publication-parity evidence remains in `../go-security`.

The installer fails on an unreviewed Python, SkyPilot, package version or tool
hash. Installation runs without network or dependency resolution; the full
provider environment must pass `pip check`, and every distribution outside the
four explicit updates must retain its version. This is a locked installation,
not a bypass for incompatible provider requirements. Existing Ray, crypto,
Pillow, cloud SDK, JWT and system-package fixes remain in their recipes.
The installer retains source locks, acceptance scripts and an installation
receipt under `/opt/adp-security/high-maintenance`.

Build this directory as a donor, validate it against each supported baseline,
publish it to the retained image repository, then update all four consumer
Dockerfiles with the resulting digest. The consumer `RUN --mount` prevents wheel
payloads from being retained as extra runtime layers. Preserve the existing
consumer user, entrypoint, command and deployment configuration.

Run the final candidate's `/opt/adp-security/high-maintenance/verify_runtime.py`
as UID/GID 1000 with no network, a read-only root and an executable temporary
`/tmp`. It verifies hashes and package versions, CRC32C file/range results, the
GKE credential contract with a synthetic local gcloud, SSM version, virtualenv
creation, offline uv/uvx install/execution, GitPython commit/clone with spaces in
the repository path, and an urllib3 loopback HTTP request. Also run the existing
SkyPilot `../verify_container.py`, SSH transport and JWT acceptance scripts on
every final candidate, and the high-security crypto/tar/provider acceptance on
that variant. These checks do not exercise live cloud provisioning or Postgres.

The 6 October reviewed snapshot assigns 168 high occurrences to these updates:
138 embedded Go, six stale uv, twelve virtualenv, six GitPython and six urllib3
matches. Reconcile fresh native reports against those exact records before
claiming remediation. This is not a count of unique vulnerabilities or all
repository highs. Preserve scanner reports and existing exact-artifact curl/SSH
reviews; unrelated matches and release-pinned external images remain open.

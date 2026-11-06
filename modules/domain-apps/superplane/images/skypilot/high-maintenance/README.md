# SkyPilot runtime maintenance

This package-only image supplies one bounded maintenance operation to the normal,
rsync, OpenSSH and high-security SkyPilot recipes. It is not a runnable release.
The consumer Dockerfiles pin its immutable registry digest.

The operation replaces CPython with the same-ABI 3.10.22 release while preserving
provider site-packages, and installs the patched kubectl 1.35.9 build. It replaces
the three embedded provider Go helpers with the reviewed
`../go-security` artifacts, replaces canonical and root-local `uv`/`uvx` executables with uv 0.13.4, and
installs certifi 2026.10.19, GitPython 3.1.62, urllib3 2.8.0, pip 26.2.2, Brotli 1.2.1 and python-discovery 1.6.0. These seven wheels come from
checksum-pinned official PyPI downloads. Virtualenv 21.7.13+adp1 retains the
existing dependency solve and refreshes its two embedded pip trust stores.
The Python 3.10+ seed is official pip 26.2.2; the Python 3.9 seed is the same
pip 26.0.1 code with certifi 2026.10.19, identified as 26.0.1+adp1. The builder
preserves licenses, regenerates wheel RECORD hashes, and updates the existing
seed hash checks. `seed-source-lock.json` and `seed-receipt.json` retain inputs
and output identities. The matching Pillow 12.2.0+adp1 or
12.3.0+adp1 wheel is built in the donor from locked source and four upstream
security patches; see [Pillow backports](pillow/README.md). `go-lock.json`
records the reviewed donor and exact old/new executable hashes; `uv-lock.json`
records the official wheel and executable hashes. The Go source, license,
upstream-test and publication-parity evidence remains in `../go-security`.

The installer fails on an unreviewed Python, SkyPilot, package version or tool
hash. Installation runs without network or dependency resolution; the full
provider environment must pass `pip check`, and every distribution outside the
nine explicit updates must retain its version. This is a locked installation,
not a bypass for incompatible provider requirements. Existing Ray, crypto, cloud SDK, JWT and system-package fixes remain in their
recipes. PEP 440 local versions satisfy both existing provider Pillow pins
without changing provider SDK metadata.
The installer retains source locks, acceptance scripts and an installation
receipt under `/opt/adp-security/high-maintenance`.

Build this directory as a donor, validate it against each supported baseline,
publish it to the retained image repository, then update all consumer
Dockerfiles with the resulting digest. The consumer `RUN --mount` prevents wheel
payloads from being retained as extra runtime layers. Preserve the existing
consumer user, entrypoint, command and deployment configuration.

`Dockerfile.runtime` applies this same bundle to the current qualified release.
Build it with the reviewed donor, run the runtime acceptance checks, publish the
result, and update `releases/superplane.lock.yaml` with the registry digest and
publication receipt. This keeps the release-pinned scan on the maintained runtime.

Run the final candidate's `/opt/adp-security/high-maintenance/verify_runtime.py`
as UID/GID 1000 with no network, a read-only root and an executable temporary
`/tmp`. It verifies hashes and package versions, kubectl client-side object generation,
CRC32C file/range results, the
GKE credential contract with a synthetic local gcloud, SSM version, virtualenv
creation, offline uv/uvx install/execution, GitPython commit/clone with spaces in
the repository path, and an urllib3 loopback HTTP request. It also runs the TIFF/JPEG/EPS
regressions and PNG/JPEG/TIFF/WebP/AVIF/JPEG2000 round trips. TLS checks reject
an untrusted loopback certificate, accept an explicitly trusted certificate,
and install a fixture wheel through uv over verified TLS. The compromised
emSign ECC Root CA - G3 must be absent from the loaded certifi store. uv
executable hashes bind its separately maintained built-in store to the
reviewed official wheel. Also run the existing
SkyPilot `../verify_container.py`, SSH transport and JWT acceptance scripts on
every final candidate, and the high-security crypto/tar/provider acceptance on
that variant. These checks do not exercise live cloud provisioning or Postgres.

The 6 October reviewed snapshot assigns 168 high occurrences to these updates:
138 embedded Go, six stale uv, twelve virtualenv, six GitPython and six urllib3
matches. Reconcile fresh native reports against those exact records before
claiming remediation. This is not a count of unique vulnerabilities or all
repository highs. Preserve scanner reports and existing exact-artifact curl/SSH
reviews; unrelated matches and release-pinned external images remain open.

The October 24 Critical update addresses CVE-2026-112393, CVE-2026-112403,
CVE-2026-112900, CVE-2026-112398, CVE-2026-96639,
GHSA-6q48-6c7w-9gw2, GHSA-hq85-5cpj-mr62 and CVE-2026-111497. Version-only
scanners may still match the Pillow 12.x backports. Keep those raw matches
with the exact source, patch, wheel and runtime evidence; no blanket ignore
is added. Publishing candidate inputs and updating release pins does not
roll out running workloads.

The same donor also supplies source-built Pillow 13.0.0+adp1 for the reviewed
Python 3.11/3.13 amd64 image recipes. All Pillow wheels link Brotli 1.2.1,
including the FreeType WOFF2 path; upgrading only the Python Brotli module
would leave the privately bundled decoder unchanged.

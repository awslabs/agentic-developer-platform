# Superplane API certifi maintenance

GHSA-6q48-6c7w-9gw2 affects the application certifi bundle and pip's vendored
bundle. This overlay installs checksum-pinned certifi 2026.10.23 and pip 26.2.2
without resolving other dependencies. Future source builds enforce the patched
certifi floor in the API's `pyproject.toml`.

The overlay also removes the exact compromised root from the system CA store,
which pip's system trust integration can consult. It checks the DER fingerprint,
persists the Debian exclusion, refreshes the bundle and hashed directory, and
requires every unrelated root to remain present. Build with the repository root
as context and this Dockerfile selected with `-f`.

Use this overlay when an installed API or transport image needs the trust-store
repair before a separately reviewed application/schema upgrade. Supply the
observed immutable image as `BASE`, its original non-root `Config.User` as
`RUNTIME_USER`, and the reviewed maintenance commit as `SECURITY_REVISION`.
The inherited application-source labels remain intact; separate maintenance
labels record the base and repair revision. Never use a floating base tag.

Before promotion, compare the original and repaired `/app` file hashes and image
configuration (except the added maintenance labels), run `python -m pip check`,
and run `../skypilot/high-maintenance/verify_tls.py` inside the repaired image.
The TLS fixture verifies removal of the affected root from both bundles and
checks trusted acceptance and untrusted rejection. Scan the exact published
digest, retaining unrelated findings.

Promote only the reviewed image fields with observed object version and prior
image preconditions, validate the patches server-side first, and require healthy
rollouts. Preserve installation ownership, sidecars, configuration, routes and
database schema. If readiness fails, restore the prior image only while the
failed candidate is still the current deployment generation. Record private
receipts for each installed consumer, including both API recovery transport and
SkyPilot authenticated transport when present. This repair does not establish
that other packages or the host are free of vulnerabilities.

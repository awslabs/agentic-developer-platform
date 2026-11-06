# System CA maintenance

Older images can retain the compromised emSign ECC Root CA - G3 in the operating
system store after certifi and pip have been upgraded. Python's default SSL
context and pip's system trust integration can still consult that store.

This overlay disables only DER SHA-256
`86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`.
It preserves every other loaded root and records a persistent Debian CA exclusion.
An unexpected certificate identity or any unrelated trust-store change fails
the build. Reapplying the repair is safe.

Build from the repository root with this Dockerfile selected using `-f`. Supply
the installed immutable `BASE`, its original non-root `RUNTIME_USER`, and the
reviewed `SECURITY_REVISION`. Keep the inherited application and source labels;
the overlay adds separate maintenance provenance. It changes no application code,
database schema, service configuration or permissions.

Run `images/skypilot/high-maintenance/verify_tls.py` from the Superplane module
inside the exact published candidate. It checks certifi, pip and the default
system CA context. Preserve raw vulnerability scans and compare application
hashes and image configuration with the base. Use guarded image-only promotion,
require healthy live probes, preserve installation ownership and retain receipts.
For SkyPilot, update its existing Terraform-owned SSM image pin through a reviewed
plan together with the installed image; do not introduce another state owner.

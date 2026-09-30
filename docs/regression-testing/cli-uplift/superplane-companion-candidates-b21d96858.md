# Superplane controller and monitor build candidates

The fresh Superplane upgrade requires a complete release. Controller and monitor
source trees differ from the deployed release, so both were built from the same
reviewed CLI integration commit as the separate API candidate:
`b21d968586747748f3b605247a33fad328d4697a`.

The companion JSON records the two successful CodeBuild runs and immutable ECR
digests. Each uploaded source archive has SHA-256
`ecbbd8d58d08cb38791cd1f40ef3be34916d381acd046871b2ad5ddba22a61ac`,
matching the already verified API/gateway archive. ECR manifest and configuration
bytes independently match their recorded digests. Configuration labels identify
the exact ADP revision, component source path and historical upstream origin;
both images are Linux/amd64.

These are build candidates only. No release-lock promotion, Kubernetes rollout,
database migration, route registration or permission change was performed.
The private upgrade candidate preserves the deployed SkyPilot 0.12.0 digest
`sha256:de41a5c61e6b62700795c64368cd1560348f27f33ebb8d7c0eb4faaba0d75019`.
Read-only deployment metadata, network-policy enforcement, backup and migration
ownership, and the fresh installer's verified plan remain required before apply.

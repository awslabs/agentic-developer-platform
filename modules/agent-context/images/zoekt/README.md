# ADP Zoekt security build

Build with `docker build -t "$ZOEKT_IMAGE" modules/agent-context/images/zoekt`.
Publish the image and use its immutable digest for both the search Deployment's
`ZOEKT_IMAGE` and ingestion's `--build-arg ZOEKT_IMAGE=...`.

The upstream revision remains `153817f643cde8b229ee388c1dddbcf07f4798af`,
matching the existing shard format. Go 1.26.8 and the checked-in module lockfiles
replace vulnerable runtime and module versions. The final image is a static,
nonroot search server with CA certificates. Indexing takes place in ingestion,
which copies this image's matching binary and provides git and language tools.
The search server does not invoke git, a shell, DNS utilities or ctags.

Do not use this dedicated search image as an indexserver or general shell image.
Before rolling out, smoke-test old and new shard reading and scan the image with
the deployment's frozen vulnerability database. Keep the image digest in the
release receipt; a successful build alone is not evidence of closure.

The previous Alpine-only Critical remediation and its evidence remain recorded
in `docs/security/runs/2026-09-27/zoekt-critical/`. This build also replaces the Go
binaries to address remaining High findings; executable hashes therefore change.

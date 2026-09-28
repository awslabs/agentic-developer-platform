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

The serving command is now `zoekt-auth-proxy`. It requires `ZOEKT_API_KEY`,
starts the raw webserver on `127.0.0.1:6071`, and exposes only authenticated
`POST /api/search` on 6070. `/healthz` returns readiness without index content.
The deployment creates `zoekt-backend-auth` once and mounts its key only into
Zoekt and the Door; it is separate from the shared Door caller credential.
Deploy both the new Zoekt image and the updated Door client together. Existing
images lack the authentication proxy and cannot be used with this manifest.
To rotate this key, coordinate updates of the Secret and both Deployments;
a mismatched or missing key refuses searches instead of allowing anonymous access.

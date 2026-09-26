# Context image and pod identity transition — #6105 / #6122

Status: preparation; live acceptance is open. This change must be integrated with the matching six-image UID/GID10001 build and reviewed image digests. It is not independently deployable with the previous1001 images.

The image owner bakes LiteLLM dependencies and the embedding health patch into the image. The manifest invokes that binary directly with read-only root, dropped capabilities, UID/GID10001 and writable home/tmp. DeepWiki retains its entrypoint and API/UI ports, moves its disposable cache from /root/.adalflow to /home/appuser/.adalflow, provides tmp and Next cache mounts, and mounts generator/embedder configuration read-only.

Ingestion, parser, context-mcp and CodeGraph must share their declared10001 storage identity. All ingestion consumers, including the scaled worker and migration Job, move together. The Mountpoint PV synthetic ownership and Zoekt reader supplemental group change together; these are necessary consumers beyond the original20-manifest scanner list, not a replacement denominator. Original244 findings remain retained.

## Required operator acceptance before rollout

1. Obtain approved read-only inventory for the target cluster and namespaces. Current default instance role cannot list workloads; no further denied inventory/probe retries.
2. Bind each deployment to a reviewed OCI repository digest and archive-verified config identity. Validate actual startup/probes/configuration and write paths under these rendered pod constraints.
3. Inventory every PV/PVC consumer, driver version and mount configuration. A changed fsGroup does not change Mountpoint synthetic ownership. Preserve existing object content and Retain reclaim policy; do not chown S3 or widen0640/0750 permissions. Prepare a reviewed new mount/PV transition with rollback to old image+old mount identity, avoiding mixed1001/10001 writers. Existing bound storage is not proven migrated by editing this template.
4. For ordinary CodeGraph PVCs verify fsGroup support and existing-file access in a disposable clone before changing serving pods. Preserve the old claim and rollback image; do not delete the claim for acceptance.
5. With the specific rollout authorization and target confirmed, perform scoped canaries, IRSA access, semantic parsing, readiness, required writes, denied writes and rollback. Existing gateway/tick holds and previously denied Mountpoint probes remain effective.

Local Docker/tmpfs fixtures cannot discharge steps3–5. No live resources have been changed. All unresolved controls remain owned by their existing parent stories.

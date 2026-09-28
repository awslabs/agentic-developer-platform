# Parser isolation: interim source boundary and remaining acceptance

PR #6069 is an interim fail-closed repair for #6059. It does not complete #6059
or authorize deployment. Production has no canonical grant issuer and refuses
structural parsing. Even with injected test grants, ingestion refuses external
publication until server-owned asset and graph authority are integrated. It
never falls back to parsing in the credential-bearing ingestion process.

The ingestion source is a fresh private clone per invocation, removed on every
return path. No persistent source-reader snapshot is published by this path;
existing readers must not interpret this as a refreshed persistent checkout.
Validated parser output is frozen before handing it to a consumer. Validation
alone does not confer S3 or Neptune write authority.

Docker requires an immutable image identifier, no network, a read-only root and
source, non-root identity, dropped capabilities, bounded memory/processes/tmpfs,
and forced container removal on completion and timeout. The subprocess backend
is an explicit test seam, unavailable through the production selector. A local
Docker test does not establish Kubernetes CNI or production workload isolation.

Before production activation or issue closure, the owning stories must supply:

- A09/S12: canonical server-verified run, tenant, asset, attempt and source-commit
  bindings. The current `ProductionAuthorizer` denies all requests. Test grants
  are synthetic and must never become a production fallback.
- Fetch authority: approved exact source/registry destinations, immutable source
  revision, expiry and cancellation, transport-level byte/deadline limits and
  redirect enforcement. Current dependency preparation has post-fetch size
  checks, not a transport-level byte quota. npm/Go client behavior alone is not
  sufficient evidence for that boundary.
- Offline dependency handoff: demonstrate TypeScript and Go indexers consume
  prepared artifacts without resolver downloads or repository-controlled
  configuration. The current `/deps` mount is not yet wired into those indexers.
- Publication authority: exact server-approved S3 objects and a graph-specific
  Neptune mutation contract, replay protection and conditional commit. The
  prototype publish grant is insufficient for ambient graph writes. The legacy
  ingestion writer must remain unreachable from this path until replaced.
- Source-reader handoff: scoped publication of the verified fresh source for
  downstream readers, plus explicit legacy persistent-data disposition.
- Runtime acceptance: actual job creation/staging, immutable image admission,
  absence of tokens/credentials/shared platform mounts, enforced CNI denial
  including metadata, resource exhaustion/timeout cleanup, and successful
  allowed parsing/publication under canonical grants.

Track each acceptance item on #6059 and its A09/S12 dependencies. Do not close
#6059 based on merged source, mocked backend tests, or synthetic grant success.

## Local review evidence (2026-09-25)

The reviewer built the parser image locally and exercised the real Docker
backend with a fresh Python fixture and synthetic three-stage grants. The
indexer produced a nonempty SCIP file; the normal output validator accepted its
size/digest/bindings. Checks inside the running container confirmed UID 1001,
no AWS/GitHub environment credentials or service-account token, denied writes
to source/root, and denied connections to an external address and EC2 metadata.
The container was removed and the caller's source stayed unchanged.

This test exposed and repaired inherited source-directory permissions,
`scip-python`'s dependency on Git version metadata, and Docker archive copying
of tmpfs output. Output now uses a bounded regular-file archive export;
container removal precedes validation/consumption. No ECR image was pushed,
no Kubernetes job deployed, and no canonical production grant was issued.

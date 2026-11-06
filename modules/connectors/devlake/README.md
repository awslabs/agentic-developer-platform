# Pinned DevLake component packaging

This optional build does not install, authorize or publish a connector. The API
and collector worker are separate images, both built from the locked Apache
DevLake commit and ADP customization commit. The API starts without a database
or Insights; its health response reports **packaged**, not provider readiness.
The worker's `--verify-package` inventories linked code. Insights owns the
[admitted collection runtime](../../../docs/engineering-insights/devlake-poc/collector-worker.md);
the generic native decoders remain in this connector module.

The source lock fetches official Docker Library base images through Google's
`mirror.gcr.io` cache to avoid Docker Hub's shared unauthenticated pull limit.
Both images retain their reviewed SHA-256 digests; a missing or different
manifest fails the build instead of falling back to an unpinned tag.

From a clean committed ADP checkout with Go 1.26.0, access to public Go
modules and the pinned OCI runtime base, build the API as a local OCI layout
without a Docker daemon:

```bash
revision=$(git rev-parse HEAD)
modules/connectors/devlake/scripts/build.sh \
  --customization "$revision" --component api \
  --output "/tmp/devlake-api-$revision.oci"
```

Build the independent worker only when selected:

```bash
revision=$(git rev-parse HEAD)
modules/insights/poc/devlake/scripts/build.sh "$revision" "/tmp/devlake-worker-$revision.oci"
```

The output directories must not exist before the build. Each contains an OCI
image index, a digest-verifiable image manifest and blobs. No registry push or
publication receipt occurs. An alternate Dockerfile path exists with
`--engine docker --tag localhost/devlake-api:<revision>` when Docker is available.

Both image recipes place `source-manifest.json`, `schema-manifest.json`, the
module contract, and the upstream `LICENSE`/`NOTICE` under `/usr/share/devlake`
and `/usr/share/licenses/devlake`. Inspect those and the resolved image digest
before any separately authorized publication. The source manifest records the
source, patch, dependency, architecture and base-image pins. Build errors have
no publication receipt; no shared pipeline activates this optional module.

To check the source, compile both packages and independently inspect the OCI
image manifests and final layers from two clean workspaces, use Go 1.26.0 and
upstream/OCI-base network access:

```bash
DEVLAKE_SOURCE_INTEGRATION=1 python3 -m unittest discover \
  -s modules/insights/tests/devlake_poc -p 'test_s09.py' -v
```

To exercise the declared API and worker build scripts from separate clean ADP
checkouts and inspect each resulting OCI image against the locked schema and
default-off contracts, run with Go 1.26.0 and upstream/OCI-base network access:

```bash
modules/insights/poc/devlake/scripts/verify-poc.sh
```

The verifier prints both digests and removes its temporary checkouts and images.
These offline checks do not prove ECR readback, deployment, or provider/runtime behavior.
Those checks remain separate acceptance requirements.

Publication is separate from the optional build. The `DevLake POC` workflow
checks connector and Insights changes on pull requests without publishing. An
operator must explicitly dispatch it on the default branch with `publish=true`
and choose `api` or `worker`; the protected `adp-devlake-publisher` environment
supplies `ADP_DEVLAKE_ACCOUNT`, `ADP_DEVLAKE_REGION`, `ADP_DEVLAKE_PUBLISH_ROLE`
and the private `ADP_DEVLAKE_CONNECTION_LABEL` secret. No shared deployment
workflow publishes this component.

Before any registry access, the selected job builds the locked component and
generates private `checks.json` and `sbom.json` evidence from source, Go package,
image, license, pinned Syft and Gitleaks checks and a protected finalizer
preflight. The publisher checks that evidence, selected identity, immutable
repository and ECR digest readback before writing its private receipt. Missing
checks, scanner tools or finalizer authority prevent publication; the finalizer
and its ledger are not yet implemented, and no ECR target has been verified.

Retry only by rerunning the selected workflow from pinned source so it rebuilds
and repeats all checks. The publisher needs scoped ECR `BatchGetImage` permission
to reconcile an interrupted upload: an existing tag is reused only if its
account, repository and digest match the checked image, followed by independent
registry and ECR readback. A failed upload, ambiguous lookup or conflicting tag
has no publication receipt. The publisher never deletes an earlier accepted
image; terminal cleanup belongs to the protected finalizer.

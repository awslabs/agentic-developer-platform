# Publishing reviewed OCI images

`publish_oci.py` publishes already reviewed linux/amd64 OCI bytes with Skopeo's
`--preserve-digests`. It supports Python 3.12 maintenance bases mirrored separately
into the existing API and paid-worker repositories, and the qualified SkyPilot
image into its existing repository. It creates no repositories or IAM grants.
The consumer build roles need only their existing repository access.

The operator approves the exact platform/config digests and review evidence before
publication. The evidence hash binds a file; this utility does not interpret it
as security approval or bypass the application release gate. Diagnostic images
carrying `com.adp.local-diagnostic` are refused. Never relabel a diagnostic
application image into a production release.

Use Python with boto3 and a supported Skopeo installation. The input is an OCI
layout directory, not a Docker save archive. The selected platform must be
reachable from `index.json`. Config, compressed blobs and uncompressed diff IDs
are verified. OCI tar and gzip layers are supported and never recompressed.
A private snapshot contains the selected platform only; source indexes,
attestations and platform blobs remain unchanged.

Before publishing bases, apply the reviewed app-owned control-plane ECR lifecycle
change: API and paid-worker retain tagged inputs alongside executor and SkyPilot.
Untagged cleanup remains enabled. Publication refuses mutable tags, wrong
account/repository identities, and lifecycle rules that can expire tagged images.
No lifecycle policy means no automatic expiration; an authorization error never
means a missing policy or image.

Supply the following values from private operator configuration and reviewed
artifacts. Run once per consumer, with a different receipt path each time:

```bash
python3 modules/domain-apps/superplane/releases/publish_oci.py \
  --kind python-base \
  --account "$ACCOUNT_ID" --region "$AWS_REGION" \
  --repository adp-superplane-api \
  --layout "$BASE_OCI_LAYOUT" \
  --platform-digest "$BASE_PLATFORM_DIGEST" \
  --config-digest "$BASE_CONFIG_DIGEST" \
  --review-evidence "$BASE_REVIEW_FILE" --review-sha256 "$BASE_REVIEW_SHA256" \
  --receipt "$API_BASE_RECEIPT" --publish
```

Repeat with `--repository adp-superplane-paid-worker` and a separate receipt.
Both repositories then contain the identical platform digest. Omitting
`--publish` performs local preparation only; it neither checks AWS nor claims
publication. Immutable tags are `python-base-<full platform hash>`.

SkyPilot uses the same exact-byte path and the `skypilot-<full platform hash>` tag:

```bash
python3 modules/domain-apps/superplane/releases/publish_oci.py \
  --kind skypilot \
  --account "$ACCOUNT_ID" --region "$AWS_REGION" \
  --repository adp-superplane-skypilot \
  --layout "$SKYPILOT_OCI_LAYOUT" \
  --platform-digest "$SKYPILOT_PLATFORM_DIGEST" \
  --config-digest "$SKYPILOT_CONFIG_DIGEST" \
  --review-evidence "$SKYPILOT_REVIEW_FILE" --review-sha256 "$SKYPILOT_REVIEW_SHA256" \
  --receipt "$SKYPILOT_PUBLICATION_RECEIPT" --publish
```

A successful receipt says `published` or `reused`, binds the review and publisher
script hashes, and confirms exact registry manifest bytes. A copied image also
records the Skopeo version. A conflicting immutable tag fails.
The private ECR authfile is deleted on exit and tokens never appear in arguments
or receipts. Receipt files are private, exclusively reserved, atomically updated
and flushed before the upload. Keep them outside public documentation.
If transport or post-upload verification fails, keep the `outcome_uncertain`
receipt and rerun with a new receipt path. Exact registry readback safely reuses
the tag. A `blocked` receipt before upload does not claim registry absence.

## Genuine application builds

Use an exact clean checkout of the selected merged source SHA. Record the SHA,
source tree hashes, explicit base references, real build logs and ECR digest
readback. Do not label an image with a newer SHA than its actual source. The
maintained build script accepts local Docker builds; CodeBuild is optional.

From that checkout, set `ACCOUNT_ID`, `AWS_REGION`, and the verified
`BASE_PLATFORM_DIGEST`, then run:

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
export IMAGE_TAG="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$IMAGE_TAG" origin/main
export REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
export ACCOUNT_ID AWS_REGION
for component in superplane-api superplane-paid-worker; do
  inputs="$(python3 modules/domain-apps/superplane/releases/resolve_lock.py "$component")"
  while IFS='=' read -r key value; do
    export "$key=$value"
  done <<< "$inputs"
  export PYTHON_IMAGE="$REGISTRY/adp-$component@$BASE_PLATFORM_DIGEST"
  bash modules/domain-apps/superplane/releases/build-image.sh "$component"
done
```

The API tag includes the source SHA and full base hash; its provenance binds the
full API repository base reference. The paid-worker tag is its full source SHA;
a different image already using that immutable tag must be reconciled, never
overwritten. Keep the selected source and lock metadata truthful when promoting
actual resulting digests.

Collect each new application image once and run the release's native/SARIF scan,
runtime checks and occurrence/file evidence bindings against those actual bytes.
Package operations during a rebuild can change dependencies. Prior source,
package and embedded-library evidence is reusable only after byte equality.
Do not rescan unchanged base, SkyPilot, controller or monitor artifacts solely
because these registry references changed. Promote SkyPilot's maintained lock
only after its publication receipt and independently approved release gate.

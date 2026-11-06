# Reading a run's original sources

Release verification and authorized users can read the immutable source captured
for a run after its worker has completed. The public Product API uses current
run and source permissions. It does not resume execution, issue provider
credentials, extend a lease or permit a new provider operation.

All paths start with
`/v1/products/{product}/projects/{project}/services/{service}/runs/{run}`.

| GET suffix | Response |
| --- | --- |
| `/source-manifest` | The run's stored source manifest and snapshot IDs; large manifests require explicit pagination |
| `/source-snapshots/{snapshot}` | The original Product snapshot receipt |
| `/source-snapshots/{snapshot}/manifest` | Exact versioned file inventory bytes, with bounded byte-range support |
| `/source-snapshots/{snapshot}/content` | Exact versioned source content bytes, with bounded byte-range support |

The caller needs the same current Product, project, run visibility and source
access required to view the run. Service-specific run restrictions still apply.
Snapshot membership, tenant, Product, project, admission delegation and original
source reference must match the stored run. Expired snapshots return `410`;
missing or unauthorized selections return `404`. Integrity changes return `409`.
There is no caller-supplied bucket, object key, role or provider endpoint.

Whole content and inventory reads up to 6 MiB retain their existing `200`
response and full byte-count/SHA-256 verification. Larger artifacts return `416`
with `detail.code=source_range_required`, a range instruction and the artifact
size. They are not reported as unsupported captures.

Send a single explicit `Range: bytes=start-end` containing at most 1 MiB to read
either artifact in bounded chunks. A successful range returns `206`, the exact
`Content-Range: bytes start-end/total`, `Accept-Ranges: bytes`, `X-Artifact-Size`,
`X-Artifact-Sha256`, `X-Artifact-Version` and the full-artifact SHA-256 as its
quoted `ETag`. The last range ends at `total - 1`; open-ended, suffix, multiple,
out-of-bounds and oversized ranges return `416` with
`detail.code=invalid_source_range` and `Content-Range: bytes */total`.

Each range pins the original storage version and verifies returned version,
length, range and available digest metadata. The caller must verify every range
header, assemble the exact byte sequence and verify its total size and SHA-256
against the original receipt before using the artifact. A partial range cannot
independently establish the whole artifact's digest. Current authorization and
snapshot identity are checked both before storage access and before bytes are
returned, including after permission revocation during a read.

The run manifest retains its original JSON response when its canonical encoded
size is at most 6 MiB. Larger manifests return `413` with
`detail.code=source_manifest_pagination_required`, `file_count`,
`source_snapshot_ids`, `manifest_sha256` and a relative `next_page` API URL.
Nothing is silently omitted from an apparently complete manifest.

To request pages explicitly, use `/source-manifest?limit=1000`; `limit` accepts
1–1000 files, and each encoded response is also bounded to 1 MiB. The response
is an envelope containing `manifest` (all original metadata and the current
`files` slice), `file_count` (the full inventory count), `offset`,
`manifest_sha256` and `next_cursor`. Continue with
`/source-manifest?limit=1000&cursor={next_cursor}` until `next_cursor` is null.
The byte ceiling can produce fewer files than the requested limit. Append each
page in offset order and replace `manifest.files` with the complete inventory.
Verify the full count and SHA-256 of the reconstructed manifest encoded as
UTF-8 JSON with sorted object keys, compact separators and unescaped Unicode.
Every page carries the same full-manifest digest, also in `X-Manifest-Sha256`.

Cursors bind the offset to that full digest; changed manifests return `409`
`source_manifest_changed`, and malformed or out-of-bounds cursors return `422`.
Each page rechecks current run/source visibility. Metadata or a single entry
that cannot fit a page fails explicitly with `413`
`source_manifest_metadata_too_large` or `source_manifest_entry_too_large`.
The separately retained snapshot inventories remain available through the
range endpoints; callers must not interpret an incomplete page as a full scan.

All responses are not cacheable. These routes supply original source evidence;
they do not establish native-provider success or replace a service's independent
result verification. Existing clients that only support whole responses must
add pagination/range reconstruction before handling sources above 6 MiB.

Worker source capture and execution continue to use the separate internal APIs
and their active work grants. A release driver uses its authorized private
Product session for the public routes and verifies the returned manifest,
receipt and bytes without fabricating an execution lease.

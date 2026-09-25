# Knowledge and indexing

`adp knowledge` uses the existing authenticated knowledge-asset registry. It does
not create another indexing service. The gateway must enable
`AGENT_CONTEXT_ENABLED` and configure its agent-context database and ingestion
queue. Missing services, denied access, and failed indexing are reported without
claiming an asset is ready for retrieval.

```sh
adp knowledge list --scope personal --type repo --page 1 --page-size 20 --json
adp knowledge show ASSET_ID --json
adp knowledge status ASSET_ID --json
adp knowledge watch ASSET_ID --timeout 300 --interval 5 --json
adp knowledge add --file asset.json --key UUID             # preview
adp knowledge add --file asset.json --key SAME_UUID --yes
adp knowledge reindex ASSET_ID --key UUID                  # preview
adp knowledge reindex ASSET_ID --key SAME_UUID --yes
adp knowledge delete ASSET_ID                             # preview
adp knowledge delete ASSET_ID --yes
adp admin indexing list --page 1 --page-size 20 --json
adp admin indexing show --run RUN_ID --json
```

An add file is the canonical `AssetCreateRequest`, for example:

```json
{"asset_type":"url","source_ref":"https://example.org/guide","scope":"personal"}
```

Supported types are `repo`, `url`, and `doc`; sources are credential-free HTTPS
or S3 references. Put repository credentials in the existing connections flow,
never in the source URL. Server-side source admission, repository accessibility,
owner, tenant, and quota checks still apply. Public repositories can resolve to
the existing shared scope. This CLI does not invent project assignment parameters
that the canonical create schema does not support.

Registration and reindex acknowledgements return `pending` (exit 4). Inspect the
real run and stages with status/watch; queued or registered does not mean usable.
Status contains run/stage timestamps and hashes of source/artifact references.
Free-text errors, metadata, and protected URLs are omitted; error presence and
stage failure remain visible. Successful indexing evidence does not itself prove
that a particular agent task has retrieved the content. Watch emits NDJSON with
`--json`, has a maximum one-hour deadline, and Ctrl-C detaches (exit 130) without
cancelling indexing. Admin run queries keep the existing stricter `require_admin`
guard.

Deletion is **soft removal**. Index artifacts remain; no graph, database, or
object-store deletion is implied.

## Bulk preview and commit

```json
{"scope":"personal","items":[{"asset_type":"url","source_ref":"https://example.org/guide"}]}
```

```sh
adp knowledge bulk preview --file SOURCES.json --json
adp knowledge bulk commit --preview-id ID --expect-hash HASH       # preview
adp knowledge bulk commit --preview-id ID --expect-hash HASH --yes
```

Preview adapts JSON losslessly to the canonical bulk parser and performs its
existing read-only duplicate and quota checks. It rejects fields the plain-text
parser cannot represent exactly (for example pipe-delimited display names).
Preview is **not final source/repository authorization**; commit performs those
canonical checks. No server rows or index jobs are created by preview. The CLI
saves the reviewed exact items in a private 0600 receipt in the selected
deployment's state directory. Output includes the hash, source fingerprints,
counts, and whether the preview can be committed. Rejected items or exceeded
quota prevent commit. Duplicate items already present are excluded. Receipts
expire after one hour and bind the gateway, authenticated actor, and tenant.
Commit uses the saved items, never a reread of SOURCES.json.

## Retry and recovery

Keep the same UUID for an add or reindex intent. Add receipts bind the exact
request and refuse a second send after an uncertain response. The server's
existing source/scope uniqueness protects registration across clients. A bulk
receipt is locked and marked attempted **before** sending; replay returns the
saved acknowledgement or remains unknown without another send. Bulk processing
can partially register assets before an error. Inspect the asset list and each
status before deciding on a new preview; do not assume rollback or silently
retry the batch.

Reindex uses `request_id` on the canonical endpoint. Its server-side receipt is
committed before queue dispatch; replay reads the asset without clearing its
attempt or republishing. A conditional generation reservation prevents competing
requests, including existing browser requests, from resetting the same attempt.
Only `indexed` or `failed` assets can start a new reindex. A registered, queued,
or indexing asset cannot be forcibly reset; unresolved dispatch needs operator
reconciliation. Receipts are retained in asset metadata, bounded at 256 requests
per asset; exhaustion refuses another keyed request rather than evicting retry
protection. Do not discard local receipts to bypass an unknown outcome.

## API and validation evidence

The CLI uses `/api/agent-context/assets` CRUD, `/{id}/status`,
`/{id}/reindex?request_id=UUID`, `/bulk/preview-json` (a read-only adapter to the
existing `/bulk` implementation), and `/bulk/commit`. Existing admin indexing
uses `/admin/indexing/runs` and `/{run_id}`. Paths are relative to the gateway API
base, matching the web client's existing knowledge path convention.

Nightly E32 adds served-CLI discovery, a local invalid target refusal, and a
soft-delete preview in the existing regression suite. It explicitly records
feature-unavailable deployments. Dedicated live source/indexing completion,
retrieval by an agent task, cross-tenant live fixtures, remote-control action
scenarios, and cleanup evidence remain separate acceptance gates for #5632;
read/preview regression is not full story closure.

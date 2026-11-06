# emitter

Implementation is delivered by the corresponding Engineering Insights service or schema story.

`registry_adapter.py` is the scoped registry/context ingest library. `ProfileAPI`
reads the gateway's authorized `listChanges` and `getRevision` routes over HTTPS,
with caller-supplied current authorization headers. `RegistryAdapter` accepts an
explicit grant checker, approved catalog `readback(reference)` interface and a
durable emitter sink.
The grant checker must bind the authenticated publisher to the requested tenant,
Project and source, including nested qualified references. It is called before
readback and again immediately before each fact is admitted, including replay.
The sink's `admit(normalized_revision, fact)` must insert
atomically if absent and return the original fact when already present; its
`quarantine({tenant_id, project_id, source_event_id, reason})` retains reasoned
gaps. Do not substitute a process-local set or a non-atomic read-then-write.
Replays keep the original ingestion time, conflicting bytes cannot replace an
original fact, and mapping changes produce new normalized revisions. An expired
or missing historical revision is quarantined without current-value fallback.
The adapter never reads gateway tables or calls the current
Product endpoint to recreate an expired revision. Profile change IDs remain
the Product/Project record revisions; link versions, effective dates and exact
source references remain distinct. The mutable Profile progress/context overlay
is not emitted as an immutable snapshot.

`CatalogAPI.get_record(reference)` implements the reviewed catalog GET-by-ID
contract in [catalog-openapi.json](../../../docs/product-context/catalog-openapi.json).
Configure its HTTPS base as the catalog root, including `/api/v1/catalog` for a
public gateway or `/v1/catalog` for an HTTPS origin. It obtains current session
headers for each call and refuses redirects. Portfolio, Initiative and
Project-scoped Contribution paths are defined by that contract; only Portfolio
routes are implemented in the current gateway. The reader verifies tenant,
source, record ID and exact revision, plus Contribution Project and nested
qualified references. A newer current revision is not a historical replacement:
it raises `CatalogRevisionUnavailable("revision_unresolvable")`; missing and
expired records have reasoned failures as well. A normalizing reader can pass
these failures through to `RegistryAdapter.ingest_catalog`, which quarantines
them without admitting current content.

The catalog contract is reviewed, not awaiting another readiness approval.
However, its record GETs have no historical revision selector and do not return
the original change ID, event timestamp, effective interval or mapping version.
`get_record` therefore returns the unchanged wire record, **not** the normalized
`readback(reference)` admission envelope. Do not fabricate those fields from
current values, invent a `/revisions` endpoint, or query gateway tables. The
catalog owner must expose authorized revision-pinned change provenance (or an
equivalent original event delivery) before this client can supply that envelope.
Until then, passing a bare record to admission quarantines it as unresolved.

The shared emitter service (#7299) still has no implementation or runtime binding
here. It must supply authenticated publisher identity, current tenant/Project
and nested-source grant checks, and a durable sink implementing the atomic
`admit`/`quarantine` operations described above, with authorized independent
readback. A catalog editor's successful GET does not establish Project access.
The existing deployment boundary is present, but its emitter role has S3 writes
without `GetObject` or KMS decrypt, and the workload has no persistent volume or
service configuration. It cannot serve as a restart-safe read/compare sink as-is.
Do not replace that prerequisite with process-local storage or new infrastructure.
These missing service capabilities affect AC-01 actual admission, AC-02 durable
replay/quarantine and AC-03 authenticated ingest/read denial. Keep admission
disabled until they are bound and verified; no deployment is performed here.

Run the client unit checks from the repository root:

```bash
python -m unittest discover -s modules/insights/tests -p 'test_catalog_client.py'
```

The local gateway checks in
`tests/e2e/product_onboarding/test_i21_catalog_client.py` exercise real Portfolio
routes and database state with fixture identity and an in-process HTTP transport.
They establish exact readback, changed-revision refusal and membership revocation,
not publisher authentication, durable admission or live verification.
With the gateway's locked development dependencies installed, run from the same
repository root:

```bash
PYTHONPATH="$PWD:$PWD/modules/gateway" AWS_EC2_METADATA_DISABLED=true python -m pytest -q -c modules/gateway/pyproject.toml tests/e2e/product_onboarding/test_i21_catalog_client.py
```

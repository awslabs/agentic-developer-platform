# Engineering Insights lineage schemas

## Business-context read result

`results/v1/business_context.schema.json` and `results/v1/error.schema.json`
define the strict `results/1.0` authorized business-context response and safe
failure body for the proposed results route. A success contains only complete,
version-pinned projection data, with qualified original references and decimal
cost strings. Incomplete coverage and pending data use errors, not empty or
partially disclosed success bodies. See the [HTTP/filter/authorization contract](../../../docs/engineering-insights/README.md#business-context-results-read-contract-results10).

## Registry and context admission

`registry/v1/admission.schema.json` defines a **separate** Insights-owned
`registry/1.0` normalized fact. It is not a raw v1/v2 producer, a Profile
response, or a Context snapshot. The Profile registry owns Product/Project
identities and dated bindings; the approved catalog owns Context identities and
links. Insights owns only its pinned normalized revision and mapping version.
The six-field `subject_ref` and `source_ref` retain original qualified identities;
`source_revisions` and `relationship_version` retain the exact versions observed.
`source_event_time` records the real source event (the Profile recorded commit),
`effective_at` the dated business link and `ingested_at` the adapter observation.

An admitted publisher must hold a current tenant/Project/source grant on **each**
page, revision and replay. Profile reads use scoped `listChanges` and
`getRevision`; catalog reads require their separately approved scoped readback.
Neither a schema-valid input nor historical access grants admission. Deduplicate
by tenant, Project, source connection, event, type, logical ID and immutable
revision; conflicting bytes, expired snapshots, unresolved mappings and revoked
access must be quarantined with a reason, never reconstructed from current data.
Mapping-rule changes produce new normalized revisions instead of overwrites.
Raw lineage v1/v2 validators and their producer allowlists are unchanged.

`lineage/v1/` is the reusable JSON Schema Draft 2020-12 contract for the five
W1 records, the section 3.0 shared envelope and the section 6.1 quarantine
envelope. Each record references `envelope.schema.json`; consumers should load
that sibling when validating a record. `schema_version` must be `1.0`.

Install `requirements.txt`, then run `python modules/insights/schemas/validate.py`
from the repository root to validate exactly the five shipped fixtures. To
validate a separate record, run
`python modules/insights/schemas/validate.py --schema story /path/to/record.json`.
The CLI exits nonzero and identifies invalid or missing fields. To import from
Python, add this directory to the import path, then use
`validate.load_schema("story")` and `validate.validate_record(record, "story")`.

Run `python modules/insights/schemas/check_traceability.py` to check every schema
property against its designated section in the lineage design. The check prints
the source row for each field and fails for undocumented fields, including fields
documented only for another record type. Quarantine's `reason` maps to the section
6.1 prose requirement. Insights CI runs this check alongside fixture validation
and also runs when the source design document changes.

The fixtures illustrate public closed story #7061 and merged PR #7062. The
story title, PR number, merge time, commit SHA and passing CI job are drawn
from that public history. Scope, connection, ingestion time and execution task
IDs are fictional. The deployment is **synthetic**: the PR's rollout jobs were
skipped, so it is not evidence of a real deployment. The test-run fixture is a
representation of the passing CI job, not an individual JUnit test attempt.
None of these examples are valid evidence of live acceptance or access grants.

## Requirements and test analytics v2

`lineage/v2/` is an additive contract. It does not modify v1 or implement a
collector, query service, migration adapter or acceptance evaluator. Its source
is [lineage section 11](../../../docs/engineering-insights/lineage-schema.md#11-requirements-and-test-analytics-v2).
The initial S3/Parquet/DuckDB design is in the delivery architecture; there is no
DuckDB runtime dependency in schema validation.

| Record type | Grain |
|---|---|
| `story_revision` | Exact observed brief/state and retained source snapshot |
| `acceptance_criterion` | One identified criterion bound to an exact story revision |
| `coverage_definition` | Versioned module, feature or scenario |
| `test_definition` | One stable executable/manual test definition revision |
| `coverage_mapping` | One versioned criterion–scenario or scenario–test link |
| `test_run` | Pack/workflow execution for one provider attempt, with explicit state revisions |
| `run_test` | Intended test instance, including matrix/parameters and pinned requirement bindings |
| `test_result` | One attempt of one planned instance; retries remain separate |
| `acceptance_assessment` | Attributable verdict for an exact criterion, with evidence and policy when automated |

Every record has a `2.0` envelope. Record references carry tenant, project,
source connection, record type, logical ID and immutable revision; the shared
`references.schema.json` also defines artifact ID/version/checksum references.
A test retry is a new result identity, a workflow rerun is a new run identity,
and a run status transition is a revision of the existing run identity.

```bash
python modules/insights/schemas/validate.py --version v2
python modules/insights/schemas/validate.py --version v2 --schema test_run /path/to/run.json
python modules/insights/schemas/check_traceability_v2.py
python -m unittest discover -s modules/insights/schemas -p 'test_*.py'
```

Python callers pass `version="v2"` to `load_schema` or `validate_record`. Existing
callers and the CLI default remain v1. Validation rejects incompatible versions,
undefined fields, missing pinned references, cross-project record links and
backwards execution timestamps. It does **not** authenticate a producer, resolve
remote references, verify object bytes, prove selector existence or authorize an
acceptance verdict. Ingestion/evaluation must enforce those service boundaries.
JSON Schema consumers must implement the cross-record scope and timing checks
as well as loading the sibling envelope and references resources.

The nine default v2 fixtures and `traceability-bundle.json` are wholly fictional.
The bundle contains module/feature/scenario parents and both mapping kinds so
its versioned references can be checked together. Artifact IDs, versions and
hashes are placeholders, not existing S3 evidence or hashes of downloaded bytes.
A fixture with `accepted` is schema data, not an authorized acceptance decision.

v1's `test_run` describes individual test-attempt evidence, although its existing
fixture represents a whole CI job. Neither can be silently relabeled a v2 pack
run. Preserve legacy records, require provider evidence to determine their grain,
and keep job-only/ambiguous records out of test-level denominators. The detailed
migration rules are in lineage section 11.11; this change delivers no backfill.

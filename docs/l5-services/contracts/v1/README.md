# Contract v1

## Transform contract (TF01)

The existing configuration, run and result operation IDs now describe a strict
`service_id: transform` variant. An omitted service ID still means Security;
DevOps continues to require `service_id: devops`. Transform configuration pins
a repository source, connection, executor binding and test policy; run input
selects one immutable repository commit and a typed refactor or dependency
upgrade goal. A Transform result carries original-patch, before/after-check,
provenance, limitation and cost-status fields instead of Security findings.
The proposed schema/client operation matrix does **not** enable the gateway,
executor or provider: those remain unavailable until the separate authority,
isolation and provider qualifications in the Transform architecture are proven.

Proposed, not deployed. [Architecture](../../architecture.md) is normative for
authorization, transitions and concurrency constraints that JSON Schema alone
cannot express. `catalog.py` generates the strict schemas, OpenAPI 3.1 definition,
positive/negative fixtures and complete client-operation matrix.

All objects reject unknown fields. References preserve Product's six-field shape.
Server-derived scope, delegation and credentials are deliberately absent from
public request bodies. Native resource references are candidates to authorize,
never proof of rights. Run requests cannot enable endpoint validation or remediation.

The FinOps cost-inquiry input and result definitions are additive to the shared v1
schemas. Request shape validation accepts the FinOps variant, but the host rejects
native submission with `native_unqualified` until supported automation, recovery
and billing isolation have private qualification evidence. Configuration writes and result
routes are not activated by this schema addition. The read-only FinOps options
route exposes `cost_inquiry` with specific unresolved prerequisites and an owned
unresolved Module/Feature mapping; it neither presumes a repository nor grants
native spending authority. Other configuration routes remain closed pending
qualified setup and integration.

Public mutation replay still requires current authority. Configuration updates
replace the complete explicit settings payload; refresh is separate and retains
overrides. Only named Profile-inherited L5 fields can be overridden, with values typed by
field. Provider account-wide requirements cannot be edited as a Product override.
Binding create/link need a pinned configuration revision; existing
binding mutations additionally need its ETag. Schema validation does not enforce
conditional HTTP headers, row-level authorization, native ownership or semantic
source immutability: host tests must exercise those checks independently.

DevOps incident policy selects allowed severities and environment references and may
exclude explicit maintenance windows. A verified event retains its source, severity
and environment for a second authority check before admission. `paused` denies
new work without changing previously accepted runs. An investigation's
`limits.policy_ref` identifies the tenant's hard organization budget configuration
by `record_id`; admission denies when that budget or its headroom is unavailable.
The DevOps configuration authoring API still saves disabled drafts only until
reviewed readiness is integrated by DA03; these fields alone do not activate
native actions.

Enabled triggers select `capability` and `binding_id` explicitly. Product-owned
[pinned source content](peer-contracts.md#pinned-source-content) captures exact
repository/document bytes and supplies hashed snapshot receipts. Setup workers
consume `SetupIntent`, not the public `Operation` status projection. MCP tools
declare success `outputSchema` envelopes with `data`, `http_status` and the exact
HTTP `etag` where applicable; clients must preserve the quoted token on edits.

Monitoring follows the [shared timing and observation contract](../../architecture.md#polling-and-assessment-monitoring).
New work is published immediately after its SQL commit; normal Security status
checks use a 600-second SQS delay. `MonitoringPolicy` defaults are resolved into
`EffectiveMonitoringPolicy` at admission. `ProviderObservation` carries available
native progress/error evidence and `RunMonitoring` keeps freshness separate from
suspected stalls and run outcome. Neither schema validation nor a provider's
record-update timestamp establishes a heartbeat guarantee.

The [compatibility matrix](compatibility.json) records source-verified host
entry points, proposed exact-case build/package paths, owners and integration
checkpoints. Proposed paths and SDK candidates are not implemented or qualified.

Reproduce from repository root with Python 3.12+ and `jsonschema`, `PyYAML`,
`openapi-spec-validator` installed in an isolated environment:

```sh
python3 docs/l5-services/contracts/v1/catalog.py
python3 docs/l5-services/contracts/v1/validate.py
```

Generated JSON examples use fictional identities and no provider calls. The check
does not prove Lambda behavior, client delivery, provider access or Context reuse.
`scenarios.json` also indexes standalone offline scenario fixtures. From the
repository root, run their named selectors with the same Python dependencies:

```sh
python3 -m unittest discover -s modules/L5-services/tests/contracts -p test_contracts.py -k test_security_contracts
python3 -m unittest discover -s modules/L5-services/tests/contracts -p test_contracts.py -k test_lifecycle_boundaries
python3 -m unittest discover -s modules/L5-services/tests/contracts -p test_contracts.py -k test_compatibility_fixtures
```

`scenarios.json` supplies independent expected scope/pinning/retry decisions and
owned lifecycle traces. The validator checks these against the written contract's
invariants and state vocabulary; it is not a state-machine or authorization implementation.
`test_contracts.py` adds independent regression examples for explicit trigger
targets, setup request preservation, bounded source receipts and MCP read/edit/
stale-edit exchanges. It checks the proposed interface, not real handler behavior.
Compatible additions use optional fields only; breaking field/state/operation
changes require a new contract version and explicit consumer compatibility tests.
This unshipped v1 proposal can be corrected before acceptance; these required-field
changes supersede its earlier review draft, not a deployed v1 consumer contract.

For Security results, `finding_count` counts normalized findings stored with the
result. `findings_artifact` identifies a deterministic JSON export derived from
the immutable finding rows when it fits the 8 MiB export bound; the artifact
metadata and bytes routes check current Product, Project, owner and source rights
on each request, including after a metadata link was issued. Provider-native
artifact citations are retained as evidence references but cannot be fetched
through this derived export route; callers do not receive an unverified raw S3
URL. `manifest_ref` is present only when the sealed manifest itself has an
artifact reference (an archive of source content is not that reference).
Security results that declare `provider_outcome` retain the existing manifest
requirement; a read projection without a manifest artifact omits that field.
`publication_id` is omitted while Context publication is unavailable; it must
never be fabricated to make an otherwise readable result pass schema validation.
Comparison responses include each immutable result revision so callers can tie
new, unchanged and resolved finding IDs to the exact scanned pair. Human triage
revisions are independent and do not alter the scan comparison.

## DevOps extension (DA01)

`service_id: devops` selects typed DevOps settings and run inputs; omitted
`service_id` preserves existing Security payloads. Admitted AWS-only DevOps
investigations may have zero Product source snapshots; Security admission still
requires at least one pinned Product source. Scope, selected resources, limits and
configuration remain pinned in the DevOps admission without inventing an archive. A DevOps run admits only
investigation, release readiness or release verification. Native prevention and
native events are not fabricated as ADP-submitted runs. `DevopsEventList` exposes
scoped event readback independently of run history, while binding readback carries
per-capability freshness/reason observations. The same versioned operation IDs
serve both services; unsupported capabilities, modes and extra fields fail
schema validation. The `launchNativeBrowser` operation returns a short-lived
browser URL exclusively to the authorized first-party UI: CLI and MCP mapping
are explicitly `-`, never a session URL.

Enabled shared triggers accept the selected service's capabilities, and DevOps
options can list all four supported capabilities; Security options retain their
two-capability limit. Submitted DevOps results carry the run's pinned `admission`
and an explicit `provider_outcome`. Completed outcomes require `evidence_ref`.
When that admission has no Product source snapshots, the result does not require
a `manifest_ref`; source-backed DevOps results and all Security results still do.
The host must verify that the result's scope, admission and provider evidence
match the referenced run, rather than trust a caller-supplied copy. Native results
continue to prohibit fabricated admission and manifest records.

JSON Schema cannot compare a nested source reference against the authenticated
Product/project. The host must use verified scope at each API and worker boundary;
`validate.py` supplies an offline `references_in_scope` oracle used in the
contract fixtures, not a replacement for runtime authorization. A valid schema
is not evidence that AWS connector prerequisites or live qualification are met.

## FinOps admission outputs (FO05)

FinOps run submission, detail and list responses carry the same immutable
admission. Its `service_id: finops` selects a scoped `native_agent_ref` and
`billing_scope`, including the admitted scope generation and available billing
snapshot references. Context `source_snapshots` may be empty; Security still
requires at least one pinned source. Native-agent references are scoped record
identifiers, not provider credentials or session URLs. The output schema keeps
FinOps admissions separate from other services and rejects extra authority fields.
These typed responses do not establish native qualification or live acceptance.

## Well-Architected architecture-review input (WA01)

`service_id: well_architected` selects `architecture_review`, an explicit
`source_only` mode and a nonempty unique subset of the five Agent pillars.
Document and repository-commit references can be mixed, but must carry pinned
revisions; no resource inspection, credentials, account authority or arbitrary
SDK arguments can be supplied by a public request. An absent service ID keeps
the existing Security contract, and `devops` keeps its own typed variant.
These v1 input schemas/operation and MCP descriptors are a proposal, not a
deployed service or evidence of provider eligibility. See the
[qualification procedure](../../well-architected/qualification.md) for the
model revision, unverified native constraints and live evidence boundary.

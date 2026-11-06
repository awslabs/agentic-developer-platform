# WA01 qualification — revision 1

This is an offline model record and **planned** private procedure for #9098,
not evidence of eligibility or source ingestion. No AWS calls, resource creation
or purchases were made for this record. Only the admitted operator, with WA07's
private eligible/ineligible fixtures, can perform live qualification. Keep all
account, role, resource and raw response identities in authorized private evidence.

## Installed worker assembly

The service root defaults to `l5_services.runtime_well_architected:factory`.
The zero-argument factory uses the shared runtime interface from
`ac72712e7eefc655a73f6df3173ab062d0ed5b63`: signed Gateway transport,
IAM-authenticated database sessions with packaged RDS trust roots, scoped Product
sources, versioned service archives, queue publication and broker sessions.
The shared session wrapper also forwards native SDK configuration so Start's
single-attempt policy is preserved. The worker itself never selects ambient
credentials for customer provider calls or accepts an authorization override.

Alongside the shared `L5_SERVICE_ID`, `L5_GATEWAY_URL`, `L5_RDS_*`,
`L5_ARCHIVE_BUCKET`, `L5_WORK_QUEUE_URL` and AWS workload settings,
`L5_WA_PEER_CONTRACTS` is optional private `worker_environment` configuration.
Leave it absent until a real receiver is installed and qualified. Native setup,
generation, collection and comparison do not require peer integration. Missing
peers keep durable publication records in `unavailable` with
`consumer_schema_unadmitted`: the architecture's `publication_blocked` outcome,
not successful delivery or reuse. Original results remain readable, and existing
publication repair can activate the retained records after peer admission.

The optional JSON object may contain `product-context`, `engineering-insights`,
both, or neither. Each configured entry has
`kind=well_architected_result`, `schema_version=1.0`,
`private_read_fencing=true`, and the peer's admitted `path` under its respective
`/internal/v1/context/` or `/internal/v1/insights/` prefix. These values select a
previously qualified peer contract; they do not grant access or prove delivery.
Do not configure a fabricated endpoint or assert fencing before the peer supports
it. Malformed contracts, unsupported fields and paths escaping the peer prefix
fail assembly; an absent peer does not. Delegation and Product scope come from
each publication's durable run, not this configuration. Each delivery requires the peer's receipt and exact
result projection readback; revocation or incompatible responses do not publish.

The installed comparison checks literal agreement with pinned source bytes. It
only marks an exact recommendation description found as a complete source line
`supported`, with that verified citation and an explicit source-agreement
explanation. Other recommendations remain `not_verifiable`. This does not prove
semantic correctness, goal satisfaction, deployment state or provider per-file
consumption; it is not a substitute for the independent native qualification
and substantive review required by the release node.

Profile setup uses the existing source-only adapter and requires the broker's
`source_role_qualification`; absent qualification fails before native setup
effects. The service authority checks the selected connection's current secret
version and exact assumed role, source-only execution-role trust, same-Region
bucket, simulated versioned-source read permission and Support response. The
setup broker repeats these observations behind the durable setup lease before
returning credentials. Its bounded setup policy excludes paid generation and
limits PassRole to the selected execution role and Well-Architected service.
IAM simulation establishes only the checked policy decision, not actual provider
ingestion or access through every resource policy.

The Gateway exports `src.l5_backend_well_architected.gateway_backends()`. It
returns the four concrete dependencies required by the shared backend registry:
`l5_wa_authority`, `l5_wa_profile_client`, `l5_wa_consumer_resolver` and
`l5_wa_qualify`. Export creation performs no database or external I/O. The existing
request backend binds these dependencies to its database, actor, delegation and
admission context; no separate application registration path is added. The local
route tests install the specified exports into the normal app's state because
the shared registry source is not present in this checkout. The controller must
reconcile its normal startup mount against the merged shared dependency; the
supplied export contract is tested locally. Source capture preserves the shared
repository, document and sparse-file implementation and its pinned manifests.

For later-task peer reuse, the Gateway requires current authority and
qualified `L5_WA_PEER_CONTRACTS`. Add `task_fencing=true` and `query_path` for
Product Context or `usage_path` for Engineering Insights, each under the peer's
own admitted path prefix. Missing configuration or authority leaves peer reuse
disabled without hiding the original Product result. Consumers verify scoped
receipts and projections; configuration alone does not grant authority. The
Gateway signs peer HTTP requests with its workload IAM credentials through the
internal API Gateway address in `L5_GATEWAY_URL`; it does not require Lambda
environment variables, follow redirects or fall back to unsigned requests.
The release owner must qualify peer contracts before enabling consumers. No
Support enrollment is performed, and final peer-reuse acceptance remains due.

After verified cleanup of an ADP-created Profile, the original human owner can
explicitly authorize the exact retained result through the Product API. Under
`/v1/products/{product_id}/projects/{project_id}/services/well_architected/results/{result_id}`,
read `GET /reuse-scope`, then submit its `scope_digest` and an explicit
`expires_at` to `POST /reuse-authorizations`, with the returned ETag in
`If-Match` and a fresh `Idempotency-Key`. Lifetime is limited to one minute
through 24 hours. List current receipts with `GET /reuse-authorizations` and
revoke one with `DELETE /reuse-authorizations/{authorization_id}` using its
revision ETag. These requests require the owner's verified human access token.

Cleanup alone grants no reuse permission. The separate consent covers reading
and later canonical-task use of exactly one result and both acknowledged
Context/Insights publications. Every read and use rechecks the owner, current
source access, terminal owned-cleanup proof, publication revisions, receipts,
invalidation state and expiry. A revoked or expired authorization cannot be
restored by replay, and replacement consent cannot revive an earlier signed
request. It never authorizes a worker, credential session, publication admission
or native operation. Source loss or archival blocks reads while consent listing
and revocation remain available to the current owner. Retained consent rows
prevent destructive schema rollback. This is an offline-tested capability;
installed later-task and cleanup acceptance still requires observed evidence.

Reproduce offline checks from the repository root with the module and Gateway
test dependencies installed:

```sh
BG_TOKEN_SECRET_KEY=fictional-test-key-0123456789abcdef \
AWS_ACCESS_KEY_ID=fictional AWS_SECRET_ACCESS_KEY=fictional \
AWS_SESSION_TOKEN=fictional AWS_CONFIG_FILE=/dev/null \
AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true \
uv run --no-sync --project modules/L5-services pytest -q \
  modules/L5-services/tests/well_architected/test_wa_factory.py \
  modules/L5-services/tests/deployment/test_wa_factory_package.py \
  modules/gateway/tests/products/test_wa_backend_authority.py \
  modules/gateway/tests/l5/test_wa_source_role_qualification.py \
  modules/gateway/tests/l5/test_wa_peer_transport.py
```

The first suite exercises SQL-owned work through the real factory with mocked
database/HTTP/AWS boundaries. The second builds the worker archive and calls
`load_service()` from its extracted Python 3.12 installation with network access
disabled and no peer contracts configured. The Gateway suite exercises the
exported callbacks through normal Product routes and the credential broker with real scoped records and mocked
external I/O. None of these checks establishes installation or native live
acceptance.

## Release-driver entry points

The service-specific driver is wired through the shared release selector
and the shared workflow's Well-Architected entry points. It uses the normal Product
bearer token to read the selected configuration, binding, run and result, plus
independent native observations from the Gateway. It does not require `adp-cred`,
worker identity, or provider credentials on the release runner. An authorized
release operator must provide the installed-release manifest, a private selection
of **two distinct** Products, and the reviewed source revision. Completed-review
selections remain read-only. First-install selections explicitly request the
bootstrap sequence below through the shared release entry points. The direct
rollup command is for completed-review readbacks, not generation.

```sh
python3 modules/L5-services/scripts/qualification-rollup.py \
  --service well_architected \
  --selection /private/selection.json \
  --release /private/installed-release.json \
  --source-sha '<reviewed-40-character-git-sha>' \
  --output /private/well-architected-observations.json
```

The private selection has `source_sha`, an `expires_at` within 24 hours, and a
`cases` array. Each case pins its Product scope, binding/run/result IDs and
revisions, source references and `project_kind=non_adp`; it also supplies a
private HTTPS `gateway_url`, bearer `token_env`, and
`provider_identity` (`account_id`, exact `role_arn`, `region`). Keep the token
in the named environment variable, not the selection file. Inputs must be
absolute, outside the checkout and readable only by the operator; the output
directory must also be private, and the output must not already exist. The
driver rejects stale or mismatched selections and writes source-bound
observations with `provider_coverage_unverified`; it does not establish native
per-file ingestion, live UI/CLI/MCP acceptance, or a reviewed deployment.

For the shared workflow, store JSON content in the protected
`ADP_L5_WELL_ARCHITECTED_LIVE_CONFIG_JSON` secret. Combine the selection fields
above with `schema_version: 1` and the existing `installed_release` object from
the installed-release manifest in that same JSON object. The manifest's
`source_revision` must equal the selection's `source_sha`. Do not embed tokens
or runner-local paths. The runner needs the private bearer token environment
variable named by each case, supplied by the release owner. A deployment AWS
identity is not Product authority; do not substitute ambient AWS credentials,
an engine deployment action, or a fabricated worker identity for that token.

The service-owned `GET /v1/products/{product_id}/projects/{project_id}/services/well_architected/runs/{run_id}/native-observations`
route derives its profile, generation, recommendations and immutable source
object from the stored run and binding. Before and after native reads, it checks
the current Product owner, configuration, selected sources, connection and
unexpired service delegation. The Gateway's existing selected-provider broker
verifies the exact account and role and applies a read-only session policy.
Recommendation detail sessions contain only small batches of exact stored ARNs,
so a large review does not exceed the STS session-policy limit or require wildcard
recommendation access. Authority is rechecked for every batch.
The response contains observations, never temporary provider credentials, and
is marked `Cache-Control: no-store`. No create, update, delete, generation Start
or PassRole permission is granted. Existing worker work-grant routes are unchanged.

The shared dispatcher validates both executable entry points and materializes
the fixture outside the checkout with private permissions. It then invokes:

```sh
python3 modules/L5-services/scripts/qualify-well-architected-release.py \
  preflight "$SOURCE_SHA" "$PRIVATE_FIXTURE" "$PRIVATE_EVIDENCE_DIR"
python3 modules/L5-services/scripts/qualify-well-architected-release.py \
  run "$SOURCE_SHA" "$PRIVATE_FIXTURE" "$PRIVATE_EVIDENCE_DIR"
python3 modules/L5-services/scripts/emit-well-architected-qualification.py \
  "$SOURCE_SHA" "$PRIVATE_FIXTURE" "$PRIVATE_EVIDENCE_DIR"
```

These entry points require the shared workflow's validated release identity and
`L5_FIXTURE_DIGEST`. For completed-review fixtures, preflight performs the same
scoped, read-only checks without writing evidence. Run repeats them and writes
`native-observations.json`. The
emitter requires observations from the same source, definition, workflow run and
fixture, no older than 15 minutes, and repeats Product/native/source verification
before writing private `qualification.json` and `delivery-evidence.json` files.
Existing outputs require reconciliation; they are never overwritten.

### First-install bootstrap

The coordinator first authorizes and configures the two selected Products,
their complete sources, provider connections, budgets and service grants.
The service must be installed, but its consumers may still be paused. In each
case, replace completed binding/run/result IDs and revisions with
`configuration_hash` and an explicit `bootstrap` object:

```json
{
  "binding_operation": {"action": "create", "configuration_revision": 1},
  "setup_idempotency_key": "wa-example-profile-0001",
  "run_idempotency_key": "wa-example-review-0001",
  "timeout_seconds": 3600,
  "poll_seconds": 10
}
```

Retain the case's full Product scope, configuration revision, exhaustive `sources`,
`project_kind=non_adp`, HTTPS/token settings and exact provider identity. The
configuration hash and revision must match the authorized Product configuration.
Business-goal references must be included in the complete selected sources;
preflight rejects a selection the existing worker could not admit.
To adopt an existing source-only profile, use `action=link` and the selected
`native_resource_ref` accepted by the existing binding-operations API. No other
setup action is permitted. Both cases must use bootstrap; do not mix modes.

Before saving the first source-only configuration, the Product owner can validate
explicit provider resources with
`POST /v1/products/{product_id}/projects/{project_id}/services/well_architected/configuration/provider-selection`.
The request names the current `connection_ref`, `region`, `execution_role_arn`
and `source_bucket`. The connection must belong to that owner and have a verified
environment binding on this Product. The execution role must be separate from
the broker role. The source bucket must be the private archive store configured
for the installed L5 runtime; this endpoint does not provision another store.

This operation reads only the selected role and bucket. It checks constrained
Well-Architected service trust, versioned source-read permission, same-Region
bucket ownership, versioning and public-access blocking. The broker needs
`iam:GetRole` and `iam:SimulatePrincipalPolicy` on the selected role, plus
`s3:GetBucketLocation`, `s3:GetBucketVersioning` and
`s3:GetBucketPublicAccessBlock` on the exact bucket. No native Profile operation,
source upload, permission change or Support subscription change occurs.
The response returns hash-pinned `execution_role_ref` and `source_store_ref`
values for the normal configuration save, with `native_qualified: false`.
Create, edit, refresh and setup admission revalidate those selections. A stale
selection must be validated and explicitly saved again. Reading saved
configuration remains available while the provider needs repair.

Repository and document references still require authorized Product source
bindings and immutable revisions. Provider-resource validation does not grant
source access, delegate execution or qualify the native service. A GitHub-backed
document uses the same installed App repository selection as its repository;
it cannot bypass a missing repository grant.

The [AWS Agent availability documentation](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent.html#agent-intro-availability)
requires Business+ Support or higher and lists US East (N. Virginia), US East
(Ohio) and US West (Oregon) as Profile hosting Regions. A successful Support API
call alone does not prove Agent eligibility. Retain a failed Agent API response
separately from a Support API failure; do not assume an `Unknown Operation`
response establishes the account's subscription tier or its cause. Native
qualification still requires the exact Agent Profile and generation readbacks.

Preflight calls the read-only Product `qualification-prerequisites` route. This
uses existing binding authority and selected-provider eligibility checks without
creating setup work, submitting a run or requiring a completed native generation.
It verifies current Product/source/connection authority and an unexpired root
service grant. Provider entitlement remains a prerequisite, never an override.

After the shared workflow resumes compatible consumers, `run` repeats those
checks, submits the selected binding operation, waits for its ready binding,
and submits one `architecture_review` run per Product through the normal Product
API. The scoped worker owns native effects and enforces the configured budgets,
quota, eligibility and source checks. The driver only polls the original operation
and run IDs until completion, using the fixture's bounded timeout (at most one
hour per Product). Failed work or an exhausted timeout fails qualification; it
does not cancel native work, erase uncertain effects or start a replacement job.
The two Products execute sequentially.

Keep both idempotency keys stable when recovering a lost response or rerunning
the same selection. Existing durable Product receipts return the original work;
the driver performs no automatic mutation retry. Reconcile uncertain work under
its existing identity before changing a selection. Once the run succeeds, the
driver pins the returned binding/run/result revisions in private observations
and waits for the latest independent comparison result, preserving the original
raw result. It then performs the same independent native/source checks as
completed-review mode.
The emitter only re-reads that resolved selection; it never repeats setup or
generation. Neither missing reviews nor unavailable provider access permits
bypassing preflight, fabricating observations, or purchasing Support.

The emitted success is limited to `selected_product_native_readback`. Both reports
retain `provider_source_coverage_unverified` and `live_acceptance=not_evaluated`.
They do not claim installation verification, a paid generation journey,
UI/CLI/MCP retrieval, portability, recovery, cleanup or Context/Insights reuse.
Those broader release criteria still require their independent observations.

Source verification requires the selected role to read the exact S3 object
version. The Product run exposes `source_manifest` from durable work records;
the driver compares its full bucket/key/version and file inventory with the
result and native generation, rather than trusting a URI echoed by AWS. It
receives that version's bytes through the authorized Gateway observation route,
which reads the exact version with the selected broker. The driver verifies archive and file hashes,
and checks each comparison citation's path, revision, line and exact quotation.

The driver also consumes the shared [retained-source readback contract](https://github.com/aws-e/adp/blob/2afe1b8b067bfdba0d606b477f41ebd620bb335f/docs/l5-services/retained-source-readback.md)
(published at `2afe1b8b067bfdba0d606b477f41ebd620bb335f`). Through its same private
Product session it reads the run's `/source-manifest`, then each selected
`/source-snapshots/{snapshot}` receipt and its `/manifest` and `/content` bytes.
It verifies immutable versions, hashes, receipt expiry, current scoped responses
and exhaustive file coverage, and compares original captured file hashes with
the native archive's selected files. Wrong, revoked, expired or changed inputs
fail without evidence emission. These are user-authorized retained-input reads,
not worker work-grant requests. No worker lease is invented or resumed. The
shared API must be installed before qualification; its implementation and the
canonical human-identity integration remain owned by the shared Gateway work.
The native evaluator independently repeats the archive read and citation check.
Missing source access, altered bytes, foreign buckets or fabricated quotations
fail qualification; this does not claim per-file provider ingestion.

The worker's pinned botocore `1.43.119` model uses `agent-profile/` resource ARNs
and `agent-recommendation/` ARNs with UUID identifiers. Binding, preflight and
qualification use these formats while retaining selected account and Region
checks; legacy `profile/` and `recommendation/` identifiers are not native IDs.

## Model and documentation baseline

| Source | Revision and limit |
|---|---|
| Local botocore service model | `wellarchitected/2020-03-31/service-2.json.gz`, botocore `1.43.120`, SHA-256 of **decompressed bytes** `bcba9531f89a168c5fa4f725bea65f8fab67d021f5aa201d6c89b6e9e814fceb` (inspected 2026-10-27). Project pins `1.43.119`; recheck the release model at integration. A model does not prove a live feature is available. |
| [Official model revision](https://github.com/boto/botocore/blob/3d3e64b320672d27f348375a1b4620a5625e25d8/botocore/data/wellarchitected/2020-03-31/service-2.json) | Source referenced by accepted design. Before activation, pin dated current AWS documentation for Support prerequisite, region, source formats/limits, role trust and readback. None of those are established by the model. |

## Capability qualification table

`unknown` blocks the affected capability; denied IAM is **not** missing Support.
Profile readback does not prove that source bytes were consumed.

| Check / affected capability | Model observation | Authorized private procedure and evidence | Status |
|---|---|---|---|
| Identity, Support and region / setup and generation | Agent APIs exist, but no Support tier or eligible region is established. | For each admitted fixture, run `adp-cred assume --service aws --label <approved-label> --exec aws sts get-caller-identity` and compare **both account and exact role**. Use that connection for read-only `ListAgentProfiles` and `GetAgentProfile` on authorized identities. Record region, dated official Support/region docs, redacted positive/negative native responses and error codes. `AccessDenied` means denied/unknown eligibility, not missing Support; successful List/Get does not prove generation eligibility. | unknown: fixtures and native responses unverified |
| Source-only profile / no resource inspection | Create requires pillars, execution role and aggregation; Get exposes optional eligibility flags and `fieldErrors`. | Only after separately authorized owned setup: require `aggregationConfiguration=[]`, admitted role and four documented review pillars; read back exact profile, aggregation, role, scheduled flag and field errors. Absent flags are unknown, not false. Do not mutate adopted resources, enable schedules or grant discovery. WA07 checks owned-only cleanup/cost. | unknown: no setup authorized |
| Pillars / review scope | `COST_OPTIMIZATION`, `SECURITY`, `RESILIENCE`, `PERFORMANCE`; the SDK enum also has `OPERATIONAL_EXCELLENCE`, but primary architecture-review documentation does not list it. Scope requires at least one supported review pillar. | Qualify `types=[ARCHITECTURE]`, selected pillars and goals against native readback before activation. | model-only; native unknown |
| Source format, size and role trust / source-only activation | [AWS primary docs](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-architecture-reviews.html) specify `s3Uri`, optional wire `s3VersionId` (mandatory for ADP immutability), and `lensArns`; ZIP ≤25 MB, folder ≤100 MB, file ≤1 MB, same-Region S3 bucket and profile, five reviews per profile per day. The SDK document shape does not constrain these keys. | Pin immutable source identity and independent byte hash. Verify actual version/bytes, selected-file manifest and source permissions before Start; after Start independently read generation and recommendation source references. Missing per-file ingestion proof is a reported limitation, not a pre-Start receipt. No unrequested resource creation or inspection permission. | documented wire contract; live provider read access and ingestion still unverified |
| Start response and read consistency / safe recovery | Start has no `clientToken`; List/Get generation exist, Stop generation does not. | For separately admitted paid work, pin original profile/name/request hash/model/time; disable SDK mutation retries. After a lost response, list **all pages**, Get candidate identity/scope/time and measure readback delay. Zero matches are not no-effect proof; multiple/unverified matches are outcome-unknown. Local timeout does not cancel native work; no blind re-Start. | model-only; native uniqueness unknown |

## Manual SCN-01 evidence and disposition

For each admitted private fixture, record approved scope/budget, selected account
and exact role, region, SDK/model and official-doc revision, request digest,
redacted positive/negative responses, profile/generation/source readback, and
independent resource/cost and owned-only cleanup disposition. Publish only
sanitized immutable references and a classification (`eligible`, `ineligible`,
`denied`, `unknown`, `unsupported`) with exact blocked capability. Stop an IAM-
denied probe rather than labeling it ineligible. Unknown/adopted resources remain
untouched; reconcile the original intent before retry or cleanup. SCN-01 remains
manual/not-run until native fixtures and independent readback are available.

## Catalog mapping and shared-operation parity

As checked against the issue #8683 body and its available comments on
2026-10-27, no authoritative Module or Feature ID mapping was verified for
`well_architected` / `architecture_review`. The WA01 story and accepted backlog
also mark both mappings **unmapped**. This is a scoped block on dependent
catalog registration and dispatch, not on independent offline input validation.
PranavSharma1000 and WA01 must supply the authoritative IDs and resolve them
with #8683 before dependent dispatch; do not guess IDs or add platform constants.

The new variants reuse the existing 33 shared operations rather than adding a
second control plane. The operation matrix, OpenAPI paths and MCP declarations
must agree on operation IDs and declared input/output schema names. A matching
matrix is contract evidence only: installed gateway/worker and live provider
access remain separately qualified by WA07.


The installed readback collector accepts an optional
`qualification_clients.design_task_id`. It resolves the authenticated human and
the canonical completed task, verifies a later creation time and the same
Product/project, then checks both consumer receipts against that user, task,
result revision and each other. Collection rechecks identity and task after the
read. Set `qualification_clients.retained_reuse=true` only when an existing
explicit owner authorization is expected after owned cleanup; the collector
reads its exact scope and receipt before and after reuse and never issues or
renews consent. Evidence emission reads an existing usage receipt, so it does
not repeat the usage write.

An independently selected denied token proves only the returned content denial.
It cannot establish source revocation or zero provider effects. Those facts
require their own observed revocation and independent native call-count traces.
Before source unlink, the collector also reads the selected result query and
every admitted source snapshot's receipt, manifest and content as the owner.
It verifies immutable versions, sizes and hashes, then tests the same paths
with a different authenticated reader. The staged unlink collector repeats
those exact denial reads as the original owner. Generic artifact routes, which
are unsupported for this service, never count as successful artifact evidence.
With both consumer receipts, a distinct completed later task, the frozen source
transition and unchanged installed artifacts, this composition can complete
`WA05/AC-03` and `WA05/SCN-03`. Missing older snapshot evidence remains listed in
`reuse_progress`; it cannot be inferred from a generic not-found response.
This alone does not complete `context-reuse`, publication fault recovery or the full
client journeys: independent provider effects and their other evidence remain
due. Collection observes existing source authority and does not unlink a source.

The acceptance report lists the remaining evidence under each unresolved
original check and keeps all original story, scenario and epic criteria. Manual
UI, installed fault recovery, retention, schedule and two-project provenance
remain due until their actual observations are collected at the selected release.

For each native case, `qualification_portability` may select
`architecture_path` and `goal_path` within its pinned architecture repository and
goal document. The collector reads the current Product GitHub binding twice,
checks its immutable commit and freshness, reads the actual retained source
archive, and verifies every selected byte against the manifest. It parses
CloudFormation JSON or Terraform JSON as data and compares canonical resource
structures and normalized goal content. Each selected file is bounded to 32 KiB
for private evidence; other formats or larger selected files remain unqualified
by this collector. It never executes IaC or derives a format from a pass flag.

With all three unchanged installed artifact checkpoints, these observations can
complete `two-non-adp-projects`: two distinct Product projects and GitHub
repositories, neither equal to the workflow's actual platform repository ID,
different architecture resources, and different goals. Tampered bytes,
incomplete pagination, revoked or stale sources, missing checkpoints and
changed structures fail verification. This check does not waive the original
WA07 criterion's provider-ingestion evidence or complete other acceptance rows.

Several remaining checks need runtime evidence interfaces, not another retry
of the release driver. The current native runtime observation exposes present
attempt/work/collection state but explicitly lacks an installed fault journal
and an independent count of actual provider Start calls. It cannot establish
lost responses, worker crashes, or a local timeout while AWS was still running.
The schedule receiver is internal to the worker; the selected-run observation
cannot expose rejected/disabled events that have no run. Source snapshot reads
currently expire independently of remote source holds, so a 410 response cannot
prove that an object was deleted or retained. A scoped experiment controller,
durable journal, real provider-attempt observations, and hold/release and event
readbacks are required before those checks can complete. Current schema hashes
likewise cannot establish an earlier migration/rollback experiment. Manual UI
acceptance remains separate while UI automation is prohibited.

## Owner-selected durable observations

The optional Gateway capability
`L5_WA_QUALIFICATION_OBSERVATIONS_ENABLED=true` exposes existing durable WA
records for a finite owner-selected scope. It is disabled by default. Install
the `l5_wa_qualification` migration before enabling it. The migration retains
selection records and refuses downgrade while any remain.

All routes are under
`/v1/products/{product_id}/projects/{project_id}/services/well_architected/qualification-experiments`:

| Method and suffix | Purpose |
|---|---|
| `POST /preview` | Preview an exact existing binding and run or schedule-event selection. |
| `POST` | Record the preview using its selection digest, configuration `If-Match` ETag, a unique `Idempotency-Key`, and an explicit expiry. |
| `GET /{experiment_id}` | Read the stored selection and its management ETag. |
| `GET /{experiment_id}/observations` | Read current durable run, source-hold and selected schedule-event records. |
| `DELETE /{experiment_id}` | Revoke the observation selection using its management ETag in `If-Match`. |

Preview takes `binding_id`, optional `run_id`, and optional `events`, each with
an exact `trigger_id` and `event_id`. Select a run or at least one schedule
event. Creation takes `selection` with that same request, `selection_digest`
from preview, and an aware ISO `expires_at` more than one minute and at most
24 hours ahead. At most ten distinct event identities and 200 retention holds
are returned; an oversized hold set fails without partial evidence. Rejected
schedule events with no admitted run remain observable. All responses use
`Cache-Control: no-store`.

The caller must remain the current human configuration owner and a member of
the Product team; platform-admin visibility is insufficient. Current source
access, selected connection, credential version, configuration, exact binding
generation and original ADP creation receipt must still match. The run's input
and admission digests are frozen by the selection. Authorization and selection
activity are rechecked before returning observations. A revoked source denies
readback. Management and revocation remain available when the capability is
disabled; replay cannot resurrect an expired or revoked selection.

The only supported mode is `observations`, and responses state
`execution_authorized: false`. Recording or revoking a selection creates no
execution grant, setup work or run; it does not revoke source holds, contact
providers, inject failures or terminate workers. A stored `provider_state` is
the runtime's durable value, not a fresh native read.

These records support investigation of WA03/AC03 retention and WA06/AC02
periodic admission. They do not complete those acceptance checks. The response
always lists the missing fault history, provider send count, native state,
source object presence and historical-transition proof. A transport send count,
if added by a separately reviewed collector, would still not establish billed
executions or native generation count. Keep the original acceptance criteria
open until their independent live observations exist.

### Installed observation collection

A resolved private release case may select up to ten existing owner observation
receipts under `qualification_experiments`. Each entry supplies the exact
`experiment_id`, integer `revision` and `selection_digest` returned by the API.
The release reader uses authenticated first-party GET requests, verifies the
current owner, frozen run/source scope and receipt activity, and retains each
receipt, observation and canonical digest. It rechecks authority before returning.
This collection does not create a selection, grant authority or submit work.
Fresh read timestamps may change during emission; all durable facts must match.

### Observing an actual source unlink

For a source-revocation check, preview and record an observation selection with
`source_revocation_binding_id` as well as `run_id`. The source must be currently
accessible, healthy and one of that run's exact pinned sources. The selection
freezes the source binding identity, generation and Product revision. If the run
already has a terminal result, it freezes the latest complete result's identity,
revision, content digest, native-generation digest and source-manifest digest.
Earlier result revisions remain retained. The review settings are pinned too. Selecting observations does not
permit or perform an unlink.

After the owner independently completes an authorized unlink through the existing
Product binding API, the same owner can read:

```text
GET /qualification-experiments/{experiment_id}/source-revocation-observations/{operation_id}
```

The response joins the earlier selection to the actual source tombstone, successful
`unlinkBinding` operation and Product change. Failed, pending or unrelated
operations, replacement sources, changed configuration/native identity, reused
binding IDs and inactive selections are rejected. The reader also verifies that
current source authorization denies the original run. It returns provenance IDs,
digests and timestamps; it does not return source content or make provider calls.

For the release path, retain `native-observations.json` before unlinking the source.
The selected source receipt must have frozen the terminal result before that
readback. Keep the original fixture and workflow context, and put the existing
run-to-unlink-operation mapping in a private JSON file. Then invoke the read-only
staged collector within that same release context:

```sh
python3 modules/L5-services/scripts/qualify-well-architected-release.py \
  observe-source-revocation "$L5_SOURCE_SHA" "$L5_PRIVATE_FIXTURE" \
  "$L5_PRIVATE_EVIDENCE" \
  --source-revocation-operations "$L5_PRIVATE_UNLINK_OPERATIONS"
```

The collector reads the unchanged selection and provenance, then requires exact,
nondisclosing 404 responses to the same owner's result, source manifest and
publication-status requests. When the baseline contains later-task reuse evidence,
its exact design-usage GET must also be denied. Foreign-reader denial remains a
separate observation. The collector writes a new private
`source-revocation-observations.json` bound to the original native evidence hash
and release context. It never issues DELETE, POST, provider calls or new work.

Emission rechecks the provenance and denied reads. The other original case is
independently read again; if it completed selected owned cleanup, its original
cleanup artifact and current absence/result proof are checked instead. Source
revocation and native binding cleanup on the same case are rejected because that
combination needs separately retained authority transitions. The original native
baseline must be no more than 24 hours old, and the owner selection must still be
active. Retained evidence is never overwritten.

This proves the selected source-access transition. Independent provider invocation
counts, source object presence during uncertainty, policy-safe hold release and
the other original acceptance checks remain required. A source-revocation receipt
alone cannot complete Context reuse or the delivery flow.

## Installed periodic admission

The existing Well-Architected minute repair invocation produces configured UTC
schedule ticks. There is no additional scheduler or public submission API.
Gateway migration `176_l5_schedule_dispatch` retains per-configuration-revision
cursors and append-only receipt observations. The WA service root gives its
distinct repair identity the `l5:schedule-admission` registry scope and one exact
IAM POST path for admitting a stored schedule receipt. Apply that service root
and roll out Gateway and worker code through the existing release/lifecycle path
before qualifying periodic work. Other services retain their existing permissions.

Each invocation initializes at most 50 previously unseen configuration revisions
and produces at most 50 due trigger events across all configurations. Unprocessed
trigger cursors remain due for the next invocation. Due work is ordered by its retained
next tick, so disabled configurations cannot occupy the head of the queue forever.
Schedules use five numeric UTC cron fields, including lists, ranges and steps;
restricted day-of-month/day-of-week fields have standard cron OR semantics.
Unsupported or impossible expressions fail configuration validation. A new
revision starts at the first matching minute at or after actual initialization;
enabling partway through a minute never creates an earlier tick. A delayed tick older than five minutes is
retained as `missed_tick` and never starts historical paid work. Restarting repair
reuses the exact event identity, which includes the configuration revision.

The internal request supplies only an existing event key. Gateway resolves the
current exact owner grant, rechecks Product membership, relationship, selected
sources, binding generation, connection, source-only role and native Profile, and
then uses common source capture, admission, budget and queue dispatch. Legacy
grants without configuration scope cannot authorize this new path. The request
actor remains the registered service; the run retains service/schedule attribution
and the original consent owner for broker accountability. Repair cannot retrieve
credentials from ordinary credential-session routes. Native resource discovery
is never enabled by schedule admission. ADP ticks use the same architecture
generation eligibility as manual source-only reviews. The native
`eligibleForScheduledGeneration` flag describes AWS-owned resource
recommendations; it remains part of the frozen Profile readback but does not
authorize or gate ADP ticks. See the [AWS Profile response fields](https://docs.aws.amazon.com/wellarchitected/latest/APIReference/API_GetAgentProfile.html)
and [architecture review procedure](https://docs.aws.amazon.com/wellarchitected/latest/userguide/agent-architecture-reviews.html).

`l5_service_event_observations` records actual receipt arrivals, duplicate arrivals,
admission decisions and repair/reconciliation outcomes. It does not backfill old
history or count provider invocations. Read these retained rows with the exact
event receipt when qualifying disabled/enabled/replayed/revoked cases. Original
native acceptance, independent provider counts and live Profile/settings readback
remain required; offline tests and migration success do not complete delivery.

| Retained reason | Next action |
| --- | --- |
| `schedule_invalid`, `schedule_mismatch` | Save a supported UTC expression and inspect the tick against it. |
| `missed_tick` | Check repair invocation health; subsequent due ticks continue without catch-up generation. |
| `delegation_denied`, `delegation_scope_changed` | Review the current owner selection and explicitly authorize its exact configuration scope. |
| `budget_exhausted` | Review the selected hard budget and existing reservations before scheduling more work. |
| `native_unqualified` | Check the selected Profile's architecture-generation eligibility, source-only settings and pinned identity. |
| `native_readback_unavailable` | Verify native API support and selected connection permissions; dependency failures remain retryable within the tick window. |

### Periodic acceptance observations

The installed generation adapter commits `wa-start-transport/v1` at the SDK's
actual `before-send` hook and appends the response's request/generation identity
at `after-call`. A missing response remains uncertain. A pre-send record alone
does not prove that AWS received or accepted a generation, and cannot authorize
another Start. These records begin with the installed instrumentation; no earlier
send counts are reconstructed.

Under the owner-only `qualification-experiments` API, `POST periodic-checkpoints`
records a current, immutable observation selected by `binding_id` and `trigger_id`.
It requires the configuration ETag and its own idempotency key. It exports the
actual schedule cursor, event arrival/decision history, runs, outbox work, transport
records, and independent Profile/generation readbacks. It does not tick a schedule,
enable a trigger, grant consent, or start native work. `GET periodic-checkpoints/{id}`
rechecks current membership, owner, source and connection rights, including for
retained observations of an earlier trigger revision or an owned cleaned Profile.

Use a fresh configuration revision for each bounded case. Record a checkpoint
after repair initializes its cursor and before the selected future due tick; record
the second checkpoint after the actual occurrence has settled. The disabled case
must span a due tick with no event, run or outbox work. The enabled and duplicate
cases need one admitted run and one independently correlated native Start; duplicate
arrival must be present in the append-only journal. Revoked-consent and hard-budget
cases need their actual admission refusal and no work or send. Existing uncertain
work cannot be erased to obtain a clean baseline.

`POST periodic-intervals/read` takes the two retained checkpoint IDs. Explicit
`read_native_audit: true` requests filtered CloudTrail readback only when
`L5_WA_PERIODIC_AUDIT_READS_ENABLED=true`. No IAM policy is installed or enlarged.
Missing selected-role read access remains an actionable incomplete observation.
LookupEvents has no resource-level IAM scope; the temporary session is limited to
that read API and selected Region. Only events matching the selected role,
Profile and interval are projected; unrelated records and credentials never leave
the reader. AWS documents [Well-Architected API logging](https://docs.aws.amazon.com/wellarchitected/latest/userguide/logging-using-cloudtrail.html).

CloudTrail delivery is delayed: an empty result alone never proves no effect.
For a negative case the verified claim is only that the selected ADP occurrence
created no run/outbox work and made no sends under the exact installed mandatory
instrumentation. No account-wide absence claim is made. Positive cases require
the matching independent CloudTrail request ID and native generation Get/List
identity; missing or ambiguous observations remain incomplete.

The private fixture's `qualification_periodic` selects exactly `enabled`,
`disabled`, `duplicate`, `revoked`, and `budget`, each with only `before_id` and
`after_id`; fixture-authored counts or pass flags are rejected. Collection and
emission read the retained evidence again under current authority. Checkpoints
must fall within the same release's independently observed installed-artifact
interval. Only the complete five-case proof promotes original `WA06/AC-02`,
`WA06/SCN-02`, and `periodic-admission`; other required criteria remain independent.

## Publication replay and later-use intervals

The owner-only `qualification-experiments/reuse-checkpoints` route records
existing work and receipts for one original result. It does not invoke publication,
task use, native Start, cleanup, or a browser. The existing immutable checkpoint
table stores the separate `wa-reuse-checkpoint/v1` contract; periodic and reuse
routes reject one another's checkpoint IDs. Both the original ready owned Profile
and a verified terminal owned-cleanup receipt are supported. Adopted or unverifiable
bindings remain outside this collector. Ready observations do not grant retained
reuse consent; post-cleanup use still requires the separate existing owner consent.

Before the separately authorized replay/use actions, submit a checkpoint with
`result_id`, `result_revision`, a distinct later `task_id`, and the original
`context_retry_key` and `insights_retry_key`. Supply the original configuration
ETag in `If-Match` and an `Idempotency-Key` for this observation. The two replay
keys select the canonical publication retry receipts; they do not execute them.
Record a second checkpoint after both publication retries and the completed
task's actual result use. Repeating a checkpoint key returns its original facts,
so it cannot retrospectively create a baseline. A new checkpoint key creates
another observation, not another native run.

`GET reuse-checkpoints/{id}` and `POST reuse-intervals/read` recheck current owner,
source, result, binding and publication identities. The interval must cover one
unchanged result within 24 hours. Explicit `read_native_audit=true` uses the same
bounded selected-role CloudTrail reader and existing audit feature flag described
above. It can read the original Profile's audit records after owned cleanup without
calling GetProfile or creating a replacement. Missing audit permission remains
an actionable observation failure; this route installs no permissions.

After these actions, keep the frozen release fixture unchanged. Create a private
map from the original run ID to only `before_id` and `after_id`, then collect it
through the same original release:

```sh
python3 modules/L5-services/scripts/qualify-well-architected-release.py \
  observe-reuse-interval "$SOURCE_SHA" "$PRIVATE_FIXTURE" "$PRIVATE_EVIDENCE_DIR" \
  --reuse-intervals /private/reuse-intervals.json
```

The stage writes `reuse-interval-observations.json`, bound to the original native
and any cleanup artifacts, and reads actual canonical task/consumer receipts.
It preserves earlier successful owner artifact reads and foreign denials. The
emitter rechecks the interval; a later source-unlink stage retains its digest
alongside the separately verified unlink proof. Collect before unlink because
revoked source rights cannot be restored or bypassed for observation.

For the original `context-reuse` check, the ready-binding interval can compose
with the existing later-task and source-revocation proof. The existing prohibition
on combining same-case native cleanup and source unlink remains: a post-cleanup
interval is partial replay/use evidence until a compatible separate source proof
exists. Neither collector manufactures that transition.

Acceptance requires unchanged installed artifacts covering the whole interval,
unchanged run/work records and append-only Start transport records across all
revisions of the selected configuration, no unmatched prior send, both original
publication retry receipts newly observed inside the interval, and the exact later
task's linked retrieval/use receipts. Delayed empty CloudTrail results corroborate
the mandatory transport journal; they never prove accountwide absence. The
verified `adp_start_count` is scoped to this configuration and interval, while
`provider_effect_count` remains unknown. Only composition with the complete
original `WA05/AC-03` proof can advance `context-reuse`. Actual publication
failure/lost-ack history for `WA05/AC-02`, full client parity, every other original
check, and manual WA07 acceptance remain separately required.

# DevOps release qualification (DA19)

This is a **build-phase** operator guide, not evidence of a deployed or qualified
DevOps release. The `journeys` selector invokes four native operations through
first-party Product, installed CLI, MCP and browser clients, then compares their
run IDs and native job IDs with independent readbacks. It does not create a
Product, provision a validation service or prove the complete epic. DA19 must
run the eventual grouped release and all four accepted live scenarios after the
harness merges, using one compatible
artifact set and two real, independently authorized non-ADP projects. Offline
examples in this repository are synthetic and cannot close acceptance.

Only the four-client `journeys` path currently connects its installed CLI,
Product API, MCP and first-party browser actions to selected provider-task
readback. The provider task type must match the submitted capability; the
provider task API does not supply measured spend, so this check cannot replace
independent usage readback. The Gateway budget API reports aggregate project
spend, not the cost of one native job; do not assign that total or a fabricated
zero to a journey. Native task cost remains unavailable, with API provenance and
scope/time coverage; positive admission allocations and request/duration bounds
still apply. Estimates must remain labelled estimates. All five selectors now check deployed ingress through the selected-role
AWS policy reader instead of a separate HTTPS credential, before performing
their actions. Journey readbacks can now use installed Product routes and native
task APIs under an independent Product credential, without a journey server.
Recovery can also submit investigations through the installed Product API and
independently read their retained native tasks and space inventory, without
custom `submit`, `work` or `spaces` servers. Isolated HTTP API route inventory and
owned cleanup can use selected AWS roles instead of four more custom channels.
Context document content can use current Product bindings and immutable GitHub
blob reads without a custom source server; see the installed-source selection
below. This does not replace the remaining Context publication or later-run channels.
The remaining `release` and `capabilities` HTTPS channels, the
legacy `journeys` channel, and the remaining recovery, Context, portability and
acceptance channels described below are not installed DevOps services.
Do not provision endpoints or per-assertion credentials to make these selectors
pass. Their replacement with the existing scoped authorities is pending; until
then, even passing offline selectors are not executable live qualification.
The worker archive can be built and checked independently of these private
live fixtures and of the shared worker runtime assembly being repaired under
the shared integration owner under #9292.

### Private Product-session transport

The shared fixture materializer writes only the private JSON fixture. DevOps's
service preflight can additionally consume an optional `private_clients` object
in that fixture. This transports maintained Product sessions, not worker IAM
credentials. The coordinator obtains these sessions through the ordinary login
flows; the driver neither invents identities nor signs in on their behalf.

The object has these fields:

| Field | Contents |
| --- | --- |
| `version` | Integer `1` |
| `environment` | Token values keyed by the manifest's referenced `DEVOPS_…` token variable names, including the two recovery users' `credential_ref` names |
| `cli_config` | The selected CLI deployment's `config.json`, with `gateway_url` equal to the selected Gateway plus `/api` |
| `cli_tokens` | That deployment's maintained `tokens.json`; do not copy a rotating refresh token from a simultaneously active CLI session |
| `browser_state` | Playwright storage-state JSON for the journey user |
| `recovery_browser_states` | Separate storage-state JSON objects keyed by the two configured recovery user IDs |

Keep the entire fixture in the existing protected configuration transport, never
in the checkout or public documentation. Browser origins and secure-cookie
domains must match the selected Gateway. Only referenced `DEVOPS_…` environment
names can be populated; this cannot set AWS credentials, executable search paths
or release identity variables. Other existing private-provider connections remain
separate prerequisites for selectors that still require them.

Preflight installs the reviewed checkout's CLI locally, using its existing
installer and deployment registry, and checks the supplied `cli_sha256` against
the reviewed `adp` entrypoint. It requires the installer's normal local tools,
including `sh`, Python and `jq`; it does not download missing CLI files. It writes
an isolated CLI home and mode-0600 session/browser files under `private-clients`
beside the fixture, outside the evidence directory. The paths in the hydrated
manifest replace `cli_home`, `cli_executable`, `browser_storage_state` and the
recovery browser paths. The original private bundle is removed from selector
snapshots and qualification evidence. Each driver process loads the token values
into its own environment rather than writing them to workflow environment files.

Repeated preflight/run/emission calls preserve refreshed local sessions instead
of overwriting them. A changed source or client bundle cannot reuse the same
private-client directory. Retain failed or uncertain attempt state for recovery;
after reconciliation, remove the attempt's owned private-client directory through
the runner's private workspace cleanup. Do not reuse these credentials in another
Product or environment.

### First installation with paused consumers

Preflight checks local input shape and client state; it does not wait for a ready
binding or complete asynchronous setup. Consumers can therefore remain paused
through preflight. The shared release owner still decides when to enable them;
these checks do not declare the incomplete runtime installation-ready.

For an authorized first-time setup, prepare the Product and its linked DevOps
configuration through ordinary Product APIs. Select its current connection,
consent, capabilities and budgets. Put `pending-setup` in the intended DevOps
trigger policies' binding IDs and the matching journey/recovery binding inputs.
The fixture's optional `installed_setup` object specifies `tenant_id`, a stable
16–128-character `receipt`, `max_requests` (8–100), `deadline_seconds` (30–900), and
the ordinary binding-operation `request`. Use `action: create` with the current
`configuration_revision`, or `action: link` with those fields and the exact
`native_resource_ref`. This requests only the existing scoped setup operations.

After consumers are enabled, the run phase reads the current linked configuration,
submits the operation to the Product's `binding-operations` route, and polls its
durable operation ID. It checks the resulting binding's Product, configuration,
connection, provider account/role/region and native space. It then replaces only
the declared pending trigger IDs through the ordinary conditional configuration
update, requires a successful Product handoff, and submits a follow-up binding
check against that new revision. Journey and recovery inputs receive the observed
binding and configuration revisions before selectors run. Existing bindings can
instead use `action: check`, `binding_id`, and an `if_match` binding revision such
as `"1"`; this does not rewrite their configuration.

Setup requests and polls share the declared bound. Failure or uncertain delivery
retains the stable request and operation receipt and emits incomplete evidence;
no selector or replacement create request is started. Local tests exercise this
sequence against mocked Product HTTP boundaries, and mounted Gateway tests verify
that accepted setup remains queued without provider calls until a worker runs.
These checks do not establish a live installation. The remaining runtime callbacks
and custom qualification channels identified above remain required work.

### Expired consent before setup starts

An accepted DevOps create request can outlive its setup consent while consumers
are paused. Fresh consent alone does not authorize that retained request. The
current Product owner must explicitly continue its authority through:

`POST /v1/products/{product_id}/projects/{project_id}/services/devops/binding-operations/{operation_id}/authority-continuations`

First read the original operation, its continuation history and current `delegation-scope` with
`for_control=true&control_action=devops_setup`. Grant fresh setup consent through
the existing owner `delegations` API. Its complete scope must equal the expired
consent, including owner, configuration, credential revision and selected sources.
Keep at least one minute of consent lifetime available for the continuation.
Submit a new stable `Idempotency-Key` and the original operation revision in
`If-Match`, with this body:

```json
{
  "action": "continue_unstarted_setup",
  "intent_hash": "<original intent hash>",
  "delegation_id": "<fresh owner setup consent>",
  "delegation_revision": 1,
  "scope_digest": "<current exact setup scope digest>",
  "reason": "Setup consent expired while consumers were paused."
}
```

The endpoint accepts only an unchanged create intent whose operation is still
accepted, with zero execution/reconciliation attempts, no binding or native
evidence, and exactly one original work item that has never been claimed. Even
an expired historical lease or an incremented fence requires reconciliation
instead. Revoked prior consent, changed ownership, scope or configuration cannot
be repaired through continuation. The endpoint returns a named error and an
action to take; it never resets work, enables a consumer or contacts a provider.

Success appends a hash-linked receipt and preserves the original request,
idempotency key, authorization record, intent hash, operation, work and native
identity. Replaying the exact continuation key and payload returns that receipt;
changing its payload conflicts. The unchanged original create key and body may
then replay under the continued authority, while a changed body still conflicts.
Use the existing work item for eventual execution. Do not submit a replacement
create operation. If consent expires again before any claim, another explicit
continuation can append to the same history.

`GET` on the same route returns owner-scoped receipt history, the original intent
hash, request and idempotency key needed for safe continuation. `active_until` is
the recorded expiry, not a claim that consent remains unrevoked. The field
`continuation_provider_effects: 0` describes the continuation action only; read
the setup operation for its eventual provider outcome. Workers and the broker
resolve current authority separately from immutable native identity and recheck
expiry, revocation, ownership, scope and credential state before provider calls.

Issuance locks the configuration, original work, operation, sorted grants and
credential in that order. Worker claims share the work-row lock, so competing
continuations and worker starts serialize in PostgreSQL. The shared L5 package
owns the receipt model and migration; Gateway bridge `175_l5_setup_authority`
installs it on the platform migration lineage. Co-deploy the matching Gateway
and worker package before using this API. Preserve receipt history on rollback;
the Gateway downgrade refuses to discard it. Offline API, worker and PostgreSQL
race tests verify these boundaries without proving live native completion.

### Source eligibility without invented indexing evidence

Product GitHub access does not establish an AWS native registration. The service
adapter discovers existing registrations through `ListServices` and `GetService`,
matches the verified GitHub repository owner and instance, and creates a separate
`AssociateService` association for each selected repository using native
`configuration.github` fields. Repository IDs, names and owner types come from
the selected GitHub reader's repository metadata, not from Product connection IDs.
Missing or ambiguous native registrations stop setup; the adapter never calls
`RegisterService` for GitHub, which that API does not support. A missing-registration
reason is retained in the durable setup record so setup can resume after the
coordinator prepares the real registration. Lost association acknowledgements
still require readback reconciliation rather than another creation attempt.
New associations explicitly disable review and automated-testing capabilities;
enabling native actions needs the separately authorized setup path. Existing
registrations and unrelated repository associations are preserved.

The native association API exposes repository identity, association status and
release capability configuration, not indexed commit revisions. New eligibility
observations can record `source_indexing` with `status: unavailable`,
`source: devops-agent:GetAssociation` and
`reason: native_api_does_not_expose_indexed_revisions`. They must leave
`source_indexed` null and `indexed_revisions` empty and retain the matching native
association observation. The release gate requires the selected repository and
space, a valid association and an enabled release-readiness capability. The
worker repeats the association read before submission; revocation prevents the
paid task call. Existing recorded eligibility snapshots remain compatible.

This does not replace current Product source authorization or exact base/head
source capture. Result citations must still match the sealed revision, changed
path, line and content digest. Readable source and a valid association are not
reported as proof of native indexing. The production exporter and the remaining
factory callbacks described below still need assembly.

### Investigation lifecycle and installed result readback

The no-argument worker entry point is
`l5_services.runtime_devops:factory`, selected through `L5_WORKER_FACTORY` with
`L5_SERVICE_ID=devops`. It consumes the shared `runtime_support` resource and
authority helpers for IAM-authenticated database sessions, signed Gateway
transport, scoped Product source reads, the archive store and service queue.
Its operation adapter connects the existing investigation and release-readiness
submit, reconciliation, observation, collection and cancellation implementations
to `ExecutionService`, including durable result persistence.

Manual investigation admission now derives a run-specific child delegation from
the owner's current Product membership, selected connection and DevOps grant.
The child requires Product read and credential-session permission, not source
pin/read permission for a source-free investigation. The broker validates the
nested DevOps hosting selection and binds the session policy to the exact native
`agentspace` ARN and the current lease deadline. The policy permits task
submission, observation and cancellation, but no space deletion or IAM actions.
Current Product authority, owner consent, binding generation and grant expiry
are rechecked before native calls. The durable integration tests exercise the
mounted Gateway, broker and installed worker with only external provider and
infrastructure boundaries replaced; these are offline checks, not live evidence.

The setup callback uses the shared durable operation and lease for native space
creation, adoption and checks. It requires an explicit Product-owner operation,
Product read and credential-session permission, and a selected investigation,
release-readiness or prevention grant; browser-only grants cannot create spaces.
Creation uses a stable operation token and ownership tag. Reconciliation reads
the original candidate or bounded native inventory rather than creating another
space after an uncertain response. The native identity and lifecycle receipt
are saved in the same transaction that completes setup work. Adoption and checks
are read-only; newly created spaces have observed enabled goal schedules disabled
before readiness is reported. Missing goal observations remain unverified.
Other setup controls, including deletion, are rejected before changing binding
state until their ownership-safe adapters are installed.

This factory is **partial, not installation-ready**. Remaining setup controls,
Gateway exports, release-verification readers, native background authorization/readback and
Context publication callbacks still need assembly. The packaged shared helper
supports source-free DevOps investigations with the admitted native space and
connection scope, and rechecks the broker before provider calls. Resource and
telemetry inputs do not require invented source snapshots. Keep consumers
disabled until the remaining connections are complete. Loader tests use the
packaged dependency and mocked external boundaries; an importable package does not
establish complete production execution or native acceptance.

The investigation adapter now polls `GetBacklogTask`, collects the accepted
execution with `ListExecutions`, and retains its journal and versioned operational
recommendations using `ListJournalRecords`, `ListRecommendations` and
`GetRecommendation`. It does not use a nonexistent `GetExecution` operation or
turn operational recommendations into security scan findings. Cancellation uses
`UpdateBacklogTask` and confirms the same task by readback; uncertain delivery
retains the original worker attempt and native identity.

ADP-submitted investigations expose the existing scoped result and artifact
routes. The result contains a digest-bound `evidence_ref`; its JSON artifact
contains the native task, accepted execution, journal records, operational
recommendations and unavailable-cost provenance. The four-client journey reads
these installed routes and verifies the artifact digest and task identity against
the independently read provider task before accepting an investigation journey.
This does not replace the other selectors' remaining custom readback channels.

### Installed Context knowledge asset readback

The Context selector can read one native knowledge asset without an asset
readback server. In the private manifest, select `context.installed_asset` and
omit `asset` from `context.readback_channels`. The selection names `token_env`
(an independent Product read credential), `agent_space_id`, `binding_revision`,
`kind` (`instruction` or `skill`), `publication_scope` (`global` or `agent`),
`visibility` (`product_team`) and the exact `native_metadata` selection described
in the [native knowledge contract](native-knowledge-contract.md). The installed
reader verifies provider agent targeting and skill activation independently of
ADP provenance. Private visibility and a generic native `context` type have no
qualified mapping and are rejected before external calls. The existing journey configuration selects the
Gateway, Product/project/binding and provider connection; no additional AWS
connection or assertion service is introduced.

This path requires one immutable `context.sources` reference, including its
source connection, record type, revision and SHA-256, because the native knowledge
publisher maps one source to one asset. It reads the installed Product binding
before contacting the provider, rejects foreign or changed bindings, then
verifies the selected AWS account and role and calls `GetAsset` and
`GetAssetContent` at the observed version. Asset metadata must match the source,
visibility and active state; the bounded archive must contain only the expected
file, whose digest must match the selected source. The four external requests
share the Context selector's request and deadline bounds. Evidence retains the
binding and native observation, not credentials or native file contents.

This replaces only the Context `asset` channel. Source entitlement, publication,
later-agent actions and the other custom channels remain unresolved; this option
does not make all-five-selector qualification executable or prove live acceptance.

### Installed recovery route inventory and cleanup

For an existing, isolated API Gateway HTTP API, select `recovery.installed_routes`
and omit `cleanup_routes` from the recovery action channels and `routes`,
`route_owners`, and `route_inventory` from its readback channels. This adapter does
not create an API or modify its tags, integrations, stages, or deployment.
The private selection has `api_id`, `reader`, and `actor`. Each authority has
`connection_label`, `account_id`, `role_arn`, and `region`; the read and action
connections must use distinct IAM roles in the same selected account and Region.
Use existing authorized connections, with read-only API access for the reader
and narrowly scoped route deletion for the actor. Route references in
`owned_routes` and `retained_routes` use `api_id:route_id` and must all belong to
that API. Other route kinds still need their installed adapters.

The API must already carry `adp:tenant_id`, `adp:product_id`, `adp:project_id`,
and `adp:qualification-run` tags matching the selected scope and current run.
Both roles verify their assumed identity and those provider-observed tags.
The reader obtains the complete paginated `GetRoutes` inventory, including
unlisted routes that must be retained. Before each `DeleteRoute`, the actor
rechecks API ownership and compares `GetRoute` with the independent baseline.
Managed routes and changed routes are not deleted. Every serving stage must use
automatic deployment. Both roles read each serving stage using stage-scoped
`ExportApi` with integration extensions included. The private baseline retains
the exported route definitions, including integration and authorization
dependencies, stage variables, and effective route settings. Bounded native reads
wait until every stage contains exactly the retained routes with their original
behavior. A changed deployment ID alone
cannot prove that a deployment includes every deletion. Each export is checked
against the stage's deployment identity before and after the read, and the
independent reader confirms the same completed configuration before cleanup is
acknowledged. Manual deployment is not performed or authorized by this adapter.

Cleanup reserves requests for deletion and subsequent route and space reads;
the reserve grows when the observed inventory needs more pages. Each before/after
verification phase uses one fresh route snapshot for inventory, ownership and
retained-route assertions. Cleanup still performs its own independent readback;
no snapshot is reused across effects. The final spaces read has a separate
allocation, within the unchanged 100-request maximum. Failures consume
their allocated request budget and never turn an incomplete inventory into
success. The private action journal persists the independent baseline before any
deletion and records uncertain deletions. If an
acknowledgement is lost, retain the original baseline and reconcile actual route
absence before continuing; do not start a new selector run blindly. No usage
total or task-cost measurement is inferred from route API calls.

This replaces four recovery channels only. Revocation, fault injection, worker
restart, release/capability readbacks, and the other selectors' remaining custom
interfaces still prevent complete installed qualification.

### Installed journey observations

Select `installed_journeys` in the private manifest and omit the `journeys`
entry from `readback_channels`. The selection contains `tenant_id`, `token_env`
and an aware `started_at` within the current release authorization. The token is
a separate Product API credential, not a new assertion service credential. It
must differ from the action credentials and remaining reader credentials. The
normal `journey_driver` still selects the Gateway, Product/project/binding,
client inputs and native provider connection.

The selector uses its freshly submitted run IDs to read the installed `/runs`,
`/results` and artifact-content routes, then compares the run with a separately
authorized `GetBacklogTask` observation. It rejects wrong tenants, Product or
binding scope, changed inputs, duplicate native executions, stale tasks and
missing provider timestamps. Investigations also require their digest-bound
result artifact. Request counts describe the qualification's API reads, including
native identity verification; they do not claim to measure provider-internal
requests. Duration is the native task's observed creation-to-update interval.
Both counts and duration remain bounded, while task cost stays unavailable.

Standalone recovery, Context, portability and acceptance selectors use the same
installed journey reader when this selection includes `runs`, a mapping of
`product`, `ui`, `cli` and `mcp` to the selected current journey run IDs. The `all`
driver discards pre-supplied IDs, runs journeys first, and passes only the new IDs
to subsequent selectors in `manifest-observed.json`. A failed journey never falls
back to old run IDs or a custom journey channel. This connection alone does not
complete the remaining capability, release and scenario-specific adapters.

### Service-owned release driver

`qualify-devops.py all` validates the selected release's source, workflow definition,
service and environment before invoking any selector. It preflights all five
selectors, snapshots the private manifest into a fresh run directory, attempts
each selector, and calls the native qualification emitter. A failed selector or
missing independent observation remains unresolved, not a passing qualification.
An existing run directory is never replayed automatically: reconcile uncertain
effects before requesting another run.

The shared dispatcher uses the executable `qualify-devops-release.py` and
`emit-devops-qualification.py` entry points with source revision, private fixture
and evidence-directory arguments. The emitter rechecks the saved attempt and
original manifest; it does not rerun native actions. Independent observation
bundles and each criterion record must carry the matching `definition_revision`
and `.github/workflows/l5-services-release.yml` as `workflow_path`, as well as
source, runtime and run identity. Missing or substituted identity fails emission.

Each successful selector retains the independent reads its validators checked in
`devops-<run-id>-<selector>.json` and writes a separate `<selector>.json` summary.
Both files are private and created exclusively. Source bytes are retained as
base64 in the evidence; each summary points to the saved readbacks and carries
the evidence file's SHA-256 digest, observed release artifacts, deployment identity
and qualification run ID. Evidence is bounded to 32 MiB per selector.

The summaries preserve the verified native results, two-user denial and cleanup
checks, later Context reuse, two-Product portability, all 63 acceptance/scenario
pairs, ten epic criteria and both SSO checks. The emitter checks generated summaries
against their saved evidence before consuming them; missing, changed, wrong-run,
symlinked or non-private evidence fails the handoff. Existing separately prepared
summaries remain supported. Selector summaries do not replace the evaluator's
independent per-record observation bundle: incomplete observations still prevent
qualification even when every selector summary is well formed. Offline fixtures
remain synthetic evidence, not native acceptance.

The shared release dispatcher must supply its verified workflow environment and
the owner-controlled private inputs. Its registration and the remaining installed
adapters are still required; this driver alone does not make the release qualified.
Do not provision substitute evidence servers. Once those prerequisites are met,
the service-owned dispatch commands are:

```bash
python3 modules/L5-services/scripts/qualify-devops-release.py preflight \
  "$SOURCE_SHA" "$PRIVATE_MANIFEST" "$PRIVATE_EVIDENCE_DIR" \
  --observations "$PRIVATE_OBSERVATIONS"
python3 modules/L5-services/scripts/qualify-devops-release.py run \
  "$SOURCE_SHA" "$PRIVATE_MANIFEST" "$PRIVATE_EVIDENCE_DIR" \
  --observations "$PRIVATE_OBSERVATIONS"
```

`preflight` validates the release identity, private inputs and all five selector
configurations without invoking selectors, writing evidence or changing the
process environment. `run` invokes the existing five-selector driver and retains
its failure status. `emit-devops-qualification.py` exposes the existing native
emitter with the same options as `qualification_devops.py`; the run driver still
emits its own qualification and delivery records. The earlier
`qualify-devops.py all` command remains compatible. These entry points are ready
for registration by the shared dispatch owner; adding them does not replace the
remaining installed selector adapters.

The variables identify the reviewed source, mode-0600 manifest and independent
observation bundle, and a pre-existing mode-0700 evidence directory outside the
checkout. The run subdirectory retains `l5-devops-qualification/qualification.json`,
`delivery-evidence.json`, selector evidence and a sanitized driver receipt.
Offline driver tests exercise sequencing and rejection with synthetic fixtures;
they do not establish installed authority or native acceptance.

## Private preparation

An authorized delivery owner supplies a JSON manifest outside the checkout in a
mode-0600 file and a pre-existing private mode-0700 evidence directory outside
the checkout. Never commit the manifest, credentials, real account/resource IDs,
operator identities, target addresses or evidence. Select a protected target and
verify the chosen AWS account **and role** through the credential broker before
provider operations. Do not inherit budgets or access from a previous flow.

The manifest pins `requirements_revision: R3-secure-native-ui-ingress`, a reviewed
40-character source SHA, deployment ID, deployed timestamp and SHA-256 hashes for
`api`, `ui`, `cli`, `mcp` and `worker`. It has `authorized: true`, a private owner
and target reference. `authorization` specifies the matching source SHA, unique
run ID, `journeys` selector and an aware expiry no more than 24 hours ahead. Each
of the six committed capabilities has its own positive spend, request and time
limits. The legacy HTTPS `readback_channels` named `release`, `capabilities`
and `journeys` specify their corresponding `source`, `url` and `token_env`.
Replace the journey channel with `installed_journeys` as described above. Supply
each environment variable privately; distinct readback credentials are
required until these unsupported channels are replaced. Endpoints must actually fetch deployed state and native results, not
replay caller-provided claims. The journeys selector checks the release artifact
readback before first-party submissions and re-reads it afterward; a matching
private manifest alone never authorizes a paid journey. Native capability and entry-point observations must
carry the matching qualification run ID and truthful usage coverage. For all five
selectors, also
provide a private `ingress_policy` object: `connection_label` for the selected
AWS credential-broker connection, `account_id`, the exact IAM `role_arn`,
`distribution_id` for the deployed gateway CloudFront distribution,
`gateway_origin_id` and `gateway_origin_domain` for the API origin, plus
`frontend_hostname` matching the selected first-party `journey_driver.gateway_url`
and one of the deployed CloudFront distribution's exact aliases (or its domain).
Supply the full
`/api/v1/products/.../browser-launch` `launch_path`, and `functions` containing every deployed SSO/helper Lambda.
`allowed_invoke_statements` maps each function and each `function:alias` to its
reviewed, exact resource-policy statement list (empty if absent). The selected
connection must permit read-only STS, CloudFront distribution, and Lambda URL,
alias and resource-policy reads. The operator must verify the inventory of
helpers and aliases against the deployed release, not just choose names from an
HTTP readback. This is a private, authorized target read; no ambient AWS role
or owner-supplied HTTP assertion substitutes for it.

The same manifest's private `journey_driver` selects one exact Product/project/
binding, the HTTPS `gateway_url` for that owner-authorized target, the expected
native `provider_hostname`, and four explicit `inputs` (`product`, `cli`, `mcp`,
`ui`) using the current DevOps run schema and binding revision. Choose only
supported native run capabilities: investigation, release readiness or release
verification. Supply distinct `receipts` for the first three submissions; the
first-party UI generates and privately journals its own run receipt. Supply
`api_token_env` and `mcp_token_env` as separate authorized credentials,
different from every readback credential. The `cli_executable` must be an
installed, executable file outside the checkout matching the private
`cli_sha256`; `cli_deployment` names its registered deployment, verified
against `gateway_url` before any actions. `cli_home` is a private existing
directory holding that deployment's signed-in session. `browser_storage_state`
is a private existing Playwright storage-state file containing a current
authorized first-party login. Install the frontend's existing `@playwright/test`
dependency and browser executable before running the selector; the script does
not install them or create a test service. Provide per-capability bounds large
enough for four submissions and bounded native polling; aggregate measured
usage must stay within each capability's budget. Resolve identities and input
sources through authorized Product configuration, not static lineage defaults.

From the reviewed checkout, after installing the L5 project dependencies and
preparing the authorized private inputs, run:

```bash
python3 modules/L5-services/scripts/qualify-devops.py journeys \
  "$REVIEWED_SOURCE_SHA" "$PRIVATE_DEVOPS_MANIFEST" "$PRIVATE_DEVOPS_EVIDENCE_DIR"
```

The script writes a mode-0600, exclusive `devops-<run-id>-journeys.json` file in
the private directory. It rejects reused run IDs, public files, expired authority,
missing bounds, non-HTTPS or shared-credential readbacks, mismatched artifacts,
wrong selected AWS identity, deployed Lambda Function URLs (including aliases),
public launch behaviors, unexpected invoke grants, missing native capabilities
and unrelated Product/UI/CLI/MCP results. It emits no raw evidence to public Actions artifacts or logs.
Reconcile uncertain jobs and cleanup privately before authorizing a fresh run.

This bounded selector is only one check: it does **not** prove two users and
denied provider calls, attest later Context reuse, validate both non-ADP
projects, qualify owned POC route cleanup or independently confirm every
committed capability from native provider APIs. It journals prepared,
acknowledged and uncertain first-party attempts privately; a timeout must be
reconciled using the same receipt, never retried with a new one. The browser
run's actual first-party receipt is captured before forwarding its request;
no handoff ticket is printed or stored in the selector output. Automation checks
the returned binding and HTTPS handoff destination, but blocks external requests
and does not visit the AWS sign-in page or console. Redirect responses fail
closed, including same-origin redirects; fixtures must use a direct first-party
page with a current session. A successful handoff is not
evidence of a completed native-console journey. If authorization or readback
fails, keep the original private journal and reconcile before a new run. Those
remaining checks require independent live
readbacks after the grouped release. A green selector or an open PR is not
merge, deployment or acceptance evidence. Keep missing or failed criteria open
under DA19 ownership.

## Isolated recovery selector

The `recovery` selector uses the same private manifest, reviewed release, current
run ID, protected evidence directory and per-capability bounds as `journeys`.
It requires `authorization.scenarios` to include `recovery`. `recovery` selects
an exact Product/project and distinct foreign Product/project, two separate
human session credential environment variables, isolated agent space and worker
pool, unique idempotency receipts for worker/event/reporting faults, explicit
owned POC and retained routes, and a bounded deadline and request count with a
separate 10–60 second cleanup grace period and five reserved cleanup/readback
requests. The
owner must independently authorize the injected faults and cleanup; this is
not permission to delete shared or adopted assets.

The private `recovery.browser` selects the first-party `gateway_url` matching
`journey_driver.gateway_url`, the current `configuration_revision`, the same
`provider_hostname`, and two distinct protected `storage_states` paths keyed by
its two user IDs. The reviewed ingress launch path must match this Product,
project and binding. Browser automation uses the installed UI for allowed
launches and makes same-origin denial requests without following redirects;
it blocks AWS and other external requests, including popup requests, and disables
service workers. It prints only a status, never a federation ticket or launch
URL. User session credentials remain separate private environment variables. A missing or stale
browser session blocks recovery; no selector provisions a new one.

Logout calls `POST /api/auth/logout` on the selected first-party browser origin
using the selected user's access token. CloudFront forwards `/api/*` to Gateway
and strips `/api`, so Gateway receives its internal `POST /auth/logout` route;
calling `/auth/logout` directly on the frontend does not reach Gateway. Remove any `logout`
action channel and its separate credential from older manifests. The selector
requires the Gateway's successful response, never follows redirects, and retains
an uncertain journal entry on an unconfirmed response without retrying. It keeps
the original token for the later `logged_out` denial probe. This verifies that
the presented token loses native-launch access; it does not establish Cognito
refresh-session revocation or replace the second user's membership-revocation
check.

For installed investigation recovery, set `recovery.installed_work` to an object
with `read_token_env`, `submit_token_env`, `agent_space_id`, `binding_revision`
and `inputs`. `inputs` maps each of the three existing recovery fault names to
its exact DevOps investigation input, including configuration revision and the
selected binding. Use two separate private Product credentials: the action
credential submits the work; the reader must have current visibility of the
same binding and runs. Neither credential may be one of the two browser users
being revoked or logged out, or one of the other channel credentials. The
selected Product/project and provider connection come from `journey_driver`;
the recovery scope and binding must match them. Omit `submit` from
`recovery.action_channels` and both `work` and `spaces` from
`recovery.readback_channels`.

The adapter reads current binding authority before submission, posts to the
normal Product run endpoint with the selected idempotency receipt, then polls
the run under the independent reader credential. A lost acknowledgement is
reconciled by replaying the same input and receipt, never by choosing another
receipt. It reads the selected native space and complete bounded task inventory,
then verifies the task, execution, original description, scope and binding
generation against the admitted run. Missing or duplicate matching tasks,
foreign scope, stale bindings, changed input and unavailable observations fail.
The private evidence includes the original Product/native readbacks.
`native_submit_count` retains the selector's compatibility field name but counts
retained native tasks, not historical API attempts; `submit_count_coverage`
makes that limitation explicit. `work_id` identifies the admitted run, not an
internal SQL work item. This adapter does not claim measured native cost.

The retained-space check lists every native space visible to the selected
account/Region connection, then reads each space's full configuration and tags.
It preserves names, descriptions, preferences, creation identities and encryption
selection in the before/after comparison; only the volatile `updatedAt` field
is excluded. Duplicate IDs, repeated cursors, a missing selected space or a
truncated inventory fail. Native calls count against the existing recovery
request limit, with a matching final-read allocation reserved before fault
actions. This inventory check is read-only; it does not authorize deleting
spaces or resources belonging to another Product.

Without `installed_work`, the legacy manifest supplies five `action_channels` (`revoke`,
`inject_fault`, `submit`, `restart_worker`, `cleanup_routes`) and seven
separately credentialed `readback_channels` (`routes`, `route_owners`,
`route_inventory`, `spaces`, `sessions`, `provider_calls`, `work`). These are
**unsupported HTTPS interface assumptions**, not installed services or services
to create for testing. Channels other than installed `submit`/`work`/`spaces` still need
adapters over existing scoped authorities; the complete recovery selector is
therefore not yet executable solely through installed adapters.
Those adapters must accept a run-bound target/scope selection, make real
scoped calls, return run-bound observations and independently read the gateway,
provider, worker and owned-resource state. A channel that replays the submitted
request or fabricates a provider session is not an independent oracle. Faults
must be safe for the isolated worker pool. Recovery probes foreign scope and
rate limiting while a user's session is still active; only then does it log out
one user and revoke the other for separate denial checks. The selected gateway
must reject each denied launch before provider session creation. A synthetic
`case: throttled` response cannot replace observing the installed rate limiter.

After the `journeys` readbacks exist for the same run, the operator can run:

```bash
python3 modules/L5-services/scripts/qualify-devops.py recovery \
  "$REVIEWED_SOURCE_SHA" "$PRIVATE_DEVOPS_MANIFEST" "$PRIVATE_DEVOPS_EVIDENCE_DIR"
```

Before any action, the selector verifies private authority, separate credentials,
exact owned-route readback, current release and approved bounds. It writes a
fresh mode-0600 JSONL journal before each external effect and fsyncs it; the
journal records prepared, acknowledged or uncertain status and receipt without
credentials. A failure leaves the journal for reconciliation; do not rerun with
the same ID or delete it to bypass uncertainty. Successful verification writes
private `devops-<run-id>-recovery.json`. Route absence and unchanged unrelated
routes/spaces require independent readback even if the action reports success.
A green offline fixture or a generic HTTP probe is **not** proof of two real
users, deployed gateway policy, real faults or cleanup; DA19 must check those
against an authorized deployed target after the grouped release.

## Later Context and final acceptance

After a verified source-backed native result and publication, select a bounded
`context` operation for a later authorized user and set
`authorization.scenarios` to include `context`. Its private manifest selection
pins Product/project, source revisions and hashes, result/publication/native
asset references, the decision intent, a fresh idempotency receipt and a
positive spend/request/deadline allocation. The private `context.action_channels`
contains `run_later`; eleven separately authorized `readback_channels` identify
`result`, `native_job`, `source`, `publication`, `asset`, `receipt`, `visibility`,
`baseline`, `later`, `usage` and `denial`. A source readback returns actual
base64-encoded content, not just a declared hash. All endpoints must refer to
existing scoped Product, native-provider and Context sources; do not create a
validation service to satisfy the harness. Action, release and reader credentials
must differ. The selector journals the request and any uncertain acknowledgment
before relying on independently fetched later-decision and denial evidence.

For GitHub document sources, remove `source` from `context.readback_channels`
and supply `context.installed_sources` in the private manifest:

```json
{
  "product_token_env": "DEVOPS_SOURCE_PRODUCT_TOKEN",
  "github_token_env": "DEVOPS_SOURCE_GITHUB_TOKEN",
  "bindings": {"123:docs/operations.md": "document-binding-example"}
}
```

These are fictional selectors, not approved targets. The Product token must
read the selected Product/project profile and source bindings; the separate
GitHub token must read the selected repository and blobs. Neither may be shared
with an action, another readback, or the installed-asset reader. Every selected
`context.sources` entry must have the exact tenant/project, Product connection,
`record_type: document`, `repository-id:relative/path` record ID, immutable Git
blob SHA as its revision, and SHA-256 of its content. The `bindings` map names
the existing Product source binding for each document, not the DevOps space binding.

The reader checks the current Product profile and healthy source binding,
fetches repository identity and blob bytes directly from GitHub, verifies the
Git object hash and content SHA-256, then rereads Product authority to reject
revocation or changes during the read. It rejects redirects and oversized
content, and charges every page and native request to the same scenario bound.
Budget at least six requests per document in addition to other Context reads;
each additional binding page costs one request on each authority read. The
existing maximum request/deadline limits are unchanged. Other document providers
are not implemented by this reader; an absent adapter remains unverified.

```bash
python3 modules/L5-services/scripts/qualify-devops.py context \
  "$REVIEWED_SOURCE_SHA" "$PRIVATE_DEVOPS_MANIFEST" "$PRIVATE_DEVOPS_EVIDENCE_DIR"
```

Run `acceptance` **only after** every accepted story journey, including the
second Product's portability journey, has produced readbacks under the same
run ID and release. This read-only selector requires `acceptance` in the
owner's scenario authorization, a private request/deadline bound and five
credential-separated `readback_channels`: `report`, `epic`, `check`, `case` and
`cleanup`. The report must cover the current R3 revision, all 63 AC/SCN pairs,
ten epic criteria, the two gateway-only SSO checks, actual native results and
completed cleanup. Independent channels reread each case, scope, revision and cleanup; attempt
statuses must be complete, and a report of itself is not independent evidence.

```bash
python3 modules/L5-services/scripts/qualify-devops.py acceptance \
  "$REVIEWED_SOURCE_SHA" "$PRIVATE_DEVOPS_MANIFEST" "$PRIVATE_DEVOPS_EVIDENCE_DIR"
```

The selector refuses a reused evidence filename. Its successful private output
is a verified count and revision, not the public record of target identities.
Offline tamper fixtures demonstrate rejection, not live exposure or live
acceptance. DA19 remains responsible for resolving failed cases and executing
the authorized post-merge evaluation; an open PR is not the grouped release.

## Two-project portability

Run `portability` after the shared release's journeys and Context have produced
current-run readbacks, but **before** final `acceptance`. Select exactly two
non-ADP Product/projects through private Product configuration: separate
repositories, teams, policies, environments and roles, plus at least one
supported non-EKS workload and selected cross-account resource role. Pin a
source SHA for each, specify two independent positive spend/request/deadline
allocations for operation and later Context use, and list only owned resources
eligible for cleanup; separately list retained assets that must stay unchanged.
Every configuration, native operation, Context operation and cleanup action
needs its own unique idempotency receipt. ADP may be an additional fixture but
never substitutes for either of these two projects.

To read each Product's native-space access through installed interfaces, set
`portability.installed_access.projects` to exactly two private selections and
omit `access` from `portability.readback_channels`. Each selection contains:

- `scope`: the selected `tenant_id`, `product_id` and `project_id`.
- `binding_id`, positive `binding_revision`, and `agent_space_id`.
- `product_token_env`: a maintained Product read session, distinct from the
  other Product's session and all action, release and other readback sessions.
- `provider`: the selected broker `connection_label`, `account_id`, `role_arn`,
  `region`, and `source_connection_id`. The project's `account_ref` must equal
  that account ID and `role_ref` must equal that exact IAM role ARN.

The adapter uses the first-party Gateway selected by `journey_driver`. It reads
the current Product binding before any native request, verifies the selected
broker role using STS, and calls `GetAgentSpace` for that exact binding. A second
Product read rejects changed or revoked authority. Four requests per Product
count against the selector's existing limit, with its cleanup reserve retained.
Wrong scope, account, role, revision, unavailable credentials, redirects and
oversized responses fail closed. This proves access to the selected native
space, not access to a different monitored resource role or completion of a
native operation. It performs no console navigation.

To submit each Product's selected operation through the installed run API, also
set `portability.installed_operations.projects` to exactly two entries containing
`scope`, `submit_token_env` and `input`. The input is the ordinary DevOps RunInput
for that Product's selected binding and capability; the maintained submit session
must differ from both Product read sessions and all other qualification sessions.
Omit `run_operation` from `portability.action_channels`. This adapter requires
the installed L5 Python package so it can use the same RunInput validator as the
Gateway rather than a second request schema.

Before submission, the adapter checks current configuration and binding scope,
positive admission allocation, deadline, concurrency, attempt and poll bounds.
It submits with the project's existing `operation_receipt`, then independently
reads the returned run through the Product API. Four HTTP requests count against
the existing selector limit, preserving its four-request cleanup reserve. A lost
or rejected response retains the original receipt in the private journal; an
acknowledged response also records the durable run ID. Reconciliation must reuse
that receipt rather than create another operation. Successful admission does not
prove native completion, measured task cost, grounding, or later Context reuse;
those still require their own independent observations.

To probe foreign Product binding access through installed interfaces, set
`portability.installed_foreign_probe` to `true` and omit `probe_foreign` from
`portability.action_channels`. This requires both installed selections above.
The probe uses the operation's maintained submit session, not an anonymous or
expired session. It reads the caller's binding, confirms the other binding
exists using that Product's independent read session, and requests the other
binding with the caller's session. Only the Product API's nondisclosing
`404`/`not_found` response is accepted. Both authorized reads are repeated to
reject revoked or changed scope during the probe. Five requests count against
the existing limit while retaining four requests for cleanup. No provider,
browser-launch or mutation call is made. This checks Product binding isolation;
it does not prove browser-launch denial or zero provider-session calls. The
independent `foreign_denial` readback and the R3 SSO checks remain required.

To confirm already prepared Product configurations without custom channels, set
`portability.installed_configuration` to `true`, omit `configure` from
`portability.action_channels`, and omit `configuration` from
`portability.readback_channels`. This requires `installed_access` and
`installed_operations`. First complete ordinary Product setup for both fixtures;
this adapter does not create configurations, bindings or native resources.

The independent reader joins the current Product profile, saved DevOps
configuration and ready binding. It reads the Product's team, selected GitHub
repository, configured access-policy reference, environment and workload kind,
and the binding's account/role identity. Repository and environment references
must still be selected by the Product. Ambiguous selections, foreign references,
stale revisions and changed authority fail closed. The reader reports configured
references, not policy enforcement, native repository indexing or source grounding.

The action uses the operation's maintained submit session to call the installed
configuration-check endpoint between two independent readbacks. It requires
ready integrations and rejects missing provider observation; an absent DevOps
integration observer remains an installation prerequisite, not a passing check.
The adapter does not edit configuration or repeat native work. Its private
journal retains the selected configuration receipt and an unverified outcome
on check failure. Configuration confirmation uses eleven HTTP requests and a
standalone read uses five, within the existing selector limit and the cleanup
reserve (at least four requests, increased by installed asset cleanup). Select a sufficient request budget within the existing maximum;
this option does not increase the authorized limit.

For isolated HTTP API route assets, set `portability.installed_assets.projects`
to two selections containing the exact `scope`, `api_id`, `reader` and `actor`.
Each role selection contains `connection_label`, `account_id`, `role_arn` and
`region`, as for installed recovery routes. Use separate broker connections and
roles for reads and deletion, and separate APIs and roles across Products. The
selected account must match that Product's `account_ref`. Set its `owned_assets`
and `retained_assets` to exact `api-id:route-id` references. Omit `assets` from
`readback_channels` and `cleanup_owned` from `action_channels`.

This adapter reads real HTTP API tags, route definitions and every serving
stage's deployed export. It requires Product tenant/project tags and the current
qualification-run tag on the API, then records a private baseline before any
deletion. Cleanup rechecks ownership and exact route contents, deletes only the
listed owned routes, and uses the independent reader to verify all other routes
and serving-stage definitions are preserved. A lost response or inconsistent
readback retains an uncertain cleanup receipt; it is not reported as deletion
success. Existing assets must already be provisioned and tagged by their owner;
the selector does not create an API or grant permissions.

Route calls share the selector's existing 100-request maximum. The adapter reserves
cleanup capacity for both Products before other actions, including the caller's
owned/retained checks and final cross-Product retained reads. Each read allocation
is released only after its corresponding read completes; final-read capacity
stays reserved until both Products have completed cleanup. The reserve increases
when observations show a larger route/stage inventory. An allocation that cannot
cover the selected APIs is rejected rather than increasing the limit. Offline
tests cover both Products and a selector execution with other channels mocked;
they do not establish complete installed-only qualification. This option supports
only isolated HTTP API routes, not arbitrary native assets or Context deletion.

For admitted investigations, set `portability.installed_native` to `true` and
omit `native` from `readback_channels`. This option uses the two existing
`installed_operations` and `installed_access` selections: it reads each original
durable run with the independent Product reader credential, checks its input,
binding generation, connection and native identity, and polls that same run
without submitting new work. It rechecks the ready binding before provider
access and again after collection. Through the selected broker role it reads
`GetBacklogTask`, then retrieves the matching scoped result and digest-verified
evidence artifact from the Product API. This supports investigations only;
release-readiness and release-verification result readers remain separate work.

A completed read needs at least ten requests, including identity verification,
the independent native task read and final authority checks. Polling and result
pagination consume additional requests within the existing operation and selector
limits, without spending reserved cleanup capacity. The adapter records native
task duration and explicitly unavailable task cost; it does not attribute monthly
account usage to an investigation. Task identity/state and a retained result do
not establish source grounding or deployment identity. Both remain unavailable
in this observation, so it cannot pass full portability acceptance on its own.
The final oracle still rejects missing source/deployment grounding rather than
copying these values from the private fixture. Retained journal and recommendation
contents are Product artifact readbacks, not independent provider-result reads.

The private `portability` selection sets `authorization.scenarios` to include
`portability`, bounds the overall request/deadline and a 10–60 second cleanup
grace. The remaining legacy protocol specifies five HTTPS `action_channels`: `configure`,
`run_operation`, `run_context`, `probe_foreign` and `cleanup_owned`. Nine
independently credentialed `readback_channels` provide `platform`, `product`,
`assets`, `configuration`, `access`, `native`, `context`, `foreign_denial` and
`usage`. Keep these authorities separate from all action and release readers.
Apart from the installed `access`, `run_operation`, `probe_foreign`, `configure`,
`configuration`, `assets`, `cleanup_owned` and investigation `native` adapters above, these are
unsupported custom interface assumptions; the complete selector still needs installed adapters.
Do not provision a test service merely to satisfy this selector. Unavailable
Context/Insights integration remains a prerequisite, not a native failure or
successful reuse.

```bash
python3 modules/L5-services/scripts/qualify-devops.py portability \
  "$REVIEWED_SOURCE_SHA" "$PRIVATE_DEVOPS_MANIFEST" "$PRIVATE_DEVOPS_EVIDENCE_DIR"
```

The selector verifies Product registry identity before changing configuration,
then reads the configured scope, selected account/role, actual native result,
later cited Context result, foreign denial, usage and cleanup. It compares the
platform source/deployment/artifact identity and build ID throughout both
projects. It journals side effects and leaves uncertain attempts private for
reconciliation; only independently owned assets can be removed. A passing
synthetic test does not prove real non-ADP portability, native execution or
selected cross-account permissions. DA19 must retain the release through
repair and check all four AC/SCN pairs on the authorized deployed target.

## Native qualification and delivery evidence

Once the installed selectors and independent readers are available, the
protected `L5 release and live qualification` workflow supplies its real
`GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT` and `DEFINITION_SHA` and writes a private
`independent-observations.json` alongside the five private selector summaries.
The owner supplies the published release identity and private evidence
directory; no observation may be reconstructed from selector counts. From the
reviewed checkout, with the existing L5 build tools installed, the workflow
runs:

```bash
python3 modules/L5-services/scripts/qualification-rollup.py \
  --service devops \
  --release "$PRIVATE_RELEASE_IDENTITY" --evidence "$PRIVATE_DEVOPS_EVIDENCE_DIR" \
  --observations "$PRIVATE_DEVOPS_EVIDENCE_DIR/independent-observations.json" \
  --output "$PRIVATE_DEVOPS_EVIDENCE_DIR/l5-devops-qualification/qualification.json" \
  --delivery-evidence "$PRIVATE_DEVOPS_EVIDENCE_DIR/delivery-evidence/delivery-evidence.json"
```

`qualification.json` uses the native evaluator's `schema_version: 1` contract:
the accepted ten `epic_criteria`, 63 `story_criteria`, 63 `scenarios`, both R3
`live_checks`, four `portability.assertions`, `cleanup.outcome`, `unresolved`,
and the source/definition/workflow/run binding. The separate
`delivery-evidence.json` uses `schema_version: 2` and references the native
qualification artifact. Both files are created mode 0600; private target and
readback identities in them are hashed, not exposed as raw URLs or account IDs.
Absent standalone `correlation_id` and `contract_digest` values are JSON `null`,
matching the canonical publisher. Populated engine identifiers remain exact;
reports from another engine dispatch are rejected rather than normalized away.

The independent observation bundle must contain `schema_version: 1`, the
actual workflow `run_id` and `run_attempt`, reviewed `source_revision` and
`gateway_revision`, the selector `qualification_run_id`, an independent
`reader` identity/revision and selected `target` connection/resource identity.
Each required record has its own `id`, `outcome`, `evidence_verified`,
`requirement_revision`, `source_revision`, `runtime_revision` (the published
deployment ID), `run_id`, `run_attempt`, `verification_layer`, private
`evidence_ref` and aware `observed_at`; the two SSO records additionally need
`observation: independent-live`. The portability assertion IDs are
`two-non-adp-products`, `non-eks-workload`, `cross-account-role` and
`retained-assets-unchanged`. Cleanup needs an independent readback, passing
outcome and aware observation time. Only an authorized independent reader may
attest these observations; copying a selector's success flag is not verification.

The rollup writes unresolved, unverified records and exits nonzero on missing,
stale, wrong-source/run or mismatched release evidence; `--report-only` keeps
those records unresolved while allowing private inspection. The two authorized
users, two non-ADP Products, non-EKS and cross-account scope, Context reuse,
owned cleanup, real provider state and live SSO denial must still be observed
on the selected release. The coordinator supplies authorized private Product
references and a second real session; none are inferred from these offline
examples. Publication of a PR or package is not installation or acceptance.

### Durable result delivery prerequisites

The worker commits a `devops_result` contribution for Context and Insights with
its normalized result. A contribution retains the selected findings, pinned
sources, original native reference, evidence references, immutable digest and
stable result revision. A source-free or ungrounded observation remains
`unavailable` with `source_evidence_unavailable`; it cannot satisfy Context reuse.

The installed worker loads each destination only from an exact qualified
`L5_CONSUMER_CONTRACTS` entry. Missing configuration disables publication without
disabling native work. The repair worker reconciles durable receipts before
sending a contribution, verifies the consumer projection, and preserves the
original result and publication identities on restart. Configure the contract
only after the actual consumer's access fencing and durable state are verified.

An authorized human can read a published producer contribution through
`GET /v1/products/{product_id}/projects/{project_id}/services/devops/results/{result_id}/devops-contributions/{destination}`.
This endpoint verifies the original result and digest, current Product/source
access and delegation, and the retained consumer receipt. The producer readback
alone does not prove a later agent used the contribution or that native UI SSO
passed. Those remain separate live acceptance observations.

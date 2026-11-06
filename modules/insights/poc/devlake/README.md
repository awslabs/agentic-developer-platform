# Optional DevLake artifact finalization

## Offline isolation selector

Run `python3 modules/insights/poc/devlake/scripts/isolation_scenario.py --scenario test_isolation`
from the repository root. Its checked-in fixture pins the current schema manifest
and selects scoped cell-role, wrong-target and offline certificate checks plus the
mock-provider collection Terraform plans. The selector refuses changed pins or a
missing TLS tool; it never connects to PostgreSQL, S3 or a deployed workload.
Its JSON result explicitly marks actual SQL/storage denials and libpq TLS denials
as `not_run`. These local checks do not satisfy the deployed isolation scenario:
an authorized operator must separately use real scoped identities and a permitted
database/storage target before claiming effective isolation.

## Offline restart selector

Run `python3 modules/insights/poc/devlake/scripts/restart_scenario.py --scenario test_restart`
from the repository root. The fixture pins the optional-intent ledger and
artifact-finalizer source digests and exact local restart checks. It tests
identical retry, conflicting or lost ledger updates, abort/expiry fences and
interrupted **artifact** cleanup. Its result reports optional-resource cleanup
as `not_implemented` and state/key loss and live restart as `not_run`.
It performs no cloud cleanup and cannot prove removal or retained database
state; those require qualified optional-resource finalization and a permitted
live operator exercise.

## Offline IAM renewal selector

Run `python3 modules/insights/poc/devlake/scripts/iam_renewal_scenario.py --scenario test_iam_renewal`
from the repository root. Its fixture pins the receipt-reader and migration
client sources, exact unit tests, two physical connections and a controlled
16-minute interval. The checks verify fresh IAM tokens and verified TLS for
local mocked `psql` processes; they never connect to a database. The result
reports the worker database client as `not_implemented` and expired static-token
server rejection and live aged backend connections as `not_run`. Those checks
require an integrated worker and an authorized database target and operator;
this offline result is not deployed IAM-renewal acceptance.

## Offline optional-mode selector

Run `python3 modules/insights/poc/devlake/scripts/optional_mode_scenario.py --scenario test_optional_deployment_modes`
from the repository root. Its fixture pins absent, connector-only and full
flag selections; reviewed source and test revisions; and the artifact
finalizer's one-hour poll and two-hour deadline. Ten local checks exercise
invalid selections, ordinary deployment independence, registered retained
dependencies, terminal intent refusal and expiry/deadline handling for image
artifacts. No workload is drained or optional resource removed. The JSON result
marks worker draining and optional-resource cleanup as `not_implemented`,
gateway/broker admission as `not_integrated`, and retained shared data and live
transitions as unverified. An authorized runtime campaign is still required.

The POC publisher accepts only a pre-registered, exact image intent in a protected
DynamoDB ledger outside the removable cell. Neither pull requests nor ordinary
ADP deployments create this ledger, select a module, or publish images. The
scheduled and manually dispatched finalizer workflow records independent recent
probes. Publication and its checks refuse to proceed without both probes and a
matching digest, repository and pinned finalizer revision. A probe tests scoped
ECR read access and simulates the cleanup policy; it does **not** perform or
prove a live deletion. Artifact retirement and independently verified cleanup
receipts are separate work; do not treat a probe as live acceptance.

Before creating any POC-owned resource, an authorized operator must establish a
private, non-cell DynamoDB table with `pilot_id` as its sole string partition
key, active server-side encryption and point-in-time recovery. Its IAM policy
must allow only the chosen finalizer role to write pilot records and the chosen
publisher role to read them. The finalizer role additionally needs scoped ECR
read and image-deletion authority and IAM policy-simulation permission. Restrict
both GitHub environments to the default branch; allow the scheduled workflow to
run without a human approval wait, while preserving the separate manual path.
Provisioning these resources is an operator action, not a prerequisite for
running offline tests. Never commit the real registration or workflow bindings.

The operator configures `ADP_DEVLAKE_PILOT_ID`, `ADP_DEVLAKE_LEDGER_TABLE`,
`ADP_DEVLAKE_ACCOUNT`, `ADP_DEVLAKE_REGION`, `ADP_DEVLAKE_FINALIZE_ROLE`,
`ADP_DEVLAKE_PUBLISH_ROLE` and `ADP_DEVLAKE_FINALIZER_REVISION` in the protected
workflow environments. The finalizer checkout is pinned to that exact approved
commit so expiry cleanup can continue after the default branch advances. Configure
`ADP_DEVLAKE_CONNECTION_LABEL` with the selected publisher connection in
`adp-devlake-publisher` and with the selected finalizer connection (from
`ADP_DEVLAKE_FINALIZE_CONNECTION_LABEL`) in `adp-devlake-finalizer`. Set these
same environment variables for the private operator registration command.
Credentials must come from the selected `adp-cred` broker connection; ambient
AWS credentials are not used. The broker must return the exact account and role.

For example, the **private** registration JSON for one component has this
shape (replace all fictional values, revision, digest and future expiry after
building the pinned source; the pilot ID is `devlake-` plus the pinned revision):

```json
{
  "pilot_id": "devlake-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "account": "123456789012", "region": "us-east-1",
  "operator": "example-operator", "finalizer_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expires_at": "2030-01-01T00:00:00Z", "poll_seconds": 3600,
  "deadline_seconds": 7200, "probes": {},
  "artifacts": {
    "api": {
      "repository_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/devlake-poc/api",
      "digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "tag": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-api", "state": "registered"
    }
  }
}
```

From a private operator workstation checked out at the pinned revision, with
configured connection and ledger variables, run
`python3 modules/insights/poc/devlake/finalize.py --register /private/pilot.json`.
Registration is conditional: it refuses an existing pilot, forged probes, or
an ECR tag already present from a previous accepted publication.
The operator then dispatches `Insights POC artifact finalizer` manually and
verifies the hourly scheduled run on the default branch before selecting manual
publication. Check the ledger for both current probe records and the exact
resource intent. A new revision needs a newly registered pilot and both probes.
If either job, role, policy simulation, ledger protection, expiry or digest
check fails, no publication receipt is issued. Live registry readback and actual
cleanup authority still require the authorized deployment owner to verify them.

The same protected workflow accepts a manual `abort` or `failure` action by the
registered operator; its hourly schedule requests finalization on expiry, even
when delivery stopped. A manual `hold` names only a component already in the
ledger and requires a successful image readback before recording operator
approval. Terminal requests fence new publication checks. The worker acquires
an expiring per-image lease, tries each owned tag at most three times, and
records a private absence or retention receipt only after separate ECR
`batch-get-image` and `list-images` checks. A lost deletion response can be
reconciled on retry; a wrong digest, held image loss, authority loss or exhausted
attempts stays incomplete and requires operator investigation. A missed
polling or terminal-cleanup deadline is recorded and escalates to the registered
operator; a manual resume does not reset the attempt limit. If the protected
publisher job fails, a separate protected finalizer job requests failure
cleanup. The actor dispatching publication must be the registered operator;
otherwise that request is refused and the operator must resume it manually.
Old accepted
images with other tags and shared resources are never deletion targets. The
ledger retains the original request, attempt count and any remaining held
artifact cost; a completed receipt is not evidence of a live run unless its
record is independently checked under the authorized account and role.

## Shared CLI merge ordering

A POC PR that edits the shared CLI capability contract runs
`modules/insights/poc/devlake/scripts/verify-poc.sh` (or its `--preflight-only`
entry point in `DevLake POC / verify`) against the PR's checked head. It must
find independently reviewed Security PR #9132 merged into the **current** main,
with successful `L5 contract and module tests`, `Lint` and `Test` check runs
from GitHub Actions and no other failed or unfinished predecessor checks. The
PR head must contain that merge and current main, and the checked-out tree must
be the same candidate. Unchanged PRs do not need this cross-flow check. An
unmerged predecessor, stale branch, missing review, skipped required check or
inaccessible GitHub evidence refuses verification; no build or publication
rights are inferred. PR validation only reads GitHub; the required-check list
is pinned to this reviewed predecessor contract rather than guessed from
unreadable branch-protection settings.

## Optional collection infrastructure (S02)

`infra/` is an independent, default-off Terraform root. Its selected state key is
`<environment>/modules/insights/devlake/cells/<cell>/terraform.tfstate`; it does not own the
shared PostgreSQL instance, the shared Evidence Lake or connector infrastructure.
The connector config's `enabled` boolean selects
`FEATURE_DEVLAKE_CONNECTORS_ENABLED`; the collection config's
`connectors_enabled` and `enabled` booleans select that master flag and
`DEVLAKE_COLLECTION_ENABLED`, respectively. With neither private config nor
named environment flag supplied, both selections default off.
When those named environment flags are supplied, only explicit case-insensitive
`true` or `false` (or an empty off value) is accepted; they must agree with the
private JSON booleans. Invalid values and collection without connectors refuse
before credential or Terraform work, even if the collection config selects off.
The connector Pod receives its master flag; the dormant collection Pod receives
both effective flags from its own Terraform variables. This does not yet wire
gateway or broker admission; that integration remains separate.
When selected, it requires the connector switch, a registered cell, an existing
RDS endpoint/resource ID, an operator-provided CA ConfigMap, an admitted workload
role boundary, the exact local schema-manifest digest and an immutable worker
image digest. Without those inputs it fails closed. This root creates the
cell-specific service account and network-isolated worker definition with **zero
replicas**: it neither migrates a schema nor starts collection. A migration receipt,
scoped SQL roles/permissions and gated runtime admission must be completed before
any worker is started. A rendered `verify-full` setting is not evidence of a
successful TLS handshake or a renewed IAM database connection.

Offline plan checks do not read the operator's target or create a database:

```bash
terraform -chdir=modules/insights/poc/devlake/infra init -backend=false
terraform -chdir=modules/insights/poc/devlake/infra test
```

The module-local `deploy.py` is a plan-first entry point. With no private
configuration, or an explicit JSON `enabled: false`, it exits without reading
credentials, initializing Terraform or creating resources. A selected plan or
apply requires a private operator configuration, checked account and exact
assumed deployment role, fresh IAM/`verify-full` SQL readback of the exact cell
and RDS resource ID, both checked S09 image receipts, registered finalizer
probes and independent registry digest readback. A saved plan must be reviewed
privately; apply requires its SHA-256 and matching pinned variables. Both
operations use the selected credential connection and a distinct collection
state key. No public document should contain the private configuration, plan,
image readbacks or actual target identity.

Before Terraform initializes, the installer conditionally records the exact
cell-owned IAM and Kubernetes names in the existing encrypted, backed-up S09
pilot ledger. It checks the saved intent by consistent readback and accepts a
retry only with the same image, schema, cell, resource names and state key.
Every cell has its own state key. The existing DB, CA ConfigMap and shared
state bucket are retained dependencies, not resources this module may delete.
Registration is not evidence that runtime cleanup authority is qualified;
resource creation remains gated on the independent finalizer work.

Connector-only installation uses `deploy_connectors.py` with a private JSON
configuration outside the checkout containing `enabled`, `environment`,
`aws_account_id`, `aws_region`, `deployment_label`, `deployment_role`,
`api_receipt`, `api_role_boundary_arn` and `provider_egress_cidrs`. The
connector entry point checks the selected deployment role and the API receipt,
then registers its exact resource intent before planning. It needs neither a
database nor a worker receipt. Both installers accept `--config` and
`--plan-file` to make a private plan, or `--apply-reviewed-plan` and
`--expected-plan-sha256 <reviewed-digest>` to apply one. An operator must set
`ADP_DEVLAKE_INTENT_CONNECTION_LABEL` to the approved finalizer-role connection
for the protected ledger write; publication preflight still uses its separately
selected publisher connection. Missing finalizer write authority fails before
Terraform starts. These commands do not establish a deployed installation.

The current S09 finalizer can independently retire registered ECR tags but
cannot yet delete or verify IAM and Kubernetes resources owned by an optional
module. An apply therefore refuses with `optional_cleanup_required` before
Terraform initializes. For a registered optional intent, abort or scheduled
expiry persists the terminal request (fencing new admission), then refuses to
retire even its image or record completion until a qualified optional-resource
finalizer exists. This deliberately retains the image and reports an explicit
cleanup gap; registration alone is not cleanup proof. Turning a selection off
does not yet drain a running worker or authorize resource removal. The S03 job
entry point and S12 lease/generation revocation contract must be wired before
that disable path can be claimed safe.

The private collection configuration names three distinct, pre-registered
PostgreSQL users: `migration_username`, `writer_db_username` and
`reader_db_username`. The worker role gets only `rds-db:connect` for the selected
resource ID and writer username; no storage or database-password grant is
installed. In the migration transaction, the migration owner verifies the
cell database, IAM-only nonprivileged users, owned tables, generation guards
and matching schema receipt before granting writer DML and reader SELECT on
only the pinned tables. Reader/exporter workload IAM deployment remains a
separate integration step; these local checks are not evidence of live grants.

Current POC database-client inventory: the migration transaction and schema
receipt reader each start a new `psql` process and request a fresh IAM token
immediately beforehand. The packaged collector accepts a gateway-admitted job
and uses renewable IAM writer/reader pools; see the
[worker contract](../../../../docs/engineering-insights/devlake-poc/collector-worker.md).
The collection Deployment still has zero replicas. No exporter or Grafana database client is installed by
these optional roots. A controlled-clock fixture checks distinct tokens on
new receipt-reader connections more than fifteen minutes apart, but cannot
prove a running worker or exporter renews credentials or a real database
rejects an expired static token. Those integrations and live checks remain
open; do not configure a static password or token as their fallback.

The current schema-v2 manifest inventories migration-owned staging tables and
generation guards. Collection requires an exact receipt for the registered
cell/resource and current image manifest. Local PostgreSQL contract tests verify
DML-only writes and immutable sealing, not deployed privileges or source access.
S01 target authorization remains a prerequisite for live work. A zero-replica Pod
definition is not a running collector.

## Scoped observation export (S04)

New source-batch manifests include `SourceBindings`, a snapshot of each source's
binding ID, generation and revision. Sealing checks that snapshot against current
collection authority, and the manifest digest protects it on readback. Export
uses the sealed snapshot for historical provenance while checking current export
grants separately. Rotating a binding therefore does not rewrite an observation.
Legacy manifests without a binding snapshot remain readable for collection, but
observation export rejects them rather than inventing historical provenance.

Authorized page limits cover the complete JSON response, including policy
descriptors. A smaller fitting prefix retains manifest order and a cursor for the
last returned item. A verified empty batch completes with empty items and policies;
it still requires source coverage, binding snapshots and current export authority.
These source-side contracts do not provide durable Insights admission or replay
checkpoints.

## Offline evidence-chain selector (S05)

`evidence_chain.py` prepares exact v2 Story, criterion and test snapshots, then
projects one Story/criterion/run through `read_evidence_chain`. Its reader callback
must supply captures from a currently authorized admission/read path. The required
keyword argument `resolve_reference` must be that reader's trusted policy resolver,
not a function or label supplied by the evidence producer. It rechecks every record,
snapshot, tested artifact and dev receipt dependency for the current reader. The
report includes those resolved dependencies and refuses shared output for private
dependencies; unresolved policies and mixed realms are rejected for either audience.
The offline fixtures use synthetic callbacks instead. Captures and a matching supplied dev
receipt are **not** deployed or independently read-back evidence. Missing selected
results stay visible, unknown selection stays unknown, and unknown requirement
bindings retain their attempts with an explicit gap instead of confirmed delivery
links. Actual artifact or code mismatches still fail. A recorded assessment
applies only to its exact criterion revision. The module does not publish a query
service, infer acceptance from a passing test, or grant private evidence to a
shared reader.

After installing `modules/insights/schemas/requirements.txt` into a local Python
environment, run the bounded offline tests from the repository root with:

```bash
PYTHONPATH=modules/insights/schemas:. python3 -m unittest modules.insights.tests.devlake_poc.test_s05
```

The existing S04 export is source-side only. Durable Insights admission, authorized
readback, controlled CI and dev receipts, and a named assessor are still required
before a live S05 evidence chain can be evaluated.

For a GitHub-backed controlled Story, `map_github_requirement_revision` takes
source-owned, immutable issue JSON bytes, an exact snapshot reference and an
explicit event identity. It maps the named criterion from that revision's
validation table, rejecting absent or ambiguous rows. It does not fetch the
source, reconstruct older edits, or establish that GitHub is the S01-selected
tracker; that source binding and independently retained snapshots remain required.

`map_native_ci_run` takes a bounded source-owned run snapshot, the exact
selection-manifest bytes and individual attempt receipts. It emits v2 test
catalog, selected-instance and attempt records through the same pre-admission
policy resolver. A completed run without per-test receipts does not create
passing test results; missing selection or requirements evidence keeps
completeness unknown. Selection snapshot bytes and immutable references are checked
even when the manifest has no instances. Each selected native `test` must carry
its own retained revision provenance (`source_event_id`, `event_time`, `ingested_at`)
alongside its ID, revision and definition fields. Reuse that provenance across CI
reruns; the current run event belongs only to the run, selected instances and results,
so identical definition revisions produce identical normalized record bytes.
Real CI source authority, independently retained receipt
versions and the S04 durable sink are still required for live acceptance.

## Work-and-cost publication scope (S06)

`dataset.py` selects one logical work/charge revision at the requested cutoff.
Because superseded revisions are not retained in that candidate, the snapshot
records `selection_as_of`, bound to its qualified policy by the snapshot digest.
The API accepts only that instant for such a snapshot (equivalent timezone
offsets are accepted). Other cutoffs return HTTP 409 `projection_incomplete`,
not complete zero totals. Reading an older cutoff requires a snapshot built for
that cutoff; this publisher does not add automatic historical lookup. Legacy
snapshots without this selection marker keep the existing historical reader behavior.

`results_publisher.py` conditionally creates
`results/v1/{tenant_id}/{project_id}/datasets/{dataset_version}/manifest.json`
to bind each version permanently to its snapshot, policy and quality digests.
Publication and pending recovery verify this binding even after the current
pointer has moved to another version. Exact replay is allowed; changed content
under an existing version is rejected without moving the pointer. A version
reserved before an interruption must be retried with identical inputs. These
objects use the existing qualified result prefix; this code grants no storage
permissions or deployment authority.

## S07 offline operating ledger

`campaign.py` evaluates **supplied** observations; it does not collect from a
provider, query the served route, grant access, or prove live acceptance. Keep
input, checkpoint and full report files in a private operator-owned directory
outside the repository. Each input must be independently recorded and reviewed:

- `--frozen`: JSON containing a `sample_revision` and exactly 20 `updates` with
  distinct `update_id`, `provider`, `revision` and native `source_at` (timezone-aware
  ISO-8601). The two-provider sample and native event times must be frozen before
  measurement. Do not change it mid-run.
- `--batch`: JSON containing a distinct `batch_id` and `reads` with matching
  `update_id`, `revision`, `authorized: true`, `read_at`, `observer` and a private
  `evidence_ref` for each independently observed first authorized served read.
  Optional `measurements` include `query_seconds`, `jobs` (per-job `vcpu` and
  `memory_mib`), `storage_gib_per_cell`, and cumulative `cost_usd` with
  `estimate`, `billed` and `basis`. A missing measurement stays unavailable.
- `--approved-limits`: JSON with `limits` matching the accepted S01 numeric
  contract and `approved: true` only after actual owner approval. Checked-in S01
  limits alone are **proposed**, not approved operating ceilings.

For each bounded observation window, supply the next private batch and reuse the
same checkpoint/report locations:

```sh
python3 modules/insights/poc/devlake/campaign.py \
  --frozen /private/operator/frozen.json --batch /private/operator/window.json \
  --approved-limits /private/operator/approved-limits.json \
  --checkpoint /private/operator/checkpoint.json --report /private/operator/report.json
```

The checkpoint and report are private mode-0600 files. Exact batch retries are
idempotent; changed samples, limits, conflicting retries and duplicate reads
are rejected. The command prints only offline status and batch count, not private
identifiers. Preserve the private evidence references and independent source/read
receipts separately: self-supplied JSON and passing local tests cannot establish
live freshness, cost, tenant isolation or recovery.

To include AC-02 recovery in that same private campaign, freeze a nonempty
`required_db_clients` list in `--frozen` containing **every** deployed DB client.
Each `--batch` can carry a `recovery` map with distinct case keys `b1`,
`source_outage`, `revocation`, `db_clients`, `modes`, `profile` and
`finalization`. A case is a record with `attempt_id`, `observer`, private
`evidence_ref` and `data` matching the recovery verifier's observations.
For finalization, each `failed`, `aborted` and `expired` attempt also needs its
own `attempt_id` and private `evidence_ref`. Cases can arrive in separate
windows, but a conflicting attempt for an already recorded case is refused.
The report retains case statuses and receipt references. If client inventory
is not frozen, recovery is explicitly `not_run`; if it is frozen but any
required observation is missing, recovery is `unavailable` rather than passed.
This is an offline check of supplied receipts, not evidence of actual faults,
read denials, IAM renewal or finalizer execution.

For AC-03, pin `upgrade` in the frozen manifest with baseline and candidate
upstream revisions and image digests, plus schema-manifest, requirements and
connector-contract SHA-256 digests. The candidate upstream and image must differ.
Submit one `upgrade` receipt in a batch with `attempt_id`, `observer`, private
`evidence_ref` and `data`. The data records matching revision `pins`, per-patch
`patch_id`, reuse/rebase/conflict status, measured `effort_minutes` and conflict
resolution. It also includes `negative_admission` cases for missing/incompatible
schema, missing guard and incompatible connector, all with independent
`migration_required` readbacks before provider calls or writes. Record migrator
receipt and first/new-scope DML, worker CREATE/ALTER/DROP denials, and a rollback
receipt with the restored prior image, Profile/retained-evidence/shared-state
comparisons and a private `evidence_ref`. Absent proof remains `unavailable`.
A local receipt report cannot prove that an upstream image was actually deployed,
that migration-role permissions were effective, or that rollback was exercised.

For AC-04, freeze an `isolation` object containing `collision_id`, policy
`descriptor_sha256` and at least four `scopes`. Each scope binds a distinct
`scope_key`, `cell_id`, `tenant_id`, `project_id`, `realm_id`,
`source_connection_id` and shared/private `visibility`. Include two tenants,
two same-tenant Projects, and a shared/private pair in one Project. Supply
one private `isolation` receipt with `attempt_id`, `observer`, `evidence_ref`
and `data` recording six-field routed identities, concurrent collection,
forged/foreign/private/stale-policy negatives, current policy graph and a
fixed revocation barrier. Record denied DB/S3/KMS workload-role probes and
independently read shared response bytes as base64 plus status, pointer,
coverage and freshness before and after rejected private publication. A
missing storage denial remains unavailable; a foreign write, leaked byte or
changed shared view fails the report. None of these supplied observations
proves actual identity, storage denial or policy enforcement without a
separate authorized live run using real workload roles and storage oracles.

## S07 bounded freshness windows

`resume_campaign.py` is the **offline SCN-01 selector**. Its opt-in Actions
wrapper has twelve sequential, four-hour measurement jobs (each capped at 300
minutes) and a short read-back job. Each job receives a fresh workflow identity
and consumes one `window-00.json` through `window-11.json` from the **same**
operator-owned private directory; a missing predecessor, batch or update blocks
the result. It never invents a native event or provisions a validation target.
Before a live dispatch, the verification owner must qualify an existing runner
pool with a persistent mode-0700 shared private mount, the protected
`adp-devlake-campaign` environment and its `ADP_DEVLAKE_CAMPAIGN_ROOT` secret.
These resources and their permissions have **not** been verified or created by
this code. Configure them privately; never put their values in the repository.

A separate `devlake-poc-provider-profile.yml` source workflow can measure the
existing GitHub and Jira provider objects and the served Product Profile and
source bindings without creating resources. Its protected environment secret
`ADP_DEVLAKE_PROBE_CONFIG` must point to an absolute, owner-owned mode-0600
JSON file on that runner. The file supplies `source_revision`, `allowed_hosts`
(an explicit list of approved HTTPS hostnames), `providers.github` and
`providers.jira` (each with `url`, `native_id` and a private `authorization`
header), plus `gateway` (`profile_url`, `bindings_url`, `authorization`,
`product_id`, `project_id`). The dispatch input `launch_manifest` points to
the private mode-0600 manifest with the same selected source revision. The
source job checks the two upstream IDs against the scoped binding and Profile
readbacks and uploads only digests and an observation timestamp; missing
approved configuration, an endpoint, a required source or a matching binding fails the job.
Its `observation.json` is an intermediate source measurement, **not** a
criterion pass receipt or live pilot acceptance. Full scenario producers must
still collect and validate each acceptance criterion before the pilot reader
can accept a summary. No source targets or credentials are supplied here.

Intermediate provider/Profile, native-readback and active infrastructure
measurements are retained through the existing private S3 CI-evidence action
under the runner's scoped identity, with seven-day retention. These diagnostic
objects do not supply GitHub artifact IDs for criterion or native pass receipts.
Only three runtime handoffs retain GitHub artifact transport: finalized
infrastructure `observation.json`, read by `source_reader.approved_reader` for
`delivery_cleanup`; pilot `summary.json`, read by the repository evaluator's
accepted `pilot.workflow` contract; and `delivery.json`, read by
`ReleaseProvider` under the accepted generic delivery contract. Their exact
names, run/attempt bindings, seven-day retention and authenticated readers remain
unchanged. Migrate these handoffs only together with their readers and contracts.

`devlake-poc-infrastructure-source.yml` has two separate protected dispatches:
`active` reads the selected IAM policy simulation, the live RDS schema receipt
through a fresh IAM token and verified TLS, and the versioned, published S3
snapshot, policy and quality lineage; `finalized` reads the protected finalizer
ledger and independently checks registered ECR image absence or approved holds.
The former uses `adp-devlake-campaign`; the latter uses the existing
`adp-devlake-finalizer` environment and its selected finalizer connection.
Each environment must privately provide `ADP_DEVLAKE_SOURCE_CONFIG` as a path
to an owner-owned mode-0600 JSON file. It includes `source_revision`; for
`active`, `database` names the scoped host, database, cell, resource ID,
username, CA path, region, account, exact role and broker label. `iam` names
the same account, region and resource ID plus the observer role, broker label,
worker role, allowed writer and forbidden username. `lineage` names the
selected account, exact role and broker label, region, bucket, tenant, project,
dataset version, and exact pointer/snapshot/policy/quality object version IDs.
The `finalized` configuration needs the source revision and the pre-existing
finalizer environment variables and connection; it refuses unqualified optional
resources. Neither policy simulation nor registered-image readback proves an
actual database denial, IAM renewal by all deployed clients, optional-resource
cleanup or complete pilot acceptance. The workflow uploads source measurements,
not criterion verdicts; absent sources or incorrect scope fail before upload.


The protected pilot qualification needs a separate, approved `delivery_cleanup`
source binding for the finalized infrastructure workflow (`observe` job,
`devlake-infrastructure-finalized-{run_id}-{run_attempt}` artifact). The private
campaign manifest must pin its actual artifact ID and SHA-256 under
`delivery_cleanup_receipt`. Dispatch the finalized source under its authorized
environment after finalization; a missing source, an incomplete ledger, optional
resources without qualified cleanup, or different registered-image readback
prevents the generic delivery receipt. The protected campaign environment also
requires `ADP_DEVLAKE_DELIVERY_TARGET`, a private JSON object with `account_id`,
`region`, `resource_kind` (`engineering-poc`), `resource_id` (the registered
pilot ID), and `component` (`devlake-poc`); its identity must match the finalizer
readback and the coordinator's accepted delivery contract. The engine supplies
`adp_correlation`, `adp_source_revision`, `adp_definition_revision`, and
`adp_contract_digest` at dispatch. Only after the pilot summary and independent
finalization readback pass does qualification upload the separate
`devlake-pilot-delivery-{run_attempt}` artifact containing `delivery.json`.
This registered-artifact check does not replace independent optional-resource or
off-state acceptance; missing evidence remains failed.

The root contains a safe campaign-ID directory with mode-0600 `launch.json`,
`frozen.json`, `approved-limits.json` and independently supplied numbered
window files. Launch pins the ID and reviewed code revision, SHA-256 digests of
the frozen sample and approved limits, a UTC `planned_start_at`, and private
`source_consent_ref`, `runner_capacity_ref`, `authority_ref` and
`engine_qualification_ref`. Its `engine_until`, `workflow_until`,
`authority_until` and `expires_at` must all cover at least 56 hours after the
planned start. The script checks structural bindings and expiration, **not**
the actual runner capacity, owner consent, access or engine authority behind
those references. An operator must check those independently before launch.
Each window ends at its real four-hour boundary and permits at most 20 minutes
for a pre-collected, independently observed batch; the final native update may
fall at the 48-hour boundary. The final read-back refuses fewer than twelve
checkpointed windows, missing updates, unapproved bounds or freshness p95 over
15 minutes. It emits no acceptance artifact and cannot replace an authorized
live soak or the other integrated scenarios.

The **offline SCN-02 selector** reads recovery receipts from the same private
checkpoint. Once independently supplied outage, B1/B2, revocation, IAM-client,
optional-mode, Profile and off-state finalization observations have been
checkpointed, run `resume_campaign.py --root <private-absolute-root>
--campaign-id <approved-id> --revision <pinned-revision> --recovery` in the
authorized operator environment. It requires the frozen DB client inventory,
all seven distinct cases and three terminal attempts, checks launch pins and
read-back consistency, and rejects any failed or missing recovery check. It
can pass recovery before the 48-hour freshness sample completes; that result
does **not** qualify freshness or the overall pilot. A successful offline
receipt evaluation does not prove a live fault injection or authorize one.

The **offline SCN-03 selector** reads the pinned upgrade receipt from that
private checkpoint: run `resume_campaign.py --root <private-absolute-root>
--campaign-id <approved-id> --revision <pinned-revision> --compatibility` in
the authorized operator environment. It compares the baseline and candidate
upstream, image, schema and connector pins; requires migration-required
denials before provider work, DML migration, worker DDL denials and a compatible
prior-image rollback. Missing or failed observations, changed launch pins and
inconsistent report receipts are refused. Passing supplied receipts before the
48-hour soak does not qualify the whole pilot or prove a live upgrade/rollback.

The **offline SCN-04 selector** reads the isolation receipt from that private
checkpoint: run `resume_campaign.py --root <private-absolute-root>
--campaign-id <approved-id> --revision <pinned-revision> --isolation` in the
authorized operator environment. It requires frozen tenant/Project/visibility
scopes and checks colliding routed identities, concurrent collection, forged
or foreign scope denials, the current policy and revocation barrier, denied
DB/S3/KMS probes and unchanged shared response bytes following rejected
private publication. It refuses missing denials, leaked work, changed scope
pins or a report that disagrees with the receipt. An offline pass can precede
completion of the freshness soak and is not live proof of real identity,
storage enforcement or an authorized revocation under load.

For unattended continuation (AC-02), add optional `continuation` maps to
successive private window batches. The seven keys are `worker_restart`,
`workflow_loss`, `queued_ci`, `token_refresh`, `checkpoint_recovery`,
`mediated_repair`, and `automatic_successor`. Each value is a receipt with
`attempt_id`, `observer`, `evidence_ref`, and `data`. Every `data` object binds
`campaign_id`, `revision`, `correlation_id`, `operation_id`, `workflow_id`,
`frozen_sha256`, `limits_sha256` and an observed ISO-8601 `observed_at`.
The restart, loss and recovery receipts additionally name an already recorded
`checkpoint_batch_id` and its `checkpoint_batch_sha256` from the protected
checkpoint. Supply independent references for repair, queued CI, verified
merge, component release, independent read-back and automatic successor.
Run `resume_campaign.py --root <private-absolute-root> --campaign-id <approved-id>
--revision <pinned-revision> --unattended` for private read-back. Missing steps
stay incomplete; incompatible pins or references are rejected. This selector
checks supplied receipts only; it neither performs nor proves a live repair,
workflow retry, merge, release or successor.

The public `campaign.schema.json` defines the shape of privately stored launch,
frozen sample, approved limits, observation batch, checkpoint and report
documents. Install the existing Insights JSON Schema dependency with
`python3 -m pip install -r modules/insights/schemas/requirements.txt` before
using the local campaign CLI. The qualification workflows install it in each
short-lived job. Both direct batch recording and protected-file read-back
validate shape and timestamps before trusting evidence or writing checkpoints;
invalid inputs produce only a document-kind error, not private contents. The
schema fixes the 20-update sample and disallows freshness limits above 15
minutes; semantic authorization and live provenance still require independent
oracles and the acceptance owner's approved run.

To index current AC-01 through AC-04 campaign results without publishing
private evidence, run `operating_report.py --root <private-absolute-root>
--campaign-id <approved-id> --revision <pinned-revision>` on the owned mount.
It checks the pinned checkpoint against the report and writes a private,
mode-0600 `operating-report.json` with run/attempt/evidence references and
explicit missing live proof and cleanup. Even after an offline pass or launch
expiry, its live verdict stays incomplete. The public
`docs/engineering-insights/devlake-poc/operating-report.md` is a sanitized
pending-live template, not a copy of that private JSON.

Post-verdict issue closure is a separate operation. The orchestration actor
calls `complete_campaign.consume(run_id, protected_host)` **after** its native
final machine verdict, never from the workflow whose evidence produces that
verdict. The host must read the protected verdict and accepted issue bindings
and atomically compare each current requirement revision while closing through
its existing scoped issue integration. The consumer requires passing AC/SCN
results and identical issue revisions before any close, reconciles a lost
acknowledgement by checking the closure marker, and refuses foreign closures
or changed requirements. It intentionally has no CLI accepting a supplied
verdict and no unconditional GitHub issue-PATCH fallback. The tests use an
in-memory host: a qualified production host, its issue-write authority and
live post-verdict execution have **not** been verified by this PR.

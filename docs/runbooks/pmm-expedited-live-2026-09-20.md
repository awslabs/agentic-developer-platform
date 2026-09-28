# Expedited PMM rollout — dev, 2026-09-20

The operator requested PMM live without the seven-day soak on 2026-09-20.
Target: account `879318057152`, region `us-east-1`, AWS profile `embark1`.
This supersedes the elapsed-time prerequisite in the earlier dated PMM readiness
report for this dev rollout. Profiles remain deferred.

## Current release: saved persona mappings are live

The basic release resolves the initiating human's saved persona mapping once
before dispatch. Users can open Settings → Agent Models and save a compatible
model for any of the 14 personas. Direct/delegated, chat, AI-DLC and replan
adapters use the same preference. Worker-security migration, role retirement,
protected policy enforcement and profiles are separate work, not prerequisites.

Live settings: <https://d1g6cal2ts4iis.cloudfront.net/settings/agent-models>.
The authenticated feature endpoint reports `agent_models=true`; the settings
route and published JavaScript return 200. No visual browser check was available.

At 15:24 UTC, a Cognito-authenticated user saved
`intent-refinement → global.anthropic.claude-haiku-4-5-20251001-v1:0` and invoked
that persona through the real WebSocket ingress. The worker used saved Haiku,
returned `PMM_LIVE_OK`, and emitted `RUN_FINISHED`. Gateway usage records confirm
two HTTP 200 provider calls under invocation
`0ed70fc1-918b-4cea-91fc-fb5969549721`, both to Haiku in account `879318057152`.
Their settled costs were $0.000513 and $0.028871, totaling **$0.029384**.
Direct/delegation and AI-DLC/replan paths passed automated adapter tests; they
were not each separately invoked live. Customer-linked accounts remain outside
this acceptance scope. No seven-day soak is claimed.

The user authorized deployment and a $10 total model-test ceiling. Earlier
qualification work retains a conservative $4 reservation, distinct from actual
spend. Including this acceptance receipt, $4.029384 is reserved/accounted for.
Recurring model probes remain suspended and admission disabled; this release
does not require daily paid qualification to save or resolve a preference.

Deployment evidence:

- PR #5584 contains the implementation and this release record.
- Gateway/frontend workflow `35518132421` succeeded from `54037213846dea7db40be8626cfca54dc3ea4fe5`.
- Worker build `35518134290` and chat build `35518258001` succeeded. Both
  ScaledJobs use immutable image digests and gradual rollout; unrelated Jobs
  were preserved.
- Webhook, ingest, tick and gateway have `PERSONA_MODEL_MAPPING_ENABLED=true`.
  Producer lookup authenticates STS proofs bound to exact request bytes and
  allows only the webhook/ingest roles. Existing gateway identity, destination
  and budget enforcement continue to apply.
- Chat run registration now discovers the existing event table/KMS wiring,
  normalizes authenticated humans, and registers its actual IRSA role as
  `chat-worker`. Failed dispatch sends an explicit WebSocket failure response.
- SSM `/adp/dev/gateway/feature-agent-models=true` persists UI activation across
  gateway redeploys. The gateway rollout completed with three ready replicas.
- `AGENT_AUTHORITY_ENABLED=false`, `ADP_CHAT_MODEL_POLICY_ENABLED=false` and
  protected PMM posture `report_only` remain. The broad webhook infrastructure
  deployment hold remains for the separate worker-security migration.

Validation: 341 PMM tests passed with four environment-dependent skips; focused
chat identity/refusal tests (40), direct/delegated producer tests (32), native
Codex tests (23), settings UI tests (30), and deployment input tests (23) passed.
TypeScript and production frontend builds passed. GitLab's deterministic webhook
contract passed. Its separate, explicitly non-gating Live Fleet diagnostic hit
the 60-second acknowledgement timeout with work already in flight; it is not
reported as a successful live check.

The sections below retain earlier verification history. Their then-current
security-cutover blockers do not block this basic release.

## Deployment record

- Replan ownership fix #5558 merged as
  `1a5ff3e8368906270c1a7c87861a9ef164c504f3`.
- Readiness evidence repair #5570 merged as
  `7381d64db992133a3fb30883329253f4c4ebfb18`.
- Both PRs passed CI before merge. Existing gateway deployment workflows publish
  the merged code. Verify deployed images and health before activation.
- The UX change is included in this rollout branch; its existing offline
  frontend, CLI and worker checks passed in the prior verification candidate.
- Infrastructure is being planned against the existing state using retained
  upgrade inputs. No broad webhook apply or elapsed-time gate bypass is implied.

## Verified preparation

- Gateway deployment runs `35503009378` and `35503198166` succeeded. A later
  concurrent main deployment, `35504420271`, is also healthy and retains these fixes.
- Authority preparation installed 25 resources. The gateway role had exhausted
  its 10,240-byte inline-policy quota; identical scoped dispatch and task-source
  grants now use two managed policies. Twelve Terraform rollout tests passed.
- Five probe resources are installed, including a suspended CronJob and its
  dedicated service account, IAM role and immutable registry identity. Its local
  execution flag is false; gateway probe admission and budgets remain disabled.
- Worker image build `35505332757` succeeded from `ed897ee42f0e224268ee1e87ad871c7d15aa016e`.
  The probe CronJob pins
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:6fb49790e5b5fe4d5b126e30fb2450ac6b8237f0a2beb9162c90a26da7a5897e`.
  The ordinary ScaledJob has not switched to it.
- The live Door ACL and server hashes match the current source, including
  tenant enforcement for gateway-mediated runs. Its ingress policy omitted the
  gateway. A reviewed change adding only the gateway namespace AND pod selector
  was applied; gateway probes returned health 200, unauthenticated tools 401 and
  authenticated tools 200. Eleven network-policy tests passed.
- Both EKS clusters' `aws-auth` maps contain only their node roles, with no legacy
  worker, user or account mapping. The cyber cluster was inspected through its
  existing authorized ARC runner identity. The main cluster's explicit legacy
  worker administrator access entry and IAM AdministratorAccess still need retirement
  after active legacy jobs drain.
- PR #5576 merged as `3eda9bea0680d5e8988b2c7f50257308b97b6208` after all CI
  checks passed. Gateway deployment `35506167046` succeeded. Its bundled smoke
  requests skipped for lack of a usable workflow refresh token; separate actual
  Cognito-authenticated probes verified auth/me 200, default administration 200,
  posture 200, unknown-model refusal 422 and an unchanged stored default afterward.
  All four gateway replicas were updated and available. Default remains NULL at
  revision 1; posture remains report_only at revision 1.
- The tick's authority IAM policy was applied through a separate reviewed plan
  containing exactly one create. Its deployed authority flag remains false.
- Two admission-only Jobs using the pinned probe image and its actual IRSA service
  account completed successfully with `claimed=false, reason=disabled`. Neither
  invoked an SDK or requested destination credentials. Receipts were retained and
  both temporary Jobs removed. The CronJob remains suspended.
- The existing marker-signing secret is a placeholder. It must be securely
  initialized after legacy work is drained/reconciled; no real signing key was
  replaced or exposed during preparation.
- PR #5577 merged as `a026371515d53d329ea13cf2e2d079fa7b2d9645` after all CI checks
  passed. Gateway/frontend deployment `35506917122` succeeded. The downloaded CLI
  exactly matches reviewed source (SHA-256
  `277fcd3f4493c15a4aaba687a5ffa8fb3e50e5895b296ea78633c3a0199a661e`), and the published
  frontend entry `/assets/index-CBP6e30S.js` contains the new model-choice feedback.
  Thirty UI tests, 52 CLI tests, TypeScript and focused lint passed locally.
  Authenticated admin reads after this deployment still show NULL default/revision 1
  and report_only/posture revision 1. The bundled workflow smoke skip described
  above is not counted as live acceptance; separate actual-login checks are retained.

The default operation is `GET` / `PUT`
`/api/admin/persona-models/default/claude-agent-sdk`. The PUT accepts
`canonical_model_id`, `expected_revision` and `reason`; only a platform admin can
use it. It requires fresh exact platform destination, SDK revision and request-shape
evidence with a provider request ID, and commits the change and audit together.
This prepares an activation operation; it does not assert that any live model has
been qualified or promote an unproven default.

The proposed Terraform admission-pause plan is NOT applied: its dependency graph
also proposed changing deployed webhook Lambda packages/configuration and creating
engine-signing resources. Preserve active jobs and review those changes separately.
Authority, run tasks, source-isolation assertions and PMM enforcement remain off.

## Resumed cutover preparation

- Refreshed Ada credentials and reconfirmed the approved AWS account. Paused
  only the webhook ScaledJob using the KEDA pause annotation after a server dry
  run; KEDA reports Paused=True. This narrow live change avoids the unapplied
  broad Terraform quiesce plan. No active worker or live queue messages remained.
  The 103 historical dead-letter messages are retained and were not replayed.
- A reviewed saved gateway Terraform plan added exactly a dedicated
  Bedrock-only role and policy. Only the gateway role may assume it; it has no
  secret, queue, storage or platform-administration permission. Registered it
  with an authenticated platform administrator and transactional audit after
  the existing real STS/IAM routing verifier succeeded. No account mappings changed.
- Fresh-container manifest generation exposed random SDK device IDs. Version-2
  normalization covers only the known anonymous fixed-session device field and
  existing date reminder. Models, tools, token limits, account and session
  identities remain covered. All nine manifests matched across fresh containers.
  These are fake-upstream shape checks, not paid provider evidence.

- Worker build `35512479119` and gateway release `35512616564` succeeded
  from source `5e33b0df222d808421a9b4cfe26e847ff8d71d08`. The protected
  worker's live secret/SQS/S3/IAM/EKS reads were denied and its scoped
  CloudWatch log event was written and read back. This proves those
  permissions, not complete protected-run compatibility.
- Published real GitHub/GitLab Lambda packages with revision checks and pinned
  S3 object versions. Both updates succeeded; all existing Lambda environment
  values were verified unchanged. The broader producer Terraform plan remains
  unapplied because it includes unrelated engine-signing provisioning.
- Two unrelated AI-DLC tasks (issues 5526 and 5532) arrived during preparation.
  Admissions were resumed to let them run on the legacy worker. Marker seeding
  refused the nonempty queue before writing; its placeholder remains unchanged.
- A one-off three-slot/$3 SDK qualification run found a second request-shape
  issue: the SDK inserts the configured dollar budget into its initial reminder.
  Two slots completed with local shape refusals and no provider request ID; a
  third started slot was stopped by deleting the Job. Retain $1 conservatively
  for that unresolved slot. Admission was restored to disabled and the CronJob
  stayed suspended. No successful provider invocation is claimed.
- Version-3 normalization additionally covers only the exact zero-spend initial
  budget reminder with equal total/remaining values. The SDK still receives the
  admitted budget unchanged; provider max_tokens and thinking limits remain
  fingerprinted. Captured $0.01 and $1 requests now match. Local shape refusal
  also aborts the SDK immediately instead of waiting for its timeout.


## Real SDK qualification and saved-mapping acceptance

Worker build `35513638857` and gateway deployment `35513639201` succeeded from
`642a77226330c1cfeeaa31b628bc7a4c3ef66985`. The exact production probe runner
first matched all three request shapes against a loopback fake provider. That
preflight wrote no gateway evidence and made no paid calls. The subsequent finite
three-slot Job completed with **three proven invocations and no errors**:

| Model | Real provider request ID |
| --- | --- |
| `global.anthropic.claude-sonnet-4-6` | `4de5eea6-d6c7-4661-b740-6e344958a742` |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | `6bff8dcc-7c40-45c8-bad9-3f24bc9d20e4` |
| `us.anthropic.claude-sonnet-4-6` | `91cc993a-2fde-4a8b-9ff7-652c77abbdbc` |

The evidence is scoped to account `879318057152`, region `us-east-1`, the pinned
SDK contract `0.3.220` and v3 request shapes. It expires on 2026-09-21 at roughly
13:41 UTC. This is real model qualification, not end-to-end persona execution.
The private ledger retains $4 conservatively against the $10 ceiling, including
the unresolved $1 from the earlier stopped attempt. Probe admission/budgets were
restored to disabled/zero; the recurring CronJob stayed suspended. No ongoing paid
refresh schedule is authorized by the test budget.

The authenticated, audited default API promoted `us.anthropic.claude-sonnet-4-6`
at revision 2. The test administrator initially had an empty workspace claim;
the normal workspace-selection API selected its existing `adp-platform` membership
and a fresh login resolved its canonical human identity. No membership or tenant
was invented for the test. All 14 persona settings loaded. Saving developer→Haiku,
reading it back, refusing a concurrent create with 409, and resetting to the proven
Sonnet default all passed. The temporary saved mapping was removed by the normal
revision-checked API.

Current v3 source checks: 39 worker probe tests, TypeScript, 43 gateway probe/default
tests, Ruff and 45 producer tests passed. The authenticated live checks above are
separate from the deployment workflow's bundled smoke job.

## Earlier isolated protected-runtime canary

The two unrelated legacy Jobs for issues 5526 and 5532 remained active. The tick
schedule and KEDA admissions were restored while isolated verification continued;
neither Job was terminated. Production authority, task services, source isolation, legacy IAM
retirement, PMM enforcement and the user-facing feature flag remain unchanged.

A temporary gateway Deployment uses the same immutable source with authority and
run services enabled against a separate empty test queue. It runs without startup
background tasks. Its independent temporary marker key does not replace the live
webhook placeholder. A temporary queue-scoped gateway IAM policy and Service are
recorded for cleanup. The separate API stage `pmm-canary-20260920` uses the actual
AWS_IAM integration and a header-selected internal ALB target; the existing `dev`
stage's deployment is unchanged and the editable API integration was restored
immediately after creating the canary snapshot.

The first actual-entrypoint protected worker was refused at task pickup, before
any model call. A separately instrumented protocol canary is diagnosing this
using the actual protected role and projected Kubernetes token. Neither this
setup nor the earlier IAM-denial canary establishes complete runtime acceptance.
All temporary canary resources must be removed after retaining receipts. At that stage, activation was pending protected runtime verification. The basic
release above subsequently separated saved mapping from that migration.

## Protected protocol acceptance completed

Worker build `35515090602` published
`adp-agent-runtime@sha256:d48c6dd28213962693145e13022416688dda7f8ad07d98a50a3c2f647aca5c58`
from `ac7b9dbf4d510dd977bc64f22b9608c8e633f48a`. Initial task acquisition now
retries workload-publication 404s within its existing five-attempt/eight-second
bound, rereading the projected token each time. It does not retry maintenance
404s or transport 403s and has no direct-SQS fallback. Twelve focused tests and
Ruff passed before the build.

The isolated protected worker passed these actual service checks with its real
IRSA identity and projected workload token:

- Acquired, heartbeated and acknowledged its dedicated-queue assignment.
- Verified a signed report-only developer decision selecting the test human's
  saved Haiku mapping from a snapshot created before enqueue.
- Read run-bound marker fields matching that human and invocation.
- Uploaded a transcript; the operator read it back from S3 and verified SHA-256.
- Called the gateway-mediated Knowledge Door tools endpoint successfully.
- Refused caller-selected marker identity (422), an invalid run credential
  (404), and an invalid workload token (404).
- Refreshed its run credential and verified the refreshed model decision.

This was **instrumented protocol acceptance from an operator-created trusted
record**, explicitly marked as a test fixture and audited. It was not a real
GitHub event, a normal coding run, delegation, AI-DLC execution, or a provider
invocation. The first fixture lacked a pre-admission snapshot and failed the
mapping check; it was retired as failed, not counted as passing evidence.
The successful fixture was acknowledged, its authority revoked, its execution
cancelled for cleanup, and its work claim released as a completed protocol test.
The temporary saved developer mapping was removed using the revision-checked API.

The latest image separately passed **unmodified entrypoint** startup and empty
protected-queue polling. Job `pmm-protected-empty-task-v4-20260920` logged
`No message available after long-poll; exiting cleanly`. A preceding identical
Job also completed, but its node terminated before log retrieval; only the
retained v4 log is cited for the explicit empty-poll result.

No model calls were added by these checks. The conservative budget reservation
remains $4 of the authorized $10; it is not an actual-spend figure.

The temporary API stage/deployment, ALB routing condition/path, gateway
Deployment/Service/Secret, Door policy, empty test queue, dedicated IAM policy
and attachment, terminal test Jobs and three probe-preflight ConfigMaps were
removed after retaining receipts. The production API stage still points to
`uwrcj9`; KEDA admissions and the tick schedule are enabled. Production frontend
and API health checks returned 200. The suspended qualification CronJob and
Terraform-managed authority preparation remain installed.

## Separate worker-security migration remains deferred

The protocol checks above do not complete #5195. Normal protected coding,
GitHub renewal, cancellation and selected credential workflows still need live
acceptance. That migration also needs the real marker key, a fresh scoped
Terraform plan, coordinated gateway/worker/producer/tick activation and verified
legacy IAM/EKS retirement. None is required for the basic saved-mapping release.
The global credential-binding rollout remains separately tracked by #3186.

The earlier model qualification evidence expires on 2026-09-21 around 13:41 UTC.
Protected enforcing mode retains its evidence requirements. Basic preference
selection does not depend on those paid probe receipts staying fresh.

## Saved mappings without the worker-security cutover

The user-approved release resolves the initiating human's saved persona mapping
once before dispatch using `PERSONA_MODEL_MAPPING_ENABLED`. Existing gateway
identity, destination and budget enforcement continue to apply. An explicit
model selection applies only to its invocation; otherwise the saved mapping,
then the persona class default, is selected. With no configured default the
existing runtime default remains in force. Lookup failures do not silently
ignore a saved choice or dispatch an AI-DLC node in an unusable running state.

GitHub direct/delegated producers and chat ingest authenticate their lookup with
STS-bound request proofs and exact endpoint IAM grants. AI-DLC and replan use
the same resolver with their already-validated human root. The native Codex
reviewer and chat SDK honor the selected model. The protected worker protocol
remains separately opt-in; this release does not require worker-role retirement.

In this mode, saving and selecting require a compatible, active, permitted
catalogue model, without requiring a recurring daily provider probe. The UI
says "Available to select"; it does not claim a successful provider invocation.
Provider access and quotas are still enforced on actual use. Advanced enforcing
mode retains its evidence requirements. Profiles are deferred.

Local validation covers direct/delegated and chat adapters, authenticated lookup,
AI-DLC pre-transition refusal and replan retryability, preference API/catalogue,
Codex model precedence, and the settings production build. Live chat acceptance
and feature activation are recorded above.

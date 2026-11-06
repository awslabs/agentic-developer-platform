# L5 working-setup delivery contract

The five installed service workers share resource assembly, not authority. A queue
envelope or caller-supplied connection is never a credential grant. Security's
installed entry point remains `l5_services.runtime:factory`; each sibling must
provide its own factory and adapter before its consumer can be enabled.
Security refuses an unrelated installed service identity or missing transport,
session, archive or queue callbacks during assembly, before work admission.

## Shared runtime API

`l5_services.runtime_support` exports:

- `gateway_transport()` creates an IAM-signed client for the selected API Gateway
  stage. It requires an installed Lambda workload and an HTTPS stage URL.
- `sessions()` opens IAM-authenticated PostgreSQL sessions using the installed
  database configuration; callers must check durable work and configuration rows.
- `ScopedSources(transport)` fetches Product source snapshots and manifests
  through the delegation-bound Product API. The caller supplies a delegation
  and current leased-work request; it must compare returned receipts with the
  durable admission before using source bytes.
- Product configuration options map verified AWS connection revisions and
  authorized environment bindings to selectable connections and regions.
  Product repository references are draft selections (`repository`); Security
  code-review admission requires an actual immutable `repository_commit` from
  the Product snapshot path. A matching repository name alone is insufficient:
  the Product source binding must verify the selected commit before effects.
  Transform also accepts an authorized repository as a disabled configuration
  draft, but enabling it or submitting a run still requires an immutable
  Product-verified commit; a draft revision is not a runnable source.
- `archive_store(service_id)` returns a versioned archive store under
  `l5/<service_id>/work/<run_id>/`. The read path checks the service prefix,
  version, size and digest. `queue_publisher(service_id)` publishes to the
  installed service's queue. Both reject an unknown or non-matching
  `L5_SERVICE_ID`; they do not grant permission to process the message.
- `provider_session(credentials, region)` constructs a provider SDK session
  **only after** a service factory obtains a scoped broker response. Never
  call it with envelope or user-provided credentials. Factories must recheck
  the durable operation, lease, delegation revision, selected connection,
  identity and expiry immediately before effects. Setup intents and runs have
  different durable proofs; persistent DevOps readback needs its own proof.
- `verify_source_receipts(run, claim, sources, service_id)` compares pinned
  admissions with current Product snapshot receipts for the leased SQL work.
  `run_provider_session(run, claim, transport, sources, provider_factory,
  *, service_id, allowed_clients)` obtains a broker session and limits provider
  clients. The caller supplies the `Run` and fenced SQL claim from
  `ExecutionService`, **not** an envelope-derived run or an allow callback.
  It checks service ownership, admission scope, broker operation/attempt,
  delegation revision, selected provider identity and expiry. On each native
  method it rechecks Product receipts; source-less FinOps work instead
  rechecks the broker's current lease and grant before the effect.
- `setup_provider_session(intent, claim, transport, sessions, provider_factory,
  *, service_id, client_name)` reads the saved configuration revision and
  rechecks the separate setup broker proof on each native method. It returns
  the verified setup scope, provider identity and guarded client. It cannot
  authorize a normal run or persistent DevOps space readback. Readback must
  present its own durable authorization path, not a borrowed setup/run lease.

For run authority the installed factory requests `/internal/v1/l5/credential-session` with
the current operation, attempt, lease fence and delegation. For setup authority
it checks the configuration revision and setup intent first, then verifies the
broker's `setup_verified` scope, connection and provider identity. Denial,
revocation or expiry stops provider calls; before each sourced provider effect,
the worker compares Product snapshot receipts with pinned admissions. Each sibling must use its own service
identity and native adapter; Security's factory must continue refusing sibling
work. Archive history and uncertain-effect recovery remain durable.

## Release boundary

Native console automation is a deployment policy, separate from Product access.
The protected release environment variable `ADP_L5_AWS_CONSOLE_AUTOMATION`
defaults to `disabled`. The current DevOps SSO qualification and FinOps native
inquiry driver require AWS console navigation, so the release refuses those
drivers before artifact publication, infrastructure installation, credentials or
browser startup unless the target policy explicitly says `allowed`. Release
workers must not enable this policy themselves to clear a failed check. Workflow
inputs and private fixtures cannot override it. The qualification job checks it
again before starting native work.

Security, Transform and Well-Architected use their documented API/CLI paths and
remain eligible when console automation is disabled. Code review, offline tests
and ADP-only UI tests may continue for every service. A disabled console path is
an unresolved native acceptance requirement, never a successful or waived test.
Do not substitute another browser or undocumented console HTTP endpoints.
FinOps' [documented interaction path](https://docs.aws.amazon.com/finops-agent/latest/userguide/how-do-i-interact-with-the-agent.html)
uses the native web application; Cost Explorer results establish billing evidence
only and cannot establish native FinOps-agent acceptance. These release checks
do not claim to control a separately launched console executor outside the
release workflow; keep its admission and consumers disabled on restricted targets.

Build one immutable worker archive, install selected service roots with consumers
paused, then run each selected service's own native qualification driver. Private
provider fixtures belong in protected configuration, not this document. Each
driver must emit delivery evidence and native qualification tied to source,
definition and workflow revisions. A missing driver fails qualification; it is
not a reason to fabricate an HTTP observation. Offline mocks establish assembly
and denial behavior only; the replacement release node owns provider jobs,
installation readback and live acceptance.

Well-Architected, FinOps, Transform and DevOps service owners can build their
adapters and offline drivers in parallel. They finalize their concrete factories
against the reviewed shared helper API; this integration owner alone updates
shared broker authority and release workflow. Product binding observations have
a limited freshness window. Saved draft selections remain usable while run
admission rechecks current bindings and seals immutable source revisions.

## Foundation and release sequencing

This delivery revision follows the user's request to finish the five existing
flows with working services. Completed nodes, failed-attempt history and the
accepted flow/invocation/concurrency limits remain. The coordinator owns
sequencing and recovery; there are no additional human approval stages.

## Delivery order

| Stage | Owner | Deliverable | Completion evidence |
|---|---|---|---|
| Shared preparation | Active delivery coordinator | Independent Terraform roots, bounded deployment role, database IAM user, protected CI inputs and verified Product source/environment bindings | Applied-state and authority readback; no service-readiness claim |
| Shared integration | Security flow's new shared integration story | Reusable production resources and delegated authority for every registered service; service qualification dispatch and private fixture materialization | Normal mounted Gateway/Lambda assembly tests and reviewed merge |
| Service integration | Each service flow's integration story | Concrete runtime factory, native adapter and executable qualification driver using the shared contract | Packaged factory and real-entry-point integration tests, reviewed merge |
| Grouped installation | Delivery coordinator and release owners | One reviewed package; plan selected roots together, install, then roll out the selected workers | Source-bound package and Terraform/lifecycle receipts |
| Native acceptance | Each existing service release node's replacement | A real selected Product journey, independent provider readback, retrieval, recovery and owned cleanup | Service qualification and delivery-evidence accepted by the native evaluator |

The shared integration story settles its exported Python interface before sibling
factory work is dispatched. Only that story edits the shared runtime authority
and release workflow. Service owners edit their factory, driver, adapters and
service infrastructure. Necessary additional files remain allowed; these are
coordination boundaries, not file-list failure gates. The coordinator integrates
any genuinely shared fixes and resumes ready flows automatically.

## Runtime and release contract

Human configuration ownership uses Product's canonical workspace user, including
the platform-verified Cognito `org_placement` link used by onboarded accounts.
Migration `171_l5_linked_draft_owners` repairs only disabled, revision-one drafts
with one matching same-workspace placement and no binding, setup intent or run.
It preserves configuration IDs, settings, versions and idempotency digests.
Revoked membership, a competing login, an unverified link or existing work leaves
the draft unchanged; a conflicting receipt aborts the migration transaction.
No credentials or native provider calls are used to reconcile ownership.

The existing `l5_services.runtime:factory` builds only Security code review.
Retain it as a compatible entry point, extract reusable resources/authority into
`l5_services.runtime_support`, and provide a factory per service.
Factories must construct the existing `ExecutionService` with actual adapters
and all required setup, run, repair, event/readback and result-publication
callbacks. An importable factory with unavailable callbacks is incomplete.

For Gateway startup, each service owner exports a synchronous `gateway_backends()`
from `src.l5_backend_<service_id>` in the Gateway package. The result is an exact
mapping of server-owned callbacks: DevOps exports `l5_devops_integration_observer`,
`l5_devops_verification_reader`, `l5_devops_release_authority`, and
`l5_devops_prevention_provider`; FinOps exports `l5_finops_authority`; Transform
exports `l5_transform_work_authority`, `l5_transform_executor_qualification_reader`,
and `l5_transform_artifact_store`; Well-Architected exports `l5_wa_authority`,
`l5_wa_profile_client`, `l5_wa_consumer_resolver`, and `l5_wa_qualify`. Gateway
registers only modules present in the installed package, rejects incomplete
exports or missing transitive dependencies, and leaves unavailable services
without callbacks. Security's existing backend remains the compatibility path.
The module factory must construct real Product-scoped admission/readback adapters,
not an allow-all placeholder. A shared registry alone does not enable a service;
service-owned exporters and mounted production checks are still required.

An exported dependency that needs the request database, actor or admission
context may explicitly define a synchronous `bind_backend(backend)` method.
Gateway calls it after constructing each request's backend and uses its returned
callable or client on that backend only; the application-level export is not
mutated. Existing callbacks without this method continue unchanged. The bound
adapter reads `backend.admission_key` and `backend.admission_operation` when
invoked, after submit/retry has assigned them. Binding performs no startup I/O
and must not store request actors on a shared application object.

The common resources comprise database sessions, signed internal Gateway
transport, Product source reads, artifact storage and the service queue publisher.
Configuration checks project current Product capability, connection version,
repository observation, scoped delegation and ready binding without rewriting
draft history or contacting a provider. A stored draft gap can clear on check,
but missing native qualification and service-owned admission callbacks remain
unavailable; checks do not issue worker authority or permit work.
Saving a configuration links its revision back to Product Profile. Those link-only
Product revisions do not make the saved inputs stale: readiness verifies a complete
interval of successful, scoped `linkL5Configuration` audit records. A real Product
edit, missing history or mismatched operation still requires a profile refresh.
Configuration snapshots, source freshness and run admission remain unchanged.
The configuration refresh API accepts optional `choices` alongside `profile_ref`.
The owner can use the ordinary reasoned overrides to replace an outdated explicit
source or connection while adopting the current profile in one conditional revision.
Choices must be authorized by that profile. Without explicit source choices, refresh
continues to preserve the prior source scope; prior versions and run inputs are retained.
The common authority helper derives service and work identity from durable
records, obtains short-lived sessions through the broker, and rejects stale,
foreign, revoked and expired work. It must support service-specific registered
worker identities; removing hard-coded Security checks must not remove scope
checks. Provider credentials never come from ambient worker credentials.

Service-specific assembly uses those helpers without implementing a second
credential platform. Security must support threat models as well as code review.
DevOps must install its persistent-space, event/readback and authenticated native
UI paths. Transform uses the existing isolated executor for finite CLI jobs and
validation; Lambda performs bounded submit/observe steps. Well-Architected uses
the native profile/generation APIs. FinOps must connect the qualified private
console executor to native inquiry and independent billing readback; CE output
alone is not a FinOps-agent response.

The shared release keeps publish, infrastructure install, rollout and native
qualification separate. Add service selection for implemented qualification
drivers and materialize private JSON fixtures inside each job's temporary
directory. A local filesystem path stored as a secret is not a portable fixture.
Missing provider prerequisites must be reported before starting paid jobs.
Workers stay disabled until the selected factory and authorization path are
installed. Repeated service verification should reuse the installed immutable
package where its source and definition receipts are compatible.

For release dispatch, store the selected service's **JSON content**, not a path,
in the protected `ADP_L5_<SERVICE>_LIVE_CONFIG_JSON` secret. Security additionally
uses `ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON` for its existing scoped selector.
The JSON object must pin the reviewed source SHA at `authorization.source_sha`
(Security), `release.source_sha` (DevOps), or `source_sha` (other service drivers).
When supplied, `release.worker` must also equal that revision. The workflow writes
the unmodified JSON to a private runner-local file and exposes its digest and
private evidence directory only inside the job. The concrete driver must check
the authorized Product selections, owner, expiry, provider roles, immutable
sources and installation before native work; the shared fixture materializer
does not invent those fields or rewrite existing attempt journals. Sibling
drivers use `qualify-<service>-release.py preflight|run SOURCE_SHA FIXTURE_PATH
EVIDENCE_DIR` plus an executable `emit-<service>-qualification.py` in the service
scripts directory. Missing driver or emitter fails before provider work.

`installation_services` is an optional comma-separated group of selected
service roots installed alongside foundation; it must contain the qualification
service. The protected infrastructure JSON must supply each selected root's
`worker_factory` and `worker_environment`. FinOps additionally requires
`executor_image_uri` in its selected `services.finops` input: an immutable
`<selected-account>.dkr.ecr.<selected-region>.amazonaws.com/adp-<environment>-l5-finops-executor@sha256:<64-hex-digest>`.
The FinOps owner builds/publishes and independently reads back that exact image;
the shared release does not synthesize a digest or enable consumers without a
qualified driver. Keep provider credentials, source fixtures and deployment
receipts out of the repository and public logs.

## Cost evidence correction

DevOps native task APIs provide task identity/state; the inspected account usage
API provides monthly aggregates, not measured per-task spend. Qualification must
not require a fabricated per-job dollar value. Keep admission allocations,
request/concurrency/time bounds and record the supported usage evidence with
its scope, observation time and coverage. Report unavailable task-level cost as
unavailable; never zero, measured, or a hard AWS spending guarantee. A reviewed
pricing-based estimate is an estimate and must retain its inputs and provenance.
This changes the impossible measurement assumption, not the accepted delivery
budgets or authorization boundary. Update the affected tests and emitter together.

## Acceptance and recovery

ADP is the first real test Product. Runtime configuration must contain no ADP
account, repository, user or Product constants. Existing portability, revocation,
recovery and cleanup criteria remain due at their declared acceptance phase.
Private user sessions and additional fixture Products are live-test inputs,
not reasons to leave executable integration unfinished.

Fresh integration merges must feed fresh release nodes through verified merge
receipts. An issue-body edit or graph file map does not update an existing
execution's release pin. Preserve the old node as superseded after it settles;
replace its final-evaluation references with the new release and retain every
acceptance predicate. Do not mark a flow complete because packages were published,
Terraform applied, or offline tests passed.

Retain native IDs, attempts, source snapshots, costs and uncertainty through
recovery. A provider acknowledgement followed by a failed readback does not
permit blind resubmission. Use native recovery/amendment APIs; do not edit the
orchestration database or reset budgets to recover progress.

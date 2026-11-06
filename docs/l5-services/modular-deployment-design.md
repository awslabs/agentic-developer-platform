# Modular L5 deployment contract

This is the deployment ownership contract for Security, DevOps, Transform,
Well-Architected and FinOps. Each service owns an executable Terraform root and
separate state under `modules/L5-services/infra/`. Gateway continues to host the
API/UI and serialized L5 migrations. Product APIs and the credential broker
continue to govern native resources in each onboarded Product's selected accounts.
This infrastructure refactor does not establish native service qualification.

## Ownership and entry points

| Location | Responsibility |
|---|---|
| `infra/foundation/` | One L5-owned managed policy attachment granting gateway send-only access to five named queues and read access to their queue registry parameters |
| `infra/security/` | Security worker/repair, retained SQS/DLQ, schedules, IAM, logs, alarms, network rules, identity and queue registration |
| `infra/devops/` | The same base resources plus native-event and periodic readback Lambdas, rules and identities |
| `infra/transform/` | Independent Transform base worker infrastructure |
| `infra/well-architected/` | Independent Well-Architected base worker infrastructure |
| `infra/finops/` | Independent FinOps base worker infrastructure |
| `infra/modules/worker-runtime/` | Reused worker/repair implementation; separate resources in each service state |
| `infra/modules/service-events/` | Reused event-driven Lambda implementation with bounded IAM and disabled rules |
| `infra/modules/service-registration/` | Worker identity and service queue discovery |
| `infra/modules/legacy-devops-events/` | Previous uninstantiated event module, retained only for explicit migration of external callers |
| `scripts/deploy-infra.py` | Selected-account plan/apply/status, grouped planning and private receipts |
| `.github/workflows/l5-services-ci.yml` | Offline validation of every root/module, mock Terraform tests, contract and Python tests |
| `.github/workflows/l5-services-release.yml` | Publish shared immutable artifacts, install foundation/selected service, then service qualification |
| `.github/workflows/l5-services-worker-lifecycle.yml` | Explicit selected-service worker rollout, rollback, pause and resume |

Paths in the first eight rows are relative to `modules/L5-services/`. Workflow
YAML stays in `.github/workflows/` so GitHub discovers it. Do not create another
copy of deployment logic in gateway or platform. Reuse existing private release
storage; this change does not create a duplicate artifact bucket.

The S3 backend key is
`l5-services/<environment>/<region>/<service>/terraform.tfstate`, including a
separate `foundation` key. Terraform 1.10+ uses native S3 lock files; CI pins
1.14.0. The state bucket must already exist, be private/versioned, and permit the
selected deployment role to access the selected state and lock objects.

## Platform input and discovery contract

The private version-1 JSON configuration contains:

| Field | Required value |
|---|---|
| `schema_version` | `1` |
| `account_id`, `region`, `environment`, `role_arn` | Selected protected target; environment is `dev`, `test` or `prod` |
| `state_bucket` | Existing private Terraform backend bucket |
| `gateway_role_name` | Existing gateway execution role; foundation owns only its L5 policy attachment |
| `platform.vpc_id`, `platform.private_subnet_ids` | Published platform network references |
| `platform.db_security_group_id`, `platform.endpoint_security_group_id`, `platform.db_port` | Published database/private endpoint references |
| `platform.permissions_boundary_arn` | Approved worker permissions boundary |
| `platform.gateway_api_arn` | Scoped `execute-api` ARN ending `/*/*/internal/*` |
| `platform.rds_connect_arn` | One database IAM user's connect ARN |
| `platform.agent_registry_table_name` | Existing identity-owned worker registry |
| `services.<service_id>.worker_factory` | Service-owned installed runtime assembly listed below; each service must qualify its branch |
| `services.<service_id>.worker_environment` | Private runtime references: `L5_GATEWAY_URL`, `L5_RDS_HOST`, `L5_RDS_PORT`, `L5_RDS_DATABASE`, `L5_RDS_USER`, `L5_ARCHIVE_BUCKET` and approved service-specific settings |
| `services.devops.native_event_pattern` | JSON string with the approved provider source and detail types |
| `services.finops.executor_image_uri` | Selected private executor image pinned by digest to the target account, region and environment |
| `services.finops.executor_surface` | Private JSON string with reviewed console selectors and nonempty `surface_revision`; required before grouped planning and forwarded unchanged to the FinOps executor |

The factory values are `l5_services.runtime:factory` for Security and
`l5_services.runtime_<service_id>:factory` for DevOps, Transform,
Well-Architected and FinOps. Lifecycle validation checks each service's factory
and its exact installed resource policies, including the FinOps executor and
Transform artifact archive. Unrelated extra policies remain a scope error.

Service IDs in JSON use `well_architected`; CLI accepts either spelling. Terraform
owns `L5_SERVICE_ID`, `L5_WORKER_FACTORY` and `L5_WORK_QUEUE_URL`; callers cannot
override those in the runtime environment. Worker, repair and DevOps event
functions also derive `L5_RDS_CONNECT_ARN` from the same `platform.rds_connect_arn`
used by their IAM policies; a legacy environment value cannot override it.
Missing or invalid runtime references produce a named-field lifecycle diagnosis
and repair action without exposing configuration values. Secrets and populated configuration
must remain private. References may come from platform outputs or SSM; pipelines
do not need blanket access to other stacks' Terraform state.

Transform lifecycle checks require all ten executor runtime references before
rollout, including the verified isolated node, cluster, namespaces, artifact
bucket and immutable images. Missing references identify the artifact/executor
bootstrap as the next step. A foreign Transform bucket or FinOps executor also
reports its field name without exposing the supplied value. Presence checks do
not replace executor isolation, runtime probes or native qualification.

Each root publishes
`/adp/<environment>/l5-services/<service_id>/work-queue-url`. Gateway resolves this
at publication time using `L5_DEPLOYMENT_ENV`, so a new service installation does
not require another gateway rollout. Explicit service queue environment variables
remain supported. Security alone preserves `L5_WORK_QUEUE_URL` and the legacy
Security SSM parameter. Other services never fall back to Security's queue.

Repair selects work through its durable Run or SetupIntent service. Claims retain
SQL lease/fence/idempotency checks, and a Lambda refuses a factory that reports a
different service. Before resume, the lifecycle command probes every installed
entry point without admitting work; failure leaves consumers paused. DevOps
rollout, rollback, teardown and resume include its native-event and periodic
readback functions and EventBridge rules. Their environment, IAM policies and
event targets must match the selected service. Receipts record each event
function’s archive digest and trigger state, including partial rollout failures.
An SQS envelope is not authority. The bundled production factory
currently assembles Security; service release stories must complete their own
factory branch and qualifications before enabling their consumers. Service-specific
worker and event identities also need matching owner-authorized delegation grants;
the legacy `l5-worker` grant is Security-only. This refactor does not broaden it
to authorize sibling workers.

The [release evidence contract](release-evidence.md) defines service reader
outputs, safe GitHub artifact publication and the engine delivery receipt.

## CI/CD and grouped delivery

Store the populated configuration in protected environment secret
`ADP_L5_INFRA_CONFIG_JSON`. The release workflow checks its role, region and
environment against the selected protected target and independently verifies STS
identity. Existing release role, artifact bucket, gateway URL and service fixture
settings remain required. No new human approval stage is introduced by this change;
existing protected-environment policy still applies.

Select a scoped L5 deployment role with protected variable
`ADP_L5_DEPLOY_ROLE_ARN`; the older `ADP_DEPLOY_ROLE_ARN` remains a compatibility
fallback only when it has the required L5 permissions. Do not change the role used
by unrelated gateway deployments to grant L5 access. The selected role and its
operator-owned ceiling must permit L5 Lambda, queue, event, log, registry and
network-rule resources, state locks and bounded worker-role creation/pass-role.
A publication-only role is insufficient, and an explicit boundary deny cannot be
fixed by adding an allow policy. The release owner must bootstrap/admit the L5
role and worker boundary through the existing protected identity process before
installation, then bind that exact job role in the flow's delivery contract.
This refactor does not broaden the gateway deployment role or its ceiling.

Tagged worker egress creation requires `ec2:AuthorizeSecurityGroupEgress` on
both the existing `security-group` and the new `security-group-rule`. The
operator-owned deployment policy checks the parent's L5 module/environment
resource tags and selected VPC separately from the new rule's matching request
tags. Request tags do not authorize an unrelated parent group. The existing
rule metadata grant covers the dependent `ec2:CreateTags` authorization for
tag-on-create. A rule-resource permission failure must be repaired in this
deployment policy; it does not require changing the network rules or worker
permissions boundary.

Selecting `transform_cluster_name` also admits the Transform worker's separate
executor archive in the operator-owned worker ceiling. The bucket is
`adp-<environment>-l5-transform-<account>`; only the exact Transform worker role
can write/read versioned `l5/transform/artifacts/` objects and discover/read
retained checkpoint paths. The service bootstrap must still install the matching
identity policy. Other service roles, unrelated paths and object deletion remain
outside that grant. Apply the reviewed ceiling before enabling Transform; an
inline worker policy cannot override a missing boundary permission.

1. PR CI validates all independent roots and reusable modules without AWS, runs
   mock plans and exercises runtime/deployment tests.
2. The release workflow publishes the shared wheel/archive once with immutable
   object versions, then resolves and verifies the archive's stored bytes.
3. `deploy-infra.py` validates all selected inputs and creates saved plans for the
   entire selected group before applying any stack. Normal release rejects deletes
   and replacements. Each state locks independently; a stale saved plan fails normally.
4. Apply foundation prerequisites and selected service plans. New SQS mappings and
   EventBridge rules start disabled. Infrastructure and worker rollout receipts are
   distinct from native qualification. A repeated unchanged plan is a no-op.
5. Service qualification verifies installed code, current Product authority,
   provider results and owned cleanup before enabling effect-producing consumers
   and emitting accepted delivery evidence. Security has a workflow job; sibling
   flows retain responsibility for their native drivers and qualification jobs.

The existing release entry point installs foundation plus its selected service.
For the first installation, select workflow operation `install` with a reviewed
main revision. This standalone operation publishes the actual package and installs
the selected roots with consumers disabled, without requiring native live-test
sessions or browser access. It still requires complete infrastructure inputs and,
for Transform, the real executor image build inputs. It cannot accept engine
delivery transport, run native qualification, enable consumers, or publish delivery
evidence. After preparing the installed runtime and authorized fixtures, use the
default `release` operation for full qualification. An installation run succeeding
does not complete a service flow.

Use operation `publish` when immutable artifacts or Transform images are needed
before the executor configuration can be assembled. It uses the same reviewed
main source, protected target, OIDC identity and publication receipts, but skips
infrastructure installation and every qualification job. Runtime configuration
and native fixture sessions are not required; Transform still requires its real
protected image-build inputs. The resulting image digests can then be used in
the reviewed executor bootstrap and private installation configuration. Publication
rejects engine delivery transport and cannot emit delivery completion evidence.

For a grouped campaign, publish once and use the same immutable coordinates with:

```sh
python3 modules/L5-services/scripts/deploy-infra.py plan \
  --config "$L5_PRIVATE_CONFIG" \
  --services foundation,security,devops,transform,well_architected,finops \
  --bucket "$L5_RELEASE_BUCKET" --source-sha "$L5_SOURCE_SHA" \
  --wheel-sha256 "$L5_WHEEL_SHA256" --manifest-version "$L5_MANIFEST_VERSION" \
  --receipt-dir "$L5_PRIVATE_RECEIPTS"
```

Use `apply` for the same campaign to generate/review saved plans and apply them in
one invocation. `status` reads selected root outputs without artifact inputs.
The CLI deliberately does not enable consumers or claim service success. Keep
receipts, Terraform plans/state and logs private; they can contain target details.
The workflow retains receipts in existing private CI evidence storage.
Rejected plans also retain a failure receipt with the selected service, resource
addresses, requested actions and whether replacement was caused by taint. The
receipt excludes resource values. Check the live resource and backed-up state
before repairing a tainted entry; a rejected plan never authorizes replacement
or an automatic state repair.

Infrastructure mutation is serialized by the existing release coordinator. A
service flow should finish its deployment stage and release that exclusion before
waiting hours for provider results. The latter remain tracked by durable L5 runs.
Gateway/frontend may continue normal automatic code deployments. Plan file lists
are explanatory, not a restriction on necessary implementation files.

The shared queue worker intentionally publishes its next durable polling step
back to its own SQS queue. Its Lambda recursion setting must be `Allow`; the
default `Terminate` setting drops a healthy chain after about sixteen invocations.
Each service root manages this setting for its queue worker, and the resume path
sets and verifies it before enabling consumers. Run deadlines, poll limits,
dispatch limits and fenced SQL work claims continue to bound execution. The
deployment role can change this setting only on the five named queue workers.

If native jobs continue while ADP polling stops, check the worker's
`RecursiveInvocationsDropped` metric and recursion setting. After correcting the
setting through the maintained lifecycle, reconcile the existing Security run
through ADP. Reconciliation replaces undelivered observation work with a poll or
collection of the same native job; an active lease must finish before takeover.
Do not submit a replacement assessment merely to recover its monitoring.

### Reconcile an existing Well-Architected schedule scope

An older Well-Architected installation can lack the repair role's schedule-admission
route and the matching agent-registry scope. Its worker lifecycle then refuses
rollout during installed-scope verification. The reviewed Terraform definitions
already contain these permissions; this case does not require a new installation.

Use the existing **L5 Service Worker Lifecycle** workflow on reviewed `main`,
selecting `reconcile-wa-schedule-scope`, service `well_architected`, the protected
environment, and the existing immutable artifact's source SHA, gateway wheel
digest and manifest version. `reconciliation_mode` defaults to `plan`. The
protected `ADP_L5_DEPLOY_ROLE_ARN`, region and `ADP_L5_INFRA_CONFIG_JSON` must agree;
ambient credentials and a different account or role cannot substitute for them.

The operation plans only `infra/well-architected/` under the normal S3 state lock.
Its fixed validator permits only an in-place addition of the exact schedule route
to `module.runtime.aws_iam_role_policy.repair` and `l5:schedule-admission` to
`module.registration.aws_dynamodb_table_item.repair`. Existing documents must
match the reviewed legacy or current scope in full. A partially completed repair
can add only the remaining permission; an already reconciled installation is a
verified no-op. Unexpected grants, another resource change, state moves, imports,
replacements, unknown values or changed root outputs require separate review.

Selecting `apply` generates one fresh saved plan, validates it against those fixed
constraints, checks the live runtime and permissions again, verifies the plan's
digest, and applies that same file. It never regenerates a plan between validation
and apply. First review the plan-only receipt, then supply its `reviewed_plan_digest`
when dispatching `apply`. The new plan must reproduce that digest of all managed
changes, private inputs, artifact pins, workflow revision and runtime readback.
Differences require another plan-only review before mutation. Terraform timestamps
and the new OIDC session's data-source identity are excluded from this comparison.
The private saved plan is not copied between jobs; a stale saved plan still fails
through Terraform's normal checks.

Both consumers must already be paused. Reconciliation verifies unchanged function
configuration, code and consumer readbacks, then exact installed permissions and
the full worker scope. It does not publish artifacts, install foundation, roll out
workers, invoke a worker/provider, enable consumers, or run browser automation.
Code rollout and native qualification remain separate operations.

The existing private lifecycle evidence upload retains the stage, requested
resource changes, safe failure category and plan/document hashes. Raw Terraform
plans, logs and private inputs stay in a temporary private directory and are
removed when the command exits. After an uncertain apply, inspect its receipt and
current permission readbacks before retrying; never force-unlock active state or
widen the reconciliation allowlist to accommodate unrelated drift.

## Existing installations: transfer ownership before applying a new root

Security retains deployed names (`adp-<environment>-l5-worker`, repair and work
queues). The old gateway `module.l5_security[0]` now references the extracted
reusable module with unchanged resource addresses. Its disabled-by-default toggle,
SSM item and registry item are retained for **existing-state compatibility only**.
New installations use independent roots; do not enable that legacy toggle.

For an environment with existing gateway-owned resources:

1. Establish one mutation owner; stop concurrent gateway/L5 applies and drain or
   pause the service consumers. Back up both states privately, recording serials.
2. Inventory the actual source state and remote resource identities. If none exist,
   leave the legacy toggle false and install the independent root normally. A
   resource conflict is an adoption problem, not permission to replace a queue.
3. Transfer existing entries from `module.l5_security[0].<resource>` to
   `module.runtime.<resource>` in the Security backend. Use controlled Terraform
   state removal/import or offline state moves with reviewed state pushes. Preserve
   IDs and queue contents. A `moved` block alone cannot cross backends. Do not run
   an infrastructure apply between releasing the old state ownership and importing.
4. Transfer gateway `aws_ssm_parameter.l5_work_queue_url[0]` to
   `module.registration.aws_ssm_parameter.legacy_security_queue[0]` and
   `aws_dynamodb_table_item.l5_security_worker[0]` to
   `module.registration.aws_dynamodb_table_item.worker`. After transfer, set the
   gateway legacy toggle false. No resource may remain owned by both states.
5. Review both refreshed plans: gateway must not destroy retained L5 resources;
   Security must not recreate or replace them. New registry paths/alarms may be
   added. Then resume through the qualified lifecycle with the pinned archive.

The old DevOps module has no in-repository caller. If an external installation used
it, inventory and explicitly migrate its differently named resources; do not apply
the new DevOps root over them and assume ownership moved. Queue deletion and state
migration are not automatic release recovery actions.

## Remaining service work

These roots provide base orchestration infrastructure, not every provider executor
or a completed service. Transform-specific executor compute, service runtime
assemblies, native DevOps integrations, private fixtures and all service acceptance
remain owned by their existing stories. Add service-specific infrastructure inside
its root, reuse common modules, and extend the shared CI/CD path. Existing acceptance
criteria, budgets, history and native cleanup requirements remain in force.

### Native provider prerequisites

The independent `modules/L5-services/infra/provider-access/` root manages selected
Product connection permissions in a separate per-connection state. It does not
alter connection trust or let workers expand their access. See
[native provider permissions](provider-connection-permissions.md) for preparation
and readback verification before native setup.

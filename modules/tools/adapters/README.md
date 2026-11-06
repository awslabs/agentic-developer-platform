# Shared tool-adapter foundation

This package is the separate service foundation for story #8447. It installs
`adp-tool-adapters` and the existing `adp-tools-shared` library, without the core
gateway distribution, SQL clients, application secrets or vendor execution.
The [approved service design](../../../docs/tools/atlassian-mcp/adapter-service.md)
defines the later authority, provider and release integration.

The foundation exposes `/healthz`, `/contract` and `/readyz`. These return only
operational status and the shared wire-schema version/digest. Readiness requires
valid deployment-owned configuration matching the image's baked source revision.
It explicitly reports `execution_enabled: false`. All internal routes reject
requests before consuming their payloads. There is no provider catalogue,
general proxy, consent implementation or credential lookup in this service yet.

## Package and image verification

Run from this directory with Python 3.12 or 3.13 and the lock-compatible uv:

```bash
uv run --frozen --group dev ruff check tool_adapters tests k8s scripts ../adp_tools/adapter_contracts.py
uv run --frozen --group dev pytest -q
```

The Docker check is intentionally explicit; a skipped `test_package.py` check is
not image qualification. For a committed, clean source checkout with a Docker
daemon, run from the repository root:

```bash
python3 modules/tools/adapters/scripts/image_smoke.py \
  --source-revision "$(git rev-parse HEAD)" \
  --evidence-dir /tmp/adp-tool-adapters-image-evidence
```

The harness builds the actual locked image, then boots it with no network,
a read-only root, a nonroot UID, no capabilities and bounded resources. It checks
health/readiness/contract responses, rejects forged internal requests, inventories
Python and Debian dependencies, checks forbidden core/SQL imports, and scans
the exported filesystem and logs for injected secret fixtures. It removes only
its uniquely named container and image tag. The evidence includes the source
file hashes, a CycloneDX inventory, the image ID, Docker-reported image size,
startup time and cleanup result. A private build log is kept for diagnostics.

`codebuild/bs-tool-adapters-smoke.yml` provides the same nonpublishing check for
the existing CodeBuild action's verified source archive. The invoking workflow
must use the existing restricted PR build role and supply `ADP_SOURCE_SHA`.
CI wiring and release integration are owned by the native #8447 release slice.
The local image ID is used only as the smoke configuration's digest; deployment
requires the published ECR manifest digest.

## Deployment contract

`k8s/render.py` emits a private Service, two-replica Deployment, dedicated service
account, nonsecret configuration and disruption budget. It requires an ECR image
pinned by digest, a same-account role ARN and a configuration whose source and
image identify the intended release. A configuration checksum rolls pods when
deployment-owned settings change.

The pod template also opts this workload out of operator auto-instrumentation
for the java, python, dotnet and nodejs runtimes
(`instrumentation.opentelemetry.io/inject-<runtime>: "false"`). The cluster
instrumentation operator otherwise injects language runtime init containers,
which the isolated namespace's restricted PodSecurity policy rejects and which
the runtime verifier refuses because the foundation admits exactly one container
and no init container. The opt-out is per-workload and changes nothing
cluster-wide; pod logs and container metrics are retained unchanged. Re-enabling
any of the four on the observed Deployment, ReplicaSet or Pod fails readback with
`adapter_deployment_mismatch`, `adapter_replicaset_mismatch` or
`adapter_pod_owner_mismatch`.

With the package installed, invoke the renderer from this directory:

```bash
uv run --frozen python k8s/render.py \
  --image "$ADP_ADAPTER_IMAGE" --namespace "$ADP_ADAPTER_NAMESPACE" \
  --role-arn "$ADP_ADAPTER_ROLE_ARN" --config "$ADP_ADAPTER_CONFIG_FILE" \
  --output "$ADP_ADAPTER_MANIFEST_FILE"
```

Configuration is JSON validated by `tool_adapters.config.AdapterConfiguration`:
`schema_version` is `1.0`, `mode` is `foundation`, and `release` is the shared
`AdapterRelease` wire type. Never place credentials in this file. Unknown fields,
duplicate keys and oversized files fail readiness without echoing their values.

The deployment entry point requires a signed, current native release-job decision.
`ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN` names the registered deployment connection,
while `ADP_TOOLS_DEPLOY_ROLE_ARN` names the distinct, reviewed workflow mutation
role. Both must be bound to the target account by the native job; neither is the
adapter pod's service-account role. The deployment entry point
rechecks authority before source upload, build start, Kubernetes apply, and
writing stage evidence; owner loss during build polling attempts to stop the
build. CodeBuild qualification produces an immutable artifact; the guarded
native publisher owns registry mutation. The unresolved adapter manifest entry
must remain disabled until operations configures the reviewed target and deploys
the core guards and native gateway endpoint.

Image qualification archives only the adapter Dockerfile, buildspec, package,
lockfile, publisher and shared tool helpers from the pinned source commit.
Gateway and façade sources are not uploaded with the adapter build. This
source-scope check does not replace the built-image SBOM and runtime readback.

The renderer does not provision IAM, network policy or private-edge authority,
apply the manifest or prove the deployed target. The native release integration
owns those operations and their readback. Story #8448 adds authenticated core
admission and adapter observation; #8449 and #8450 add vendor execution; #8453
owns the full coordinated handoff, isolation/load qualification and rollback.
`docker_reported_image_size_bytes` records `docker image inspect .Size`, not
compressed registry bytes or a comparable uncompressed image size across Docker
storage drivers. Earlier raw evidence retains its original field and should be
read with this limitation. Compressed registry size and production capacity remain
live release measurements owned by #8453.

## Compatible release and recovery (#8453)

This procedure is for the **reviewed, integrated** Atlassian release, not the
foundation service alone. Its current `/readyz` still reports
`execution_enabled: false`; a successful adapter job does not make the new
connection available. Do not advertise MCP access until the gateway, façade,
interceptor, schema/migrations, UI and CLI are compatible, the four routes are
verified, and managed Identity consent/read works on the served target. Keep
existing Jira, GitHub and Slack behavior available while preparing the handoff.

For a normal user on that qualified release: open **Settings → Connections →
Atlassian (Jira)**, choose **Connect**, approve the provider consent and return
to ADP. Select the available site and permitted tools when the integrated UI
offers them. If setup is required, unavailable or still shows only the older
Jira connection, ask a platform administrator; do not add a PAT, register an
OAuth app, configure an MCP server or paste a token into ADP. **Reconnect**
and **Disconnect** use the same Connections card. The current foundation and
older client are not evidence that this new journey is deployed.

Before an administrator authorizes a protected release, confirm the reviewed
source commit and prior compatible manifest, the registered deployment
connection and target account, private provider application configuration,
Identity callback/return and policy, additive gateway migration and recovery
consumer, and preserved existing credentials and pending operations. Keep real
account IDs, callback URLs, secrets, route mappings, receipts and readbacks in
the protected operator store, never in this document. Do not set up a separate
user-token store or refresh worker. Native deployment approval and the held
release lease still apply; a local plan does not authorize dispatch.

The release coordinator first reconciles automatic runs and their independently
observed receipts with `modules/tools/scripts/reconcile_release.py` (read-only,
`plan-only` output). Select each missing component from the *same* reviewed
source and confirm its protected job and independent served-state readback:

| Component | Existing workflow | Actual trigger/condition |
| --- | --- | --- |
| Gateway, installed CLI and additive migrations | `gateway-deploy.yml` | `push` to `main` for selected source/CLI/migration paths or reviewed `workflow_dispatch`; the gateway calls `run-gateway-migrations.yml` when its backend changed or on dispatch. |
| Frontend | `gateway-frontend-deploy.yml` | `push` to `main` for frontend paths or reviewed `workflow_dispatch`. |
| Façade/reconciler | `tools-facade-deploy.yml` | `push` to `main` for façade/shared paths or reviewed `workflow_dispatch`. |
| Interceptor | `tools-interceptor-deploy.yml` | `push` to `main` for interceptor paths or reviewed `workflow_dispatch`. |
| Adapter image/service | `tools-platform-deploy.yml` | Protected `workflow_dispatch` on `main`, **first attempt**, `mode=release`, `stage=tool-adapters`, `environment=dev`, nonempty `adp_correlation`. The `adapter-foundation` job is in this same workflow; a push or scheduled drift check does not publish it. |
| Shared provider/platform targets, if selected | `shared-agentcore-tools-deploy.yml`, then `tools-platform-deploy.yml` | Review the shared receipt before the protected platform dispatch; do not infer a platform release from an adapter-only change. |

The adapter stage has no production release condition. The dedicated protected
handoff job accepts `adapter_action=handoff` or `rollback`, using the current
native release authority and the original coordinated route plan. It requires
`ADP_TOOLS_ADAPTER_PREPARATION_JSON`, a protected data-only object containing
`api_id`, `stage`, `gateway_id`, `foundation_state` (`bucket`, `key`),
`adapter_namespace`, `workloads` (the gateway and façade namespace, deployment
and container selectors), and the reviewed `release` tuple. Do not supply target
IDs, deployment IDs or an operator-built handoff plan. The preparation producer
derives the retained ingress, checks the deployed adapter source and image,
preserves the Knowledge target's explicit `execute-api` signing service, and
creates independently checked private targets and immutable baseline/candidate
stages. Preparation selects only the compatible legacy baseline; the guarded
switcher fences and drains work before activating the adapter.

Rollback loads the original encrypted preparation plan and checks its protected
configuration, current core transition and independently served routes; it does
not repeat candidate preparation or require healthy adapter pods. Restoring a
legacy baseline still requires the gateway and façade to roll out and read back
the new release generations before switching routes and reopening admissions.
It does not reconfigure or restart the dormant adapter. Forward activation and
restoration of an adapter-backed baseline retain adapter rollout checks. If core
rollout or route verification fails, admissions remain fenced for a guarded retry.

Preparation journals remain encrypted in the selected foundation state bucket
under `tools/adapters/handoff/<api-id>/<source-revision>.json`. Retain these across
interruption. The Knowledge-edge Terraform module independently reads the
selected prepared deployment and keeps its routes, API policy, endpoint policy
and stage during later applies. Its reader requires Python and the AWS CLI with
read access to the selected API and endpoint. Conflicting Knowledge integration
changes fail closed and require coordinated reconciliation rather than resetting
active connector routes. The separate opt-in preparation IAM policy belongs only
to the dedicated handoff role and requires exact reviewed network selectors.

The job publishes signed dispatch context before mutation and a generic delivery
receipt only after final route and core-transition readback. That receipt is not
provider/client acceptance. Both context and generic delivery receipts use the
existing `s3-oidc-v1` transport; GitHub artifact storage is not required for the
handoff receipt. The staged delivery publisher includes its schema and isolated
Pydantic dependency from the reviewed workflow definition.

Private adapter routes require the protected edge proof and the exact configured
AWS role. API Gateway forwards role credentials as an STS assumed-role ARN;
the adapter checks its partition, account and role name before accepting the
session. A session of another role or account is refused.

If activation finished but evidence publication failed, recover it with the
protected `rollback` job using the original image/source and retained journal.
The current authorized job may take over a fully verified active transition
within the same connection and mutation roles; it cannot take over an in-flight
transition or another plan. The previous owner remains recorded. The handoff
job runs controller code from its reviewed workflow commit while verifying the
older artifact source independently.

After rollback, publish the replacement adapter, then prepare a new handoff.
Preparation reads the consistent disabled release from the runtime maps, retains
the exact restored deployment as its baseline, and advances both generations by
one. It refuses an active or inconsistent baseline. A rollback to legacy does
not require the dormant adapter image, so a replacement image may differ from
that retained legacy release; active-to-active image replacement remains refused.

In the protected evaluation workflow, supply legacy
fixtures through `ADP_TOOLS_ATLASSIAN_REGRESSION_JSON` and matching ordinary owner
sessions through `ADP_TOOLS_ATLASSIAN_REGRESSION_SESSIONS_JSON`. The runner binds
them to the selected release and uses independent provider readers. Unavailable
consent, original refresh/request-count predicates and transition-specific
client/grant/effect evidence remain pending; passing other scenarios never
establishes handoff parity. Session tokens are removed without deleting retained
operation journals.

Do not retry a failed release as a new job, rerun a failed attempt or
manually `kubectl apply` to bypass the protected gate. Preserve the previous
immutable images, route manifest, generations, credentials and pending-work
ledger. For an interruption, fence new connector admission, observe the latest
run, and provide the coordinator with owner-only previous-manifest and fresh
before/after runtime readbacks plus the pinned gateway and façade images. Its
`protected-apply-required` decision proposes review, not a dispatch or proof
that the old group is restored. An uncertain registration or Jira write must
be reconciled from its original operation; never replay the provider effect.

On a failed handoff, keep the group fenced while the approved operator restores
**all four** prior connector routes/enablement and exact prior artifacts through
the existing protected component procedures. The façade and interceptor
workflows accept `rollback_to_digest` and `rollback_to_version` respectively;
`gateway-deploy.yml` has `product_rollback`, but these are not a substitute for
a coordinated adapter/route rollback. If the approved prior group cannot be
restored with available protected mechanisms, do not reopen it: record the
unresolved component and escalate to the release owner. Independently read back
all four destinations, digests, credential references and pending work against
the pinned previous manifest **before** releasing the fence. Do not delete
shared infrastructure or roll back additive schema while legacy rows exist.
Publication, merge or a green workflow receipt alone is not served-state or
provider acceptance; the final combined campaign supplies that evidence.

### Source-pinned role walkthrough (SCN-03)

Use this checklist only against the **final reviewed release**, not a simulated
foundation target. The release owner records the full 40-character source commit,
workflow definition commit, requirement revision, prior and candidate manifest
digests, protected target identity and reviewer in a private versioned record.
Keep the real account, provider/client identity, route URLs, credentials, exact
receipts and evidence in that record; publish only sanitized outcomes. An
independent operator, not the release author alone, signs off each row. Record
`blocked` or `not-run` rather than `pass` if the qualified target, access,
policy/consent or an independent observation is unavailable.

Before using the workflow table above, read each workflow **at the recorded
source**, not from a moving checkout or a green run. From the repository root,
with `REVIEWED_SOURCE` set to the full approved commit already present locally,
the following command is read-only and prints the adapter definition for review:

```bash
git show "$REVIEWED_SOURCE":.github/workflows/tools-platform-deploy.yml
```

Repeat for `gateway-deploy.yml`, `run-gateway-migrations.yml`,
`gateway-frontend-deploy.yml`, `tools-facade-deploy.yml`,
`tools-interceptor-deploy.yml` and `shared-agentcore-tools-deploy.yml`.
Compare each recorded dispatch mode, stage, environment, definition revision
and rollback input to that pinned definition and the independent protected
workflow receipt. A changed definition or a missing source pin stops the
walkthrough; neither this command nor the table authorizes dispatch.

| Reviewer journey | Action on the pinned release | Independent result to record | Cleanup / failure |
| --- | --- | --- | --- |
| User | In Settings → Connections, use the **Atlassian (Jira)** card to Connect, complete managed consent and return, select the permitted site, then make one authorized read through the served UI and installed CLI. Check Reconnect/Disconnect status without pasting credentials or configuring MCP manually. | Record the observed served UI/CLI revision, connection status, selected site and provider read result separately from the workflow receipt. A legacy-only card, missing consent or unserved CLI is not a pass. | Disconnect only the disposable fixture connection after preserving the original legacy selection; check the prior Jira account still works. |
| Administrator | Check the application/provider configuration, callback/return policy, additive migration state, recovery consumer, flags, and no ADP user-token storage. Compare workflow triggers and source/definition pins to the preceding `git show` readbacks. | Independently compare the private Identity/provider status, migration/consumer readiness and actual protected job inputs; count registrations before/after. Receipt success alone does not prove any of them. | Revoke only disposable test grants and remove only fixture-owned setup; retain the deployment client, credentials and pending recovery jobs. |
| Operator | Check all four prior and proposed route/digest records, fence admissions, interrupt an isolated apply, observe recovery and perform the authorized group rollback without stopping unrelated tools. | Compare four independently served routes and exact prior artifacts, credential references, generation and pending-work ledger before reopening. Reject stale leases, duplicate registration and unknown write replay; record a blocked row if rollback cannot restore an exact prior group. | Return fixture routes and flags to the pinned prior manifest, verify all four routes and unrelated tool health, settle fixture operations and release the fence only after the independent readback. |

Keep each result, observer timestamp, reviewer and cleanup outcome in the
private stage report. A local fixture check can validate the checklist and
pinned workflow source; only an independently followed walkthrough on the
deployed release can pass SCN-03 or AC-03 live claims.

## Controlled foundation qualification

The dev-only `adapter-foundation` job in `tools-platform-deploy.yml` runs
`deploy.sh foundation` after service readback and before publishing its native
release documents. This is part of the same guarded release, not a second
workflow dispatch. Do not invoke it with ambient credentials outside that job.

Before enabling the stage, operations must provision the separate
`adp-tools-<environment>-adapters-foundation-check` CodeBuild project and its
read-only runtime-image permissions, versioned source/result storage and pinned
PostgreSQL image. The workflow mutation role also needs the reviewed project
dispatch/result permissions and read access to the gateway deployment in the
selected cluster. Set `ADP_TOOLS_FOUNDATION_GATEWAY_REVISION` in private deployment
configuration to the reviewed, deployed gateway commit. The registered
connection, physical target, adapter image and native lease remain required;
the qualifier does not create or replace any of them.

`foundation_release.py` reads the running gateway and adapter images, checks
their source bindings, and supplies the exact digest references to CodeBuild.
`foundation_runtime.py` pulls those images without rebuilding them. It mounts
only test fixtures into the gateway, installs locked test dependencies in
gateway-only executable temporary storage for native PostgreSQL drivers, and
keeps `/tmp` non-executable. It uses a uniquely named internal Docker network and
ephemeral PostgreSQL database. Provider and Identity transport are substituted;
no live user tokens, database credentials or provider access enter these tests.

Four real test cases emit measured registration counts, role/policy responses,
packaged-worker restart state, competing-lease outcomes and distinct fixture
identities. Adapter probes verify contract/readiness endpoints and reject forged
internal requests. The runner checks packaged source hashes and forbidden
imports, compares database names before/after, removes its own containers and
network, and independently reads back cleanup. Failed or skipped tests, missing
measurements, leaked resources and changed runtime bindings cannot produce a
passing summary. Failed attempts retain their bounded result when available.

The native job verifies the version and checksum of the CodeBuild result, checks
the lease throughout the wait, and repeats runtime readback before producing
`atlassian-stage-summary.json`. The summary states `controlled_integration`,
`fixture_source: generated` and `provider_live_attestation: false`. Publication
signs it as `atlassian-foundation` for the exact repository, workflow and original
run attempt. An evaluator must bind the same operations predecessor and workflow,
require the `adapter-foundation` job, and request logical artifact
`tools-live-evaluation-{run_id}-{run_attempt}` at `atlassian-stage-summary.json`.
Its storage target comes from the verified deployment receipt, not the artifact.
The flow owner must update an older `tools-live-evaluation.yml` binding before
using this same-run transport.

Run the contract/driver tests from this directory with
`uv run --frozen --group dev pytest -q tests/test_foundation.py`. They substitute
Docker/AWS command responses and are not evidence of a deployed image run.
After merge and prerequisite integration, w03b operations owns the actual
protected-stage execution and cleanup evidence. Coordinated cutover and final
provider-live acceptance remain with #8453 and #8454 respectively.

### Identity stage handoff (#8723)

The proposed repository target `tool-adapters-identity` is an accepted-plan
`operational_release_binding` with a [generic v2 delivery contract](../../../docs/delivery-flow/generic-delivery-contract.md),
not a new infrastructure account or a second provider proxy. Set its workflow to
`.github/workflows/tools-platform-deploy.yml`, required jobs to `guard` and
`adapter-identity`, and business inputs to `mode=identity`, `stage=tool-adapters`,
`environment=dev`. The gateway revision comes from the same approved
`adp_source_revision` used for the adapter; no separate gateway input exists.
Select the **same reviewed source revision** for the native gateway operations
release and Identity adapter release. The guard checks the reviewed source
against main ancestry; the driver compares its ECR source tag with the
running gateway image digest and pod image IDs. Do not select the old mutable
foundation gateway variable. Set `resource_kind=eks-namespace` and
`resource_id=<cluster>/<adapter namespace>` in the accepted delivery contract.
The existing protected cluster and namespace settings supply the runtime values;
the reader compares them with native readback. Do not place their real identities
in this public document.

Set `required_checks` to `8448.AC-01`, `8448.AC-02`, `8448.AC-03`, `8448.AC-04`
and `identity-fixtures-clean`; set `cleanup_required=true`,
`evidence_artifact=delivery-evidence`, and pin the accepted contract's
`baseline_revision` and `definition_revision`. The engine supplies
`adp_contract_digest`, correlation and source/definition transport inputs.
The protected Identity job publishes a signed original-dispatch context before
publishing or applying images, so a terminal failed attempt can be reconciled
without accepting its unqualified result. The reviewed definition input can
precede the running main commit only when the workflow bytes are identical.
Successful protected runs publish the independent list-style four-row summary
as `atlassian-identity-<run_id>-<attempt>/atlassian-stage-summary.json`, the
generic `delivery-evidence-<attempt>/delivery.json`, and a separately signed
private `atlassian-identity` repository artifact. The repository evaluator must
require the Identity job, artifact and provenance; it must not treat the old
foundation archive as Identity proof. The existing generic job guard needs a
v2-aware server-side authorization for the selected release and held lease before
Identity can dispatch; never remove the client guard to work around it.
`uv run --frozen --group dev pytest -q tests/test_identity.py tests/test_identity_dispatch_context.py` checks the reader and dispatch context
without cloud access; only operations can run the protected image qualification,
and the coordinator owns correlated w04 evaluation after the enabling merge.

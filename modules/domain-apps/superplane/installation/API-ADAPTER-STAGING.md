# Explicit API adapter configuration — app-only installer proposal

26 September 2026. Approved design; implementation is developed separately from the native profile installer. No installation or cloud/configuration changes are authorized by this document. Parent confirmed the live API has none of `ADP_GATEWAY_INTERNAL_URL`, `ADP_GATEWAY_INTERNAL_API_KEY`, `SUPERPLANE_OPERATION_GATEWAY_URL`, `SUPERPLANE_OPERATION_GATEWAY_REGION`. Preserve the current management-only installation until a separately reviewed complete upgrade is executable. PR #6401 covers native profile compatibility only and must not silently absorb this wider change.

## Proposed scope

Add an optional closed `api_adapters` mapping to the maintained private installer environment. Omission preserves existing management-only behavior and manifests. A complete map can be staged for management mode while its API remains management-only; full mode requires complete configuration and existing production capability checks. Do not create Gateway keys, IAM policies, shared domain bindings, run grants or provider authority in this feature.

Suggested shape (schema illustration only; null values are invalid and deliberately supply no target):

```yaml
api_adapters:
  vault:
    url: null
    secret_key_ref:
      name: null
      key: null
    transport:
      namespace: null
      service: null
      port: null
  dispatcher:
    endpoint: null
    region: null
    role_arn: null
    api_id: null
    stage: null
```

Use the `vault.transport` form only for the existing internal Gateway Service selected by the operator. Validate its URL host and explicit port against that exact Service and namespace. Do not guess a `bedrockgateway` URL from a label. If actual live transport is a private HTTPS API/ALB instead, replace this schema with one explicitly reviewed alternative using bounded resolved destination IPs; do not accept arbitrary combinations of Service names, URLs and CIDRs. Resolve the concrete live transport before coding the final schema. TLS should be required unless the existing reviewed internal service contract specifically uses HTTP confined to the cluster; record that exception as part of the selected transport rather than accepting arbitrary cleartext external URLs.

Dispatcher endpoint must be the actual API Gateway invoke origin/stage for the protected IAM route, with exact region, API ID and stage matching the URL. Custom domains add mapping verification and are outside the smallest initial slice. Reject credentials, query, fragment, extra path suffixes, IP literals, redirects and mismatched region/API/stage. No defaults derived from account names, management origin, executor authority endpoint or environment variables.

The secret reference selects an **existing Kubernetes Secret/key in the API namespace**. Render it directly as required `valueFrom.secretKeyRef`; the installer must never fetch its value, copy it into another Secret, put it in argv/receipts/manifests, or grant the API permission to enumerate/read Secrets. If only a Secrets Manager value exists, its projection is an explicit prerequisite owned by the existing credential installer. Secret metadata UID/resourceVersion may be recorded for drift/recovery, but never values or credential-derived hashes. Startup fails on absent key. Rotations retain the existing Secret owner; the upgrade replaces the API pod as needed for env-projected values.

## Exact current consumers

- `src/superplane-api/app/adapters/adp_vault_client.py:22` posts only `/internal/v1/credential-evidence`, using `X-Internal-Api-Key`. `build_vault_client` requires both existing settings. This is evidence access, not executor credential delivery. Gateway `src/internal/vault_evidence_routes.py:345` uses its existing `verify_internal_or_irsa` boundary.
- `src/superplane-api/app/adapters/operation_dispatch.py:75` signs POSTs with API workload credentials, SigV4 service `execute-api`, and explicit signing region. Paths are `/internal/v1/controller-execution/{producer-readiness,verify-run,dispatch}`. Requests do not carry an execution run token. `OperationDispatcher.ready` already verifies domain/org/ADP-org identity in the readiness response.
- Gateway `src/internal/domain_operation_runtime.py:30` additionally checks current IAM-derived registry identity and `domain:operation-producer` scope. `src/internal/domain_operation_store.py:27` selects a protected `ADP_DOMAIN_OPERATION_BINDINGS` entry with domain org/ADP org, producer/worker registry IDs, DB schema/secret, queue, worker namespace/SA/container/image allowlist, repository and observation endpoint. IAM invoke permission alone cannot establish this readiness.
- `installation/manifests.py:647` currently assigns a derived control-plane role in full mode; `infra/control-plane/irsa.tf:34` trusts both API and controller SAs and its policies include no `execute-api:Invoke`. A role-name match is insufficient. The explicit dispatcher role must override the API annotation only; do not change controller or SkyPilot roles.

## IAM and workload identity

The dispatcher role must already exist in the selected account. The installer reads its trust, attached/inline policies and permission boundary and records reviewed identity facts. Trust must bind the actual management cluster OIDC provider, audience `sts.amazonaws.com`, exact API namespace and `superplane-api` ServiceAccount; reject wildcard subject/other-service trust for the dedicated producer role. Its invoke permission is limited to the selected API/stage POST paths above, not `execute-api:*` or arbitrary routes. Existing non-dispatch permissions require explicit inventory/review, not implicit inheritance from the controller/provider role.

IAM policy inspection/simulation helps diagnose denial but cannot prove effective authorization in the presence of SCPs, resource policies and registry state. Post-rollout, inside the actual API pod, call bounded STS GetCallerIdentity and require the observed assumed-role account/name to match the selected role; report sanitized ARN only. Then call the existing non-mutating `producer-readiness` and validate its exact org response. Never use dispatch as a health probe. Verify no static AWS credential env/file override, node-IMDS fallback or copied executor/run credential is present. Render `AWS_EC2_METADATA_DISABLED=true` and regional STS configuration so the configured role and egress have deterministic behavior. Respect IRSA webhook token projection; do not enable a generic Kubernetes service token merely to obtain AWS identity.

The shared Gateway owner supplies current registry and domain-operation bindings; this app-only change reads/verifies them through supported endpoints. No install-time registry insertion or shared config edits. Missing bindings produce a named readiness gap and preserve management mode. Producer role access must not accidentally provide `domain:operation-executor` or provider-secret delivery scope.

## Network policy must match real traffic

Current `installation/manifests.py:598` allows API egress to domain-owned peers on 8000/8081/9090/46581 and broad TCP 443/5432 excluding link-local. Gateway is added to API **ingress**, but `peers[:2]` excludes it from API egress. Thus a non-443 internal Gateway service is currently blocked even after setting vault URL/key.

Add one API-only egress rule for the selected Gateway namespace and actual Service-selected pod labels on its real target port. Keep namespaceSelector and podSelector together; a namespace-only rule widens to every pod there. Confirm Service selectors/ClusterIP pre-DNAT behavior with the existing AWS NetworkPolicy engine, including ownership labels. Do not add blanket API→all-port Gateway namespace access or change monitor/controller/SkyPilot policies.

Dispatcher and regional STS need TCP 443 and DNS. Existing broad 443 egress technically covers external destinations but does not prove routing, API Gateway/resource policy, TLS or IAM readiness. Record that existing exposure rather than claiming the new installer has destination-isolated these endpoints. If the desired deployment requires narrowed egress, use actual approved private endpoint/subnet CIDRs and their lifecycle owner; do not pin public DNS answers as permanent allowlists or invent Service selectors for an external API. Preserve management DNS and database policies. Do not broaden DNS/5432/network exclusions as part of this feature.

Validate actual traffic under the API's rendered policy: selected Gateway evidence endpoint reachable; neighboring unapproved service/port blocked; producer readiness and regional STS reachable with TLS; external redirect refused. Use harmless supported read requests and record denied/unavailable distinctly. The general preflight echo-pod test proves enforcement, not these destination-specific paths.

## Preflight sequencing: an additional source integration gap

`installation/runner.py:409` runs full image capabilities with an empty probe environment; cluster equivalent at `:576` passes no full-mode values. Yet `app.installation.capability_details` calls real `compose()`, which needs configured DB/vault settings. Rendering final API env alone will therefore still leave full image preflight unable to establish all four ports.

Implementation must treat this explicitly, preserving both checks:

1. Isolated image/contract preflight remains non-mutating and does not claim live authority. It must exercise the actual configured composition, with existing secret references supplied only through a separately declared restricted probe mechanism. No permissive adapter injection, hardcoded capability booleans or “expected unavailable therefore success” change. Do not place real shared-key or IAM identity into the generic unprivileged temporary image-probe namespace by default.
2. Actual installed API private verification exercises composition, configured DB/shared schema, existing vault evidence and SigV4 producer readiness under the API SA/policy. Non-mutating target credentials and exact requests must be reviewed. These checks must complete before public full activation/workload admission.

Resolve the restricted probe mechanism before code: either extend a dedicated API-namespace preflight Job to reference the already-owned Secrets with no generic Kubernetes token and no dispatch ability, or separate packaged-image rejection checks from the still-mandatory installed configuration gate. The latter changes verification staging and requires an explicit reviewed transition that keeps the public/API management gate active until live adapter checks pass. Merely skipping current four-port preflight and relying on `/health` is not acceptable. Existing management-only receipts remain valid and their execution path must be unchanged.

## Files and bounded work

New `installation/api_adapters.py` should own closed schema validation, API env/SA/policy projection and sanitized verification response checks. `config.py` adds only the optional named field. `manifests.py` calls the helper and avoids conflicting derived API role assignment. `runner.py` records reviewed non-secret adapter targets, metadata and verification, wires the approved probe sequence, and includes changes in plan/receipt identity. Environment example/README document omission behavior and exact prerequisites.

`AdpVaultClient` currently constructs default httpx transport without explicit `trust_env=False` or a strict URL policy. The optional installer URL restriction prevents accidental destination selection, but proxy environment injection still changes the network destination. In this app-only slice explicitly disable ambient proxies and redirects for the evidence client, retain bounded timeout, and test no credential-bearing redirection. Do not change executor delivery transport or trust semantics.

Preserve the four composition ports and existing approval/admission logic. Do not add a mode switch that starts arbitrary dispatch in management-only mode; stage configuration while admission remains gated. Before switching modes, inspect outstanding admitted outbox rows because the existing startup path can start a configured dispatcher even in management mode. Configuration activation must not unexpectedly resume previously admitted work; any pending dispatch is part of the concrete upgrade review.

## Required tests and live evidence

- Omitted config yields identical management-only API env/SA/network policy; no extra Secret/role/probe reads. Full missing/partial config refuses clearly.
- Strict schema rejects extra keys, inline secrets, wrong namespace/key syntax, arbitrary URL/path, endpoint-region-API mismatch, wrong-account/wildcard role, and incompatible transport destination.
- Rendered API alone gets exact four env settings, required Secret key ref, role annotation and selected Gateway egress; no secret bytes in plan/receipt/log/argv. Other workload roles unchanged.
- Actual API composition with fixture transport proves vault client and dispatcher are configured; evidence path, signature service/region and exact producer response binding are checked. API service authority remains distinct from human spend approval.
- Role trust/policy fixtures cover wrong OIDC/SA/audience, extra principals/routes, missing permission and denied registry. Runtime STS mismatch and producer-readiness refusal never produce ready.
- Network fixtures plus authorized cluster traffic tests cover selected service/target port, unrelated pod/port denial, real service-selector behavior, TLS/redirect handling and DNS.
- Rotation/drift/restart/rollback preserve source Secret ownership, route fencing and known management service availability. A failed full activation cannot claim management mode is still live unless observed.
- Remote domain/controller/installer tests validate code. Parent's live installation verification validates effective identity, network, existing registry and exact adapter targets. GPU spending remains behind separate explicit workload target/budget/cleanup approval.

## Inputs still required

Actual vault service URL, namespace/service/target port and transport security; existing API-namespace Secret name/key and owner; exact producer API Gateway invoke URL/API ID/stage/region; existing bounded API role/trust/policy and registry identity; corresponding existing domain-operation binding readiness; chosen restricted preflight mechanism. None is filled from inference in this plan. Parent may collect these read-only; this design authorizes no creation or mutation.

## Chosen staged verification sequence — implementation-ready refinement

This section supersedes the earlier alternatives for preflight sequencing. Parent's read-only live findings establish Gateway Service `adp-gateway/bedrockgateway`, Service port `80`, target port `8080`, selector `app=bedrockgateway`, and producer endpoint `https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev`. No key has been read. Proposed vault base is the service DNS name `http://bedrockgateway.adp-gateway.svc.cluster.local:80`, derived from those observed Service facts; parent must confirm this matches the installation's approved internal HTTP trust boundary before use. Producer signing region is `us-east-1`, API ID `59o2rakc50`, stage `dev`. Secret name/key, API role and protected registry binding remain unresolved; this update supplies none.

### 1. Image-only contract check; no live readiness claim

Add an `app.installation image-contract` command that imports the installed actual API/executor/Harness modules, confirms expected versioned entrypoints and supported contract shapes, and exercises only pure validation/rejection paths that require no credentials or database. Its output is a closed report such as `image_contract_version: 1`, supported contract versions, source/release IDs, `configuration_verified: false`, `authority_verified: false`, `production_ready: false`. It must not emit the four live `capabilities: true` fields or install substitute adapters. The existing remote tests establish detailed semantic conformance; this packaged-image check establishes that the selected image contains their required entrypoints.

Use this command for the isolated Docker `--network=none` and generic no-identity ClusterProbe image phases. Do not configure these probes with fake vault keys, placeholder DSNs or real broad shared credentials. Preserve the isolated Node/profile validator `VERIFY_PROGRAM` checks, since those consume reviewed non-secret profile bytes and pure plan validation. Record separate `image_contract` and `profile_validation` receipt entries. Keep management-only install without `api_adapters` on its existing unchanged management-capabilities path.

The existing four-port `capability_details` implementation remains intact. Move its full-mode installer invocation to the actual configured staged API verification below; removing it from image-only execution must not remove it from required activation gates.

### 2. Stage configuration on the installed API with two explicit safety gates

Introduce an exact boolean `SUPERPLANE_OPERATION_DISPATCH_ENABLED` consumed by `Composition.start_dispatcher` and any code path that starts/drains/recover-dispatches that composed dispatcher. Staged manifests explicitly set it `false`. Full activated manifests explicitly set it `true`. For deployments that omit the new optional adapter mapping, preserve the current behavior/default; do not silently alter another installed operation host. Reject invalid boolean strings. Readiness must report the effective value rather than whether a dispatcher object exists.

Staged API also stays `SUPERPLANE_MANAGEMENT_ONLY=true`. However, **management mode alone is not a paid-admission fence**: current `management.py:MANAGEMENT_ROUTES` includes serving create/delete, workspace creation and lifecycle continuation routes. `main.py:179` currently starts a dispatcher in this mode. Therefore staging must gate new paid admission explicitly while allowing safe reads, previews and existing management administration. The same activation setting can provide the refusal, but it must be enforced at the actual central paid `HarnessOperationFacade.open_operation` boundary (and audited direct admission paths), not merely in UI routes or catalog flags. Return a stable unavailable/staged error before budget reservation/admission/outbox insertion. Keep existing approval and human grant checks unchanged; staged approval storage may remain available, but it must not admit work.

Construct the real dispatcher transport even while disabled so its non-mutating `ready(org_id)` call can be verified. Do not call `dispatcher.start`, `drain_once`, `deliver`, `recover_once`, or `dispatch` in verification. Tests must prove all existing management/full lifespan start paths respect the effective guard. Inventory existing pending/active admitted work before staging; if an upgrade would disrupt it, refuse that transition until the approved operation owner establishes a safe state. The flag does not cancel an already executing worker or revoke prior authority.

Apply the same admission-availability check at domain service entrypoints **before** provisional domain quota writes as well: `deployment_operations.create` reserves deployment GPU quota before calling the facade. A facade-only refusal would prevent shared admission yet leave a staged Pending deployment/quota reservation. Audit workspace/lifecycle creation for analogous pre-admission durable writes. Central facade enforcement is defense in depth; tests assert unchanged domain quota/intent rows as well as shared budget/outbox rows.

Stage only API configuration/identity/policy while preserving the existing management route. Do not remove the management service, overwrite source Secrets, activate an executor assignment or enable legacy reconcilers. Apply reviewed database migration separately if required by the selected image. Use the installer's existing fenced object and route receipt mechanics, recording `adapter_stage: verifying`; a crash/resume must remain disabled rather than infer activation from successful Deployment rollout.

### 3. Verify the actual staged API

Run a dedicated bounded installer verification command **inside the actual API container**, using its configured env, mounted existing Secret reference, DB transport, IRSA projection and effective NetworkPolicy. Use an explicit read-only command, not importing `app.main` in a way that starts its lifespan/dispatcher. The existing live process remains management-only with dispatch/admission disabled.

Require all of the following, with separate evidence fields:

- Exact source/image/release/DB head and non-secret selected adapter target agreement; observed effective `management_only=true`, `dispatch_enabled=false`, and paid admission disabled.
- Existing `capability_details` returns all four real composed ports and retains its existing smoke limitations. This proves correctly shaped unauthorized refusal, not live positive credentials or human authorization.
- Actual `STS GetCallerIdentity` from the configured API workload matches the selected account and role; region/role trust and no static fallback checks remain as above.
- `OperationDispatcher.ready(actual_domain_org)` returns the existing exact domain/org/ADP-org binding and ready value. This is a readiness POST, not paid dispatch. Record no run or lease as created.
- A separate known-credential **metadata-only** control verifies the configured vault evidence path against an existing authorized org/workspace/principal and exact opaque credential reference/report binding selected by the parent. Use current authenticated installer/user context and existing grant checks; do not manufacture the human principal from environment strings. The response must match expected owned credential identity/version/service/current state without returning raw credential material. A dummy unknown credential yielding 403 proves only rejection and cannot satisfy this positive control. If no supported read surface can supply this safely in management mode, add a narrowly authenticated installation verifier invoking the same evidence adapter and grant checks; do not expose arbitrary internal evidence access publicly. A legitimate metadata refusal/unavailable result leaves staging incomplete.
- Actual request to Gateway Service port80 reaches pod target8080 under the selected policy. Render API-only egress to namespace `adp-gateway` + selector `app=bedrockgateway`, port8080, and account for Service/pre-DNAT port80 behavior using the observed CNI policy implementation. Test observed Service path, not just direct pod IP. Do not widen to arbitrary Gateway ports/pods. DNS, producer HTTPS443 and regional STS443 must be observed under the same API boundary.
- Existing authenticated public management reads and unrelated ADP health still pass. Explicit negative paid-create test reports staged refusal with no additional admission/reservation/outbox rows; it must not be an otherwise valid spending request sent speculatively.

Capture no Secret bytes, authorization headers, DSNs or bearer credentials. Verification failures preserve management-only/disabled stage and leave the exact reason in the private receipt. Do not claim the prior management service survived if its independent read probes fail.

### 4. Enable only the verified exact stage

Before full activation, recheck source/release, environment digest, profile digest, Secret metadata generation, selected role/SA identity, target org/workspace/cluster and all stage observations. Any drift or expired evidence requires re-verification. Record an exact activation intent in the installer receipt before mutation. Require prior reviewed plan authorization to cover this transition; no timeout or elapsed period implies approval.

Render full API with `SUPERPLANE_MANAGEMENT_ONLY=false` and `SUPERPLANE_OPERATION_DISPATCH_ENABLED=true` only after the stage's actual adapter/identity/network/metadata gates pass and the existing full workspace/executor/profile prerequisites are satisfied. Startup retains the existing live four-port gate. Full readiness requires exact image/DB/profile, verified execution bindings, and dispatcher readiness again; publication uses the existing conditional route protocol. Do not equate enabled dispatch with permission to create a new paid operation; actual workload still needs its own immutable preview/human approval/budget/cleanup limits.

If rollout/verification fails, keep or restore the known stage configuration with dispatch disabled using receipt-bound object identities, then independently verify management reads; keep public full route disabled under existing compensation rules. Never automatically restart old pending dispatch during rollback. Cross-schema rollback constraints still apply.

### Scope and required regression additions

- New source-only image-contract output cannot be consumed as live `capabilities`; missing adapter configuration still fails staged/full readiness. No fake-key production-ready fixture or feature flag weakens `capability_probes`.
- Exact three configurations: legacy management omitted (unchanged), configured verifying stage (management true/admission+dispatch false), activated full (existing gates true/dispatch enabled). Restart/resume at each point preserves state.
- Management-mode serving/workspace/lifecycle admission attempts cannot exploit the existing route allowlist; no reservation/outbox row or recovery dispatch while disabled. Explicit read-only dispatcher readiness remains callable.
- Real positive metadata fixture plus wrong org/workspace/user/revoked grant/control credential failures, unknown credential refusal, role mismatch, wrong producer binding, Service network denial and proxy/redirect handling.
- Failure after stage apply, between verification and enable, during full rollout and during compensation never silently enables dispatch; setting/profile/Secret generation drift invalidates retained proof.

Implementation should remain a separate app-only PR after review of this staged design and concrete missing Secret/role inputs. No code has been written for this adapter feature.

## Rollout clarification

Uninterrupted management routing is not required. Use the existing installer
`execute()` route-disable, rollout, verification and compensation protocol. Stage
the API internally with admission and dispatch disabled, verify the actual API,
then activate and pass full verification before existing route publication. A
failure leaves the route disabled and restores the disabled API stage when the
recorded object/schema boundary permits it. Report management availability only
after independently observing it; a disabled public route is not availability.
This clarification supersedes earlier language requiring preservation of a live
management route or a separate transition engine.

## Metadata transport precision after review

The installer uses authenticated EKS API `PartialObjectMetadata` negotiation with
no full-object Accept fallback, rather than kubectl output filtering. This avoids
requesting Secret `.data`/`.stringData`; it does not claim that metadata is incapable
of containing legacy embedded secrets in annotations. Metadata responses stay
private, annotations are discarded without logging/persistence, and receipts keep
only UID/resourceVersion. Existing Kubernetes Secret-get RBAC remains necessary.
This precise boundary supersedes any earlier absolute claim that no sensitive
metadata bytes can be received. It introduces no proxy or credential broker.

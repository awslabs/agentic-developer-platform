# Atlassian MCP deployment configuration

The gateway does not require users to enter a personal access token, create a
developer app or edit an MCP configuration. Its provider setup uses a protected,
deployment-owned plan. Terraform prepares two separate Secrets Manager secrets:
one for that plan and one for the dynamic client-registration state. The client
secret begins with `{"state":"unregistered"}`; the leased setup worker replaces
it after actual provider registration. Terraform ignores those later client-state
updates. Neither secret contains a user token.

The plan secret deliberately has **no default value**. The authorized release
operator must obtain the actual protected-resource and authorization-server
metadata, verify the advertised resource, issuer, endpoints and supported flows,
then store the complete plan described by `atlassian_mcp_config` in the protected
secret. Its `app_secret_ref` must name the separate client secret; `return_url`
must be the actual browser-accessible gateway return route. Do not publish the
plan, registration credentials, target URL, or provider evidence in this repo.
Only after independently qualifying that plan should the operator set the
Terraform variables `atlassian_mcp_contract_revision` and
`atlassian_mcp_metadata_sha256`. The metadata digest is SHA-256 over the
JSON-encoded pair of provider documents with sorted keys, as checked at runtime.

Terraform publishes the plan reference and the two nonsecret qualification
values to environment-scoped SSM parameters. Both maintained gateway deployment
paths render them as `BG_ATLASSIAN_CONFIG_REF`,
`BG_ATLASSIAN_CONTRACT_REVISION` and `BG_ATLASSIAN_METADATA_SHA256` in the
gateway ConfigMap. The recovery CronJob runs one leased setup or revocation tick
with the **same pinned image, service account and ConfigMap** as the gateway;
the application retrieves the secret only through its IAM role. A missing or
unqualified plan leaves setup unavailable rather than inventing a provider,
grant, policy observation or live acceptance. Deployments and IAM grants still
require their normal operations approval and independent runtime checks.

The gateway and its recovery tick share a service role with exact plan/client
secret access and environment-scoped Atlassian MCP Identity setup and token
permissions. The adapter's dedicated role has no secret access. Its isolated
user-token permissions remain disabled by default; after protected observation
qualification, the authorized operator explicitly sets
`atlassian_mcp_identity_enabled=true` for the reviewed adapter deployment.
This opt-in grants only the selected environment's workload identity and
credential provider, not direct provider credentials or token refresh. IAM
policy tests and a rendered plan do not establish that the grants are deployed.

For the manual gateway IAM rollout, AWS managed-policy descriptions cannot be
changed in place. Preserve the existing `agents` description exactly and use
the existing **Gateway Infra Apply** workflow on a reviewed main revision. The
operator first scopes `apply_targets` to
`aws_iam_policy.gateway_role["agents"]` (relative to
`modules/gateway/infra`). Independently read the actual IAM default policy
version and verify that it contains the retained GitHub/Slack OAuth app grants
(`OauthAppExternalSecrets`, `OauthAppRegistration`, `OauthAppWorkloadSetup`,
`SlackAppSetupProgress`) alongside the existing model/catalogue grants. Only
after that readback succeeds may the operator scope a subsequent reviewed apply
to `aws_iam_policy.gateway_role["identity"]` and
`aws_iam_policy.gateway_role["data"]`, moving the existing OAuth statements
and adding the Atlassian setup/secret grants. Their role attachment resources
are `aws_iam_role_policy_attachment.gateway_role["agents"]`,
`aws_iam_role_policy_attachment.gateway_role["identity"]` and
`aws_iam_role_policy_attachment.gateway_role["data"]`; check the scoped plan
for replacements and verify attachment/default-version readback after apply.
Do not infer full Terraform convergence from either scoped apply or change the
workflow to dispatch itself. These actions require the normal authorized
operator and target-role verification; this document is not an apply record.

The setup scenario is selectable as `SCN-8454-01` in the shared runner. Its
repository-owned driver calls the actual gateway prepare, start, complete,
site-selection and disconnect APIs. The separate observer checks the pinned
dynamic-registration client in Identity and asks Atlassian MCP for the owner's
subject and sites. The protected workflow materializes data-only JSON and
three separate human sessions in private runner files; an operator must not
provide an executable Python module. Its private JSON secret omits local file
paths, which the workflow generates, and pins the account, region, gateway,
run, owner, provider metadata digest, both observer role ARNs and their
`oidc-core`/`oidc-adapter` labels. No user supplies MCP settings or app secrets.

A prepared provider can report `consent_required` before compatibility or tenant
policy evidence exists. This permits only owner-bound consent and its independent
account/site observation; it does not activate tool execution or relax the
existing policy, release, scope and write-admission checks. A known policy denial
still prevents starting or completing consent.

The runner publishes the authorization URL into its private authorization file.
After the owner follows that URL and returns to the signed-in Connections page,
the page completes the attempt through the authenticated gateway. The gateway
compares a digest of the returned `session_id` with the retained Identity session
for that owner. The Identity session URI is not copied into the Connections-page
redirect; duplicate or missing callback parameters are refused. The runner polls its owner-bound attempt for
the resulting connection revision instead of requiring a manually written
consent receipt. A return URL alone is not evidence of consent: Identity completion
and independent account/site observation must succeed before the attempt is
reported completed. Expired, superseded, foreign-owner or changed attempts do
not advance the runner.

If Identity completion succeeds before the adapter configuration is available,
the attempt reports `observing`. The runner can retry only the pending observation
with the retained owner/session binding; the gateway does not repeat Identity
completion. Configure the adapter from the existing grant, then resume within
the original attempt deadline. Connection readiness comes from the completed,
configuration-bound provider observation rather than a deployment flag or an
asserted organization policy approval. Actual tool execution still evaluates
current ADP policy, membership, site scopes, release state and write approval.

The two dedicated GitHub OIDC observer roles are **opt-in**.
After reviewing the role-only Terraform plan on an approved main revision, the
operator may use the manual Gateway Infra Apply input
`enable_atlassian_setup_observer_roles=true`; later plans and applies retain
both roles from Terraform state or refuse a partial role state. Configure the
protected workflow role variables from the reviewed Terraform output rather
than reusing the trusted deployment role. The observer exchanges a new GitHub
OIDC token for each read and verifies the exact selected account and role.
The workflow records non-token recovery state in versioned private CI evidence
and checks independent readback before removing its local journal. A selected
connection resumes only when its previous manifest URI, version and source
revision are provided in protected resume data and the live owner binding
still matches; uncertain starts require manual reconciliation. Runner loss
before evidence publication has no recoverable receipt and cannot be called a
successful cleanup.

These code paths and controlled fixtures do **not** establish production DCR
compatibility, evidence-bound gateway readiness, actual human browser consent,
installed MCP configuration, tenant policy approval, or live refresh/revocation.
An approved target, provisioned roles, consenting accounts and independently
recorded provider and policy observations are still necessary. Never turn a
green synthetic setup fixture into deployed acceptance.

### Effective read-policy observations

The owner-authenticated `POST /api/auth/connections/atlassian-mcp/policy/observe`
route checks the current connection revision and selected site before requesting
four schema-pinned reads from the adapter. Each probe supplies an independently
prepared expected-result digest. The adapter retrieves a transient Identity
token, rechecks the binding, and compares actual provider results with those
expectations. Responses are scoped to that owner, connection, site, configuration
and read/search permission set. They never update the tenant-wide policy gate:
one user's denied site cannot block another user's connection.

Protected setup manifests may include `policy_observation` containing `tenant_id`,
`grant`, `probes`, `expected_results` and `checks`. The grant is the existing
independent MCP reader configuration, not a token or a new credential source.
`checks` contains `read` and `search` entries, each with a provider-relative `path`
and `expected_result_sha256`. The workflow creates private grant and observation
ledger paths; the manifest cannot choose local paths. The separate reader must
match the independently observed registered client, owner, provider account and
site. Unavailable, stale, foreign or mismatching results cannot pass.

Setup records these results as `policy_observation` by a **machine**. This proves
effective read/search access, not an administrator's identity or action, OAuth
redirect-domain approval, or write/delete permission. Denied observations retain
the same selected grant and mark cleanup `pending-policy`; resume refreshes the
observation rather than reconnecting or inventing approval. After fresh allowed
readback, normal cleanup and its independent verification still run. Pending
cleanup is never successful acceptance. The existing protected recovery snapshot
retains the owner and connection revision for a later runner; no manual ledger
editing is required.

The standalone setup qualifier checks read-back of an isolated AgentCore Identity
provider, a completed owner-bound Identity consent session and independent MCP
resource, subject and site observations. Controlled SDK/provider fixtures produce
only a candidate result, never live acceptance. A repository-owned live reader
and real consenting account remain necessary before the protected setup scenario
can consume this qualification.

Client-operation verification keeps a separate run-owned reservation for each
client and operation. Its direct provider reader compares only a prechosen
Jira summary/description or Confluence title/body sentinel, not unpredictable
provider-generated IDs, timestamps or links; it checks the returned resource
ID and Jira creator independently. Issue creation counts a uniquely marked
provider search result and refuses incomplete searches or duplicate matches.
The retained REST reader checks accessible-resources and an appropriate **REST**
read scope (`read:jira-work` for Jira, `read:confluence-content.all` for Confluence).
The protected client path instead uses an independent, read-only MCP observer
with the owner's existing Identity grant and the pinned v2 MCP resource. It does
not request REST scopes, create a second provider, or start another consent flow.
The MCP plan's `:agent-interface` scopes do **not** establish REST access.
For issue update, comment and transition,
the operation ledger can reserve an explicit `mutation_expected` value and save
the independently read issue/history baseline before dispatch. Confirmation and
later readback reuse that baseline after restart, require exactly one new event
from the admitted account, and compare the requested content. Issue search is
never used to count these mutations. Missing read authority, incomplete history,
changed older events, duplicate effects or an oversized baseline prevent
confirmation. Installed-client dispatch and owned-provider-fixture cleanup are
connected to the native matrix; live provider qualification remains separate.

The protected `ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_JSON` secret contains only
`run_id`, two distinct `owners` keyed by `claude-code` and `codex`, matching
`readback` bindings, and optional separately enabled `cleanup` bindings for both
clients. Each readback binding names the expected account, region, environment,
`adapter_role_arn`, `adapter_label` (`oidc-adapter`), existing workload/provider,
provider subject, selected sites, and subject/site schema digests. Its `calls`
map pins the discovered schema digest and data-only argument template for each
of `issue`, `search`, `comments`, `changelog` and `page`. Templates substitute
fixture values with objects such as `{"fixture":"site_id"}`. Every target, query
and pagination argument must use its corresponding fixture substitution;
constants, nested substitutions, extra targets and misplaced substitutions are
refused before dispatch. The observer supports only explicit site, resource,
query and pagination field mappings and checks their types against the pinned
discovered schema. Unknown argument shapes remain blocked, not guessed from
REST names. In particular, `cloudId` must always receive the requested site.

The binding step checks the role against the existing protected
`ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN` variable. This GitHub observer role
is separate from the adapter service role under test; no third reader role is
needed. Every Identity request verifies that exact role and account. Each read
checks the provider subject, site scopes and current tool schemas before calling
the fixed read-only tool inventory. Changed or missing schemas block observation.
The workflow installs the locked adapter dependencies and materializes private
per-client ledgers and OIDC profiles. Tokens stay transient; manifests and ledgers
must never contain access tokens. Legacy owner-only manifests are accepted only
by explicitly opted-out controlled fixtures, not the protected entrypoint.

Each cleanup binding contains `enabled: true`, `tenant_id`, `site_id`,
`project_key`, `delete_call` and `projects_call`. The two calls each contain
`schema_sha256` and `arguments`, pinned to observed `deleteJiraIssue` and
`listJiraProjects` schemas. Both bind `cloudId` to `{"fixture":"site_id"}`;
the delete call binds its issue identifier to `{"fixture":"provider_id"}`.
Cascading deletion is forbidden. The existing per-owner Identity provider must
already have separately consented `delete:jira:agent-interface` access for this
site. The harness cannot grant that consent. Normal adapter consent scopes and
the installed clients' tool inventory do not acquire deletion access.

Before creating an issue, the matrix checks this cleanup capability, the selected
project, provider account and schemas. It reserves a random ownership marker in
the private `jira-fixture.json` ledger and generates marked create/update content
and dependent issue IDs. Cleanup only deletes an issue whose creator, project,
site and marker match that reservation, after every attempted write has an
independently confirmed receipt and provider observation. It uses a non-retrying
write transport. Unknown writes keep cleanup pending; failed or unattempted
matrix cells never become passes because cleanup succeeded.

Finalization seals the original native dispatch before deletion. Retain the whole
private client directory across restart, including operation, fixture, transport,
receipt and finalization records. Resumption rechecks these records and provider
absence without opening another native session or resetting the finite status-read
allowance. An empty search alone does not prove absence: every known issue also
needs a direct read. Currently only an explicit resource-bound 404 is accepted;
denied or ambiguous responses remain pending. The real provider's not-found
response and consent behavior still need live qualification.

`SCN-8454-04` now exercises a bounded worker-recovery path, either alone or with
the matrix selected by `SCN-8454-0[24]`. It uses the original two approved client
plans, withholds the last planned write response, records an interrupted cleanup
consumer attempt, and recovers twice. It does not add a write or exchange another
native capability. Any reconstructed matrix result is checked against original
receipts and independently verified cleanup; a failed row is not simply relabeled.

The consumer is executable as `python -m tests.e2e.tools.atlassian_recovery_consumer`.
It requires `--manifest`, `--gateway`, `--revision`, and `--client` from the original
private run, plus `--plans` when using local state rather than a restored capsule.
It consumes only already-attempted operations and
retains a private `cleanup-consumer.json` journal. Missing or exhausted original
recovery authority leaves the work pending. Confirmed provider cleanup does not
turn failed or unattempted client operations into passes.

The protected workflow seals each client's original private state into an
authenticated encrypted capsule, bound to its owner, run, plan, release and
original call inventory. The existing immutable CI evidence uploader publishes
the capsule and a non-secret binding record. Only after version/checksum readback
and successful restoration does the loss fixture remove the original client
directory. Recovery then reopens the restored state and independently verifies
provider effects and cleanup. The final reader retrieves the original publication
again; a producer's `runner_loss` label alone cannot qualify this scenario.

The workflow uses the protected `ADP_TOOLS_ATLASSIAN_RECOVERY_KEY_B64` secret:
base64 encoding of 32 random bytes, provisioned by the operations owner rather
than by end users. Retain it for the recovery lifetime and keep it separate from
the capsules. Plaintext capabilities and login tokens must never be published.
The `harness` dependency group pins the encryption library without adding it to
the production adapter dependency set.

For a replacement runner, the operations executor supplies the selected immutable
receipts in protected `ADP_TOOLS_ATLASSIAN_RECOVERY_RESUME_JSON`. It maps each
original client to its publication `uri`, `version`, workflow `run_id`, `attempt`
and definition `revision`. The existing workflow invokes
`python -m tests.e2e.tools.atlassian_recovery_handoff` before evaluation. This
verifies the original target and owner, restores into new private directories,
persists a confined recovery-root map, and invokes the original consumer twice
without a new login or native admission. The original authenticated plans are
reconstructed from the capsule; humans do not reconstruct local files. Missing
receipts, unknown effects, expired original authority or failed readback remain
pending. Arbitrary runner loss before a verified publication is not qualified by
this controlled loss point. Do not manually delete pending private state.

Local tests cover archive tampering, fresh-root recovery, separate consumer and
reader entrypoints, and unavailable publication with modeled external services.
Actual provider/runner acceptance remains separate. The regression collector now
executes and independently replays the original legacy checkers, but its protected
live driver bindings are not complete. Four-connector scenario integration and
full-flow production also remain unfinished.

The setup observer also installs the served, source-pinned CLI in a temporary
home, reads both generated MCP configurations and verifies removal of that home.
This produces the mandatory `mcp_configuration` action without manual MCP edits.
It does not produce conditional organization-policy approval: that still needs
an authoritative policy observation, not an inference from connection readiness.

The protected workflow independently validates retained setup and client-matrix
evidence. Setup requires the private `setup-recovery.json` ledger to match the
selected owner, run, fixture kind and gateway, with every required action observed
and cleanup confirmed. Matrix validation requires all sixteen installed-client
rows, matching source and deployment revisions, distinct operation identities,
provider content/effect checks and confirmed cleanup; controlled rows do not pass.
The reader can also rebuild a full-flow report from `evidence.json`, `live.json`
and the seven `stage-<id>.json` reports, preserving all original predicates and
scenario rows. A complete producer summary alone cannot establish acceptance.
The authority scenario runs ordinary installed-client probes and independently
qualifies their retained native/provider observations. Its protected configuration
uses `ADP_TOOLS_ATLASSIAN_AUTHORITY_JSON` and separate ordinary owner refresh
sessions in `ADP_TOOLS_ATLASSIAN_AUTHORITY_SESSIONS_JSON`. The binding step derives
the current source, run and release hash; session tokens remain in private files,
outside the report. All sixteen cases remain visible. Unsupported expiry/refresh
history and catalogue-transition observations remain pending rather than being
inferred from successful reads. An unknown original-owner read is checked through
its original operation reference, at most three times, without issuing it again.

The four-connector scenario still lacks complete executable producer and concrete
reader qualification. Regression live bindings and complete
stage production are also required, so full-flow completion remains false. These
are implementation gaps, not merely missing provider access. A wildcard selection
records unregistered original scenarios and cannot report a complete matrix.

### Guarded connector transition

The production fence writer is `src/internal/tool_adapter_release.py` in the
gateway. It reuses the existing signed deployment-job check; an evaluation job
cannot write this state. Migration `138_tool_adapter_transition` adds a nullable
transition journal without activating any connector. The journal retains the
original four-route manifest, immutable API deployment IDs, owner, phase and drain
deadline. A new transition increments both generations. Repeated requests retain
the original deadline; an older controller cannot reopen a newer generation.

`tools-platform-deploy.yml` selects the corresponding deployment-only job with
`stage: tool-adapters` and `adapter_action: handoff` or `rollback`. It uses the
protected `ADP_TOOLS_ADAPTER_HANDOFF_JSON` data document, not executable fixture
modules. Its `plan` contains `api_id`, `stage` and `before`/`after` snapshots. Each
snapshot identifies its deployment, release, status and all four connector target
IDs, backend kinds and independently computed route/target digests. `selectors`
names the protected account, region, AgentCore gateway, prepared baseline and
candidate stages, internal legacy/adapter ALB hosts, gateway/facade workloads and
adapter namespace. No credentials belong in that document.

The handoff job uses the separate protected `adp-tools-handoff-dev` environment,
not evaluation's deployment role. Optional Terraform in
`platform/automation-infra/tools-handoff-deployment.tf` limits that identity to
the selected private API stage, four exact prepared targets and one cluster.
`platform/infra/tools-handoff-rbac.tf` grants its separate group patch access only
to the named gateway/facade/adapter deployments and adapter configuration. Both
are disabled by default. The existing evaluation group does not gain gateway
patch access. Operators must apply these reviewed prerequisites and select the
new mutation role through the existing release binding; this code does not change
an accepted live operation or assume the role has already been deployed.

The deployment helper independently reads the prepared versions and refuses
changes outside the four connector paths. It verifies the exact AWS role and
Kubernetes cluster, fences admission, waits for the existing 20-second work bound,
updates release generations, switches the API stage and checks all four routes
again. Failed forward application attempts one restoration; failed restoration
leaves the durable group fenced. Initial rollback keeps Atlassian disabled rather
than routing Atlassian MCP onto legacy Jira. A retry consumes the retained plan
and phase instead of constructing a new generation.

This is not yet the complete isolation campaign. Automatic preparation/publication
of the immutable route versions, provider-effect and interruption probes, and the
scenario-06 independent reader still need integration. Do not treat a successful
transition or its controlled tests as evidence that all acceptance predicates pass.

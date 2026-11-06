# Gateway streaming load test

This Locust client sends real authenticated model requests. Use a dedicated test tenant and an approved target. Keep its JWT in a file readable only by the operator. The client never prints authentication headers.

```bash
python3 -m venv /tmp/gateway-perf-venv
/tmp/gateway-perf-venv/bin/pip install -r tests/performance/gateway/requirements.txt
export ADP_PERF_TOKEN_FILE=/private/path/test-token
export ADP_PERF_OUTPUT_DIR=/private/path/results
export PERF_LABEL=baseline
export PERF_MAX_REQUESTS=2000
export PERF_STAGES=60:3,120:10,210:30,330:60,390:90
/tmp/gateway-perf-venv/bin/locust -f tests/performance/gateway/locustfile.py \
  --headless --host https://gateway.example.com/api --stop-timeout 120 \
  --only-summary --csv "$ADP_PERF_OUTPUT_DIR/$PERF_LABEL" \
  --html "$ADP_PERF_OUTPUT_DIR/$PERF_LABEL.html"
```

Each stage is `cumulative_end_seconds:simultaneous_sessions`. The example models 3, 10, 30, 60 and 90 sessions. These are continuously requesting synthetic model clients, not complete developer or agent workflows. A zero-user final stage provides a cooldown window. `PERF_MAX_REQUESTS` caps generations, independently of run duration.

Defaults: Haiku 4.5, 70% longer requests with synthetic context and a 768-token output cap, 30% short requests with a 128-token cap, and 0.5–2 seconds think time. `PERF_MODEL`, `PERF_LONG_TOKENS`, `PERF_CONTEXT_REPEATS` (default 60), and `PERF_LONG_RATIO` change those parameters. Larger tests incur model charges; keep request counts, token limits, and tenant quotas bounded.

Set `PERF_API=responses` for models served by the gateway's `/openai/v1/responses` route. Set `PERF_MODEL` to the configured model ID, `PERF_REASONING_EFFORT` to the desired reasoning level (default `medium`), and `PERF_RESPONSES_MAX_TOKENS` to the total output budget (default 2048, including reasoning tokens). The client records terminal usage and incomplete-response details. This route requires a content-bearing `response.completed` event with completed status; `response.incomplete`, `response.failed`, and streamed errors count as failures.

`PERF_LONG_WORDS` adds a requested word count to the long prompt. Use it when you want a complete answer within the output budget. Without it, long token caps above 1000 request a deliberately long response for drain testing; Responses API token-budget exhaustion can then produce an incomplete response. `PERF_READ_TIMEOUT` controls the idle socket timeout (default 180 seconds), not a total generation deadline. Match reasoning, prompt/output budgets and caching assumptions when comparing models, and record the distinct API routes.

Set `PERF_UNIQUE_PROMPTS=true` to prepend a unique synthetic request ID. This varies the prompt prefix to probe cache sensitivity; inspect provider usage/cache counters rather than assuming cache misses. By default, prompts repeat. Zero-user stages stop all users together, allowing existing streams to drain within Locust's stop timeout.

Every successful generation requires nonempty streamed content, no stream error, and a terminal `[DONE]` marker. JSONL evidence records first-content latency, full completion latency, largest content-chunk gap, response status, stream completion and client-observed concurrency. Stop-timeout interruptions are explicitly identified in new runs. The full-response duration replaces Locust's default headers-only timing.

Locust also records TTFT under the `STREAM` request type. Its combined aggregate count therefore includes both timing and generation records. Use `POST LLM/*` rows or the JSONL generation records for request/error counts. Keep rejected, interrupted, and incomplete streams visible; do not count HTTP 200 alone as success.

For scaling validation, collect per-pod CPU/memory, HPA desired/current counts, readiness/restarts, database CPU/connections and Redis health alongside these files. Test the same image and instrumentation on each candidate. Verify scale-out, return to the minimum replica count, and graceful draining under active long streams. A quota rejection or overloaded shared database is not gateway CPU saturation.

Run the local SSE validation tests with the same virtual environment; these use a loopback server and make no cloud requests:

```bash
/tmp/gateway-perf-venv/bin/python tests/performance/gateway/test_stream_validation.py
```

## Bounded controller and private reports

`campaign_controller.py` has an opt-in **smallest checkpoint**, a separate
**model ramp**, and a bounded **comparison** mode. Legacy `locustfile.py` stage end times remain cumulative;
controller stages describe **minimum dwell after actual sessions are reached**.
The checkpoint accepts only two mandatory one-session, zero-dwell API stages and
at most 40 attempts, 180 active seconds and 120 drain seconds. It uses one
physical attempt on each API and fails when a terminal/content event is missing.
No mode is evidence of deployed capacity until an operator checks the exact
revision, health, provider measurements and protected cleanup receipt.

An operator must prepare a private owner-only JSON config and existing owner-only
token/evidence directories before running anything. The file has these required
fields (values below are **fictional shape examples**, not approved targets,
prices, SLOs, identities, cost authorizations or runnable credentials):

```json
{
  "schema_version": 1,
  "scenario": "smallest-checkpoint",
  "seed": 42,
  "target": "https://gateway.example.com/api",
  "revision": "replace-with-verified-deployed-revision",
  "approved_account": "replace-with-approved-account-reference",
  "approved_role": "replace-with-exact-approved-role",
  "recovery_owner": "replace-with-named-owner",
  "campaign": "fictional-campaign-001",
  "campaign_owner": "fictional-fixture-owner",
  "journal_dir": "/private/protected-shared-journal",
  "recovery_config": "/private/recovery-config.json",
  "evidence_dir": "/private/protected-results",
  "identities": [{
    "id": "fictional-developer", "kind": "developer", "tenant": "fictional-tenant",
    "root_human": "fictional-owner", "ownership_receipt": "protected-receipt-reference",
    "token_file": "/private/fictional-test-token"
  }],
  "models": [
    {"api": "chat", "model": "fictional-chat-binding", "sdk_version": "replace-me",
     "max_input_tokens": 100, "max_output_tokens": 10,
     "price_per_input_token": 0.000001, "price_per_output_token": 0.000001},
    {"api": "responses", "model": "fictional-responses-binding", "sdk_version": "replace-me",
     "max_input_tokens": 100, "max_output_tokens": 10,
     "price_per_input_token": 0.000001, "price_per_output_token": 0.000001}
  ],
  "stages": [
    {"name": "chat", "required": true, "desired_sessions": 1, "spawn_rate": 1,
     "min_dwell_seconds": 0},
    {"name": "responses", "required": true, "desired_sessions": 1, "spawn_rate": 1,
     "min_dwell_seconds": 0}
  ],
  "limits": {"attempts": 2, "active_seconds": 10, "drain_seconds": 5,
             "input_tokens": 200, "output_tokens": 20, "max_spend": 1,
             "setup_seconds": 600, "cleanup_seconds": 600},
  "slos": {"ttft_ms": 1000, "http_ms": 5000, "terminal_ms": 1000}
}
```

For an explicitly authorized single-model ramp, use the same private bindings,
select `"plan": "model_ramp"`, explicit `"matrix_cell": "opus_ramp"` or
`"astra_ramp"` (matching the private model binding) and `"ramp_api": "chat"`
or `"responses"`, and
provide a positive numeric `think_time_seconds`. Replace the checkpoint stages
with five required `ramp: true` stages named `ramp-10`, `ramp-60`, `ramp-100`,
`ramp-150`, `ramp-200`, setting `desired_sessions` to the corresponding count,
`min_dwell_seconds` to 60/120/120/120/300, and a positive `spawn_rate` on each.
Declare one campaign-wide attempt (≤12,000), active (≤1,440 seconds), drain
(≤240 seconds), input/output token and spend bound, plus the same explicit SLOs.
Transitions together cannot exceed 120 seconds. Sessions repeat distinct
physical requests with the declared think time; every attempt consumes a shared
permit and reserves its maximum cost before dispatch. A session count reflects
started workers, not passing requests. Failed or unreached plateaus remain failed
or not-run, even if workers were scheduled; the stop rule requires ≥99% complete
streams at **each** plateau, with bounded drain. Use a dedicated private evidence
directory for each run: `checkpoint-results.json` is written once and never
overwritten. Loopback checks do not establish a live health, cost or capacity
receipt; the operator must verify those signals independently before accepting
any matrix cell.

For one identity, cache, or agent fan-out comparison cell, use
`"plan": "comparison"` and select a single `comparison_cell`: `shared_identity`,
`developer_only`, `distinct_developers`, `distinct_tenants`, `distinct_agents`,
`fan_out`, `cached_context`, or `unique_context`. Supply the same explicit
`ramp_api`, private model and owned identity bindings and positive
`think_time_seconds`. The single required stage is named after the selected
cell, with `ramp: true`, positive `spawn_rate` and `min_dwell_seconds`, and
integer `desired_sessions` from the number of identities up to
`safe_inflight_sessions` (at most 200). Each cell has its own private results
directory, ≤1,200 attempts, ≤600s active time, ≤240s drain, shared numeric
token/spend caps and predeclared SLOs; one cell does not lend its budget to
another. `qualified_capacity_receipt` must be a protected owner-only JSON file
for a nonlocal run. Its `revision`, `target`, `approved_account` and
`approved_role` must match the campaign, its `status` must be `pass`, its
`safe_inflight_sessions` must cover the declared ceiling, `health` must contain
`oom: false`, `restarts: 0`, `dependencies_ok: true`, and `protected_receipt`
must reference separately verified proof. A file alone does **not** verify
authority or prove its claims: the operator must authenticate the proof and
confirm it is a previously demonstrated safe in-flight bound before a billed
run. `local_test` permits only fictional loopback receipts.

The shared-identity and cached/unique cells use one owned developer; distinct
developers must be different human roots in one tenant, and distinct tenants
must use different owned human roots. Agent cells use separate owned agents;
fan-out requires at least two agent identities rooted in the same human chain.
For cache cells, `context_file` is a private owner-only file containing at least
1,024 words, and the declared maximum input token reservation must cover its
byte length plus headroom. Cached requests reuse its full prompt; unique
requests place a deterministic changing key before it. A cached-context cell
cannot succeed without at least two provider cache-counter samples and a
positive observed cache hit; unique-context needs two counter samples and
separate prompt hashes. Protected `checkpoint-results.json` retains per-request
identity, tenant, human root, prompt digest, observed cached tokens and
fan-out/concurrency counts, but not raw prompts or tokens. A successful local
cell only establishes the local fixture behavior; it does not certify a matrix
cell without independently verified health, deployed revision and cleanup.

`recovery_config` is a **separate** private file for an independently scheduled
janitor. It binds the same campaign, owner, protected shared journal path,
exact target, revision, approved account and role, and finite `cleanup_seconds`
≤600. Live setup now requires `backend: "adp"`: `campaign_adp.py` calls actual
ADP quota APIs and selected-connection Cognito operations, not a fixture service.
The old HTTP adapter remains a synthetic test backend and a cleanup-only reader
for existing journals; it cannot start a new live campaign.

### Production fixture prerequisites

Install the gateway quota and membership receipt endpoints in
`src/admin/campaign_quota.py` and `src/admin/campaign_identity.py` before
running this version of the harness. Unlike the earlier harness-only changes,
this repair requires a gateway release. It adds no database table: the quota
edit and its immutable receipt commit together in the existing audit table.
Only exact user scopes are supported; existing admin permissions, tenant
membership checks and organization locking apply. A lost reply can be read back
at the exact operation ID. Changed revisions are refused even if the values
happen to match. This interface never deletes metering or accounting records.

Provision dedicated **enabled** Cognito users with **revoked**, nonprivileged ADP
memberships and human root attribution before the campaign. The harness temporarily
activates only that gateway membership; it never enables, disables or changes the
Cognito identity. Shared users, new tenant creation,
workload agents and broad organization quotas are not supported by this adapter.
It explicitly rejects caller kind `agent` rather than counting human tokens as
agents. Use the separate `adp-native` backend below for admitted native workloads;
human tokens and local-human operator capabilities cannot substitute for native
workload credentials. Each supported developer caller
has an `identity`, a `quota` and a `secret`
inventory entry. The controller checks those bindings and journals setup before
activating memberships, changing quotas or issuing tokens. Token cache paths must be
absent at setup and reside on a protected shared mount accessible to recovery.
Password sources are pre-existing operator-managed Secrets Manager secrets
containing `username` and `password`; the campaign does not create or delete
these sources. It removes its newly issued token caches during cleanup.

Cache cleanup never unlinks the configured cache pathname after a credential probe.
It first journals a unique private claim directory beside the cache, then atomically
renames the directory entry into that claim. It verifies the claimed file's inode,
modification time and content digest against the recorded mutation before deleting
through the claim directory's open descriptor. Writers may replace the original
cache pathname; they must not modify recovery's private claim directories or journals.
The filesystem must support atomic same-filesystem rename, hard links and directory
fsync. An unexpected file or symlink is linked back without replacing any newer
entry and retained in the claim with a non-clean hold. If returning the object is
impossible, including for a directory, it remains at the protected claim path recorded
in the operations journal for operator recovery. Cleanup never recursively deletes it.

Recovery reconciles pending claims before interpreting an absent cache as restored.
Interrupted renames and deletions retain their durable intent and can be resumed;
unexpected objects remain held. Empty claim directories and recovery receipts remain
as audit evidence. Before starting this protocol, recovery upgrades the journal to
version 2 so older version-1-only readers refuse it rather than incorrectly declaring
a claimed cache clean. This reader still accepts version 1. Keep the version-2-capable
recovery reader installed during controller rollback until all such journals and
claims have authoritative cleanup receipts; do not downgrade journal versions manually.

In addition to the common recovery bindings, the private recovery file contains:

```json
{
  "backend": "adp",
  "gateway_admin_token_file": "/private/recovery-admin-token",
  "aws_connection": "fictional-scoped-recovery-connection",
  "aws_region": "us-east-1",
  "user_pool_id": "us-east-1_example",
  "health_file": "/private/independent-health.json",
  "health_owner": "fictional-signal-owner",
  "fixtures": [
    {"kind": "identity", "binding": "owned-cognito-sub", "username": "owned-user",
     "tenant": "owned-tenant", "root_human": "owned-cognito-sub",
     "ownership_receipt": "private-ownership-reference", "desired": {"enabled": true}},
    {"kind": "quota", "binding": "owned-cognito-sub",
     "ownership_receipt": "private-ownership-reference",
     "desired": {"rpm": 20, "tpm": 2000, "concurrent_requests": 2}},
    {"kind": "secret", "binding": "/private/cache/caller-token",
     "identity": "owned-cognito-sub", "password_secret_id": "owned-password-secret",
     "client_id": "owned-cognito-client", "ownership_receipt": "private-ownership-reference",
     "desired": {"reference": "/private/cache/caller-token"}}
  ]
}
```

This fragment is not a runnable configuration. Include `schema_version: 1`,
`campaign`, `owner`, `journal_dir`, `target`, `revision`, `approved_account`,
`approved_role` and `cleanup_seconds`. `approved_role` is the exact STS
assumed-role ARN **without its final session segment**, for example
`arn:aws:sts::000000000000:assumed-role/fixture-recovery`. Each AWS operation uses
the named broker connection; STS checks both account and this exact role.
No ambient credential fallback is permitted. Recovery needs scoped
`AdminGetUser`, `AdminInitiateAuth` and `GetSecretValue` authority for these exact fixtures,
plus existing gateway user-management and quota-administration permissions. Passwords and tokens
travel through private input/output pipes, never command arguments or journals.

The independent signal owner must atomically refresh `health_file` with
`campaign`, `target`, `revision`, `approved_account`, `approved_role`, `owner`
(matching `health_owner`), Unix `observed_at`, and boolean `healthy`,
`no_owned_active_clients`, `normal_user_denials_ok`. All must be true and the
observation at most 30 seconds old. Cleanup requires an observation made after
recovery began, as well as a successful actual `/health/detailed` check. A stale
or missing observation leaves a hold, not a passing receipt. This private file
is an operator attestation, not authenticated deployment proof; its owner must
verify the deployed revision and signals before authorizing a run.

Recovery first restores matching quota versions, then reinstates the membership's
original revocation, then removes unchanged owned token files. Membership restoration
requires the exact activation receipt and unchanged user/membership versions under
database locks. Transactional receipts reconcile lost setup and restoration replies.
Before caching a token, the gateway must authenticate it as the exact nonprivileged
fixture. Before removing the cache, the same authentication endpoint must reject the
token for revoked membership or expiry; a usable token or unavailable probe retains
a non-clean hold. Cognito sign-out alone cannot invalidate the gateway's locally
validated JWTs, and Cognito offers no conditional identity mutation. Legacy Cognito
mutation journals therefore remain readable but require operator recovery: this
version never disables or signs out a possibly reassigned Cognito identity.
The protected journal and `*.operations.json` retain receipts and unknown
accounting independently; do not delete them to clear a hold. Retain this
version of the cleanup reader and both receipt endpoints during rollback until
all its journals are retired by the recovery owner.
The recovery configuration is fingerprinted into new journals before setup.
Do not retarget it during recovery; refresh credential file contents in place
without changing the journaled bindings. Retargeting fails before any operation.

For recovery to survive runner loss, the operator must verify persistent shared
encrypted journal/cache mounts, independently scheduled cleanup, scoped recovery
credentials and signal ownership. No scheduler, mount, credentials or cloud
resources are provisioned by these tests. A local directory is **not proof of
remote durability**. `local_test` permits plain HTTP only for loopback tests;
it cannot select a real target.

### Native workload campaigns and recovery

The `adp-native` backend runs checkpoints and agent comparison cells using
explicitly pre-admitted, active, dedicated native workloads. It does not dispatch
arbitrary work or create credentials. Operators must first admit the fixtures
through the existing native admission path, arrange private refreshable credential
sources, and install independent recovery. No deployment or billed run is implicit.
Dedicated fixture runtimes must remain idle except for controller-authorized
traffic; do not borrow credentials from agents doing other work. Their identities
and credentials must not be shared with clients outside this campaign.
The inventory must include every participating workload and its ancestors, with
one root for a `fan_out` cell. Fixture grants must not permit `dispatch` during
measurement: fan-out measures already-admitted descendants, not new untracked
dispatches. Each workload must match its protected tenant, human owner, invocation,
attempt, credential epoch, pod UID and parent lineage. Do not reuse production work.

The native `GET /internal/v1/agent/campaign-binding` requires the existing IAM,
run-credential and verified-workload authentication. The corresponding human-owned
`GET /activity/invocations/{invocation}/campaign-binding` returns an exact protected
binding and version. Neither endpoint issues credentials or admits new executions.
Setup compares these snapshots and journals the complete inventory before its
first mutation. `PUT /activity/invocations/{invocation}/campaign-enrollment/{operation}`
records a version-checked enrollment receipt, then retains the exact pod's exit
evidence before requests begin. Enrollment does not revoke the worker. The controller
only creates its cache after validating the matching enrollment receipt.

Native recovery configuration uses the common campaign, owner, journal, revision,
target, account, role, cleanup and health bindings, plus these fictional fields:

```json
{
  "backend": "adp-native",
  "aws_connection": "campaign-recovery",
  "aws_region": "us-east-1",
  "gateway_admin_token_file": "/private/campaign/root-human-token",
  "native_target": "https://native.example.test",
  "native_aws_connection": "campaign-native-worker",
  "native_approved_role": "arn:aws:sts::000000000000:assumed-role/campaign-worker",
  "fixtures": [
    {
      "kind": "workload",
      "binding": "fixture-root",
      "tenant": "example-tenant",
      "root_human": "example-human",
      "ownership_receipt": "private-owned-fixture-reference",
      "attempt": 1,
      "credential_epoch": 1,
      "workload_uid": "example-pod-uid",
      "parent": null,
      "run_credential_file": "/private/campaign/root-run-source",
      "workload_token_file": "/private/campaign/root-workload-source",
      "token_file": "/private/campaign/root-cache"
    }
  ]
}
```

This is a configuration fragment, not an executable live plan. For each child add
its own entry with `parent` equal to its inventoried parent's binding and distinct
source/cache paths. Campaign `identities` must exactly match this inventory with
`kind: agent`, `id` equal to `binding`, and matching tenant, root human, ownership
receipt and token-file path. A checkpoint or model ramp uses one workload; the
`distinct_agents` and `fan_out` comparison plans use every declared identity under
the existing safe-ceiling and qualified-capacity-receipt requirements.

Despite its historical field name, `gateway_admin_token_file` must authenticate
as the workload's actual human owner with current membership; admin privileges do
not bypass ownership. All files and their containing directory must be private
(0600 files, 0700 directory); cache paths must initially be absent. The native
runtime owns and refreshes the source files. Before each inference request, the
controller reads those sources and revalidates the enrolled binding. Same-epoch
refresh is allowed; changed attempts, epochs, grants or registrations fail closed.
The selected native broker connection is checked with STS for the exact account
and assumed role before signing each request with SigV4. There is no ambient SDK
credential fallback, HTTP redirect, environment proxy, or qualifying retry.
Install the pinned performance requirements (including boto3); `campaign_signer.py`
must remain alongside the controller. The existing shared permit ledger covers
each physical inference attempt across native sessions and both API protocols.
The one-use dispatch check runs after credential verification and signing,
immediately before sending the model request. A permit expiring during preparation
does not authorize inference.

An independent recovery owner can submit the captured `expected_version` to
`POST /activity/invocations/{invocation}/campaign-stop/{operation}` using a stable
UUID operation ID. The gateway verifies current human ownership and membership,
records authority retirement transactionally, then retains the exact pod's exit
evidence before requesting shutdown with a 30-second Kubernetes grace period.
UID and resource-version preconditions preserve replacement pods and other
controllers' finalizers. This path does not depend on an already-revoked worker
revalidating a queued abort. It requires the gateway's namespace-scoped pod
`get`, `patch` and `delete` permissions; this repository change does not apply
those permissions to a deployment.

`stop_requested` means only that shutdown was requested. Repeating the same
operation reconciles lost replies and returns `workload_exited` only after
UID-bound container termination is observed and durably recorded. A missing pod,
expired lease or retired credential is not exit proof. The gateway releases only
its campaign finalizer after the receipt is durable; it preserves financial holds
and unrelated resources. `PUT /activity/invocations/{invocation}/campaign-retirement/{operation}`
can independently fence an exact execution, but its `authority_revoked` receipt
does not establish physical exit. Retirement fences descendant admissions even
when the root already completed. Cleanup of each explicitly owned descendant still
requires its own captured binding and operation; there is no tenant-wide discovery
or deletion endpoint. Changed bindings retain a conflict rather than targeting a
newer attempt or registration.

Restart cleanup reads the version-3 journal, including workloads whose enrollment
reply was lost, and reconciles their stable stop operation IDs within the declared
deadline. Only matching durable exit receipts authorize removal of unchanged
controller-owned caches. Runtime-owned source files, financial records, unrelated
workloads and operator replacements are preserved. Pending exits, unknown request
accounting and changed resources retain holds and produce a nonzero result. Keep
a version-3-compatible independent cleanup reader installed across rollback until
the cleanup receipt is authoritative; older readers refuse the new journal version.
Use a cleanup bound that includes the 30-second pod shutdown grace and observation
time. Health attestations must be refreshed independently during recovery.

Local native integration tests exercise real HMAC authentication, binding and
ownership checks, the controller, permit ledger and restart journal against
simulated DynamoDB/Kubernetes, STS, IAM transport and inference. Run them from
`modules/gateway` with its pinned test environment:

```bash
AWS_EC2_METADATA_DISABLED=true python -m pytest \
  tests/agentauth/test_campaign_native_harness.py \
  tests/agentauth/test_campaign_pod_recovery.py \
  tests/activity/test_campaign_recovery_routes.py -q
```

These tests do not establish deployed inference, real IAM authorization or live
capacity. The authorized both-API checkpoint, independent runner-loss receipt and
representative matrix remain separate operator-owned live acceptance.

### Running supported campaigns

Operator terminal, **only after** the private target, revision, scoped identity,
price/SLO authority, recovery owner and journal/backend are verified:

```bash
python3 tests/performance/gateway/campaign_controller.py \
  --config /private/checkpoint-config.json --validate-only
python3 tests/performance/gateway/campaign_controller.py \
  --config /private/checkpoint-config.json --run
python3 tests/performance/gateway/campaign_controller.py \
  --config /private/checkpoint-config.json --report
```

Independent recovery scheduler, with its own authorized fixture backend:

```bash
python3 tests/performance/gateway/campaign_controller.py \
  --config /private/recovery-config.json --resume-cleanup
```

The report writes owner-only `campaign-report.json` (private revision, hashes,
per-stage observations, identity counts and cleanup status) and
`sanitized-report.md` beneath `evidence_dir`; it does **not** publish either.
Review the sanitized file before sharing. Missing or unverified matrix cells
stay `blocked` or `not-run`; short successful checkpoints do not turn into a
200-session claim. An operator can add protected `matrix-evidence.json` there
for measured cells, with exact revision, protected receipt reference, declared
numeric caps/spend, observed attempts/completions, p95 SLOs, health and stage
measurements. Each ramp requires full 10/60/100/150/200 plateaus with the
predeclared 60/120/120/120/300-second dwell, ≥99% complete streams at each
plateau and overall, and no restarts or OOM. For an executed ramp or comparison, the report instead derives attempts,
completions, plateau dwell, concurrency, latency, spend reservations, identity
and cache counts from its private controller result. The `matrix-evidence.json`
entry for the matching `matrix_cell` or `comparison_cell` can supply independent
`protected_receipt`, `operator_verified: true`, exact `revision`, `target` and
`config_hash`, and observed `health` (`oom: false`, `restarts: 0`,
`dependencies_ok: true`). It **cannot** replace controller counts or SLOs; a
mismatch fails the cell. Missing cleanup or attestation leaves it blocked.
Loopback evidence remains blocked even if an attestation file claims otherwise.
One report cannot qualify unrelated cells using measurements from another run;
keep per-cell raw evidence privately and combine only independently accepted
reports for a final matrix. The report evaluates supplied operator evidence;
it cannot authenticate a receipt's source or authorize billed runs.
`--report` exits nonzero while mandatory cells or cleanup evidence remain
incomplete. A janitor can restore all fixtures while still returning nonzero
when inference accounting is unknown; it never deletes that accounting record.

Controller-mode Locust workers must explicitly receive `PERF_CONTROLLER_URL`,
`PERF_CONTROLLER_TOKEN_FILE`, `PERF_IDENTITY_ID`, `PERF_IDENTITY_KIND`,
`PERF_RESERVE_INPUT_TOKENS`, `PERF_PRICE_PER_INPUT_TOKEN`,
`PERF_PRICE_PER_OUTPUT_TOKEN`, and `PERF_DRAIN_SECONDS`; both token files must be
owner-only. The controller endpoint defaults to loopback; a cross-host
endpoint must receive an operator-configured TLS 1.2+ server context and each
worker must verify its certificate (use `PERF_CONTROLLER_CA_FILE` when its CA is
not in the system trust store). No cross-host endpoint, network route, certificate
or service is provisioned by this repository. Set Locust `--stop-timeout` at or
below the drain cap. Workers obtain an expiring permit and a single-use dispatch
approval immediately before the physical request; an expired, replayed or
unreachable approval denies the request. Missing controller replies stop
admissions and leave uncertain outcomes visible.

Local loopback checks (synthetic tokens only; no cloud service or billed request):

```bash
/tmp/gateway-perf-venv/bin/python -m unittest discover \
  -s tests/performance/gateway -p 'test_*.py' -v
```

## Matched stream-failure diagnosis (#7033)

This is a **manual, owner-gated diagnosis**, not a flag for the gateway
controller. The controller above makes bounded gateway requests but does not
make matched direct Bedrock SDK calls. Its passing run, or a successful direct
call on its own, cannot attribute the original Opus empty/partial stream.
The local [parser regressions](test_stream_validation.py) and gateway route
fault tests should pass before a billed attempt. The approved
[discovery bound](../../../docs/design-notes/7030-gateway-performance/campaigns.md#finite-plans-and-implementation-checkpoints)
is **at most 300 physical attempts per model, 300 active seconds and 120 seconds
of concurrent drain**, including both arms, warmup and retries. Smaller
approved numeric input/output token, spend, request and per-arm caps also apply.
Never provision a validation service merely for this diagnosis.

1. **Authorize and freeze.** A named diagnostic owner privately records the
   deployed image/revision, selected target and exact gateway/direct-control
   identity, model entitlement, destination/region, fixed request bytes and
   hashes, SDK/botocore versions, input/context, output and reasoning caps,
   pricing, quota, predeclared stop/SLO limits, fixture ownership and protected
   receipt location. Verify the selected role using the approved credential
   connection; ambient credentials are not a substitute. No bindings, numeric
   token/spend caps, protected storage or cleanup owner means *not run*.
   Fix the request body for the entire comparison. Never publish these bindings,
   raw prompts, secrets, URLs, account identifiers or private receipts here.
2. **Pair comparable attempts.** Exercise gateway Chat and direct SDK
   InvokeModelWithResponseStream with the same effective destination and
   provider-submitted request bytes *after* Chat translation; the gateway's
   HTTP Chat envelope is different. Protect both body digests and verify the
   fixed prompt/context and output cap. Pin effective SDK HTTP retry, connect/read
   timeout and pool settings; report any remaining transport difference instead
   of calling the arms identical. The direct arm bypasses the gateway but not
   the provider quota. Use a declared seed to interleave/counterbalance pairs
   across cold client creation, reuse, an explicitly recorded idle interval and
   concurrency 1 → 5 → 10. Include a fixed long-context cell only within the
   same limits. Track actual in-flight provider calls, not session labels.
3. **Count and correlate before interpreting.** Assign a private pair key and
   distinct generation/physical-attempt keys before each submission. Record
   client identity *class* and reuse age, executor/connection wait, SDK
   before-send, physical socket attempt, provider acknowledgement when known,
   first-content/terminal/EOF times, byte/frame counts, exception category,
   HTTP status, terminal validity, reader/client closure and token/cache usage.
   Gateway and direct controls must count *all* attempts, including SDK retries,
   queue cancellation, unknown outcome and early disconnect. The gateway
   `bedrock_gateway_sdk_before_send_total` counter is pre-socket correlation,
   **not** proof the provider received anything. Protect per-attempt evidence
   and its join key; publish only aggregate, sanitized categories.
4. **Stop, drain, reconcile.** Enforce the common physical-attempt, active,
   token and spend limits before submission. Abort on missing telemetry,
   safety-guard trip, restart/OOM, quota failure or repeated incomplete streams;
   never replay uncertain or post-content inference. Stop new admissions, drain
   concurrently for no more than 120 seconds, close readers and owned clients,
   retain ambiguous spend bounds and record unresolved attempts. Restore owned
   quotas/fixtures, revoke test users, remove secrets and preserve audit and
   accounting evidence. Record cleanup and source revision privately.
5. **Attribute only with joined evidence.** Compare paired failures at the
   socket, provider and translation boundaries. A gateway-only failure before
   provider acknowledgement may implicate its lifecycle; a confirmed provider
   failure in both arms may be external. A direct success, HTTP 200, a missing
   provider receipt or a locally passing loopback test alone proves neither.
   If the original failure does not reproduce or a boundary cannot be joined,
   report `unattributed` and leave AC-02 open. Do not tune retries, timeout or
   client reuse based solely on an unmatched sample. Diagnosis is not the
   deployed five-minute 200-session AC-03 qualification.

### Protected report format

The operator copies this **fictional, not-run shape** into private storage,
fills all bounds and evidence references *before* dispatch, then records one
row per physical attempt (including direct controls and unknowns). Keep the
per-attempt rows and joins protected; this public example is not a live receipt.
Use `not_run`, `unattributed`, `gateway_attributed` or
`external_attributed` for `attribution.classification`; the latter two require
joined protected evidence, not control success alone.

```json
{
  "schema_version": 1,
  "example_only": true,
  "status": "not_run",
  "authorization_ref": null,
  "diagnostic_owner_ref": null,
  "protected_receipt_ref": null,
  "revision_ref": null,
  "cleanup_owner_ref": null,
  "profile": {
    "destination_ref": null,
    "model_ref": null,
    "identity_ref": null,
    "sdk_versions_ref": null,
    "request_hash_ref": null,
    "fixed_context_and_caps_ref": null,
    "effective_transport_settings_ref": null,
    "pairing_seed": null,
    "idle_interval_seconds": null
  },
  "limits": {
    "physical_attempts": null,
    "active_seconds": null,
    "drain_seconds": null,
    "input_tokens": null,
    "output_tokens": null,
    "max_spend": null,
    "stop_and_slo_ref": null
  },
  "cohorts": ["cold", "reused", "idle", "concurrent_1", "concurrent_5", "concurrent_10", "long_context"],
  "attempts": [],
  "totals": {
    "gateway_requests": null,
    "gateway_sdk_before_send": null,
    "gateway_socket_attempts": null,
    "direct_socket_attempts": null,
    "provider_acknowledged": null,
    "complete_streams": null,
    "failed_before_content": null,
    "failed_after_content": null,
    "unknown": null,
    "observed_spend": null,
    "reserved_ambiguous_bound": null
  },
  "attribution": {
    "classification": "not_run",
    "original_failure_reproduced": null,
    "matched_controls_succeeded": null,
    "joined_evidence_refs": [],
    "unresolved_boundaries": [],
    "reason": null
  },
  "cleanup": {"state": "not_run", "protected_receipt_ref": null}
}
```

Each protected `attempts` row carries `pair_id`, `arm`, `cohort`,
`generation_id`, `physical_attempt_id`, `client_class`, `reuse_age_ms`,
`queue_wait_ms`, `connection_wait_ms`, `sdk_before_send`,
`socket_submission`, `provider_acknowledgement`, `http_status`,
`first_content_ms`, `terminal_ms`, `eof_ms`, `valid_terminal`,
`content_bytes`, `exception_class`, `reader_closed`, `client_closed`,
`usage_ref` and `outcome`. Mark an unobservable boundary `unknown`, never
zero. Aggregate all rows and unknown attempts; attach the operator's protected
authorization, health, source revision, correlation and cleanup receipts. No
row, template or local test is evidence that the live diagnosis ran.

## Opus live-qualification handoff (#7033 AC-03)

**Not run; named live-validation owner and human acceptance required.** This
plan describes what the deployed gateway must prove after the truthful-stream,
accounting and diagnosis fixes pass review and deployment. Neither this public
template nor a loopback result establishes an Opus cause, a deploy, or a passing
five-minute 200-session plateau. Do not activate metered serving, provision a
validation service, or run a billed campaign merely to prepare this handoff.

1. **Preflight privately.** The named owner must verify the actual deployed
   revision and canary health of both gateway APIs, compatible terminal and
   settlement ownership, #7031 isolation, #7032 dispatch guard and #7033
   interrupted-stream fixes, and the separate #7035/#7036 fixture and controller
   prerequisites. Select the authorized target, *exact* connected IAM role,
   owned Opus model/API binding, fixed context/payload, SDK version, output and
   reasoning caps, provider quota and cleanup owner. Record numerical input,
   output, reasoning and spend ceilings and **predeclared** first-content,
   HTTP-lifetime and terminal/handoff p95 SLOs. The existing 90-second
   first-content guard is an emergency stop, **not** an approved SLO. Missing
   identity, quota, pricing, cap, SLO, protected receipt storage or live
   authorization means do not start. Never place these private bindings in Git.
2. **Use the existing bounded model ramp.** Supply the controller's private
   `model_ramp` configuration with `matrix_cell: opus_ramp` and the actual
   compatible API binding. Its five *required* stages are `ramp-10`, `ramp-60`,
   `ramp-100`, `ramp-150` and `ramp-200`, with minimum full dwell of 60, 120,
   120, 120 and **300 seconds after the target sessions are reached**;
   transitions together take at most 120 seconds. A positive, declared think
   time and fixed model/context/caps apply throughout. The conjunctive global
   maximum is 12,000 *physical* attempts/model, 1,440 active seconds and 240
   seconds of concurrent drain, with separately approved smaller request,
   token and maximum-priced-spend caps taking precedence. Account for warmup,
   SDK retries, unknowns and outstanding reservations *inside* these caps.
3. **Stop safely, never manufacture completion.** Use the existing shared
   attempt permits and pre-dispatch cost reservation; stop new admissions when
   any bound, error-window/latency guard, quota, mandatory telemetry or cleanup
   condition fails, or any pod restarts/OOMs. Drain only within the fixed global
   deadline, close clients/readers, and retain ambiguous charges. An aborted
   stage, a missed target, an early 200 plateau or exhausted cap is incomplete,
   not extrapolated to a pass. No uncertain or post-content replay. Treat HTTP
   200 without content/valid terminal, truncated frames, protocol error,
   timeout and cancellation as failures with exact stage and physical-attempt
   identities; do not silently discard unknown or lost-worker attempts.
4. **Keep the complete protected receipt.** Save controller config/hash and
   journal, all physical attempts and stage admission/terminal/error counts,
   achieved sessions/dwell, first-content and HTTP latency distributions,
   terminal/handoff/EOF timing, actual in-flight and fan-out, selected-role
   verification, deployed revision, per-pod CPU/memory/restarts/OOM, quota,
   database/Redis health, measured token/cost and outstanding reservations.
   Include signed or separately verified operator health, ownership and
   cleanup receipts; restore quotas, revoke test users and remove secrets.
   `STREAM TTFT/*` rows are timings, **not additional generations**.
5. **Decide from the full run.** Require ≥99% valid completed streams at
   *each* required plateau and overall, no restarts/OOM, complete five minutes
   at 200, predeclared SLOs met, all-attempt accounting and verified cleanup.
   Controller results and separately authenticated operator evidence must
   agree. Report skipped, aborted, missing, unknown or unverified stages as
   incomplete. The human live-acceptance owner decides after reviewing the
   protected receipt. An open/merged PR or deployment alone is not acceptance;
   roll back only compatible transport versions while keeping receipt readers
   and financial gates active.

### Protected qualification receipt shape

This **fictional, not-run** JSON illustrates required categories, not a runnable
configuration or completed evidence. Replace all null measurements *privately*
and store per-attempt rows, health, settlement and cleanup proofs in the
protected owner-selected location; no raw credentials or environment identity
belong in public artifacts. Keep the independent diagnosis report separate.

```json
{
  "schema_version": 1,
  "example_only": true,
  "scenario": "MOD-047-F001-S003",
  "criterion": "AC-03",
  "status": "not_run",
  "live_owner_ref": null,
  "authorization_ref": null,
  "deployed_revision_ref": null,
  "protected_receipt_ref": null,
  "controller_config_hash_ref": null,
  "preflight": {
    "target_and_role_ref": null,
    "model_fixture_and_sdk_ref": null,
    "upstream_quota_and_pricing_ref": null,
    "predeclared_slo_ref": null,
    "dependency_and_canary_refs": [],
    "cleanup_owner_ref": null
  },
  "limits": {
    "physical_attempts": null,
    "active_seconds": null,
    "drain_seconds": null,
    "input_tokens": null,
    "output_tokens": null,
    "reasoning_tokens_if_enabled": null,
    "max_spend": null
  },
  "stages": [
    {"name": "ramp-10", "sessions": 10, "min_dwell_seconds": 60, "actual_sessions": null, "reached_at_ref": null, "actual_dwell_seconds": null, "attempts": null, "valid_completed": null, "status": "not_run"},
    {"name": "ramp-60", "sessions": 60, "min_dwell_seconds": 120, "actual_sessions": null, "reached_at_ref": null, "actual_dwell_seconds": null, "attempts": null, "valid_completed": null, "status": "not_run"},
    {"name": "ramp-100", "sessions": 100, "min_dwell_seconds": 120, "actual_sessions": null, "reached_at_ref": null, "actual_dwell_seconds": null, "attempts": null, "valid_completed": null, "status": "not_run"},
    {"name": "ramp-150", "sessions": 150, "min_dwell_seconds": 120, "actual_sessions": null, "reached_at_ref": null, "actual_dwell_seconds": null, "attempts": null, "valid_completed": null, "status": "not_run"},
    {"name": "ramp-200", "sessions": 200, "min_dwell_seconds": 300, "actual_sessions": null, "reached_at_ref": null, "actual_dwell_seconds": null, "attempts": null, "valid_completed": null, "status": "not_run"}
  ],
  "totals": {
    "total_transition_seconds": null,
    "physical_attempts": null,
    "complete_streams": null,
    "failed_before_content": null,
    "failed_after_content": null,
    "provider_terminal_failed_or_incomplete": null,
    "protocol_errors": null,
    "admission_or_quota_rejections": null,
    "lost_worker_attempts": null,
    "http_200_missing_terminal": null,
    "errors": null,
    "cancelled": null,
    "unknown": null,
    "peak_inflight": null,
    "restarts": null,
    "oom": null,
    "observed_spend": null,
    "reserved_ambiguous_bound": null
  },
  "health": {"status": "not_run", "protected_ref": null},
  "cleanup": {"status": "not_run", "protected_ref": null},
  "human_acceptance": {"status": "pending", "owner_ref": null}
}
```

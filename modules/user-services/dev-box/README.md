# Dev Box service contract (v1)

This module is the shared owner-host contract for #7368 and the provider,
identity, reconciliation and assistant-installation work that follows it. The
gateway owns the canonical database migration and authenticated admission at
`/cloud-desktops`; this module does **not** start an EC2 instance or ship a host
controller in this slice. Its independent deploy pipeline belongs to #7369.
The versioned example in `contract-v1.json` is a consumer fixture, not live
provider evidence.

## AWS access as the owner

Connecting and verifying an AWS account in ADP enables its role for the owner and
agents on their Dev Box. New connections enable agent access by default and the
connection screen discloses this. Existing connections with agent access disabled
keep that setting; enable it in Settings → Connections if desired. Only connections
mapped directly to the owner are considered. Team and organization credentials do
not become Dev Box credentials through this path.

The owner shell and Crew receive an AWS `credential_process` configuration
automatically. With one directly mapped connection, ordinary commands such as
`aws sts get-caller-identity` work without login or an explicit assume step. SDKs
that support `credential_process` refresh the temporary session automatically.
The host discovers new connections every minute. With several direct connections,
select a profile shown by `adp-cred list`:

```bash
aws --profile adp-aws-development-<connection-hash> sts get-caller-identity
adp-cred assume --service aws --label development --exec aws sts get-caller-identity
```

Profiles bind to a connection ID. An ambiguous default refuses; disabling or
invalidating a selected connection never switches the caller to another role.
Region comes from the host configuration; use `--region` for a different region.
The EKS personas continue to use the existing `adp-cred assume` transport.

The gateway checks the signed instance identity, current host lease, owner
membership, connection consent and verified secret version for every issuance.
It uses the existing STS broker and limits sessions to 15 minutes. Removing access
blocks further issuance; a session already delivered remains usable until its AWS
expiry. The generated config contains profile metadata only. Credentials pass in
memory to the SDK; ADP does not write them to AWS credential files. Code running
as the owner or Crew can use that owner's enabled connections and must be trusted
with the role's permissions. The audit identifies the owner and host run, without
claiming to distinguish a human shell from a local agent process.

Rollout requires the gateway AWS host route, the guest IAM grant for that exact
route, and a Crew bundle containing `host/adp-cred`. The updated Crew service
creates `/run/adp-crew-aws`; the owner shell setup publishes its SDK configuration.
For an existing box, preserve its workspace and update its runtime through the
normal release procedure. This feature does not require an EKS persona migration.

## Decision: owner code and deletion

A Dev Box may execute the owner's tools only through a fenced machine identity
(#7371), not by inheriting gateway/controller AWS authority. The gateway stores
the owner-local user ID resolved from signed login and current membership, the
organization, and a server-configured deployment ID. No user-supplied owner or
deployment ID is trusted. An instance, volume, operation, event and usage record
carry the same scope and machine generation. The logical box remains after a
replacement; the instance generation changes, so an old device cannot act as
the replacement. A device reference is nullable until enrollment.

Deletion is asynchronous. An accepted `release` sets desired state to
`released`, leaves the observed state unchanged, and reserves the slot until
the provider reconciler has accounted for retained disks and finalized the
box as `released`. An unknown provider result is **not** success: an operation
in `pending` or `unknown` blocks another mutating operation until the controller
has classified the provider outcome (see "Provider outcomes" below); a `failed`
launch, or an `unknown` launch the controller has stopped retrying, no longer
blocks the owner's `release` or `replace`. Unknown-outcome resolution for stop,
start and release belongs to #7370. No admission response asserts `ready`,
`stopped` or `deleted`.

Disabling admission does not hide inventory or prevent an authenticated owner
from requesting stop or release. It denies setup and start; cleanup and
revocation remain possible while new allocation is paused.

For an operator-initiated rollback, run the gateway command
`python -m src.cloud_desktop.rollback --org-id <organization-id> --deployment-id <deployment-id> --policy-id <policy-id>`
from an authorized gateway runtime using its configured database connection. Replace
each placeholder with the exact approved policy identity;
`BG_DEV_BOX_DEPLOYMENT_ID` must match. The command locks that policy row and
disables admissions atomically. Repeating it is safe. It does not call Terraform,
destroy an instance, remove inventory, revoke an existing session, or delete a
retained workspace disk. Stop, release, owner reads and compatible provider
reconciliation remain available. The deployment manifest names a rollback
adapter, but the engine does not invoke that name automatically; an operator
must use the command until a reviewed controller and rollback integration exist.

## Admission and policy

The operator supplies `BG_DEV_BOX_DEPLOYMENT_ID` and an explicitly enabled
`dev_box_policies` row per organization and deployment; absence denies all
new setup and start operations. The approved `dev_box_template_versions` row
fixes the image/profile revision, instance size and disk size. The policy must allow that
size, disk and total live-box and running-box limits. Admission locks the policy
row, checks capacity and existing owner reservation, and writes the logical box,
pending operation and admission
event in one transaction. A non-released box consumes a slot, even if it has
not reached a provider. Until #7372 reconciles running capacity, every such
reservation conservatively consumes both limits. The database also enforces one
live owner reservation, one active operation per box and one digest per
owner/org/deployment/key.

`POST /cloud-desktops` accepts only `{ "template": "assistant", "profile":
"crew" }` plus a required `Idempotency-Key` header. Start, stop and release
are `POST /cloud-desktops/{id}/{action}` with the same header. Every mutation
returns HTTP 202 with `contract_version`, `desktop_id`, `operation_id` and the
operation's status. Read and poll routes return only the caller's scoped box.
Unknown or foreign identifiers return 404, rejected policy/quota/template
returns 403, and conflicting operations, keys or existing reservations return
409. Missing membership returns 403 before a reservation can be created.

`POST /cloud-desktops/ensure` takes the same template, profile and idempotency
header. It creates the owner’s first box through setup, returns an existing
pending operation while that box changes state, or starts the *same* stopped
box through the existing start admission. An observed running machine returns
HTTP 200 with `operation_id: null` and `progress: "last_observed_running"`; this
does not mean Crew is restored or ready. Pending admissions return HTTP 202
and their real operation ID. A mismatched template, unverified state or reused
key from an earlier completed attempt returns 409; no replacement or second
box is created. Provider work still runs only in the shared controller.

Consumers must preserve the fixture's generation, template revision, status,
readiness/error class, resource retention and usage provenance. Cost records
represent infrastructure intervals and are separate from model accounting.
The gateway migration is the sole Dev Box schema authority; downstream stories
extend it rather than create competing schema heads. Before deploy, configure
approved private networking, IAM/image, size and budget through #7369 and obtain
operator approval. Actual two-owner cloud qualification remains #7217's work.

## Controller operation path

An enabled policy must set a positive maximum lease in minutes before new
admissions; idle stop is optional, but if set must be positive. A successful
setup or explicit start records a new lease. Owners may report host activity or
assistant activity through `POST /cloud-desktops/{desktop_id}/activity` with
`{"kind":"host"}` or `{"kind":"assistant","busy":true}`. Assistant activity
never overwrites the host's activity. A busy report defers idle shutdown for
up to 15 minutes unless the maximum lease expires first. Reporting
`{"kind":"assistant","busy":false}` records consolidation completion. The
periodic controller queues a durable stop on inactivity or lease expiry;
pending lifecycle work delays the decision, and an accepted fresh heartbeat
cancels a not-yet-started idle stop. After provider intent begins, a new
heartbeat is rejected rather than promising the host will remain running.
Expired busy work is recorded as a timeout event, or as interrupted when a
maximum lease takes precedence; neither is silently marked complete.

Admission requires positive `BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES` and
`BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES` alongside the deployment ID and an
enabled organization policy. These are deployment-wide bounds across tenants;
an unset bound denies new setup/start/replacement. The gateway serializes
admissions using a transaction-scoped deployment lock on PostgreSQL, and
counts pending operations and unverified stops against running capacity.
Keep the same deployment bounds in the controller environment so delayed
provider actions are checked against current capacity. A verified stopped
machine frees running capacity, but an unverified stop does not.

Cost admission also requires positive deployment-wide
`BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB`,
`BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS` and
`BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD`, plus organization policy values for
`max_storage_gb`, `max_running_hours`, `budget_usd`, and the approved EC2
instance-hour, disk GB-hour and snapshot GB-hour rates. Pending compute reserves
a full maximum lease; all reserved or retained storage reserves **24 hours** at
the larger approved storage rate. Admission refuses work if observed costs plus
these conservative allowances exceed either limit. These are estimates, not a
real-time AWS billing hard cap. Provider inventory gaps and unpriced resources
remain unknown, never zero-cost evidence; changing approved rates begins a new
observation interval instead of repricing unobserved history.

Authenticated owner status at `GET /cloud-desktops/{desktop_id}` includes
`infrastructure_cost`, with known estimates separate from reconciled billing,
an unknown total when provider coverage has a gap, retained disk/snapshot
obligations, pending cleanup and a sanitized policy reason. It never returns
provider IDs or model spend in that field. `GET /cloud-desktops/policy` reports
whether the smallest approved template can currently be admitted and, if not,
the denial reason even before an owner has a box; a different template can
still be denied separately at setup. Both reads require live owner membership.

The gateway's owner API records admitted operations; it never calls EC2 from the
request. The module release archive includes `controller/`, whose polling entrypoint
reads those same gateway database records and reconciles them against the exact
approved release and Terraform outputs. A controller runtime must install the
matching gateway package, mount the verified release manifest and outputs, set
`BG_DEV_BOX_DEPLOYMENT_ID` to that manifest's deployment, and run
`python -m controller.worker --manifest <approved-manifest> --outputs <verified-outputs>`
from the extracted module directory. The runtime principal must be authorized to
assume the release's dedicated controller role; the worker checks the account and
assumed role, refreshes the session, and limits its polling to its deployment.
Do not enable admission until that runtime has been deployed and observed. The
foundation deployment alone publishes the module archive but does **not** start
this worker or establish guest identity or assistant-profile readiness. Later
lifecycle actions and operator-owned cloud qualification remain separate checks.

An active guest cannot be stopped or released unless its run-registry revocation
is confirmed. A failed or unavailable registry returns 503 before admission;
no successor is admitted, and the owned machine and disk remain accounted for.
Start still verifies the owned workspace disk before acting.

When the verified controller release supplies an encrypted workspace policy, setup
records a stable volume client token before creation and does not succeed until
both the machine and its owned, compatible disk are independently observed.
Stop leaves that disk attached. Start verifies the recorded disk, encryption,
zone, size and attachment before starting the recorded machine. Replacement
requires a stopped old machine and an explicit request; the controller observes
its owned disk detached before launching a new machine generation and attaching
the same disk. A lost attach/detach response stays unknown until observed. Old
instance and disk records remain available for attribution and later release.
Host authority uses the same controller and gateway run registry rather than a
separate guest identity table or a model key on the guest.

### Provider outcomes

A setup or replace launch carries one `ClientToken` (`provider_token`) for its
whole life, and the controller classifies the RunInstances outcome (#8037). A
synchronous refusal (`UnauthorizedOperation`, `InvalidParameter*`,
`Unsupported*`, `InvalidAMIID*`, `InstanceLimitExceeded`, `VcpuLimitExceeded`,
`InsufficientInstanceCapacity`, `MissingParameter`) launched nothing: the
operation becomes `failed` with `error_class = provider_refused:<Code>`, a
`launch_refused` event is recorded and the box stays `requested` with observed
state `unknown`, so the owner may release or replace it — or, for a failed
setup, simply ensure again (see *Capacity fallback* below). Two refusals are
tried further before that: `InsufficientInstanceCapacity` and `Unsupported`
(type not offered in that zone) move the launch to the next admitted size, then
to the next approved subnet. A launch EC2 accepts
and then ends itself — inventory shows `shutting-down`/`terminated`, or
`stopped` with a `StateReason` other than `Client.UserInitiatedShutdown` (for
example `Client.InvalidKMSKey.InvalidState`) — is `failed` the same way with
`error_class = provider_refused:<StateReason.Code>` (or
`instance_terminated_at_launch` without a code), a `launch_terminated` event,
and the instance row recorded with its provider id and observed state so the
release path only verifies the termination. `pending` and `running`
observations are unchanged. Any other error or an
empty inventory read is treated as a send that may have reached EC2: the
controller stays `unknown` and re-sends RunInstances on later ticks **with the
same token**, which EC2 de-duplicates for 24 h, for at most
`DEV_BOX_LAUNCH_RETRY_BOUND` claims (default 12) and only while the operation is
younger than 23 h. After that bound a clean empty inventory read makes the
operation `failed` with `error_class = launch_unconfirmed`. The gateway reads
the same bound: a `release` or `replace` admitted against an `unknown` launch
past the bound (and without a live lease) marks that launch `failed` and
records `launch_abandoned`; a `pending` operation, or a fresh or leased
`unknown` one, still returns 409. The release reconciler then settles each
`failed` launch by one inventory read with the launch's own token and
ownership tags: no instance proves the launch absent (`provider_request_ref`
becomes `launch-absent:<token>`, event `launch_absent`) and the release
continues; a machine found that way is adopted (`launch_adopted`) and
terminated like any recorded instance. A launch that is not `failed`, or an
inventory read that errors, keeps the release `unknown` with
`launch_inventory_unresolved` — a box is never released blind. Every tick that
changes an operation writes
one structured line (`operation_id`, action, status, progress, error class,
provider code) to the controller pod log.

### Capacity fallback (#8423)

A release carries an ordered launch plan: `fallback_sizes` (the template's size
first, then the other `allowed_sizes`, in the order the tfvars list them or as
`fallback_sizes` overrides) and `private_subnets` (the template's subnet first,
then `private_subnet_ids`, each with its availability zone from
`data.aws_subnet`). The controller tries every size within a subnet before the
next subnet, so a box lands in the template's zone whenever any admitted size
is available there. A **replace** only varies the size: the retained workspace
disk pins the zone (EBS volumes never leave it), and a plan with no approved
subnet in that zone fails the replace with `workspace_zone_unavailable` and no
provider call. Each fallback attempt carries a fresh `ClientToken` derived from
the operation's token and the attempt index, committed (with the size on the
instance row and a `launch_capacity_fallback` event) **before** it is sent, so a
controller that dies between attempts re-sends exactly that attempt with its own
token — the #8037 rule that an uncertain send is never replayed under a new
token holds, because only a *definite* refusal advances the plan. When the plan
is exhausted the operation fails as before and the controller log names every
`(size, zone)` refused. The guest/controller IAM admits exactly the plan
(`ec2:InstanceType` list, one subnet ARN per approved subnet), `runtime.py`
verifies that, and usage is priced at the rate of the size that launched
(`instance_hour_rates_usd`, a size without an entry inherits
`instance_hour_rate_usd`).

A **setup** whose launch was refused (`provider_refused:*`, `launch_unconfirmed`,
`launch_abandoned`, `workspace_zone_unavailable`) with no machine recorded is
retried by `POST /cloud-desktops/ensure`: the box advances a generation and a new
setup is admitted under the normal capacity check (`setup_retry_admitted`
event). The Dev Box page's failure alert offers that Retry. A failed replace is
not retried this way; the owner releases or waits for the next tick as before.

### Recovery after a controller restart

The controller is stateless between ticks: every claim re-derives the next step
from the operation's `progress` marker and a fresh provider read, so a restart
(deploy, node drain, crash) during a setup, start or replace resumes rather than
replays. The attach step persists `volume_intent` before CreateVolume,
`attachment_intent` once the disk is observed and recorded, and
`attachment_requested` immediately before AttachVolume (#8100); the replace's
detach step persists `detach_intent` before DetachVolume and `admitted` once the
disk is observed free. Both disk observations judge the single attachment against
the two instances the operation may legitimately involve: the **target** (the
generation this operation launched — the recorded instance row, or inventory by
the launch `ClientToken` when the row has no provider id yet) and the
**previous** generation being replaced. A disk `attached` to the target is the
attach step's completed result and the operation proceeds to `running`; a disk
`attaching` to the target or still `detaching` from the previous instance is
`attachment_not_observed` / `detach_not_observed` and is re-read next tick; a
disk that reads `available` after `attachment_requested` is attached again
(EC2 moves a disk to `attaching` synchronously, so an `available` disk has no
attach in flight and a second AttachVolume cannot double-attach). Only an
attachment to an instance that is neither previous nor target is
`workspace_attached_elsewhere`, and it is never detached by the controller.

These observations are bounded. `workspace_provider_unknown`,
`workspace_not_observed`, `attachment_not_observed`, `detach_not_observed`,
`workspace_attached_elsewhere` and `replacement_old_instance_unverified` park
the operation as `unknown` while the retry window is open (`provider_unknown`
on the launch inventory itself is not bounded: an unconfirmed launch stays
`unknown`, as above, rather than being settled blind); once the same
observation is reported on consecutive claims past `DEV_BOX_LAUNCH_RETRY_BOUND`
(default 12, roughly two minutes at the default 10 s interval) or the 23 h token
window, the operation becomes `failed` with that `error_class`, records a
`recovery_exhausted` event and logs a StateReason-style detail (which disk, which
holder, which instances were expected). A clean tick clears `error_class`, so one
late transient does not fail a slow but healthy launch. A `failed` operation no
longer blocks the owner: `release` and `replace` are admitted directly. While the
operation is still `unknown`, a `release`/`replace` request receives 409 with the
unresolved class, the claim count against the bound and roughly how long until
the controller fails it (`DEV_BOX_RECONCILE_INTERVAL_SECONDS`, default 10, is the
gateway's estimate of the controller interval); `launch_abandoned()` remains the
owner-side fence past the bound should that final tick never commit.

## Assistant-host identity

Host-run admission is off unless the approved release includes both a run-registry
ARN for the **same** registry used by gateway run binding and the exact gateway
API/stage execution ARN. The module deployment scopes the controller's assumed
role to that table; the guest keeps its SSM-management role and only the
narrow model-route invocation grant. The platform operator supplies the private
`ADP_DEV_BOX_RUN_TABLE_ARN` deployment variable from the approved gateway
registry before enabling this option; absence does not grant a fallback role.
When that registry is encrypted with a customer KMS key, set
`ADP_DEV_BOX_RUN_TABLE_KMS_KEY_ARN` to the exact key ARN (same account and region):
the controller gets `kms:Decrypt/Encrypt/GenerateDataKey/DescribeKey` on it via
DynamoDB only (#8139), the release manifest records it as `run_table_kms_key_arn`,
and `runtime.py` accepts exactly that grant; the self-managed stage resolves the
key from the table's SSE settings instead. Leave it empty for an AWS-owned key.
Set `ADP_DEV_BOX_GATEWAY_EXECUTE_ARN` to the approved gateway API **and stage**
execution ARN in that same account and region to permit signed guest calls.
Set `ADP_DEV_BOX_GATEWAY_PRINCIPAL_ARN` to the exact gateway IRSA role ARN
(`TF_VAR_gateway_principal_arn` in `dev-box-deploy.yml`, #8049) to create the SSH
grant role that `adp devbox ssh` depends on; the self-managed stage resolves the
same value from the live `gateway-service` ServiceAccount instead. Leave it empty
and no grant role exists, the gateway renders `BG_DEV_BOX_SSH_GRANT_ROLE_ARN=""`
and `POST /cloud-desktops/{id}/ssh-access` answers 503.
The runtime receipt `runtime.py` writes (`DevBoxRuntimeEvidence` in the gateway's
`deployment_runtime_contract.py`) carries the optional `run_table_kms_key_arn`
from the manifest (#8145): the gateway-side deployment read-back then expects the
controller's `owner-bound-host-runs` policy to hold the table statement plus exactly
the DynamoDB-only KMS statement for that key, and only the table statement when the
receipt names no key (`deployment_dev_box_run_policy_changed` /
`deployment_dev_box_run_key_policy_changed` otherwise). Receipts without the field
remain valid.
Without both values the guest gets no model-route IAM grant, and no network path to
the gateway API either: the guest group's only HTTPS egress is the gateway (EKS) and
SSM endpoint groups plus, in user-data mode, the S3 prefix list. The gateway REST API
is a Regional (public) endpoint that no VPC endpoint can serve, and its hostname
resolves into the region's generic EC2 address space (not the published
`API_GATEWAY` ranges, which are API Gateway's own outbound traffic), so no
destination allowlist exists. An approved gateway therefore adds exactly one HTTPS
egress to the internet through NAT (`aws_vpc_security_group_egress_rule.api_gateway`,
tcp/443 to `0.0.0.0/0`; `runtime.py` requires exactly that rule and refuses any other
port, protocol or IPv6 source). The gateway's identity check stays the control on what
the box can obtain. Without the rule the Crew host bootstrap cannot fetch its restore
scope and readiness never completes (dev 2026-10-19, #8223). The only additional
guest grant is `execute-api:Invoke` on the native
`POST /agent/model/*/invoke` route; no Bedrock, STS assume-role, other gateway
routes, or controller permissions are granted. The guest role's entry in the
gateway IAM agent registry is owned by the module (`infra/agent-registry.tf`,
`aws_dynamodb_table_item.guest_agent`, table from SSM
`/adp/<env>/gateway/agent-registry-table`, `requires_host_identity=true`, created
with the approved gateway; dev 2026-10-19 the entry was missing and the gateway
refused the host, #8223); the host installer in #7222 must use SigV4 from the
instance profile and pass only its controller-issued run ID. API Gateway must
verify the signature and pass its edge-verified assumed-role session identity;
the gateway compares both that instance ID and the approved guest role to the
controller-written run on every request and excludes request and response bodies and IAM headers from
host chat audit records. A run ID copied to another machine is not sufficient.
The gateway recognizes module guest roles from their registry-owned
`adp-dev-box-guest-` role name, independently of the submitted run ID. A custom
guest-role registry entry must set `requires_host_identity=true`. Such callers
cannot select ordinary webhook or EKS runs, including after host retirement;
generic workers retain their existing run-binding behavior.

A signed owner can call `POST /cloud-desktops/{desktop_id}/host-enrollment` with
`{"public_key_digest":"<64 lowercase hex characters>"}` after the recorded
instance is running. The reply has a five-minute challenge and device ID; only
the digest is stored. The host installer owned by #7222 must place the raw
challenge **without a newline** in `/run/adp/dev-box-enrollment` and the approved
public-key bytes in `/run/adp/dev-box-public-key`, both root-only ephemeral files.
The controller uses owned EC2 observation and an SSM command on that exact
instance to compare only the two SHA-256 digests. These files, their contents,
run IDs and any model credentials must never appear in logs or release artifacts.
It polls the same SSM command for at most 30 seconds, allowing initial missing
results and pending execution. Terminal failure or timeout grants no authority.
After a successful result, enrollment rechecks challenge expiry and current
authorization; renewal rechecks the existing session and authorization before
extending it. The controller's exact-table IAM grant uses the item actions
required by DynamoDB transactions, not a `TransactWriteItems` IAM action.
The controller records the issued run on the owner-scoped access session; #7222
must configure a secure, scoped way for its guest proxy to consume that run
without placing a browser token, shared platform role or direct Bedrock access
on the machine. This delivery path is **not** established by this module alone.

Host runs issued before the guest-role binding upgrade lack that recorded role
and are refused by the gateway. Coordinate a fenced stop and fresh enrollment on
the verified current generation; do not hand-edit old run records. If the
model-route ARN is absent, the controller retains registry fencing but cannot
mark new enrollment ready or renew old runs.

A host run and its access session last as long as the box's own lease
(`DevBox.lease_expires_at`, set from policy `max_lease_minutes` on every start) and
end only through an explicit lifecycle action: stop, replace and release revoke
the run and terminate the session before the action is admitted; the box lease
itself retires what remains. There is no shorter run lease and no renewal tick:
the five-minute lease renewed by a controller round-trip over SSM fenced a working
dashboard whenever a gateway rollout, controller restart or SSM delay made one
tick late (#8223). Idle-stop still runs on the activity heartbeat; it is a stop.

`renew_crew_host.py` (the host side of the five-minute renewal) is gone from the image,
the user-data bundle and the controller's required-file check.

A **stop keeps the generation's approved device** (`fence_host(keep_devices=True)`):
the owner's browser approved this machine once, and the same machine comes back on
the same generation. On the next start the controller alone re-binds the host run
(`rebind_host_run`: new run, new access session, executor back to `provisioning`),
then the readiness command verifies the host profile, bundle and certificate before
any lease is published. Replace and release revoke the device: that machine is gone.
A run is never revived from a stale command or replaced by another owner's run. Disabling owner policy or revoking membership denies the next
model request; the controller also sweeps and revokes the remaining row.
Only the non-streaming native `/model/{model_id}/invoke` route admits a host run;
the gateway refuses host streaming and other model routes before invocation.
An already-authorized invocation may finish after revocation: the one-attempt
provider call uses a 10-second connection timeout and a 3600-second response
read timeout (plus gateway processing), so revocation fences new requests
rather than instantly cancelling provider work. Fresh requests read the
current registry row and fail. A registry error leaves the transition unadmitted and
its result uncertain until a verified retry; it is not a successful stop or
release. Cloud proof and cleanup remain #7217's operator-owned work.

## Owner experience (Settings → Dev Box, #8094 / #8109 / #8140)

The gateway frontend exposes the owner API at `/settings/dev-box` (lazy route in
`App.tsx`, `FeatureGate` on `crew_dashboard`, nav entry "Dev Box" next to "Runtime").
The page and the chat's "Open my EC2 Crew" action share one hook,
`frontend/src/hooks/useDevBox.ts`, so the flow cannot drift between them. The page
never calls `POST /cloud-desktops/{id}/ssh-access`; that route mints 15-minute
credentials for the CLI's `ProxyCommand` only.

**One action.** *Open my Dev Box* is the only primary control (#8140, owner
feedback: "one simple start devbox button and do enrollment behind it"). Its label
adapts to what the click will do — *Provision and open my Dev Box* (no box), *Start
and open my Dev Box* (stopped), *Open my Dev Box* (otherwise). The click itself does
one synchronous thing: it opens a new tab on the ADP route `/settings/dev-box/open`
(`pages/settings/DevBoxOpen`, via `services/devBoxOpen`), carrying a one-time *open
intent* nonce. Browsers allow `window.open` only inside the click, so a tab opened there
is never a pop-up; the earlier design opened the tab *after* resolving the ticket and
every browser blocked it. That interstitial tab — not the page — runs
`useDevBox().openBox()`, in order:

1. `GET /cloud-desktops` (and `GET /cloud-desktops/crew-template` when there is no box),
   `POST /cloud-desktops/ensure` with a fresh `Idempotency-Key`; poll
   `GET …/operations/{op}` (up to 5 min) while the controller launches or starts the machine.
2. Poll `GET /cloud-desktops/{id}` until `status=observed_state=running` (up to 10 min,
   covering a user-data Crew bootstrap).
3. `GET /crew/target`. **Enroll this host from the browser only when the desktop read says
   `host_binding: "none"`** (no device approved for this generation yet), once, through the headless
   `services/crewHostEnrollment.enrollCrewHost` (`GET
   …/host-enrollment-key` → `POST …/host-enrollment` → `POST …/host-enrollment/{device}/sealed`;
   the device key is generated in the browser and never leaves it). A 409 *"A host
   enrollment is pending"* / *"A host run is already active"* is **awaited, not
   re-submitted**; a 409 *"Current host has not been verified"* / *"…key is not
   available"* is retried on the next poll (the controller has not attested the host
   yet); any other refusal stops the flow **by name** (`Host enrollment refused: <server
   detail>`) with Retry. 409 `lease_fenced` on an `enrolled` or `active` binding is the
   controller's to resolve (it re-binds the run after every start by itself): the page shows
   the controller's `host_readiness_refusal` when there is one and keeps polling; it never
   enrolls a second time. 404 `crew_scope_refused` is a named refusal, not a missing target.
4. Poll `GET /crew/target` until 200 naming exactly this `desktop_id` + `machine_generation`.
5. `POST /crew/handoff-ticket` (resolve) and then `location.replace(handoff_url)` in
   that same tab: a top-level navigation on the Crew origin (which is what lets it set
   the first-party `adp_crew` cookie), so the interstitial becomes the Crew dashboard
   and never stays in history. It is never a blank tab: it shows "Connecting to your
   Dev Box…", the lifecycle timeline, and refusals by name with Retry and "Back to Dev
   Box". ADP stays open in the first tab, which watches the box (re-reads every 5 s,
   up to 15 min) until Crew is ready or the tab is gone; "coming back" is a tab switch.
   If a blocker still refuses the click-time open, the page offers the interstitial as a
   visible "Open in new tab" link (same origin, no `noopener` — `noopener` makes
   `window.open` return `null` even on success, which is what made the old
   blocked-tab detection unreliable).

Re-clicking while that tab is open focuses it instead of opening another. The intent
nonce lives for two minutes in `localStorage` and is consumed once; without it (direct
visit, reload, stale link, or a cross-site navigation to the URL) the interstitial is
**connect-only**: it hands over a box that is already running with Crew ready and
otherwise names the state and points back to the Dev Box page — it never provisions,
starts or enrolls. The flow therefore runs in exactly one tab; the page and the chat
action make no API call on the click. The lifecycle timeline (shown in the
interstitial) records the first-observed time of requested → launching → running →
installing Crew (`user-data bootstrap` when `crew_bootstrap=user-data`) → **enrolling
this host** → **starting Crew** → Crew ready.

| State | What the owner sees | Source |
|---|---|---|
| Policy denied | One sentence (the `reason`), nothing clickable | `GET /cloud-desktops/policy` → `admission_allowed=false` |
| No box, admitted | **Provision and open my Dev Box** only | `GET /cloud-desktops` empty + policy allowed |
| Stopped | **Start and open my Dev Box** (primary); Replace, Release | `boxState()` in `useDevBox.ts` |
| Running | **Open my Dev Box** (primary) with the Crew-target state as help text (*Crew is ready on this host* / *Crew is installed; opening the box enrolls this host automatically* / *lease fenced…*); the inline **Connect via SSH** block right under the button (how it works, alias `adp-devbox-<name>`, prerequisites, copyable `adp update` / `adp devbox ssh-config <name>` / `adp devbox ssh <name>`, VS Code / Kiro hint, host-key note; shown whenever a box exists), Stop, Replace (**disabled**: the API requires a stopped, verified workspace), Release; status/observed/desired, generation, template + revision, lease countdown, resources, bootstrap receipt when the desktop read carries `crew_bootstrap_receipt` | desktop read, `GET /crew/target`, `GET …/resources` |
| Transitioning (`requested` launch, replace in flight, start/stop admitted but not yet observed) | **Open my Dev Box** (joins the in-flight operation) + Refresh; status line | `boxState()` |
| Failed | Release only; a setup whose launch EC2 refused stays `requested` and the failure alert's **Retry** admits a fresh setup (#8423) | `boxState()`; `ensure` |
| Failure | Named: operation `desktop_operation_failed — <error_class> at step “<progress>”`, `Host enrollment refused: …`, server `detail` for 403/409 refusals, the blocked-migration sentence for `crew_migration_blocked`; Retry. No raw JSON | `OperationResponse`, enrollment routes |
| Release | Modal that refuses to submit until **retain** or **delete** is chosen | `ReleaseRequest {workspace_retention}` |
| Admin (org/platform admin only) | Read-only "Release values": bootstrap mode, template + revision, admission. Image revision, profile revision and model pin are SSM-only and are not exposed by the owner API | desktop/policy reads |

There is no separate Start, Enroll or Open dashboard button. The manual enrollment
view (`components/chat/CrewHostEnrollment.tsx`, the thin view over the same headless
function) remains on Settings → Runtime under **Advanced: enroll my Crew box manually**
for the case where an owner wants to enroll without opening the dashboard; nothing
else needs it.

Component tests: `frontend/src/__tests__/pages/DevBox.test.tsx`,
`frontend/src/__tests__/hooks/useDevBox.test.tsx`,
`frontend/src/__tests__/components/OpenMyCrewBox.test.tsx`. SSH setup from the
laptop is described in [connect-from-laptop.md](../../../docs/kiro-crew/connect-from-laptop.md)
and the CLI prerequisites in [adp-cli/getting-started.md](../../../docs/adp-cli/getting-started.md#dev-box-ssh-prerequisites).

## Explicit release and retention

Release accepts only an authenticated owner's explicit
`POST /cloud-desktops/{desktop_id}/release` request with an `Idempotency-Key`
and JSON `{"workspace_retention":"retain"}` or
`{"workspace_retention":"delete"}`. This choice is stored on the operation and
cannot change on idempotent replay. The worker fences guest access, then
terminates only recorded, ownership-verified machine generations. It keeps the
box reserved and the operation unknown while a provider result is uncertain.
For `delete`, it removes only the recorded owned disk after machines are
verified terminated, and records independent volume disappearance before
marking release complete. For `retain`, the disk and any recorded snapshots
remain attributed; a retained disk still counts against the policy capacity
limit after release. Owner-scoped `GET /cloud-desktops/{desktop_id}/resources`
shows retention and deletion state during recovery and after release. Neither
a provider acknowledgement nor a controller restart is proof of deletion.

## Assistant EC2 scenario and CI

The existing CLI uplift assistant suite includes E51, a two-owner Dev Box
setup/stop/start/replacement/release journey. It remains blocked unless the
private run configuration includes `dev_box_lifecycle` with `approved: true`,
the exact `platform_account` as `account`, the exact `region`, an approved
`template` and `profile`, and an `approved_budget_usd` within the harness's
configured daily limit. Do not select it without operator-approved spend and a
verified running controller. On-instance intent and operation IDs are journaled;
after a crash, reconcile and release recorded resources rather than submitting
another setup. The API-only journey does **not** assert live provider/SSM proof,
so even a successful local journey remains BLOCKED in the suite until #7217's
operator-owned qualification checks independent provider inventory, identity
fencing, retained cost and cleanup for the deployed revision. No CI job
provisions a cloud fixture. Module CI runs the gateway-mounted controller,
fake EC2/SSM lifecycle tests and the offline assistant harness in addition to
the release/image checks. The existing E51 journey accepts an offline-only
`identity_probe` adapter: it checks owner enrollment, wrong-owner and replay
denial, controller-backed attestation, owner/tenant attribution, wrong machine,
role, run and model-route denial, and fencing after stop, replacement and
release. The adapter's public-key digest and transient challenge/run ID stay in
memory; its journal records only owner fixture, box, generation and intent or
verified state. No provider adapter is installed into the live harness by
this module. #7217 must wire an approved deployed provider/SSM/guest adapter
and independently verify the release and cleanup before claiming live
acceptance; an offline probe cannot lift E51's blocked live status.

## Crew host profile (repository installer)

`image/install_crew.py --manifest <approved-profile.json> --bundle <local-crew.tar.gz>`
installs a locally supplied profile; it never fetches `latest` or embeds an
owner credential. The manifest has exactly `schema_version: 1`, `revision`,
`source_revision` (full commit), `crew_version: "0.11.2"`, and `bundle_sha256`.
The SHA-256 covers the complete tarball of the pinned Crew Python environment,
Claude clients, default agent, embedding model, and private guest transport;
the tarball must contain `venv/bin/kirocrew`, both client executables under
`clients/node_modules/.bin`, `default-agent.json`,
`models/qwen3-embedding-0.6b-q8_0.gguf`, a pinned `host/node`, and the bundled
`host/crew-host-dashboard.js` built by `npm run build:host-dashboard` and
`host/crew-host-bootstrap.js` built by `npm run build:host-bootstrap` in the
agent package. The release
owner must supply the approved bundle and profile to the selected host; an
arbitrary guest manifest or unverified image is not approval. The installer
extracts to an immutable revision, validates its contents, switches the
`current` link atomically and reports its revision and checksum. It does not
write to `/mnt/crew`. `/mnt/crew` is the owner's retained workspace disk: the
controller attaches it (`/dev/xvdf`) and, since #8223, the readiness command
first mounts it there (`identity.workspace_mount_command`: device addressed by
EBS volume id, formatted xfs only when it carries no filesystem, mounted
`noatime,nodev,nosuid`, recorded in fstab with `nofail`), so the restore helper
finds an empty, root-owned home. The local-only `adp-crew.service` runs as `agent` and
will not start without `/run/adp/crew-ready`. The host keeps its own state on the same
disk under the root-only `/mnt/crew/.adp-host` (excluded from checkpoints, ignored by
the first-boot restore): the box's SSH host keys (`ssh-host-keys/`, so a Replace keeps
one fingerprint), the owner's home (`home/ec2-user`, bind-mounted over `/home/ec2-user`
— seeded once from the image's skeleton, then the developer's own: tmux, Codex and
Claude Code state survive a Replace) and the shared `workspace/` (bind-mounted at
`/workspace`, setgid group `crew` = the owner and the Crew agent, so checkouts are the
same for an SSH session, Kiro Crew, Codex and Claude Code). `adp-crew.service`
will not start without `/run/adp/crew-ready`; provisioning this profile alone
is **not** host enrollment, checkpoint restoration or dashboard readiness.

The pinned bundle must also include `host/crew-host-model.js`, built with
`npm run build:host-model`. `adp-crew.service` now starts through this bundled
model supervisor, not by launching KiroCrew directly. It requires both a matching
root-managed host readiness receipt and `/run/adp/crew-model.json` with the
current `run_id`, `lease_generation`, exact approved `gateway_execute_arn` and
`model_id`. The configured model must also pass the gateway registry policy.
The supervisor obtains temporary instance-profile credentials, signs only the
non-streaming gateway model route, and gives Crew an ephemeral loopback Messages
key. A matching restored Crew profile must select the Claude backend and approved
model; it is verified read-only before the supervisor launches the service.
Missing or stale authority prevents startup or new model requests; denied
gateway calls are not retried. The image contains no run ID, model credential or
endpoint. Installing the profile does not provision this runtime configuration:
the verified controller still needs a secure host-bound receipt and config
delivery path before activating a host run. No plaintext run ID may be placed
in SSM command parameters. Offline adapter tests do not prove installed model
usage or live revocation.

**ADP tools in Crew on the box (#8380).** The same supervisor is the box's tools
path. The bundle carries the ADP tools MCP shim under `host/adp/` (`adp-mcp.py`
and the modules it imports, run by the bundle's own `venv/bin/python3`). When the
supervisor has started the loopback adapter it registers the `adp-tools` server in
the Crew agent specification and Claude Code settings on `/mnt/crew`
(`renderCrewToolsServer`), pointing the shim at that adapter with the adapter's
secret and the current host run (`ADP_CREW_TOOLS_URL`, `ADP_CREW_TOOLS_TOKEN`,
`ADP_CREW_RUN_ID`). The shim's `GET /tools`, `POST /tools/call`, recovery and
receipt routes reach the adapter, which forwards each one through the signed
`POST /agent/crew/tools` gateway route with the instance-role SigV4 signature and
the current model token, after a token refresh, exactly like a model call. The
gateway proves the caller is the current host of the owner's live run, mints a
30-second host assertion bound to tenant, owner, run, box, instance and
generation, and relays to the tools façade with that assertion as the only
identity; the façade forwards it to the gateway's tools authorize plane, which
resolves the owner's live target again and applies the owner's policy, consent,
entitlements, budgets and receipts as for a laptop call. Nothing on the box runs
`adp login`; a stopped or replaced box loses tools with its lease. The guest role
gains exactly one route, `POST /agent/crew/tools`. The shim keeps its operation
ledger in `/mnt/crew/.adp`, which checkpoints exclude.

The Crew image also contains a root-only enrollment input writer. It accepts
the challenge and public-key bytes only on standard input, rejects untrusted
paths and writes mode-0600 files under `/run/adp` without printing their
contents. The image does not run the writer or contain enrollment inputs.
Never pass plaintext inputs in SSM command parameters or logs.
The writer can generate a host-local, mode-0600 RSA key pair with
`--generate-key` (only the public key is returned), then accept a base64-encoded
RSA-OAEP/SHA-256 sealed challenge on standard input with `--sealed`. Concurrent
requests share a lock; after interrupted publication it recovers the public
key from the private key rather than replacing an already registered identity.
Encryption alone does not authenticate the sender. After checking the selected
image, owner and running instance, the controller obtains only the guest's
public key and offers it through an owner-authenticated, no-store gateway
lookup. The signed-in owner registers its digest, seals the returned short-lived
challenge with that key, and submits only the ciphertext through the scoped
gateway route. The controller rechecks the current owner, generation, recorded
image and provider-observed host before sending ciphertext to the root-only
writer; it compares only resulting digests and clears consumed ciphertext.
Failed or stale delivery stays pending; no run is issued without a sealed
submission on the pinned Crew image. The owner-facing UI and an installed
gateway-to-guest check are still missing. This exchange does not provision
certificates, restore the workspace or make the dashboard ready on its own.

`image/crew.pkr.hcl` can build a separate versioned Crew image from the
approved private Dev Box base without changing the generic base recipe. Supply
only local, approved profile manifest and bundle paths plus the exact source,
profile and image revisions; the image build invokes the installer with both
release revision checks and verifies the bundle digest before publishing the
immutable profile. Its temporary build inputs are removed; no token, TLS key,
owner workspace or unpinned network download is part of the image. The Dev Box
release must select and verify this derived image before an owner machine uses
it. The deployment workflow does **not** build it: the release operator must
supply the approved derived image ID and `ADP_DEV_BOX_CREW_PROFILE_REVISION`
when enabling dashboard ingress. When the run registry and gateway route are
selected, the release operator must also set the public
`ADP_DEV_BOX_CREW_MODEL_ID` to the approved gateway model (not a guest secret).
Release admission rejects a missing profile or model pin and readback rejects an AMI whose profile tag differs from that pin. Image
installation alone does not start Crew or make it `crew_ready`. The
controller checks the selected host's installed profile revision, source
revision, required service files and possession of the matching root-only
private enrollment key through a secret-free SSM readback before issuing or
renewing a pinned host run. That check does not establish that the
service is running or that owner data has been restored. Pinned host enrollment
prepares an inactive run and keeps gateway and model routing pending until those
checks can establish readiness. Enrollment does not activate that run.
The run store atomically activates the prepared host run and its lease marker
only for the same live owner, instance, deployment and generation. The controller
requests authenticated on-box discovery and exact-version restore using only the
public gateway ARN and model ID in SSM; it passes a hash of the run ID (never
the run ID or restore grant) to read back the root-owned restore receipt and
running service health before admitting the host. An owned host refreshes its
local lease expiry after the registry renewal is confirmed; a failed renewal
leaves the old expiry in place and fails closed.
Retrying enrollment on the same machine fences an old run only when both the
recorded access session and the run registry lease have expired; an apparently
expired database session cannot displace a still-live run. A retry needs a newly
generated public key; reusing the earlier key returns a conflict rather than
reusing its identity.
The controller must still provide per-host certificates, scoped run authority and
verified restore receipts before starting the service and registering readiness.


The installer also writes `adp-crew-host.service`, which runs the bundled guest
HTTPS proxy as `agent` only after the Crew service and `/run/adp/crew-ready`
are present. Starting it additionally needs root-managed `/run/adp/crew-host.json`
and a gateway-client CA file in `/run/adp/crew-host/`. The host creates its
self-signed private-IP certificate and key locally at the current machine
generation; only its certificate fingerprint is read by the attested controller
and stored against that generation. The gateway verifies that pin before
sending dashboard traffic. The private key never leaves the box. The host receipt must bind the current run, owner,
logical box, instance, generation, profile revision and restored manifest
version to `/run/adp/crew-ready`. The guest checks those bindings on every new
request and verifies the gateway's client-certificate fingerprint and signed
mint envelope; a missing or stale receipt fails closed. No certificate or
signing secret is embedded in the profile. The controller sets database `crew_ready` only after the on-box exact-version
restore, trusted gateway client CA publication, receipt readback and service
health succeed; the offline installer does not assert those steps happened.

### Stop/start and Replace resume the Crew home (#8223 causes 18/19)

`/run/adp` is tmpfs: a stop/start loses every host receipt (enrollment key, host
certificate, `crew-ready`) while the retained workspace disk keeps the Crew home.
Two legs make a same-generation restart a *resume* instead of a dead end:

* **Controller.** An observed stop parks the owner's `CrewExecutor` for that
  generation (`ready` → `stopped`, `state.py::park_host_executor`). The enrollment
  that follows the start mints a new provisioning run and returns the executor to
  `provisioning` (`identity.py::resume_host_executor`, also from a stale `ready`),
  which is the state readiness, the gateway's host-restore context and lease
  activation all key on. Without it no readiness command was ever sent and the UI
  showed `lease_fenced` forever.
* **Host.** The bootstrap issues **one idempotent command** to `restore_crew_host.py`
  (mode `ensure`, #8459): the restorer answers `ready` (this run's receipts are already
  published), `resumed` (a home this box provisioned — root-only marker
  `.adp-host/provision.json` — was re-pinned to the new run and Crew started, data untouched),
  `needs_grant` (empty home, the scope pins a checkpoint: the bootstrap fetches the restore
  grant and asks again) or `restored` (fresh first host, or restored from the pinned
  checkpoint). An occupied home without a trusted marker is refused by name
  (`CrewHomeUnknownProvenance`); receipts for another run are refused, never replaced. The
  marker is bound to tenant, user and box — not to the machine generation, so a **Replace onto
  the retained disk resumes the workspace** too. The older `probe`/`restore`/`resume` modes
  stay callable for one release; nothing calls them.

The controller passes the gateway's `CREW_DASHBOARD_ORIGIN` to the host as
`ADP_CREW_DASHBOARD_ORIGIN`; `--configure` renders it into Kiro Crew's
`dashboard.url`, whose derived Host allow-list is what the browser must match when it
arrives through the edge (cause 20). Bundles that predate the variable ignore it.

`--configure` also seeds Kiro Crew's own first-run marker (`.kiro_cli_setup_complete`,
content `complete`, owner-only) and the second required agent spec
`.kiro/agents/kirocrew-lite.json` when they are absent (#8223 cause 30). Kiro Crew wraps
its dashboard in a Kiro CLI prerequisite gate that renders the application only when the
install is "established" (that marker, or earlier sessions/history) or a signed-in
`kiro-cli` is probed. A platform home runs the Claude Code backend and never has
`kiro-cli`, so without the marker every fresh box showed "set up Kiro CLI" and no agent
could start. The platform performed the setup, so it records it the way Kiro Crew does;
nothing in Kiro Crew is changed and existing files are never rewritten.

The controller has no Deployment env of its own: it reads `CREW_DASHBOARD_ORIGIN` and
`FEATURE_CREW_DASHBOARD` from the shared `bedrockgateway-config` ConfigMap, which both
gateway renderers fill from SSM `/adp/<env>/gateway/crew-dashboard-origin` and
`feature-crew-dashboard` (the same parameters the gateway Deployment env uses). When the
dashboard feature is on and the origin is missing or malformed, readiness refuses the tick
with the event `crew_dashboard_origin_missing` on the box's current operation and a
controller warning, and retries on the next tick, instead of configuring a loopback
`dashboard.url` that the edge can never match. With the feature off, the host is
configured without an origin as before.

### Checkpoints from the box (#8379)

The retained disk protects the Crew home against stop/start and Replace, not against
losing the volume, releasing the box or moving to another zone or account. The host
therefore publishes **verified S3 checkpoints** of its home through the same system the
crew pods use (#7202), with nothing but its instance role:

* **What.** `image/checkpoint_crew_host.py` captures the allowlisted home with
  `crew_checkpoint.capture` (memory databases through SQLite online backup, every other
  approved file byte-exact; `config.json`, crons, credentials, keys and the root-only
  `.adp-host` marker are excluded, as before). The archive is capped at 512 MiB.
* **When.** `adp-crew-checkpoint.timer` runs a background (non-quiesced) capture every
  15 minutes while the host is ready; an unchanged inventory uploads nothing (the last
  verified files digest is remembered root-only in `/mnt/crew/.adp-host/checkpoint.json`).
  Before a stop the controller runs the **final** capture over SSM
  (`controller/checkpoint.py`: `--mode final` stops the Crew units first, so the manifest
  is a quiesced one), waits at most 150 s and records the outcome on the stop operation as
  `pre_stop_checkpoint_{verified,unchanged,failed,skipped}`; the stop proceeds in every
  case (fail-open: the disk still holds the home and the last periodic checkpoint remains
  the recovery point). `adp-crew-checkpoint-final.service` repeats the final capture from
  its `ExecStop` at shutdown for stops the controller did not drive; after a
  controller-driven stop it finds the inventory unchanged.
* **How.** The host signs `POST /agent/crew/host-checkpoint/begin` and `/complete` with
  SigV4 from its instance role (credentials from IMDSv2, stdlib only) and uploads with the
  gateway-issued presigned PUTs. The gateway binds the instance to exactly one running
  box, its owner and the owner's `ready` executor for that generation, admits the capture
  with the current verified checkpoint as predecessor, verifies the exact S3 versions and
  advances `latest_verified_checkpoint_id` in one transaction
  (`src/crew/checkpoint_routes.py`). The binding deliberately does not need the live run:
  both stop paths fence host authority at admission, and the final capture runs after that
  fence while the machine is still up; publication is still fenced on the current
  generation, so a retired generation cannot publish. The guest grant names the two routes
  next to the model, token-refresh and host-restore routes.
* **Restore precedence.** A home this box provisioned (marker present) **resumes**; only an
  empty home restores — fresh for a first host (#8171), otherwise from the owner's latest
  verified checkpoint, which a Replace inherits. So a Replace onto a **new** disk, or a
  released box provisioned again, comes back with the owner's memory.

Live verification after a deploy: stop the box from the UI, then
`aws s3 ls s3://adp-<env>-crew-checkpoints-<account>/ --recursive` shows
`crew-checkpoints/<owner-key>/<checkpoint-id>/{archive,manifest}` and the stop operation
carries a `pre_stop_checkpoint_*` event. The UI does not show the last checkpoint yet
(follow-up).

## Crew bootstrap modes (#8055)

The Crew profile reaches a guest in one of two ways, selected by the foundation
variable `crew_bootstrap` (`environments/<env>/modules/dev-box.tfvars`, or
`deploy-dev-box.sh --crew-bootstrap`). Both consume the same artifact: the bundle and
profile manifest `install_crew.py` validates, addressed by the bundle digest at
`s3://adp-terraform-state-<account>/dev-box/image-inputs/<bundle-sha256>/{crew.tar.gz,profile.json}`
(the prefix `build-dev-box-image.sh --crew-bundle` and the bundle producer, #8054, upload to).

| | `image` (default) | `user-data` |
|---|---|---|
| Where the profile is installed | By `image/crew.pkr.hcl` into a derived AMI; the launch template carries **no user data** (byte-identical to the module before #8055) | At first boot of the **base AMI**, by `infra/crew-bootstrap.sh.tftpl` rendered into the launch template's user data |
| Pins | AMI tag `adp:profile-revision`; the release manifest's `crew_profile_revision` must equal it | `crew_bundle_sha256`, `crew_bundle_s3_uri`, `crew_profile_revision`, `crew_source_revision` (the bundle's own commit — the base AMI was built from another) and the SHA-256 of every helper script, all rendered into user data |
| Launch time | Base + nothing | + download and install of the bundle (minutes, bundle-size dependent). A **stopped box that starts again does not pay it**: cloud-init runs the script once per instance and the receipt is restored from `/var/lib/adp` by `tmpfiles.d` |
| Immutability | The running host is the verified image | **Deviation from image immutability**: the host is base image + a content-addressed install performed at boot. Every fetched object is verified against a digest that is part of the reviewed Terraform state before use, so nothing unpinned is downloaded; the trade is that the install happens on the guest rather than in the image build |
| Guest grants | SSM core (+ model route when pinned) | + `s3:GetObject` on `arn:aws:s3:::<state-bucket>/dev-box/image-inputs/*` (`crew-bootstrap-inputs-read`) and one 443 egress to the S3 **gateway endpoint prefix list**. The bucket is SSE-S3, so no KMS grant |
| Controller readback | `crew_profile_command`: current link, `profile.json` revision + source revision, required files | The same check **plus** the root-owned receipt `/run/adp/crew-bootstrap.json` must record `status: ok` for exactly the pinned profile revision, source revision and bundle digest; a missing, failed, foreign or writable receipt fails closed (`controller/identity.py`, `tests/test_controller_crew_bootstrap.py`) |
| Release readback (`runtime.py`) | Rejects any user data; AMI profile tag must match | Requires user data that pins the digest, location, both revisions, digest verification and the receipt path, and contains no secret-shaped content; accepts the base AMI (no profile tag) or an AMI carrying the same profile; requires the prefix-list egress and the scoped read grant, nothing more |
| Use | Production-grade hosts | Fast dev iteration on the Crew bundle without an AMI build |

User data carries no secrets: object paths, digests and revisions only (`AC-04`:
`aws ec2 describe-launch-template-versions --launch-template-id <id> --versions <n>
--query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d`).
The bootstrap: waits for the instance credentials (retrying `aws s3 cp`), downloads
bundle + manifest to a root-only `/var/tmp` directory, verifies `sha256sum`, checks
the manifest against the pins, fetches the helpers from `<prefix>/adp/` and verifies
each digest, runs `install_crew.py --expected-source-revision --expected-profile-revision`,
installs the same root-only helpers with the same modes as `crew.pkr.hcl`, runs a
readiness preflight (the controller's layout check, a syntax pass over the helpers,
`verify_crew_ready.py --help`, the two units), writes the receipt and removes its
inputs. Nothing starts Crew; enrollment, restore and `crew_ready` remain the
controller's job exactly as in image mode. Log: `/var/log/adp-crew-bootstrap.log`.

Interpreter: both the bootstrap and `crew.pkr.hcl` run `install_crew.py` and the
preflight on the bundle's relocatable CPython 3.12 (`python/bin/python3`, staged out of
the digest-verified tarball) when the bundle carries one, else on AL2023's system
`python3` (3.9); the receipt records which (`interpreter`). The installed helpers are
executed by the systemd units and the controller's SSM commands with `/usr/bin/python3`,
so every file under `image/*.py` and `crew_checkpoint.py` must stay importable on 3.9
(`tests/test_guest_python_compat.py` compiles them against the 3.9 grammar and imports
them under a 3.9 interpreter when one is available).

`GET /cloud-desktops/{id}` reports the deployment's mode as `crew_bootstrap`
(`BG_DEV_BOX_CREW_BOOTSTRAP`, rendered by both gateway renderers from SSM
`/adp/<env>/dev-box/crew-bootstrap`; absent reads as `image`).

Stage (self-managed, after the bundle exists in S3):

```bash
modules/user-services/dev-box/scripts/deploy-dev-box.sh --env dev --skip-image --skip-verify \
  --crew-bootstrap user-data --crew-bundle-sha <sha256> --profile-revision <rev> \
  --crew-model-id <approved gateway model id>
# --crew-bundle-sha / --profile-revision default to the bundle build-crew-bundle.sh last
# published (SSM crew-bundle-sha256 / crew-bundle-revision). The stage reads
# <prefix>/profile.json for the source revision, uploads the helper scripts to
# <prefix>/adp/, applies the foundation (new launch template version), verifies the
# release and redeploys the controller. SSM crew-bootstrap keeps the mode for later
# runs without flags; `--crew-bootstrap image` switches back.
```

ADP-managed: `ADP_DEV_BOX_CREW_BOOTSTRAP`, `ADP_DEV_BOX_CREW_BUNDLE_S3_URI`,
`ADP_DEV_BOX_CREW_BUNDLE_SHA256`, `ADP_DEV_BOX_CREW_SOURCE_REVISION` environment
variables (`.github/workflows/dev-box-deploy.yml`) feed both the release context and
the `TF_VAR_*` set, so the manifest and the applied foundation agree.

## Self-managed deployment in the platform account (optional module)

Dev Box is an **optional module**: the core platform never requires it, and an
environment without `environments/<env>/modules/dev-box.tfvars` is exactly
today's platform (empty `BG_DEV_BOX_*` in the gateway, no desktop option in the
UI, no Dev Box call in `deploy-all.sh`). Everything Dev Box-specific lives in this
directory; `platform/scripts/deploy-all.sh` holds only the switch, the journal note
and one call into `scripts/deploy-dev-box.sh`.

| Path | Purpose |
|---|---|
| `scripts/deploy-dev-box.sh` | The stage entrypoint (`deploy-all.sh --dev-box on` Step 12, `--dev-box-only`, or standalone). Seven steps: platform prerequisites → image → foundation → release manifest + `runtime.py` read-back → gateway ConfigMap + controller → seed → live verification. Refuses with the missing input named (exit 2). |
| `scripts/build-dev-box-image.sh` | Runs `image/buildspec.yml` in CodeBuild `adp-<env>-dev-box-image` (Packer, Session Manager transport, private build instance); publishes `/adp/<env>/dev-box/image-id` and its revisions. Add `--crew-bundle --crew-manifest --profile-revision` (local files or the `s3://…/dev-box/image-inputs/<sha>/` URIs the bundle build publishes) for the Crew image. |
| `scripts/build-crew-bundle.sh` + `scripts/crew_bundle.py` | Produce `crew.tar.gz` + `profile.json` from the pins in `image/crew-bundle.pins` (CodeBuild by default through `image/bundle-buildspec.yml`; `--local` on Linux x86_64). See "Crew image pipeline". |
| `scripts/release-crew-image.sh` + `scripts/crew_release_pin.py` | Bundle → Crew AMI → tag read-back → release pin (SSM `image-id`, `image-revision`, `profile-revision`, `crew-model-id`; tfvars `crew_model_id`) → stage. Plan by default, `--pin` writes, `--apply` also runs the stage. |
| `.github/workflows/dev-box-crew-image.yml` | ADP-managed run of the same two scripts (`--pin`) under the `dev-box-deploy.yml` connection identity; uploads the pin plan. |
| `scripts/self_managed_release.py` | Builds the same release manifest as the ADP-managed workflow from the foundation's `release_context` output. |
| `scripts/verify-dev-box.py` | Live acceptance: creates the operator's box through the API, waits for `running`, checks SSM `Online`, reports Crew readiness, releases. Exit codes in the header. |
| `infra/platform/` | Workspace KMS key, SSM interface endpoints + group, image CodeBuild project (platform VPC has none of these). |
| `infra/` (`main.tf` + `self_managed.tf`) | The foundation, plus the controller runtime IRSA role, the policy/template values and SSM `/adp/<env>/dev-box/*`. |
| `infra/crew_bootstrap.tf` + `infra/crew-bootstrap.sh.tftpl` | Crew bootstrap modes (#8055): `crew_bootstrap = image \| user-data`; the user-data mode's pinned first-boot script, S3 read grant and gateway-endpoint egress. |
| `k8s/controller.yaml` | How `controller/worker.py` runs: Deployment `dev-box-controller` in `adp-gateway`, gateway image, code and release from ConfigMaps, IRSA `adp-<env>-dev-box-controller-runtime`. |
| `.github/workflows/dev-box-deploy.yml` | The ADP-managed alternative for a customer connection (GitHub protected environment). Same foundation, same manifest. |

Operator sequence on an environment whose gateway is deployed:

```bash
# 1. inputs (dev ships them): environments/<env>/modules/dev-box.tfvars + dev-box-platform.tfvars
# 2. whole platform with the module, or the module alone on an existing environment
./platform/scripts/deploy-all.sh --update --dev-box on        # or: --dev-box-only
# 3. acceptance as the owner (also what the stage runs when `adp token` works)
python3 modules/user-services/dev-box/scripts/verify-dev-box.py --env <env>
# later: a Crew image with a pinned profile arms the guest's model route and the dashboard
modules/user-services/dev-box/scripts/release-crew-image.sh --env <env> --crew-model-id <approved id> --apply
# (bundle → Crew AMI → read-back → SSM + tfvars pin → stage; see "Crew image pipeline")
```

The seed never writes SQL from the operator box: `python -m src.cloud_desktop.seed`
runs inside a gateway pod with Terraform's `policy_spec` output and reports
`changed: false` on repeat. The UI shows the desktop option once the seed has added
`ec2-instance` to the organization's `crew.allowed_targets` and the gateway carries
`BG_DEV_BOX_DEPLOYMENT_ID`; both renderers (gateway-deploy.yml and deploy-all.sh)
read that value from `/adp/<env>/dev-box/deployment-id` on every deploy.

### Keeping the controller current (refresh-controller.sh)

The controller runs the gateway release image with `controller/*.py` mounted from the
ConfigMap `dev-box-controller-code`. Two things drift it from `main` in a live
environment, and both happened on dev on 2026-10-18 (#8208 merged, the gateway
rolled twice, the controller kept six-hour-old code and image):

| Drift | Cause | Converged by |
|---|---|---|
| image | a gateway deploy rolls `deploy/bedrockgateway` only | `gateway-deploy.yml` job `refresh-dev-box-controller`, after the backend rollout, off the critical path (`continue-on-error`, bounded wait) |
| code | a merge touching `controller/**` runs `dev-box-ci.yml` (tests) only | `dev-box-controller-deploy.yml` on push to `main` (dev) |
| either, by hand | operator on the box | `scripts/deploy-dev-box.sh --controller-only` |

All three call `scripts/refresh-controller.sh`, which re-applies the ConfigMap from the
checkout, pins the controller to a digest-addressed `adp-gateway` image (default: the one
the gateway runs), records the source revision on the pod template and waits for the
rollout. It exits 0 without changes where Dev Box is not deployed, so callers need no
guard. A floating tag is refused; a failed rollout prints the controller log and exits 1.

### SSH access wiring (`adp devbox ssh`, #8001 / #8049)

`POST /cloud-desktops/{id}/ssh-access` (`src/cloud_desktop/ssh_access.py`) assumes
the module's SSH grant role (`infra/ssh-grant.tf`) for 15-minute credentials scoped
to one instance. Three pieces carry it, all inside this module:

| Piece | Where | Set by |
|---|---|---|
| Grant role `adp-dev-box-ssh-grant-*`, trusting only `gateway_principal_arn` | `infra/ssh-grant.tf` | foundation apply; `deploy-dev-box.sh` passes `-var gateway_principal_arn=<gateway IRSA role>` read from `serviceaccount/gateway-service` in `adp-gateway` (refuses with exit 2 when the annotation is absent); `dev-box-deploy.yml` passes `ADP_DEV_BOX_GATEWAY_PRINCIPAL_ARN` |
| Inline policy `adp-dev-box-assume-ssh-grant` on the gateway role (`sts:AssumeRole` on the grant role only) | `infra/ssh-grant.tf` (`aws_iam_role_policy.gateway_ssh_grant`) | same apply — no gateway-infra change is needed to enable SSH |
| SSM `/adp/<env>/dev-box/ssh-grant-role-arn` → ConfigMap `BG_DEV_BOX_SSH_GRANT_ROLE_ARN` | `infra/self_managed.tf` → `modules/gateway/k8s/configmap.yaml` | stage step 5 patches the live ConfigMap; `gateway-deploy.yml` and `deploy-all.sh` render the same key on every deploy (absent, `" "` or not a role ARN ⇒ `""`, route 503) |

Rollout on an existing environment: `scripts/deploy-dev-box.sh --env <env>
--skip-image --skip-verify` (creates the grant role, the gateway inline policy
and the SSM value, patches the ConfigMap, restarts the gateway), then
`adp devbox ssh <box>` from a laptop with `adp login`. Verify with
`aws ssm get-parameter --name /adp/<env>/dev-box/ssh-grant-role-arn` and
`kubectl -n adp-gateway get configmap bedrockgateway-config -o jsonpath='{.data.BG_DEV_BOX_SSH_GRANT_ROLE_ARN}'`
(both the grant role ARN).


### Reconnectable terminal sessions (#8560)

The base Dev Box AMI installs the distribution's `tmux` package and prepares an
`ec2-user` configuration. An interactive terminal login for that user attaches
(or creates) the private `main` session. A non-interactive SSH command, SCP and
other OS users do not auto-attach. The default tmux socket lives under the
user's mode-0700 `/tmp/tmux-<uid>` directory; the launcher uses a private umask
and never shares a socket path with the Crew `agent` user. Shells *outside* tmux
retain the 900-second idle timeout; tmux panes start without the inherited
`TMOUT` so detached sessions stay alive. The login notice explains that session
processes survive a connection drop but **not** instance stop/start or Replace.
Nothing is added to the Crew checkpoint allowlist.

To check a staged AMI after the Dev Box delivery owner has replaced one box,
connect as `ec2-user` through the existing SSH or SSM path. In **terminal A**,
run `sleep 600`, disconnect, then reconnect in **terminal B** and use `tmux ls`
to confirm the `main` session and running command. From an independent
non-interactive SSH command, verify `echo ok` runs and SCP completes without
attaching. As the `agent` OS user, `tmux ls` must not list the owner's session
and attaching through the default socket must fail; verify the socket directory
is 0700. Finally, stop/start the box, reconnect, and confirm the old `sleep`
process is gone. This is a live procedure, not a result established by the
offline image tests. Do not share transcripts containing private box identifiers.

### GitHub as the owner: `gh` and `git` on the box (#8555)

A box has no GitHub credential of its own. The owner connects GitHub once in
Settings → Connections (AgentCore Identity holds the provider token; ADP keeps the
pointer and the observed account, as for the GitHub tool) and ticks "Include
repository access" (or "Grant repository access" later). The same connection
serves people who never use a box: `adp github token` on a laptop asks
`POST /auth/connections/oauth/github/token` with the owner's session.

On the box the path is the one the model and tools already use:

| Piece | What it does |
|---|---|
| `POST /agent/crew/github-token` (`src/crew/host_github.py`) | Same host proof as `/crew/tools` (instance identity + current host run + model token). Resolves the owner's consented GitHub connection with the repository grant (`src/crew/github_access.py`), fetches the token from AgentCore Identity, re-observes the account, writes `crew_github_token_minted` to the audit log and answers `{token, login, expires_at}` with `expires_at` one hour out. Refusals: 404 `github_not_connected`, 403 `github_consent_required` / `github_repository_consent_required`, 409 `github_reconnect_required` or `lease_fenced`, 503 `github_user_access_disabled` while `ADP_GITHUB_USER_ACCESS_ENABLED` is not `true`. |
| Guest role `approved-model-route-only` + `runtime.py` | The route is in the guest policy and the stage verifier's expected list. |
| Adapter `GET /github/token` (`crew-model-adapter.ts`) | Mints through the signed route, keeps the token in memory until five minutes before its horizon, clears it on suspend/close, and turns a refusal into the sentence the box prints. |
| `host/adp-github-token`, `host/gh`, `host/git-credential-adp` (bundle `host/`, first on PATH) | `gh` is the real GitHub CLI with `GH_TOKEN` served per call; the git credential helper answers `get` for `https://github.com` and stores nothing. The owner shell's helper reads the adapter address from the private `/run/adp-dev-box-shell/model.env` when the shell has not called `adp_dev_box_model` yet. |
| Image (`prepare_shell.sh`) + bundle pins | The image installs `git` and sets the system `credential.https://github.com.helper`; the bundle carries the pinned GitHub CLI release as `host/gh-cli/gh` (the build instance has no egress to cli.github.com). |
| Checkpoints | `hosts.yml` and `.git-credentials` are already refused by the allowlist; nothing on the box writes a token anyway. |

Fencing is inherited: stop, Replace or release ends the lease, and the next mint
is refused like a model call. Disconnecting GitHub or pausing "Allow agents to
use this account" stops new mints within the hour. Operator setup is the existing
one for the GitHub OAuth app in Settings → Connections (platform admin registers
the app's client id and secret; the app's callback is AgentCore's).

### The box CLI acts with the owner's full authority (#9640)

Until #9640 the `adp` CLI on a box (owner shell and Kiro Crew sessions alike) was
limited to Product/flow reads and inert flow drafts, and saw Products at
membership scope. The owner decided their own Dev Box must have the same access as
their laptop CLI, so an agent there can monitor flows and resolve what blocks them.

How it works now: the CLI's host-run binding (the loopback adapter, its secret and
the current host run) is exchanged through the adapter for an **owner session**:
`POST /crew/orchestration/owner-session` on the gateway, callable only by the verified
host run with its current model token. The gateway mints a one-hour token in its
revocable token table that *is* the owner's login-equivalent context (their workspace
user, their team, their platform-admin standing read from Cognito groups) and carries
`delegation = dev-box:<box>:<host_run>`. The CLI caches it under `~/.adp/crew-owner-session.json`
(0600; refreshed five minutes before expiry or when the run changes) and then calls
the public API (`gateway_url` from the mint response; `ADP_PUBLIC_API_URL` overrides the
`CORS_ALLOWED_ORIGINS` default) exactly as the laptop does. Every request made with the
session is logged with that `delegation`, and the mint itself is audited
(`dev_box_owner_session_minted`). A fenced or expired host run cannot mint; an
existing session lasts at most an hour.

The public API accepts the session through its own trusted path
(`src/auth/owner_session.py`, wired into `get_current_user` and the token middleware
beside Cognito validation): signature, expiry and database revocation are checked on
every request, the subject (the owner's verified login subject, resolved with
`login_subject_for_user`, so linked workspace accounts whose login lives in their
placement link are handled) must still resolve to an unrevoked member of the session's
workspace, platform-admin standing is re-decided exactly as for a Cognito login
(database revocations plus current Cognito groups, so offboarding takes effect on a
live session), and the delegation is attribution only. Ordinary legacy exchange tokens
carry no `dev-box:` delegation and keep their existing restrictions. The regression
`tests/auth/test_owner_session_auth.py` mints a session on the full application and
uses it against `/v1/products` and the gate route as member, admin, demoted admin,
linked-account admin, revoked session, removed membership, forged and legacy tokens,
and checks that the `request_end` log lines of those public requests carry the owner
as principal and the exact `dev-box:` delegation (the dependency stores the session
context on the request state, since public paths are outside the token middleware).

Consequences: `adp flow gate approve|reject`, `adp flow budget|retry|node|execution`,
`adp product create|update|lifecycle|source-*` and `adp product budget set` work on the
box; `adp product list` shows what the owner sees in the dashboard. Code running as the
owner or Crew on the box therefore holds the owner's full ADP authority and must be
trusted accordingly; the earlier read/draft relay remains for older bundles.

### ADP skills from the Kiro Crew dashboard (#9589)

A dashboard session is not the owner shell. Kiro Crew runs Claude Code and Codex as
the `agent` user in the retained Crew home `/mnt/crew`; the owner shell path
(`prepare_shell.sh`, the `claude()`/`codex()` functions, `/home/ec2-user`) never
touches that home. Until #9589 a dashboard session therefore found neither the ADP
skills nor the box CLI's host-run binding (dev generation 67, 2026-11-04: the agent
looked for `~/.claude/skills/adp-flow` under `/mnt/crew`, found nothing and stopped).

What `crew-host-model.js` does at every Crew start (`crew-host-model-entrypoint.ts`):

| Piece | What it does |
|---|---|
| `seedCrewHomeSkills` | Runs the bundle's `host/adp/adp_skills.py` for `claude-code` and `codex` with `--home /mnt/crew` and the verified `host/adp/skills` source — the same installer, source and provenance markers as `adp claude setup --skills-only` in the owner shell. Idempotent; a modified or user-owned skill directory is left alone with a warning. A failure is journaled (`crew_home_skills_unseeded`) and Kiro Crew still starts. |
| Kiro Crew gateway environment | `PATH` has the bundle's `host/adp` right after `host/` (the bundle's Node stays first), and `ADP_CREW_TOOLS_URL`, `ADP_CREW_TOOLS_TOKEN`, `ADP_CREW_RUN_ID` carry the loopback adapter and this host run, exactly as the owner shell's `model.env` and the ADP tools shim receive them. `adp status --json` inside a dashboard session therefore answers `{"mode":"host-run",…}`. |
| `writeCrewHomeGuidance` (#9655) | Writes one managed block (HTML-comment markers, always at the top of the file, rewritten at every start; everything after it is the user's and is kept byte for byte; a file whose markers are misplaced or unmatched is left untouched and journaled as `crew_home_guidance_ambiguous`) into `/mnt/crew/.claude/CLAUDE.md` and `/mnt/crew/.codex/AGENTS.md`, and creates `/mnt/crew/workspace`. It states what nothing on the box used to: this home is retained and the session scratch area is not, so checkouts go under `/mnt/crew/workspace/<repo>`; commit messages must reference their issue (`commit_refs_rules`); the box CLI acts with the owner's authority and gate decisions stay the owner's; the owner's connected AWS accounts are reachable through `AWS_CONFIG_FILE=/run/adp-crew-aws/config`, `adp-cred list`, `aws --profile …` / `adp-cred assume …`, 15-minute sessions, and what the credential errors mean. No secrets, no per-boot values beyond the region. Advisory: a failure journals `crew_home_guidance_unwritten` and Kiro Crew still starts. Origin: on 2026-11-06 a Crew session built an adp-flow plan in its scratch area, the push was refused for a missing issue reference, and the resume discarded the plan. |
| `writeCrewHomeCodexConfig` (#9628) | Claude Code is configured by the `ANTHROPIC_*` environment; Codex reads a provider from `~/.codex/config.toml`, so the entrypoint writes a managed block into `/mnt/crew/.codex/config.toml` at every start: the same `adp-shell` provider the owner shell's `codex()` function passes (this boot's loopback adapter `/v1`, responses API, the published model, `web_search` disabled, the `adp-tools` MCP server, the home trusted) and exports `OPENAI_API_KEY` beside `ANTHROPIC_API_KEY`. Settings outside the marker lines (Codex's own `[tui]`, the user's) are kept; a user file that already defines those keys is left alone and `crew_home_codex_config_user_owned` is journaled. Advisory like the skills: a failure journals `crew_home_codex_unconfigured` and Kiro Crew still starts. |

Using the `adp-flow` skill from the dashboard: keep repository checkouts under the
Crew home (`/mnt/crew/workspace/<repo>`; the guidance block above tells the agent so, and
the skill refuses a scratch-directory checkout); ask for the skill in plain
words or with Kiro Crew's `$adp-flow` mention. Kiro Crew auto-approves Claude Code's
read-only built-ins (Read, Grep, Glob) and the ADP tools server; every `adp …` Bash
call asks for the owner's approval once in the dashboard unless the session runs in
YOLO mode. Since #9640 the CLI there acts with the owner's own authority (see above),
gate decisions included.

## Crew image pipeline

Nothing in the repository is downloaded without a pin, and nothing in the image
is produced by hand. `image/crew-bundle.pins` holds every input of the bundle;
the parity test keeps the kirocrew wheel, `awscli` and embedding-model pins
identical to `modules/agent-factory/agent/Dockerfile.sandbox`, and adds two the
guest needs but the container does not:

| Bundle member | Pinned input |
|---|---|
| `venv/` (kirocrew 0.11.2 + awscli 1.46.1), `default-agent.json` | wheel URL + sha256 from the Dockerfile; relocated to `/opt/adp/crew/current/venv` |
| `python/` | CPython 3.12 `install_only_stripped` x86_64 tarball + sha256 from astral-sh/python-build-standalone `SHA256SUMS` — kirocrew requires >= 3.12, the AL2023 guest has 3.9 and the private build instance has no package egress |
| `clients/node_modules` | `npm ci` from `modules/agent-factory/agent/kirocrew-clients/package-lock.json` |
| `host/node` | `bin/node` of the Node 22 LTS linux-x64 tarball + sha256 from `nodejs.org/dist/v<ver>/SHASUMS256.txt` (also the npm used to build) |
| `host/crew-host-{model,dashboard,bootstrap}.js` | `npm run build:host-model` / `build:host-dashboard` / `build:host-bootstrap` in `modules/agent-factory/agent` at the bound commit |
| `models/qwen3-embedding-0.6b-q8_0.gguf` | model URL + sha256 from the Dockerfile |

`scripts/crew_bundle.py pack` writes the archive deterministically (sorted
members, root ownership, commit-time mtimes, gzip without a timestamp, no
`__pycache__` or pip `direct_url.json`) and `profile.json` with exactly
`{schema_version: 1, revision, source_revision, crew_version: "0.11.2",
bundle_sha256}`, then validates both with `image/install_crew.py`'s own checks.
The default revision is `crew-<yyyymmdd>-<sha12>`; `source_revision` is the full
HEAD commit. Transitive Python dependencies of `awscli` are resolved by pip at
build time; two builds of the same commit on the same day are byte-identical only
when that resolution is unchanged.

Where it runs: `scripts/build-crew-bundle.sh --codebuild` (default) starts the
module's image CodeBuild project with `image/bundle-buildspec.yml`
(`codebuild-run.sh` → `CODEBUILD_BUILDSPEC_OVERRIDE`), which runs the same script
with `--local`, uploads `crew.tar.gz` + `profile.json` to
`s3://adp-terraform-state-<acct>/dev-box/image-inputs/<bundle-sha256>/` and
publishes `/adp/<env>/dev-box/crew-bundle-sha256` and `crew-bundle-revision`. The
operator box only receives `profile.json` and `bundle.env` (URIs, revision,
digest) under `.adp-dev-box/<env>/crew-bundle/`. `--local` assembles on a Linux
x86_64 box with `curl`, `tar`, `xz`, `python3`; `--offline-stage` is the test-only
path that packs a prepared tree without network access.

One command releases a Crew image on the self-managed platform account:

```bash
modules/user-services/dev-box/scripts/release-crew-image.sh --env dev --crew-model-id <approved id> --apply
#   [1/4] build-crew-bundle.sh --codebuild        → s3://…/dev-box/image-inputs/<sha>/{crew.tar.gz,profile.json}
#   [2/4] build-dev-box-image.sh --crew-bundle s3://… --crew-manifest s3://… --profile-revision crew-<date>-<sha12>
#         (image/buildspec.yml re-verifies bundle_sha256 and prints the size; the Packer build instance
#          fetches the bundle from S3 itself into /var/tmp and digest-checks it — the ~1 GB never
#          streams through the session tunnel into tmpfs; builds base + Crew AMIs, publishes
#          /adp/dev/dev-box/{image-id,image-revision,image-source-revision,base-image-id,profile-revision})
#   [3/4] read-back: the published AMI must carry adp:profile-revision / adp:image-revision /
#         adp:source-revision equal to the pin; any difference refuses the release
#   [4/4] crew_release_pin.py: SSM image-id, image-revision, profile-revision, crew-model-id (+ the
#         environments/dev/modules/dev-box.tfvars crew_model_id line, diff printed) and a JSON journal
#         line in .adp-dev-box/dev/release/crew-image-journal.jsonl; then
#         deploy-dev-box.sh --env dev --skip-image --skip-verify
```

Without `--apply` the command builds and prints the pin plan but writes nothing;
`--pin` writes the pin without running the stage; `--skip-image` re-pins the AMI
already in SSM (for example to change the model id); `--dry-run` touches no
CodeBuild run and plans from whatever SSM holds. The ADP-managed path
(`.github/workflows/dev-box-crew-image.yml`, inputs `environment`,
`crew_model_id`, optional `profile_revision`) runs the same script with `--pin`
under the `dev-box-deploy.yml` connection and uploads the plan, whose
`github_variables` block names the `ADP_DEV_BOX_IMAGE_ID`,
`ADP_DEV_BOX_IMAGE_REVISION`, `ADP_DEV_BOX_CREW_PROFILE_REVISION` and
`ADP_DEV_BOX_CREW_MODEL_ID` values `dev-box-deploy.yml` then applies. Acceptance is
live, not the merged PR: a box launched through `POST /cloud-desktops/ensure`
(profile `crew`) must reach `crew_ready=true` and open the dashboard
(`scripts/verify-dev-box.py --env <env> --require-crew`).


## Resume-path defects found on the first real stop/start (2026-10-20)

The first same-generation stop/start on dev exposed five defects in the resume path, fixed together:

- `restore_crew_host.py` demanded a **root-owned** Crew home for `probe`/`resume`, but a restore hands the
  home to the `agent` user, so every real resume was refused ("untrusted Crew host directory"). The home is
  now accepted when owned by root or `agent` (never group/world-writable, never a symlink); `/run/adp` and the
  `.adp-host` marker directory still require root.
- Refusals lost their reason: the restorer printed only the exception class and the bootstrap only `Error`.
  Both now print class **and** fixed message; the controller records `host_refused:<Class>` on the
  generation's instance row and one `DevBoxEvent` per class, the desktop read exposes it as
  `host_readiness_refusal`, and the Dev Box page shows it instead of a bare "lease fenced".
- `checkpoint_crew_host.py` could not import `crew_checkpoint` under `python3 -I` (the shutdown capture died
  with `ModuleNotFoundError`); the helper now names its own directory on `sys.path`.
- The host-model service exited 1 silently when start-up failed. It now names the reason on stderr. The reason
  on dev was a **stale authority receipt**: readiness retries outlived the run lease the receipt carried, so
  `crew-host.json` said the lease had expired. A republished scope for the same run with a later expiry now
  renews the receipt instead of being skipped as "identical" or refused as "different".
- `--configure` on resume carries `ADP_CREW_DASHBOARD_ORIGIN` exactly as on restore (tested).

Run the image/controller tests with `umask 022`; CI does.

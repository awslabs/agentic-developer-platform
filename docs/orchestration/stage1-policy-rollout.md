# Stage 1 execution policy contract and rollout

Corrective work for #5128 under #5134, dependent on ownership PR #5169.
Implementation, merged revisions and live acceptance are separate milestones.
Stage 2 and Q1 are outside this work. Live A0/A1 gates remain NOT RUN.

## Cross-account deployment compatibility — release blocker

Existing workers must continue assuming user-created roles in non-platform AWS
accounts. Both saved connections (`adp-cred assume`, including `--exec`) and
direct `aws sts assume-role`/SDK calls are in scope. Existing role ARNs, trusted
source principals, external IDs, permissions, session tags, duration, refresh,
role chaining and deployment region must remain usable without customer IAM edits.

The compatibility implementation includes the owner's approved user-credential
contract. Activation still requires the scoped rollout and live acceptance:

- Version 2 policies can explicitly select vault credential IDs and exact customer
  role ARNs with their user-configured permissions. Saved roles and direct SDK
  source sessions support this authority for currently implemented task actions.
  This does not add an engine deployment/coordinator dispatcher: those remain
  distinct prerequisites in #5174. Existing human/legacy Operations deployment
  paths retain their semantics until a verified compatible migration.
- The protected platform role still denies direct STS. Task SDKs now use
  `adp-cred worker-session` as a refreshable default source provider. The gateway
  mints a restricted session of the original worker role, preserving the principal
  named in customer role trusts. The source session permits cross-account
  AssumeRole, session tags/source identity and GetCallerIdentity; it explicitly
  denies other AWS operations and assumption of platform-account roles. The
  customer's ordinary SDK controls the destination ExternalId, session options
  and chaining; ADP does not inject a destination session policy.
- Issuance remains disabled until the source principal's platform Kubernetes
  access is removed in an approved rollout. IAM session policies do not restrict
  Kubernetes authentication. Each issuance checks the configured cluster's EKS
  access entry and, where applicable, aws-auth role/user/account mappings. The
  rollout must inventory all platform clusters/regions and implicit creator
  access before setting `AGENT_TASK_SOURCE_ISOLATION_CONFIRMED=true`. That
  acknowledgment is not an automated account-wide inventory. Never restore
  Kubernetes access while any issued source session remains live.
- The authority switch changes the shared ScaledJob/service account and gateway
  authentication. Keeping a flag off on one worker is not proof of a compatible
  separate deployment cohort; that routing and authentication path needs design
  and verification before it can be proposed.

Keep existing deployment execution available while this is resolved. Do not
automatically migrate these runs, require customers to rewrite role policies,
grant unrestricted STS access to policy workers, or bypass accepted deployment
gates. The 17-create prerequisite plan in #5176 is historical plan evidence and
is not sufficient for a compatible release or ready for apply approval.

The transport correction preserves refreshable platform IRSA in dedicated
`ADP_WORKER_IRSA_*` variables before ordinary AWS variables are replaced or
removed. Customer tools retain their credentials/profile and region; platform
model, broker, trigger and provenance signatures use platform identity and the
gateway region. Nested `--exec` preserves the original platform identity.
Protected CLI requests carry current signed run/pod proof and reject redirects;
missing platform identity refuses the call without selecting customer credentials.

Source credentials are bounded by the live grant and the STS 15-minute minimum;
refresh rechecks isolation and authority. Cancellation or a shortened grant
blocks delivery after provider lookup. Auditing persists user/run/grant and
expiry without keys. Already-issued source or destination sessions remain usable
until AWS expiry/revocation; the source session policy does not constrain the
destination role session. Customer environment/shared keys and named/default
profiles keep normal SDK precedence. Generated config files are private and
cleaned up after worker execution. Platform SDK clients and beads subprocesses
restore platform credentials and region. The loopback model hop skips local AWS
auth; the proxy still signs upstream using protected identity and run/pod proof.

Regression coverage exercises real SDK refresh and signing with disposable STS
responses, nested CLI environments and a real TLS proxy receiver. Gateway tests
preserve the saved role's STS parameters without adding `Policy` or `PolicyArns`
for human, legacy policy-less and explicitly accepted version 2 user authority.
These tests do not prove live IAM trust or engine deployment dispatch support. Acceptance must additionally run
both existing assumption paths against an authorized non-platform fixture,
deploy/read back/clean up a disposable resource, refresh and chain roles, and
keep gateway/GitHub/model calls working during deployment. Record source and
destination caller identities and sanitized provider evidence; never credentials.

## Vault API-key compatibility — release blocker

Workers must also retain existing authorized use of user-supplied API keys and
other credentials through ADP vault. Preserve service/label selection, existing
user/team/org authorization, proxy credential injection, enabled raw-key delivery
to SDKs/tools, and file materialization where configured. Existing scope flags,
destination restrictions, audit records and credential rotation remain effective;
this requirement does not enable vault capabilities that were previously disabled.

Protected broker binding now covers proxy, materialize and metadata as well as
saved roles and raw keys. User/invocation/pod proof and the canonical tenant must
match before vault access. Raw/file delivery requires registry-granted capability
plus the existing caller scope; a header alone cannot grant it. CLI scope headers
and the proposed IAM route allowlist are wired. Endpoint tests exercise API-key
rotation, proxy destination restrictions, file URLs and sanitized auditing.
These paths support human-authorized and legacy policy-less runs.

The owner resolved the credential-authority decision on 2026-09-15: **“yes, if the
role has permissions (given by the user) adp should be able to do it”**, following
explicit confirmation that workers must also use API keys through ADP vault.
This is the recorded design amendment to #5128/#5174, implemented as an explicit
version 2 contract. It is not authorization for infrastructure activation.

A plan may accept `user_credentials` with required
`permission_mode: user_configured`, `lifetime: provider_managed`, selected
`vault_credential_ids`, exact `aws_role_arns`, and the existing task `actions`.
At least one target is required; wildcard roles, duplicate targets and actions
outside the policy are rejected. Credential selection uses existing vault ACLs at
human acceptance and again during execution. A model cannot accept this policy.
Version 1 remains the default and keeps its old serialization/hash and constrained
credential semantics. Old accepted policies do not acquire user authority.

The broker binds the actual credential ID/secret reference, authenticated user,
tenant, current assignment, membership/role, accepted version, gates and limits.
Raw/file delivery still needs enabled delivery and registry/client scopes; proxy
host restrictions and rotation remain effective. The direct source can assume
only accepted customer role targets. Destination roles and API keys retain their
configured provider permissions; ADP does not insert a destination STS policy.
The accepted plan summary displays these selections and lifetime limits.

Cancellation/expiry stops new issuance/refresh and is rechecked before provider
effects and credential delivery. Already-issued sessions, copied keys and file
URLs follow provider lifetime/revocation; ADP does not claim to revoke them
instantly or constrain every external action they permit. Task approvals remain
required even if a selected provider credential is capable of broader effects.
Removing a policy by amendment withdraws authority from dispatch and workers,
without reverting the flow to legacy permissions. Truly policy-less flows keep
legacy behavior. Live provider acceptance remains required before migration.

Live release acceptance must exercise an authorized API call through vault proxy
injection and a tool/SDK consuming an enabled raw API key, plus file delivery
where configured. Verify the selected service/label, credential rotation, existing
access refusals, audit records and operation while customer AWS credentials are
loaded. Use a controlled provider fixture and keep API keys out of evidence/logs.
Transport tests alone do not establish vault workflow compatibility. This gate
applies alongside cross-account role compatibility; neither is optional for release.

## Enforced boundaries

The accepted plan supplies the policy; protected execution/grant records supply
the flow, human authority, repository and current node attempt. Caller headers
cannot select a different policy or spend identity. Legacy policy-less plans
retain their existing path. Malformed stored policy refuses execution.

Engine and delegated dispatch check current membership and approval role,
action, scope, limits and ownership before publication. A missing membership
role cannot gain approval authority through the legacy RBAC rollback flag.
Policy refusal before commit leaves the node/attempt unchanged and cancels the
unpublished child reservation. Replays and publication recheck live policy.
Gate nodes remain human-only even when an internal caller supplies an action.

Protected model requests and credential issuance revalidate the current SQL
assignment, accepted version, principal, role, live grant, scope, ownership and
deadline. The deadline starts at the first committed flow dispatch and survives
retries/amendments. Status, cancellation and cleanup remain available after an
action refusal.

## Provider scope and actual lifetime

| Capability | Current behavior |
| --- | --- |
| Evaluation | Requires machine acceptance for that evaluation; GitHub token has contents/PR/issues/checks/metadata read access |
| Review | GitHub contents/checks/metadata read and PR/issues write |
| Develop/repair | GitHub contents/PR/issues write only if the policy also permits autonomous merge; contents-write otherwise defeats a human merge gate |
| GitHub token lifetime | Requires more than one hour plus mint allowance before grant expiry, policy expiry and flow deadline; returned provider expiry must be current and within all three bounds |
| AWS/raw-secret brokers | Refused for policy-governed work because accepted connection/action scope cannot yet be enforced |
| Autonomous wave coordinator | Refused for policy-governed work; no accepted coordinator capability exists |

GitHub tokens already issued remain usable until their provider expiry or
provider-side revocation. Policy revocation immediately stops subsequent broker
issuance and gateway model requests; it does not retrospectively revoke a token
held by a remote process. Shorter lifetimes, mediated branch writes and AWS
action scoping are implementation prerequisites in
[#5174](https://github.com/aws-e/adp/issues/5174). This scope is narrower than
unrestricted agent operations and must be visible when opting in a flow.

## One allowance across descendants

Dispatch uses the existing reservation service to hold the configured run
ceiling. A policy smaller than that ceiling is refused, never used to clamp a
hold while leaving the worker's actual ceiling unchanged. Model requests use
one stable tenant/flow accumulator across developer, reviewer, repair and eval
runs. Existing user/team/org and run/chain limits also remain effective.

The protected authority table records initialization once, without storing
billing totals. Missing Redis state after initialization is unknown spend.
Existing unmetered work needs reconciliation before opt-in. An unobserved or
unpriced provider result retains its reservation and blocks new spend until
trusted reconciliation. Restarting a worker or amending a plan cannot reset it.

Each actual provider attempt receives a fresh server-generated request ID.
Repeated client `X-Request-ID` values cannot replace an earlier charge. Logging
preserves the same server ID used by budget reservation and usage reconciliation.
The worker proxy refreshes protected run/pod proof on every request and signs
using worker IRSA even when Operations loads task AWS credentials.

Supported policy requests are explicit Anthropic text requests with an output
limit and published model/context/pricing data. The quote reserves the full
published input capacity at the highest applicable input/cache-write rate plus
the requested maximum output. This pessimistic reservation avoids relying on
byte estimates for hidden provider framing; trusted usage returns unused
headroom. The original body frames are replayed unchanged. Upload completion
triggers another authorization check before spend.

Policy-bound Bedrock clients disable SDK retries for both streaming and ordinary
invocations. An ambiguous provider failure retains the original hold; another
attempt must return through admission with a fresh server-owned spend ID.

Responses, non-text inputs, stateful server history/MCP/tools, missing bounds
and unsupported models/routes are refused. Their bounded-provider prerequisite
is [#5175](https://github.com/aws-e/adp/issues/5175).

## Deployment dependencies and acceptance

Follow [the ownership rollout](stage1-ownership-rollout.md) and the canonical
deployment guide. Account `879318057152`, profile `embark1`, region `us-east-1`;
use the existing registered connection and ARC runner. No broad Terraform apply
or EKS allowlist change is part of this work.

The earlier 17-create proposal is historical. Task-source IAM/RBAC/configuration,
a source-role trust update and removal of existing Kubernetes access change its
scope; a fresh reviewed plan is required. #5161's “No IAM change” scope remains
unamended.
It is not a complete activation plan. Before opting in, also
provision the missing exact webhook admission, gateway dispatch and tick
authority permissions; register the protected worker identity; deploy compatible
gateway/tick/webhook/immutable worker revisions; configure protected signing,
ownership, producer authentication and shared budget enforcement.

PR #5176 proposes explicit direct Bedrock denial. Verify that denial before
activation; otherwise gateway spend enforcement is bypassable. AWS/raw-secret
refusals must not be loosened without scoped enforcement, and deployment runs
must not be moved onto that unsupported path. Do not merge webhook infrastructure
edits into its automatic broad apply workflow without a reviewed scoped rollout.

Code validation covers real SQL membership/version/role reads, delegated HTTP
dispatch, broker permission/expiry checks, real Lua reservations and provider
request/reconciliation integration. The real TLS worker proxy test checks exact
body bytes and refreshed identity proof. Review and live acceptance must still
record the exact head, merge SHA, running revisions and A0/A1 evidence listed in
the ownership rollout. #4539 and #4898 require live proof before adoption.

Rollback stops new admissions and reconciles in-flight effects while retaining
claims, accepted versions, authority receipts and the shared spend record.


## Compatibility revision validation (2026-09-15)

The gateway internal/agentauth/runtime-policy regression passes with 958 tests
and four skipped. The worker CI compatibility selection passes with 242 tests;
the additional focused control-token/credential/entrypoint selection passed 191
tests (overlapping coverage, not additive). Node control/identity/beads suites
pass 267 tests at 97.64% line and 92.50% branch coverage; the 85% gate is unchanged.
TypeScript build, scoped gateway Ruff and YAML checks pass. Terraform validation
and 38 manifest tests pass in #5176. Read-only AWS IAM custom-policy simulation
passed nine source-policy action/resource cases, including customer STS allows
and platform-role/non-STS explicit denials. Simulation does not exercise customer
trust policies or an actual assumed session.

The latest pushed CI must be checked independently; these local results do not
assert full CI success. At `a6daef07`, security scanning completed but its summary
gate reported 485 new critical/high findings; the worker runner stopped during
control-token tests without final step logs; the full gateway suite reported ten
vault fixture identity failures (the registered dependency now receives the same
IAM provider double after test module reloads). No gate has been bypassed.
No AWS resources, EKS access or active worker flags were changed. Live acceptance
remains open; the credential semantics amendment above requires explicit v2 human
acceptance before it can apply to a plan.


Validation of the user-permission implementation: **2,572 gateway orchestration/internal/agentauth tests passed, 4 skipped**. The auth-module reload/order regression passed **92 tests**, and final schema/endpoint checks passed **139** (overlapping coverage). **21 PlanSummary tests**, frontend TypeScript/Vite build, scoped Ruff, workflow YAML and diff checks passed. Tests use real SQL, policy acceptance and broker endpoints with provider doubles; they cover preserved role options/permissions, raw key rotation, proxy/file delivery, selection/ACL refusals, cancellation during secret/provider calls, source target refresh, legacy serialization and policy removal. Live provider/A0/A1 acceptance is not established by these tests.

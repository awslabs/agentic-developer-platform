# Stage 1 execution policy contract and rollout

Corrective work for #5128 under #5134, dependent on ownership PR #5169.
Implementation, merged revisions and live acceptance are separate milestones.
Stage 2 and Q1 are outside this work. Live A0/A1 gates remain NOT RUN.

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

The 13-resource bootstrap plan is unapplied and needs the #5161 “No IAM change”
scope amended. It is not a complete activation plan. Before opting in, also
provision the missing exact webhook admission, gateway dispatch and tick
authority permissions; register the protected worker identity; deploy compatible
gateway/tick/webhook/immutable worker revisions; configure protected signing,
ownership, producer authentication and shared budget enforcement.

The proposed protected worker boundary still allows direct Bedrock invocation.
Remove that allowance and verify explicit denial before activation; otherwise
gateway spend enforcement is bypassable. Keep AWS/raw-secret broker refusals in
place. Do not merge webhook infrastructure edits into its automatic broad apply
workflow without a reviewed scoped rollout.

Code validation covers real SQL membership/version/role reads, delegated HTTP
dispatch, broker permission/expiry checks, real Lua reservations and provider
request/reconciliation integration. The real TLS worker proxy test checks exact
body bytes and refreshed identity proof. Review and live acceptance must still
record the exact head, merge SHA, running revisions and A0/A1 evidence listed in
the ownership rollout. #4539 and #4898 require live proof before adoption.

Rollback stops new admissions and reconciles in-flight effects while retaining
claims, accepted versions, authority receipts and the shared spend record.

# Comment authority and truthful dispatch lineage — issue 7845

## Verdict and scope

**Architect-approved / ready for implementation**, as a design decision by the
native Codex SDK architect on **2026-10-16 UTC**. This is not human approval,
independent implementation review, deployment approval, or evidence that the
vulnerabilities have been repaired. Reviewed `main` source:
`c958000f0829b1462c2f4ce23bfb743676b97306`. The task branch initially contained an
empty worker-start commit above that revision; its source tree was identical.

Only a verified repository writer should be able to control a running agent or
approve its next action. Every GitHub dispatch must also meet the installation's
account-eligibility policy, and a machine event must never acquire human authority
through a default lineage value. Implement the contract below, including sibling
channels found during the audit, rather than limiting work to the original file list.

[Story #7845](https://github.com/aws-e/adp/issues/7845) owns acceptance;
[native parent #7840](https://github.com/aws-e/adp/issues/7840) is a dated security
index under [#4489](https://github.com/aws-e/adp/issues/4489), not a deployment
instruction. Both parents and the story/amendment were read. The verified mid-run
clarification adds issue-body refinement to the requested repository design PR.
The [issue-body snapshot](issue-body.md) is the publication source for that edit;
the GitHub issue remains authoritative after publication. The original issue had
no numbered AC or scenario IDs; the refinement introduces stable `AC-01`–`AC-10`
and `SCN-01`–`SCN-12`, without renumbering pre-existing IDs.

The [implementation backlog](backlog.md) assigns bounded work and integration
order. The [audit and release map](audit-notes.md) explains inventory coverage,
caller reachability, exclusions, release controls and remaining evidence gaps.
No production code, credentials, infrastructure, database schema or running job
is changed by this design PR. No private scan ledger was fetched. Source findings
below are independently observed in public code, not claims about the private
scanner's reproduction or a live incident.

## Confirmed behavior and corrections to the story

| Finding | Source observation and consequence | Exposure boundary |
|---|---|---|
| Monitoring commands lack an author gate | `parseUserCommands` accepts slash commands after only an ID/bot-name filter; built-ins execute before callbacks. Gating `onCommand` alone misses pause, stop and extend. [Monitoring parser and loop](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/monitoring.ts#L884-L919). | Confirmed source defect in the PM monitoring path; not proof this path is running in a particular installation. |
| Superpower permits unauthorized and stale approval | The initial scan accepts old approvals; polling accepts feedback as continuation. Prefix matches accept more than an exact action. Guidance is promoted to privileged instructions while the issue body is fenced. [Approval and prompt code](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/agent-superpower.ts#L299-L415). | Confirmed in the source-built prototype workflow. The hosted worker does not select this executable by persona name. |
| Account floor omits issue and PR events | `_check_min_author_association` returns early for every family except `issue_comment`; errors are swallowed. [Existing floor](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/webhook-ingress/lambda/github/handler.py#L619-L699). | Additional exposure requires a configured floor and otherwise admitted identity. An unknown commenter does not automatically pass the identity resolver. |
| Issue-event lineage is hardcoded human-rooted | Only comments and PRs compute correlation; fallback sets human root/new chain/depth zero. [Fallback and signed authority](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/webhook-ingress/lambda/github/handler.py#L2048-L2077). | False classification is confirmed. Vault escalation is conditional on the legacy authority path and resolvable credential owner; protected authority separately checks the verified human event. Do not claim all current deployments grant vault access from this bit alone. |
| Existing approval fixes must be retained | `ApprovalService` already bounds waits and checks permissions; `skill-agent` checks request IDs and both control/effect repositories. PM reassessment already calls the helper. [Approval service](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/services/ApprovalService.ts#L51-L161), [skill approval](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/skill-agent.ts#L113-L141). | A blanket claim that all approvals lack authorization is false. Consolidation must not regress timeout, target-repository checks or outcome handling. |

Additional in-scope consumers are PM `/monitor`, PM health actions, legacy retry
lock override/guidance, MCP's prompt-embedded approval poll, and hosted AIDLC
synthetic presence. Their exact paths and effects are in the audit, not deferred
to a future inventory exercise. Prompt-only context, reporting deduplication and
independently authenticated dashboard controls are distinguished there.

Two compatibility defects matter to this change: monitoring's paused branch
currently skips command polling, and GitHub CLI node IDs are cast to numbers then
used in numeric comparisons. A safe gate must not preserve those mistakes and
make authorized resume/stop or once-only processing unreliable. Also normalize
`/queryPM` consistently: the parser lowercases input but its vocabulary contains
mixed case. These are bounded changes required by the control regression contract,
not permission to refactor unrelated monitoring behavior.

## Design: authority boundaries and interfaces

### 1. One runtime gate, with bounded freshness

Reuse [comment-authority.ts](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/utils/comment-authority.ts#L1-L28),
not author association, repository ownership inferred from a login, or a model's
interpretation of the comment. The current fixed GitHub origin, encoded path
segments, redirect refusal and 10-second request timeout remain mandatory.
Reuse the installation-token provider/refresh path; never embed tokens in logs,
state, prompts or denial comments.

**Proposed interfaces** in that existing module (names are a design contract,
not currently available APIs):

- `CommentEnvelope`: immutable provider comment ID (opaque string), numeric
  author ID, login, provider author type, body, created/updated timestamps,
  repository, issue/PR number and URL. Fetch adapters retain those fields and
  paginate; a GitHub GraphQL node ID is not a REST integer.
- `CommentAuthorityContext`: current run ID, trusted control repository,
  trusted effect repository if different, token provider, trusted worker identity,
  session start, injected monotonic clock and per-run cache. Missing repository,
  login, identity metadata or token denies; caller-provided comment fields cannot
  override the trusted repository/run context.
- `authorizeComment(comment, context, purpose)` returns a discriminated decision:
  `allowed` with actor, scope and checked time, or `denied` with one bounded reason
  (`machine_author`, `invalid_author`, `insufficient_permission`,
  `permission_unavailable`, `stale_comment`, `wrong_request`). Only an authorized
  descriptor reaches command/effect callbacks. Keep
  `hasRepositoryWritePermission(owner, repo, login, token): Promise<boolean>` as
  a compatibility wrapper; it must not create a second policy implementation.
- Accepted current permission tiers are exactly `admin`, `maintain`, `write`.
  Read, triage, none, unknown/custom-unresolved, malformed JSON, redirects,
  timeout, 401/403/404/429 and network/5xx failures deny. Do not infer write from
  `MEMBER`, `COLLABORATOR`, token ownership or comment body. Match returned actor
  identity to the provider-supplied ID where the endpoint supplies it; retain the
  numeric ID throughout the decision/audit record.

**Freshness decision:** a cache lives for the monitoring session, but its entries
do not. Key by repository, immutable author ID and credential generation; successful
permission results expire after 60 seconds, negative results after 15 seconds,
transport errors after 5 seconds. Coalesce simultaneous requests; bound the cache
(e.g. 256 LRU entries). Eviction never means allow. Clear on token/account changes.
Require a fresh lookup immediately before releasing any approval gate. This
resolves the original story's contradictory “short-lived” versus “session-life”
wording. A control permission revocation has at most a 60-second observation delay;
approval cannot use a cached positive. No new tenant configuration is needed.

Provider type `Bot`, GitHub Actions, trusted runtime self identity and existing
known agent identities remain excluded from interactive controls/approvals even
if they have write permission. Keep bot detection separate from the permission
predicate. **Do not equate `GITHUB_ACTOR` with the agent's identity**: it can name
the human who started the workflow. A writer is not disqualified for being the
triggering human. Machine accounts represented as ordinary GitHub users cannot
be reliably inferred from spelling alone; trusted registered machine identity
metadata must supplement the provider type. Unknown identity metadata denies
privileged interactive actions, rather than guessing.

Denial is an observable result, not an exception converted to success. Record
run/repository/comment ID, author ID/login, purpose, reason, permission tier when
known, cache age and request ID. Never log raw guidance, authorization headers or
provider error bodies. Deduplicate requester notices by comment ID and update one
issue status/denial comment at most once per minute. Machine/self comments get
logs but no reply loop. A notice says permission could not be verified when the
API is unavailable, not that the user definitely lacks write access. Posting a
notice may fail without allowing the action. A user retries with a **new comment**
after recovery; denied historical comments are not silently executed later.

### 2. Monitoring and sibling comment consumers

Make `parseUserCommands` asynchronous and require the authority context before
returning executable commands. The only production caller is the monitoring loop;
update it, callbacks and tests together. Keep lexical parsing internal and pure,
but do not export it as an alternate executable-command producer.

Commands remain `/status`, `/pause`, `/resume`, `/retry`, `/stop`, `/extend`,
`/help`, `/instruct` and `/queryPM`, with exact case-insensitive verb matching.
No new query alias is required. Permission applies even to status/help. Preserve
the existing extend increment and outer run deadline; permission does not grant
an unbounded lifetime or change hosted control policy.

Start the session with a server-time/ID cursor and an explicit triggering comment
when available. Do not replay issue history on a fresh run. Process all pages
since the cursor, ascending by provider creation time and stable ID; use an opaque
seen-ID set, not `Math.max` on GraphQL IDs. Equal timestamps do not lose comments.
Recognized command comments are accepted at most once per session, including
multiple verb lines in their original order. Denied/non-command comments also
advance observation, and editing an observed comment is not a new command.
Pagination failure leaves the unobserved cursor unchanged and executes nothing
from an incomplete batch. A bounded page budget defers work; it does not skip
unseen commands. Retain command polling while paused, but suspend normal board/
agent monitoring work. Resume, stop and extend remain usable while paused.

Apply the same descriptor to:

- PM startup `/monitor` and reassessment choices: require the current trigger or
  post-start comments, exact grammar and current scope; preserve its existing
  permission check, replacing duplicate policy rather than removing it.
- PM health `/approve`, `/skip`, `/retry`, `/cleanup`, `/hold`, `/custom`: bind
  the reply to the particular posted health proposal/run. Cleanup and custom
  actions need authorization before reaching `executeAction`, not after.
- Legacy `AgentService` retry detection and retry guidance: authorize the **same**
  fresh `/retry` comment before both lock override and guidance selection. A
  substring anywhere in recent history must not force a lock takeover.
- MCP onboarding: remove the prompt's ad hoc “any approved text” poll; the host
  posts a request and invokes the shared bounded approval service before admitting
  the approved phase. Merely telling the model to call a helper is not a gate.
- Hosted AIDLC synthetic presence: only an authorized, exact stage/run-bound
  trigger may supply human-presence evidence. Do not mint `HUMAN_TURN` from the
  first non-bot comment after an arbitrary marker or a fallback history comment.
  Preserve native human-presence/gate semantics; no pending verified answer means
  no synthetic human event. This does not turn a repository writer into an
  authorized delivery-engine approver: engine policy checks still apply.

Existing authenticated hosted controls remain independent:
[gateway control authorization](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/gateway/src/activity/control_service.py#L436-L506)
requires tenant **and** run owner; its signed delivery, idempotency, pause/steering
queue and graceful abort are not replaced with GitHub comments. If comment API
access fails, an already authorized owner can use that existing control route
where supported. Do not impersonate that owner from a repository permission result.
Legacy workflows without hosted controls retain their execution timeout and
GitHub-authorized workflow cancellation as the operator recovery path.

### 3. Explicit approval bound to the exact design and run

Use `ApprovalService` for the bounded wait/outcome protocol and
`parsePlanApproval` for anchored grammar. Consolidate the service's more permissive
regex and separate permission cache into the shared policy. Keep outcomes
`allowed-once`, `rejected`, `unavailable`; only `allowed-once` admits implementation.
Do not import an auto-running agent executable just to reuse its polling function.

For Superpower and other proposal gates, the host owns a pending request record:
`{requestId, runId, repository, issueNumber, planCommentId, planDigest, postedAt,
expiresAt, state}`. Generate an unguessable request ID per plan revision, post the
exact plan plus instructions, and obtain the returned comment ID/server timestamp
before entering `pending`. Posting failure leaves the gate closed. If a plan is
revised, edited, rejected or replaced, invalidate the old request and publish a new
one. A process restart publishes a new request; this design does not add durable
cross-run approval replay or a new database table.

Recognized actions are exactly `/approve <requestId>` and
`/reject <requestId> [single-line feedback]`, matching the entire trimmed comment.
Bare `/approve`, `/go`, `/lgtm`, quoted/embedded commands, wrong IDs, extra approval
text, feedback and comments predating the posted request never open the gate.
Both initial scan and polling use the same verifier: server timestamps, unchanged
comment version, current request/run, human author, repository/effect-repository
write, fresh permission lookup and unexpired request. Scan after publishing to
catch a fast reply; do not scan the history for a prior run's approval. Use the
posted comment time, not a later `waitForApproval()` start time, to avoid losing
a response between posting and polling. Equal timestamp requires a distinct
reply ID after the request; malformed time denies. Re-fetch the request's bound
plan before consuming approval; digest mismatch invalidates it.

Consume approval once for that plan/run; feedback may drive a new design revision
but is never continuation authority. Rejection stops or revises as existing caller
policy permits. Preserve Superpower's 15-minute default wait and skill-agent's
30-minute wait; shared service bounds are configured by caller, never made
unlimited. Preserve `ApprovalService`'s error threshold and timeout denial.

### 4. Guidance is authorized task data, not privileged instructions

Authorize harvested `/context`, `/guidance`, `/resume`, `/use`, `/instruction` and
`/direction` comments using exact verbs, current trigger/session selection and
both repository scopes. Then wrap the text with the existing
[trust boundary helper](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/utils/trust-boundary.ts#L1-L38)
in **both** brainstorming and implementation prompts. Include actor/comment
provenance as host-generated metadata outside the body. The host states that
verified guidance may refine task intent within the authorized scope but cannot
approve a plan, bypass a gate, change tool policy or nominate credentials.

Remove “IMPORTANT: follow this guidance” and “skip brainstorming” privilege
promotion. Fence prior agent summaries and rejection/retry/custom text as data
as well. Reassessment summaries must say “comment claimed approval”, never
convert a `MEMBER`/`OWNER` word match into a trusted approval fact. Ordinary issue
history may remain available as fenced context; reading data is not authorization.

Use the current fence format for compatibility; it is defense in depth, not an
unbreakable delimiter or an execution authorization mechanism. A comment containing
fence-like text still cannot affect the host's gate or tool policy. Rendered-prompt
shape tests and host-effect tests are both required; a text snapshot alone does
not prove instruction isolation.

### 5. A single GitHub admission decision for every dispatch family

Keep webhook HMAC verification, identity/tenant resolution, deduplication, rate
limits and the signed human-authority envelope. Do not reuse an unsigned comment,
marker or lineage bit as identity proof. Refactor the current floor call into
one mandatory `authorizeGithubDispatch(candidate, payload, resolved, tenantItem)`
call after pure intent classification and before **any** persona publication or
engine-command persistence. Its structured allow/deny result is required by both
branches. Installation lifecycle events and non-dispatching events do not need
an eligibility decision, but remain in the event registry as explicit no-ops.

Maintain one event/action registry shared by classification and admission
coverage checks. The independently asserted supported dispatch matrix is:

| Event/action | Acting account | Association evidence |
|---|---|---|
| `issue_comment.created` on issue or PR | signed `sender.id`, required to equal `comment.user.id` | `comment.author_association` only for that same immutable actor |
| `issues.opened` with the inception label | signed `sender.id` | `issue.author_association` only if `issue.user.id == sender.id` |
| `issues.labeled` with a mapped persona | signed labeler `sender.id`, **not issue author** | actor-bound API evidence when the object author is different |
| `pull_request.opened` | signed `sender.id` | PR association only on matching `pull_request.user.id` |
| `pull_request.synchronize`, `pull_request.ready_for_review` | signed pusher/readiness actor, **not PR author** | actor-bound API evidence when different |

Draft/branch restrictions, bot synchronization suppression and
`GITHUB_AUTO_PR_REVIEW_ENABLED` stay in force. A passing floor cannot enable a
currently disabled dispatch family. A future family must supply an actor resolver,
association proof policy and lineage adapter; otherwise admission denies it.

**Association is not repository permission.** Preserve the existing floor order
`NONE < CONTRIBUTOR < COLLABORATOR < MEMBER < OWNER`. Missing setting in a
successfully read installation record means no optional floor; a failed/missing
installation lookup is not proof the setting is absent. Invalid configured values
deny as `eligibility_policy_invalid`. Unknown association values are insufficient
proof, not a new high tier. Human comment-driven commands/dispatch additionally
require repository write; the optional floor never replaces that requirement.
Engine comments retain their signed-envelope and platform `PLAN_APPROVE` gate
in addition. Machine chained dispatch has separate provenance rules below and
never becomes an interactive human approval.

**Decision for actor mismatch:** do not copy an issue author's association to a
labeler, and do not make every legitimate labeler fail solely because the webhook
omits its association. Add a small Lambda-side GitHub actor-evidence adapter
(proposed `lambda/common/github_actor_authority.py`), reusing the repository's
fixed-origin, no-redirect, bounded-request/token patterns. No new synchronous
gateway call or mirror of org membership in a new table is proposed. The existing
[App REST client pattern](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/gateway/src/admin/connections/github_client.py#L162-L245)
and [current App permission manifest](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/gateway/src/admin/connections/service.py#L90-L107)
are the two existing patterns to mirror; the gateway package is not imported into
the Lambda zip.

The adapter accepts only the verified installation, repository ID/owner/name and
numeric sender ID. It resolves current login from that identity and obtains a
repository-restricted installation token using the ingress's own configured App,
never a caller-supplied credential, URL, account or PAT fallback. Verify the
installation and repository relationship before using the token. Lambda already
has reads for that App's ID/key and the corresponding KMS key
([existing IAM](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/webhook-ingress/infra/iam.tf#L148-L196));
this is not authority to add wildcard or per-tenant secret reads. Existing
multi-App registrations whose association cannot be checked by this ingress's
App fail visibly; operator must correct the ingress/App binding, not supply
substitute credentials.

Evidence is a **proven lower bound**, sufficient to meet the configured floor,
not a guess at a GitHub association string:

1. A human matching a personal repository owner's immutable ID proves `OWNER`.
2. For an organization-owned repository, a current successful organization
   membership check for the actor proves `MEMBER`; repository permission alone
   does not. The existing App manifest requests `members:read`; older installations
   may need their owner to accept this already-declared permission. A 403 is
   unavailable, not nonmembership.
3. A successful collaborator-membership check proves `COLLABORATOR`; a permission
   response proving write-or-above also proves at least that lower bound. Public
   `read` permission by itself does not prove collaboration.
4. For the `CONTRIBUTOR` threshold only, an authenticated, bounded commit-author
   lookup may establish at least one repository commit tied to the numeric actor.
   Require the returned commit's GitHub author ID to match; login/email/text alone
   is not evidence. A missing result is not evidence that no contribution exists:
   absent further signed payload proof, deny as insufficient/unavailable.
5. Signed actor-bound payload association can supply its stated lower bound.
   `FIRST_TIMER`, `FIRST_TIME_CONTRIBUTOR`, `MANNEQUIN`, unknown or absent values
   supply no elevation. `NONE` imposes no association threshold but does not
   waive the identity, human-comment-write or bot provenance rules.

Avoid unnecessary lookups once a sufficient lower bound is proved. Bound the
whole lookup by the Lambda remaining-time budget (at most 10 seconds), cache
non-secret decisions for at most 60 seconds under installation/repository/actor,
and fail closed on incomplete evidence. Reuse token-expiry handling; never cache
an error as success. The Lambda currently depends only on boto3; any JWT/crypto
packaging needed for this adapter is a **required code-package deliverable**, with
pinned transitive dependencies and an installed-zip import/authentication fixture.
Do not assume gateway dependencies or a Lambda layer already exist. No new cloud
resource, schema or tenant setting is needed; API calls and package size do grow.
No sub-second latency or cost promise is made without measurement.

Denial returns a stable 403 reason for policy/permission failure; provider/store
unavailability returns 503 with a distinct `eligibility_unavailable` result.
Neither publishes SQS work, human authority nor a pending engine command. Record
bounded reason metrics and a delivery outcome; no raw comment in metrics. Keep
unavailability retryable: do not consume the delivery's terminal dedup marker
before a decision can be recomputed. Existing delivery IDs make authorized
redelivery idempotent. Policy denials are terminal; recovery requires a new event
or explicitly authorized redelivery after correcting policy. Do not add a
fail-open switch.

### 6. Correlation and authority cannot silently reset

Call existing `determine_correlation` through an exhaustive event adapter for
comments, issues and PRs. Use the server channel key for issue or PR; issue body
and PR/comment markers are untrusted hints. Retain marker signature validation,
server-row provenance resolution, tenant/install/repository binding and the
separate human-sender condition on `VerifiedHumanEvent`. The helper's current
human branch trusts `resolved.user_kind`; require a matching verified GitHub
human sender and proven identity before selecting that branch.

A verified, eligible human event starts a new chain with that person's resolved
ID, `is_human_rooted=true`, depth 0 and an actual correlation ID. A registered bot
with valid server-bound continuation inherits root, parent, generation depth and
credential depth; only `spawn_persona` increments on a real spawn. Preserve
`credential_chain_depth` so an apparently shallow continuation cannot restore
credential authority. No-op events never increment depth.

**Missing/failed lineage is not a new root.** Represent it explicitly as
`is_human_rooted=false`, empty root, `is_new_chain=false`, no trusted parent,
and a conservative depth at `MAX_CHAIN_DEPTH`, with reason `lineage_unavailable`.
Refuse GitHub publication for that decision; do not send a “machine” envelope
with depth zero and assume that fixes the spawn limit. Emit/audit the classification
even when no job is published. Preserve known server depth on partial failures
rather than replacing it with zero. Reject malformed/negative/bool depth at the
publisher instead of the current defensive reset. Remove both the handler's
trusted fallback and the blocked-record writer's default-true human field.

This intentionally ends the `issues.opened` bot-label exception as an independent
source of human authority. Existing bot work must carry verified server lineage
or use the separately authenticated service/agent-trigger path. Valid chained bot
PR review remains possible under its existing opt-in flag. EventBridge roots and
Task API service invocations keep their own verified service identity; they do
not inherit a GitHub floor or pretend to be human. This story does not authorize
new agent dispatch by a label/comment.

The existing downstream
[credential-owner calculation](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/webhook-ingress/lambda/common/spawn_persona.py#L738-L782)
must return an empty `authorized_user_id` for a non-human-rooted record even if a
human ID is present elsewhere. Test both legacy and protected-authority modes.
A trusted signed-human envelope is constructed only through the existing verified
human API, never from the new association adapter or correlation fallback.

### 7. Coverage guard, storage and compatibility

Implementation note: the maintained `comment-consumers-7845.json` registry and
the webhook CI source/AST check classify executable, context-only and reporting
comment readers, generated workflow entrypoints and supported GitHub dispatch
events. A newly discovered reader or event fails until the authority boundary
and regression fixture are reviewed. Lambda packaging separately extracts the
GitHub archive and imports its handler, JWT and cryptography outside the source
tree; this is an offline build check, not a deployment or live acceptance receipt.
Operators must use a worker image and source-executed workflow checkout built
from the fixed revision, and drain old affected runs under existing authority.

Add the mandatory-gate note next to the existing helper. Add a maintained
comment-consumer registry and AST/source-discovery test covering TypeScript,
Python and generated workflow/prompt templates. Each discovered comment reader
must be classified as executable influence, fenced context or reporting-only.
Unknown consumers and unknown dispatching event families fail the check until
reviewed; an unused import of the helper is not sufficient. Pair this with
unauthorized-input integration tests that observe zero callback/tool/queue effects
through each real entry point. Static discovery is a regression aid, not a proof
against arbitrary model-generated code or dynamically installed customer plugins.

Reuse existing installation settings, identity records, correlation/event stores,
run state and logs. No database migration, new table, Secrets Manager convention
or IAM broadening is proposed. The static inventory includes those definitions;
there is no claim that a new schema is required. No alteration to webhook HMAC,
credential broker access, tenant-owner hosted controls, engine permissions,
AIDLC human gates, GitHub protections or deployment holds is permitted.

Authorized writers retain steering, stop, extend, retry and explicit approval.
Intentional compatibility changes are exact command grammar, no historical replay,
request IDs on formerly bare approval commands, short permission freshness bounds,
and refusal of ambiguous actors/unproven bot roots. Announce those changes in
request/status text and operator release notes. Do not retain a legacy bypass to
avoid a permission-lookup outage.

## Validation and handoff

The [verification record](verification.md) distinguishes completed design checks
from the proposed implementation and live tests, with reproducible commands.

The one authoritative acceptance table is in the [issue body](issue-body.md).
[Backlog scenarios](backlog.md) map controlled fixtures to those IDs and distinguish
pre-merge checks from later live acceptance. The design audit is reproducible:

```bash
python3 docs/design/comment-authority-7845/audit.py
python3 scripts/check-public-docs.py
git diff --check
```

Run from the repository root with Python 3 and Git, with the reviewed source
commit locally available. The audit reads that commit, not the moving working
tree. It checks inventory equality and immutable-source/local link paths and line
bounds. It does not execute runtime code, contact AWS, test remote links for
availability, or prove the future implementation. Inventory updates are explicit
via `audit.py --write`; changing the reviewed revision requires renewed review.

Implementation review/CI can proceed independently of live access. The story
coordinator owns naming the authorized release operator and staging fixture;
that assignment is not an implied deployment grant. The release operator records
image digest, Lambda code hash, installed source revision and live AC evidence in
private approved storage. Use `Refs #7845`, not an automatic closing reference,
until both pre-merge and live acceptance pass. This design assignment ends at the
published ready design PR and verified issue-body update; it does not close the
security story.

# agent-codex-reviewer

`agent-codex-reviewer` is a Codex SDK execution adapter packaged inside the
standard `adp-agent-runtime` image. It has no image, queue, KEDA job, service
account, or IAM role of its own.

The shared worker consumes the normal agent envelope and selects this adapter
from the persona name. Existing personas continue through the Claude worker;
names under `agent-codex-*` select a packaged Codex adapter. The reviewer is the
first implementation of that convention.

```text
authenticated GitHub webhook
  -> agent-submit FIFO
  -> agent-scaledjob / adp-agent-runtime
  -> Python entrypoint persona router
  -> agent-codex-reviewer / Codex SDK
```

The shared entrypoint owns SQS acknowledgement, visibility heartbeats, tenant
GitHub authentication, checkout, status reporting, and the gateway proxy. The
adapter receives the prepared checkout and tenant default/developer GitHub
token. On an issue mention it publishes an issue-readiness review. On an
eligible pull request it publishes a code verdict, can push bounded mechanical
fixes, and can optionally merge. It does not attempt formal self-approval.

## Gateway-only model access

Engine review and repair continuations select `agent-codex-reviewer` and use the
protected `review_cycle_input`, without requiring a webhook payload or an
`agent/issue-N` branch. Shared-worker flows with repair permission let the reviewer
fix against the story and acceptance criteria, then review the final child commit
before an exact-lease push. Reviews with no repair permission remain read-only.
There is no mechanical file/line limit in this story repair path.

Assignments with `reviewer_owned_delivery` keep the same controller and review/
repair threads alive after each inspected push. The host polls the gateway's
canonical merge-check policy every minute. No configured or observed CI means
no CI wait. Required pending or missing checks wait; failing applicable checks
feed provider evidence to the retained repair thread. Optional checks remain
optional under the existing repository policy. The controller never turns an
unavailable observation into success.

CI polling does not consume `CODEX_REVIEWER_TURN_TIMEOUT_MS`: that allowance covers
cumulative model execution across retained review, repair and verification turns.
The gateway still enforces the flow's policy window, spend and claim on every
observation and model call. A repair that makes no progress returns its real
findings rather than repeating model calls on the same head.

The TypeScript reviewer publishes its exact-head verdict through the existing
Python evidence adapter, then checks current merge permission and repository
rules. It calls GitHub with the reviewed SHA and the allowed merge method (or
joins the required merge queue). It observes an actual merge before finishing;
a lost response is reconciled before retrying. New CI failures or base changes
return to the same retained repair thread.

The engine observes the merged PR, validates the accepted review evidence and
completes the story. It does not perform a competing merge for these assignments.
A missing worker terminal report after merge does not require another review.
Explicit blockers stop delivery; they do not dispatch another paid agent.

Completed review bytes are retained in the worker's existing S3 reporting spool
before upload and merge. Reporting retries reuse those bytes and never start a
model. Legacy assignments retain their original single-pass behavior. Review
reports stay outside implementation commits.

Codex uses the same loopback SigV4 proxy as every other hosted agent. Its SDK
base URL is `http://127.0.0.1:9090/openai/v1`; the proxy signs and forwards
`POST /openai/v1/responses` to the ADP gateway `/agent` route. The shared
entrypoint rejects any `ADP_BEDROCK_VIA` value other than `gateway`, so this
adapter has no direct-Bedrock fallback.

The Kubernetes worker pod is also the execution security boundary. The adapter
uses Codex `danger-full-access` mode because the pod intentionally does not
grant the user-namespace capability required by Codex's nested Linux sandbox.
This does not make the pod privileged: the filtered child environment,
gateway-only model path, pod filesystem/network controls, and controller-side
Git and bounded-fix checks remain in force.

## Feature controls

- `@agent-codex-reviewer` issue mentions and eligible pull-request events both
  select `agent-codex-reviewer` through the existing persona intent mapping;
  there is no separate reviewer routing flag or message shape.
- `CODEX_REVIEWER_APPLY_FIXES` enables bounded mechanical repairs.
- `CODEX_REVIEWER_MERGE_ENABLED` defaults to `true`; set it to `false` to stop
  after approval instead of squash-merging the current, successfully checked
  head.
- `CODEX_REVIEWER_MODEL` selects the model identifier. `ADP_MODEL_RESOLVED` takes
  precedence; the fallback is `openai.gpt-5.6-sol`, with high reasoning.

## Adding another Codex persona

Keep transport unchanged. Register the persona name through the existing
catalogue, package its adapter in `adp-agent-runtime`, and extend the shared
entrypoint's persona-to-command allow-list. For example,
`agent-codex-architect` should select its adapter from `persona`; it must not
add an engine field, queue, webhook dispatcher, or GitHub identity.

## Local verification

```bash
npm ci
npm test
```
# Reviewer reliability

Engine repair assignments act on their supplied findings before verification.
For a conflicting PR, the controller fetches and prepares the assigned base
merge; Codex resolves the files, and the controller verifies and publishes the
reviewed tree with the original head as its first parent. Review and repair use
separate retained conversations. Each pass publishes inspected progress before
waiting for CI; subsequent failures stay in those same conversations.

An interrupted SDK response stream may resume once on its existing thread and
working tree, within the original configured deadline. Missing terminal events
cannot count as success. Provider safety refusals, authorization failures,
invalid verdicts and expired deadlines do not trigger transport recovery.

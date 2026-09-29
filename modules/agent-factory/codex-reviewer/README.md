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

The reviewer owns **review → fix → test → merge**. Invoke it on a PR and it
repairs issues within the story, runs focused tests, pushes the fixes, waits for
CI when present, and merges the reviewed PR. CI failures return to the same
reviewer for repair. There is no developer handoff or extra approval stage.
It stops for a genuine blocker, a changed PR, or an existing repository merge
requirement. Source repairs are not restricted to “mechanical” findings.

Issue-only mentions remain read-only issue-readiness reviews. The shared worker
owns authentication, checkout and queue delivery. Both PR mentions and engine
assignments reuse one review/repair loop; their existing publication transports
remain internal implementation details.

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
- `CODEX_REVIEWER_APPLY_FIXES` defaults to `true`; an explicit `false` requests review without repairs.
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

## Codex developer

`agent-codex-developer` runs the official Codex SDK using the same checkout,
branch, shell tools, GitHub credential renewal and model proxy as the existing
worker. Mention `@agent-codex-developer` after deploying the updated worker and
persona registry and selecting a compatible model. It implements the issue,
runs tests locally, commits, pushes, and opens a ready PR. The adapter verifies
that the open PR contains the final local commit and substantive changes.
It does not depend on the Task API validation service. The embedded entrypoint
requires scoped GitHub token mode; mediated GitHub tool support is not yet wired.

For a standalone run using your existing `gh` authentication:

```bash
npm ci
npm run build
# Set OPENAI_BASE_URL and OPENAI_API_KEY for your authorized model endpoint.
# For ADP use https://YOUR_GATEWAY/api/openai/v1 and your access token.
CODEX_DEVELOPER_MODEL=openai.gpt-6-sol node dist/developer-entry.js \
  --repo owner/repository --issue 123 --workspace /tmp/new-developer-checkout
```

The workspace must be a new path; the runner clones the repository there.
Use `--base branch-name` to develop against a specific base branch. Run the
standalone command inside an environment where the agent is authorized to use
the available shell credentials. It has full shell and network access, matching
the worker's execution model. The default model turn deadline is 30 minutes.

### Hosted progress reporting

The embedded Codex developer consumes SDK events as they arrive and forwards
intentional explanations and command/file activity to the same reporting
components as the Claude developer. It posts and edits a live comment on the
tagged issue, streams the GitHub check run, saves the final transcript and SDK
session ID for the worker, and serves the authenticated Agent Activity explanation
stream. Completion includes the verified PR URL; failures publish a failure report.
Reasoning items and raw tool output are not published.

Agent Activity records come from the normal webhook invocation and worker
lifecycle. A standalone run does not create those records and is not a hosted
reporting qualification. The native adapter currently exposes the explanation
stream without advertising Claude-specific pause/resume/steer support.

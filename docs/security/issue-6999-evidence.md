# Browser fetch and callable timer scan review (#6999)

This is a **candidate source review, not a closure decision**. The original SARIF is
private; its per-result indices/native ratings have not been verified. A pinned,
scoped candidate scan found all eleven original-rule matches plus both unsafe
controls, with zero scan errors. All eleven records remain **unresolved** until
the original report is reconciled and their dispositions are reviewed. The source baseline is
`fa75f1c407ffeb075d43271eb8a18accaa6458b3`; the original report digest is
`4b77d2237eb6864e2228eb433a6ec314a94aad858417f7ee094248c9ad6227bd`.
The script `.github/scripts/reconcile_6999.py` verifies that digest and the
exact rule/path/line selector for **each** record, using the baseline severity
resolver (native before CVSS; `error` alone is unrated). It reports accepted
suppressions separately. Do not commit the SARIF or its raw output.

| Rule | Original location | Execution context, input and control at this revision | Candidate line / per-record status |
|---|---|---|---|
| SSRF | `demos/domain-mri/public/app.js:23` | Static browser demo: `api` calls use literal `/api/runs` paths plus a run ID received from its API; no server-side fetch or bearer header. Verify that the returned ID cannot alter the origin. | 23; unresolved |
| Eval | `modules/agent-factory/agent/src/run-heartbeat.ts:205` | Node interval takes an inline arrow-function callback; `intervalMs` controls only the delay, and `stop()` clears the handle. Open review PR #6378 has relevant callable-timer proof, but is not merged in this snapshot. | 205; unresolved |
| SSRF | `modules/gateway/frontend/src/pages/DomainMRI.tsx:24` | Browser page wraps the mounted demo's static task routes, prepends the configured API base, attaches a Cognito token and abort signal. Verify paths and redirect handling before non-applicability. | 24; unresolved |
| SSRF | `modules/gateway/frontend/src/services/activity.ts:207` | Browser admin transcript: configured API base plus encoded invocation ID and optional tenant query; bearer header. A second fetch at line 182 serves the member transcript and is outside this assigned selector. | 208; unresolved |
| SSRF | `modules/gateway/frontend/src/services/agentExplanations.ts:19` | Browser event stream: configured API base plus encoded invocation ID and bearer header. Merged PR #6376 already sets `redirect: 'error'`; verify it remains effective. | 19; unresolved |
| SSRF | `modules/gateway/frontend/src/services/api.ts:92` | Shared browser client: deployment API base and caller-supplied endpoint, with bearer header. Check whether callers can supply an absolute path or trigger a cross-origin redirect. | 93; unresolved |
| SSRF | `modules/gateway/frontend/src/services/auth.ts:285` | Browser code exchange: configured GitHub broker endpoint, application state and code in POST JSON; no bearer header. Examine redirect behavior for token/code disclosure. | 285; unresolved |
| SSRF | `modules/gateway/frontend/src/services/auth.ts:355` | Browser Cognito code exchange: configured hosted UI endpoint and form body; token response stays client-side. Check redirect behavior. The refresh call at line 383 is outside this selector. | 356; unresolved |
| SSRF | `modules/gateway/frontend/src/services/taskActivity.ts:25` | Browser task stream: strict task ID shape, encoded path, configured API base, bearer header. Check redirect behavior. | 25; unresolved |
| Eval | `modules/tools/task-sdk/codex-runner.mjs:95` | Node runner: the matched deadline timer is an inline callback at line 111 in this checkout; its deadline changes only the numeric delay. Cleanup also uses an inline callback for forced kill. Confirm scanned span before disposition. | 95; unresolved |
| SSRF | `modules/tools/task-sdk/test/codex.test.mjs:72` | Node test invokes an ephemeral loopback MCP server with its generated test token; no provider workload. Confirm the server is actually bound to loopback. | 72; unresolved |

The assignments contain nine fetch matches and two timer matches. The candidate
retains nine SSRF and two eval-rule observations even after the redirect change;
these are not eleven confirmed exploits. Source inspection alone cannot establish
the claimed nine critical/two high **original per-record** ratings,
nor does a browser context by itself prove redirect safety. Prior work #6119
merged a wider inventory but did not close all records; #6968 owns Axios, not
these native fetches. Existing inline Semgrep annotations in some browser files
are not evidence of accepted suppression or a safe disposition.

## Candidate checks

`python3 scripts/security/verify_issue_6999_semgrep.py --semgrep-command "uvx --with 'setuptools<81' --from semgrep==1.80.0 semgrep" --sarif-output /tmp/issue-6999-candidate.sarif`
uses a fresh private output path and requires `uvx` plus network access to the
pinned local Semgrep package. The rule fixture is a frozen copy of the two
registry rules used for this candidate check; its contents have not been
confirmed against the inaccessible original scan. The unsafe URL and string
timer source lives in a `.txt` fixture copied to a temporary `.js` file during
the scan, so routine source scans do not inherit the deliberately unsafe code.
The scoped run returned eleven assigned-rule matches, **two** unsafe-control
matches (one SSRF and one eval), zero accepted suppressions and no scan errors;
the retained raw SARIF from this local run has SHA-256
`dd563e32c07ba0a2c5aecb1562299416b61f9b3353808cc915f503b2a849ef31`.
The raw copy at the private run-local path is not a published or durable artifact.
A wider registry scan of these ten source files emitted 32 findings but reported
one partially analyzed file; it is **not** evidence of a clean full scan.

The full frontend suite passed 2,915 tests across 176 files (the focused
redirect subset passed 99); the frontend and agent builds passed, 36
heartbeat/control tests passed, and all 26 Task SDK tests passed. The
previously skipped Codex CLI integration case used the local investigator build
and the installed CLI against a fake loopback model, not a provider service.
Local Node tests and Semgrep do not establish a deployed artifact or production
availability. These local checks ran no provider workloads.

## Verification and handoff

With the authorized scan-reader connection, obtain the original SARIF privately,
then run `python3 .github/scripts/reconcile_6999.py <private-sarif-path>` and
record only the eleven selectors, native ratings, suppression statuses and
original indices in a restricted evidence store. Check the candidate scan for
those same selectors and unsafe positive controls before marking any record
fixed or evidence-reviewed not applicable. This local check used source revision
`cc69c9a1cf60ebcc060e8c55c2608130328cefcb`; the frontend build's
`dist/index.html` SHA-256 was
`aaafb747ad314660954d659e3fd5fec4b8555b0b7d5d08c7a85b7fbf5f60af9c`.
This is a local artifact digest, **not** the release's built bundle or deployed
checksum: the deployment workflow injects environment configuration at build
time. No immutable release image digest, rollout or rollback target has been
verified here.

The frontend release consumer is `.github/workflows/gateway-frontend-deploy.yml`:
when separately authorized it builds a main-branch commit with release-specific
settings, publishes `dist/` and verifies the served `index.html`. The agent
runtime and embedded Task SDK are copied into the image assembled by
`modules/agent-factory/agent-worker-image/Dockerfile`; the build/rollout consumer
is `.github/workflows/agent-worker-image.yml`, which requires an immutable image
digest before new worker jobs use it. The separate chat-agent consumer uses
`.github/workflows/chat-agent-deploy.yml` and its agent Dockerfile. The image
maintainer must supply the tested source revision, the actual image digest and
current pinned deployment input for **each** consumer before release handoff.
If a rollout is approved and must be undone, restore the maintainer-recorded
previous frontend bundle or previous pinned worker/chat image using the
existing release process. No production update is requested by this review.

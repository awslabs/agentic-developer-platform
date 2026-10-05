# Browser fetch and callable timer scan review (#6999)

This is a **source-level inventory, not a closure decision**. The original SARIF is
private; neither its per-result indices/native ratings nor candidate scan results
have been verified here. All eleven assigned records remain unresolved until the
original report and candidate scan are checked. The source baseline is
`fa75f1c407ffeb075d43271eb8a18accaa6458b3`; the original report digest is
`4b77d2237eb6864e2228eb433a6ec314a94aad858417f7ee094248c9ad6227bd`.
The script `.github/scripts/reconcile_6999.py` verifies that digest and the
exact rule/path/line selector for **each** record, using the baseline severity
resolver (native before CVSS; `error` alone is unrated). It reports accepted
suppressions separately. Do not commit the SARIF or its raw output.

| Rule | Original location | Execution context, input and control at this revision |
|---|---|---|
| SSRF | `demos/domain-mri/public/app.js:23` | Static browser demo: `api` calls use literal `/api/runs` paths plus a run ID received from its API; no server-side fetch or bearer header. Verify that the returned ID cannot alter the origin. |
| Eval | `modules/agent-factory/agent/src/run-heartbeat.ts:205` | Node interval takes an inline arrow-function callback; `intervalMs` controls only the delay, and `stop()` clears the handle. Open review PR #6378 has relevant callable-timer proof, but is not merged in this snapshot. |
| SSRF | `modules/gateway/frontend/src/pages/DomainMRI.tsx:24` | Browser page wraps the mounted demo's static task routes, prepends the configured API base, attaches a Cognito token and abort signal. Verify paths and redirect handling before non-applicability. |
| SSRF | `modules/gateway/frontend/src/services/activity.ts:207` | Browser admin transcript: configured API base plus encoded invocation ID and optional tenant query; bearer header. A second fetch at line 182 serves the member transcript and is outside this assigned selector. |
| SSRF | `modules/gateway/frontend/src/services/agentExplanations.ts:19` | Browser event stream: configured API base plus encoded invocation ID and bearer header. Merged PR #6376 already sets `redirect: 'error'`; verify it remains effective. |
| SSRF | `modules/gateway/frontend/src/services/api.ts:92` | Shared browser client: deployment API base and caller-supplied endpoint, with bearer header. Check whether callers can supply an absolute path or trigger a cross-origin redirect. |
| SSRF | `modules/gateway/frontend/src/services/auth.ts:285` | Browser code exchange: configured GitHub broker endpoint, application state and code in POST JSON; no bearer header. Examine redirect behavior for token/code disclosure. |
| SSRF | `modules/gateway/frontend/src/services/auth.ts:355` | Browser Cognito code exchange: configured hosted UI endpoint and form body; token response stays client-side. Check redirect behavior. The refresh call at line 383 is outside this selector. |
| SSRF | `modules/gateway/frontend/src/services/taskActivity.ts:25` | Browser task stream: strict task ID shape, encoded path, configured API base, bearer header. Check redirect behavior. |
| Eval | `modules/tools/task-sdk/codex-runner.mjs:95` | Node runner: the matched deadline timer is an inline callback at line 111 in this checkout; its deadline changes only the numeric delay. Cleanup also uses an inline callback for forced kill. Confirm scanned span before disposition. |
| SSRF | `modules/tools/task-sdk/test/codex.test.mjs:72` | Node test invokes an ephemeral loopback MCP server with its generated test token; no provider workload. Confirm the server is actually bound to loopback. |

The assignments contain nine fetch matches and two timer matches. Source inspection
alone cannot establish the claimed nine critical/two high **per-record** ratings,
nor does a browser context by itself prove redirect safety. Prior work #6119
merged a wider inventory but did not close all records; #6968 owns Axios, not
these native fetches. Existing inline Semgrep annotations in some browser files
are not evidence of accepted suppression or a safe disposition.

## Verification and handoff

With the authorized scan-reader connection, obtain the original SARIF privately,
then run `python3 .github/scripts/reconcile_6999.py <private-sarif-path>` and
record only the eleven selectors, native ratings, suppression statuses and
original indices in a restricted evidence store. Check the candidate scan for
those same selectors and unsafe positive controls before marking any record
fixed or evidence-reviewed not applicable. The frontend's release artifact is
its built browser bundle; heartbeat and Task SDK require their respective
package builds. No deployed digest, rollout or rollback target is established
by this source-level inventory. Rollback of a candidate change means restoring
its prior release artifact through the component maintainer's approved process.

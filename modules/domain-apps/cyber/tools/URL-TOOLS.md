# URL tools for Claude Agent SDK Tasks

Task submission remains `POST /v1/tasks`. The shared Claude Agent SDK lifecycle,
model proxy and Task IPC live in `modules/tools/task-sdk/`. Cyber owns its tool
schemas, investigation skill and native-browser adapter.

Common Crawl scan/result/read calls go through the IAM-authenticated
`/tools/cyber/common-crawl` Lambda route. Queries use the existing bounded Athena
workgroup; capture reads use stored WARC coordinates. Historical evidence is
separate from live observations, and no archive match establishes safety.

Browser start/step/inspect/close calls execute inside the trusted Task worker.
`cyber_tools.task_browser.TaskBrowser` is a thin wrapper around the existing
`local_browser.investigation_request` integration. AWS AgentCore hosts the browser;
the maintained native Playwright process preserves the session. There is no new
browser Lambda, browser service, load balancer, VPC Link, or browser job queue.

The host-owned `ADP_TASK_TOOL_ROUTES` registry chooses HTTPS endpoints or explicitly
configured local handlers. The model provides a tool name, never an import path,
URL endpoint or credentials. Each browser call checks current Task/client grants
through the existing generic `tool-authorize` endpoint. Sessions remain bound to
the verified Task attempt, and uncertain actions are never replayed. Host cleanup
closes local sessions before finalizing the Task; AgentCore expiry remains the
backstop if the entire worker disappears.

Browser start supports desktop/mobile profiles and host/observed_external scope.
Navigation, observed links/controls, back/root/scroll/wait and screenshots reuse
the existing implementation. A Task input `browser_scope=host` prevents widening
scope. Native page-generated requests and redirects are not restricted by that
chosen-action scope. Browser inspect reads paged evidence or a bounded JPEG image
preview for the SDK model. Original screenshots, DOM and WARC bytes are preserved
using shared Task artifact helpers, with hashes and source metadata.

Binary evidence uses the generic `tool-blob/1` JSON manifest: retrieve each part,
base64-decode its data, concatenate in offset order, and verify lengths/SHA256.
The model transport accepts bounded inline image previews, never remote image URLs.

## Deployment

Follow the canonical existing-stack update guide at
`docs/adp-platform-deployment/deploy-with-agent.md`.

- Publish the cyber-tools Lambda using `infra/build-image.sh`, configure the
  existing Common Crawl Athena/Glue/S3 policy and environment, review/apply its
  saved Terraform plan, then publish the shared API stage through its owner.
- Add the exact Common Crawl POST route to the protected worker's
  `task_tool_invoke_resources`. No browser API route is needed.
- Build/deploy the Task worker with the local adapter and shared SDK runtime, and
  refresh the gateway worker-digest allowlist. The existing domain staging includes
  native browser code and its Python dependencies.
- Build/deploy the gateway's generic bounded-image contract change and keep its
  scheduled Lambda consumer on the same image.
- Enable cyber's `task_url_tools_enabled` setting. It generates the HTTPS archive
  routes, local browser routes and stop-only browser cleanup registry.
- Grant the chosen client/persona `cyber.common_crawl_scan`,
  `cyber.common_crawl_result`, `cyber.common_crawl_read`, `cyber.browser_start`,
  `cyber.browser_step`, `cyber.browser_inspect`, and `cyber.browser_close`.
  Submit a fresh Task; accepted Tasks retain their original grants.
- Verify live archive queries, browser observations and visual preview, artifact
  retrieval, cancellation and cross-Task ownership refusals. Leave the AI-DLC
  engine paused during this maintenance.

The six-hour Task deadline is separate from the existing bounded browser session
lease. A deliberately new session can use a new session_key after a confirmed
close/expiry, but never to replay an uncertain start. Query time budgets are
checked during polling/cleanup; Athena independently enforces the scan-byte cap.

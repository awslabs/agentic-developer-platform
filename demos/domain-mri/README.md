# Domain MRI demo

A standalone, no-login demo of ADP's Cyber Task API: submit a domain, follow real SSE progress, then read or download the final offline HTML report. There are no npm dependencies or frontend build steps.

```bash
cd demos/domain-mri
npm start
# http://localhost:4318
```

Requires Node 22+ and, for live investigations, an AWS CLI session that can read the configured Cognito app client's secret. Alternatively set `MRI_CLIENT_SECRET` in the server environment. Credentials and OAuth tokens stay on the server. The existing Task API still enforces the service identity's hierarchy, model mapping, tool grants and budget policy. The demo does not change any of those settings.

The defaults target the existing dev deployment and `sophos-labs-hierarchy-opus5-check`, whose Cyber model is Opus 5. Live submissions incur normal Task/model costs. To use another deployment, configure `MRI_TASK_API_URL`, `MRI_TOKEN_URL`, `MRI_CLIENT_ID`, `MRI_USER_POOL_ID`, and `AWS_REGION`; update the UI's model label if its saved mapping differs. `PORT` defaults to 4318. `MRI_STATE_FILE` defaults to `/tmp/adp-domain-mri-demo-state.json` and stores submission IDs and Task snapshots, never credentials. Keep that file across restarts to retain recovery and duplicate-submission protection.

The server binds only to `127.0.0.1` for local development. Do not expose the EC2 host through public listeners, public EC2 URLs, or third-party tunnels. A hosted demo must use the existing ADP application domain and its deployment boundary. For private development access from another computer, forward the port:

```bash
ssh -L 4318:127.0.0.1:4318 ubuntu@YOUR_DEV_BOX
# Open http://localhost:4318 on your computer.
```

“Watch recorded demo” replays the real WICAR investigation completed on 27 September 2026, clearly labelled as recorded. It needs no AWS credentials, makes no paid API calls, and includes the original final report and screenshot. Replay timing is accelerated; event timestamps are original. This sample's DOM and network coverage limitations are retained in its report.

Live events come directly from the Task API through the same-origin server. EventSource reconnects with `Last-Event-ID`; status polling recovers completion if the stream disconnects. Reloading the page resumes its session. Submission retries reuse their original idempotency key, and the server admits only one active live investigation at a time. Stop requests use the Task cancellation API. Reports are checked against the upstream SHA-256 receipt and rendered in a sandboxed iframe.

The prototype deliberately has no user login or tenant switcher. Do not place this credential-backed demo on the public Internet without an access boundary and abuse controls. A deployable public version should use ADP's existing user/session authorization rather than exposing the service account.

```bash
npm test
```

Tests cover domain validation, uncertain submission recovery without duplicate Tasks, active-run exclusion, cross-origin submission refusal, and report integrity checks. The recorded sample is a prior investigation, not a current safety guarantee.

### Hosted client

The existing ADP S3/CloudFront frontend serves `/domain-mri` behind its normal
Cognito sign-in. It is a browser-only Task API client: no additional server,
proxy endpoint, service credential, or gateway backend deployment is required.

The client calls `/api/v1/tasks` with the signed-in user's Cognito access token,
then uses the existing status, SSE events, cancellation and artifact endpoints.
The Cyber persona's saved model selection and the user's Task policy determine
execution; the client does not override models or budgets. Authorized humans
need an active workspace membership and existing human Task enrollment.

The page preserves an idempotency key across ambiguous submission retries,
recovers the current Task and event cursor from session storage on refresh,
and verifies report SHA-256 before displaying it in a sandboxed iframe. Report
and Task data are fetched with authorization; no recorded reports or service
secrets are published as frontend assets. The standalone local demo above
retains its separate replay mode.

Publish with the existing frontend-only deployment workflow. Gateway backend,
worker images, API authorization and infrastructure remain unchanged.

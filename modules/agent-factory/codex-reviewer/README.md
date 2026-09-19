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
token. It publishes verdict comments, can push bounded mechanical fixes, and
can optionally merge. It does not attempt formal self-approval.

## Gateway-only model access

Codex uses the same loopback SigV4 proxy as every other hosted agent. Its SDK
base URL is `http://127.0.0.1:9090/openai/v1`; the proxy signs and forwards
`POST /openai/v1/responses` to the ADP gateway `/agent` route. The shared
entrypoint rejects any `ADP_BEDROCK_VIA` value other than `gateway`, so this
adapter has no direct-Bedrock fallback.

## Feature controls

- Pull-request events select `agent-codex-reviewer` through the existing persona
  intent mapping; there is no separate reviewer routing flag.
- `CODEX_REVIEWER_APPLY_FIXES` enables bounded mechanical repairs.
- `CODEX_REVIEWER_MERGE_ENABLED` enables squash merge after a current-head
  review and successful checks.
- `CODEX_REVIEWER_MODEL` selects the gateway model identifier.

## Local verification

```bash
npm ci
npm test
```

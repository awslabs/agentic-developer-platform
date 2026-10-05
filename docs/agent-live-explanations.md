# Live implementation explanations (#4989)

In Agent Activity, running entries offer **View live stream** in the desktop list, mobile cards and grouped chains when the read feature is enabled. This opens Invocation Detail and its authenticated explanation feed.

Agent Activity → Invocation Detail shows public messages and tool activity while a hosted run is working. Claude and Codex use the same progress envelope and UI. Claude emits public text at sentence/line boundaries when partial messages are available, and tool starts/completions. Codex forwards public message updates when the SDK emits them, command starts/completions, changed file paths, MCP calls, web searches, and plan updates. Raw tool results, terminal output, private reasoning, and unvalidated reviewer verdicts are excluded. Codex command previews are credential-redacted. Completed transcripts remain available separately.

The feature defaults off. Set `FEATURE_AGENT_EXPLANATIONS_ENABLED=true` separately in the gateway and worker. The gateway deployment reads `/adp/<environment>/gateway/feature-agent-explanations`; the worker Terraform setting is `agent_explanations_enabled`. Neither enables `FEATURE_AGENT_CONTROL_ENABLED`. The read-only worker starts the authenticated listener and registers its endpoint without installing pause/steer hooks. Existing protected identity, approved image and gateway-only ingress prerequisites still apply. Rollback disables the read flag on both sides; transcripts remain available.

The browser sends its access token in an Authorization header to `GET /activity/invocations/{id}/agent/events`. No query parameters, worker address or worker token are accepted. The gateway resolves the destination, validates its CIDR/port, requires current human membership and protected run ownership, and streams from the existing worker listener. It checks membership, expiry, registration and ownership throughout the connection. Authorization checks and downstream sends have five-second timeouts. A stalled subscriber cannot stall agent execution.

Each explanation event has version 1, invocation_id, generation, sequence, timestamp, kind and an allowlisted text payload. The optional `payload.progress` contains `id`, `category` (`message`, `tool`, `plan`), `state` (`running`, `completed`, `failed`), and `started_at`. Repeated IDs update the same visible entry; tool timers show wall-clock elapsed time since the observed start and disappear on tool/run completion. Codex identities are scoped per turn because SDK IDs can be reused. Running text/plan updates are deduplicated and limited to at most one per 250 ms per ID; completed updates bypass that time limit. Consumers that only read text remain compatible. The `Last-Event-ID` cursor is invocation:generation:sequence. History is limited to 128 events and 256 KiB in the worker, with a 16 KiB serialized event limit and four subscribers. The gateway allows four connections per run and 64 per process. Expired or foreign cursors receive an explicit reset. There is no cross-pod replay. The live feed sends events immediately; it does not wait for the activity list’s 30-second refresh or GitHub reporting intervals. Long tools do not stream raw output, and model reasoning may still produce a visible gap. Heartbeats arrive every two seconds, carry their own timestamp and never change the last-explanation timestamp. Browser memory is also bounded; omitted history is visible. Readers choose when to jump to the latest update.

The implementation is in the [worker history](../modules/agent-factory/agent/src/explanation-events.ts), [authenticated listener](../modules/agent-factory/agent/src/control-listener.ts), [gateway stream](../modules/gateway/src/activity/explanation_stream.py), and [Invocation Detail feed](../modules/gateway/frontend/src/components/LiveExplanations.tsx).

## Verification

- Worker: `npm test -- --runInBand src/claude-progress.test.ts src/explanation-events.test.ts src/control-listener.test.ts src/control-runtime-factory.test.ts` in `modules/agent-factory/agent`.
- Gateway: `pytest tests/activity/test_explanation_stream.py tests/activity/test_control_proxy.py` in `modules/gateway`.
- UI: `npx vitest run src/__tests__/components/LiveExplanations.test.tsx src/__tests__/services/agentExplanations.test.ts` in `modules/gateway/frontend`.
- Browser: install `@playwright/test@1.63.0` and Chromium, then `npx playwright test --config tests/e2e/explanations.config.ts`. This builds the real frontend and measures two markers arriving before a held-open HTTP stream completes, on desktop and mobile. It is a deterministic local fixture, not AWS acceptance. The dedicated PR workflow runs it.

For live acceptance, the existing authenticated registered-control fixture emits two real SDK-authored explanations when the read flag is enabled. It uses the same runtime factory and authored-text extraction, with foreground waits between explanations. Observe the rendered Invocation Detail through the deployed CloudFront → gateway → worker path and measure each event timestamp against browser receipt (at most five seconds on the controlled healthy path). Verify the served revision and both image digests, expired/revoked access, reconnect/reset, terminal transcript access and fixture cleanup. A reader must review the actual mechanism/tradeoff/evidence explanation for #5827. Local test success does not close live acceptance or authorize mutation rollout.

### Isolated live delivery, September 24, 2026

Source `671c5a4b0551b208fb89c927501d125c368b33fd` delivered two real SDK-authored messages through CloudFront, the fixture API edge, gateway and worker into the built browser UI in 357 ms and 360 ms. Both arrived before SDK completion. Gateway mutation controls were disabled. Anonymous, non-owner, cross-tenant and target-override probes returned 401, 404, 404 and 400. The single SDK query completed with worker exit 0 and one durable queue acknowledgement.

This used a locally served frontend build and a temporary CloudFront distribution; it does not claim ordinary-dashboard rollout. The demo explanation used “tokens” too broadly: the observed contract is authored messages. Human comprehension acceptance is pending in #5827. Ordinary mutation gates and the shared public frontend/login deployment were not changed for this test.

### Live verification, September 25, 2026

The source-grounded fixture `10db4297-0e56-4c6f-b0eb-6fe223d2fa0d`
produced two distinct authored explanations through CloudFront, received by the
browser 635 ms and 278 ms after their event timestamps. Keyboard access at
390×844 and disconnect/reconnect passed. The actual worker completed with exit 0
and one protected queue acknowledgement. Source revision:
`3ed76d7196098a24c0e3806f095e060dc0dfdce7`.

The read feature is configured for dev independently of mutation controls.
Human comprehension/reproduction acceptance remains separate and pending;
automated delivery evidence does not supply that review.

# Superplane onboarding UI

The browser half of workspace and provider onboarding — what an operator sees on a freshly
installed control plane with **zero workspaces**. Issue #5730, EPIC #4910.

This directory is the domain app's own code. The gateway holds only the mounting adapter
(`modules/gateway/frontend/src/pages/Superplane.tsx`, which renders `<OnboardingView />` behind
the default-off `superplane` feature gate in `App.tsx`) and no domain logic.

## Modules

| File | Responsibility |
|------|----------------|
| `contract.ts` | The API surface as data: every endpoint with its method, path and `served` flag, plus the parsers. The single place that decides what this feature may call. |
| `client.ts` | Issues the requests `contract.ts` declares, classifies outcomes, and parses responses fail-closed. |
| `operations.ts` | Operation identity and durable receipts — one identity across repeated submit, refresh and timeout. |
| `readiness.ts` | Three independent readiness readings. Deliberately exports no aggregate. |
| `OnboardingView.tsx` | The screen. Zero-workspace start, role gating, readiness, failure states. |
| `CreateWorkspaceFlow.tsx` | Review-then-submit workspace creation. |
| `ProviderConnectionPanel.tsx` | Provider connection bind / validate / revoke. |
| `ReadinessPanel.tsx` | Renders the three readings without collapsing them. |

## Four properties this code exists to hold

**An unserved endpoint is answered locally; no request is sent.** Superplane traffic passes
through a gateway proxy that forwards only the `[method, path]` pairs in
`modules/gateway/src/domain_proxy/superplane_routes.json`. A request to an unlisted path comes
back as a 404 that is indistinguishable from "that workspace does not exist" — so a client that
tries anyway sends the user hunting for a missing resource instead of a missing route.
`__tests__/contract.test.ts` reads that allowlist and unpacks it exactly as
`domain_proxy/superplane.py` does, in **both** directions: nothing may claim to serve what the
proxy drops, and nothing forwarded may be marked unavailable.

**There is no aggregate `ready` field.** Control-plane health, workspace state and provider
reachability are three separate tri-state readings, and AC-04's prohibition is enforced by the
*absence of the reducing function*. `readiness.test.ts` fails if one is added. A stale
observation degrades to `unknown`, not to `not ready` — stale is not evidence of current health,
and "not ready" invites a fix where "unknown" invites a check.

**`unknown` is never rewritten to `failed`.** A lost reply leaves an operation `unknown`.
Reporting it as failed invites a resubmission, and the operation it would duplicate may have
succeeded and may be billing. The receipt is persisted **before** the request goes out, so a tab
closed mid-flight leaves a recoverable identity rather than an orphan.

**Secrets are structurally excluded, not redacted.** This surface carries credential
*references* only. Response parsing names its fields explicitly rather than spreading, so a
value the server should never have sent has no path into state, storage or diagnostics. The
tripwire **raises** rather than redacting — a redacted leak is a leak that shipped, and the point
is that one cannot be introduced unnoticed. The field-name list is shared with the CLI helper
(`modules/gateway/cli/adp-superplane-onboarding.py`) and a test asserts the two agree.

## Diagnostics name the capability, not the story

User-facing text explains the service condition in user-actionable terms — "workspace plan
preview is not available on this deployment", not an internal identifier. The machine-readable
`code` field keeps its stable technical value so automation can branch on it.

## Toolchain

This directory has **no build of its own**. It is compiled, tested, typechecked and linted by the
gateway frontend, reached through the `superplane-ui` path alias in that package's
`vite.config.ts`, `tsconfig.json` and `vitest.config.ts`.

```bash
cd modules/gateway/frontend
npx vitest run                 # includes this directory's __tests__
npx tsc --noEmit               # vitest strips types without checking them
npm run lint                   # runs eslint here too, via lint:superplane-ui
```

Do not run `npx vitest`, `npx tsc` or `npx eslint` from *this* directory: there is no local
toolchain, so npx silently installs an unrelated package version (and vitest reports "No test
files found" with a non-zero exit that is easy to misread as a real failure).

`gateway-ci.yml` is the only lane that runs frontend checks for this path.
`superplane-domain-ci.yml` matches the same path but runs Python and Go only — so a UI-only
change without the `gateway-ci.yml` path entry would get a green report from a lane that executed
no frontend check at all, which is worse than no coverage because the check name reads as passing.

## AC-to-test matrix

| AC | Required result | Covering tests | Status |
|----|-----------------|----------------|--------|
| AC-01 | Feature off leaves navigation unchanged; on with zero workspaces an authorized admin can begin onboarding; other roles see only permitted actions | `OnboardingView.test.tsx`: `AC-01: beginning onboarding with zero workspaces`, `the create flow is reachable and scoped (AC-01/AC-03)`; `ProviderConnectionPanel.test.tsx`: `AC-01: what a read-only user sees` | Source complete |
| AC-02 | Connection/binding and create/adopt journeys reach durable progress and verified readiness; refresh, timeout and repeated submit preserve one operation identity | `operations.test.ts`: `repeated submit`, `refresh`, `timeout and lost replies`, `changed payload`, `payload fingerprinting`; `CreateWorkspaceFlow.test.tsx`: `AC-02: the reviewed plan revision binds the submission`, `AC-02: one operation identity across repeated submit, refresh and timeout` | Source complete; **live acceptance blocked** (see below) |
| AC-03 | Cross-org/workspace access, credential substitution, revoked credentials and late responses after org switching denied or discarded; secrets absent from persistence and diagnostics | `client.test.ts`: `late responses after an organization switch`, `provider connections`; `operations.test.ts`: `organization and deployment scoping`, `secret absence in persistence`; `ProviderConnectionPanel.test.tsx`: `AC-03: binding a vault credential`, `AC-03: validation is the service's answer, never the client's`, `AC-03: revoking a connection`, `the secret-material tripwire itself` | Source complete |
| AC-04 | Partial bootstrap failure, missing capabilities and provider unavailability produce actionable states; control-plane health alone never marks a workspace execution-ready | `readiness.test.ts`: `the AC-04 prohibition`, `freshness`, `workspace readiness`, `provider readiness`, `partial failure produces actionable states`; `OnboardingView.test.tsx`: `AC-04: create is disabled, with the gap named…`, `AC-04: control-plane health never marks a workspace execution-ready`, `AC-04: actionable failure states`, `AC-04: a served capability route is not by itself permission to create`; `CreateWorkspaceFlow.test.tsx`: `AC-04: refusing to submit what cannot be submitted safely` | Source complete |
| AC-05 | Keyboard navigation, accessible labels/focus, narrow-screen layout, session expiry across successful and failed onboarding | `OnboardingView.test.tsx`: `AC-05: keyboard and accessible structure`; `CreateWorkspaceFlow.test.tsx`: `AC-05: the plan is reviewable and accessible` | **Partial** — narrow-screen layout is CSS-only and unverified; jsdom has no layout engine |
| AC-06 | Affected CI and browser integration tests pass at the final revision; record a real demonstration with exact release, workspace and operation IDs | `gateway-ci.yml` (vitest + typecheck + lint), `superplane-domain-ci.yml` | **CI complete; live demonstration not done** |

The equivalent CLI surface is `adp superplane onboarding`, covered by
`modules/gateway/tests/cli/test_superplane_onboarding.py`.

## What is honestly not available yet

Five endpoints this feature needs are absent from the proxy allowlist at this revision, and
`agent/issue-5535` (which owns them) does not yet add them:

| Endpoint | Consequence |
|----------|-------------|
| `capabilities` | Capability reporting is unavailable, so create is refused rather than risking an unidempotent submit |
| `previewWorkspace` | No reviewable plan, so creation cannot proceed past review |
| `adoptWorkspace` | BYOC adoption is reported unavailable and sends no request |
| `getOperation`, `recoverOperation` | Operation lookup and recovery are reported unavailable |

These are surfaced as an honest `unavailable` state. **There are no fixture or demo fallbacks in
production paths** — a synthesised success here would be a claim that infrastructure exists when
it does not. When the routes land, the `served` flags flip; the create path is already
implemented and tested against that state by
`TestTheCreatePathOnceTheEndpointsAreServed` in the CLI suite and by the flow tests here.

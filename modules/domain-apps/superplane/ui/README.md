# Superplane onboarding UI

The [authoritative Superplane design](../DESIGN.md) governs architecture and ownership.
This document provides supporting implementation detail or historical evidence;
its availability statements do not imply that pending design requirements are implemented.

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

This matrix locates checks; it is not final-revision execution evidence. The
bounded Demo 1 submission adds registered-workspace receipt re-entry, separate
readiness/access observations and a guarded retirement inventory review. It does
not complete the wider onboarding story. Component fixtures cannot prove real
provider binding, create/adopt readiness, or deployed authorization denials.

| AC | Required result | Covering tests | Status |
|----|-----------------|----------------|--------|
| AC-01 | Feature off leaves navigation unchanged; on with zero workspaces an authorized admin can begin onboarding; other roles see only permitted actions | `OnboardingView.test.tsx`: `AC-01: beginning onboarding with zero workspaces`, `the create flow is reachable and scoped (AC-01/AC-03)`; `ProviderConnectionPanel.test.tsx`: `AC-01: what a read-only user sees` | Source complete |
| AC-02 | Connection/binding and create/adopt journeys reach durable progress and verified readiness; refresh, timeout and repeated submit preserve one operation identity | `operations.test.ts`: `repeated submit`, `refresh`, `timeout and lost replies`, `changed payload`, `payload fingerprinting`; `CreateWorkspaceFlow.test.tsx`: `AC-02: the reviewed plan revision binds the submission`, `AC-02: one operation identity across repeated submit, refresh and timeout` | Source complete; **live acceptance blocked** (see below) |
| AC-03 | Cross-org/workspace access, credential substitution, revoked credentials and late responses after org switching denied or discarded; secrets absent from persistence and diagnostics | `client.test.ts`: `late responses after an organization switch`, `provider connections`; `operations.test.ts`: `organization and deployment scoping`, `secret absence in persistence`; `ProviderConnectionPanel.test.tsx`: `AC-03: binding a vault credential`, `AC-03: validation is the service's answer, never the client's`, `AC-03: revoking a connection`, `the secret-material tripwire itself` | Source complete |
| AC-04 | Partial bootstrap failure, missing capabilities and provider unavailability produce actionable states; control-plane health alone never marks a workspace execution-ready | `readiness.test.ts`: `the AC-04 prohibition`, `freshness`, `workspace readiness`, `provider readiness`, `partial failure produces actionable states`; `OnboardingView.test.tsx`: `AC-04: create is disabled, with the gap named…`, `AC-04: control-plane health never marks a workspace execution-ready`, `AC-04: actionable failure states`, `AC-04: a served capability route is not by itself permission to create`; `CreateWorkspaceFlow.test.tsx`: `AC-04: refusing to submit what cannot be submitted safely` | Source complete |
| AC-05 | Keyboard navigation, accessible labels/focus, narrow-screen layout, session expiry across successful and failed onboarding | `OnboardingView.test.tsx`, `CreateWorkspaceFlow.test.tsx`, `ApprovalPanel.test.tsx`, `RetirementPanel.test.tsx`; `browser-tests/verify_onboarding_browser.py` | Component and browser scenarios are separate; only an executed browser check establishes layout evidence, and wider supported modes still require coverage |
| AC-06 | Affected CI and browser integration tests pass at the final revision; record a real demonstration with exact release, workspace and operation IDs | `gateway-ci.yml` (vitest + typecheck + lint), `superplane-domain-ci.yml`, `superplane-ui-browser-ci.yml` | Requires final-head check results plus separately authorized live evidence; CI wiring alone is not a pass |

The equivalent CLI surface is `adp superplane onboarding`, covered by
`modules/gateway/tests/cli/test_superplane_onboarding.py`.

### Isolated onboarding browser checks

The maintained browser lane runs `browser-tests/test_onboarding_fixture.py` before
`browser-tests/verify_browser.py`, which also runs the onboarding scenarios. The
fixture serves the lifecycle-plan list requested after creation, even when no
next plan exists. Unexpected API requests, retirement admission, and non-fixture
origins fail the check rather than reaching a real service. The CLI endpoint table
mirrors the same source-owned contract; declaring a retirement route does not add
a CLI removal command or make backend admission available.

Install the locked Gateway frontend dependencies first (`npm ci` from
`modules/gateway/frontend`). With pytest and Playwright 1.55.0 installed in the
active Python environment, run the fixture unit tests from the repository root:

```bash
python -m pytest -q -o addopts= modules/domain-apps/superplane/ui/browser-tests/test_onboarding_fixture.py
```

Actual keyboard and layout validation additionally requires Chromium and its
native libraries (`python -m playwright install chromium --with-deps` on a
supported test host). From the repository root, run the maintained browser lane:

```bash
python modules/domain-apps/superplane/ui/browser-tests/verify_browser.py
```

The fixture tests should pass without a browser; they check transport behavior,
not rendering. The browser runner records fixture-only screenshots and results
under `test-results/superplane-browser/`. Neither result proves live onboarding.

### Bounded delivery and remaining acceptance

Retirement review displays owned deletions, preserved resources and unknown cost
exposure. The current backend contract refuses admission until staged cleanup
access exists. No approval or deletion is synthesized by the UI. Registered
workspaces expose the original request and phase history; recovery of a lost
request ID before registration still needs backend discovery support.

AC-02 retains real create/adopt and provider create/bind/rotate/revoke journeys;
AC-03 retains real API denials across all supported provider variants; AC-05
retains browser checks across successful and failed supported modes. AC-06 needs
results bound to the final code revision, not historical CI claims. Live AC-02
and AC-06 evidence belongs to the separately authorized #5540 evaluation owner,
after compatible backend/release integration, approval and identity handoffs,
staged cleanup access, and a bounded live target/browser driver are available.
That private evaluation must record exact release, workspace and operation IDs.
Merging this source submission does not close those criteria or the wider story.
Rolling back the UI does not delete workspaces or provider connections.

## Route availability and live acceptance

All mapped onboarding endpoints are mounted by the composed API, included in the gateway
allowlist and recorded in the permission inventory. Both client contracts mark these routes
served. Deployment feature gates, advertised capabilities, authorization and approval checks
remain independent requirements; a served route does not establish workspace readiness.

Unavailable-deployment tests explicitly disable the relevant routes in test fixtures and
continue asserting that no request is sent. Separate contract tests compare the shipped flags
with the real proxy allowlist in both directions. Production paths use no fixture or demo
fallbacks.

Live acceptance still requires the deployed release to complete an authorized onboarding
journey, preserve its operation identity and report verified workspace readiness. Remote CI
does not substitute for that demonstration or for browser layout verification.

## Serving workload operations

`ServingPanel.tsx` uses the maintained deployment preview, create, list and
UUID-scoped teardown routes. Profile discovery checks current workspace grants,
canonical target/credential bindings, the maintained plan producer and dispatcher
readiness before enabling submission. The form selects validated model options
from this catalog. Teardown review remains available to an authorized caller even
when the original serving profile is no longer installed. The review renders the plan carried by the exact approval request,
including its immutable workload image, target, resource ceiling, runtime and
maximum additional cost. Approval is checked again immediately before submission.

Create and stop have separate durable request receipts, scoped to deployment,
organization and workspace. Receipts contain identifiers and a payload fingerprint,
not model inputs, images or credential values. A lost reply retains its identity;
saved requests recover their operation status by idempotency key without re-entering
model inputs or replaying a mutation. Reviewing the same inputs can also recover
the approval and retry that same request.
Lists refresh every ten seconds; inaccessible results are removed and late results
from a previous workspace are discarded. Operation success, missing list entries
and accepted stop requests never establish provider absence or cost settlement.

`ServingPanel.test.tsx` covers lost replies/reload, original-resource stop requests,
changed plans, revoked and mismatched approvals, cross-workspace responses, and
read-only views. These are remote CI transport-fixture tests. The isolated
Chromium checks below cover browser layout. Live serving acceptance, bounded logs,
result links, endpoint access and reconciled workload costs remain required for complete #5731 delivery.

`Superplane UI Browser CI`, called by Gateway CI, starts the maintained Vite
frontend on loopback and renders this component in Chromium with fixture HTTP
responses. It checks keyboard submission, review/approval/stop interaction and
horizontal overflow at 360px and 1280px, and uploads screenshots plus a fixture
receipt. Browser requests outside the loopback origin are refused. This is
isolated browser evidence, not a deployed onboarding or serving demonstration.
Tailwind explicitly scans the domain UI so its classes are included in the
shipped frontend bundle.

## Batch operations

`BatchPanel` consumes the governed batch profile and Job routes. Users select a
fixed immutable image/invocation, review the exact resource/runtime/cost envelope,
obtain approval and submit. The shared workload action rechecks the current plan
and approval before admission and persists a `batch:<workspace>:...` receipt
separate from serving receipts. Reload can recover accepted operations without
re-entering the invocation. A stop uses the original Job UUID and its separately
approved teardown plan.

The list is bounded to 100 with an explicit truncation message. Wrong-workspace
and late responses are refused; revoked access clears displayed rows. Job outcome,
logs, results and observed cost remain unavailable until those backend contracts
are composed. A terminal operation is not reported as verified cleanup. In-flight
cancellation has a separate action from the stop button.

The isolated Chromium CI entry exercises serving and batch separately with
fixture HTTP transports, keyboard operation and 360/1280px screenshots. This is
browser evidence, not live workload acceptance.


## Cancellation

Both workload lists expose cancellation only when the server advertises current
cancellation authority. The action addresses the displayed original operation;
a retry after a lost reply uses those same IDs and creates no new request identity.
Revoked access disables retries and workspace changes discard late replies.
Cancellation acknowledgement never marks resources absent. Only the backend's
`CancelledBeforeDispatch` outcome is displayed as not needing workload cleanup;
other cancellations remain pending reconciliation. Keyboard cancellation is
included in the isolated Chromium scenarios for both workload kinds.

Workload rows offer current status and bounded Pod log windows when the API advertises observation support. Current READ authorization, original resource identity and a current manager lease are required. These observations do not establish endpoint acceptance, cleanup or reconciled cost. See `executor/WORKLOAD-OBSERVATIONS.md` for limits and read credential requirements.

The workload budget view shows original/stop reservation ceilings, held amounts, ledger freshness and workspace reservation capacity. Values use exact USD millionths; estimated and provider-reconciled cost remain unavailable until supported evidence exists. Historical allocation counts are not live presence or cleanup proof. See `executor/WORKLOAD-ACCOUNTING.md`.

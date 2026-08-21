# Edge & Internal-Plane Header-Trust Boundary — Design Review

**Status:** Review complete — 2 design decisions need operator sign-off before implementation
**Issue:** #3985 (Group A of sub-EPIC #3984, parent EPIC #615)
**Author:** @agent-architect
**Date:** 2026-08-21
**Findings covered:** f-42dca300 (CRITICAL), f-bab519a0, f-9bbb81ba, f-72ed7277 (HIGH)

---

## Executive summary

The issue's threat model is **correct and the CRITICAL is real**, but one link in the
stated root-cause chain is wrong in a way that changes which fix is load-bearing, and
two of the three proposed app-layer fixes are **not implementable as written** against
the current schema.

| Issue's claim | Reality | Consequence |
|---|---|---|
| CloudFront `/api/*` reaches the pod and "API Gateway's `AWS_IAM` route is bypassed" | `/api/*` targets the **VPC origin = internal ALB directly** (`cloudfront/main.tf:253`). API Gateway is not bypassed — it is **not in the path at all** | Layer 2 (routing) is the load-bearing fix, not layer 1 |
| "replace `Managed-AllViewer` with a policy that **strips** the identity headers" | CloudFront origin-request policies are **allowlist-only** (`none`/`whitelist`/`allViewer`/`allViewerAndWhitelistCloudFront`). There is no strip/deny semantic | Chosen mechanism must change — see D1 |
| enforce `caller.scope in {internal, platform}` | `TokenContext` has **no `scope` field** (`shared/schemas/auth.py:22-32`); `agent_entry_to_token_context` drops it (`agent_registry.py:238-257`). No `platform` scope exists — the only seeded value is `internal` (`agent-registry-seed.tf:42`) | Needs a schema change; allowlist set is `{internal}` |
| enforce `caller.org_id == target_org_id` on **every** internal read/write | **5 of 12 internal routes have no target org in the request** to compare against | Not implementable as a uniform rule — see D2 |

**Verdict: 🔴 Not ready as a single unit.** Split recommended (see §7).

---

## 1. Confirmed exposure chain (verified at HEAD)

The CRITICAL reproduces. The chain is:

1. `cloudfront/main.tf:247-269` — behavior `/api/*`, `target_origin_id = local.use_vpc_origin ? local.vpc_origin_id : local.alb_origin_id` (`:253`) → **the internal ALB**, via `aws_cloudfront_vpc_origin.api` (`:119-140`).
2. `origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id` (`:259`) — `Managed-AllViewer` (`:391-393`) forwards **all** viewer headers verbatim.
3. `aws_cloudfront_function.strip_api_prefix` (`:91-105`) rewrites `/api/internal/v1/...` → `/internal/v1/...`. The `/api/*` pattern **does** match `/api/internal/...`.
4. `k8s/ingress.yaml:64-73` — one catch-all rule, `path: /`, `pathType: Prefix` → the pod. No path discrimination, no header conditions. (Repo-wide there are **zero** `aws_lb_listener_rule` resources.)
5. `gateway-deploy.yml:226` substitutes `BG_TRUST_APIGW_HEADERS=true` **unconditionally**, though `config.py:45` defaults it `False`.
6. `internal/auth_deps.py:58-70` — `X-Caller-Identity` present ⇒ IRSA path, no transit proof.

So `https://<cf>/api/internal/v1/...` + a forged `X-Caller-Identity` naming a registered role ARN reaches the credential plane. **The API Gateway `AWS_IAM` `/internal/{proxy+}` route (`api-gateway/main.tf:243-276`) is a parallel path, not a chokepoint.**

### Second exposure path the issue only parenthesises

`/.well-known/*` (`cloudfront/main.tf:294-310`) also uses `Managed-AllViewer` (`:306`) and also targets **the same api origin** (`:300`) — but has **no** `function_association`, so no prefix rewrite. Lower severity (paths must start `/.well-known/`), but it must be covered by the same fix. `/gitlab/*` (`:272-288`) is also AllViewer but targets a different origin.

### Stale doc that would mislead a future reviewer

`modules/gateway/docs/endpoint-audit.md:59` asserts `/api/internal/*` falls through to S3 (`server: AmazonS3`) and `:64-67` concludes internal endpoints are "NOT reachable via CloudFront". That is **contradicted by the current terraform**. Almost certainly observed before `enable_vpc_origin` was flipped on. This doc should be corrected in the same PR — it is the artifact most likely to cause someone to reclose this finding as a false positive.

---

## 2. Decision D1 — the edge mechanism (needs sign-off)

**The issue's proposed mechanism does not exist.** Origin-request policies cannot strip
headers; `headers.header_behavior` accepts only `none | whitelist | allViewer |
allViewerAndWhitelistCloudFront`. "Strip X" must be expressed either as an *allowlist of
everything except X*, or as a CloudFront Function that deletes X.

### Option A (recommended) — extend the existing CloudFront Function with a denylist

A function is already associated with `/api/*` on `viewer-request`
(`cloudfront/main.tf:262-265`). Extend it to `delete` the dangerous headers, and attach
the same function to `/.well-known/*` and `/gitlab/*`.

```js
function handler(event) {
  var request = event.request;
  // Trust-boundary strip: these headers are only meaningful when injected by
  // API Gateway's AWS_IAM integration. From the public edge they are
  // attacker-controlled. Deleting on viewer-request is unbypassable.
  var BLOCKED = [
    'x-caller-identity', 'x-amzn-iam-user-arn', 'x-amzn-requestcontext',
    'x-auth-source', 'x-internal-api-key', 'x-agent-scopes'
  ];
  for (var i = 0; i < BLOCKED.length; i++) delete request.headers[BLOCKED[i]];
  // X-Agent-* is a family; delete by prefix.
  for (var h in request.headers) {
    if (h.indexOf('x-agent-') === 0) delete request.headers[h];
  }
  request.uri = request.uri.replace(/^\/api(?=\/|$)/, '');
  if (request.uri === '') request.uri = '/';
  return request;
}
```

Why this over the allowlist:
- **No enumeration risk.** The gateway serves an Anthropic-compatible surface — it reads `authorization`, `x-api-key`, `anthropic-version`, `anthropic-beta`, `content-type`, `x-forwarded-for`, `x-real-ip`, `user-agent`, `x-request-id` (grep over `src/`), and CORS is `allow_headers=["*"]` (`app.py:164`). Any allowlist that misses one silently breaks a client, and the failure mode is a confusing 4xx from application code, not a policy error.
- **Fail-safe direction.** A denylist that misses a header leaves a known-enumerated risk; an allowlist that misses one causes an outage. Given `BLOCKED` is exactly the set the app trusts, the denylist is complete by construction — and it is enforced *in the same file that defines the app's trust list*, so the two can be kept in sync by review.
- Cheapest diff: one resource body + two `function_association` blocks.
- Cost: CloudFront Functions ≈ $0.10/1M invocations. Negligible.
- Note `delete` on `request.headers` is normal CloudFront-JS 2.0; header keys are lowercased by the runtime, so match lowercase.

Also fixes in passing: the current regex `/^\/api/` is **not segment-anchored**, so `/apifoo` → `/foo`. Use `/^\/api(?=\/|$)/`.

### Option B — custom origin-request policy (allowlist)

Acceptable but strictly riskier. If the operator prefers it, two non-obvious requirements:
- It must set `cookies_config = all` **and** `query_strings_config = all`. `Managed-AllViewer` forwards both today; a custom policy defaults them off, which would **break the OAuth callback** (`?code=`/`state=`) and any cookie-borne session. This is the single most likely way to turn this security fix into a login outage.
- The allowlist must be enumerated from the grep above, and re-verified whenever a new upstream header is honoured.

**Recommendation: Option A. Consider doing both** (function strips; policy narrows) only if the operator wants belt-and-braces — but ship A first, alone, so a regression has one obvious cause.

### Fourth layer available cheaply

`waf_web_acl_arn = ""` at the root (`infra/main.tf:716`), so **no WAF is attached** despite the module supporting it. A WAF rule blocking these header names at the edge is a cheap independent control. Out of scope here; worth a follow-up.

---

## 3. Decision D2 — the internal-plane tenant invariant (needs sign-off)

"Enforce `caller.org_id == target_org_id` on every internal read/write" **cannot be applied
uniformly**: no internal handler reads `request.state.token_context` today, and 5 of 12
routes carry no target org at all. A per-route decision is required *before* coding, or the
implementing agent will invent one.

| # | Route | Target org in request? | Required decision |
|---|---|---|---|
| 1 | `POST /issue-magic-link` | No — writes `org_id="__internal__"` (`routes.py:198`) | Platform-only. Restrict to `scope=internal` callers |
| 2 | `POST /resolve-user` | Indirect — `channel_context` selects the org for shadow-user creation (`routes.py:271-289`) | Platform-only; caller must not choose the org |
| 3 | `POST /resolve-installation` | No — scans all `Organization` rows (`routes.py:432-437`) | Platform-only |
| 4 | `GET /admin/tenant-config/{tenant}` | Param echoed, **not** used for scoping (`admin_routes.py:38-47`) | Compare `caller.org_id == tenant`, or make platform-only |
| 5 | `GET /admin/audit-entries` | `org_id` query is **optional** (`admin_routes.py:53,73-74`) — omitting it returns rows across all tenants | **Make `org_id` required** and compare. ⚠️ `tests/internal/test_admin_routes.py:241-276` currently *asserts the vulnerable behavior* (omitted ⇒ no `org_id` in WHERE). That test must be inverted |
| 6 | `POST /provenance` | Body `org_id` written verbatim (`provenance_routes.py:122`); actor FKs validated for existence but **not** for membership of `body.org_id` (`:84-106`) | Compare, **and** validate actor ∈ org |
| 7-11 | `user-credentials`, `proxy-request`, `credential-materialize`, `credential-raw-read`, `credential-assume-role` | Org derived from body `user_id` → `resolve_credential_binding` → `users.org_id` | Compare derived org against caller. **See interaction below** |
| 12 | `POST /knowledge-assets/status-callback` | No org column in the WHERE clause (`status_callback_routes.py:116,138`) | Platform-only; any shared-secret holder can flip any asset by UUID |

**Interaction with #3142 credential binding (`docs/design/credential-authorization-binding.md`):**
routes 7-11 already have a binding mechanism — but it runs in **shadow mode by default**
(`credential_binding.py:81,97-107`), so a missing `invocation_id` silently falls back to the
caller-supplied body `user_id`, and DDB errors fail **soft** to `""` (`:194-204`). The
caller-vs-target compare added here is the control that makes shadow mode safe. Sequence
matters: do **not** treat these as independent fixes.

**Prerequisite schema change:** add `scope` to `TokenContext` and copy it in
`agent_entry_to_token_context` (`agent_registry.py:248-257`), otherwise the "platform-only"
column above has nothing to test. Note `is_admin` and `account_type` are hardcoded there,
so `scope` is the only viable discriminator. The only seeded internal principal is
`scaledjob-worker` with `scope=internal`, `org_id=__platform__` (`agent-registry-seed.tf:37-42`).

---

## 4. Layer 2 must be enforced at the ALB, not in app middleware

The issue offers "an ALB listener rule / gateway middleware" as alternatives. They are **not**
equivalent — one of them breaks a live caller.

`modules/agent-context/images/ingestion/status_callback.py:65,79-80` posts to
`http://bedrockgateway.adp-gateway.svc.cluster.local/internal/v1/knowledge-assets/status-callback`
with `X-Internal-Api-Key` (URL from `agent-context-deploy.yml:165`). This is **pod → ClusterIP
Service → pod**: it never traverses the ALB or API Gateway.

- **App middleware rejecting `/internal/*` without an API-GW transit signal ⇒ breaks agent-context ingestion callbacks.**
- **ALB-level enforcement ⇒ in-cluster traffic is unaffected** (never hits the ALB).

Therefore: enforce at the routing layer. And note **both** CloudFront's VPC origin and API
Gateway's VPC Link arrive at the *same* ALB on port 80 with no distinguishing attribute, so a
listener rule needs a discriminator. Two workable shapes:

- **(a) Separate listener port.** Point the API-GW VPC-Link integrations at a second ALB listener (e.g. 8081) and add a rule that only that listener serves `/internal/*`; the CloudFront-facing listener 80 returns 403 for `/internal/*`. Clean, no header trust, and the SG rule that already exists (`api-gateway/main.tf:89-99`, source = VPC-Link SG) makes it enforceable.
- **(b) Header discriminator.** API GW injects a secret header only it can set; the ALB rule requires it. **This is only sound if layer 1 ships first and holds** — otherwise a client sets the header through CloudFront. Layers 1 and 2 are coupled under (b); under (a) they are independent.

**Recommendation: (a).** It removes the coupling, and it is the only shape where layer 2 is
a true chokepoint rather than another trusted header.

---

## 5. App-layer fixes — corrections to the proposed diff

**f-bab519a0 (`dependencies.py:154-168`) — raise 403. Agreed, with two notes.**
- The fallback's stated purpose is "allows the onboarding endpoint to be called by any valid IAM identity" (`:156-158`). It has **zero test coverage** — every test overrides `get_current_user` via `dependency_overrides`. So CI will not tell you whether something depended on it. Enumerate callers before merging.
- Worse than the issue states: `get_current_user` backs ~18 routers (admin, usage, budget, knowledge, activity, ratelimit…). The fallback yields `org_id=""` on **all** of them. Before shipping, confirm no query treats empty `org_id` as *unscoped* — an `org_id=""` context reaching a tenant-scoped filter is a second cross-tenant read.

**`verify_internal_or_irsa` — require a second factor. Agreed, but fix the fall-through first.**
There is an ordering bug in the current code that the proposed fix does not address:
`parse_assumed_role_arn` returns `None` for an unparseable ARN (`agent_registry.py:214-215,234-235`),
`extract_iam_identity_from_headers` then returns `None` (`middleware.py:483-485`), and
`auth_deps.py:64,85-86` therefore **falls through to the shared-secret path**. So an attacker
supplying a *malformed* `X-Caller-Identity` is routed to shared-secret auth rather than
rejected. After the fix, **presence of `X-Caller-Identity` must be terminal** — never fall back.
(Note `parse_assumed_role_arn`'s fallback at `:230-232` returns any string starting
`arn:aws:iam::` containing `:role/` verbatim, unvalidated.)
Also: `tests/internal/test_auth_deps.py:122-133` currently **pins** the IRSA-`None` ⇒
shared-secret fallback as intended behavior. That test must be inverted.

**f-72ed7277 — deleting `extract_api_gateway_context` is safe. Confirmed, with added scope.**
- Nothing in the deployed system sets `X-Auth-Source`: the Lambda authorizer that would is **unattached** — `authorizer_id` flows only to outputs, with no consumer anywhere (`lambda-authorizer/main.tf:496-506`, own comment at `:480-494` says so), and every method is `NONE` or `AWS_IAM`. The agent sigv4-proxy sends only `x-agent-orgid`, **not** `x-auth-source` (`sigv4-proxy.ts:81`). So the function is dead in production and safe to delete.
- **Scope the issue misses:** `budget/enforcement_middleware.py:143-147` trusts `x-agent-budgetconfigid` under the same `trust_apigw_headers` flag. Same class, same flag, must be in this fix.
- **Do not** delete the `#747` override at `middleware.py:499-506`. That path trusts `X-Agent-OrgId` **only** when the registry entry's `scope == "internal"`, and is pinned by `tests/iam_identity.py:343-383` (accepted for internal) and `:385-426` (rejected for shared). It is the legitimate tenant-attribution mechanism for the worker. Residual risk — a compromised internal-scope agent can misattribute spend — is accepted by design; record it rather than "fixing" it here.
- 18 tests in `tests/test_apigw_auth.py` assert on `extract_api_gateway_context` and must be deleted/rewritten in the same PR. `tests/test_apigw_auth.py:674-699` asserts `/health`, `/admin/models`, `/` must **not** trigger IAM extraction — if the fix widens the middleware to `/internal/v1/*` (it is currently absent from `ENFORCED_PATHS`, `shared/enforced_paths.py:24-34`), those exemptions must survive.

---

## 6. Deployment — ordering, and one trap

The issue correctly identifies that CloudFront/API-GW/ALB changes need a manual
`gateway-infra-apply.yml` (`workflow_dispatch` only). Two things it omits.

**Ordering.** Each layer is independently safe; the risky one is last:
1. **Layer 1 (edge strip)** — manual infra apply. Immediately closes the CRITICAL from the internet. Independently revertable.
2. **Layer 3 (app)** — merges via `gateway-deploy.yml`. Strictly stricter; no infra dependency.
3. **Layer 2 (routing)** — manual infra apply. Riskiest; do it last, after §4(a) is agreed.

**The `enable_vpc_origin` trap.** `gateway-infra-apply.yml:354` sets
`TF_VAR_enable_vpc_origin` from a live ALB probe. If the probe returns empty at apply time,
`local.api_origin_enabled` goes false and the `/api/*` **and** `/.well-known/*` behaviors are
**removed entirely** — every API call falls through to the S3 SPA (HTTP 200 + HTML, the exact
failure mode CLAUDE.md warns about for `VITE_API_URL`). Pre-apply check: confirm the plan
shows an **update** to the behaviors, not a delete. Run `terraform plan` and require
0 unexpected destroys, per the issue.

**Rollback lever worth documenting.** `BG_TRUST_APIGW_HEADERS` is substituted to `true`
unconditionally (`gateway-deploy.yml:226`) though it defaults `False` (`config.py:45`).
Flipping it false in the configmap disables `dependencies.py:141` and both
`middleware.py:559/591` branches — an emergency kill-switch for f-bab519a0 and f-72ed7277
requiring no code change. It would also disable legitimate IAM agent auth, so it is a
break-glass, not a fix.

---

## 7. Recommended split

The four findings do not share a fix surface as evenly as the grouping implies. Layers 1 and 3
are ready to implement today; the internal-plane invariant needs D2 decided first.

- **A1 — edge strip + app registry-reject (f-42dca300 partial, f-bab519a0, f-72ed7277).** Ready. CloudFront Function denylist + `dependencies.py` 403 + terminal `X-Caller-Identity` + delete `extract_api_gateway_context` + budget-middleware header. Closes the internet-facing CRITICAL.
- **A2 — internal-plane tenant compare + `/internal/*` routing (f-9bbb81ba, f-42dca300 remainder).** Blocked on D1/D2 sign-off: `TokenContext.scope`, the per-route table in §3, and the ALB shape in §4.

---

## 8. Validation gaps

The issue's Validation section is reasonable but omits the tests that currently **pin the
vulnerable behavior** and must therefore be *changed*, not merely added:

- `tests/internal/test_admin_routes.py:241-276` — asserts omitted `org_id` ⇒ no org filter. Invert.
- `tests/internal/test_auth_deps.py:122-133` — asserts IRSA-`None` ⇒ shared-secret fallback. Invert.
- `tests/test_apigw_auth.py` — 18 assertions on `extract_api_gateway_context`. Delete with the function.
- `tests/test_apigw_auth.py:674-699` — `/health`, `/admin/models`, `/` exempt from IAM extraction. Must still pass.
- `tests/auth/test_status_callback_routes.py:79-83` — mutates `sys.modules` for `auth_deps`; fragile to any import-shape change there.

Add: a test that `/internal/v1/*` rejects a request whose `X-Caller-Identity` is *malformed*
(currently falls through to shared secret), and an edge test asserting the CloudFront Function
deletes each blocked header (CloudFront Functions are unit-testable via `aws cloudfront
test-function`).

---

## 9. Latent bug found during review — file separately

`webhook-ingress/lambda/common/gateway_client.py:88,103-110` builds
`{apigw-invoke-url}/internal/v1/resolve-user` and sends `X-Internal-Api-Key` over plain
`urllib` with **no SigV4 signing**. But API Gateway's `/internal/{proxy+}` is `AWS_IAM`
(`api-gateway/main.tf:243-246`), so the unsigned request is rejected by API Gateway **before
reaching the pod**. `resolve-user`, `resolve-installation` and `provenance` via this client
therefore fail-soft to `None` today (`:84-86`).

Implications: (a) regression risk for those three endpoints under this issue is **nil** —
they are already dead on that path; (b) it is a real functional bug deserving its own issue;
(c) **do not** "fix" it by relaxing the `AWS_IAM` route — sign the request instead.

---

## References

- Issue #3985; sub-EPIC #3984; EPIC #615
- Prior work: #575 (shared-secret → IRSA migration, CLOSED — this issue is its unfinished half), #1108 (`/internal/{proxy+}` AWS_IAM route), #260/#240 (dual-path auth), #747 (internal-scope org override), #3142 (`docs/design/credential-authorization-binding.md`)
- Acknowledged TODO now superseded: `provenance_routes.py:16-19`
- Cleanups in scope while in these files: dead duplicate `_verify_internal_key` (`internal/routes.py:49`), stale docstring (`credential_routes.py:12-13`), stale `docs/endpoint-audit.md:59,64-67`

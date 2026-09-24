# S10 — verified gateway caller identity: revalidation and disposition

Work-package **S10** of the 2026-09-21 security scan (parent #5599, issue #5609).
Revalidates the 2026-08-30 ticket **#4701** ("Internal and gateway planes trust
unverified caller-identity headers as authentication") against current `main`.

- Revalidated at commit **`7b43ab8b9381f4d3a7f17acc7caca5b15f191a54`** (`main` merge of #5873).
- Findings covered: `f-0c03d9e0-5c00-41d6-b466-6725a26a293e`,
  `f-9d38d1ac-4c97-4fdf-abe7-f07a1860d5e6`, `f-ec1294be-fec9-4e1a-b6a7-ffa09dd0a0bb`.
- Scope of this document: **read-only revalidation**. No production source, shared
  fixture, release pin, scanner baseline or flow registration was changed.

## Summary

**The exercised public/internal header-authentication paths and canonical shared-secret
comparison have merged fixes; #4701's full acceptance remains open.** AWS **A01 / #5653**
(PR #5737, merge `6455f8b5`, 2026-09-22) supplies the provenance control, and **A05 / #5656**
(PR #5787, merge `058745bc`, 2026-09-23) fixes the active shared-secret comparison.
Internal rate limiting, an unused duplicate comparator, and the additional acceptance
items mapped below remain to reconcile. This package introduces no competing implementation.

The load-bearing change is that an asserted identity is now only an identity where the
edge vouched for it. `X-Caller-Identity` alone proves nothing: the gateway additionally
requires a constant-time match on `X-Adp-Edge-Provenance`, a 64-character secret that
Terraform generates, stores in SSM as a `SecureString`, and exposes only to API Gateway
and the gateway pod. API Gateway writes both headers **only** on its three `AWS_IAM`
routes and blanks both on every unauthenticated route, so a direct-to-cluster or
direct-to-invoke-URL caller can copy the identity header but cannot satisfy the proof.

**Two important qualifications the older ticket's prose does not survive.**

First, the superseded scanner narrative on #5653 asked to make the shared secret mandatory
"in all cases" and remove the identity short-circuit. #4701 itself lists signed-edge,
workload-identity and mutual-TLS alternatives. The two supported IRSA callers
(the `scaledjob-worker` pods and the platform deploy-runner)
authenticate by SigV4 and hold no copy of `BG_INTERNAL_API_KEY`, so requiring both would
403 every worker credential fetch and every customer-deploy credential assumption. The
current owner deliberately declined that instruction and recorded why in
`src/internal/auth_deps.py:24-45`. This preserves A01's reviewed acceptance ("retain a
proven SigV4/IAM path rather than mandating both IAM and a shared secret") and this
revalidation assignment's explicit instruction.

Second, **code remediation is not deployed protection everywhere.** A01 is the one
package in this scan day with retrievable live evidence (dev gateway, account
879318057152: forged-identity 403 and direct 401 captured across replicas, per
`S21-evidence-ledger.md:508`). A05's owner records live acceptance as **PENDING**
(`:512`). Nothing in this document establishes production rollout.

Residual controls and acceptance gaps are mapped below. Their presence does not invalidate
the tested fixes, and those fixes do not establish completion of the entire older ticket.

| # | #4701 finding | Verdict on current `main` | Closed by | Owner |
|---|---|---|---|---|
| 1 | Public edge trusts client identity header | **Remediated in code** | A01 #5653 / PR #5737 `6455f8b5` | A01 (closed) |
| 2 | Internal plane accepts header with no gate | **Remediated in code** | A01 #5653 + #3985 scope gate | A01 (closed) |
| 3 | Shared secret compared with `!=`, no rate limit | Active comparison fixed; rate-limit acceptance **open** (R2); unused comparator remains (R4) | A05 #5656 / PR #5787 `058745bc` | A14 #5670 coordination; S10 retains residual acceptance |

---

## 1. Finding 1 — public edge trusted a client-written identity header

**Verdict: remediated in code.** Three defences now compose, and the assertion fails
closed if any is absent.

*Application.* `src/auth/caller_provenance.py` is the single definition of a trustworthy
assertion. `verified_caller_identity()` returns an ARN only when the trust flag is on
(`:89`), a provenance secret is configured (`:96`), and the supplied
`X-Adp-Edge-Provenance` matches it under `secrets.compare_digest` (`:101`). Each failure
emits a distinct reason (`header_trust_disabled`, `provenance_secret_unconfigured`,
`invalid_edge_provenance`) and a metric, so a caller broken by the tightening surfaces as
a signal at the point of rejection rather than an unexplained downstream 403. The ARN is
deliberately **not** logged — it is attacker-controlled on exactly the requests that reach
there.

All three consumers route through that one helper, which matters because partial fixes in
this area have left doors open before:

| Consumer | Location | Reads identity via |
|---|---|---|
| `get_current_user` (~18 routers) | `src/auth/dependencies.py:207-210` | `has_caller_identity_assertion` → `verified_caller_identity` |
| `TokenContextMiddleware` (metered proxy) | `src/auth/middleware.py:498` | same helper |
| `extract_iam_identity_from_headers` | `src/auth/middleware.py:389` | same helper |
| Internal-plane guard | `src/internal/auth_deps.py:178-181` | same helper |

I verified the closure is total by tracing every site that can mint an
`auth_source="iam"` context. `auth_source="iam"` is set in exactly one place
(`src/auth/agent_registry.py:285`, reached only via registry resolution), and
`request.state.token_context` is assigned at only four sites
(`middleware.py:506-507,544-546`, `internal/auth_deps.py:244`, `proxy/routes.py:272,278`).
The proxy site is Cognito-JWT only and returns any context already set upstream
(`proxy/routes.py:264-278`). The #240 `X-Auth-Source` / `X-Agent-*` trust branch was
removed by #3985 (`middleware.py:525-527`, `proxy/routes.py:239-242`), and
`X-Identity-Source` from the ticket's prose **does not exist in this repository** —
superseded scanner narrative, not a live header.

*Edge.* `modules/gateway/infra/modules/api-gateway/main.tf:196-204` defines two
treatments. `blank_caller_identity` sets both headers to `''`; `verified_caller_identity`
maps `context.identity.userArn` plus the generated secret. Applied per route:

| Route | Auth | Treatment |
|---|---|---|
| `/`, `/{proxy+}` catch-all, `/auth/github/{proxy+}` | `NONE` | **blanked** (`:253`, `:286-291`, `:416`) |
| `/agent`, `/agent/{proxy+}`, `/internal/{proxy+}` | `AWS_IAM` | identity + proof (`:311`, `:338-343`, `:379-384`) |

Blanking at API Gateway — not only at CloudFront — is the control that answers the
ticket's "sidestepped by addressing the API endpoint directly" concern, and the code says
so at `:282-285`. CloudFront additionally deletes the header family in a viewer-request
function (`cloudfront/main.tf:138-145`), but it is not the only way in, so it is not
load-bearing.

A plan-time `lifecycle { postcondition }` (`api-gateway/main.tf:476-512`) decodes the
rendered body and rejects an integrated edge path that omits either mapping. This guards
API Gateway header configuration; it does not by itself prove that every newly mounted
application router has an authentication dependency. The separate route inventory below
must retain that distinction.

*Configuration.* `BG_TRUST_APIGW_HEADERS` is no longer forced on. Both renderers read it
from SSM defaulting to `false` (`gateway-deploy.yml:450-451`,
`deploy-all.sh:947-948`); the ConfigMap keeps a placeholder
(`k8s/configmap.yaml:168`); the application default is `False`
(`src/shared/config.py:75`); and both renderers **fail the deploy** if trust is on while
the provenance secret is empty (`gateway-deploy.yml:627-633`,
`deploy-all.sh:1014-1019`). This directly closes #4701's worst-named bug class
("fail-open configuration default carried forward"), and the flag is not the sole control
in any case — the proof check is independent of it.

## 2. Finding 2 — internal plane accepted the header with no gate

**Verdict: remediated in code.** `verify_internal_or_irsa`
(`src/internal/auth_deps.py:146-271`) now enforces, in this order: provenance
verification (`:181`), registry resolution (`:200`), resolvability (`:214`), and only
then the scope check against `INTERNAL_PLANE_SCOPES = {"internal", "platform"}`
(`:230`). Scope is therefore consumed **after** verified identity, never before — the
ordering S10's acceptance asks to prove.

The scope allowlist is not self-assignable: the admin API constrains caller-settable
scope to `^(shared|personal)$`, so `internal` and `platform` are written only by
Terraform seeds (`auth_deps.py:66-88`). A registered agent holding valid IRSA
credentials for some other purpose cannot reach the internal plane merely by being
registered.

Presenting `X-Caller-Identity` is **terminal**: a failed assertion is rejected, never
fallen through to the shared secret (`:183-195`). Without this, anyone holding the secret
could send a forged ARN and receive the same 200 as a legitimate caller, masking the
attempt.

**Mount coverage — dependency inventory plus source review.** I enumerated the real
application's routes and inspected each route's dependency tree (reproducible probe in
Appendix B). This executes application construction, not requests through all 78 endpoints:

```
total mounted /internal/* routes : 78
  guarded DIRECTLY by verify_internal_or_irsa : 37
  guarded INDIRECTLY (wrapper calls it)       : 32   [require_agent_transport, verify_model_probe_irsa]
  NO path to that guard                       :  9
```

Eight of the nine use other verification paths, and one intentionally publishes public
verification keys. Their absence from the shared-guard dependency count is not itself
an authentication bypass; the mechanisms were inspected individually below.

| Route(s) | Proof required | Evidence |
|---|---|---|
| `/agent/work/admit`, `/agent/roots/admit`, `/agent/persona-model/resolve` | STS `GetCallerIdentity` SigV4 proof, signed invocation header, role allowlist | `work_routes.py:41-68`; `external_roots.py:151-155`; `persona_model_selection.py:33-36` |
| `/agent/chat/model-decision` | Kubernetes TokenReview pod identity + live grant + bound dispatch pointer | `chat_model.py:43-54` |
| `/agent/arc/model-decision`, `/agent/arc/cyber/jobs`, `/agent/arc/cyber/result` | GitHub OIDC claims → registered workflow + human owner | `arc_model.py:72-134`; `cyber_jobs.py:68-105` |
| `/agent/legacy-chat-preflight` | `extract_iam_identity_from_headers` (hence the provenance helper) | `model_policy_keys.py:39` |
| `/agent/model-policy-keys` (GET) | None by design — publishes only the **public** half of a signing key | `model_policy_keys.py:15-24` |

Two handlers read `X-Caller-Identity` raw (`domain_operation_runtime.py:36`,
`vault_evidence_routes.py:265`), which looks like a bypass but is not: both require
`auth_source == "iam"` on the server-held context *and* a registry row whose scope is in
`INTERNAL_PLANE_SCOPES` (`:38-41`, `:267-271`). The raw header only selects which row to
re-read strongly-consistently; it grants nothing on its own. Both sit behind
router-level guards. Worth a readability note to their owners (S12), not a finding.

## 3. Finding 3 — non-constant-time secret comparison and no rate limiting

**Comparison: remediated in code.** `_verify_internal_key`
(`src/internal/auth_deps.py:107-144`) uses `hmac.compare_digest` on both encoded sides.
Absent/empty is handled *before* the call because `compare_digest` raises on `None` —
getting that order wrong would turn a hardening change into a 500 on every internal
request, which is the ticket's own "incorrect constant-time comparison" bug class. All
shared-secret failures return one identically-built response (`_reject_internal_key`,
`:96-105`), so absent, empty, wrong-length and wrong-content are indistinguishable in
status and body. No timing experiment was performed; early returns and the comparison
primitive's length behavior do not establish identical end-to-end latency.

The ticket's duplicate comparison is **not fully removed**. `src/internal/routes.py:77-91`
still defines an older `_verify_internal_key` using `!=`. No mounted production call site
was found: the four handlers in that module use `verify_internal_or_irsa`, which calls
the corrected helper in `auth_deps.py`. This is an unused residual helper and a future
reuse hazard (R4), not evidence of a currently reachable timing oracle. The existing
constant-time regression checks the canonical module, not every duplicate in the repository.
The agent-context Door has a separate service-key check using `compare_digest`; it is not
the duplicate gateway helper described by the older ticket.

**Rate limiting: still open (R2).** `/internal/` is absent from `ENFORCED_PATHS`
(`src/shared/enforced_paths.py:24-35`), which the rate-limit middleware consumes alongside
the auth and budget middlewares (`ratelimit/enforcement_middleware.py:20,104`). Verified by grep: no
`/internal` reference anywhere in `src/ratelimit/`. So the unrate-limited-attempt half of
this finding is **not** closed. Severity is materially reduced — a constant-time
comparison removes the byte-by-byte oracle, leaving only unthrottled brute force against
a full-entropy secret — but the acceptance item is unmet.

## 4. Stable identity/tenant context contract (for S11, S13, S15)

Downstream packages should **consume** this and not redefine it:

1. **Never read `X-Caller-Identity` to establish identity.** Call
   `verified_caller_identity(request, settings=get_settings())` from
   `src/auth/caller_provenance.py`. Pass your own module's settings — this is a
   load-bearing seam, not cosmetic: each consumer resolves configuration through the
   `get_settings` imported into its own module, and omitting it made the first cut of
   A01 reject the legitimate IRSA path with a 403 (`caller_provenance.py:74-82`).
2. **Preserve each authentication surface's fallback contract.** In `get_current_user`,
   an unproven IAM header is ignored and the caller may still authenticate through an
   independently valid Cognito JWT (`dependencies.py:203-205`). Only a proven IAM assertion
   commits that dependency to IAM resolution. In `verify_internal_or_irsa`, raw assertion
   presence is terminal: failed provenance cannot fall back to the shared secret.
   `test_caller_provenance.py` covers that fallback ending in 401 when no JWT is supplied;
   valid-JWT behavior here is established by the source branch, not that negative test alone.
3. **For IAM contexts, tenant/org identity comes from the registry row**, never from a caller header.
   `agent_entry_to_token_context` (`src/auth/agent_registry.py:280-300`) is the only
   place `auth_source="iam"` is minted, and it carries `scope`, `requires_run_identity`
   and `credential_scopes` through from the registry.
4. **Authorize after verification, never before** — the `auth_deps.py:181 → :200 → :230`
   ordering. S13 (roles/admin) inherits this ordering requirement directly.
5. **New `/internal/*` routers** must either depend on `verify_internal_or_irsa`, carry
   independently reviewed verification, or have an explicit narrowly public contract such
   as publishing public verification keys. If you add an edge route, the Terraform
   postcondition (`api-gateway/main.tf:476-512`) will fail your deploy until the header
   mapping is declared — that is intended.
6. **`internal` / `platform` scopes must stay non-self-assignable.** Any change relaxing
   the admin API's `^(shared|personal)$` constraint defeats the internal-plane gate
   entirely.

## 5. Validation performed

Offline, external adapters mocked, no live tenant/cloud calls, no secret access.

```
cd modules/gateway
python3 -m pytest tests/auth/test_caller_provenance.py \
  tests/auth/test_caller_provenance_infra.py tests/auth/test_spoofed_context_rejected.py \
  tests/auth/test_iam_identity.py tests/test_apigw_auth.py -q
# 109 passed

python3 -m pytest tests/internal/test_auth_deps.py tests/auth/test_internal_routes.py \
  tests/auth/test_endpoint_inventory_complete.py tests/auth/test_dependencies.py \
  tests/auth/test_authority_after_authentication.py \
  tests/auth/test_validation_path_boundary.py -q
# 118 passed

python3 -m ruff check src/internal/auth_deps.py src/auth/dependencies.py \
  src/auth/middleware.py src/auth/caller_provenance.py
# All checks passed
```

**227 tests pass.** The four cases S10's acceptance names are all covered:

| Case | Evidence |
|---|---|
| Anonymous | `test_auth_deps.py::TestNeitherPresent::test_no_headers_rejected` |
| Spoofed header | `test_caller_provenance.py` — all cases; `test_apigw_auth.py::TestApiGatewayHeadersNoLongerAuthenticate::test_headers_do_not_authenticate_with_trust_enabled` |
| Wrong tenant/scope | `TestInternalPlaneScope::test_non_internal_scope_rejected`, `::test_non_internal_scope_does_not_fall_back_to_shared_secret` |
| Valid call still works | `TestInternalPlaneScope::test_internal_scope_accepted`, `::test_platform_scope_accepted_deploy_runner`, `::test_clusterip_shared_secret_caller_not_scope_checked` |
| Constant-time secret | `TestSharedSecretConstantTimeComparison` (`test_auth_deps.py:384`) |

The supervising review independently repeated the combined 227-test selection using
`uv run --frozen --extra dev --python 3.12 python -m pytest` at the pinned source revision:
**227 passed in 1.91 seconds** (13 warnings). This verifies the named test selection;
the scope tests above are not exhaustive cross-tenant authorization tests for every route.

**The spoofing tests are real evidence, not theatre.** `test_caller_provenance.py:44-50`
uses a well-formed assumed-role ARN naming the genuinely seeded privileged principal
(`adp-dev-agent-scaledjob-role`, scope `internal`, `allowed_models=["*"]`) rather than
malformed junk. That distinction decides whether the suite is evidence at all: the
pre-fix code rejected unparseable ARNs too, so a junk-payload test would pass against
the vulnerable version. These requests would succeed with real platform authority if
provenance were skipped.

**Not run / not verified:** no live or staging calls, so #4701's operator end-to-end
check (out-of-network forged assertion; in-cluster call from a workload holding no
internal identity; latency indistinguishability) is **unverified by me**. A01's recorded
dev-gateway rollout evidence covers part of this; production is unverified.
`terraform plan`/`validate` was not run — no Terraform was changed, and the infra
assertions above are source inspection plus the existing
`test_caller_provenance_infra.py` suite.

## 6. Disposition per finding, and routing of residuals

| Finding | Verdict | Code | Tested | Deployed | Acceptance |
|---|---|---|---|---|---|
| `f-0c03d9e0…` (edge header trust) | Remediated | A01 #5653 / `6455f8b5` | yes | dev only | open (supervisor) |
| `f-9d38d1ac…` (internal plane no gate) | Remediated | A01 #5653 + #3985 | yes | dev only | open (supervisor) |
| `f-ec1294be…` (timing oracle) | Remediated (comparison); **R2 open** (rate limit) | A05 #5656 / `058745bc` | yes | owner: live **PENDING** | open (supervisor) |

This report does not assign a false-positive disposition or independently reproduce the
historical scanned revision. It validates current controls and preserves the remaining
acceptance gaps instead of inferring full closure from merged fixes.

### Additional material acceptance from #4701

| Acceptance item | Current source/evidence | Remaining responsibility |
|---|---|---|
| Apply the nested internal-plane edge deny | Both deployment paths already call gated `modules/gateway/scripts/apply-internal-plane-deny.sh` after confirmed front-door repoint: `.github/workflows/gateway-deploy.yml:894-927` and `platform/scripts/deploy-all.sh:1274-1281`. The nonrecursive base manifest glob is deliberate ordering; replacing it with a generic recursive apply would bypass that gate. | Source ordering exists; S10 must reconcile actual environment rollout evidence. No deploy was run here. |
| Restrict gateway reachability from low-trust workloads | Caller-side egress policy and provenance checks compose; the source's network-policy-controller settings are recorded in Appendix A. | Effective cluster enforcement and remaining callers require rollout evidence; source objects alone do not establish it. |
| Explicit legacy shared-secret fallback switch and migration | Dual auth deliberately remains for supported ClusterIP callers. There is no separately verified default-off legacy-fallback switch; an unconfigured internal key fails closed. | S10 retains the acceptance gap and coordinates any defaults/caller migration with A03 #5655. Do not disable valid callers or require both IAM and a shared secret. |
| Audit every internal authentication decision with verified principal and method | Provenance rejection metrics and guard logs exist. This review does not establish a durable audit record for every decision. | S10 retains this acceptance; coordinate shared audit design with S13 #5612 before assigning an implementation extension. |
| Enforce proof coverage for every mounted internal router | Appendix B inventories dependencies; the nine exceptions are reviewed in §2. This is not an exhaustive request-level regression over every route. | S10 retains regression-coverage acceptance; future guards must allow declared independent proof and intentionally public verification keys. |
| Restrict direct API access with a resource policy; rate-limit internal attempts | Conditional policy and missing rate-limit scope are described in R1/R2. | A03 #5655 and A14 #5670 are the coordination owners; completion is not established here. |
| Operator refusal, legitimate-call and timing checks | A01 records scoped dev evidence; no new live or timing experiment was run by this revalidation. | S10/supervisor must reconcile rollout and final acceptance. |

### Residual gaps — routed, not implemented here

**R1 — empty source-CIDR defaults omit the optional API Gateway policy. → coordinate with A03 / #5655**
(fail-closed defaults owner). `aws_api_gateway_rest_api_policy.main` is
`count = 0` whenever both CIDR lists are empty
(`api-gateway/main.tf:554`, `:597`), both variables default to `[]`
(`infra/variables.tf:688-698`), and `grep -rn "route_source_cidrs" environments/`
returns **nothing** (exit 1). These source defaults omit the policy unless overridden;
they do not prove whether a deployed dev policy exists, because live configuration,
Terraform state and external overrides were not queried. Those routes remain `AWS_IAM`
in source, so SigV4 is still the admission control, and the
unauthenticated catch-all blanks the identity header. It is the missing defence-in-depth
layer #4701 asked for. A03 owns fail-closed defaults; sequencing a CIDR default is its
call, not S10's.

**R2 — `/internal/` is not included in the inspected rate-limit middleware. → coordinate with
A14 / #5670; retain the exact internal-auth acceptance under S10.** `ENFORCED_PATHS`
(`src/shared/enforced_paths.py:24-35`) omits `/internal/`.
This is the unclosed half of `f-ec1294be…`. I did **not** implement it: that file is the
shared enforcement list feeding three middlewares (auth, budget, rate-limit), adding
`/internal/` would change budget and auth-middleware behaviour for every internal route
as a side effect. A14 already owns gateway rate-limit configuration, service, routes,
backends and enforcement. Its existing scope does not automatically establish that this
specific internal-auth criterion is assigned or complete: coordinate it explicitly, with a
scoped design such as a rate-limit-only path set, rather than start a competing implementation.

**R3 — stale comment, documentation only.**
`modules/agent-factory/webhook-ingress/infra/scaledjob-netpol.tf:101-104` still asserts
the gateway "treats the `X-Caller-Identity` header as an authenticated identity assertion
with no signature check", and lists an app-side guard as a future prerequisite
(`:123-125`). That guard shipped as `caller_provenance.py`. The comment now understates
the gateway's posture. No security impact — the netpol's actual effect (no in-cluster
egress path to the gateway, by omission) is unchanged and still correct. Left unedited:
it is a caller-side file owned by another package.

**R4 — unused duplicate gateway secret comparator. → S10 acceptance/coordination with A05.**
The unused `src/internal/routes.py::_verify_internal_key` retains `!=` (§3). Remove or
delegate that helper and extend the canonical-only regression guard when source ownership
allows it. A10 #5664 is actively changing `internal/routes.py`; this read-only assignment
does not create another writer. No currently mounted route was found using the residual helper.

## Appendix A — what was checked, and what was not

Checked by source inspection at `7b43ab8b`: the four application consumers; every
`auth_source="iam"` mint site and `token_context` assignment; all 23 `/internal/*`
routers; API Gateway route/header mappings and the plan-time postcondition; the
CloudFront viewer-request function and its behaviour coverage; both deploy renderers and
the ConfigMap placeholder; the agent-pod egress policies; the agent-context Door key
check. Checked by execution: the 227 tests and the route-mount probe.

**Not checked:** any live environment (no cloud, tenant or secret access in scope);
`terraform plan`; whether NetworkPolicy enforcement is actually on in any cluster beyond
`environments/dev/platform.tfvars:36` setting
`enable_network_policy_controller = true` (the platform default is `false`,
`platform/infra/variables.tf:179-183` — objects are accepted but never enforced when
off); production rollout of either A01 or A05.

## Appendix B — route-mount probe

Run from the repository at the pinned revision `7b43ab8b9381f4d3a7f17acc7caca5b15f191a54`.
The probe constructs the application without entering its lifespan or sending requests.
It walks dependencies and classifies the two wrapper functions whose source was inspected
in this review. Wrapper names are an explicit reviewed allowlist, not proof that an arbitrary
future wrapper invokes the guard on every path. No cloud credentials are required.

```bash
cd modules/gateway
BG_TOKEN_SECRET_KEY=revalidation-token-placeholder \
BG_INTERNAL_API_KEY=revalidation-key-placeholder \
AWS_EC2_METADATA_DISABLED=true \
uv run --frozen --extra dev --python 3.12 python - <<'PY'
from collections import Counter

from fastapi.routing import APIRoute
from src.app import create_app

wrappers = {"require_agent_transport", "verify_model_probe_irsa"}


def dependency_names(dependant):
    names, seen, pending = set(), set(), [dependant]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        names.add(getattr(item.call, "__name__", ""))
        pending.extend(item.dependencies)
    return names


counts = Counter()
exceptions = []
for route in create_app().routes:
    if not isinstance(route, APIRoute) or not route.path.startswith("/internal/"):
        continue
    names = dependency_names(route.dependant)
    if "verify_internal_or_irsa" in names:
        category = "direct"
    elif names & wrappers:
        category = "wrapper"
    else:
        category = "other"
    counts[category] += 1
    if category == "other":
        exceptions.append((sorted(route.methods), route.path))
print("total:", sum(counts.values()), "categories:", dict(counts))
for methods, path in exceptions:
    print(",".join(methods), path)
PY
```

Both `BG_*` values are inert import placeholders (`token_manager.py:54` requires the first).
The supervising review independently reproduced:

```text
total: 78 categories: {'direct': 37, 'wrapper': 32, 'other': 9}
```

The nine routes in the other category are accounted for in §2. A future route-coverage
regression must permit the reviewed independent verification mechanisms and intentionally
public key endpoint, while detecting newly unprotected routes. That acceptance remains
with S10; this inventory is evidence preparation rather than the regression itself.

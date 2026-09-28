# Spike 4188 — run design for the gated dsh experiment

> **Status: DESIGN ONLY — NOT RUN. Awaiting human gate.**
>
> Issue #4188, sub of #4174 / EPIC #1219. Architect-authored per the request on
> #4188 to specify the spike concretely before anyone stands up third-party code.
>
> **Subject:** `deepseek-ai/deepseek-harness` pinned at `dsh-v0.1.1-rc.2`.
> **Source spec:** `docs/research/deepseek-harness-fit-assessment.md` §6.
> **Deliverable of the run:** `docs/research/dsh-experiment-results.md` — a decision
> record, not an integration.

## 0. What this document is for

§6 of the assessment specifies the experiment. This document makes it *runnable*:
it grounds §6 against the live tree, fixes the six places where §6's assumptions
do not match what is actually deployed, and states the isolation gates that must
pass **before** the harness is given a model credential.

**The outcome "confirmed — do not adopt" is a complete success.** So is
"inconclusive", per test. There is no version of this spike that ends with dsh in
the dispatch path. Anyone who finds themselves wiring dsh into a live path has
left the spike.

## 1. Grounding — where §6 diverges from the live tree

Every row here was verified against the tree at `6f16e51`. These are corrections
to the *experiment spec*, not to the assessment's verdict, which stands.

| # | §6 assumption | Live reality | Consequence for the run |
|---|---|---|---|
| 1 | "Kill the pod; a fresh pod resumes from the persisted log" | dsh's three persistence backends (JSONL / SQLite / in-memory, `packages/session/`) are all **node-local filesystem**. The cluster is EKS Auto Mode (`platform/infra/modules/eks/main.tf:53-58`); there is **no EFS and no RWX filesystem** in the tree. The only `ReadWriteMany` volume is Mountpoint-for-S3, whose own header states "**NO random/partial writes, NO file locking**" (`modules/agent-context/manifests/s3-files-storage.yaml:1-9`) — exactly what SQLite and an appending JSONL writer need. | **Test 5 needs an explicit durability substrate or it is not a test of dsh.** See §4. This is the single most consequential item in this document. |
| 2 | "existing sigv4 sidecar at `127.0.0.1:9090`" | Not a sidecar container. It is a **subprocess spawned by our Python entrypoint** — `SIGV4_PROXY_SCRIPT = "/app/dist/sigv4-proxy.js"` (`modules/agent-factory/agent-worker-image/entrypoint.py:1641`), started at `:1429`, health-gated at `:1698`, killed at `:1526` — bundled inside the agent-runtime image. | A dsh pod does **not** inherit it. Must run as a real sidecar container in the experiment pod. See §3.4. |
| 3 | "read-only agent-context credentials" as a structural guard (§6 invariant 5, and the ⚠️ note) | **No read-only mode or scope exists.** The Door has exactly one credential, `x-internal-api-key` (`modules/agent-context/door/auth.py:52`), with no scopes. And **2 of the 7 verbs write**: `remember` (`door/mcp_app.py:290` → `remember_backend.py:18`) and `experience` with `action="save"` (`mcp_app.py:306` → `personal_context/storage.py:95`). | 🔴 **§6's stated structural guard does not exist.** Combined with mandatory `approval: 'never'` in headless, dsh would auto-approve writes into the personal-context store. See §3.5 — the guard becomes *don't grant the Door credential for the write-capable surface at all*. |
| 4 | Budget/rate-limit enforcement will simply apply to the dsh pod | Enforcement keys off `token_context` in ASGI scope state, set upstream by `TokenContextMiddleware`. **If it is absent, the request passes through *unenforced*** (`budget/enforcement_middleware.py:81-86`). And `token_context` for a pod comes from SigV4-via-API-GW plus a **DynamoDB agent-registry lookup** on the caller role ARN; an unregistered role gets **403 `agent_not_registered`** (`auth/middleware.py:397-410,499-504`). | 🔴 **The experiment's role must be registered in the agent registry or there is no experiment** — every request 403s. And an unregistered-but-admitted path would make tests 2 and 3 *silently vacuous*. See §3.4. |
| 5 | NetworkPolicy as the egress control | The repo already warns: on EKS Auto Mode "if the network-policy agent is not active, the API server accepts this object and **silently enforces nothing**. `kubectl get networkpolicy` succeeding is NOT evidence that traffic is being filtered" (`modules/agent-context/manifests/networkpolicy.yaml:22-27`). No `vpc-cni` addon, no Cilium, and no `enableNetworkPolicy` anywhere in `platform/` or `environments/`. | 🔴 **Isolation must be proven by positive traffic tests, not by object existence.** See §5. A stronger control is available and unused by §6: gVisor. |
| 6 | A low budget can be set for the experiment subject (§7, and test 2) | `BudgetCreateRequest.entity_type` is `Literal["org","department","team","user"]` — **`service_account` is not creatable via the admin endpoint** (`admin/schemas.py:298`), even though the enforcement hierarchy does check `service_account` (`budget/enforcement_service.py:265-268`). Per-agent budgets were removed in #3985 (`budget/enforcement_middleware.py:95-106`), and there is **no per-run-id cap at all**. | The spend bound needs a direct `budget_configs` row, or capping the subject's team/org. §7 revised accordingly. This is also independent confirmation that #4187 is genuinely unbuilt. |

Also corrected: the assessment and `manifests/context-mcp.yaml:2` both undercount the
Door's tools. There are **7** — `search`, `understand`, `impact`, `browse`,
`remember`, `experience`, `secure` (`door/mcp_app.py:216-343`, pinned in
`modules/agent-context/tests/conftest.py:303`). #4188's body says 5, following
`CLAUDE.md`. Not material to the verdict; material to test 4's wording.

### What §6 got right, verified

- **The gateway-fronting claim holds.** `ENFORCED_PATHS` is a real single source of
  truth imported by the auth, budget, and rate-limit middlewares, and both
  `/v1/chat/completions` and `/v1/messages` are in it
  (`modules/gateway/src/shared/enforced_paths.py:25-26`). Tests 1-3 genuinely
  require **no gateway change**.
- **Run attribution works as described** — the gateway reads `x-agent-runid` into a
  contextvar for usage logging (`modules/gateway/src/proxy/routes.py:141-146`).
- **The Door derives ACL from per-caller headers** — `x-github-login`,
  `x-github-teams`, `x-tenant-id`, `x-owner-sub` (`door/acl.py:37-40`, extracted
  at `:116`), reachable in-cluster at
  `context-mcp.agent-context.svc.cluster.local:5100` over Streamable HTTP at
  `/mcp` (`manifests/context-mcp.yaml:3,108-122`; `door/mcp_app.py:143-146`). The
  static-header blocker in §Q3 is real.

## 2. Hypothesis and decision value

**Hypothesis (unchanged from §6):** a dsh headless pod can run a real coding task
against ADP's gateway with budgets, rate limits, and metering intact, consuming
agent-context MCP tools — and its event-sourced session survives a pod kill and
resumes.

**What each result licenses:**

| Test 5 outcome | What we may conclude | What we may **not** conclude |
|---|---|---|
| **Confirmed** — resume works across pod replacement | dsh's event-sourced log + non-truncating repair is a validated design, strengthening #4186's choice to copy it | That dsh should run our agents. The §Q2/§Q3/§Q6 blockers are independent and unaffected. |
| **Refuted** — resume does not survive | Only meaningful *if the substrate was genuinely durable* (§4). Then: the crash-recovery claim in `docs/subsystems/persistence.md:15` is aspirational, and #4186 must not lean on it as a reference | That durable resume is infeasible — that would be a claim about dsh, not about the architecture |
| **Inconclusive** | Say so plainly and state which substrate constraint blocked it | Anything. An inconclusive test 5 must not be written up as a soft confirmation of the existing verdict. |

The failure mode to guard against: running test 5 on node-local `emptyDir`,
observing that nothing survives, and recording "refuted." That would be a
measurement of *our* storage, reported as a finding about dsh. §4 exists to
prevent exactly that.

## 3. Environment design

### 3.1 Namespace and identity

- Namespace `dsh-experiment`, used for nothing else, created by the manifests in
  `experiments/dsh-4188/` — **not** under any module's production infra path.
- ServiceAccount `dsh-experiment-sa` with **no `eks.amazonaws.com/role-arn`
  annotation at all**. Not a narrowed IRSA role — *none*. §6 invariant 1 says
  "IRSA scoped by resource ARN"; the correct scope here is zero, because nothing
  the harness does needs an AWS API call. The sigv4 sidecar (§3.4) carries the
  only AWS identity in the pod.
- `automountServiceAccountToken: false` on the harness container's pod spec, so a
  compromised harness cannot talk to the API server at all.
- No RoleBinding. The SA has no Kubernetes RBAC.

### 3.2 Node isolation — use gVisor

§6 relies on namespace + NetworkPolicy. Given §1's NetworkPolicy-enforcement row, that is not enough on its
own for third-party code with a documented plaintext-credential finding and
documented outbound telemetry. The platform **already has** a stronger boundary:

- `runtimeClassName: gvisor` (`platform/infra/gvisor-runtime.tf:23-41`), which
  carries its own `nodeSelector adp.io/runtime=gvisor` and matching toleration.
- The gVisor node group is tainted `adp.io/runtime=gvisor:NO_SCHEDULE` with
  `desired_size = 0` (`platform/infra/gvisor-nodegroup.tf:140-156`), so the
  experiment scales a node up on demand and back to zero at teardown.

This puts a syscall boundary between the harness and the node, which is the
control that holds regardless of whether the CNI enforces NetworkPolicy.

### 3.3 Resource caps

§6 does not bound compute. Add, in the manifests:

- `ResourceQuota` on the namespace: hard caps on `pods`, `requests.cpu`,
  `requests.memory`, `limits.*`, and `persistentvolumeclaims`. Mirrors the
  existing convention in `platform/k8s/resource-quotas.yaml`.
- `LimitRange` with a default request/limit so an unbounded Node process cannot
  consume a node.
- The harness container's `securityContext` mirrors our ScaledJob hardening —
  `allowPrivilegeEscalation: false`, `capabilities: drop: [ALL]`,
  `readOnlyRootFilesystem` where the harness tolerates it
  (`modules/agent-factory/webhook-ingress/infra/scaledjob.tf:256-262`).
- Default-deny **ingress and egress**, mirroring
  `webhook-ingress/infra/scaledjob-netpol.tf:16-31`, with an allow-list of exactly:
  kube-dns (53/UDP+TCP), the sigv4 sidecar (loopback, needs no policy), and the
  Door on 5100 **only if** test 4 is in scope for the run.

### 3.4 Model access — the sigv4 sidecar, as a real sidecar

Per §1's sigv4 row. The experiment pod runs two containers:

| Container | Role | Identity |
|---|---|---|
| `dsh` | the harness, `--profile headless` | no AWS identity, no SA token |
| `sigv4-proxy` | signs and forwards to the gateway via API GW | the only AWS identity in the pod |

The sidecar re-signs with `service: 'execute-api'` using `defaultProvider()` — i.e.
pod IRSA — so **the sidecar container needs an IRSA role with
`execute-api:Invoke`** on the gateway's `/agent/*` path (the shape of
`webhook-ingress/infra/scaledjob-iam.tf:148-155`). This is the one place the
"no IRSA" rule of §3.1 is relaxed, and it is relaxed on the *sidecar*, not on the
harness container.

🔴 **That role must be registered in the DynamoDB agent registry** (§1, agent-registry row).
Without a registry entry the gateway returns 403 `agent_not_registered` and
nothing runs. Register it as a **dedicated experiment agent**, scope
`external` — *not* `internal`, because internal-scoped agents may override
`org_id` via `X-Agent-OrgId` (`auth/middleware.py:415-422`), which is the last
header with real authority and has no business being held by third-party code.
Deregister at teardown (§8).

⚠️ **Verify enforcement is actually live before trusting tests 2 and 3.** If
`token_context` is missing, both middlewares *pass the request through
unenforced* (`budget/enforcement_middleware.py:84-86`). So a "no denial observed"
result is ambiguous between "dsh ignored the cap" and "enforcement never ran."
Test 1 (a `usage_logs` row with the right `agent_run_id`) is the precondition that
disambiguates: run it first, and treat tests 2-3 as `inconclusive` if test 1 did
not confirm.

dsh gets **one** custom-provider row pointing at `http://127.0.0.1:9090` with
`api: anthropic-messages` — targeting `/v1/messages`, which is in
`ENFORCED_PATHS`. Per §Q4 this also sidesteps the pi-ai `developer`-role and
`max_completion_tokens` compat class entirely (feeding test 8).

Provider `headers` must include `X-Agent-RunId: dsh-4188-<run>` so spend is
attributable (`routes.py:141-146`) and so test 1 has something to query. The
gateway reads it lowercase via `request.headers.get("x-agent-runid")` — the name is
`x-agent-runid`, **not** `X-Agent-Run-Id`; a hyphenated spelling silently yields a
null `agent_run_id` rather than an error, which would make test 1 look refuted for
the wrong reason. Note the sidecar also injects `x-agent-runid` itself from
`ADP_MESSAGE_ID` (`sigv4-proxy.ts:85-87`), so set that env var rather than relying
on dsh's provider headers surviving the re-sign.

**No long-lived provider credential enters the pod.** This is the mitigation §Q6
finding 2 calls for, and it means the plaintext-credential finding has nothing
valuable to leak.

### 3.5 agent-context access — and the write problem

§6 invariant 5 is "reads only," and its ⚠️ note says to enforce that structurally
via read-only credentials. Per §1's read-only row, **those do not exist**, and headless mode
*mandates* `approval: 'never'` — every tool call auto-approved. So the structural
guard has to come from what the credential can reach, not from a mode flag:

- Recommended: **run test 4 against a throwaway tenant identity whose
  personal-context store is empty and disposable**, accepting that `remember` /
  `experience save` may write into it, and delete it at teardown. The write
  surface is then real but worthless.
- The ACL half of test 4 (verify a doc outside the tenant is **not** returned) is
  the part with actual decision value, and it is a read.
- If a disposable tenant identity cannot be arranged, **drop test 4 to
  `inconclusive` and say why.** Do not run it against a real tenant's store to
  keep the matrix full. A missing test 4 costs little; a third-party harness with
  auto-approved writes into a real personal-context store is the "most serious
  risk" row in the issue's own impact table.

### 3.6 Telemetry and egress

- Do **not** mount `dsh-llm-deepseek` at all. It is the package that sends the
  per-home UUID as `x-deepseek-harness-user-id` (§Q6 finding 3).
- Set `DSH_TELEMETRY_DISABLED`, knowing upstream states it "does **not** suppress
  direct feedback acknowledgement or the DeepSeek provider header."
- Because of that, the egress allow-list — not the env var — is the control. And
  because NetworkPolicy enforcement is unproven (§1, NetworkPolicy row), §5 requires observing
  blocked egress rather than trusting it.
- Never expose the web profile. Headless "mounts no Host, HTTP server, Web
  runtime, or browser plugin," so there is no listening port to expose — which is
  itself the mitigation for §Q6 finding 1.

### 3.7 Pinned build

- Upstream ref **`dsh-v0.1.1-rc.2`** exactly, recorded as a commit SHA in the
  results record (a tag on a repo averaging 171 commits/day is not a pin).
- Built into our own ECR, scanned by our existing pipeline. **Not** pulled from a
  public registry at pod start.
- `pnpm-lock.yaml` committed as run evidence — 1,550 packages, one patched dep.
- Do-not-mount list: `dsh-llm-deepseek`, anything under `packages/host/`,
  `packages/client/`, `dsh-web-app`, and `subagent-claude-code` (the last for the
  license nuance at `THIRD_PARTY_NOTICES.md:100-102`, not for risk).

## 4. Test 5 — the durability substrate decision

Test 5 is decisive, so the substrate is a design decision, not a runner's
improvisation. Three options; the third is the only one that tests the claim.

| Option | What it would show | Verdict |
|---|---|---|
| **A. `emptyDir`**, pod killed and rescheduled | Nothing survives — but that is true of *any* harness on `emptyDir`. Measures our storage, not dsh. | 🔴 Do not use. Produces a false "refuted". |
| **B. Mountpoint-for-S3 RWX PV** (`s3-files-storage.yaml` pattern) | dsh's SQLite backend and appending JSONL writer both need random writes and locking, which Mountpoint explicitly does not provide (`s3-files-storage.yaml:1-9`). Expect corruption or open failure. | 🔴 Do not use. A crash here is a Mountpoint finding, not a dsh finding. |
| **C. JSONL backend on a node-local volume + sync to S3 at `session/flush` boundaries**, then restore into a fresh pod before start | `session/flush` is documented as "the ordering and error-observation barrier before the loop claims its next turn" (`docs/subsystems/persistence.md:13`) — i.e. a consistent point to copy a whole file. Whole-object writes are what S3 supports. This tests dsh's actual claim: does a log restored from a flush boundary resume without truncation, closing the interrupted turn? | ✅ **Use this.** |

Option C is deliberately the same shape as #4186's Phase 2 (checkpoint to object
storage at turn boundaries, reusing the terminal-transcript upload pattern at
`agent-worker-image/entrypoint.py:397-441`). That is a feature: it means the spike
exercises the substrate #4186 is going to build anyway.

**Recorded for test 5, in this much detail:**
1. The task, and a checkpoint of observable progress before the kill (files
   touched, tools called, turns completed).
2. Exactly what was interrupted — mid-turn or between turns, and whether a
   `session/flush` had completed.
3. Whether the reloaded log contained an open `turn/start` with no `turn/end`, and
   whether repair closed it with a synthetic
   `turn/end { reason: { kind: 'interrupted' } }` as
   `docs/subsystems/persistence.md:15` claims.
4. **Whether prior work was redone** — measured by comparing tool-call sequences
   and token spend before and after, not asserted from the transcript reading
   plausibly.
5. Any manual intervention needed to make resume happen. If the runner had to
   hand-edit the log, that is `inconclusive`, not `confirmed`.

**Inconclusive triggers for test 5:** substrate corruption, the harness failing to
start for unrelated reasons, the kill landing outside a flush boundary so no
checkpoint existed, or any hand-repair of the log.

## 5. Pre-run isolation gates — positive tests, not assertions

These run **before** the harness receives a model credential. **The run is gated
on all five passing.** Per §1's NetworkPolicy row, "the object exists" proves nothing.

| Gate | Method | Pass |
|---|---|---|
| G1 — no platform-secret reach | From a pod in the namespace with the experiment SA, attempt `aws secretsmanager get-secret-value` on a known platform secret and attempt an API-server call | Both fail. No SA token mounted; no IRSA to assume. |
| G2 — telemetry egress actually blocked | Run the harness image with a stub task; observe egress at the node with a request to a controlled external endpoint | The controlled request **does not arrive**. Observed, not configured. If NetworkPolicy is silently unenforced, this gate is what catches it — and if it fails, the run stops until egress is enforced by another means. |
| G3 — no listening port | `ss -ltnp` in the harness container; `kubectl get svc,endpoints -n dsh-experiment` | No listening socket, no Service. (Headless mounts no server; this verifies it.) |
| G4 — throwaway credentials only | Enumerate every env var, mounted secret, and file under `$DSH_HOME` | Nothing of value present. Specifically no `.credentials.yaml` with a real key. |
| G5 — namespace containment | Confirm the pod landed on a gVisor-tainted node; confirm ResourceQuota is bound | `runtimeClassName: gvisor` honoured, quota enforced. |

If G2 cannot be made to pass, **the run does not proceed.** The issue's impact
table rates "outbound telemetry not blocked" as a real harm, and the assessment
records that the env var does not fully suppress egress.

## 6. The eight tests — pass / fail / inconclusive

Verdict vocabulary is exactly `confirmed` / `refuted` / `inconclusive`, per the
issue. "Inconclusive" is a first-class outcome; forcing a binary on a test that
did not really run is how experiments manufacture confidence.

| # | Claim under test | Method | `confirmed` | `refuted` | `inconclusive` |
|---|---|---|---|---|---|
| 1 | Gateway fronting works; spend is attributable. **Run this first — it is the precondition for 2 and 3** (§3.4) | Run a task; query `usage_logs` for `agent_run_id = dsh-4188-<run>` | Rows present, correct run id, non-zero tokens | Requests served but unlogged or unattributed | Task never reached the gateway |
| 2 | Budget enforcement **denies**, not silently serves | Set a deliberately low budget for the experiment subject; exhaust it | Gateway returns **402** and dsh stops. 402 specifically — the middleware uses 402 not 429 because "the AWS SDK auto-retries 429 … which causes the client to appear hung" (`budget/enforcement_middleware.py:193-200`) | Requests keep being served past the cap | Only **503** seen — that is `check_unavailable` (an unreadable ledger), deliberately *not* a cap (`:211-219`). Not a pass. |
| 3 | Rate-limit rejections surface as errors, not hung turns | Drive concurrency past the limit | dsh surfaces the **429** as an error | dsh swallows it into a hung turn | Limit never reached |
| 4 | MCP tools usable; per-caller ACL respected | Point `mcp-client` at `context-mcp.agent-context.svc.cluster.local:5100/mcp/` with a **disposable** tenant identity (§3.5) | ≥2 of the 7 verbs called and results used, **and** a doc outside the tenant is **not** returned | ACL bypassed — cross-tenant content returned | No disposable identity available (§3.5), or the Door unreachable |
| 5 | **Durable resume — decisive.** See §4 | Substrate option C; kill mid-task; restore into a fresh pod | Interrupted turn closed synthetically; run continues; prior work **measurably** not redone | Log truncated, or run restarts from scratch on a genuinely durable substrate | Any of §4's inconclusive triggers |
| 6 | Waste controls fire | Long task; watch for `compaction/*` events and spill locators | Both fire; token-meter output coherent | Neither fires on a task that clearly exceeds the window | Task too short to trigger either |
| 7 | Cache hygiene comparable to our worker | Multi-turn run; compare prompt-cache hit rate against a Claude-SDK worker baseline | Comparable | Materially worse | No comparable baseline captured |
| 8 | Compat overrides needed? (feeds §Q4) | Record whether `compat: { supportsDeveloperRole, maxTokensField }` was required | Records the answer either way | — | Not exercised. Note: §3.4 chooses `anthropic-messages`, which is *expected* to need none — so a "none needed" result says little about the OpenAI path. |

### Additional claim checks (near-zero marginal cost while the environment exists)

| Claim (source) | How to check | Note |
|---|---|---|
| MCP headers are static per process, so one process = one identity (§Q3, `mcp-client/src/index.ts:87-88`) | Attempt to vary identity headers per session without restarting | If **refuted**, that materially weakens the tenant-isolation blocker and should be reported loudly — it is the strongest argument against a hosted dsh |
| Headless can do no HITL at all (§Q2, server→client "dead capability") | With no answerer registered, invoke an approval-requiring tool and `ask_user_question` | Expect `unavailable` → deny, and `NO_PROVIDER` |
| No budget enforcement in dsh (§Q4) | Inspect `ctx.tokenMeter` behaviour at a cap | Expect measurement only. Test 2 is the counterpart: *our* gateway enforces |
| Three security findings (§Q6) | No auth on web server (inspect, do not expose); plaintext `.credentials.yaml`; telemetry egress (= gate G2) | The web-server finding is verified by reading config and confirming G3 — **not** by binding it to a network |

## 7. Spend bounding

#4187 (enforced per-run and per-attempt spend cap) is **open**, so the manual
fallback applies, per the issue's own dependency note:

- A dedicated budget subject for the experiment with a deliberately low cap, so
  the gateway's existing enforcement bounds the spike. Test 2 exercises the same
  mechanism, so the bound and the test are the same control — the experiment
  cannot overspend without failing test 2, which is a tidy property.
- **Mechanically** (per §1's budget-subject row): the admin endpoint cannot create a
  `service_account` budget, so either insert a `budget_configs` row directly
  (`entity_type='service_account'`, `entity_id=<the experiment agent>`,
  `enforcement_mode='hard'`, `budget_amount_usd` at the intended cap — model at
  `shared/models/budget.py:10-21`), or give the experiment its own throwaway
  `user`/`team` subject and cap that via
  `POST /admin/organizations/{org_id}/budgets`. Minimum creatable amount is
  `0.01` (`admin/schemas.py:295-302`).
- Confirm `budget_check_enabled` is not globally disabled
  (`budget/enforcement_service.py:309-310`) before relying on the cap as the bound.
- Actual spend reported in the record, queried from `usage_logs` by
  `agent_run_id`.
- Because there is **no per-run cap**, the period budget is the only bound. Keep
  the period short (daily) so a mistake cannot run for a month.

## 8. Teardown — a verified deliverable, not an intention

| Step | Verification |
|---|---|
| Delete the namespace | `kubectl get all -n dsh-experiment` returns nothing; `kubectl get ns dsh-experiment` reports NotFound |
| Revoke the throwaway credentials | Proven dead by a **failing call**, not by having clicked revoke |
| Deregister the experiment agent from the DynamoDB agent registry, and delete its IRSA role and `budget_configs` row | A signed call from that role now returns 403 `agent_not_registered` |
| Delete the disposable tenant identity + its personal-context entries (§3.5) | Entries absent |
| Scale the gVisor node group back to `desired_size = 0` | No lingering nodes |
| Delete the experiment ECR image + the S3 checkpoint prefix from §4 option C | Absent |
| Confirm nothing shared was touched | No diffs to `adp-agents`, `agent-context`, or `adp-gateway`; no Terraform state changes in any module |

An experiment left running is a failed experiment regardless of its results.

## 9. Sequencing

- **#4186 (durable cross-pod session continuity)** consumes test 5. #4186's
  Phase 3 is the resume branch and Phase 4 is the deadline change; test 5's result
  is only useful if it lands **before Phase 3 freezes**. If the spike cannot be
  gated in time, #4186 should proceed on its own reasoning rather than wait — its
  Phase 1-2 (escape the session id, persist durably) do not depend on this at all.
- **#4187 (spend cap)** — §7. Nice to have, not a blocker.
- **The individual borrows** (fail-closed vocabulary, spill-to-file, context
  economy, seam discipline) are filed separately and **do not depend on this
  spike**. Per the issue's non-goals, they must not be held up waiting for it.

## 10. Non-goals

Unchanged from §6 and the issue: no multi-tenancy, no HITL gates, no web UI, no
replacement of any production worker, no adp-trigger dispatch, no production
deployment of any part of the harness, no permanent environment. And **no
adoption as the worker runtime** — the spike tests the reasoning under that
verdict, it does not reopen it.

## References

- `docs/research/deepseek-harness-fit-assessment.md` §6 (spec), §Q2-Q4, §Q5, §Q6
- `docs/research/dsh-experiment-results.md` (the record this spike produces)
- Live tree: `platform/infra/modules/eks/main.tf:53-58`;
  `platform/infra/gvisor-runtime.tf:23-41`; `platform/infra/gvisor-nodegroup.tf:140-156`;
  `modules/gateway/src/shared/enforced_paths.py:25-26`;
  `modules/gateway/src/proxy/routes.py:141-146`;
  `modules/gateway/src/budget/enforcement_middleware.py:193-200,211-219`;
  `modules/agent-context/door/{auth.py:52,acl.py:37-40,mcp_app.py:143-146,216-343}`;
  `modules/agent-context/manifests/{networkpolicy.yaml:22-27,context-mcp.yaml:108-122,s3-files-storage.yaml:1-9}`;
  `modules/agent-factory/agent-worker-image/entrypoint.py:397-441,1641`;
  `modules/agent-factory/webhook-ingress/infra/{scaledjob.tf:256-262,scaledjob-netpol.tf:16-31}`
- Issues: #4188 (this spike), #4174 (synthesis), #4186, #4187, EPIC #1219, #4160

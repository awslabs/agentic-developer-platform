# dsh gated experiment — results

> **Status: NOT YET RUN.** This is the decision record for the experiment
> specified in `docs/research/deepseek-harness-fit-assessment.md` §6 and designed
> in `docs/spikes/spike-4188-dsh-experiment.md`. Issue #4188.
>
> Every verdict below is `not-yet-run`. **The runner fills them in, including the
> negatives.** A record with a missing or quietly-omitted verdict is a failed
> experiment regardless of what the others say.

## How to fill this in

- Every test gets one of exactly three verdicts: **`confirmed`**, **`refuted`**,
  **`inconclusive`**. No fourth option, and no blanks.
- `inconclusive` is a legitimate, expected outcome. Use it whenever the test did
  not really run. Forcing a binary on a test that did not execute is how an
  experiment manufactures false confidence.
- State the **method actually used**, not the method planned. If you deviated from
  the spike design, say so and why.
- **Report negatives at the same volume as positives.** The stated purpose of this
  experiment is that it can come back negative.
- Be blunt about anything the assessment got wrong. That is a deliverable, not a
  criticism of the assessment.

## 1. Environment as actually run

| Item | Planned (spike §3) | As run |
|---|---|---|
| Namespace | `dsh-experiment` | _not-yet-run_ |
| Upstream ref / commit SHA | `dsh-v0.1.1-rc.2` @ SHA | _not-yet-run_ |
| Profile / bundles | `headless`; `dsh-llm-deepseek` NOT mounted | _not-yet-run_ |
| Node isolation | `runtimeClassName: gvisor` | _not-yet-run_ |
| ServiceAccount / IRSA | harness SA: no IRSA, no SA token; sidecar: `execute-api:Invoke` only | _not-yet-run_ |
| Agent-registry entry | dedicated experiment agent, scope `external` | _not-yet-run_ |
| Model path | sigv4 sidecar → API GW → gateway `/v1/messages` | _not-yet-run_ |
| agent-context access | disposable tenant identity, or test 4 dropped | _not-yet-run_ |
| Test-5 substrate | JSONL + S3 sync at `session/flush` (spike §4 option C) | _not-yet-run_ |
| Spend bound | `budget_configs` row, `hard`, daily period | _not-yet-run_ |
| Window (start → teardown) | — | _not-yet-run_ |

## 2. Pre-run isolation gates

**The run is gated on all five passing** (spike §5). These are positive traffic
tests, not object-existence checks.

| Gate | Result | Evidence |
|---|---|---|
| G1 — no platform-secret or API-server reach | _not-yet-run_ | |
| G2 — telemetry egress observed blocked | _not-yet-run_ | |
| G3 — no listening port | _not-yet-run_ | |
| G4 — throwaway credentials only | _not-yet-run_ | |
| G5 — gVisor node + quota bound | _not-yet-run_ | |

If G2 did not pass, the run should not have proceeded. Record what happened.

## 3. The eight tests

### Test 1 — Gateway fronting and spend attribution
- **Claim:** requests appear in `usage_logs` with the correct `agent_run_id`, token counts non-zero.
- **Method:** _not-yet-run_
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`
- **Note:** this is the precondition for tests 2 and 3. If it did not confirm, those two are `inconclusive` — a missing `token_context` makes both middlewares pass traffic through unenforced (`budget/enforcement_middleware.py:84-86`), so "no denial" would not mean "dsh ignored the cap."

### Test 2 — Budget enforcement denies rather than silently serving
- **Claim:** with budget exhausted, dsh requests are denied by the middleware.
- **Method:** _not-yet-run_
- **Observed:** _not-yet-run_ (record the exact HTTP status)
- **Verdict:** `not-yet-run`
- **Note:** a pass is **402**, not 429 (`budget/enforcement_middleware.py:193-200`). A **503** is `check_unavailable` — an unreadable ledger, deliberately not a cap (`:211-219`) — and is **not** a pass.

### Test 3 — Rate-limit rejections surface as errors, not hung turns
- **Claim:** dsh surfaces 429 as an error rather than swallowing it into a hung turn.
- **Method:** _not-yet-run_
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`

### Test 4 — MCP tools usable; per-caller ACL respected
- **Claim:** dsh calls ≥2 agent-context verbs and uses the results; a document outside the tenant is **not** returned.
- **Method:** _not-yet-run_
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`
- **Note:** the Door exposes **7** verbs, and 2 of them write (`remember`, `experience action=save`). There is no read-only credential. If no disposable tenant identity was available, this test should be `inconclusive` — not run against a real tenant's store.

### Test 5 — Durable resume — **DECISIVE**

Reported in more detail than the others, per the issue: a reader must be able to
judge it independently.

- **Claim:** kill the pod mid-task; a fresh pod resumes from the persisted log with the interrupted turn closed, and continues without redoing prior work.
- **Substrate actually used:** _not-yet-run_ — and state whether it was genuinely durable across pod replacement. This determines whether a `refuted` verdict says anything about dsh at all (spike §4).
- **The task, and progress observed before the kill** (files touched, tools called, turns completed): _not-yet-run_
- **What exactly was interrupted** — mid-turn or between turns; had a `session/flush` completed? _not-yet-run_
- **What was expected to survive:** _not-yet-run_
- **What actually survived:** _not-yet-run_
- **Did the reloaded log contain an open `turn/start` with no `turn/end`, and did repair close it with a synthetic `turn/end { reason: { kind: 'interrupted' } }`** as `docs/subsystems/persistence.md:15` claims? _not-yet-run_
- **Was prior work redone?** Measured how — compare tool-call sequences and token spend before/after. Do not assert this from reading the transcript. _not-yet-run_
- **Any manual intervention?** If the log had to be hand-repaired, the verdict is `inconclusive`, not `confirmed`. _not-yet-run_
- **Verdict:** `not-yet-run`
- **What this licenses / does not license:** fill in against the table in spike §2.

### Test 6 — Waste controls fire
- **Claim:** compaction and spill fire on a long task; token-meter output is coherent.
- **Method:** _not-yet-run_
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`

### Test 7 — Cache hygiene
- **Claim:** prompt-cache hit rate on a multi-turn run is comparable to our Claude-SDK worker.
- **Method / baseline used:** _not-yet-run_
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`

### Test 8 — Compat overrides
- **Claim:** whether `compat: { supportsDeveloperRole, maxTokensField }` overrides were needed (feeds back into §Q4).
- **Observed:** _not-yet-run_
- **Verdict:** `not-yet-run`
- **Note:** the spike targets `anthropic-messages`, which is *expected* to need none. A "none needed" result therefore says little about the OpenAI-shaped path; say so rather than generalizing.

## 4. Additional claim checks

| Claim (assessment §) | Method | Observed | Verdict |
|---|---|---|---|
| MCP headers static per process → one process = one identity (§Q3) | | _not-yet-run_ | `not-yet-run` |
| Headless profile cannot do HITL at all (§Q2) | | _not-yet-run_ | `not-yet-run` |
| No budget enforcement in dsh — measures but does not deny (§Q4) | | _not-yet-run_ | `not-yet-run` |
| Security: no auth on web server (§Q6 #1) | | _not-yet-run_ | `not-yet-run` |
| Security: plaintext credentials on disk (§Q6 #2) | | _not-yet-run_ | `not-yet-run` |
| Security: outbound telemetry / identity egress (§Q6 #3) | = gate G2 | _not-yet-run_ | `not-yet-run` |

If the static-header claim comes back **refuted**, report it prominently — it is
the strongest single argument against a hosted multi-tenant dsh, and refuting it
would materially change §Q3 and the risk table.

## 5. Is the assessment's verdict upheld?

- **Verdict under test:** ADAPT — borrow the architecture, do not adopt as the worker runtime (§Q5 Option C).
- **Upheld / requires revision:** _not-yet-run_
- **What the assessment got wrong:** _not-yet-run_ — state plainly. The spike design already found six spec-level divergences before the run (spike §1); add anything the run itself contradicts.
- **Corrections to make to `deepseek-harness-fit-assessment.md`:** _not-yet-run_

Note that upholding the verdict is the expected outcome and is a success. The
verdict does not depend on the experiment; the experiment tests the reasoning
underneath it.

## 6. Spend

| Item | Value |
|---|---|
| Cap configured | _not-yet-run_ |
| Actual spend (from `usage_logs` by `agent_run_id`) | _not-yet-run_ |
| Compute (node-hours) | _not-yet-run_ |

## 7. Teardown confirmation

| Step | Verified | Evidence |
|---|---|---|
| `kubectl get all -n dsh-experiment` returns nothing | _not-yet-run_ | |
| Namespace absent | _not-yet-run_ | |
| Throwaway credentials proven dead by a failing call | _not-yet-run_ | |
| Experiment agent deregistered; IRSA role + `budget_configs` row deleted | _not-yet-run_ | |
| Disposable tenant identity + personal-context entries deleted | _not-yet-run_ | |
| gVisor node group back to `desired_size = 0` | _not-yet-run_ | |
| ECR image + S3 checkpoint prefix deleted | _not-yet-run_ | |
| No shared cluster resource modified; no platform namespace touched | _not-yet-run_ | |

An experiment left running is a failed experiment regardless of its results.

## 8. Consequences for in-flight work

- **#4186 (durable cross-pod session continuity)** — what test 5 changes, if anything, about its Phase 3 resume-branch design: _not-yet-run_
- **#4187 (per-run spend cap)** — what §7 above suggests about the need for it: _not-yet-run_
- **The separately-filed borrows** — unaffected by design; they do not depend on this experiment.

## References

- `docs/spikes/spike-4188-dsh-experiment.md` — the run design
- `docs/research/deepseek-harness-fit-assessment.md` §6, §Q2-Q4, §Q5, §Q6
- Issues: #4188, #4174, #4186, #4187, #4160, EPIC #1219

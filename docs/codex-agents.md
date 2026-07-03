# Codex Agents — User & Operator Guide

How to use, monitor, and roll back the two OpenAI **Codex** surfaces in the
hosted-agent platform. Codex is a *second* coding model (`openai.gpt-5.5` on
Amazon Bedrock) exposed **without a second runtime** — the Claude Agent SDK
worker stays the outer loop and drives Codex as a CLI tool.

Part of EPIC #2702. Model-path details (CLI version, model ID, endpoint, auth)
are settled in the spike note
[`docs/agent-context/design-notes/2703-codex-bedrock-model-path-spike.md`](agent-context/design-notes/2703-codex-bedrock-model-path-spike.md)
— this guide cites it rather than restating it.

---

## 1. The two surfaces at a glance

| Surface | How you trigger it | Who writes the code | When to reach for it |
|---|---|---|---|
| **codex-bridge skill** | Any persona run where the triggering issue/comment **explicitly says "Codex"** (e.g. "use Codex to…", "ask Codex to review this") | The supervising Claude agent delegates one bounded task to Codex, then reviews the output before committing | You want a cross-model second opinion or a single bounded piece written by Codex, inside an otherwise normal Claude run |
| **`codex` supervisor persona** | Mention **`@agent-codex`** on an issue | Codex writes the implementation; the Claude worker supervises — decomposes, delegates each task, reviews every diff, and finalizes the PR | You want the whole issue implemented by Codex, with Claude as reviewer/finisher |

Both run inside the **same per-run pod and workspace** as any other persona.
All existing plumbing (issue→branch→PR, check runs, progress comments, retries,
telemetry) is unchanged.

---

## 2. Triggering the codex-bridge skill

The skill is **human-gated**: it fires **only** when the text that summoned the
run explicitly names Codex. Concrete triggers the agent looks for:

- "use Codex to …"
- "ask Codex to review this diff / file / PR"
- "pass this to Codex" / "have Codex write / implement …"
- "get a second opinion from Codex"

If no such phrase is present, the agent does the work itself and **does not**
run the skill. This gate is a hard requirement — it prevents unexpected
Bedrock spend and code authored by a model the user did not choose.

**Example — explicit request in a comment:**

```
@agent-developer please implement the retry helper.
Use Codex to write the exponential-backoff function.
```

The developer agent runs normally and delegates just that one function to Codex
via the codex-bridge skill (write mode), reviews the diff, and commits.

The skill wrapper is `run-codex.sh` (staged into `.claude/skills/codex-bridge/`
from `modules/agent-factory/skills/codex-bridge/` by the existing
`stage-personas.sh` / `entrypoint.py` machinery). It closes stdin, enforces a
hard timeout, resets AWS creds to the pod's IRSA defaults, and passes the
instruction as a single argv.

## 3. Triggering the `codex` supervisor persona

Mention **`@agent-codex`** on an issue:

```
@agent-codex implement the acceptance criteria in this issue.
```

The Claude worker loads the `codex` persona (`modules/agent-factory/rules/personas/codex.md`),
decomposes the issue into bounded tasks, delegates each to Codex via
codex-bridge, reviews every diff, and finalizes the PR. **Being invoked as
`@agent-codex` satisfies the codex-bridge gate for that run** — no separate
"use Codex" phrase is needed.

The persona is **mention-triggered only** — it is intentionally absent from the
label map, and is the **last** entry in `MENTION_TO_PERSONA`
(`modules/agent-factory/webhook-ingress/lambda/common/personas.py`) so it can
never shadow another persona under first-match routing.

---

## 4. The gating contract (the EPIC acceptance gate)

This is the contract EPIC #2702 is closed against: **the same coding task, with
the word "Codex" removed, must NOT invoke the skill.** Two independent gates
enforce it:

1. **Skill-description gate (codex-bridge).** The skill's `SKILL.md` instructs
   the agent to run it *only* when the triggering text explicitly names Codex,
   and to do the work itself otherwise. A developer-agent run whose task text
   never mentions Codex therefore completes with Claude's own tools and the
   skill stays dormant.
2. **Mention-routing gate (persona).** The `codex` persona is reachable **only**
   through the literal `@agent-codex` mention in `_extract_mention_persona()`
   (substring match against `MENTION_TO_PERSONA`). A comment that does not
   contain that exact token never routes to the Codex supervisor.

**Matrix mapping** (see §7 for the executable matrix): the negative test —
identical task, no "Codex" keyword — exercises gate #1. Because the gate is a
deterministic property of the skill description + the routing dict (not a
model-judgment call), it holds regardless of task phrasing.

---

## 5. Model backing

- **Model:** `openai.gpt-5.5`, served from the Bedrock **`bedrock-mantle`**
  endpoint (`us-east-1`, Responses API only).
- **CLI:** `@openai/codex@0.142.5`, using its built-in `amazon-bedrock`
  provider (do **not** override `base_url`/`wire_api`).
- **Auth:** the pod's IRSA / ScaledJob-role SigV4 credential chain — **zero
  secrets**. `AWS_BEARER_TOKEN_BEDROCK` remains a break-glass fallback only.

Full derivation, the working `config.toml`, and the headless transcript are in
the spike note (link above). Do not duplicate those values here — the spike note
is the single source of truth.

---

## 6. Known limitations (v1)

- **No gateway metering.** Codex invokes Bedrock **directly** via the pod's IRSA
  identity, so its token usage bypasses the gateway's per-tenant budget /
  rate-limit / cost-attribution path that Claude traffic goes through (see
  [`docs/runbook-bedrock-routing.md`](runbook-bedrock-routing.md)). Codex spend
  is bounded per-run by the supervisor's task delegation but is **not** metered
  in v1. Gateway passthrough for Codex is tracked separately (#2709) and is an
  explicit follow-up, not a v1 requirement.

- **IAM — scoped-policy sufficiency (carried over from spike #2703 §6, now
  closed).** The spike's one open item was whether the *scoped*
  `agent-worker-scoped-permissions` policy — not the role's `AdministratorAccess`
  attachment — authorizes the mantle path. Evidence gathered from inside the
  agent-worker pod (identity
  `arn:aws:sts::879318057152:assumed-role/adp-dev-agent-scaledjob-role/…`):

  - **Live invoke succeeded.** `run-codex.sh write "…"` returned **exit 0** with
    `model: openai.gpt-5.5`, `provider: amazon-bedrock`, `approval: never`,
    authenticating via the pod IRSA chain with no bearer token — a real
    end-to-end Codex→Bedrock call from the ScaledJob role.
  - **Scoped-only policy simulation confirms it.** An `iam simulate-custom-policy`
    run against **only** the `BedrockModelInvoke` statement (the simulator
    ignores the `AdministratorAccess` attachment) returns **`allowed`** for
    `bedrock:InvokeModel` on
    `arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-5.5`, and
    `implicitDeny` for an unscoped service-level ARN — matching spike prediction
    #1 (mantle authorizes against `foundation-model/*`).

  **Conclusion:** the scoped policy is sufficient for the mantle path; **no IAM
  change is required** to run Codex under the scoped role. Narrowing /
  removing the `AdministratorAccess` attachment is owned by **#2713** — this
  evidence de-risks that work.

- **Startup plugin-cache 401 is benign.** Codex makes a best-effort call to
  `chatgpt.com/backend-api/plugins/featured` on startup; with no ChatGPT auth it
  logs a `401` and continues. Ensure pod egress lets that host fail fast rather
  than hang (spike §5.3).

---

## 7. Validation matrix (run against dev)

Operators/ops-flow runs execute these on **scratch issues** and post run-log /
PR links as evidence on EPIC #2702. Each row is one dev agent run.

| # | Test | Trigger | Expected result |
|---|---|---|---|
| 1 | **Skill positive** | `@agent-developer` mention + "use Codex …" on a small coding task | codex-bridge fires; Codex writes the change; agent reviews; **PR opened** |
| 2 | **Skill negative (gating — EPIC gate)** | Same task, **no** "Codex" keyword | codex-bridge does **NOT** fire; agent completes with its own tools |
| 3 | **Persona happy path** | `@agent-codex` on an issue with acceptance criteria | Supervisor delegates → reviews → **PR opened** |
| 4 | **Persona fallback** | `@agent-codex` with an induced Codex failure (e.g. force a non-zero wrapper exit) | Supervisor notes the failure and **completes the task itself**; PR still opened |
| 5 | **Regression** | Plain `@agent-developer` run, no Codex anywhere | Behaves exactly as before; skill dormant |

**Deterministic pre-check (no scratch run needed):** matrix #2's guarantee is a
property of the skill-description gate + the mention-routing dict (§4), not a
model judgment — a task without "Codex" cannot reach either surface. This was
confirmed by inspection alongside a live positive invoke of the skill wrapper
from the agent-worker pod (exit 0, `openai.gpt-5.5`), which exercises the
matrix-#1 path end-to-end.

---

## 8. Monitoring a run

- **Progress comments** on the issue show the persona's plan and status, exactly
  as for other personas.
- The `codex` persona's PR description states explicitly **which parts Codex
  authored vs. which the supervisor wrote** (including any diffs it rejected or
  tasks it finished itself after a Codex failure).
- Codex CLI streams its session to the run log (session id, model, provider,
  token count per invocation). A **non-zero wrapper exit** is the reliable
  fail signal — the supervisor surfaces it and falls back to finishing the task.

---

## 9. Rollback

Codex is additive and opt-in; disabling it does not touch other personas.

| Scope | Action | Effect | Time |
|---|---|---|---|
| **Disable the supervisor persona** | Remove the `"@agent-codex": "codex"` entry from `MENTION_TO_PERSONA` (`modules/agent-factory/webhook-ingress/lambda/common/personas.py`) and redeploy the webhook Lambda | `@agent-codex` mentions no longer route anywhere; skill unaffected | minutes |
| **Disable the codex-bridge skill** | Remove/stop staging `modules/agent-factory/skills/codex-bridge/` (drop it from the staged skill set); rebuild `adp-agent-runtime` | No persona can invoke Codex; runs fall back to Claude-only | one image rebuild |
| **Full revert** | Revert the child-story PRs (#2704 image, #2705 skill, #2706 persona) and rebuild the agent-worker image | Platform returns to Claude-only; no infra/Terraform/KEDA changes to undo | one image rebuild |

There is **no infrastructure to tear down** — no new AWS resources, no Terraform
state, no Secrets Manager entries were added for the v1 Codex path (auth is IRSA
SigV4). Rollback is code-only.

---

## References

- Spike note (model path, CLI, auth): [`docs/agent-context/design-notes/2703-codex-bedrock-model-path-spike.md`](agent-context/design-notes/2703-codex-bedrock-model-path-spike.md)
- Bedrock routing / gateway metering context: [`docs/runbook-bedrock-routing.md`](runbook-bedrock-routing.md)
- codex-bridge skill: `modules/agent-factory/skills/codex-bridge/SKILL.md`
- codex supervisor persona: `modules/agent-factory/rules/personas/codex.md`
- EPIC #2702; child stories #2703 (spike), #2704 (image), #2705 (skill), #2706 (persona); follow-ups #2709 (gateway passthrough), #2713 (IAM narrowing)

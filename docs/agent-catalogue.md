# ADP Agent Catalogue

**This is the authoritative list of every agent persona ADP ships.** If a persona is
not in the table below, it does not exist. If a trigger string is not in the table
below, it does not dispatch anything.

> **Source of truth:** [`modules/agent-factory/webhook-ingress/lambda/common/personas.py`](../modules/agent-factory/webhook-ingress/lambda/common/personas.py).
> That file's `MENTION_TO_PERSONA`, `LABEL_TO_PERSONA`, and `AUTOMATIC_PERSONAS`
> collections are what the platform actually routes on. Mention strings in this
> document are kept in parity with it by an automated test
> ([`lambda/common/tests/test_persona_catalogue_parity.py`](../modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_catalogue_parity.py)),
> which fails CI if a mention string here does not exist in the code or vice versa.
> Edit the code first, then this table.

## How to summon an agent

Comment the agent's **mention string** on a GitHub issue or PR. A pod spins up, does
the work, and reports back on the issue. Some personas also respond automatically to
the events documented in the table. Use labels only when you specifically want a
deterministic GitHub Actions pipeline (see [Label triggers](#label-triggers) below).

```
@agent-developer please implement this issue.
```

## The 13 personas

| Persona | Mention string (exact) | Webhook label | ARC label | What it does | Typical use |
|---|---|---|---|---|---|
| `developer` | `@agent-developer` | `developer` | `agent-developer` | Writes production code + unit tests, opens the PR. Matches existing codebase patterns; ships working software over perfect software. | "Implement this issue." The default for any code change. |
| `pm` | `@agent-pm` | `pm` | `agent-pm` | Orchestrates the AIDLC workflow, decomposes work, manages the board, unblocks other agents. | Multi-issue coordination, EPIC decomposition, status roll-ups. |
| `operations` | `@agent-operations` | `agent-operations` | `agent-operations` | Deploys, monitors, and maintains infrastructure. Reliability-, cost-, and security-focused; everything idempotent and reversible. | Deployments, Terraform applies, cluster/infra debugging. |
| `reviewer` | `@agent-reviewer` | `agent-reviewer` | `agent-reviewer` | The quality gate: reviews for correctness, security, and maintainability. Blocks on real issues, suggests on style. | PR review. Auto-triggered for `agent/issue-*` branches. |
| `agent-codex-reviewer` | `@agent-codex-reviewer` | *(none)* | *(none)* | Codex SDK quality gate packaged in the shared `adp-agent-runtime` image and selected from the standard envelope's persona field. Reviews issues and PRs; on eligible PRs it can apply bounded mechanical fixes, wait for checks, and merge. | Mention it for an issue review, or let it automatically review ready pull requests from `agent/issue-*` branches through the shared `agent-submit.fifo` queue. |
| `architect` | `@agent-architect` | `agent-architect` | `agent-architect` | Designs systems, defines interfaces, makes technology decisions — always validated against the live codebase, not assumptions. | Design review of an issue before implementation; design docs. |
| `product` | `@agent-product` | *(none)* | `agent-product` | Gathers requirements, writes user stories and acceptance criteria. Represents the user's perspective. | Turning a vague ask into a testable, prioritized spec. |
| `malware-analysis-agent` | `@agent-malware-analysis-agent` | `malware-analysis-agent` | `malware-analysis-agent` | Runs a structured 7-stage malware analysis pipeline on a sample (S3 pointer + issue), posting one comment per stage. | Cyber domain pack: triage → OSINT → static → dynamic → verdict → report. |
| `pt-superpower` | `@agent-superpower` | `superpower` | `agent-pt-superpower` | Pen test / security review. **See the constraint below — this persona has no prompt file and currently dispatches without a persona identity ([#4037](https://github.com/aws-e/adp/issues/4037)).** | Security review (⚠️ known-broken, see constraints). |
| `superplane-operator` | `@agent-superplane-operator` | *(none, deliberately)* | *(none)* | SRE/DevOps for Superplane multi-cloud GPU infrastructure: cluster health, incident response, GPU utilization, cost right-sizing. Runs a structured alert-driven investigation (acknowledge → metrics → root cause → impact → remediation → confidence). | Superplane domain pack: alerts, incidents, idle/over-provisioned GPU capacity. |
| `superplane-researcher` | `@agent-superplane-researcher` | *(none, deliberately)* | *(none)* | Development-side counterpart: model deployment, GPU sizing and cost estimation, CI/CD, deployment strategies, workspace and dev-environment setup. Estimates before provisioning. | Superplane domain pack: "what will this run on, what will it cost, how do I deploy it?" |
| `aidlc` | `@agent-aidlc` | *(none — see constraints)* | *(none)* | Runs the AIDLC inception workflow: problem framing, scope, design options, risks, acceptance criteria. Never enters Construction. | Producing structured inception artifacts from a raw intent. |
| `codex` | `@agent-codex` | *(none, deliberately)* | *(none)* | Supervisor that delegates bounded implementation tasks to the OpenAI Codex CLI via the `codex-bridge` skill, reviews every diff, and owns the final PR. | Only when a human explicitly wants Codex to do the work. |

**Note on the two label columns:** the webhook label and the ARC label are *different
names for different dispatch systems* and they do not agree. See
[Label triggers](#label-triggers).

## Routing rules

These are properties of the dispatch code, not conventions — they change what actually
happens when you type a mention.

- **First complete token wins, in dict order.** `_extract_mention_persona()`
  ([`intent_parser.py`](../modules/agent-factory/webhook-ingress/lambda/github/intent_parser.py))
  returns on the first complete mention token it finds, scanning
  `MENTION_TO_PERSONA` in declaration order. Token-boundary matching keeps the older
  `@agent-codex` supervisor distinct from `@agent-codex-reviewer`.
- **There is no fan-out.** Mentioning two agents in one comment dispatches **one**
  agent — the first match — not both. (`_extract_all_mention_personas()` exists but has
  zero callers.) To summon two agents, post two comments.
- **Mentions in bot comments are not triggers.** A bare `@agent-X` from a bot sender is
  logged and dropped (`BotMentionWithoutDispatchMarker`); bot-authored dispatch requires
  an explicit `adp-dispatch:<persona>` marker. Agents dispatch each other with
  `adp-trigger`, not by posting mentions.
- **Mention strings are matched as complete tokens**, including inside a code block or
  quoted line. Avoid pasting trigger strings into issue bodies you don't want to
  dispatch.

## Label triggers

Two unrelated systems dispatch agents from labels, and **their label names conflict** —
which is why the table above has two columns:

| System | Label style | Path |
|---|---|---|
| Webhook ingress (primary) | bare, e.g. `developer`, `pm`, `superpower` | GitHub webhook → Lambda → SQS → KEDA → agent-worker pod |
| ARC / GitHub Actions | `agent-` prefixed, e.g. `agent-developer`, `agent-pt-superpower` | `issues.labeled` → `.github/workflows/agent-<persona>.yml` on self-hosted runners |

Use the **mention** path for open-ended agent work. Use the ARC **label** path when you
want a deterministic, auditable, repeatable Actions pipeline
(see [`modules/agent-factory/SETUP-GUIDE.md`](../modules/agent-factory/SETUP-GUIDE.md)).

`skill-agent` has an ARC workflow label but is **not** a persona — it is absent from
`VALID_PERSONAS` and has no mention string.

## Per-persona constraints

- **`agent-codex-reviewer`** — mention-triggered for issue review and also automatic for
  eligible ready PR events. Both paths select `agent-codex-reviewer` in the existing
  envelope, and the shared worker routes that persona to the packaged Codex adapter.
- **`aidlc`** — mention-triggered only; no `LABEL_TO_PERSONA` entry. It has a second
  trigger path: `issues.opened` carrying the `aidlc-intent` label (the label *is* the
  authorization). Its workflow is gated: every stage ends at a human approval gate with
  no auto-advance, and a re-mention that contains no gate answer (`approve`,
  `feedback:`, or `skip`) re-posts the open gate rather than advancing it.
- **`codex`** — mention-triggered only, intentionally not label-triggerable. Runs on the
  same Claude SDK worker as every other persona but delegates to the Codex CLI, which is
  human-gated: it may only run when a human explicitly asked for Codex.
- **`malware-analysis-agent`** — lives in the cyber domain pack
  (`modules/domain-apps/cyber/agent/personas/`), not in `rules/personas/`; it is staged
  into the worker image by `stage-personas.sh`. Expects a sample reference (`s3://...`).
- **`pt-superpower`** — ⚠️ **known broken —
  [#4037](https://github.com/aws-e/adp/issues/4037).** No persona prompt file exists for
  it anywhere in the repo, so a dispatch succeeds and runs a pod with **no persona
  identity** — it looks like a success in the Activity feed but produces a generic
  agent. Do not rely on this persona until #4037 lands.
- **`product`** — has an ARC label but no webhook label; summon it by mention.
- **`superplane-operator`**, **`superplane-researcher`** — live in the Superplane domain
  pack (`modules/domain-apps/superplane/agent/personas/`), not in `rules/personas/`;
  staged into the worker image by `stage-personas.sh` like the cyber pack's personas.
  Both are **mention-triggered only, deliberately** — `superplane-operator` can allocate
  paid compute, and a label is a weaker trigger than a mention (a stale label on a
  reopened issue re-dispatches), so the trigger surface is restricted for the same reason
  as `codex`. Their filenames are namespaced (`superplane-operator.md`, not
  `operations.md`) because domain personas stage **flat** into `/app/personas/` and
  override core ones of the same name — an un-namespaced `developer.md` here would
  silently replace ADP's core `developer` persona for every agent run.

# ADP Agent Catalogue

This catalogue lists the GitHub agent personas and their exact triggers. Registration
identifies a routing target; execution also requires the matching deployed runtime,
persona enablement and a compatible model. Task API profiles use a separate registry,
linked in [Codex capabilities and verification](#codex-capabilities-and-verification).

> **Source of truth:** [`modules/agent-factory/webhook-ingress/lambda/common/personas.py`](../modules/agent-factory/webhook-ingress/lambda/common/personas.py).
> That file's `MENTION_TO_PERSONA`, `LABEL_TO_PERSONA`, and `AUTOMATIC_PERSONAS`
> collections are what the platform actually routes on. Mention strings in this
> document are kept in parity with it by an automated test
> ([`lambda/common/tests/test_persona_catalogue_parity.py`](../modules/agent-factory/webhook-ingress/lambda/common/tests/test_persona_catalogue_parity.py)),
> which fails CI if a mention string here does not exist in the code or vice versa.
> When adding a trigger, edit the code first, then this table.

## How to summon an agent

Comment the agent's **mention string** on a GitHub issue or PR. A pod spins up, does
the work, and reports back on the issue. Some personas also respond automatically to
the events documented in the table. Use labels only when you specifically want a
deterministic GitHub Actions pipeline (see [Label triggers](#label-triggers) below).

```
@agent-developer please implement this issue.
```

## GitHub personas (18 mention-triggered, 1 automatic-only)

| Persona | Mention string (exact) | Webhook label | ARC label | What it does | Typical use |
|---|---|---|---|---|---|
| `developer` | `@agent-developer` | `developer` | `agent-developer` | Writes production code + unit tests, opens the PR. Matches existing codebase patterns; ships working software over perfect software. | "Implement this issue." The default for any code change. |
| `pm` | `@agent-pm` | `pm` | `agent-pm` | Orchestrates the AIDLC workflow, decomposes work, manages the board, unblocks other agents. | Multi-issue coordination, EPIC decomposition, status roll-ups. |
| `operations` | `@agent-operations` | `agent-operations` | `agent-operations` | Deploys, monitors, and maintains infrastructure. Reliability-, cost-, and security-focused; everything idempotent and reversible. | Deployments, Terraform applies, cluster/infra debugging. |
| `reviewer` | `@agent-reviewer` | `agent-reviewer` | `agent-reviewer` | The quality gate: reviews for correctness, security, and maintainability. Blocks on real issues, suggests on style. | PR review by mention or label; eligible automatic PR events select the Codex reviewer. |
| `agent-codex-developer` | `@agent-codex-developer` | *(none)* | *(none)* | Native Codex SDK developer: reads the issue, implements changes, runs tests, commits, pushes and opens a ready PR. | Implement an issue using Codex. Publishes progress, command activity and a final transcript. |
| `agent-codex-reviewer` | `@agent-codex-reviewer` | *(none)* | *(none)* | Reviews, fixes, tests and merges PRs, including resolving merge conflicts within scope. Respects existing repository merge requirements. Issue-only reviews are read-only. | PR review by mention or eligible automatic PR events; also used by engine review assignments. No separate developer handoff is required. |
| `agent-codex-architect` | `@agent-codex-architect` | *(none)* | *(none)* | Produces structured designs and implementation stories. On GitHub, can publish requested child stories and their dependency links. | Design an issue or decompose it into linked implementation stories. |
| `agent-codex-product` | `@agent-codex-product` | *(none)* | *(none)* | Produces requirements, user stories and acceptance criteria, preserving source requirements and explicit amendments. | Turn a vague request into a testable specification; Task API supports clarification and continuation. |
| `agent-codex-pm` | `@agent-codex-pm` | *(none)* | *(none)* | Plans work and schedules eligible GitHub issues, respecting blockers and avoiding duplicate dispatch. | Dispatch ready work and leave dependent work blocked until its prerequisites are met. |
| `agent-codex-intent-refinement` | `@agent-codex-intent-refinement` | *(none)* | *(none)* | Refines an intent into a structured draft with requirements, assumptions and unresolved questions. | Clarify scope before implementation; Task API supports clarification and continuation. |
| `architect` | `@agent-architect` | `agent-architect` | `agent-architect` | Designs systems, defines interfaces, makes technology decisions — always validated against the live codebase, not assumptions. | Design review of an issue before implementation; design docs. |
| `product` | `@agent-product` | *(none)* | `agent-product` | Gathers requirements, writes user stories and acceptance criteria. Represents the user's perspective. | Turning a vague ask into a testable, prioritized spec. |
| `malware-analysis-agent` | `@agent-malware-analysis-agent` | `malware-analysis-agent` | `malware-analysis-agent` | Runs a structured 7-stage malware analysis pipeline on a sample (S3 pointer + issue), posting one comment per stage. | Cyber domain pack: triage → OSINT → static → dynamic → verdict → report. |
| `pt-superpower` | `@agent-superpower` | `superpower` | `agent-pt-superpower` | Pen test / security review. **See the constraint below — this persona has no prompt file and currently dispatches without a persona identity ([#4037](https://github.com/aws-e/adp/issues/4037)).** | Security review (⚠️ known-broken, see constraints). |
| `superplane-operator` | `@agent-superplane-operator` | *(none, deliberately)* | *(none)* | SRE/DevOps for Superplane multi-cloud GPU infrastructure: cluster health, incident response, GPU utilization, cost right-sizing. Runs a structured alert-driven investigation (acknowledge → metrics → root cause → impact → remediation → confidence). | Superplane domain pack: alerts, incidents, idle/over-provisioned GPU capacity. |
| `superplane-researcher` | `@agent-superplane-researcher` | *(none, deliberately)* | *(none)* | Development-side counterpart: model deployment, GPU sizing and cost estimation, CI/CD, deployment strategies, workspace and dev-environment setup. Estimates before provisioning. | Superplane domain pack: "what will this run on, what will it cost, how do I deploy it?" |
| `aidlc` | `@agent-aidlc` | *(none — see constraints)* | *(none)* | Runs the AIDLC inception workflow: problem framing, scope, design options, risks, acceptance criteria. Never enters Construction. | Producing structured inception artifacts from a raw intent. |
| `codex` | `@agent-codex` | *(none, deliberately)* | *(none)* | Supervisor that delegates bounded implementation tasks to the OpenAI Codex CLI via the `codex-bridge` skill, reviews every diff, and owns the final PR. | Only when a human explicitly wants Codex to do the work. |

The automatic-only `intent-refinement` persona is also registered. It has no
mention or standard label mapping; the platform selects it for intent refinement.
It is distinct from the explicitly mentionable Codex Intent Refinement persona.

**Note on the two label columns:** the webhook label and the ARC label are *different
names for different dispatch systems* and they do not agree. See
[Label triggers](#label-triggers).

## Codex capabilities and verification

The six `agent-codex-*` personas use native Codex SDK adapters in the shared
`adp-agent-runtime` worker. The older `codex` supervisor remains a separate
Claude SDK persona that delegates to the Codex CLI.

Use the exact mention from the table. An explicit compatible model can be selected
on a separate line, for example:

```text
@agent-codex-developer
/model gpt6-sol

Implement this issue and run the relevant tests.
```

Developer opens ready PRs after its checks; the current framework instruction does
not support requesting a draft PR. Reviewer owns **review → fix → test → merge**
for PR invocations, stopping for genuine blockers or existing repository rules.
See the [native adapter guide](../modules/agent-factory/codex-reviewer/README.md)
for runtime configuration and engine review behavior.

Architect, Product, PM and Intent Refinement also have Task API profiles:
`agent-task-gpt-architect`, `agent-task-gpt-product`, `agent-task-gpt-pm` and
`agent-task-gpt-intent-refinement`. These are Task identifiers, not GitHub mentions;
they are registered in
[`tasks/personas.py`](../modules/gateway/src/tasks/personas.py).
Task clarification replies continue the same task. Architect's Task profile returns
a design and proposed stories without publishing them; PM's Task profile returns a
schedule proposal without dispatching agents. Native publication and dispatch are
implemented in their GitHub host.

**Bounded live verification in the dev deployment, 29 September 2026:**

| Codex persona | Verified workflow | Coverage limit |
|---|---|---|
| Developer | GitHub issue → two-file implementation → seven passing tests → PR; final commit independently tested. Activity completed and progress streamed. [Disposable PR #6795](https://github.com/aws-e/adp/pull/6795) was closed without merging. | Small GitHub task; this test did not exercise engine execution or live control commands. |
| Reviewer | Earlier bounded PR review and live-control qualification. | The review/fix/test/merge behavior is implemented; this refresh did not rerun merge or engine qualification. |
| Architect | Two GitHub runs published native child stories and dependency links. | Task profile remains report-only. |
| Product | Task clarification completed and the answer was preserved in the requirements. | GitHub workflow was not live-qualified in these checks. |
| PM | GitHub dispatch selected ready work, respected blockers and reused the child invocation on repeat. | Task profile does not dispatch; full engine orchestration was not qualified in these checks. |
| Intent Refinement | Task clarification completed and preserved the user's data-access restriction. | GitHub workflow was not live-qualified in these checks. |

These checks demonstrate the listed workflows, not sustained reliability or broad
quality/cost acceptance. Shared Activity infrastructure exposes progress and remote
controls according to the running adapter's advertised capabilities; a registered
persona alone does not guarantee every control is available at every stage.
Codex Operations and Codex AI-DLC are not registered GitHub personas.

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
  Claude SDK adapter in the shared worker and delegates to the Codex CLI, which is
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

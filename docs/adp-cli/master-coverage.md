# ADP CLI master coverage

This is the source coverage and acceptance ledger for Epic [#5644](https://github.com/aws-e/adp/issues/5644), reconciled against the final integration [#6262](https://github.com/aws-e/adp/pull/6262) on 26 September 2026. The checked [command inventory](command-inventory.md) contains **274 parser-backed command forms**. Forms count parser leaves, not completed stories, deployed capabilities or accepted live scenarios.

| Integration checkpoint | Parser-backed forms | Source coverage |
|---|---:|---|
| First batch [#6253](https://github.com/aws-e/adp/pull/6253) | 252 | Hierarchy, access/session controls, machine identities, person caps, rate limits, model policy, routing, knowledge, GitLab, research and recovery |
| Lifecycle batch [#6256](https://github.com/aws-e/adp/pull/6256) | 266 | Platform and Superplane lifecycle adapters |
| Final coding batch [#6262](https://github.com/aws-e/adp/pull/6262), including [#6260](https://github.com/aws-e/adp/pull/6260) | 274 | Human Task enrollment, durable investigator chat, Claude/Codex repository patch Tasks and bounded model probes |

The hosted coding and chat implementations are integrated in the final source batch. Both reuse the existing Task API; the CLI does not introduce a replacement dispatcher. Coding includes server-verified repository/issue/commit context and the E42 installed-CLI scenario. Chat retains stable requests and task-correlated conversation history. Their live chat, coding/control, publication and story acceptance holds remain open.

## Final CLI master at a glance

| Surface | Integrated command coverage | Acceptance boundary |
|---|---|---|
| Identity and administration | Sessions, tenants, hierarchy, memberships, access, machine identities, credentials and identity claims | Authorized tenant/member scope and each lifecycle's live evidence |
| Spend and model controls | Budgets, person caps, rate limits, usage, model policy, routing and Task model catalogue/probes | Standing policy, enforcement and provider evidence |
| Hosted work and remote control | Task submission/status/monitor/abort, hosted coding/chat, Activity reads/SSE and advertised controls | Existing Task API and Activity/ControlService; accepted submission is not completion |
| Delivery and integrations | Flows, GitHub/GitLab, knowledge, research, Superplane and platform lifecycle | Deployment support, scoped effects, publication and cleanup evidence |
| CLI operation | Install/update, deployment selection, capabilities, doctor and checked inventory | Parser/help/manifest/served-bundle parity and installed-client checks |

## Latest retained live checkpoint

The gateway serves the final coding batch (source `b1e266d97a1bf6e4a9c1805a02dd7482b3eafbe0`), with all 35 downloaded files verified against the release. Disposable-EC2 run [36212510138](https://github.com/aws-e/adp/actions/runs/36212510138) passed installation, login and all 19 executable nightly story-read scenarios: **21 passed, 1 failed, 3 blocked**. The hierarchy lifecycle passed seven authorization/membership checks but failed cleanup because the harness omitted the organization's automatically created default children. Exact CLI recovery subsequently removed those children and the organization with absence readback; the original case remains failed. EC2 cleanup completed and the instance was independently confirmed terminated.

The earlier run [36211848174](https://github.com/aws-e/adp/actions/runs/36211848174) passed all eight vault lifecycle checks. AWS metadata corroborated scheduled deletion of its exact owned secret; physical purge and recovery-window expiry were not claimed. Its original overall failure remains recorded. Research and Superplane reads still require domain fixtures; hosted coding requires explicit enrollment and its bounded scenario. Passing reads does not establish mutation, inference, remote-control or full story acceptance.

## Available in baseline source

| Area | Current command groups |
|---|---|
| Session and local tools | `login`, `status`, `refresh`, `logout`, `import`, `token`, `codex`, `claude`, `kimi`, `serve`, `daemon` |
| CLI lifecycle and selection | `help`, `version`, `update`, `uninstall`, `deployment`, `--deployment` |
| AWS and routing | `aws`, `bedrock`, `admin bedrock` |
| GitHub and administration | `github`, `admin login`, `admin setup`, `admin github` |
| Models and delivery flows | `models`, `flow` |
| Superplane | `superplane` workspace, deployment, account, provider and onboarding commands; deployment/domain availability remains required |

## Merged and reviewed additions

| Candidate | Addition | Boundary |
|---|---|---|
| #6074 — merged | `task submit`, `task status`, `task monitor`, `task abort` | Existing `/v1/tasks` API and registered Task service-principal credentials; accepted submission is not completion |
| #5716 — merged / #5621 | `capabilities`, `doctor`, checked command manifest | Support, deployment enablement, permission and readiness remain separate |

## Master coverage after the Epic's stories pass

| Story | Coverage to add or complete | Reuse |
|---|---|---|
| #5516 | Hosted submission and authoritative follow/status/logs/wait | [Hosted coding](hosted-coding.md) via existing Task API, human repository enrollment, Claude/Codex engines, Task model catalogue/probes and E42; live acceptance and test/publication evidence remain held |
| #5589 | [Budget CLI](budgets.md): own reads and exact-period administrator list/show/set/delete/status; E26 nightly reads, live enforcement acceptance pending | Existing budget APIs and usage accounting |
| #5621 | Capabilities, diagnostics and command inventory | PR #5716 merged; live acceptance pending |
| #5622 ([tenant CLI](tenant.md), source; live held) | Tenant selection and isolation | Existing deployment/session selection and server-authorized membership |
| #5623 | [Hierarchy CLI](hierarchy.md): organizations, departments, teams, memberships and tenant-org links; E29 reads, live lifecycle acceptance pending | Protected administration APIs, revision adapters and durable membership removal |
| #5624 | [Machine identity CLI](machine-identities.md): 15 lifecycle leaves; source implemented, live acceptance held | Existing SQL IAM, IAM registry, Cognito and canonical persona principal services; guarded revisions and durable registration/retirement receipts |

| #5625 | Access requests and session revocation | Existing authorized access administration |
| #5626 | [Person-wide caps/defaults](person-budgets.md), revision-safe admin writes and member reports; E35 nightly self reads/refusal, live enforcement held | Canonical person APIs; self writes unavailable by existing policy |
| #5627 — source implementation | Rate-limit administration and client enforcement | Existing rate-limit services |

| #5628 ([usage CLI](usage.md), source implementation; live held) | Usage, spend and request-log exports | Existing scoped readers |
| #5629 | [Activity CLI](agent.md): list/chain/detail/status/wait/transcript/SSE and capability-gated controls; live acceptance held | Existing Activity/ControlService; Task input/cancel remain Task API operations |
| #5630 | `flow node resume`, `flow recover-pr` with reviewed revisions; E37 nightly reads/refusals | Integrated inception/amendment and bounded live recovery acceptance pending |
| #5631 | [Credential and identity CLI](vault.md), source implementation; live acceptance held | Existing vault and identity APIs; metadata revision adapter, protected input and unverified claim readback |
| #5632 | [Knowledge assets and indexing progress](knowledge.md) | CRUD/status/watch, keyed reindex, exact bulk receipts, guarded admin indexing; E32 reads/previews; live retrieval acceptance held |
| #5633 | [Personal Bedrock routing and administrator mappings](bedrock-routing.md) | Revision-bound self selection/reset, exact mappings and compatible connection links; E34 previews; live inference/restoration held |

| #5634 | GitHub installations, App keys and org bindings | Existing GitHub helpers/services |
| #5635 | GitLab connection and agent readiness | Existing GitLab integration |
| #5636 ([model policy/costs](model-policy.md), source implementation; live held) | Model defaults, runtime posture and persona cost | Existing model-policy APIs; retain upstream evidence dependencies |
| #5637 | Superplane wire-contract repairs | Already closed; regression only |
| #5639 | [Research reads and proposal review](research.md), E25 nightly reads; bounded scan/generation held | Existing Superplane research APIs; idempotent proposal identity and human revision checks |

| #5638 | Superplane workspace/deployment/provider lifecycle | App-owned lifecycle adapters, revisions, namespace assertions, scoped events, E39 read-only scenario; live compute/cleanup held |
| #5640 | Durable hosted conversation start/resume/readback; source integrated, live acceptance held | Existing hosted chat APIs backed by Task API; stable requests and exact task-correlated history |
| #5641 ([platform facade](platform.md), source implementation; live held) | Platform lifecycle status and governed deployment facade | Canonical deployment tooling |

The resulting CLI is a common terminal surface for identity, infrastructure connections, budgets, models, delivery, hosted work and domain operations. A single selected deployment and authorized tenant scope apply throughout. Task submission reuses Task API, and control commands advertise only runtime-supported operations.

Completion requires parser/help/manifest/install/update/download parity, stable machine output, authorization/refusal tests and each story's required live evidence. Source merged, bundle published and live accepted are distinct states.

The [command inventory](command-inventory.md) lists parser-backed command forms and options, including the delegated Superplane onboarding helper. Task and capability publication are tracked in the [qualification record](../design-notes/5644-cli-control-qualification/README.md).

#5634 GitHub maintenance source: [commands and acceptance hold](github-maintenance.md). Six new leaves cover disconnect, key activation and org binding; E28 is read/preview regression only, with live isolated maintenance and consumer continuation still held.

Access/session #5625 supplies seven additional source forms and nightly E33;
see [scope and examples](access-and-sessions.md). Live membership/spend and
revocation timing are not inferred from those source forms.

#5635 GitLab source adapter: six leaves and [published human API contract](gitlab.md), with E30 discovery/refusal regression. Real dedicated-project webhook/run/artifact and disconnect cleanup acceptance remains held.

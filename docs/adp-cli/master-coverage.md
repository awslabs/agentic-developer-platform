# ADP CLI master coverage

This is the coverage target for Epic [#5644](https://github.com/aws-e/adp/issues/5644), checked against main `200e97aa7` and open PRs on 25 September 2026. A target entry does not mean the command is installed or the server permits it. The final leaf-command/flag inventory is the checked command manifest from #5621, reconciled with parser source and the served bundle.

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
| #5516 | Hosted submission and authoritative follow/status/logs/wait | Existing Task API; reconcile human authority and repository-persona support, no new dispatcher |
| #5589 | Budget configuration and actual enforcement | Existing budget APIs and usage accounting |
| #5621 | Capabilities, diagnostics and command inventory | PR #5716 merged; live acceptance pending |
| #5622 | Tenant selection and isolation | Existing deployment/session selection and server-authorized membership |
| #5623 | Organizations, departments, teams and memberships | Existing administration APIs |
| #5624 | Service accounts, agent registrations and canonical principals | Existing identity/registration APIs |
| #5625 | Access requests and session revocation | Existing authorized access administration |
| #5626 | Person-wide caps and inherited defaults | Existing person budget policy |
| #5627 | Rate-limit administration and client enforcement | Existing rate-limit services |
| #5628 ([usage CLI](usage.md), source implementation; live held) | Usage, spend and request-log exports | Existing scoped readers |
| #5629 | [Activity CLI](agent.md): list/chain/detail/status/wait/transcript/SSE and capability-gated controls; live acceptance held | Existing Activity/ControlService; Task input/cancel remain Task API operations |
| #5630 | Recovery and integrated CLI qualification | Existing AI-DLC and regression runner |
| #5631 | [Credential and identity CLI](vault.md), source implementation; live acceptance held | Existing vault and identity APIs; metadata revision adapter, protected input and unverified claim readback |
| #5632 | Knowledge assets and indexing progress | Existing knowledge/indexing APIs |
| #5633 | Personal Bedrock routing and administrator mappings | Existing routing helpers/services |
| #5634 | GitHub installations, App keys and org bindings | Existing GitHub helpers/services |
| #5635 | GitLab connection and agent readiness | Existing GitLab integration |
| #5636 | Model defaults, runtime posture and persona cost | Existing model-policy APIs; retain upstream evidence dependencies |
| #5637 | Superplane wire-contract repairs | Already closed; regression only |
| #5638 | Superplane workspace/deployment/provider lifecycle | Existing Superplane commands and domain contracts |
| #5639 | Research inspection and proposal review | Existing Superplane research APIs |
| #5640 | Durable hosted conversation start/resume/readback | Existing hosted chat APIs; no replacement Task dispatcher |
| #5641 | Platform lifecycle status and governed deployment facade | Canonical deployment tooling |

The resulting CLI is a common terminal surface for identity, infrastructure connections, budgets, models, delivery, hosted work and domain operations. A single selected deployment and authorized tenant scope apply throughout. Task submission reuses Task API, and control commands advertise only runtime-supported operations.

Completion requires parser/help/manifest/install/update/download parity, stable machine output, authorization/refusal tests and each story's required live evidence. Source merged, bundle published and live accepted are distinct states.

The [command inventory](command-inventory.md) lists parser-backed command forms and options, including the delegated Superplane onboarding helper. Task and capability publication are tracked in the [qualification record](../design-notes/5644-cli-control-qualification/README.md).

#5635 GitLab source adapter: six leaves and [published human API contract](gitlab.md), with E30 discovery/refusal regression. Real dedicated-project webhook/run/artifact and disconnect cleanup acceptance remains held.

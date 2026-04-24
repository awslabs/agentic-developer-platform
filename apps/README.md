# apps/

Domain packs. Each subdirectory is a **peer app** that plugs into the harness and reuses the platform core.

If `ARCHITECTURE.md` is the mental model and `modules/harness/contracts/` is the contract, this directory is where domains actually live.

## What goes here

One subdirectory per domain. Examples: `threat-research/`, `ml-platform/`, `data-platform/`, `sre/`, `finance-ops/`.

Each app is a self-contained pack:

```
apps/<domain>/
├── README.md      # what this domain does, who owns it
├── agents/        # agent declarations (contract + persona + permissions + tests)
├── tools/         # tool adapters — register with harness/mcp-hub
├── jobs/          # job handlers — register with harness/jobs
├── events/        # event type schemas + producers/consumers
├── skills/        # domain skills (playbooks)
├── schemas/       # domain types (Sample, Model, Dataset, …)
├── policy/        # domain-informed policy rules (TLP, egress labels, …)
├── frontend/      # optional domain UX
└── infra/         # domain-specific Terraform (specialised clusters, pools, …)
```

Not every app needs every folder. Start with `agents/` + `tools/` + whatever else the first skill actually requires. Grow from there.

## What apps must not do

These invariants are what make the platform stay coherent. If any of them slip, fix it fast:

1. **Don't reimplement harness primitives.** No queues, retry loops, permission engines, artifact stores, or event buses inside an app. If one is missing from the harness, that's a harness gap, not an app concern.
2. **Don't reach into other apps.** No `from apps.threat_research import …` inside `apps/ml-platform/`. Cross-app interaction goes through tools, events, or context — always via the harness.
3. **Don't bypass policy.** Every gated action goes through the harness policy engine. Adding a local bypass because it's faster is how platforms lose their audit story.
4. **Don't couple to a specific contract version invisibly.** Declare which versions of which contracts the app depends on, so breaking changes are detectable.

## What a new app owes the platform

When adding a domain pack, you're committing to:

- **Declared tools, jobs, events** — all registered via the contracts in `modules/harness/contracts/`.
- **Typed agent contracts** — every agent declares inbox and outbox schemas.
- **Tests for skills and agents** — regression fixtures that run on deploy.
- **An owner** — a team or person who responds when something breaks.
- **A README** — at minimum, what the domain does, who owns it, and how to run it locally.

## Adding a new app

The short version:

1. Open an ADR under `docs/decisions/` proposing the domain. Why is this its own app, not a subsystem of an existing one?
2. Create `apps/<domain>/` with a README.
3. Write the first agent (five-file shape — `agent.yaml`, `persona.md`, `inbox.schema.json`, `outbox.schema.json`, `tests/`).
4. Write any tools the agent needs, registered with `harness/mcp-hub`.
5. Deploy and run the agent against a realistic inbox item.

"Do I really need a new app, or is this a skill/tool inside an existing one?" — default to *skill or tool in an existing app* unless the domain has a distinct user base, distinct policy posture, or distinct infrastructure. Merging is cheap; splitting later is easier than unsplitting.

## Current apps

None yet. The first domain pack to land here is threat-research, which is already partially represented across the platform via existing modules (Bedrock Gateway, Agent Factory, Agent Context) but has not yet been formalised as an `apps/threat-research/` pack.

See `ARCHITECTURE.md` for the target structure and the gap against today.

## Anti-patterns to watch for

These are the failure modes that tend to appear first:

- **A domain folder that contains its own "platform" subfolder.** Usually means someone rebuilt a harness primitive instead of asking for it.
- **Shared utility code between two apps.** Usually means a missing harness surface or a missing shared library. Solve at the right layer; don't link two apps together.
- **An app that imports directly from `modules/agent-factory/` or `modules/gateway/`.** Apps should use these via their public surfaces (invocation API, LLM egress), not by linking to their internals.
- **Policy rules scattered in tool code.** If there's a TLP rule or a scope check inside a tool adapter, it belongs in `policy/` and in the harness policy engine, not in the tool.

If you see one of these, raise it — it's a design conversation, not a code-review nit.

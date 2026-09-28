# Harness Contracts

The public API between the **harness** and **apps**. Everything here is versioned; breaking changes require a version bump and a migration path.

If `ARCHITECTURE.md` is the mental model, this directory is the shape of the contract that makes the model real. An app is "on ADP" when it conforms to these contracts.

## Status

Contracts are still being formalized. The schemas in this directory are the target; some are stubs, some are not yet written. This README is the source of truth for what each contract covers.

**One contract is now real: [`hitl-ticket` v1](../../../contracts/hitl-ticket/v1/)** (issue #4178). It lives under the repo-root `contracts/` tree, not in this directory, because that is where the executed-contract convention already lives (`contracts/provenance/v1/` plus a CI job that runs its golden fixture). See [Where contracts live](#where-contracts-live) below.

## What lives here

| File | Contract | Who produces | Who consumes |
|---|---|---|---|
| `tool.schema.json` | A tool registration: name, version, input/output schema, required permissions, rate limits | Apps (tool adapters) | `harness/mcp-hub` |
| `job.schema.json` | A job kind registration: name, version, input/output schema, retry policy, worker pool | Apps (job handlers) | `harness/jobs` |
| `event.schema.json` | An event type: name, version, payload schema, ordering guarantees | Apps (event producers) | `harness/events` |
| `artifact.schema.json` | An artifact kind: mime type, tags, retention class, access policy | Apps | `harness/artifacts` |
| **[`hitl-ticket` v1](../../../contracts/hitl-ticket/v1/) ✅ written** | A HITL ticket shape: scope, prompt, fail-closed answer vocabulary, timeout, approvers | Apps and harness skills | `harness/hitl` |
| `skill.manifest.json` | A skill declaration: name, version, required tools, required scopes, inputs, outputs | Skill authors | `harness/skill-registry` |
| `agent.manifest.json` | An agent declaration: name, version, runtime, model, inbox/outbox, permissions, persona | Agent authors | `modules/agent-factory` via `harness/agent-registry` |
| `inbox-item.schema.json` | The envelope around a typed work item: caller identity, timestamp, priority, artifact refs | Callers | Agents |
| `outbox-item.schema.json` | The envelope around a typed result: producing agent, version, provenance link, artifact refs | Agents | Downstream consumers |
| `policy.schema.json` | Policy rule: condition, effect, scope, precedence | Platform + domain ops | `harness/policy` |
| `provenance-record.schema.json` | A node in the derivation DAG | Every harness surface | `harness/provenance` |

## Where contracts live

Two locations, for a reason worth stating rather than leaving as an inconsistency:

| Location | What's there |
|---|---|
| `modules/harness/contracts/` (this directory) | The **catalogue** — the table above, the versioning rules, the decision tree. Documentation. |
| `contracts/<name>/v<n>/` (repo root) | **Written contracts**: validator + golden fixture + a CI workflow that executes the fixture. |

The root tree is where `contracts/provenance/v1/` already lives, and the
convention there is the one that demonstrably prevents drift — a fixture that CI
runs on every PR that touches it. Written contracts go there so they inherit that
enforcement. This directory stays the index.

## Versioning rules

- **Every contract has a `version` field.** Start at `1`. Never remove.
- **Additive changes** (new optional fields) don't bump the major version.
- **Breaking changes** (removed/renamed/retyped fields, changed semantics) require a new version number.
- **Consumers pin** to specific versions. The harness serves multiple active versions during deprecation.
- **Deprecation cycle** — announce → serve both versions → remove. Minimum window to be decided, but never less than one full release cycle.

Breaking a contract silently is the single most damaging thing that can happen to the platform. When in doubt, add a new version rather than mutating the existing one.

## The contract shape, generally

Every contract here follows the same rough pattern:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "adp://contracts/<name>/v<n>",
  "title": "<name> v<n>",
  "type": "object",
  "required": ["name", "version", "owner", "..."],
  "properties": {
    "name":    { "type": "string" },
    "version": { "type": "integer", "minimum": 1 },
    "owner":   { "type": "string", "description": "App or team that owns this" },
    "...": { "...": "..." }
  }
}
```

Three fields are present on *every* contract: `name`, `version`, `owner`. That's the minimum substrate the harness needs to register, dedupe, and route questions.

> **The JSON-Schema envelope above is aspirational; the `name`/`version`/`owner` substrate is not.**
> No `*.schema.json` file and no JSON-Schema validator library exists anywhere in
> this repo, so the envelope has never been executed by anything. The first
> written contract (`hitl-ticket` v1, #4178) keeps the three substrate fields —
> pinned to literal values, so a document from another contract or a future v2
> cannot validate as v1 — but expresses the shape as a Pydantic model with a
> CI-executed golden fixture, because that is the convention this repo actually
> runs. Later contracts should follow the executed one. If a JSON-Schema
> validator is ever adopted deliberately, revisit this.

## Which contract do I need?

Quick decision tree for new capabilities:

- Exposing a synchronous verb agents can call? → `tool.schema.json`
- Long-running work that outlives a single call? → `job.schema.json`
- A thing the system should notify about when it happens? → `event.schema.json`
- A large blob (sample, PCAP, model) that tools/jobs produce or consume? → `artifact.schema.json`
- Need a human in the loop? → **[`hitl-ticket` v1](../../../contracts/hitl-ticket/v1/)** (written)
- A playbook composing the above for a specific outcome? → `skill.manifest.json`
- A complete agent declaration? → `agent.manifest.json`
- Policy rule (who may do what, when)? → `policy.schema.json`

## Non-goals for this directory

What this directory is **not**:

- **Not implementations.** No service code, no Terraform, no Docker. Only schemas and their documentation. Implementations live in sibling directories (`mcp-hub/`, `jobs/`, `events/`, etc.).
- **Not tool-specific or domain-specific schemas.** `Sample`, `Model`, `Dataset` are domain types — they live in `apps/<domain>/schemas/`, not here. This directory only holds the *envelopes* apps plug into.
- **Not a runtime registry.** The runtime registry (what's currently registered, by whom, at what version) is exposed by each surface's service. This directory is the compile-time contract.

## Adding or changing a contract

1. Propose a change in an ADR under `docs/decisions/`.
2. If additive: update the schema, bump nothing, add examples.
3. If breaking: create a new file (e.g., `tool.schema.v2.json`), update consumers to support both, deprecate the old with a removal target.
4. Update this README's table.
5. Update any affected guide under `docs/guides/`.

## Current state

**1 of 11 written.** They land incrementally as each harness surface is built out. The test is executability, not existence: if a contract has a validator, a golden fixture, and a CI job that runs the fixture, the contract is real; if only this README mentions it, it's still a target.

| Contract | State |
|---|---|
| [`hitl-ticket` v1](../../../contracts/hitl-ticket/v1/) | ✅ Written — validator + golden fixture + [CI](../../../.github/workflows/hitl-ticket-contract-tests.yml) (#4178) |
| The other ten | Target — this README only |

`hitl-ticket` landed first, out of the order below, because it was the one with
demand: two independent research investigations converged on a durable HITL
ticket as ADP's missing primitive, and a long-running agent's value depends on
being able to pause for a human and resume safely. It also landed *before* any
consumer, so the two divergent approval implementations can be reconciled against
a written shape instead of against each other.

Remaining ordering, most likely:

1. `tool.schema.json` — `modules/harness/mcp-hub/` is the most-built surface.
2. `agent.manifest.json` and `skill.manifest.json` — needed before formalizing existing agents under the new shape.
3. `inbox-item.schema.json` / `outbox-item.schema.json` — needed to typify the invocation surface.
4. The rest — as harness surfaces are built.

No contract is blocking; the gating factor is each surface's implementation, not the schema.

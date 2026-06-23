# Agent Context — Knowledge Layer docs

Design + architecture artifacts for the code Knowledge Layer (EPIC #1345).

## Start here
- **`knowledge-layer-product-vision.md`** — the **product vision**: who it's for, the problem, the outcome, and the moat. The *why*. Read this first.
- **`knowledge-layer-design.md`** — the **design of record** (consolidated). What we build now (§1–§15: stores, indexing, MCP verbs, SBOM/vuln loop, ACL, schema, serverless posture) **plus** the north-star architecture (§16: hybrid edge/hub, four-tier ingestion, target graph schema). The *how* and *where it's heading*.

## Detail (implementation-level, referenced by the design of record)
- **`database-design.md`** — the `agent_context` Postgres schema (catalog, deps, vulnerabilities, run/stage tracking).
- **`design-1348-replace-openviking.md`** — OpenViking → S3 Vectors + S3/Mountpoint implementation detail.
- **`design-1586-knowledge-layer-agent-integration.md`** — wiring the MCP Door into agent runtimes (identity bridge, tool registration).
- **`design-notes/`** — per-issue implementation notes (Zoekt #1346, structural #1357, SBOM #1358, vuln loop #1360, DeepWiki #1382, IaC graph #1647, live-AWS research #1546).
- **`knowledge-layer-c4.html`** — C4 architecture diagram (Context → Container → Component), self-contained HTML.
- **`workspace.dsl`** — Structurizr DSL model (one source → all C4 views). See `workspace.README.md` to render.

## Archived (superseded — kept for history, not implementable)
- **`archive/knowledge-layer-storage-design.md`** — the prior design of record; consolidated into `knowledge-layer-design.md`.
- **`archive/vision-federated-code-intelligence.md`** — the prior north-star brief; folded into `knowledge-layer-design.md` §16.
- **`archive/neptune-deep-graph-design.md`** — Neptune graph design authority (EPIC #1529); folded into the design of record's graph sections.
- **`archive/neo4j-deep-graph-design.md`** — superseded by Neptune (Neo4j Community is GPLv3 — violates #1345's permissive-license mandate).

## Related (not in this folder)
- EPIC #1345 (Knowledge Layer) + child EPICs E1–E7, plus sub-EPIC #1443 (per-repo C4 generation — `workspace.dsl` is its target output form).
- Separate future pillars: IaC dependency graph (#1647), live-AWS resource graph (#1546), Personal Context (#1287).

# Agent Context — Knowledge Layer docs

Design + architecture artifacts for the code Knowledge Layer (EPIC #1345).

## Start here
- **`knowledge-layer-storage-design.md`** — the design of record (storage, indexing, stores, SBOM/vuln loop, §12 spectrum positioning, §13 serverless/portability). Read this first.
- **`knowledge-layer-c4.html`** — C4 architecture diagram (Context → Container → Component), self-contained HTML.
- **`workspace.dsl`** — Structurizr DSL model (one source → all C4 views). See `workspace.README.md` to render to HTML/PlantUML/Mermaid.

## Detail
- **`database-design.md`** — the `agent_context` Postgres schema (catalog, deps, run/stage tracking).
- **`design-1348-replace-openviking.md`** — OpenViking → S3 Vectors + S3/Mountpoint design.
- **`design-notes/`** — per-issue design notes (Zoekt #1346, structural #1357, SBOM #1358, vuln loop #1360, DeepWiki dual-sink #1382).

## Related (not in this folder)
- EPIC #1345 (Knowledge Layer) + sub-EPIC #1443 (per-repo C4 generation — `workspace.dsl` is its target output form).

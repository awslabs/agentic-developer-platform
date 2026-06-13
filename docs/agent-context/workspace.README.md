# Rendering `workspace.dsl` → diagrams / HTML

`workspace.dsl` is a [Structurizr DSL](https://docs.structurizr.com/dsl) model of the Knowledge Layer.
One model → all C4 views (Context, Containers, Components) render automatically and stay consistent.

## Option A — Structurizr Lite (interactive HTML, recommended)
Runs a local web UI that renders + lets you explore/export the diagrams.
```bash
# from this directory (must be the dir containing workspace.dsl)
docker run -it --rm -p 8080:8080 \
  -v "$PWD":/usr/local/structurizr \
  structurizr/lite
# open http://localhost:8080  → Context / Containers / Components views
```

## Option B — Structurizr CLI → PlantUML/Mermaid (static export, for committing)
```bash
docker run --rm -v "$PWD":/usr/local/structurizr structurizr/cli \
  export -workspace workspace.dsl -format plantuml/c4plantuml -output ./out
# or: -format mermaid    (renders in GitHub/markdown natively)
# then render the .puml with any PlantUML renderer, or embed the .mmd in markdown
```

## Option C — no Docker
- Paste the DSL into the online editor at https://structurizr.com/dsl (renders instantly), or
- Export to Mermaid (Option B) and view in any Mermaid viewer / GitHub markdown.

## Views defined
| View | C4 level | Scope |
|------|----------|-------|
| `Context` | 1 | Knowledge Layer + external actors (agents, admins, GitHub, Bedrock, agent-factory, gateway) |
| `Containers` | 2 | All runnable units (worker, MCP server, Zoekt, DeepWiki, LiteLLM, stores) with Ephemeral/AlwaysOn/Managed tags |
| `IngestionComponents` | 3 | Inside the Ingestion Worker — the 6 stages + StageTracker/Config |
| `McpComponents` | 3 | Inside the Context MCP Server — ACL filter + search backend |

## Note for the per-repo C4 sub-EPIC (#1443)
This hand-authored DSL is the *target form* for what #1443 auto-generates per ingested repo:
the **Component view** can be emitted programmatically from each repo's `code-index.json`
(symbols + call graph → DSL components + relationships), and the **Context/Container views**
LLM-assisted from the DeepWiki wiki. Structurizr DSL is a good output format because it's
text (diffable, versionable) and renders all levels from one source.

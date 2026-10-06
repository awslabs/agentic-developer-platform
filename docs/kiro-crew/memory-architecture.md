# Kiro Crew memory architecture

How [Kiro Crew](https://github.com/kirodotdev/KiroCrew) remembers, and what that
means when it runs against the ADP gateway. Written from the Kiro Crew source at
`68d920b` (2026-10-05, `src/kiro_crew/__init__.py` version 0.9.0; the installed
release used in [setup-on-adp-devbox.md](setup-on-adp-devbox.md) is 0.7.2). Primary
sources: `docs/system-specs/modules/memory-skills-hooks.md` (the memory spec),
`docs/architecture/context-management.md`, `docs/system-specs/modules/knowledge.md`,
`docs/system-specs/modules/learn-cron-dashboard.md`, and the code in
`src/kiro_crew/{memory,memory_stores,memory_schema,memory_v2,memory_recall,
vector_memory,embeddings,history,history_consolidation,learn}.py` and
`src/kiro_crew/knowledge/`. File references below are to that repository.

## In one paragraph

Everything is local to the Kiro Crew gateway process: markdown files plus SQLite
under `~/.kiro/crew/`, embeddings computed in-process by a vendored llama-cpp
runtime (Qwen3-Embedding-0.6B). The agent can only *write* memory through one
explicit tool (`learn_add`); everything else — facts, episodes, daily summaries —
is produced by automatic, model-driven consolidation. Reads happen two ways: a
budgeted block injected at session start, and an on-demand `memory_recall` tool
that runs a hybrid vector + keyword search. There are two store generations
(Global V1 and per-member private V2), a separate Knowledge Library that builds a
local entity graph over documents, and a deliberately single-user security
posture. None of it depends on the agent harness, so it works unchanged with the
`claude` harness that routes model calls through ADP.

## 1. Layers and stores

The spec names "six memory layers" (`memory-skills-hooks.md:125-186`) and "a
store's three paths" (`:190-245`).

### Global V1 — the default, shared store

| Store | Path under `~/.kiro/crew/` | Contents | Format |
|---|---|---|---|
| Markdown root | `workspace/memory/` | `preferences.md`, `projects.md`, `history/{date}.md` daily summaries | markdown, owner-editable |
| Vector store | `memory.db` | `semantic_memory` key-value facts (`pref.*`, `project.*`, `user.*`, `lesson.*`; JSON value ≤ 4096 B, confidence-gated); `episodic_memories` (10–2000-char fragments, `embedding` BLOB, tags, importance); `memory_events` audit (≤ 10k rows) | SQLite; FAISS optional and rebuildable, stdlib cosine fallback |
| FTS index | `memory_index.db` | FTS5 over the markdown files | SQLite |
| Lessons fallback | `lessons.jsonl` | used only when the vector store holds no lesson | JSONL |
| Named V1 stores | `memory_stores/<name>/` | same layout per named store | — |

### Member V2 — private memory per crew member

One SQLite `memory.db` per immutable `member_id`/`store_id` (`memory_schema.py`;
`create_member_database` / `open_member_database` in `vector_memory.py`) holding
facts, permanent rules, episodes, learned daily history (`memory_history`),
revisions, proposals, `memory_fts` and embeddings — no JSONL and no separate FTS
database. The manual persona, permanent rules (`trust/member-rules/`), briefing
(`members/<slug>/briefing.md`), preference/project anchors and project guides stay
owner-managed documents outside the database (`context-management.md:578-597`).
A member cannot read Global V1 or sibling stores (`mcp_tools/learn.py:58-67`).

### Knowledge Library — a separate subsystem

`workspace/knowledge/knowledge.db`: `items` (chunks with `embedding` BLOB),
`items_fts`, and entity/relation tables. Documents come from watched folders,
uploads, agent artifacts, fetched URLs and connectors. Global scope; searched by
the `local_knowledge_search` tool (`knowledge.md:1-16, 429-510`). See §6.

## 2. Write path — how memories are created

- **Explicit tool: `learn_add`** (`mcp_tools/learn.py:76, 253`) → `POST /api/lessons`
  → `vector_memory.write_lesson()` as `lesson.<md5>` with confidence 1.0 and
  source `user_explicit`; gated by `capabilities.memory_writes` and
  `memory.persistence_enabled` (`learn-cron-dashboard.md:89-100`). Also available
  as `kirocrew learn add|list|remove`. **There is no `memory_save` tool**: the only
  other agent-facing memory tool, `memory_recall`, is read-only.
- **Automatic consolidation** (`history.py` `HistoryConsolidator`, spec `:333-420`)
  — two LLM-summarised paths per session: (a) every 30 messages
  (`_CONSOLIDATION_THRESHOLD`): ≤ 20 semantic facts and, in V1, a rewrite of
  `preferences.md` / `projects.md`; (b) after 3 h idle (`memory.history_idle_hours`):
  append `history/{date}.md`, ≤ 10 episodes, ≤ 10 implicit lessons. V2 commits all
  of it plus a source-span receipt in one SQLite transaction
  (`VectorMemoryStore.apply_consolidation`, `history_consolidation.py`). Facts,
  episodes and summaries are therefore **model-extracted, not verbatim**; only
  `learn_add` lessons are stored as given. Display-only `notice` rows never reach
  the prompt.
- **Operator paths:** `kirocrew consolidate [session_key|--all]` (`cli.py:787-870`)
  forces consolidation and triggers skill extraction; `POST /api/memory/consolidate`;
  `kirocrew memory migrate` (markdown → semantic/episodic) and `import`;
  `POST /api/memory/promote` (episodic patterns → facts); `/api/memory/seed` (owner
  copies V1 rows into a V2 store).
- The spec says correction detection "runs after each ACP response"
  (`learn-cron-dashboard.md:82`); that hook was not located in the code at this
  revision.

## 3. Read path — what the model sees

### Injected at session start (Global V1)

`context-management.md:84-176`. Protected blocks: the full `preferences.md`,
`pref.*` facts, lessons (`[Learned corrections]`, scoped by `repo_scope`), a
bounded activity index and a `[Memory tools]` line pointing at `memory_recall`. A
background `[Memory activity]` block (on by default, `memory.inject_activity`):
projects, daily history (14 days in full, decayed summaries to day 180),
query-ranked task facts and episodes (`_EPISODIC_INJECT_CAP`). Each block is
admitted whole or dropped under a character share of `_CONTEXT_BUDGET_BASE`
(`context_assembly/budget.py`): lessons ≈ 22.6 %, history 16 %, semantic and
episodic ≈ 7.7 % each. Warm turns inject nothing new.

### V2 members

Every turn gets identity, permanent rules, bound persona, admitted project guides
and query-free scoped lessons — **no searched memory block**
(`context-management.md:537-549`, `install.md:84-87`). Facts and episodes come only
through `memory_recall`.

### `memory_recall` (on demand)

`mcp_tools/learn.py:234-250`: `query` (1–2000 chars) → `GET /api/memory/recall?q=`
with the session's execution context → `recall_json(context_cap=3000)`, 16 KiB
payload cap (`memory_recall.py:13`).

| Policy | V2 (`memory_v2.py`) | V1 |
|---|---|---|
| Score | `0.7·cosine + 0.3·term coverage`, × `(0.85 + 0.15·importance)` | semantic `0.6·vector + 0.4·keyword`; episodic `cosine × (0.7 + 0.3·importance) × e^(−0.03·days)` |
| Admission | cosine floor 0.62 (0.57 for queries > 300 chars); lexical ≥ 50 % of terms and ≥ 2 matches | relevance floors 0.55 / 0.42 |
| Diversity | MMR; age-neutral | MMR λ 0.6; top-8 |
| Output | per-row `retrieval` evidence and a `provisional` operating point | ranked rows |

### Embeddings

`embeddings.py`, spec `:2218-2555`: Qwen3-Embedding-0.6B Q8_0 GGUF (≈ 610 MB,
1024-dim), sha256-pinned, downloaded in the background to `~/.kiro/crew/models/`
on first gateway start, run in-process via vendored llama-cpp-python 0.3.34 — one
shared singleton for memory and the Knowledge Library.
`memory.embedding_provider` is coerced to `llama_cpp`. Until the model lands (or
on an unsupported CPU) `embed()` returns `None`, retrieval degrades to FTS/keyword,
and rows are back-filled later without a restart.

## 4. Lifecycle

| Concern | V1 | V2 |
|---|---|---|
| Dedup | episodic cosine > 0.88 (`memory.episodic_dedup_threshold`); lessons substring / topic-overlap ≥ 50 % / cosine > 0.85 | exact text only; never auto-deletes distinct rules (spec `:1632-1720, 3184-3208`) |
| Forgetting | history time tiers, episodic exponential decay, eviction at 10,000 (`memory.episodic_max_count`) (spec `:2190-2216`) | no age decay, no eviction; explicit forgetting / validity intervals only |
| Confidence & provenance | automated semantic writes need ≥ 0.8; `user_explicit` wins; last 20 revisions | conflicting automated updates become owner **proposals**; all revisions kept |
| Audit | `memory_events` | same, plus proposals |
| Injection screening | every write | every write |

Review and edit: dashboard Memory tab (GET/PUT preferences/projects/history,
semantic PUT/DELETE, episodic DELETE, `GET /api/memory/records` with signed
previews via `memory_edit.py`; spec `:2609-2700`) and
`kirocrew memory list|search|show|stats|audit|export|migrate|backup|backups|restore|carve`
(`cli.py:2881-2990`). Writes are gated by session recognition; incognito sessions
are read-only; temporary sessions neither read nor write.

Backups: `memory_backup.py` / `member_memory_backup.py` — online SQLite backup
ZIPs with SHA-256 manifests into `memory_stores/.member-backups/<store>/`, plus
validated staged restore; `kirocrew snapshot` produces a portable backup of the
whole state tree (everything derives from `KIROCREW_HOME`, `config/paths.py:262`).

Privacy: the spec states that "member memory is not an adversarial same-host
confidentiality boundary" (spec `:24-29`). Stores are exposed read-only inside
the agent sandbox; writes go through the gateway. Private V2 turns require
`agent.sandbox=auto` with working Linux/WSL namespaces or macOS Seatbelt; native
Windows, unconfined execution, unsupported MCP backends and Kiro internal
delegation refuse them (`install.md:104-107`).

## 5. Harness coverage — works with the `claude` harness

`context.py:2452-2466`: every provider, Claude Code included, receives the same
injected memory, lessons and skills context; the only Claude-specific difference
is an extra steering block (because `claude-agent-acp` does not read Kiro agent
`resources`). `memory_recall` and `learn_add` are served by the `kirocrew-core`
MCP server, which the Claude harness mounts unless the agent's tools list is
empty (`claude-code-provider.md:155`). The phrase "pinned to the kiro harness"
does not occur in the repository; the two kiro-only behaviours are
`chat.disableInheritingDefaultResources` handling (spec `:1522-1536`) and the
private-V2 refusal of "Kiro internal delegation". No per-harness memory
capability set exists in `harness-parity.md`.

### MCP servers

Kiro Crew's built-in MCP servers are subcommands of its own binary —
`kirocrew mcp-core|mcp-cron|mcp-computer|mcp-dashboard|mcp-work|mcp-crew-log|mcp-debug|mcp-panel`
(`cli.py:2684-2727`) — spawned **per session over stdio** (`acp/session_mcp.py:214`)
and listed in the session's `mcpServers`; they live and die with the session and
call back into the gateway's loopback HTTP API. An optional long-running broker
(`src/kiro_crew/mcp_gateway/`) can pool stdio backends behind per-session stubs
and validate tool schemas by digest; it is off by default. Only the dashboard
(`127.0.0.1:5476`) listens on a port.

## 6. The Knowledge Library's entity graph

Kiro Crew calls the Knowledge Library "a personal knowledge graph"
(`knowledge.md:5`). Ingestion chunks documents and runs `EntityExtractor`
(`knowledge/extractor.py`) — an LLM pass returning `title, entities, relations,
category, summary`, with each untrusted chunk wrapped in nonce-suffixed delimiters
against prompt injection. Items, FTS and the entity/relation tables live in
`knowledge.db` (`knowledge/store.py`). Retrieval (`HybridRetriever`,
`knowledge/retrieval.py`) fuses three legs by reciprocal-rank fusion (k = 60,
vector weighted × 2, recency tie-break): FTS5 keyword, **graph** (query words and
adjacent pairs resolve to entities, expand two hops, rank items by mention
count — the leg that serves multi-hop questions, `knowledge.md:87`), and vector.
A golden-set benchmark exists (`kirocrew bench kb-retrieval`). Conversational
memory (§1–4) is *not* graph-based; no entities are extracted from chats.

## 7. Posture: single-user by design

Kiro Crew describes itself as "a single-user, self-hosted tool where the pack
installer is the machine owner" (`governance.md:2643`) and says its isolation
"is not the multi-tenant answer" (`cloud.md:355`); it "has no account system of
its own" (`README.md:296-302`). Crew *members* are agent personas, not people;
owner/guest is per-slot sharing within one owner's crew; the "tenancy" tables in
`runtime_ownership.py` concern sandbox processes, not organisations.

## 8. Implications for running it against ADP

- **Governance comes from ADP, not Kiro Crew.** With the `claude` harness every
  model call — chat, consolidation, subagents, knowledge extraction — is metered,
  budgeted and rate-limited against the one ADP identity the Crew runs as, and
  appears in the ADP usage log. A Kiro Crew instance is therefore **one person's**
  workspace; several people need several Crews, each with its own ADP sign-in.
- **Nothing in memory is harness-specific**, so the ADP wiring in
  [setup-on-adp-devbox.md](setup-on-adp-devbox.md) changes no memory behaviour.
- **The memory store is not pluggable.** `VectorMemoryStore` and `KnowledgeStore`
  open SQLite directly; there is no remote backend. Externalising memory means
  either carrying the whole `KIROCREW_HOME` tree (per-user volume, or
  `kirocrew snapshot` / `restore` around an ephemeral lifetime) or forking the
  store layer. Porting the *design* — consolidation cadence, explicit-lesson-only
  writes, budgeted injection, hybrid recall, provenance — onto ADP's own
  gateway-backed `MemoryProvider` is the path that keeps ADP's multi-tenant
  authorisation. The persistent per-user executor discussed under story #147 is
  where a Kiro Crew runtime would fit if it is adopted inside the platform.
- **Cold-start cost** when embedded in an ephemeral runtime: the gateway process,
  the 610 MB embedding model (bake into the image) and consolidation that must be
  forced before shutdown, since Kiro Crew's own 3-hour idle trigger never fires in
  a short-lived pod.

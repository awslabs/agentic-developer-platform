# Complex Task Chat Agent — Design Doc

**Status:** Draft  •  **Owner:** @prsaws  •  **Written:** 2026-04-19  •  **Location:** `modules/agent-factory/agent/src/complex-task-chat/`

This doc designs the replacement for the Python SQS-consumer worker that currently runs behind KEDA (`adp-agent-gateway:latest`, pulled into ScaledJob `agent-gateway-worker` in ns `adp-gateway-agents`). The replacement is a TypeScript worker inside the existing `modules/agent-factory/agent/` package, driven by the Claude Agent SDK, with first-class pluggable context management (LCM-inspired) and pluggable cross-agent memory.

---

## 1. Goals and non-goals

### Goals

1. **Swap the KEDA worker image from the Python single-shot completer to a tool-using Claude Agent SDK worker** — same set of tools/skills the GitHub ARC agent gets (`Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, `Skill`).
2. **Make context management pluggable** behind a single interface so we can ship `LcmContext` in Phase 1 and evolve to condensation, rerank, or entirely different strategies without touching agent code.
3. **Make cross-agent memory pluggable** behind a single interface so we can connect today's GH-agent learnings, add chat-agent preferences, add persona learnings, and later swap in Bedrock AgentCore Memory, OpenSearch, or something custom — without touching agent code.
4. **Sessions must not silently lose information.** Every message is persisted and retrievable by ID; compaction produces summaries that cite their source message IDs.
5. **Personas must be able to learn.** Base persona (identity, mindset, role) is static and versioned; persona learnings (accumulated experience) are durable, per-persona memory records written and read by agents over time.
6. **Keep the entire change inside one Docker image** — one TypeScript build, two entry modes (`github` vs `complex-task-chat`) dispatched by env var.

### Non-goals (Phase 1)

- Cross-session chat memory, user personalization, or preference learning (scaffolded via the `MemoryProvider` port; no concrete business logic yet).
- FTS or semantic search over session history.
- Sub-agent delegation / capability grants for expansion (plain `expand_summary` returns source messages directly).
- Multi-depth condensed DAG (bounded-depth phase 2; see §12).
- Abandoning the Python worker image immediately — it stays deployable until the new image is validated.

---

## 2. Where this fits

```
SQS FIFO tasks queue ──► KEDA ScaledJob (adp-gateway-agents ns)
 (MessageGroupId=sid)    ├── pulls ECR image adp-agent-gateway:<tag>
 (MessageDedupId=tid)    │   (TODAY: Python sqs_consumer.py)
                         │   (NEW:   TS complex-task-chat-agent.ts)
                         │
                         ├── env: INPUT_QUEUE_URL / RESPONSE_QUEUE_URL
                         │        CONTEXT_TABLE            (new)
                         │        ARTIFACTS_TABLE          (new)
                         │        ARTIFACTS_BUCKET         (new)
                         │        MEMORY_TABLE             (new)
                         │        CONTEXT_STRATEGY=lcm     (new)
                         │        MEMORY_STRATEGY=dynamo   (new)
                         │        ANTHROPIC_MODEL / LCM_SUMMARY_MODEL
                         │
                         └── IRSA: adp-agent role, extended with
                                   bedrock:InvokeModel (Sonnet 4.6),
                                   dynamodb:* on context/artifacts/memory tables,
                                   s3:{Put,Get,Delete}Object on artifacts bucket
```

- **FIFO queue** serializes per-session turns (`MessageGroupId = session_id`); different sessions run in parallel.
- Existing worker's ServiceAccount + KEDA trigger are reconfigured to the FIFO queue; Docker base stays the same. Net change: (1) queue flips standard→FIFO, (2) binary inside the pod, (3) three new DDB tables + one S3 bucket, (4) IRSA permission extension, (5) session-sweeper Lambda on the context table's TTL stream.

---

## 3. Architecture overview

```
┌────────────────────────────────────────────────────────────────────┐
│  complex-task-chat-agent.ts  (entrypoint)                          │
│                                                                     │
│    for each SQS message:                                           │
│      task = parse(msg)                                             │
│      reply = await runTurn(task)                                   │
│      send_response(reply); delete_message(msg)                     │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│  runTurn(task)                                                     │
│                                                                     │
│   1. persona   = loadBasePersona(agent_type)                       │
│                  (from /app/personas/<type>.md baked in image)     │
│   2. memBlock  = await memory.retrieve({                           │
│                    query, scope:{user, component, persona}})       │
│                  (returns persona-learnings + user/component mems) │
│   3. ctx       = await context.assemble({sessionId, userMsg,       │
│                                          tokenBudget})             │
│   4. messages  = buildPrompt(persona.base, memBlock, ctx.messages, │
│                              userMsg)                              │
│   5. tools     = [...base, ...context.tools(), ...memory.tools()]  │
│                  (memory.tools includes save_learning)             │
│   6. result    = await runQuery({messages, tools, model})          │
│   7. await context.record({sessionId, userMsg, assistantReply})    │
│   8. (Phase 2+) async extractor: promote draft-learnings          │
│   9. return result                                                 │
└──────────────┬───────────────────────────────┬─────────────────────┘
               │                               │
               ▼                               ▼
    ┌────────────────────┐          ┌──────────────────────┐
    │  ContextManager    │          │  MemoryProvider      │
    │  (port)            │          │  (port)              │
    ├────────────────────┤          ├──────────────────────┤
    │  LcmContext        │          │  NullMemoryProvider  │
    │   ├── Store        │          │  DynamoMemoryProvider│
    │   ├── Summarizer   │          │  RouterMemoryProvider│
    │   ├── Eviction     │          │  ReadOnlyGitBranch…  │
    │   └── TokenEstim.  │          │  AgentCoreMemory…    │
    └────────────────────┘          └──────────────────────┘
            │                                  │
            ▼                                  ▼
    DynamoDB: adp-chat-context       DynamoDB: adp-agent-memory
    Bedrock:  summary model           (later) AgentCore / OpenSearch
```

Key invariant: **the agent orchestrator never depends on a concrete context or memory implementation**. Everything is wired at startup by a factory reading env vars.

---

## 4. Directory layout

```
modules/agent-factory/agent/src/
├── index.ts                          # existing — ARC/GitHub entrypoint
├── agent-worker.ts                   # existing — unchanged
├── startup.sh                        # dispatches on $AGENT_ENTRYPOINT
│
└── complex-task-chat/                # NEW
    ├── complex-task-chat-agent.ts    # entrypoint (SQS loop + runTurn)
    ├── run-query.ts                  # extracted-from-agent-worker stream loop
    ├── persona-loader.ts             # TS port of Python loader
    ├── sqs-client.ts                 # receive/send/delete helpers
    │
    ├── context/                      # ContextManager port + impls
    │   ├── types.ts                  # ContextManager, ResolvedItem, ports
    │   ├── factory.ts                # buildContextManager(env) → ContextManager
    │   ├── lcm/
    │   │   ├── lcm-context.ts        # LcmContext class (composes ports below)
    │   │   ├── assembler.ts          # context_items → messages[]
    │   │   ├── compactor.ts          # leaf (+ future condensed) summarization
    │   │   ├── summary-format.ts     # XML wrapper for summary messages
    │   │   ├── expand-summary.ts     # agent tool: expand_summary(id)
    │   │   └── config.ts             # budgets, fresh tail size, chunk sizes
    │   ├── store/
    │   │   ├── port.ts               # ContextStore interface
    │   │   └── dynamo-store.ts       # DynamoDB impl
    │   ├── summarize/
    │   │   ├── port.ts               # Summarizer interface
    │   │   └── bedrock-summarizer.ts # 3-level escalation via Bedrock
    │   ├── eviction/
    │   │   ├── port.ts               # EvictionPolicy interface
    │   │   └── chronological.ts      # keep-newest, drop-oldest
    │   └── tokens/
    │       ├── port.ts               # TokenEstimator interface
    │       └── char-estimator.ts     # ~4 chars/token heuristic
    │
    └── memory/                       # MemoryProvider port + impls
        ├── types.ts                  # MemoryProvider, MemoryRecord, etc.
        ├── factory.ts                # buildMemoryProvider(env) → MemoryProvider
        ├── null-memory.ts            # no-op (default)
        ├── dynamo-memory.ts          # adp-agent-memory table impl
        ├── router-memory.ts          # fan-out decorator
        ├── caching-memory.ts         # LRU decorator
        └── tools.ts                  # agent tools: recall_memory, save_fact
```

Files under `context/` and `memory/` are each self-contained — they do not import from each other or from `complex-task-chat-agent.ts`. The orchestrator is the only composition point.

---

## 5. Chat agent orchestrator

`complex-task-chat-agent.ts` — ~80 lines:

```ts
import { buildContextManager } from "./context/factory";
import { buildMemoryProvider } from "./memory/factory";
import { loadPersona } from "./persona-loader";
import { runQuery } from "./run-query";
import { SqsClient } from "./sqs-client";
import { baseTools } from "./base-tools";

const TOKEN_BUDGET = Number(process.env.CONTEXT_TOKEN_BUDGET ?? 150_000);

async function main() {
  const context = buildContextManager();
  const memory  = buildMemoryProvider();
  const sqs     = new SqsClient();

  // KEDA ScaledJob: process one message and exit.
  const messages = await sqs.receive();
  if (messages.length === 0) { console.log("no messages"); return; }

  for (const msg of messages) await processOne(msg, { context, memory, sqs });
}

async function processOne(msg, { context, memory, sqs }) {
  const task = JSON.parse(msg.Body);
  const { task_id, session_id, message, agent_type, user_id, tenant_id } = task;

  try {
    // Defense-in-depth ownership check: gateway ingest already enforced this,
    // but re-verify so a leaked/forged SQS message can't access another user's session.
    await context.assertOwnership(session_id, user_id);

    const scope = {
      user: user_id,                          // Cognito sub (or m2m:<client_id>)
      tenant: tenant_id,                      // optional, from custom:tenant_id claim
      component: task.component,
      persona: agent_type,                    // enables persona-learning retrieval
    };

    const persona = await loadPersona(agent_type, {
      memory, query: message, tokenBudget: 500,
    });  // returns { baseSystemPrompt, learnings } — learnings come from memory

    // Separate retrieval for user/component memories (distinct from persona learnings
    // so they can be rendered in different prompt blocks).
    const memBlock = await memory.retrieve({
      query: message,
      scope: { user: scope.user, component: scope.component },
      tokenBudget: 500,
      kinds: ["preference", "fact"],
    });

    const systemPrompt = composeSystemPrompt({
      base: persona.baseSystemPrompt,
      personaLearnings: persona.learnings,
      memories: memBlock,
    });
    const systemTokens = estimateTokens(systemPrompt);
    const ctx = await context.assemble({
      sessionId: session_id, userMessage: message,
      tokenBudget: TOKEN_BUDGET - systemTokens,
    });

    const tools = [...baseTools, ...context.tools(), ...memory.tools()];
    // memory.tools() includes save_learning — agent can durably record insights

    const result = await runQuery({
      systemPrompt,
      messages: [...ctx.messages, { role: "user", content: message }],
      tools,
      model: persona.modelOverride ?? process.env.ANTHROPIC_MODEL,
      cwd: "/tmp/workspace",
    });

    await context.record({
      sessionId: session_id,
      userMessage: { role: "user", content: message },
      assistantMessage: { role: "assistant", content: result.text },
    });

    await sqs.sendResponse({ task_id, session_id, text: result.text,
                              tokens: result.tokens, status: "completed" });
    await sqs.deleteMessage(msg.ReceiptHandle);
  } catch (err) {
    await sqs.sendResponse({ task_id, session_id, text: `error: ${err.message}`,
                              status: "failed" });
    // do not delete — DLQ policy applies
    throw err;
  }
}

main().catch(e => { console.error(e); process.exit(1); });
```

**This is the only file that knows about both context and memory.** Neither imports the other.

`runQuery` is extracted from the existing `agent-worker.ts` stream loop (around lines 846-987) minus the GitHub/Beads/memory-branch concerns — it keeps `resilientQuery`, heartbeat, post-completion force-exit, turn logging, and result collection. Both entrypoints import it.

---

## 6. Personas (base + learnings)

A persona has two layers with different operational properties:

| Layer | What it is | Changes | Author | Source of truth |
|---|---|---|---|---|
| **Base persona** | Identity, mindset, tone, role-specific instructions. "You are @agent-developer. Follow the ADP workflow. Your job is…" | Rare (PR review) | Humans | Repo → baked into image |
| **Persona learnings** | Accumulated experience. "When deploying the gateway, health endpoint is `/api/gateway/health`. Skip `terraform apply` on agent-factory until PR #11 imports are done." | Frequent, per-run | Agents themselves (Phase 1) + async extractor (Phase 2) | `adp-agent-memory` DynamoDB |

**Important:** Persona learnings ride entirely on the `MemoryProvider` port — they are not a separate subsystem. They are memory records scoped by `persona`.

### 6.1 Composition at turn time

The orchestrator composes the final system prompt as:

```xml
<persona>
  <!-- base persona text, read from /app/personas/<agent_type>.md -->
</persona>

<persona-learnings>
  <!-- top-N learnings for this persona, retrieved from MemoryProvider -->
  <!-- bounded by memory budget (default ~500 tokens) -->
</persona-learnings>

<memories>
  <!-- user/component memories, also via MemoryProvider -->
</memories>
```

### 6.2 Base persona loading

Base personas are baked into the Docker image from `modules/agent-factory/rules/personas/<type>.md`. Rationale:

- Personas are configuration, not content — they change on the order of weeks, via PR review.
- Zero runtime dependencies (no S3 call, no ConfigMap mount) — matters for KEDA cold-start latency.
- Rollback of the agent image rolls back the persona set it was tested against.
- Single source of truth: git.

Loader shape (sketch):

```ts
// persona-loader.ts
export interface ComposedPersona {
  name: string;
  baseSystemPrompt: string;       // from disk
  learnings: MemoryRecord[];      // from MemoryProvider, at call time
  modelOverride?: string;
}

export async function loadPersona(
  name: string,
  deps: { memory: MemoryProvider; query: string; tokenBudget: number }
): Promise<ComposedPersona> {
  const baseSystemPrompt = fs.readFileSync(`${PERSONAS_DIR}/${name}.md`, "utf-8");
  const learnings = await deps.memory.retrieve({
    query: deps.query,
    scope: { persona: name },
    tokenBudget: deps.tokenBudget,
    kinds: ["learning"],
  });
  return { name, baseSystemPrompt, learnings };
}
```

Rendering into the final system prompt is the orchestrator's job — the loader just assembles the data.

### 6.3 Writing learnings

Two paths, phase-gated:

1. **Agent-authored (Phase 1).** The agent calls `save_learning(content, scope)` via a tool (exposed by `MemoryProvider.tools()`). Happens inline during a turn when the agent decides something is durable. Default `scope.persona = <current agent_type>`; agent can add `component`/`user`.
2. **Post-turn extraction (Phase 2+).** A separate process reads completed session transcripts and extracts candidate learnings. This is the AgentCore `SUMMARY`/`EPISODIC` pattern — bolt-on without changing the port.

### 6.4 Quality controls (phased)

Risks with agent-authored learnings: noise, incorrect facts treated as ground truth, unbounded growth. Mitigations (all additive):

- **Token budget cap on injection** (default ~500 tokens into prompt).
- **TTL on learning records** (default 90 days — old facts decay).
- **`kind="learning"` + `source.agent`** metadata for audit.
- **Draft promotion flow** (later) — agents write `kind="draft-learning"`; a review step (human or automated) promotes to `kind="learning"` before it's retrievable for injection.
- **Semantic retrieval** (later) — when `OpenSearchMemoryProvider` lands, learnings get embedded at write time so retrieval is relevance-ranked, not keyword-matched.

Phase 1 ships with: token cap + TTL + agent/source attribution. The rest are follow-ups.

### 6.5 Cross-agent benefit

Because persona learnings live in the shared `adp-agent-memory` table, a learning written by the GH `operations` agent is **immediately** retrievable by a chat `operations` agent (and vice versa). Same port, same scope. No migration, no coordination. This is a key reason for not building persona learnings as a dedicated subsystem.

---

## 7. Artifacts

The ephemeral `/tmp/workspace` is the agent's scratchpad. Anything the agent produces for the user — presentations, PDFs, CSVs, images, generated code bundles — must be lifted out of scratch before the pod exits. That is what the `ArtifactStore` port is for. **Ephemeral storage for thinking, durable artifact store for delivery and iteration.**

### 7.1 Public port

```ts
// artifacts/port.ts
export interface ArtifactStore {
  publish(input: {
    sessionId: string;
    taskId?: string;
    localPath: string;
    filename?: string;
    contentType?: string;
    ttl?: number;                    // seconds; default 30 days
    supersedes?: string;             // artifact id that this replaces (lineage)
    source?: "agent" | "user";       // default "agent"
  }): Promise<ArtifactRef>;

  fetch(artifactId: string, destPath: string): Promise<void>;

  listBySession(sessionId: string, filter?: {
    contentType?: string;
    filename?: string;
    limit?: number;
  }): Promise<ArtifactRef[]>;

  tools(): AgentTool[];
}

export interface ArtifactRef {
  id: string;                        // opaque (e.g. art_01HX...)
  url: string;                       // pre-signed GET URL
  urlExpiresAt: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  checksum: string;                  // sha256
  createdAt: string;
  supersedes?: string;               // artifact id this replaces, if any
  source: "agent" | "user";
}
```

### 7.2 Phase 1 implementation: `S3ArtifactStore`

- Bucket: `adp-<env>-chat-artifacts-<account>`
- Keying: `s3://<bucket>/<sessionId>/<taskId>/<filename>` — stable per-turn URL
- Lifecycle policy: 30-day expiration (configurable per upload via `ttl`)
- **TTL auto-extend:** on `publish`, the effective TTL is `max(requestedTtl ?? 30d, sessionTtl - now)`. Artifacts within a live session never expire before the session itself. Implemented via per-object `x-amz-expiration` overrides (or a Lambda that bumps the object's expiration when the session header refreshes).
- Delivery: pre-signed `GET` URLs with default 7-day expiry (shorter than object TTL)
- Catalog: DynamoDB table `adp-<env>-chat-artifacts`
  - PK: `session#<sid>`, SK: `art#<createdAt>#<id>`
  - Attributes mirror `ArtifactRef` + `s3Key` + `ttl` (matches S3 expiration)
  - GSI-1 on `id` for `fetch(id, ...)` without knowing session
  - Catalog row TTL matches the S3 object's expiry so metadata and object disappear together
- Session cleanup: when the session-header sweeper Lambda (§8.8) fires, it also batch-deletes this session's artifact catalog rows and issues an S3 `DeleteObjects` for the session prefix.
- Upload mechanism: `PutObject` in Phase 1. S3 Files (Mountpoint for S3) is a later swap candidate — same `ArtifactStore` port, different internals. Rejected for Phase 1 because small-file `PutObject` is simpler and cheaper for typical artifact sizes.

### 7.3 Agent-facing tools

- `publish_artifact(path, filename?, contentType?, supersedes?)` — upload and return `ArtifactRef`
- `fetch_artifact(id, destPath)` — pull a prior artifact back to local workspace for reuse/editing
- `list_artifacts(filter?)` — enumerate artifacts for the current session

These are registered via `ArtifactStore.tools()` and merged into the agent's tool set by the orchestrator.

### 7.4 Back-and-forth editing flow

Canonical sequence when the user asks the agent to iterate on an earlier output:

```
Turn N   (pod A):  write deck.pptx → publish_artifact → art_1 returned in reply
                    (pod A exits; /tmp gone)

Turn N+1 (pod B):  LCM.assemble() returns prior messages including art_1 ref
                    fetch_artifact("art_1", "./deck.pptx")
                    edit deck.pptx
                    publish_artifact("./deck.pptx", supersedes: "art_1") → art_2
                    (pod B exits)

Turn N+2 (pod C):  LCM returns messages including both art_1 and art_2
                    fetch_artifact("art_2", ...)  → latest version
                    edit, publish → art_3 (supersedes: art_2)
```

Every artifact is durable for its TTL; the agent can always walk backward by id.

### 7.5 User-uploaded attachments (inbound)

Uploads from the chat UI follow the same catalog, tagged `source: "user"`:

- UI requests a pre-signed `PUT` from the gateway.
- UI uploads directly to `s3://<bucket>/<sid>/<taskId>/<filename>` (or to an inbox prefix).
- Gateway writes an `ArtifactRef` to the catalog with `source: "user"`.
- SQS task payload carries `attachments: [artifactId, ...]`.
- Agent sees the refs in its prompt and calls `fetch_artifact` to pull bytes into `/tmp/workspace`.

Same port, same catalog, one more attribute.

### 7.6 Guardrails

1. **LCM summary preservation rule.** The compaction summary prompt explicitly says: *"Preserve artifact IDs (`art_*`) and URLs verbatim; never paraphrase them."* Prevents reference loss when older turns are summarized.
2. **Catalog survives compaction.** The artifact catalog lives outside LCM. `list_artifacts()` always returns the full session history even if conversation turns have been compacted.
3. **Post-turn consistency check** (orchestrator). If the assistant reply contains delivery language ("here's the file", "download", "attached", "updated version") and no artifact was published this turn, log a warning and optionally append a note to the response. Prevents silent "I made it!" replies with nothing attached.
4. **Per-session quota** (soft, configurable). Cap live artifacts per session (e.g. 100, LRU evict) to bound growth.
5. **Shared filename convention.** `publish_artifact` defaults to using the local filename; the S3 key embeds `taskId` so each version gets a distinct URL while the logical name stays stable.

### 7.7 What this is NOT

- **Not a collaborative editor.** Real-time co-editing (CRDT, OT) is out of scope.
- **Not a workspace persistence layer.** The workspace (`/tmp/workspace`) remains ephemeral. Cross-turn persistence is achieved via the artifact catalog + LCM's preserved conversation, not via a mounted filesystem.
- **Not a general-purpose file service.** The store is scoped to chat sessions and their artifacts; broader document management belongs elsewhere.

---

## 8. Context Manager (LCM)

### 8.1 Public port

```ts
// context/types.ts
export interface ContextManager {
  assemble(input: {
    sessionId: string;
    userMessage: string;
    tokenBudget: number;
  }): Promise<{ messages: SDKMessage[]; meta: AssemblyMeta }>;

  record(input: {
    sessionId: string;
    userMessage: SDKMessage;
    assistantMessage: SDKMessage;
    userId: string;                // written to header on session create
    tenantId?: string;
  }): Promise<void>;

  /**
   * Throws if the session exists and is owned by a different user.
   * On first call (no header yet), creates the header with ownerUserId.
   */
  assertOwnership(sessionId: string, userId: string): Promise<void>;

  tools(): AgentTool[];
}

export interface AssemblyMeta {
  rawMessageCount: number;
  summaryCount: number;
  estimatedTokens: number;
  compactionTriggered: boolean;
}
```

### 8.2 Internal ports (composable)

```ts
// context/store/port.ts
export interface ContextStore {
  appendMessage(sessionId: string, msg: StoredMessage): Promise<string>;     // → messageId
  appendSummary(sessionId: string, sum: StoredSummary): Promise<string>;     // → summaryId
  readContextItems(sessionId: string): Promise<ContextItem[]>;
  replaceRange(sessionId: string, fromOrd: number, toOrd: number,
               replacement: { type: "summary"; summaryId: string }): Promise<void>;
  getMessagesByIds(ids: string[]): Promise<StoredMessage[]>;
  getSummaryById(id: string): Promise<StoredSummary | null>;
}

// context/summarize/port.ts
export interface Summarizer {
  summarize(input: {
    text: string;
    mode: "normal" | "aggressive" | "truncate";
    previousSummary?: string;
    targetTokens: number;
  }): Promise<string>;
}

// context/eviction/port.ts
export interface EvictionPolicy {
  pick(evictable: ResolvedItem[], budget: number, prompt: string): ResolvedItem[];
}

// context/tokens/port.ts
export interface TokenEstimator { count(text: string): number; }
```

### 8.3 LcmContext composition

```ts
export class LcmContext implements ContextManager {
  constructor(private deps: {
    store: ContextStore;
    summarizer: Summarizer;
    evictor: EvictionPolicy;
    tokens: TokenEstimator;
    config: LcmConfig;
  }) {}
  // assemble(), record(), tools() implementations...
}
```

Default config:

```ts
export interface LcmConfig {
  freshTailCount: number;       // 16
  leafChunkTokens: number;      // 20_000
  leafTargetTokens: number;     // 1200
  summaryTimeoutMs: number;     // 60_000
  maxTurnsPerCompaction: number;// 1 (Phase 1: leaf-only, one pass per turn)
  summaryModel: string;         // env LCM_SUMMARY_MODEL, default "global.anthropic.claude-sonnet-4-6"
  summaryEndpoint: "bedrock" | "gateway";  // env LCM_SUMMARY_ENDPOINT, default "bedrock"
  // Phase 2+ knobs
  incrementalMaxDepth: number;  // 0 in Phase 1, 1 in Phase 2
  leafMinFanout: number;        // 8 (used when incrementalMaxDepth > 0)
  condensedMinFanout: number;   // 4
  condensedTargetTokens: number;// 2000
}
```

### 8.4 Assembly algorithm (Phase 1)

```
assemble(sessionId, userMessage, tokenBudget):
  items   = store.readContextItems(sessionId)          # ordered list
  resolved = resolveAll(items)                          # msg parts OR summary XML wrapper
  freshTail, evictable = splitByTail(resolved, freshTailCount)
  tailTokens = sum(freshTail.tokens)                    # always included
  remaining  = tokenBudget - tailTokens - userMessageTokens
  kept       = evictor.pick(evictable, remaining, userMessage)
  messages   = sanitizeToolUsePairing([...kept, ...freshTail].map(r => r.message))
  return { messages, meta }
```

`splitByTail` protects the last `freshTailCount` raw messages from eviction (summaries in that tail are counted toward eviction since they're not raw turns). `sanitizeToolUsePairing` ensures every `tool_result` has its `tool_use`; drop orphans.

### 8.5 Compaction algorithm (Phase 1 — leaf only)

Runs inside `record(...)` *after* the new messages are appended:

```
record(sessionId, userMessage, assistantMessage):
  store.appendMessage(sessionId, user)
  store.appendMessage(sessionId, assistant)
  maybeCompact(sessionId)

maybeCompact(sessionId):
  items = store.readContextItems(sessionId)
  candidate = oldestContiguousMessages(items, freshTailCount, leafChunkTokens)
  if tokens(candidate) < leafChunkTokens: return
  text = concatenateWithTimestamps(candidate)
  summary = summarizer.summarize({ text, mode: "normal", targetTokens: leafTargetTokens })
  if tokens(summary) >= tokens(text):
    summary = summarizer.summarize({ text, mode: "aggressive", targetTokens: leafTargetTokens/2 })
  if tokens(summary) >= tokens(text):
    summary = deterministicTruncate(text, ~512 tokens)
  summaryId = store.appendSummary(sessionId, { depth: 0, kind: "leaf",
                                                content: summary,
                                                sourceIds: candidate.map(c => c.messageId),
                                                earliestAt, latestAt })
  store.replaceRange(sessionId, candidate[0].ordinal, last.ordinal,
                     { type: "summary", summaryId })
```

Compaction is **inline and best-effort**: a summarization failure does not block the response flow. The next turn will retry.

**Summary prompt preservation rules** (added to `Summarizer` system prompt):
- Preserve artifact IDs (`art_*`) and pre-signed URLs verbatim — never paraphrase.
- Preserve tool-call structure: summarize intent, keep identifiers (file paths, command names) verbatim.
- Preserve error strings and exact command outputs when they establish state.

### 8.6 Summary XML wrapper (agent-facing injection format)

```xml
<summary id="sum_a1b2c3" kind="leaf" depth="0" descendant_count="0"
         earliest_at="2026-04-19T09:12:00Z" latest_at="2026-04-19T09:48:00Z">
  <content>
    ...summary text with timestamps...
    Expand for details about: exact commands, error strings, intermediate state
  </content>
</summary>
```

Injected as a synthetic `user` message. Gives the model enough metadata to reason about scope and call `expand_summary(id)` when needed.

### 8.7 Tools exposed to the agent

`expand_summary(summaryId)`:

```json
{
  "name": "expand_summary",
  "description": "Fetch the raw source messages that a summary was built from. Use when a summary is too compressed for the current task.",
  "input_schema": {
    "type": "object",
    "required": ["summary_id"],
    "properties": {
      "summary_id": { "type": "string", "description": "e.g. sum_a1b2c3" }
    }
  }
}
```

Handler: `store.getSummaryById(id)` → returns the source message IDs → `store.getMessagesByIds(ids)` → renders as a fenced block citing `earliest_at/latest_at`.

### 8.8 DynamoDB schema — `adp-chat-context`

Single-table, discriminated by `SK` prefix:

| PK | SK | Item | Attributes |
|---|---|---|---|
| `session#<sid>` | `header` | session header | `lastActivityAt`, `ttl` (epoch seconds), `createdAt`, `status`, `ownerUserId`, `tenantId?` |
| `session#<sid>` | `item#<ord:08>` | context item | `type` (`msg`\|`sum`), `ref` (messageId or summaryId), `ordinal` |
| `session#<sid>` | `msg#<messageId>` | raw message | `role`, `content`, `parts` (JSON), `ts`, `tokens` |
| `session#<sid>` | `sum#<summaryId>` | summary record | `depth`, `kind`, `content`, `sourceIds`, `parentIds?`, `earliestAt`, `latestAt`, `tokens` |

**Access patterns:**
1. `readContextItems(sid)`: `Query PK=session#<sid> AND begins_with(SK, "item#")` — returns ordinals in SK order.
2. `getMessagesByIds(ids)`: `BatchGetItem` with `(PK=session#<sid>, SK=msg#<id>)` pairs.
3. `getSummaryById(id)`: `GetItem` — but we don't always know `sid` from just `id`. Solution: embed the session id inside the summary id: `sum_<sid>_<hash>`. Parse to locate PK. (Alternative: GSI on `summaryId` — adds cost, skipped.)
4. `replaceRange(from, to, ref)`: `TransactWriteItems` deletes `item#<from..to>` and writes a new `item#<from>` pointing at the summary. `ordinal` of the new item = `from` (chosen to preserve chronological order).

**Session header + TTL semantics (Option C):**
- DynamoDB TTL attribute is configured on the table, keyed on the `ttl` attribute.
- Only the `header` row carries a `ttl`. Child rows (`item#*`, `msg#*`, `sum#*`) never expire on their own.
- On every write to a session, the orchestrator performs an `UpdateItem` on the header: `SET lastActivityAt = now, ttl = now + 90 * 86400`. One extra write per turn — cheap.
- When DynamoDB expires the header, a Lambda subscribed to the table's TTL stream receives the delete event, `Query`s `PK=session#<sid>`, and `BatchDelete`s all remaining rows for that session plus the matching artifact catalog rows and S3 artifact objects (session cleanup is one fan-out per expiry).
- Read-path check (defensive): the assembler treats a session as absent if the header row is missing, even if child rows transiently remain during sweeper lag.

Provisioning: on-demand. Expected per-session size: small (~hundreds of items, ~1 MB).

---

## 9. Memory Provider

### 9.1 Public port (stays small and deliberately open)

```ts
export interface MemoryRecord {
  id: string;                      // opaque — impl-defined format
  content: string;                 // natural-language fact/preference/learning
  scope: MemoryScope;
  kind?: string;                   // "preference" | "fact" | "learning" | "episodic" | ...
  tags?: string[];
  source?: { agent?: string; sessionId?: string };
  createdAt: string;
  updatedAt?: string;
  metadata?: Record<string, unknown>;
}

export interface MemoryScope {
  user?: string;
  component?: string;
  tenant?: string;
  persona?: string;                // "developer" | "operations" | "reviewer" | ...
  // intentionally open — impls can index any subset
}

export interface MemoryQuery {
  query: string;
  scope?: MemoryScope;
  limit?: number;
  tokenBudget?: number;
  kinds?: string[];
}

export interface MemoryProvider {
  retrieve(input: MemoryQuery): Promise<MemoryRecord[]>;
  save(record: Omit<MemoryRecord, "id" | "createdAt">): Promise<MemoryRecord>;
  delete?(id: string): Promise<void>;
  tools(): AgentTool[];
  capabilities(): MemoryCapabilities;
}

export interface MemoryCapabilities {
  semanticSearch: boolean;
  keywordSearch: boolean;
  tagFiltering: boolean;
  scoping: Array<keyof MemoryScope>;
  delete: boolean;
  asyncExtraction: boolean;
  ttl: boolean;
}
```

**Key constraints:**
- `MemoryRecord.id` is opaque — impl defines format.
- `MemoryRecord.content` is plain text — works across every backend.
- `MemoryRecord.scope` is an open bag; impls ignore unsupported keys.
- `kind` / `tags` / `metadata` are free-form — impls can index or ignore.
- `capabilities()` is the feature-discovery escape hatch so the agent (or a router) can degrade gracefully.
- **`scope.persona`** is how persona learnings are kept: a memory record scoped to `{persona: "operations"}` is a persona learning. No separate subsystem.

### 9.2 Phase 1 implementations

- **`NullMemoryProvider`** — always empty, no tools, all capabilities `false`. Default for environments where memory is disabled.
- **`DynamoMemoryProvider`** — writes/reads `adp-agent-memory` table (schema below). Keyword substring filter over `content`, scope-indexed via GSI. No embeddings.

### 9.3 Tools the provider exposes

Depending on `capabilities()`, a `DynamoMemoryProvider` might expose:

- `recall_memory(query, scope?)` — retrieves top-N matching records.
- `save_fact(content, scope, kind?)` — agent-triggered write.
- `save_preference(content, user)` — convenience wrapper with scope fixed to user.
- `save_learning(content, scope?)` — convenience wrapper; defaults `scope.persona` to the current agent's persona and `kind="learning"`. This is how the agent accumulates persona-scoped experience across runs.

`tools()` returns only the tools the impl can honor. `NullMemoryProvider.tools()` returns `[]`.

### 9.4 Default agent-side prompt injection

The orchestrator retrieves memories scoped to `{user, component, persona}` and renders them into two prompt blocks:

```xml
<persona-learnings>
  <memory id="mem_..." scope="persona:operations" kind="learning" updated="2026-04-10">
    EKS cluster name is adp-dev-eks-cluster (with -cluster suffix). Scripts
    using "adp-dev-eks" are broken.
  </memory>
  <memory id="mem_..." scope="persona:operations" kind="learning" updated="2026-03-28">
    Gateway health endpoint: /api/gateway/health, served by svc bedrockgateway
    in ns adp-gateway.
  </memory>
</persona-learnings>

<memories>
  <memory id="mem_..." scope="user:pranav" kind="preference" updated="2026-04-14">
    Prefers real-time updates during long-running tasks.
  </memory>
  <memory id="mem_..." scope="component:gateway" kind="fact" updated="2026-03-28">
    Gateway deployed in ns adp-gateway; RDS db bedrockgw-dev-postgres.
  </memory>
</memories>
```

Both blocks together are capped at ~500 tokens by default (configurable). Agent can call `recall_memory(query, scope)` if more is needed.

### 9.5 DynamoDB schema — `adp-agent-memory`

| PK | SK | Item |
|---|---|---|
| `scope#<type>#<value>` | `mem#<createdAt>#<recordId>` | record |

e.g. `PK=scope#user#pranav`, `SK=mem#2026-04-19T10:00:00Z#01HX...`

**Attributes:** `content`, `kind`, `tags` (list), `source` (map), `updatedAt`, `metadata` (map), optional `ttl`.

**GSI-1:** `PK=id`, `SK=recordId` — for `delete(id)` without knowing scope.
**GSI-2 (optional, Phase 2):** `PK=scope#<type>#<value>`, `SK=kind#<kind>#<ts>` — for kind-filtered retrieval.

Access patterns:
- `retrieve(query, scope)`: query by scope PK, newest-first; apply substring filter over `content`; cap at `limit`.
- `save(record)`: `PutItem`. TTL applied per kind (table below).
- `delete(id)`: `GetItem` via GSI-1, then `DeleteItem`.

**Per-kind TTL policy** (applied at `save` time, extended on read/re-save):

| `kind`           | Default TTL | Extend on read? | Rationale |
|------------------|-------------|-----------------|-----------|
| `preference`     | **none**    | n/a             | User prefs persist until changed by the user |
| `fact`           | 180 days    | yes             | Component-level facts get refreshed by use |
| `learning`       | 90 days     | yes             | Cold learnings decay; hot ones stay |
| `draft-learning` | 14 days     | no              | Auto-expire if review loop doesn't promote |

`retrieve` MAY bump the `ttl` of returned records to `now + defaultTtl` when `extend on read` is true; this keeps heavily-used knowledge alive without explicit writes. DynamoDB TTL is configured on the table and honors the `ttl` attribute on each row.

### 9.6 Decorators (no impl changes to compose)

```ts
const memory =
  new LoggingMemoryProvider(
    new CachingMemoryProvider(
      new RouterMemoryProvider([
        new DynamoMemoryProvider(ddb, "adp-agent-memory"),
        // future: new ReadOnlyGitBranchMemoryProvider(adpBranchReader),
      ])
    )
  );
```

`RouterMemoryProvider`:
- `retrieve()` → fan-out to children, merge (dedup by `id`, cap at `limit`).
- `save()` → writes to a single designated child (e.g. first in list).
- `capabilities()` → union of children's.
- `tools()` → union of children's tools.

---

## 10. Infrastructure changes

### 10.1 New DynamoDB tables (Terraform)

Location: `modules/agent-factory/infra/` (the module that already owns chat-agent infra).

```hcl
resource "aws_dynamodb_table" "chat_context" {
  name         = "adp-${var.environment}-chat-context"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"
  attribute { name = "PK" type = "S" }
  attribute { name = "SK" type = "S" }
  ttl { attribute_name = "ttl" enabled = true }  # only header rows carry ttl
  stream_enabled   = true
  stream_view_type = "OLD_IMAGE"                 # sweeper Lambda subscribes
  point_in_time_recovery { enabled = true }
}

resource "aws_dynamodb_table" "chat_artifacts" {
  name         = "adp-${var.environment}-chat-artifacts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"
  attribute { name = "PK" type = "S" }
  attribute { name = "SK" type = "S" }
  attribute { name = "id" type = "S" }
  ttl { attribute_name = "ttl" enabled = true }  # catalog row expires with object
  global_secondary_index {
    name            = "by-id"
    hash_key        = "id"
    projection_type = "ALL"
  }
  point_in_time_recovery { enabled = true }
}

resource "aws_dynamodb_table" "agent_memory" {
  name         = "adp-${var.environment}-agent-memory"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"
  attribute { name = "PK" type = "S" }
  attribute { name = "SK" type = "S" }
  attribute { name = "id" type = "S" }
  ttl { attribute_name = "ttl" enabled = true }   # per-kind ttl per §9.5
  global_secondary_index {
    name            = "by-id"
    hash_key        = "id"
    projection_type = "ALL"
  }
  point_in_time_recovery { enabled = true }
}

resource "aws_s3_bucket" "chat_artifacts" {
  bucket = "adp-${var.environment}-chat-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_lifecycle_configuration" "chat_artifacts" {
  bucket = aws_s3_bucket.chat_artifacts.id
  rule {
    id     = "default-30-day-expiry"
    status = "Enabled"
    expiration { days = 30 }
  }
}

# Sweeper: subscribes to chat_context DDB TTL stream; on session-header delete,
# batch-deletes all child rows + artifact catalog rows + S3 artifact prefix.
resource "aws_lambda_function" "session_sweeper" {
  function_name = "adp-${var.environment}-chat-session-sweeper"
  runtime       = "nodejs22.x"
  handler       = "index.handler"
  role          = aws_iam_role.session_sweeper.arn
  timeout       = 60
  # source packaged from modules/agent-factory/agent/dist/sweepers/session-sweeper.js
}

resource "aws_lambda_event_source_mapping" "session_sweeper" {
  event_source_arn  = aws_dynamodb_table.chat_context.stream_arn
  function_name     = aws_lambda_function.session_sweeper.arn
  starting_position = "LATEST"
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["REMOVE"]
        dynamodb  = { Keys = { SK = { S = ["header"] } } }  # only header removals
      })
    }
  }
}
```

### 10.2 IAM additions to `adp-agent` IRSA role

- `dynamodb:PutItem / GetItem / UpdateItem / Query / BatchGetItem / BatchWriteItem / TransactWriteItems` on `chat_context`, `chat_artifacts`, `agent_memory` tables (and `by-id` GSIs).
- `s3:PutObject / GetObject / DeleteObject / ListBucket` scoped to the `chat_artifacts` bucket ARN.
- `bedrock:InvokeModel` on the Sonnet 4.6 model ARN (Phase 1: used for both main turns and LCM summarization).

Session sweeper Lambda gets its own IRSA-equivalent role with narrower scope: `dynamodb:Query/BatchWriteItem` on the three tables + `s3:DeleteObject/ListBucket` on the artifact bucket. No Bedrock, no SQS.

### 10.3 Task queue: FIFO conversion

The existing `adp-<env>-agent-gateway-tasks` standard queue is replaced by `adp-<env>-agent-gateway-tasks.fifo`. The queue and its DLQ sibling (`adp-<env>-agent-gateway-dlq.fifo`) are both FIFO. Rationale and full flow in Open Q5 (§14).

```hcl
resource "aws_sqs_queue" "agent_gateway_tasks" {
  name                        = "adp-${var.environment}-agent-gateway-tasks.fifo"
  fifo_queue                  = true
  content_based_deduplication = false   # we send explicit MessageDeduplicationId
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"
  visibility_timeout_seconds  = 900     # matches activeDeadlineSeconds of the ScaledJob
  message_retention_seconds   = 345600  # 4 days
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.agent_gateway_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "agent_gateway_dlq" {
  name       = "adp-${var.environment}-agent-gateway-dlq.fifo"
  fifo_queue = true
}
```

Gateway ingest (API Gateway → Lambda) must set on every `SendMessage`:

- `MessageGroupId = session_id` — serializes per-session turns
- `MessageDeduplicationId = task_id` — idempotent enqueue if the UI retries a send

### 10.4 KEDA ScaledJob update

Patch the existing `agent-gateway-worker` ScaledJob in place (same service account, same min/max replicas, same activeDeadlineSeconds) — change:

- `image` → new tag of `adp-agent-gateway` (TS worker)
- `scaledJob.triggers[0].metadata.queueURL` → the new FIFO queue URL
- Add env:
  - `AGENT_ENTRYPOINT=complex-task-chat`
  - `CONTEXT_STRATEGY=lcm`
  - `MEMORY_STRATEGY=dynamo`
  - `CONTEXT_TABLE=adp-dev-chat-context`
  - `ARTIFACTS_TABLE=adp-dev-chat-artifacts`
  - `ARTIFACTS_BUCKET=adp-dev-chat-artifacts-<account>`
  - `MEMORY_TABLE=adp-dev-agent-memory`
  - `CONTEXT_TOKEN_BUDGET=150000`
  - `ANTHROPIC_MODEL=global.anthropic.claude-sonnet-4-6`
  - `LCM_SUMMARY_MODEL=global.anthropic.claude-sonnet-4-6`  (same model as main turns in Phase 1)
  - `LCM_SUMMARY_ENDPOINT=bedrock`  (Phase 1; flip to `gateway` after end-to-end validation)
  - `MODEL_ENDPOINT=bedrock`  (Phase 1; same switch for main turns)

### 10.5 Dockerfile + startup

Keep the existing multi-stage Node 22 Dockerfile at `modules/agent-factory/agent/Dockerfile`. Changes:

- `package.json` add deps: `@aws-sdk/client-sqs`, `@aws-sdk/client-dynamodb`, `@aws-sdk/lib-dynamodb`.
- `startup.sh` dispatch:

```sh
#!/bin/sh
set -e
case "${AGENT_ENTRYPOINT:-github}" in
  github)
    # existing: health server + agent-worker
    node dist/health-server.js &
    node dist/index.js
    ;;
  complex-task-chat)
    node dist/complex-task-chat/complex-task-chat-agent.js
    ;;
  *)
    echo "unknown AGENT_ENTRYPOINT: ${AGENT_ENTRYPOINT}" >&2
    exit 1
    ;;
esac
```

One image, one tag, dispatched by env var — this is what lets the GH agent and the chat agent share build+deploy pipelines.

### 10.6 Build/publish pipeline

Add a new CodeBuild project or a new target in the existing `bs-agent-gateway` buildspec that builds the TS image from `modules/agent-factory/agent/Dockerfile` and pushes to `adp-agent-gateway:<sha>`. Reuse the existing `s3://adp-terraform-state-<platform-account-id>/codebuild/adp-source.zip` pattern — no new S3 bucket.

---

## 11. Rollout strategy

1. **Merge code + infra changes behind default `AGENT_ENTRYPOINT=github`.** KEDA pods continue running the old image on the existing standard queue. No production impact.
2. **Deploy the new TS image to ECR.** Tag it; do not promote to `:latest`.
3. **Deploy new tables, bucket, sweeper Lambda, and FIFO queue pair** (alongside the existing standard queue — no cutover yet). Update IRSA. Nothing references them yet.
4. **Flip gateway ingest to the FIFO queue** (dual-write optional during the cutover window for safety). Verify `MessageGroupId=session_id` and `MessageDeduplicationId=task_id` on every enqueue.
5. **Swap the dev KEDA ScaledJob** to (a) the new image + env vars, and (b) the FIFO queue URL. Watch a few runs end-to-end covering: single-turn, back-to-back turns (Open Q5 scenario), artifact publish, artifact fetch-and-edit, memory save/recall, TTL sweeper activation.
6. **Drain and delete the old standard queue** once the FIFO path has been stable for a full day.
7. **Promote image to `:latest`**. Old Python code remains in repo for one release for rollback.
8. **Remove the Python worker** (`modules/agent-factory/gateway/app/`) in a follow-up PR once the TS worker has soak-tested for a week.

Rollback paths:
- **Code issue:** `kubectl set image` reverts the ScaledJob to the previous tag. Gateway keeps sending to FIFO; old image consumes from FIFO fine.
- **FIFO queue issue:** re-enable dual-write from gateway ingest; point KEDA back at the standard queue temporarily.

---

## 12. Evolution path

Each of these is additive — no changes to the `ContextManager` or `MemoryProvider` ports, no changes to `complex-task-chat-agent.ts`.

### Context Manager

- **Phase 2: condensation.** Add a `CondensationStrategy` port and a `CondensedCompactor` that runs after leaf compaction. Set `incrementalMaxDepth=1`, flip via env. Schema already carries `parentIds` and `depth`.
- **Phase 3: prompt-aware eviction.** New `BM25EvictionPolicy` impl of `EvictionPolicy`. One-line swap in factory.
- **Large-file interception.** Add an `IngestionFilter` port to `LcmContext.record()`; implement `LargeFileInterceptor` that extracts big file blocks to S3 and replaces them with `<file id=file_...>` references. Add `describe_file(id)` tool.
- **Full-scan search.** Add a `SummarySearcher` port + `DynamoFullScanSearcher` impl + `grep_session` tool.
- **Full replacement.** Write a new `FooContext` implementing `ContextManager`. Register in factory. Change env var. Done.

### Memory Provider

- **`OpenSearchMemoryProvider`** — use the OpenSearch Serverless collection already provisioned by `agent-context` (when deployed) for semantic retrieval. Implement `capabilities().semanticSearch = true`.
- **`AgentCoreMemoryProvider`** — backed by Bedrock AgentCore Memory. Handles async extraction strategies (SEMANTIC, SUMMARY, USER_PREFERENCES, EPISODIC). Swap via env.
- **`ReadOnlyGitBranchMemoryProvider`** — reads the existing `adp`-branch records produced by the GH agent's `memory.ts`. Compose via `RouterMemoryProvider` to immediately unify GH-agent learnings with chat-agent memory without migrating data.
- **Migration away from `agent_learning/*.md`.** Once `DynamoMemoryProvider` is live, update the GH agent prompt to call `save_learning(content, {component, persona})` instead of writing gitignored files. One prompt edit, one tool added; deletes a broken pattern.
- **Persona-learning quality controls.** Layer in: (a) draft promotion — agents write `kind="draft-learning"`, an async/human step promotes to `kind="learning"` before it's injected; (b) TTL extension/compaction — periodic merge of overlapping learnings into a consolidated record; (c) semantic retrieval via `OpenSearchMemoryProvider` so learnings are relevance-ranked not keyword-matched; (d) per-persona caps on total stored learnings to bound growth.

### Model endpoint

- **Phase 1:** direct Bedrock (`bedrock-runtime`) for both main agent turns and LCM summarization.
- **Phase 2 (post end-to-end validation):** migrate both to the internal gateway (`bedrockgateway` in ns `adp-gateway`). Unified observability (CloudTrail + gateway logs), centralized model aliasing, per-tenant quotas, A/B model swaps without redeploying the worker. One env var flip (`LCM_SUMMARY_ENDPOINT=gateway`, `MODEL_ENDPOINT=gateway`). No code changes — the `Summarizer` and `runQuery` wrappers already take the endpoint as config.
- **Phase 3 (opt-in):** tiered summary models (`LCM_SUMMARY_MODEL_LEAF` cheap, `LCM_SUMMARY_MODEL_CONDENSED` premium) once condensation lands.

### Agent tools/skills

Both ports expose their tools via `tools()`. Adding a new tool is local to the impl; the orchestrator never changes. The base tool set (`Bash`, `Read`, …, `Skill`) stays identical to the GH agent, preserving feature parity.

---

## 13. Testing strategy

### Unit

- `LcmContext` against a fake `ContextStore` + stub `Summarizer` → assert assembly budget, fresh-tail protection, compaction trigger.
- `BedrockSummarizer` three-level escalation → assert fallback to deterministic truncate when LLM output exceeds input.
- `ChronologicalEviction` → assert ordering preserved after pick.

### Contract tests (shared suite, run against every impl)

- `context/store/_contract.test.ts` runs the same scenarios against `DynamoContextStore` (via DynamoDB Local) and a future `InMemoryContextStore`.
- `memory/_contract.test.ts` runs the same scenarios against `DynamoMemoryProvider` and `NullMemoryProvider` (sanity) and any future impl. Catches implicit coupling.

### Integration / e2e

- Reuse the pattern from `modules/agent-factory/tests/` (two-stage: generate suite, execute against deployed infra — issues #25/#27).
- New suite: send synthetic SQS message → poll response queue → assert shape + timing.

---

## 14. Open questions

1. **Persona loading inside the container.** ✅ **Resolved 2026-04-19.** Split into two layers (see §6): (a) **base persona** baked into the Docker image at `/app/personas/<type>.md` from `modules/agent-factory/rules/personas/`; (b) **persona learnings** retrieved via `MemoryProvider` with `scope.persona = <type>`, written by agents over time via `save_learning`. Base persona is configuration (rare, PR-reviewed); learnings are data (frequent, agent-authored). No new subsystem — learnings ride on the memory port.
2. **Workspace CWD for tools.** ✅ **Resolved 2026-04-19.** Ephemeral `/tmp/workspace/<session_id>` per run. No EFS/PVC, no workspace continuity layer. Cross-turn continuity for deliverables is handled by §7 `ArtifactStore` (fetch → edit → publish). S3-Files-as-workspace rejected for Phase 1 due to POSIX edge cases, latency on chatty tool use, and per-op cost; S3 Files remains a future swap candidate inside `S3ArtifactStore` and for future dedicated ports (shared-assets, user-data-room, user-code).
3. **Summary model.** ✅ **Resolved 2026-04-19.** Phase 1 uses **Sonnet 4.6** (`global.anthropic.claude-sonnet-4-6`) for both main turns and LCM summarization — same model for consistency during initial rollout and to avoid multi-model debugging during stabilization. Pinned via `LCM_SUMMARY_MODEL` env so we can swap without code changes. Endpoint in Phase 1 is **direct Bedrock** (`bedrock-runtime`). Once the chat worker is end-to-end validated, migrate `Summarizer` + the main agent's model calls to route through the **gateway** (`bedrockgateway` in ns `adp-gateway`) for unified observability, quotas, and model aliasing. The `Summarizer` port does not care which endpoint — one config change flips it.
4. **Session TTL.** ✅ **Resolved 2026-04-19.** 90-day sliding TTL per session via a session-header row (Option C). Per-session header row `PK=session#<sid>, SK=header` carries `{lastActivityAt, ttl=lastActivityAt+90d}`; child rows (msg/sum/item) have no TTL of their own. On every write to the session, `UpdateItem` refreshes the header's `ttl`. A sweeper Lambda subscribes to the DynamoDB TTL stream on `adp-chat-context`: when the header expires and is deleted, the Lambda `Query`s by session PK and `BatchDelete`s all children + the artifact catalog rows + S3 artifact objects for that session. **Memory records:** per-kind TTL — `preference` no TTL, `fact` 180 days (extended on read/re-save), `learning` 90 days (extended on read/re-save), `draft-learning` 14 days. **Artifacts:** S3 lifecycle 30 days default; on `publish` the TTL is auto-extended to `max(30 days, sessionTTL)` so artifacts within a live session never expire before the session they belong to.
5. **Concurrent turns on the same session.** ✅ **Resolved 2026-04-19.** SQS FIFO with `MessageGroupId = session_id`, `MessageDeduplicationId = task_id` (Option A). FIFO serializes per-session turns at the queue layer — no application-level locks, no optimistic concurrency, no recomputed Bedrock calls. Cross-session parallelism is preserved (different session = different group). Per-turn writes use `TransactWriteItems` (user msg + assistant msg + context_items + header refresh in one atomic write) so a pod crash mid-turn leaves zero partial state; a retry pod (after visibility timeout) re-processes the original message idempotently by `task_id`. **UX follow-ups (Phase 2):** UI disables input during in-flight turn; gateway enforces per-session backlog cap (e.g. 10 pending); optional queue-position indicator in the send response.
6. **`user_id` propagation from the gateway.** ✅ **Resolved 2026-04-19 (Option A + D).** API Gateway's Cognito authorizer validates the JWT at the edge; the ingest Lambda reads `event.requestContext.authorizer.claims` and populates the SQS task payload with `user_id = claims.sub` (Cognito `sub` — opaque, stable, unique), plus optional `tenant_id` (from `custom:tenant_id` if configured), `user_email` (for logging only, never a scope key), and `source = "rest" | "websocket" | "agent-m2m"`. M2M callers get `user_id = "m2m:<client_id>"` by convention. Ingest Lambda enforces session ownership (writes `ownerUserId` on first message; rejects mismatches on subsequent). Chat worker trusts the payload but verifies session ownership at assembly time (defense in depth). Lambda resource policy restricts invocation to the API Gateway ARN only. Worker does not re-validate the JWT. See §15 for cross-team dependencies on the gateway side.

---

## 15. Cross-team dependencies

Items that require work outside this module (gateway team). Tracked as Phase 1 blockers for the chat agent to function correctly.

1. **Ingest Lambda extracts Cognito claims.** The agent-gateway ingest Lambda (producer for the FIFO tasks queue) must read `event.requestContext.authorizer.claims.sub` and populate `user_id` on every outgoing SQS message. Confirm current behavior; implement if missing.
2. **Cognito app client claim configuration.** If multi-tenancy is in scope, the `bedrockgw-dev-client` Cognito app client should issue the `custom:tenant_id` attribute in its token. Trivial Cognito config change.
3. **Session creation endpoint.** `POST /sessions → {session_id}` on the gateway, which writes the initial `header` row to `adp-<env>-chat-context` with `ownerUserId = claims.sub`. UI calls this once before its first `SendMessage`. Without this, every first message has to race to write the header.
4. **Lambda resource policy.** Ingest Lambda's resource policy must restrict invocation to the REST API Gateway ARN and the WebSocket API ARN. No cross-account principals, no wildcard invokers.
5. **WebSocket path parity.** The `bedrockgw-dev-api-authorizer` on the WebSocket `$connect` route already validates JWT via `?token=`. The ingest path from the WebSocket `$default` / custom routes must thread `claims.sub` onto the SQS message the same way REST does.
6. **Per-session backlog cap (Phase 2).** Ingest Lambda checks FIFO queue depth for the session's MessageGroup and rejects `SendMessage` with HTTP 429 once depth exceeds a configured threshold (e.g. 10). Prevents a runaway client from creating a huge serialized backlog for one session. Not a Phase 1 blocker but should be scoped alongside UI queue-position work.

---

## 16. What this doc does not do

- **No code yet** — this is a shape document. Code lives in a follow-up PR.
- **No GH-agent refactor to use `DynamoMemoryProvider`** — separate issue. The chat agent ships first; GH agent migrates second, behind the same port.
- **No GraphRAG / Neptune integration** — deliberately out of scope. If we later want the chat agent to query agent-context's knowledge graph, it happens via an MCP tool, not via the context or memory ports.

---

## Appendix A — attribution

The LCM context manager design is inspired by:

- [martian-engineering/lossless-claw](https://github.com/martian-engineering/lossless-claw) (MIT) — DAG-based summarization approach, XML summary wrapper format, three-level escalation pattern, fresh-tail protection.
- [Voltropy LCM paper](https://papers.voltropy.com/LCM) — foundational algorithm.

No code is copied; this is a from-scratch implementation in the shape of our stack (TypeScript + Claude Agent SDK + DynamoDB + Bedrock).

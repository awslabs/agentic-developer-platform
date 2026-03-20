# Agent Memory Systems: Research, Architecture, and Implementation Plan

**Issue**: #141 — Research: Agent Memory Systems — Specialisation, Persistent Learning, and Cross-Session Knowledge
**Date**: 2026-02-18
**Author**: AI Research Agent
**Scope**: Academic research, open source projects, commercial products (Aug 2025 – Feb 2026)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Part 1: Research Findings](#part-1-research-findings)
   - [1.1 Academic Research](#11-academic-research)
   - [1.2 Open Source Memory Frameworks](#12-open-source-memory-frameworks)
   - [1.3 Commercial Products and Services](#13-commercial-products-and-services)
3. [Part 2: Architecture Recommendation](#part-2-architecture-recommendation)
   - [2.1 Proposed Memory Architecture](#21-proposed-memory-architecture)
   - [2.2 Specialisation Mechanism](#22-specialisation-mechanism)
   - [2.3 Memory Consolidation Pipeline](#23-memory-consolidation-pipeline)
   - [2.4 Cross-Agent Knowledge Sharing](#24-cross-agent-knowledge-sharing)
4. [Part 3: Implementation Plan](#part-3-implementation-plan)
   - [3.1 Phase 1: Foundation](#31-phase-1-foundation)
   - [3.2 Phase 2: Intelligence](#32-phase-2-intelligence)
   - [3.3 Phase 3: Specialisation](#33-phase-3-specialisation)
   - [3.4 Technology Choices](#34-technology-choices)
5. [Part 4: Memory Schema Design](#part-4-memory-schema-design)
   - [4.1 TypeScript Interfaces](#41-typescript-interfaces)
   - [4.2 DynamoDB Table Design](#42-dynamodb-table-design)
   - [4.3 Integration Code Examples](#43-integration-code-examples)

---

## Executive Summary

Our current `agent_learning/` system is a flat collection of 13 markdown files containing ~250 discrete learnings. Agents read ALL learnings before each run — no relevance filtering, no specialisation, no feedback loops. This research evaluates the state of the art in agent memory systems and recommends a phased approach to building persistent, specialised memory for our GitHub Actions agent.

### Key Findings

1. **Academic consensus**: Agent memory should be multi-tiered (episodic/semantic/procedural), with adaptive retrieval that selects relevant memories based on task context rather than loading everything.

2. **Best open source option**: **Mem0** (47.6k stars, TypeScript SDK, graph+vector hybrid) is the most production-ready memory-as-a-service, but for our scale and AWS-native stack, a **custom DynamoDB + Amazon Titan Embeddings** solution provides better control and lower cost.

3. **Critical insight**: For corpora under ~1,000 records (our current scale), **tag-based retrieval with recency weighting often outperforms pure vector search**. Vector search becomes essential only at scale (>5K records) or for discovering non-obvious connections.

4. **Recommended approach**: Build a custom memory system on DynamoDB + S3 + Titan Embeddings, borrowing architectural patterns from MIRIX (six memory types), FluxMem (adaptive retrieval), and CrewAI (composite scoring). Integrate directly into our existing TypeScript agent via the AWS SDK.

### Current System Limitations Identified

| Limitation | Evidence from Codebase |
|-----------|----------------------|
| No structured retrieval | `CodeGenerationAgent.ts` lines 279-286: injects ALL learnings as flat text |
| No specialisation | `PlanningAgent.ts` line 18: `generatePlan()` takes only `issueContext`, no agent role context |
| No feedback loop | No mechanism for agents to report which learnings helped |
| No cross-session state | `AgentOrchestrator.ts` line 56: `state` object is ephemeral, reset each run |
| No pattern recognition | `agent_learning/*.md`: flat markdown with YAML tags but no similarity grouping |
| No memory config | `ConfigLoader.ts`: only AWS/Bedrock config, no memory-related settings |

---

## Part 1: Research Findings

### 1.1 Academic Research

We surveyed 14 papers from Aug 2025 – Feb 2026 covering agent memory architectures, retrieval mechanisms, specialisation, consolidation, and multi-agent memory sharing. Full details in [agent-memory-research-sources.csv](./agent-memory-research-sources.csv).

#### 1.1.1 Memory Architecture Papers

**FluxMem: Choosing How to Remember** (Lu et al., Feb 2026) — [arXiv:2602.14038](https://arxiv.org/abs/2602.14038)
- **Key contribution**: Adaptive memory structure selection. Different task contexts activate different memory organizations.
- **Relevance**: Directly addresses our specialisation problem. A DevOps task should retrieve deployment memories differently than a code generation task.
- **Architecture**: Three-level hierarchy with probabilistic fusion mechanism across memory structures.
- **Result**: 9.18% improvement over fixed memory structures.
- **Takeaway for us**: Don't use one retrieval strategy for all tasks. Weight memory retrieval by task type.

**HyMem: Hybrid Memory Architecture** (Zhao et al., Feb 2026) — [arXiv:2602.13933](https://arxiv.org/abs/2602.13933)
- **Key contribution**: Dual-granular storage (summary + detail) with dynamic two-tier retrieval.
- **Relevance**: Our `agent_learning/` files are all "detail" with no summaries. A summary layer would enable faster screening.
- **Result**: 92.6% reduction in computational cost vs full-context approaches.
- **Takeaway for us**: Store both a one-line summary and full detail for each memory. Retrieve summaries first, then fetch full detail only for top-K matches.

**MIRIX: Multi-Agent Memory System** (Wang & Chen, Jul 2025) — [arXiv:2507.07957](https://arxiv.org/abs/2507.07957)
- **Key contribution**: Six memory types — Core, Episodic, Semantic, Procedural, Resource, Knowledge Vault.
- **Relevance**: Provides a comprehensive taxonomy for our memory schema design.
- **Result**: 35% higher accuracy with 99.9% reduced storage.
- **Takeaway for us**: Adopt a four-type model (Episodic, Semantic, Procedural, Agent Profile) appropriate for our use case. The MIRIX "Knowledge Vault" maps to our consolidated/verified knowledge tier.

#### 1.1.2 Procedural Memory and Skill Acquisition

**ProcMEM: Learning Reusable Procedural Memory** (Mi et al., Feb 2026) — [arXiv:2602.01869](https://arxiv.org/abs/2602.01869)
- **Key contribution**: Transforms episodic narratives into executable Skills with activation/execution/termination conditions.
- **Relevance**: Our error/fix tables in `agent_learning/` are episodic narratives. ProcMEM shows how to convert these into reusable procedures.
- **Example**: "CrashLoopBackOff → check pod logs → check env vars → check image tag" becomes a procedural skill activated by the error pattern.
- **Takeaway for us**: Build a consolidation pipeline that extracts step-by-step procedures from recurring error/fix patterns.

**Evolving Programmatic Skill Networks** (Shi et al., Jan 2026) — [arXiv:2601.03509](https://arxiv.org/abs/2601.03509)
- **Key contribution**: Skills evolve through experience with maturity-aware update gating.
- **Relevance**: Our procedures should mature — a procedure used 10 times with 90% success rate should be weighted higher than a new untested one.
- **Takeaway for us**: Track execution count and success rate for procedural memories.

**Memento: Fine-tuning LLM Agents without Fine-tuning LLMs** (Zhou et al., Aug 2025) — [arXiv:2508.16153](https://arxiv.org/abs/2508.16153)
- **Key contribution**: Memory-based RL for agent improvement without model fine-tuning. Uses episodic memory with case-selection policy.
- **Relevance**: We can't fine-tune Claude, so improving agent behavior must come through better memory selection.
- **Takeaway for us**: The feedback loop (which memories were useful) is the training signal for better retrieval.

#### 1.1.3 Memory Consolidation

**TraceMem: Weaving Narrative Memory Schemata** (Shu et al., Feb 2026) — [arXiv:2602.09712](https://arxiv.org/abs/2602.09712)
- **Key contribution**: Three-stage consolidation: Short-term → Synaptic → Systems consolidation.
- **Relevance**: Maps to our proposed pipeline: Raw learnings → Structured records → Consolidated knowledge.
- **Takeaway for us**: Consolidation should be a pipeline with distinct stages, not a single transformation.

**CogEvo-Edu** (Wu et al., Nov 2025) — [arXiv:2512.00331](https://arxiv.org/abs/2512.00331)
- **Key contribution**: Confidence-weighted consolidation with spatiotemporal value for knowledge chunks.
- **Relevance**: Knowledge items should have confidence scores that increase with verification.
- **Takeaway for us**: When multiple agents independently confirm the same learning, boost its confidence score.

#### 1.1.4 Multi-Agent Shared Memory

**Collaborative Memory** (Rezazadeh et al., May 2025) — [arXiv:2505.18279](https://arxiv.org/abs/2505.18279)
- **Key contribution**: Private + shared memory tiers with dynamic access controls.
- **Relevance**: Maps directly to per-agent specialisation memory + shared knowledge pool.
- **Takeaway for us**: Each agent instance should have a private profile, but all agents share the knowledge base.

**Context-Aware MCP** (Jayanti & Han, Jan 2026) — [arXiv:2601.11595](https://arxiv.org/abs/2601.11595)
- **Key contribution**: Shared Context Store (SCS) for multi-agent coordination.
- **Relevance**: MCP pattern could enable memory sharing across agent sessions in our GitHub Actions pipeline.

**RCR-Router** (Liu et al., Aug 2025) — [arXiv:2508.04903](https://arxiv.org/abs/2508.04903)
- **Key contribution**: Role-aware context routing — dynamically selects relevant memory subsets based on agent role.
- **Relevance**: Directly addresses how a DevOps agent should retrieve different memories than a code gen agent.
- **Takeaway for us**: Memory retrieval must include agent role as a query parameter.

#### 1.1.5 Episodic Memory

**CAST: Character-and-Scene Episodic Memory** (Ma et al., Jan 2026) — [arXiv:2602.06051](https://arxiv.org/abs/2602.06051)
- **Key contribution**: Scene-based episodic memory organized by time/place/topic.
- **Relevance**: Our episodes are naturally structured: timestamp/environment/issue-type.
- **Takeaway for us**: Index episodic memories by multiple dimensions for flexible retrieval.

**The Pensieve Paradigm** (Liu et al., Feb 2026) — [arXiv:2602.12108](https://arxiv.org/abs/2602.12108)
- **Key contribution**: Agents manage their own context via memory tools (pruning, indexing, note-taking).
- **Result**: 10-20% accuracy improvement on chat memory tasks.
- **Takeaway for us**: Consider giving agents explicit memory-management tools rather than hiding memory behind infrastructure.

---

### 1.2 Open Source Memory Frameworks

| Project | Architecture | Storage | Retrieval | Memory Types | TS Support | Maintenance | License | Fit (1-5) |
|---------|-------------|---------|-----------|-------------|------------|-------------|---------|-----------|
| **Mem0** | Graph + vector hybrid | Qdrant/custom | Vector + graph traversal | User, Session, Agent | ✅ npm `mem0ai` | Very Active (47.6k ⭐) | Apache 2.0 | **5** |
| **Letta (MemGPT)** | Tiered (core/archival/recall) | PostgreSQL + pgvector | Vector search | Core, Archival, Recall | ✅ npm `@letta-ai/letta-client` | Active (21.2k ⭐) | Apache 2.0 | **4** |
| **Zep** | Temporal knowledge graph | Custom graph store | Graph traversal + vector | Facts, Relationships, Temporal | ✅ npm `@getzep/zep-cloud` | Active (4.1k ⭐) | Apache 2.0 | **3** |
| **CrewAI** | Unified memory with scopes | Configurable (vector stores) | Composite scoring | Unified (shallow + deep) | ❌ Python only | Very Active (44.3k ⭐) | MIT | **3** |
| **LangChain** | Modular memory modules | Configurable | Vector + summary + entity | Buffer, Summary, Entity, Vector | ✅ npm `langchain` | Very Active | MIT | **3** |
| **AutoGen** | Teachability agent pattern | Custom | Agent-driven | Teachable patterns | ❌ Python/.NET only | Active (54.6k ⭐) | MIT | **2** |
| **Semantic Kernel** | Memory abstractions + connectors | Multiple vector DBs | Vector search | Generic memories | ❌ Python/.NET/Java | Active (27.3k ⭐) | MIT | **2** |

#### Detailed Evaluations

**Mem0** (Recommended for reference architecture)
- **Strengths**: Graph + vector hybrid provides both structured relationship tracking and semantic search. TypeScript SDK with simple `add()`/`search()` API. Multi-level memory (user, session, agent state) maps well to our needs. 26% accuracy improvement over OpenAI Memory in benchmarks.
- **Weaknesses**: Requires Mem0 Platform for managed deployment or self-hosting the full stack. Adds external dependency. Cloud pricing could grow with scale.
- **For our use case**: Best architectural reference. We can adopt its memory type taxonomy and API patterns while building on DynamoDB for AWS-native deployment.

**Letta (MemGPT)** (Most mature self-editing memory)
- **Strengths**: Agents can read AND write their own memory. Tiered architecture (core = in-context, archival = long-term, recall = conversation). Self-manages what to remember. TypeScript SDK available.
- **Weaknesses**: Requires running Letta server (PostgreSQL + Python). Heavy infrastructure for our use case. Designed for chatbot-style agents, not task-execution agents.
- **For our use case**: Self-editing memory concept is excellent — agents should be able to save learnings as they work. But the full Letta server is overkill for GitHub Actions runners.

**Zep** (Best temporal knowledge graph)
- **Strengths**: Temporal awareness (knows when facts changed). Automatic fact extraction from conversations. Sub-200ms retrieval. TypeScript SDK.
- **Weaknesses**: Community Edition deprecated. Cloud-only going forward. Designed for conversational assistants, not DevOps agents.
- **For our use case**: Temporal tracking is valuable (e.g., knowing that an EKS fix was discovered on Feb 16 and verified on Feb 17). But cloud-only adds vendor dependency.

**CrewAI** (Best composite retrieval)
- **Strengths**: Unified memory with composite scoring (semantic similarity + recency + importance). Hierarchical scope tree. Deep recall mode uses LLM for query analysis.
- **Weaknesses**: Python only. Tied to CrewAI framework.
- **For our use case**: Composite scoring pattern is exactly what we need. Adopt the scoring approach: `relevance = 0.5*similarity + 0.3*recency + 0.2*importance`.

---

### 1.3 Commercial Products and Services

| Product | Memory Type | Integration | Pricing | Fit (1-5) |
|---------|-----------|-------------|---------|-----------|
| **DynamoDB** | Structured KV with GSIs | Native AWS SDK (TypeScript) | Pay-per-request (~$0.25/1M reads) | **5** |
| **Titan Embeddings** | Text → 1024-dim vectors | Bedrock API (TypeScript) | ~$0.0001/1K tokens | **5** |
| **S3** | Object storage | Native AWS SDK | ~$0.023/GB/month | **4** |
| **Bedrock Agent Memory** | Session memory + summarization | Bedrock Agent framework | Per-API-call pricing | **3** |
| **Bedrock Knowledge Bases** | RAG pipeline | Bedrock + OpenSearch/Aurora | Complex pricing model | **3** |
| **Mem0 Platform** | Managed graph+vector | REST API + TypeScript SDK | Freemium, usage-based | **4** |
| **Pinecone** | Vector database | REST API + Node.js SDK | Free tier, then pay-per-query | **3** |
| **Zep Cloud** | Temporal knowledge graph | REST API + TypeScript SDK | Usage-based | **3** |

#### AWS-Native Stack Recommendation

For our GitHub Actions agent running on EC2 with existing AWS infrastructure:

1. **DynamoDB** — Primary memory store. Already in our stack (Cognito agent metadata uses it). Pay-per-request pricing scales to zero when idle. GSIs enable flexible queries by domain, agent role, error type, etc.

2. **Amazon Titan Embeddings** — Vector generation. Called via Bedrock API from our TypeScript agent. No infrastructure to manage. ~$0.0001/1K tokens means embedding all 250 current learnings costs less than $0.01.

3. **S3** — Archival storage. Raw `agent_learning/*.md` files backed up here. Versioned for history tracking.

This stack avoids any external dependencies beyond our existing AWS account and keeps everything within our IAM permission boundary.

---

## Part 2: Architecture Recommendation

### 2.1 Proposed Memory Architecture

We propose a three-tier memory architecture inspired by MIRIX's six-type taxonomy, simplified for our use case:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AGENT EXECUTION                              │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │ PlanningAgent │    │ CodeGenAgent │    │ AgentOrchestrator    │  │
│  │              │    │              │    │                      │  │
│  │ retrieveFor  │    │ retrieveFor  │    │ storeEpisodic()     │  │
│  │ Planning()   │    │ CodeGen()    │    │ storeFeedback()     │  │
│  └──────┬───────┘    └──────┬───────┘    └──────────┬───────────┘  │
│         │                   │                        │              │
│         └───────────────────┴────────────────────────┘              │
│                              │                                      │
│                    ┌─────────▼─────────┐                           │
│                    │  MemoryService    │                            │
│                    │                   │                            │
│                    │ retrieve()        │                            │
│                    │ store()           │                            │
│                    │ consolidate()     │                            │
│                    │ updateFeedback()  │                            │
│                    └─────────┬─────────┘                           │
│                              │                                      │
└──────────────────────────────┼──────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   MemoryRetriever   │
                    │                     │
                    │ Tag-based filter    │
                    │ + Recency scoring   │
                    │ + Vector similarity │
                    │ + Composite ranking │
                    └──────────┬──────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
   ┌───────▼───────┐  ┌───────▼───────┐  ┌───────▼───────┐
   │  WORKING      │  │  SHORT-TERM   │  │  LONG-TERM    │
   │  MEMORY       │  │  MEMORY       │  │  MEMORY       │
   │               │  │               │  │               │
   │ In-context    │  │ DynamoDB      │  │ DynamoDB +    │
   │ window        │  │ (TTL: 30d)   │  │ S3 + Titan    │
   │ managed by    │  │               │  │ Embeddings    │
   │ Claude Code   │  │ Recent        │  │               │
   │               │  │ episodes,     │  │ Consolidated  │
   │ Current task, │  │ hot errors,   │  │ knowledge,    │
   │ retrieved     │  │ active        │  │ procedures,   │
   │ memories      │  │ procedures    │  │ agent         │
   │               │  │               │  │ profiles      │
   └───────────────┘  └───────────────┘  └───────────────┘
```

#### Memory Tiers Explained

| Tier | Storage | Retention | Content | Access Pattern |
|------|---------|-----------|---------|---------------|
| **Working Memory** | Claude context window | Current session only | Retrieved memories + current task | Injected into prompt |
| **Short-term Memory** | DynamoDB with TTL (30 days) | Auto-expires | Recent episodes, hot errors, in-progress learnings | Fast key/tag lookup |
| **Long-term Memory** | DynamoDB + S3 + embeddings | Permanent | Consolidated knowledge, procedures, agent profiles | Vector search + tag filter |

### 2.2 Specialisation Mechanism

Agent specialisation works through **role-weighted memory retrieval** (inspired by RCR-Router):

```
┌─────────────────────────────────────────────────┐
│                 AGENT PROFILES                    │
│                                                   │
│  DevOps Agent Profile                            │
│  ├─ domain: "devops"                             │
│  ├─ totalTasks: 45                               │
│  ├─ successRate: 78%                             │
│  ├─ strongDomains: [eks, terraform, ci-cd]       │
│  └─ weakDomains: [frontend, cognito]             │
│                                                   │
│  CodeGen Agent Profile                           │
│  ├─ domain: "code-generation"                    │
│  ├─ totalTasks: 30                               │
│  ├─ successRate: 85%                             │
│  ├─ strongDomains: [python, fastapi, tests]      │
│  └─ weakDomains: [terraform, eks]                │
│                                                   │
│  Security Agent Profile                          │
│  ├─ domain: "security"                           │
│  ├─ totalTasks: 10                               │
│  ├─ successRate: 90%                             │
│  ├─ strongDomains: [iam, auth, secrets]          │
│  └─ weakDomains: [frontend, ci-cd]               │
└─────────────────────────────────────────────────┘
```

**How specialisation affects retrieval:**

1. **Task type detection**: From the issue title/body, classify the task domain (DevOps, code-gen, security, testing, docs).
2. **Role-weighted scoring**: Memories tagged with the task domain get a 2x relevance boost.
3. **Cross-domain surfacing**: If a DevOps task involves Cognito (a weak domain), surface Cognito-specific learnings at higher priority.
4. **Profile-informed guidance**: If the agent's profile shows low success rate on a domain, inject extra caution learnings.

**Retrieval scoring formula:**
```
score = (0.4 * semantic_similarity) +
        (0.3 * recency_score) +
        (0.2 * usefulness_score) +
        (0.1 * domain_match_bonus)
```

Where:
- `semantic_similarity`: Cosine similarity between task description embedding and memory embedding (0-1)
- `recency_score`: Exponential decay — `exp(-days_since_creation / 30)` (0-1)
- `usefulness_score`: `use_count / (use_count + 5)` — learnings used more often score higher (0-1)
- `domain_match_bonus`: 1.0 if memory domain matches task domain, 0.5 if partial match, 0.0 otherwise

### 2.3 Memory Consolidation Pipeline

Inspired by TraceMem's three-stage consolidation:

```
Stage 1: CAPTURE (During Agent Run)
─────────────────────────────────
  Agent encounters error → writes raw episode to DynamoDB
  {error, resolution, context, tags, timestamp}

                    ▼

Stage 2: STRUCTURE (Post-Session Job)
─────────────────────────────────────
  ConsolidationJob runs after each agent session:
  1. Parse raw episodes from the session
  2. Generate embeddings via Titan Embeddings
  3. Match against existing semantic memories
  4. If similar memory exists (cosine > 0.85):
     → Increment verification count
     → Update confidence score
     → Merge resolution details
  5. If new pattern detected:
     → Create new semantic memory
     → Tag with domain and error type

                    ▼

Stage 3: PROMOTE (Weekly/On-Demand)
──────────────────────────────────
  Review high-confidence semantic memories:
  1. If verified by 3+ agents → promote to "verified" tier
  2. If used 5+ times successfully → create procedural memory
  3. If not accessed in 90 days → archive to S3
  4. Merge duplicate/similar learnings
  5. Update agent profiles with success metrics
```

**Consolidation example:**

```
Raw Episode (agent_learning/2026-02-16-deployment-learnings.md #1):
  "EKS Auto Mode requires sts:TagSession on cluster role trust policy"

After Consolidation:
  SemanticMemory {
    domain: "eks",
    fact: "EKS Auto Mode creates instance profiles for nodes, and the
           cluster role's trust policy must include both sts:AssumeRole
           AND sts:TagSession. Without sts:TagSession, NodeClass shows
           InstanceProfileCreationFailed.",
    confidence: 0.95,  // Verified in 2 sessions
    tags: ["eks-auto-mode", "sts-tag-session", "instance-profile"],
    verifiedCount: 2,
    lastVerified: "2026-02-17"
  }

  ProceduralMemory {
    taskPattern: "EKS Auto Mode nodes not provisioning",
    steps: [
      "1. Check NodeClass status: kubectl get nodeclass default -o yaml",
      "2. Look for InstanceProfileCreationFailed in status.conditions",
      "3. Check cluster role trust policy for sts:TagSession",
      "4. Add sts:TagSession if missing",
      "5. Verify node provisioning starts"
    ],
    successRate: 1.0,
    executionCount: 2
  }
```

### 2.4 Cross-Agent Knowledge Sharing

```
┌────────────────────────────────────────────────────┐
│              SHARED MEMORY POOL                     │
│                                                     │
│  ┌─────────────┐  ┌────────────┐  ┌─────────────┐ │
│  │  Episodic    │  │  Semantic  │  │  Procedural │ │
│  │  Memories    │  │  Knowledge │  │  Skills     │ │
│  │  (all agents)│  │  (verified)│  │  (mature)   │ │
│  └──────┬──────┘  └─────┬──────┘  └──────┬──────┘ │
│         │               │                │         │
│         └───────────────┴────────────────┘         │
│                         │                           │
│              ┌──────────▼──────────┐               │
│              │  Relevance Filter   │               │
│              │  (role + task type) │               │
│              └──────────┬──────────┘               │
│                         │                           │
└─────────────────────────┼───────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
   ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
   │ DevOps     │   │ CodeGen   │   │ Security  │
   │ Agent      │   │ Agent     │   │ Agent     │
   │ Run #47    │   │ Run #48   │   │ Run #49   │
   │            │   │           │   │           │
   │ Gets:      │   │ Gets:     │   │ Gets:     │
   │ - EKS tips │   │ - API     │   │ - IAM     │
   │ - TF fixes │   │   patterns│   │   policies│
   │ - CI/CD    │   │ - Test    │   │ - Auth    │
   │   learnings│   │   patterns│   │   patterns│
   │            │   │           │   │           │
   │ + Own      │   │ + Own     │   │ + Own     │
   │   profile  │   │   profile │   │   profile │
   └────────────┘   └───────────┘   └───────────┘
```

**Sharing rules:**
1. **All episodic memories are shared** — any agent can learn from any other agent's experience.
2. **Semantic memories are shared after verification** — only promoted once confirmed by multiple agents.
3. **Agent profiles are private** — each agent tracks its own success metrics.
4. **Retrieval is role-filtered** — agents primarily see memories relevant to their task type.
5. **Cross-domain surfacing** — when a task spans domains, surface learnings from all relevant domains.

---

## Part 3: Implementation Plan

### 3.1 Phase 1: Foundation (Estimated: 2-3 weeks)

**Goal**: Replace flat markdown reading with structured, relevant memory retrieval.

#### What to Build

1. **DynamoDB Memory Table**
   - Single table design with GSIs for flexible queries
   - TTL for auto-expiring short-term memories
   - Migration script to import existing `agent_learning/*.md` files

2. **MemoryService Class** (TypeScript)
   - `storeEpisodic(episode)` — save raw learning from agent session
   - `retrieveRelevant(taskContext, limit)` — get top-K relevant memories
   - Tag-based filtering + recency scoring (no vector search yet)

3. **Integration into CodeGenerationAgent**
   - Replace "read all learnings" with `MemoryService.retrieveRelevant()`
   - Inject `## Relevant Past Learnings` section instead of all 13 files
   - Limit to top 10 most relevant memories per run

4. **Integration into PlanningAgent**
   - Add memory retrieval to `generatePlan()` — inject relevant past experiences
   - Help planning agent understand what went wrong in similar past tasks

5. **Migration Script**
   - Parse all 13 existing `agent_learning/*.md` files
   - Extract individual learnings from markdown headers
   - Create DynamoDB records with tags from YAML front matter
   - Preserve the original markdown files as backup

#### Phase 1 Technology Choices

| Component | Technology | Justification |
|-----------|-----------|---------------|
| Storage | DynamoDB (single table) | Already in our AWS stack, pay-per-request, TTL support |
| Query | DynamoDB Query + GSI | Tag-based + recency filtering sufficient for <1K records |
| Integration | `@aws-sdk/client-dynamodb` | Already a dependency in our TypeScript agent |
| Config | `ConfigLoader.ts` extension | Add memory table name and retrieval settings |

#### Phase 1 Files to Modify

| File | Changes |
|------|---------|
| `ConfigLoader.ts` | Add `memoryTableName`, `memoryEnabled`, `memoryMaxResults` config |
| `CodeGenerationAgent.ts` | Replace flat learnings injection with `MemoryService.retrieveRelevant()` |
| `PlanningAgent.ts` | Add `relevantMemories` parameter to `buildPlanningPrompt()` |
| `AgentOrchestrator.ts` | Add `MemoryService` to constructor, call `storeEpisodic()` after completion |
| **New**: `MemoryService.ts` | Core memory service with CRUD + retrieval |
| **New**: `MemoryMigration.ts` | Script to import existing learnings |

### 3.2 Phase 2: Intelligence (Estimated: 3-4 weeks)

**Goal**: Add vector-based semantic search and a feedback loop.

#### What to Build

1. **Vector Embeddings**
   - Generate embeddings for all memories via Amazon Titan Embeddings
   - Store embeddings as part of each DynamoDB record (as binary attribute)
   - Add cosine similarity calculation in TypeScript

2. **Semantic Retrieval**
   - When agent receives a task, embed the task description
   - Retrieve candidates by tag filter, then re-rank by cosine similarity
   - Composite scoring: `0.4*similarity + 0.3*recency + 0.2*usefulness + 0.1*domain`

3. **Feedback Loop**
   - After each agent run, identify which retrieved memories were referenced
   - Increment `useCount` for memories that influenced successful outcomes
   - Decrement relevance for memories retrieved but not used

4. **Post-Session Consolidation Job**
   - Run after each agent session completes
   - Parse new episodic memories from the session
   - Match against existing semantic memories (cosine > 0.85 = similar)
   - Merge duplicates, increment verification counts
   - Create new semantic memories for novel patterns

#### Phase 2 Technology Choices

| Component | Technology | Justification |
|-----------|-----------|---------------|
| Embeddings | Amazon Titan Embeddings (Bedrock) | AWS-native, no extra infra, ~$0.0001/1K tokens |
| Similarity | Application-side cosine similarity | Fast enough for <5K records, avoids vector DB dependency |
| Consolidation | TypeScript function in agent | Runs post-session, no separate infrastructure |

### 3.3 Phase 3: Specialisation (Estimated: 4-6 weeks)

**Goal**: Agents develop domain expertise and track their own performance.

#### What to Build

1. **Agent Profiles**
   - Track per-agent, per-domain success metrics
   - Store in DynamoDB with agent ID as partition key
   - Update after each task completion

2. **Role-Aware Retrieval**
   - Detect task domain from issue context
   - Apply domain-specific retrieval weights
   - Surface cross-domain learnings when task spans multiple domains

3. **Procedural Memory**
   - Extract multi-step procedures from recurring error patterns
   - Track procedure execution count and success rate
   - Surface mature procedures for known error patterns

4. **Cross-Session State**
   - Agent remembers its own past actions across runs
   - Track which strategies it has tried for recurring problems
   - Avoid repeating failed approaches from previous sessions

### 3.4 Technology Choices

| Decision | Choice | Alternatives Considered | Why |
|----------|--------|------------------------|-----|
| **Primary Store** | DynamoDB | PostgreSQL, MongoDB | Already in AWS stack, pay-per-request, GSIs for flexible queries, TTL for auto-expiry |
| **Embeddings** | Amazon Titan Embeddings | OpenAI, Cohere, local models | AWS-native, no extra API keys, called from existing Bedrock config |
| **Vector Search** | Application-side cosine similarity | Pinecone, OpenSearch, pgvector | Sufficient for <5K records, no extra infrastructure |
| **Archival Storage** | S3 | N/A | Already in stack, versioning for history |
| **Framework** | Custom (borrow patterns) | Mem0, Letta, LangChain | Avoids framework lock-in, tailored to our TypeScript agent, minimal dependencies |
| **Memory Format** | Structured JSON in DynamoDB | Graph DB, document DB | Flexible schema, easy to query, supports GSIs |

---

## Part 4: Memory Schema Design

### 4.1 TypeScript Interfaces

```typescript
// ==========================================
// Memory Type Definitions
// ==========================================

/**
 * Base interface for all memory records
 */
interface MemoryRecord {
  id: string;                    // UUID
  memoryType: 'episodic' | 'semantic' | 'procedural' | 'profile';
  createdAt: string;             // ISO 8601
  updatedAt: string;             // ISO 8601
  tags: string[];                // Searchable tags
  domain: string;                // Primary domain (devops, code-gen, security, testing, docs)
  embedding?: number[];          // 1024-dim Titan embedding vector
  relevanceScore: number;        // Computed composite score (0-1)
  useCount: number;              // Times this memory was retrieved and used
  lastUsed?: string;             // ISO 8601
  ttl?: number;                  // DynamoDB TTL (Unix timestamp for auto-expiry)
}

/**
 * Episodic Memory — "What happened during a specific agent run"
 *
 * Example: "Last time I deployed to EKS, the pods crashed because of
 *           missing env var BG_TOKEN_SECRET_KEY in the ConfigMap"
 */
interface EpisodicMemory extends MemoryRecord {
  memoryType: 'episodic';
  issueNumber: number;           // GitHub issue that triggered this
  agentRole: string;             // Role of the agent (devops, code-gen, etc.)
  taskType: string;              // What the agent was doing
  context: string;               // Brief description of the situation
  action: string;                // What the agent did
  outcome: 'success' | 'failure' | 'partial';
  errorMessage?: string;         // If failure, the error
  resolution?: string;           // How it was resolved
  summary: string;               // One-line summary for quick scanning
  sessionDuration?: number;      // Minutes
  modelCost?: number;            // USD spent on this session
}

/**
 * Semantic Memory — "A verified fact or knowledge item"
 *
 * Example: "EKS Auto Mode requires sts:TagSession on the cluster
 *           role trust policy for instance profile creation"
 */
interface SemanticMemory extends MemoryRecord {
  memoryType: 'semantic';
  fact: string;                  // The knowledge statement
  source: string;                // Where this was learned (issue, docs, experience)
  confidence: number;            // 0-1, increases with verification
  verifiedCount: number;         // How many agents have confirmed this
  lastVerified?: string;         // ISO 8601
  contradictions?: string[];     // Known exceptions or caveats
}

/**
 * Procedural Memory — "A step-by-step procedure for a known task"
 *
 * Example: "To fix CrashLoopBackOff: 1) check pod logs,
 *           2) check env vars, 3) check image tag"
 */
interface ProceduralMemory extends MemoryRecord {
  memoryType: 'procedural';
  taskPattern: string;           // What triggers this procedure (e.g., "CrashLoopBackOff")
  preconditions: string[];       // What must be true before executing
  steps: ProcedureStep[];        // Ordered steps
  expectedOutcome: string;       // What success looks like
  successRate: number;           // 0-1, based on past executions
  executionCount: number;        // How many times this has been followed
  averageDuration?: number;      // Minutes
}

interface ProcedureStep {
  order: number;
  description: string;
  command?: string;              // Optional CLI command
  expectedOutput?: string;       // What to look for
  onFailure?: string;            // What to do if this step fails
}

/**
 * Agent Profile — "An agent's track record and specialisation"
 *
 * Example: "I have successfully completed 15 Terraform tasks
 *           with 80% first-attempt success rate"
 */
interface AgentProfile extends MemoryRecord {
  memoryType: 'profile';
  agentId: string;               // Unique agent identifier
  totalTasks: number;
  successfulTasks: number;
  failedTasks: number;
  successRate: number;           // 0-1
  domainMetrics: DomainMetric[];
  commonErrors: ErrorFrequency[];
  preferredStrategies: string[];
  lastActive: string;            // ISO 8601
}

interface DomainMetric {
  domain: string;                // e.g., "devops", "code-gen", "security"
  taskCount: number;
  successRate: number;
  averageDuration: number;       // Minutes
  lastTask: string;              // ISO 8601
}

interface ErrorFrequency {
  errorPattern: string;          // e.g., "CrashLoopBackOff"
  count: number;
  lastOccurrence: string;
  resolutionRate: number;        // How often this agent resolves this error
}

// ==========================================
// Memory Service Interface
// ==========================================

interface MemoryServiceConfig {
  tableName: string;             // DynamoDB table name
  region: string;                // AWS region
  embeddingModel: string;        // Bedrock model ID for embeddings
  maxResults: number;            // Default retrieval limit
  shortTermTTLDays: number;      // TTL for short-term memories (default: 30)
  similarityThreshold: number;   // Cosine threshold for "similar" (default: 0.85)
}

interface RetrievalContext {
  taskDescription: string;       // What the agent is trying to do
  taskDomain: string;            // Detected domain
  agentRole?: string;            // Agent's specialisation
  issueNumber?: number;          // Current issue
  errorContext?: string;         // If retrying, what went wrong
}

interface RetrievalResult {
  memories: MemoryRecord[];      // Ranked by relevance
  totalMatches: number;          // Total candidates before limit
  retrievalTimeMs: number;       // Performance tracking
}

interface MemoryService {
  // Storage
  storeEpisodic(episode: Omit<EpisodicMemory, 'id' | 'createdAt' | 'updatedAt' | 'embedding'>): Promise<string>;
  storeSemantic(knowledge: Omit<SemanticMemory, 'id' | 'createdAt' | 'updatedAt' | 'embedding'>): Promise<string>;
  storeProcedural(procedure: Omit<ProceduralMemory, 'id' | 'createdAt' | 'updatedAt' | 'embedding'>): Promise<string>;

  // Retrieval
  retrieveRelevant(context: RetrievalContext, limit?: number): Promise<RetrievalResult>;
  retrieveByDomain(domain: string, limit?: number): Promise<MemoryRecord[]>;
  retrieveByTags(tags: string[], limit?: number): Promise<MemoryRecord[]>;

  // Feedback
  updateFeedback(memoryId: string, wasUseful: boolean): Promise<void>;

  // Consolidation
  consolidate(sessionId: string): Promise<{ merged: number; created: number; archived: number }>;

  // Profile
  updateProfile(agentId: string, taskResult: TaskResult): Promise<void>;
  getProfile(agentId: string): Promise<AgentProfile | null>;
}

interface TaskResult {
  issueNumber: number;
  domain: string;
  success: boolean;
  duration: number;
  errorsEncountered: string[];
  memoriesUsed: string[];        // IDs of memories that were retrieved
}
```

### 4.2 DynamoDB Table Design

**Single Table Design** with composite keys and GSIs:

```
Table: agent-memories
─────────────────────

Primary Key:
  PK (Partition Key): memoryType#domain    (e.g., "episodic#devops")
  SK (Sort Key):      timestamp#id         (e.g., "2026-02-18T10:30:00Z#uuid")

Attributes:
  id          (S)  - UUID
  memoryType  (S)  - episodic|semantic|procedural|profile
  domain      (S)  - devops|code-gen|security|testing|docs
  tags        (SS) - String set of tags
  summary     (S)  - One-line summary
  data        (S)  - JSON-encoded full memory record
  embedding   (B)  - Binary: 1024-dim float32 vector (4096 bytes)
  relevance   (N)  - Computed score 0-1
  useCount    (N)  - Times used
  createdAt   (S)  - ISO 8601
  updatedAt   (S)  - ISO 8601
  ttl         (N)  - Unix timestamp for auto-expiry
  agentId     (S)  - For profile records
  issueNumber (N)  - For episodic records

GSI-1: ByDomain
  PK: domain
  SK: relevance (descending)
  → Query: "Get top 20 most relevant memories for domain=devops"

GSI-2: ByAgent
  PK: agentId
  SK: updatedAt
  → Query: "Get agent profile and recent memories for agent X"

GSI-3: ByTag
  PK: tag (inverted index — one record per tag per memory)
  SK: createdAt
  → Query: "Get all memories tagged with 'eks-auto-mode'"

GSI-4: ByIssue
  PK: issueNumber
  SK: createdAt
  → Query: "Get all memories from issue #115"
```

**Capacity Estimates:**

| Metric | Phase 1 | Phase 2 | Phase 3 |
|--------|---------|---------|---------|
| Total records | ~300 (imported) | ~500 | ~2,000 |
| Record size (avg) | ~2 KB | ~6 KB (with embedding) | ~6 KB |
| Reads/month | ~1,000 | ~5,000 | ~20,000 |
| Writes/month | ~100 | ~300 | ~1,000 |
| **Estimated cost** | **~$0.10/month** | **~$0.50/month** | **~$2/month** |

### 4.3 Integration Code Examples

#### 4.3.1 MemoryService Class Interface

```typescript
// .github-agent/agent/src/services/MemoryService.ts

import { DynamoDBClient, QueryCommand, PutItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import { v4 as uuid } from 'uuid';

export class MemoryService {
  private dynamo: DynamoDBClient;
  private bedrock: BedrockRuntimeClient;
  private tableName: string;
  private embeddingModel: string;

  constructor(config: MemoryServiceConfig) {
    this.dynamo = new DynamoDBClient({ region: config.region });
    this.bedrock = new BedrockRuntimeClient({ region: config.region });
    this.tableName = config.tableName;
    this.embeddingModel = config.embeddingModel || 'amazon.titan-embed-text-v2:0';
  }

  /**
   * Store an episodic memory from a completed agent session
   */
  async storeEpisodic(episode: Omit<EpisodicMemory, 'id' | 'createdAt' | 'updatedAt' | 'embedding'>): Promise<string> {
    const id = uuid();
    const now = new Date().toISOString();

    // Generate embedding for the episode summary + context
    const textToEmbed = `${episode.summary}. ${episode.context}. ${episode.resolution || ''}`;
    const embedding = await this.generateEmbedding(textToEmbed);

    await this.dynamo.send(new PutItemCommand({
      TableName: this.tableName,
      Item: {
        PK: { S: `episodic#${episode.domain}` },
        SK: { S: `${now}#${id}` },
        id: { S: id },
        memoryType: { S: 'episodic' },
        domain: { S: episode.domain },
        tags: { SS: episode.tags },
        summary: { S: episode.summary },
        data: { S: JSON.stringify({ ...episode, id, createdAt: now, updatedAt: now }) },
        embedding: { B: new Float32Array(embedding).buffer as any },
        relevance: { N: '0.5' },
        useCount: { N: '0' },
        createdAt: { S: now },
        updatedAt: { S: now },
        issueNumber: { N: String(episode.issueNumber) },
        // Short-term: auto-expire after 30 days
        ttl: { N: String(Math.floor(Date.now() / 1000) + 30 * 86400) },
      },
    }));

    return id;
  }

  /**
   * Retrieve relevant memories for a given task context
   * Uses composite scoring: tag match + recency + usefulness + domain
   */
  async retrieveRelevant(context: RetrievalContext, limit: number = 10): Promise<RetrievalResult> {
    const startTime = Date.now();

    // Step 1: Query by domain (most relevant partition)
    const domainResults = await this.queryByDomain(context.taskDomain, 50);

    // Step 2: Query by detected tags (if error context provided)
    const tagResults = context.errorContext
      ? await this.queryByTags(this.extractTags(context.errorContext), 20)
      : [];

    // Step 3: Merge and deduplicate
    const allCandidates = this.deduplicateById([...domainResults, ...tagResults]);

    // Step 4: Score and rank
    const taskEmbedding = await this.generateEmbedding(context.taskDescription);
    const scored = allCandidates.map(memory => ({
      memory,
      score: this.computeCompositeScore(memory, taskEmbedding, context),
    }));

    scored.sort((a, b) => b.score - a.score);

    const topK = scored.slice(0, limit);

    return {
      memories: topK.map(s => ({ ...s.memory, relevanceScore: s.score })),
      totalMatches: allCandidates.length,
      retrievalTimeMs: Date.now() - startTime,
    };
  }

  /**
   * Composite scoring inspired by CrewAI + RCR-Router
   */
  private computeCompositeScore(
    memory: MemoryRecord,
    taskEmbedding: number[],
    context: RetrievalContext
  ): number {
    // Semantic similarity (0-1)
    const similarity = memory.embedding
      ? this.cosineSimilarity(taskEmbedding, memory.embedding)
      : 0;

    // Recency score — exponential decay over 30 days
    const daysSinceCreation = (Date.now() - new Date(memory.createdAt).getTime()) / 86400000;
    const recency = Math.exp(-daysSinceCreation / 30);

    // Usefulness score — saturating function
    const usefulness = memory.useCount / (memory.useCount + 5);

    // Domain match bonus
    const domainMatch = memory.domain === context.taskDomain ? 1.0
                      : this.hasOverlappingTags(memory, context) ? 0.5
                      : 0.0;

    return (0.4 * similarity) + (0.3 * recency) + (0.2 * usefulness) + (0.1 * domainMatch);
  }

  private cosineSimilarity(a: number[], b: number[]): number {
    let dotProduct = 0, normA = 0, normB = 0;
    for (let i = 0; i < a.length; i++) {
      dotProduct += a[i] * b[i];
      normA += a[i] * a[i];
      normB += b[i] * b[i];
    }
    return dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
  }

  private async generateEmbedding(text: string): Promise<number[]> {
    const response = await this.bedrock.send(new InvokeModelCommand({
      modelId: this.embeddingModel,
      contentType: 'application/json',
      body: JSON.stringify({ inputText: text }),
    }));

    const result = JSON.parse(new TextDecoder().decode(response.body));
    return result.embedding;
  }

  /**
   * Update feedback — track which memories were actually useful
   */
  async updateFeedback(memoryId: string, wasUseful: boolean): Promise<void> {
    // Increment or decrement the useCount
    await this.dynamo.send(new UpdateItemCommand({
      TableName: this.tableName,
      Key: { /* lookup by id via GSI */ },
      UpdateExpression: wasUseful
        ? 'ADD useCount :inc SET lastUsed = :now, updatedAt = :now'
        : 'SET updatedAt = :now',
      ExpressionAttributeValues: {
        ':inc': { N: '1' },
        ':now': { S: new Date().toISOString() },
      },
    }));
  }

  /**
   * Post-session consolidation
   */
  async consolidate(sessionId: string): Promise<{ merged: number; created: number; archived: number }> {
    let merged = 0, created = 0, archived = 0;

    // 1. Get all episodic memories from this session
    const sessionMemories = await this.queryBySession(sessionId);

    // 2. For each, check if similar semantic memory exists
    for (const episode of sessionMemories) {
      const embedding = episode.embedding!;
      const existingSemantics = await this.queryByDomain(episode.domain, 100);

      const similar = existingSemantics.find(s =>
        s.memoryType === 'semantic' &&
        s.embedding &&
        this.cosineSimilarity(embedding, s.embedding) > 0.85
      );

      if (similar) {
        // Merge: increment verification count
        await this.incrementVerification(similar.id);
        merged++;
      } else {
        // Create new semantic memory
        await this.storeSemantic({
          domain: episode.domain,
          fact: (episode as EpisodicMemory).resolution || (episode as EpisodicMemory).summary,
          source: `Issue #${(episode as EpisodicMemory).issueNumber}`,
          confidence: 0.5,
          verifiedCount: 1,
          tags: episode.tags,
          memoryType: 'semantic',
          relevanceScore: 0.5,
          useCount: 0,
        });
        created++;
      }
    }

    // 3. Archive old, unused memories
    const staleMemories = await this.queryStale(90); // Not used in 90 days
    for (const stale of staleMemories) {
      await this.archiveToS3(stale);
      archived++;
    }

    return { merged, created, archived };
  }
}
```

#### 4.3.2 Modified CodeGenerationAgent.buildCodeGenPrompt()

```typescript
// BEFORE (current — lines 279-286 of CodeGenerationAgent.ts):
// The prompt includes a flat "Write Learnings Before Finishing" section
// with no retrieved memories. Agents read ALL learnings via file system.

// AFTER (proposed):
private buildCodeGenPrompt(
  plan: Plan,
  projectDir: string,
  issueContext?: IssueContext,
  relevantMemories?: RetrievalResult  // NEW PARAMETER
): string {
  // ... existing prompt building ...

  // Replace flat learnings injection with targeted memories
  const memorySection = relevantMemories && relevantMemories.memories.length > 0
    ? this.formatMemoriesForPrompt(relevantMemories)
    : '';

  return `${existingPrompt}

## Relevant Past Learnings (Retrieved from Memory)
${memorySection || 'No relevant past learnings found for this task.'}

## MANDATORY: Store Learnings After Completion
After completing your work, the orchestrator will automatically store your learnings.
Focus on documenting:
- Errors encountered and their root causes
- Workarounds that worked
- Non-obvious configuration requirements
- Things that didn't work and why`;
}

private formatMemoriesForPrompt(result: RetrievalResult): string {
  let output = `*${result.memories.length} relevant memories retrieved (${result.retrievalTimeMs}ms)*\n\n`;

  for (const memory of result.memories) {
    const data = JSON.parse((memory as any).data || '{}');

    if (memory.memoryType === 'episodic') {
      output += `### 📝 Episode: ${data.summary}\n`;
      output += `- **Issue**: #${data.issueNumber} | **Domain**: ${data.domain}\n`;
      output += `- **Error**: ${data.errorMessage || 'N/A'}\n`;
      output += `- **Resolution**: ${data.resolution || 'N/A'}\n`;
      output += `- **Relevance**: ${(memory.relevanceScore * 100).toFixed(0)}%\n\n`;
    } else if (memory.memoryType === 'semantic') {
      output += `### 💡 Knowledge: ${data.fact}\n`;
      output += `- **Confidence**: ${(data.confidence * 100).toFixed(0)}% (verified ${data.verifiedCount}x)\n`;
      output += `- **Domain**: ${data.domain}\n\n`;
    } else if (memory.memoryType === 'procedural') {
      output += `### 📋 Procedure: ${data.taskPattern}\n`;
      output += `- **Success Rate**: ${(data.successRate * 100).toFixed(0)}% (${data.executionCount} runs)\n`;
      data.steps?.forEach((step: any) => {
        output += `  ${step.order}. ${step.description}\n`;
      });
      output += '\n';
    }
  }

  return output;
}
```

#### 4.3.3 Modified AgentOrchestrator.run()

```typescript
// BEFORE (current — AgentOrchestrator.ts lines 46-124):
// No memory integration. State is ephemeral.

// AFTER (proposed):
async run(issueContext: IssueContext): Promise<void> {
  // ... existing setup ...

  // NEW: Initialize memory service
  const memoryService = new MemoryService({
    tableName: this.config.memoryTableName || 'agent-memories',
    region: this.config.awsRegion,
    embeddingModel: 'amazon.titan-embed-text-v2:0',
    maxResults: 10,
    shortTermTTLDays: 30,
    similarityThreshold: 0.85,
  });

  try {
    // NEW: Retrieve relevant memories BEFORE planning
    const taskContext: RetrievalContext = {
      taskDescription: `${issueContext.issueTitle}. ${issueContext.issueBody?.substring(0, 500)}`,
      taskDomain: this.detectDomain(issueContext),
      issueNumber: issueContext.issueNumber,
    };

    const relevantMemories = await memoryService.retrieveRelevant(taskContext);
    console.log(`📚 Retrieved ${relevantMemories.memories.length} relevant memories (${relevantMemories.retrievalTimeMs}ms)`);

    // Pass memories to planning and code generation
    await this.handlePlanning(repoDir, relevantMemories);
    // ... existing approval loop ...
    await this.handleCodeGeneration(repoDir, relevantMemories);

    // NEW: Store episode AFTER completion
    const sessionDuration = (Date.now() - new Date(this.state!.startTime).getTime()) / 60000;
    await memoryService.storeEpisodic({
      memoryType: 'episodic',
      issueNumber: issueContext.issueNumber,
      agentRole: this.detectDomain(issueContext),
      taskType: issueContext.issueTitle,
      domain: this.detectDomain(issueContext),
      context: issueContext.issueBody?.substring(0, 500) || '',
      action: this.state!.plan?.summary || '',
      outcome: this.state!.phase === 'complete' ? 'success' : 'failure',
      summary: `Issue #${issueContext.issueNumber}: ${issueContext.issueTitle}`,
      tags: this.extractTags(issueContext),
      relevanceScore: 0.5,
      useCount: 0,
      sessionDuration,
    });

    // NEW: Run consolidation
    await memoryService.consolidate(`session-${issueContext.issueNumber}`);

    // NEW: Update feedback — mark which memories were useful
    for (const memory of relevantMemories.memories) {
      // Simple heuristic: if task succeeded, all retrieved memories get positive feedback
      await memoryService.updateFeedback(memory.id, this.state!.phase === 'complete');
    }

  } catch (err) {
    // ... existing error handling ...
  }
}

/**
 * Detect task domain from issue context
 */
private detectDomain(issueContext: IssueContext): string {
  const text = `${issueContext.issueTitle} ${issueContext.issueBody || ''}`.toLowerCase();

  const domainKeywords: Record<string, string[]> = {
    'devops': ['terraform', 'eks', 'kubernetes', 'deploy', 'pipeline', 'ci/cd', 'infrastructure', 'helm', 'docker'],
    'code-gen': ['implement', 'feature', 'endpoint', 'api', 'service', 'function', 'class', 'module'],
    'security': ['security', 'auth', 'iam', 'cognito', 'jwt', 'encryption', 'vulnerability', 'audit'],
    'testing': ['test', 'e2e', 'unit test', 'integration test', 'coverage', 'fixture'],
    'docs': ['documentation', 'readme', 'research', 'openapi', 'schema', 'diagram'],
  };

  let bestDomain = 'code-gen';
  let bestScore = 0;

  for (const [domain, keywords] of Object.entries(domainKeywords)) {
    const score = keywords.filter(kw => text.includes(kw)).length;
    if (score > bestScore) {
      bestScore = score;
      bestDomain = domain;
    }
  }

  return bestDomain;
}
```

#### 4.3.4 Post-Session Consolidation Function

```typescript
/**
 * Runs after each agent session to consolidate learnings
 * Inspired by TraceMem's three-stage consolidation
 */
async function postSessionConsolidation(
  memoryService: MemoryService,
  sessionEpisodes: EpisodicMemory[],
  learningsFilePath: string
): Promise<void> {
  console.log(`🔄 Starting post-session consolidation (${sessionEpisodes.length} episodes)...`);

  // Stage 1: Parse raw learnings file if it exists
  if (fs.existsSync(learningsFilePath)) {
    const content = fs.readFileSync(learningsFilePath, 'utf-8');
    const parsedLearnings = parseLearningsMarkdown(content);

    for (const learning of parsedLearnings) {
      // Check if this learning already exists as a semantic memory
      const existing = await memoryService.findSimilar(learning.summary, 0.85);

      if (existing) {
        // Merge: update confidence and verification
        console.log(`  ✅ Merged with existing: "${existing.summary.substring(0, 60)}..."`);
      } else {
        // Create new semantic memory
        await memoryService.storeSemantic({
          domain: learning.domain,
          fact: learning.content,
          source: learningsFilePath,
          confidence: 0.5,
          verifiedCount: 1,
          tags: learning.tags,
          memoryType: 'semantic',
          relevanceScore: 0.5,
          useCount: 0,
        });
        console.log(`  🆕 Created: "${learning.summary.substring(0, 60)}..."`);
      }
    }
  }

  // Stage 2: Extract procedural patterns from error/fix pairs
  const errorFixPairs = sessionEpisodes
    .filter(e => e.outcome === 'success' && e.errorMessage && e.resolution);

  for (const pair of errorFixPairs) {
    // Check if a procedure already exists for this error pattern
    const existingProcedure = await memoryService.findProcedure(pair.errorMessage!);

    if (existingProcedure) {
      // Update success rate
      await memoryService.updateProcedureStats(existingProcedure.id, true);
      console.log(`  📋 Updated procedure: "${existingProcedure.taskPattern}"`);
    }
    // New procedures are created manually during Phase 3
  }

  // Stage 3: Update agent profile
  const agentId = process.env.AGENT_ID || 'default';
  for (const episode of sessionEpisodes) {
    await memoryService.updateProfile(agentId, {
      issueNumber: episode.issueNumber,
      domain: episode.domain,
      success: episode.outcome === 'success',
      duration: episode.sessionDuration || 0,
      errorsEncountered: episode.errorMessage ? [episode.errorMessage] : [],
      memoriesUsed: [],
    });
  }

  console.log(`🔄 Consolidation complete.`);
}

/**
 * Parse a learnings markdown file into structured learning items
 */
function parseLearningsMarkdown(content: string): ParsedLearning[] {
  const learnings: ParsedLearning[] = [];

  // Extract YAML front matter
  const frontMatterMatch = content.match(/^---\n([\s\S]*?)\n---/);
  const tags = frontMatterMatch
    ? extractYamlTags(frontMatterMatch[1])
    : [];

  // Split by ## headers (each is a separate learning)
  const sections = content.split(/\n## /).slice(1); // Skip title

  for (const section of sections) {
    const lines = section.trim().split('\n');
    const title = lines[0].replace(/^#+\s*/, '').replace(/^\d+\.\s*/, '');
    const body = lines.slice(1).join('\n').trim();

    // Extract inline tags
    const inlineTags = body.match(/`tags:\s*(.*?)`/)?.[1]?.split(',').map(t => t.trim()) || [];

    // Detect domain from tags
    const domain = detectDomainFromTags([...tags, ...inlineTags]);

    // Extract error/fix if present
    const errorMatch = body.match(/\*\*(?:Fix|Solution|Resolution)\*\*:?\s*([\s\S]*?)(?=\n\n|$)/);

    learnings.push({
      summary: title,
      content: body.substring(0, 1000),
      domain,
      tags: [...new Set([...tags, ...inlineTags])],
      resolution: errorMatch?.[1]?.trim(),
    });
  }

  return learnings;
}
```

---

## Appendix A: Research Sources

Full details for all 30+ researched papers, open source projects, and commercial products are available in:
- [agent-memory-research-sources.csv](./agent-memory-research-sources.csv)

## Appendix B: Current System Analysis

### Files Analysed

| File | Key Finding |
|------|------------|
| `agent_learning/*.md` (13 files) | ~250 discrete learnings across 13 flat markdown files. Rich content but no structured retrieval. YAML tags exist but unused for filtering. |
| `.github-agent/CLAUDE.md` | Stage 7 mandates learnings but doesn't specify how they're consumed by future agents. |
| `CodeGenerationAgent.ts` | Lines 279-286 inject "Write Learnings" instruction but don't retrieve past learnings. The prompt is built fresh each time. |
| `PlanningAgent.ts` | `generatePlan()` (line 18) takes only `issueContext` — no past learnings or memory input. |
| `ConfigLoader.ts` | Only AWS/Bedrock config. No memory-related settings. |
| `AgentOrchestrator.ts` | `state` (line 56) is ephemeral — reset each run. No cross-session persistence. No memory service integration. |

### Current Learning File Analysis

| Learning File | Domain | Learnings Count | Key Topics |
|--------------|--------|----------------|------------|
| `2026-02-15-session-learnings.md` | DevOps, CI/CD | 20 | EKS pod eviction, GitHub token, Terraform state, container builds |
| `2026-02-16-deployment-learnings.md` | DevOps, EKS | 20 | EKS Auto Mode, sts:TagSession, deployment wiring |
| `2026-02-17-ci-fix-learnings.md` | CI, Code Quality | 6 | Ruff lint, TypeScript, frontend components |
| `2026-02-17-cloudfront-vpc-origin.md` | Infrastructure, Security | 6 | CloudFront VPC Origin, SSE streaming |
| `2026-02-17-deploy-pipeline-fixes.md` | CI/CD | 4 | JMESPath, CloudFront distribution lookup |
| `2026-02-17-e2e-test-learnings.md` | Testing, Auth | 7 | Cognito JWT validation, M2M auth |
| `2026-02-17-environment-repeatability.md` | DevOps, Database | 7 | Security groups, ConfigMap templating, Alembic |
| `2026-02-17-issue-124-learnings.md` | Auth, Infrastructure | 3 | Cognito agent credentials, Secrets Manager |
| `2026-02-17-issue-128-documentation-learnings.md` | Documentation | 6 | OpenAPI, database schema, sequence diagrams |
| `2026-02-17-issue-129-security-review-learnings.md` | Security | 5 | Admin auth bypass, deprecated endpoints |
| `2026-02-17-issue-133-security-fixes.md` | Security | 6 | Auth dependency injection, feature flags |
| `2026-02-17-unified-cognito-auth.md` | Auth | 5 | Cognito JWT, resource server, pre-token Lambda |
| `2026-02-18-session-learnings.md` | DevOps, Auth | 18 | E2E wiring, CloudFront VPC Origin, Bedrock proxy |

**Total**: ~113 distinct learning items across ~13 domain areas.

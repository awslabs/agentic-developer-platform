# Design Proposal: Persistent Memory for GitHub Actions Agent

**Issue**: #141
**Date**: 2026-02-18
**Status**: Proposal
**Related**: [Agent Memory Research](./agent-memory-research.md) | [Sources CSV](./agent-memory-research-sources.csv)

---

## 1. Problem Statement

### Current System

Our GitHub Actions agent uses a flat file-based learning system (`agent_learning/`) where agents write markdown files after each run. Before starting, agents are instructed to "read ALL files in `agent_learning/` folder." This approach has served us through 13 learning files and ~113 discrete learnings, but it has fundamental limitations:

### Specific Limitations

| # | Limitation | Evidence | Impact |
|---|-----------|----------|--------|
| 1 | **No structured retrieval** | `CodeGenerationAgent.ts:279-286` injects "Write Learnings" but doesn't retrieve past ones. Agents must read all 13 files manually. | Agent wastes context window on irrelevant learnings. A DevOps task gets frontend CI fix tips. |
| 2 | **No specialisation** | `PlanningAgent.ts:18` — `generatePlan()` takes only `issueContext`, no agent role or past performance data. | A security agent doesn't know it has 90% success rate on IAM tasks. A DevOps agent doesn't know it consistently struggles with Cognito. |
| 3 | **No feedback loop** | No mechanism anywhere in the agent code to track which learnings actually helped. | Useless learnings accumulate at the same priority as critical ones. |
| 4 | **No cross-session state** | `AgentOrchestrator.ts:56` — `state` is an in-memory object reset every run. | Agent can't remember what it tried on a previous run of the same issue. Retries repeat the same failed approaches. |
| 5 | **No pattern recognition** | `agent_learning/*.md` files are flat markdown with YAML tags, but tags are never queried programmatically. | The same "sts:TagSession" error appears in 2 files but isn't automatically surfaced as a high-priority verified pattern. |

### Scale of the Problem

With 13 files totaling ~40KB of text, agents must process all of it to find potentially relevant learnings. As the system grows to 50+ files, this will:
- Consume 10-15% of the context window just for learnings
- Increase token costs by ~$0.02 per run
- Dilute signal-to-noise ratio as irrelevant learnings outnumber relevant ones

---

## 2. Proposed Solution Overview

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          GITHUB ACTIONS WORKFLOW                        │
│                                                                         │
│  ┌─────────────────────┐                                               │
│  │  Issue Trigger       │──── Issue #N assigned to agent               │
│  └──────────┬──────────┘                                               │
│             │                                                           │
│  ┌──────────▼──────────┐     ┌──────────────────────────┐             │
│  │  AgentOrchestrator   │────▶│  MemoryService           │             │
│  │                      │     │                          │             │
│  │  1. Detect domain    │     │  retrieveRelevant()      │             │
│  │  2. Retrieve memories│◀────│  → Tag filter            │             │
│  │  3. Generate plan    │     │  → Recency scoring       │             │
│  │  4. Execute code gen │     │  → Vector similarity     │◀──┐        │
│  │  5. Store episode    │────▶│  → Composite ranking     │   │        │
│  │  6. Run consolidation│────▶│                          │   │        │
│  │  7. Update feedback  │────▶│  storeEpisodic()         │   │        │
│  └──────────────────────┘     │  consolidate()           │   │        │
│                               │  updateFeedback()        │   │        │
│                               └──────────┬───────────────┘   │        │
│                                          │                    │        │
└──────────────────────────────────────────┼────────────────────┼────────┘
                                           │                    │
                              ┌────────────▼──────────┐   ┌────▼────────┐
                              │      DynamoDB          │   │   Bedrock   │
                              │   agent-memories       │   │   Titan     │
                              │                        │   │   Embeddings│
                              │  Episodic memories     │   └─────────────┘
                              │  Semantic knowledge    │
                              │  Procedural skills     │
                              │  Agent profiles        │
                              │                        │
                              │  GSIs for:             │
                              │  - ByDomain            │
                              │  - ByAgent             │
                              │  - ByTag               │
                              │  - ByIssue             │
                              │                        │
                              │  TTL for auto-expiry   │
                              └────────────┬───────────┘
                                           │
                              ┌────────────▼──────────┐
                              │      S3 Archive        │
                              │  Raw learning files    │
                              │  Archived memories     │
                              │  Versioned history     │
                              └────────────────────────┘
```

### Key Design Decisions

1. **Custom DynamoDB solution** over third-party frameworks — avoids vendor lock-in, leverages existing AWS stack
2. **Tag-based retrieval first, vectors second** — simpler, faster, and sufficient for <1K records
3. **Post-session consolidation** — memory improvement happens asynchronously, not during time-critical agent execution
4. **Four memory types** — Episodic, Semantic, Procedural, and Agent Profile

---

## 3. Recommended Approach

### Build Custom on AWS (DynamoDB + Titan Embeddings + S3)

After evaluating 7 open-source frameworks and 8 commercial products (see [research document](./agent-memory-research.md)), we recommend building a custom memory system. Here's why:

### Decision Matrix: Build vs Buy

| Criterion (Weight) | Custom DynamoDB | Mem0 Platform | Zep Cloud | Letta (self-hosted) | Bedrock Agent Memory |
|-------------------|:-:|:-:|:-:|:-:|:-:|
| **AWS-native (25%)** | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **TypeScript SDK (20%)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Control & Customisation (20%)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐ |
| **Operational Simplicity (15%)** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐ |
| **Cost at our scale (10%)** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ |
| **No vendor lock-in (10%)** | ⭐⭐⭐⭐⭐ | ⭐⭐ | ⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Weighted Score** | **4.65** | **3.25** | **2.90** | **3.30** | **3.55** |

**Winner: Custom DynamoDB solution** — scores highest on AWS-native integration, TypeScript SDK availability, customisation flexibility, and cost.

### Why Not the Alternatives?

| Option | Why Not |
|--------|---------|
| **Mem0** | Excellent product but adds external API dependency. We'd be sending our agent learnings (which contain AWS error messages and infrastructure details) to a third-party service. For a security-conscious project, keeping data within our AWS account is preferred. |
| **Zep Cloud** | Community Edition deprecated. Cloud-only adds vendor dependency. Designed for conversational assistants, not task-execution agents. |
| **Letta** | Requires running a PostgreSQL + Python server. Overkill for GitHub Actions runners that start fresh for each job. |
| **Bedrock Agent Memory** | Limited to Bedrock Agent framework. Our agent uses Claude Code SDK directly, not Bedrock Agents. Session memory only — no custom memory types or consolidation. |

---

## 4. Integration Points

### Exact Files to Modify

#### 4.1 `ConfigLoader.ts` — Add Memory Configuration

```typescript
// CURRENT (lines 8-26):
async load(): Promise<Config> {
  this.config = {
    awsRegion,
    secretPrefix: process.env.SECRET_PREFIX || 'github-agent',
    pollingInterval: parseInt(process.env.POLLING_INTERVAL || '30000', 10),
    maxRetries: parseInt(process.env.MAX_RETRIES || '5', 10),
    logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'INFO',
    bedrockModel: process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-4-5-20251101-v1:0',
  };
  // ...
}

// PROPOSED (add memory config):
async load(): Promise<Config> {
  this.config = {
    awsRegion,
    secretPrefix: process.env.SECRET_PREFIX || 'github-agent',
    pollingInterval: parseInt(process.env.POLLING_INTERVAL || '30000', 10),
    maxRetries: parseInt(process.env.MAX_RETRIES || '5', 10),
    logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'INFO',
    bedrockModel: process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-4-5-20251101-v1:0',
    // NEW: Memory configuration
    memoryEnabled: process.env.MEMORY_ENABLED !== 'false',  // Default: enabled
    memoryTableName: process.env.MEMORY_TABLE_NAME || 'agent-memories',
    memoryMaxResults: parseInt(process.env.MEMORY_MAX_RESULTS || '10', 10),
    memoryEmbeddingModel: process.env.MEMORY_EMBEDDING_MODEL || 'amazon.titan-embed-text-v2:0',
    memoryShortTermTTLDays: parseInt(process.env.MEMORY_SHORT_TERM_TTL_DAYS || '30', 10),
  };
  // ...
}
```

#### 4.2 `types/index.ts` — Add Memory Type Definitions

```typescript
// ADD to existing types/index.ts:

export interface MemoryConfig {
  memoryEnabled: boolean;
  memoryTableName: string;
  memoryMaxResults: number;
  memoryEmbeddingModel: string;
  memoryShortTermTTLDays: number;
}

export interface Config extends MemoryConfig {
  awsRegion: string;
  secretPrefix: string;
  pollingInterval: number;
  maxRetries: number;
  logLevel: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
  bedrockModel: string;
}

// Memory record types (see full definitions in research doc Part 4)
export interface MemoryRecord { /* ... */ }
export interface EpisodicMemory extends MemoryRecord { /* ... */ }
export interface SemanticMemory extends MemoryRecord { /* ... */ }
export interface ProceduralMemory extends MemoryRecord { /* ... */ }
export interface AgentProfile extends MemoryRecord { /* ... */ }
```

#### 4.3 `CodeGenerationAgent.ts` — Inject Relevant Memories (lines 159-287)

**Current** (`buildCodeGenPrompt` at line 159): Builds a static prompt with no past learnings. The "MANDATORY: Write Learnings" section (lines 279-286) tells the agent to write learnings but never injects previously learned information.

**Proposed**: Add `relevantMemories` parameter and inject a `## Relevant Past Learnings` section before the execution rules:

```typescript
// Line 16: Change signature
async executePlan(
  plan: Plan,
  workDir: string,
  issueNumber: number,
  issueContext?: IssueContext,
  relevantMemories?: RetrievalResult  // NEW
): Promise<CodeResult> {
  // Line 36: Pass memories to prompt builder
  const prompt = this.buildCodeGenPrompt(plan, projectDir, issueContext, relevantMemories);
  // ... rest unchanged
}

// Line 159: Change signature
private buildCodeGenPrompt(
  plan: Plan,
  projectDir: string,
  issueContext?: IssueContext,
  relevantMemories?: RetrievalResult  // NEW
): string {
  // ... existing prompt ...

  // INSERT before "## Execution Rules" (before line 234):
  const memorySection = relevantMemories?.memories.length
    ? this.formatMemoriesForPrompt(relevantMemories)
    : '';

  // Replace lines 279-286 with:
  return `${existingPromptUpToExecutionRules}

## Relevant Past Learnings (${relevantMemories?.memories.length || 0} memories)
${memorySection || 'No past learnings found for this task type.'}

## MANDATORY: Store Learnings Before Finishing
Before you finish, create a learnings file at \`agent_learning/{date}-issue-{issue_number}-learnings.md\`.
The orchestrator will also automatically store structured memories from this session.

${executionRulesSection}`;
}
```

#### 4.4 `PlanningAgent.ts` — Include Past Learnings in Planning (lines 94-149)

**Current** (`buildPlanningPrompt` at line 94): The planning prompt includes repository structure and issue context but no past learnings.

**Proposed**: Add relevant memories to the planning context:

```typescript
// Line 18: Change signature
async generatePlan(
  issueContext: IssueContext,
  workDir: string,
  feedback?: string,
  previousPlan?: Plan,
  relevantMemories?: RetrievalResult  // NEW
): Promise<Plan> {
  // Line 40: Pass memories to prompt builder
  const prompt = isRevision
    ? this.buildRevisionPrompt(issueContext, feedback!, previousPlan!, repoContext)
    : this.buildPlanningPrompt(issueContext, repoContext, relevantMemories);
  // ...
}

// Line 94: Change signature
private buildPlanningPrompt(
  issueContext: IssueContext,
  repoContext: { structure: string; fileCount: number },
  relevantMemories?: RetrievalResult  // NEW
): string {
  // INSERT after "## GitHub Issue" section:
  const memoryContext = relevantMemories?.memories.length
    ? `
## Past Learnings (from previous agent sessions)
The following learnings are relevant to this task based on domain matching and semantic similarity:

${relevantMemories.memories.map((m, i) => {
  const data = JSON.parse((m as any).data || '{}');
  return `${i + 1}. [${m.domain}] ${data.summary || data.fact || data.taskPattern}
   Relevance: ${(m.relevanceScore * 100).toFixed(0)}% | Used: ${m.useCount} times`;
}).join('\n')}

Consider these learnings when creating your plan. If a past learning indicates a known issue or solution, incorporate it into the plan steps.
`
    : '';

  return `${existingPrompt}${memoryContext}${yourTaskSection}`;
}
```

#### 4.5 `AgentOrchestrator.ts` — Memory Lifecycle (lines 46-124)

**Current** (`run` at line 46): No memory integration. State is ephemeral.

**Proposed**: Add MemoryService to the orchestrator lifecycle:

```typescript
// Line 24: Add import
import { MemoryService } from '../services/MemoryService';

// Line 26-44: Add to constructor
constructor(
  // ... existing params ...
  private memoryService?: MemoryService  // NEW (optional for backward compat)
) { /* ... */ }

// Line 46: Modify run()
async run(issueContext: IssueContext): Promise<void> {
  // ... existing workspace setup (lines 47-65) ...

  // NEW: Retrieve relevant memories (after line 54)
  let relevantMemories: RetrievalResult | undefined;
  if (this.memoryService) {
    try {
      const taskContext = {
        taskDescription: `${issueContext.issueTitle}. ${issueContext.issueBody?.substring(0, 500)}`,
        taskDomain: this.detectDomain(issueContext),
        issueNumber: issueContext.issueNumber,
      };
      relevantMemories = await this.memoryService.retrieveRelevant(taskContext);
      console.log(`📚 Retrieved ${relevantMemories.memories.length} relevant memories`);
    } catch (err) {
      console.log(`⚠️ Memory retrieval failed (continuing without): ${(err as Error).message}`);
    }
  }

  try {
    // Line 68: Pass memories to planning
    await this.handlePlanning(repoDir, relevantMemories);
    // ... existing approval loop (lines 71-104) ...
    // Line 106: Pass memories to code generation
    await this.handleCodeGeneration(repoDir, relevantMemories);
    // ... existing PR creation (lines 108-110) ...

    // NEW: Post-completion memory operations (after line 113)
    if (this.memoryService) {
      try {
        await this.memoryService.storeEpisodic({
          memoryType: 'episodic',
          issueNumber: issueContext.issueNumber,
          agentRole: this.detectDomain(issueContext),
          taskType: issueContext.issueTitle,
          domain: this.detectDomain(issueContext),
          context: issueContext.issueBody?.substring(0, 500) || '',
          action: this.state!.plan?.summary || '',
          outcome: 'success',
          summary: `Issue #${issueContext.issueNumber}: ${issueContext.issueTitle}`,
          tags: this.extractTags(issueContext),
          relevanceScore: 0.5,
          useCount: 0,
        });
        console.log('📝 Stored episodic memory for this session');

        // Run consolidation
        const stats = await this.memoryService.consolidate(
          `session-${issueContext.issueNumber}`
        );
        console.log(`🔄 Consolidated: ${stats.merged} merged, ${stats.created} created, ${stats.archived} archived`);
      } catch (err) {
        console.log(`⚠️ Memory storage failed (non-blocking): ${(err as Error).message}`);
      }
    }
  } catch (err) {
    // ... existing error handling (lines 118-121) ...
    // NEW: Store failure episode
    if (this.memoryService) {
      try {
        await this.memoryService.storeEpisodic({
          // ... failure record ...
          outcome: 'failure',
          errorMessage: (err as Error).message,
        });
      } catch {} // Silent fail — don't compound errors
    }
  }
}
```

---

## 5. New Components

### 5.1 MemoryService (`services/MemoryService.ts`)

Core service with full CRUD operations, retrieval, consolidation, and feedback tracking. See [research document Part 4.3.1](./agent-memory-research.md#431-memoryservice-class-interface) for complete implementation.

**Key methods:**
- `storeEpisodic(episode)` → Store a learning from agent session
- `storeSemantic(knowledge)` → Store a verified fact
- `storeProcedural(procedure)` → Store a step-by-step procedure
- `retrieveRelevant(context, limit)` → Get top-K relevant memories
- `updateFeedback(memoryId, wasUseful)` → Track which memories helped
- `consolidate(sessionId)` → Merge and promote learnings

### 5.2 MemoryRetriever (internal to MemoryService)

Composite retrieval engine:
1. **Tag filter** — DynamoDB query on domain + tags
2. **Recency scoring** — Exponential decay over 30 days
3. **Vector similarity** — Cosine similarity using Titan Embeddings (Phase 2)
4. **Composite ranking** — Weighted combination: `0.4*similarity + 0.3*recency + 0.2*usefulness + 0.1*domain_match`

### 5.3 ConsolidationJob (runs post-session)

Three-stage consolidation inspired by TraceMem:
1. **Capture**: Parse episodic memories from the session
2. **Structure**: Generate embeddings, match against existing semantics, merge or create
3. **Promote**: Verified knowledge promoted; stale memories archived to S3

### 5.4 MemoryMigration Script

One-time script to import existing `agent_learning/*.md` files:
- Parse markdown headers as individual learnings
- Extract YAML front matter for tags and categories
- Detect domain from tags
- Create DynamoDB records
- Preserve original files as backup in S3

---

## 6. Storage Design

### 6.1 DynamoDB Table: `agent-memories`

```
Table Design (Single-Table Pattern):
─────────────────────────────────────

Primary Key:
  PK: memoryType#domain          (e.g., "episodic#devops")
  SK: timestamp#id               (e.g., "2026-02-18T10:30:00Z#abc123")

Attributes:
┌──────────────┬──────┬────────────────────────────────────────────────┐
│ Attribute    │ Type │ Description                                    │
├──────────────┼──────┼────────────────────────────────────────────────┤
│ PK           │ S    │ Partition key: memoryType#domain               │
│ SK           │ S    │ Sort key: timestamp#id                         │
│ id           │ S    │ UUID                                           │
│ memoryType   │ S    │ episodic|semantic|procedural|profile           │
│ domain       │ S    │ devops|code-gen|security|testing|docs          │
│ tags         │ SS   │ String set of searchable tags                  │
│ summary      │ S    │ One-line summary for quick display             │
│ data         │ S    │ JSON: full memory record (type-specific)       │
│ embedding    │ B    │ Binary: 1024-dim float32 vector (4096 bytes)   │
│ relevance    │ N    │ Composite relevance score (0-1)                │
│ useCount     │ N    │ Times retrieved and used                       │
│ confidence   │ N    │ Verification confidence (0-1)                  │
│ createdAt    │ S    │ ISO 8601 timestamp                             │
│ updatedAt    │ S    │ ISO 8601 timestamp                             │
│ ttl          │ N    │ Unix timestamp for DynamoDB TTL auto-expiry    │
│ agentId      │ S    │ For profile records                            │
│ issueNumber  │ N    │ For episodic records                           │
└──────────────┴──────┴────────────────────────────────────────────────┘

GSI-1: ByDomain
  PK: domain  |  SK: relevance (DESC)
  Purpose: "Get top 20 most relevant memories in domain=devops"
  Projection: ALL

GSI-2: ByAgent
  PK: agentId  |  SK: updatedAt (DESC)
  Purpose: "Get agent profile and recent activity"
  Projection: ALL

GSI-3: ByIssue
  PK: issueNumber  |  SK: createdAt (DESC)
  Purpose: "Get all memories from issue #115"
  Projection: KEYS_ONLY + summary + memoryType

GSI-4: ByType
  PK: memoryType  |  SK: updatedAt (DESC)
  Purpose: "Get all procedural memories" for consolidation
  Projection: ALL
```

### 6.2 S3 Bucket Structure

```
s3://bedrockgw-{env}-agent-memory/
├── raw-learnings/                 # Original markdown files
│   ├── 2026-02-15-session-learnings.md
│   ├── 2026-02-16-deployment-learnings.md
│   └── ...
├── archived-memories/             # Memories archived from DynamoDB
│   ├── 2026-02/
│   │   ├── episode-abc123.json
│   │   └── episode-def456.json
│   └── 2026-03/
│       └── ...
└── embeddings-cache/              # Cached embeddings for bulk operations
    └── batch-2026-02-18.json
```

---

## 7. Migration Strategy

### Step 1: Create DynamoDB Table

```bash
aws dynamodb create-table \
  --table-name agent-memories \
  --attribute-definitions \
    AttributeName=PK,AttributeType=S \
    AttributeName=SK,AttributeType=S \
    AttributeName=domain,AttributeType=S \
    AttributeName=relevance,AttributeType=N \
    AttributeName=agentId,AttributeType=S \
    AttributeName=updatedAt,AttributeType=S \
    AttributeName=issueNumber,AttributeType=N \
    AttributeName=createdAt,AttributeType=S \
    AttributeName=memoryType,AttributeType=S \
  --key-schema \
    AttributeName=PK,KeyType=HASH \
    AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes \
    'IndexName=ByDomain,KeySchema=[{AttributeName=domain,KeyType=HASH},{AttributeName=relevance,KeyType=RANGE}],Projection={ProjectionType=ALL}' \
    'IndexName=ByAgent,KeySchema=[{AttributeName=agentId,KeyType=HASH},{AttributeName=updatedAt,KeyType=RANGE}],Projection={ProjectionType=ALL}' \
    'IndexName=ByIssue,KeySchema=[{AttributeName=issueNumber,KeyType=HASH},{AttributeName=createdAt,KeyType=RANGE}],Projection={ProjectionType=KEYS_ONLY}' \
    'IndexName=ByType,KeySchema=[{AttributeName=memoryType,KeyType=HASH},{AttributeName=updatedAt,KeyType=RANGE}],Projection={ProjectionType=ALL}' \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

### Step 2: Run Migration Script

```typescript
// scripts/migrate-learnings.ts
import * as fs from 'fs';
import * as path from 'path';
import { MemoryService } from '../services/MemoryService';

async function migrateLearnings() {
  const memoryService = new MemoryService({ /* config */ });
  const learningsDir = path.resolve('agent_learning');
  const files = fs.readdirSync(learningsDir).filter(f => f.endsWith('.md'));

  console.log(`Found ${files.length} learning files to migrate.`);

  let totalRecords = 0;
  for (const file of files) {
    const content = fs.readFileSync(path.join(learningsDir, file), 'utf-8');
    const learnings = parseLearningsMarkdown(content);

    console.log(`  ${file}: ${learnings.length} learnings`);

    for (const learning of learnings) {
      await memoryService.storeSemantic({
        memoryType: 'semantic',
        domain: learning.domain,
        fact: learning.content,
        source: file,
        confidence: 0.7,  // Higher initial confidence for existing learnings
        verifiedCount: 1,
        tags: learning.tags,
        relevanceScore: 0.5,
        useCount: 0,
      });
      totalRecords++;
    }
  }

  console.log(`Migration complete: ${totalRecords} records created.`);
}
```

### Step 3: Verify Migration

```typescript
// Verify record counts by domain
const domains = ['devops', 'code-gen', 'security', 'testing', 'docs'];
for (const domain of domains) {
  const count = await memoryService.countByDomain(domain);
  console.log(`  ${domain}: ${count} records`);
}
```

### Step 4: Feature-Flag Rollout

Enable memory via environment variable:
```yaml
# .github/workflows/agent-execute.yml
env:
  MEMORY_ENABLED: 'true'
  MEMORY_TABLE_NAME: 'agent-memories'
```

Keep the existing `agent_learning/` file reading as fallback for the transition period.

---

## 8. Cost Estimate

### Phase 1 (Foundation) — Months 1-2

| Service | Usage | Monthly Cost |
|---------|-------|-------------|
| DynamoDB (On-Demand) | ~300 records, ~1K reads, ~100 writes | ~$0.10 |
| S3 (Archival) | ~1 MB raw learnings | ~$0.01 |
| **Total** | | **~$0.11/month** |

### Phase 2 (Intelligence) — Months 3-4

| Service | Usage | Monthly Cost |
|---------|-------|-------------|
| DynamoDB (On-Demand) | ~500 records, ~5K reads, ~300 writes | ~$0.50 |
| Titan Embeddings | ~500 texts × 100 tokens avg | ~$0.005 |
| S3 (Archival) | ~5 MB | ~$0.01 |
| **Total** | | **~$0.52/month** |

### Phase 3 (Specialisation) — Months 5-8

| Service | Usage | Monthly Cost |
|---------|-------|-------------|
| DynamoDB (On-Demand) | ~2K records, ~20K reads, ~1K writes | ~$2.00 |
| Titan Embeddings | ~2K texts × 100 tokens avg | ~$0.02 |
| S3 (Archival) | ~20 MB | ~$0.01 |
| **Total** | | **~$2.03/month** |

### Cost Comparison

| Approach | Monthly Cost (Phase 3) | Notes |
|----------|----------------------|-------|
| **Custom DynamoDB** | **~$2/month** | AWS-native, no external deps |
| Mem0 Platform | ~$10-50/month | Usage-based pricing |
| Zep Cloud | ~$20-100/month | Usage-based pricing |
| Pinecone Serverless | ~$5-25/month | Vector search only |
| Bedrock Knowledge Bases | ~$10-50/month | OpenSearch Serverless minimum |

---

## 9. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|-----------|
| **Memory retrieval adds latency** | Medium | Medium | Set timeout (5s max). Gracefully degrade to no-memory mode if retrieval fails. |
| **DynamoDB single-table design becomes complex** | Medium | Low | Start simple with 4 GSIs. Migrate to multi-table if needed. |
| **Embedding quality insufficient** | Low | Medium | Titan Embeddings v2 is well-established. Fall back to tag-based retrieval if vectors aren't helpful. |
| **Memory grows unbounded** | Medium | Low | TTL auto-expires short-term memories. Consolidation archives stale ones. DynamoDB scales automatically. |
| **Consolidation produces incorrect merges** | Medium | Medium | High similarity threshold (0.85). Manual review option for merged memories. Keep source episodes for audit trail. |
| **Agent prompt becomes too long** | Medium | Medium | Strict limit of 10 memories per retrieval. Summary-first format keeps token usage low. |
| **Migration corrupts existing learnings** | Low | High | Preserve original files in S3. Migration is additive (doesn't delete files). Feature-flag rollout enables rollback. |
| **Increased complexity in agent codebase** | Medium | Medium | MemoryService is optional (feature-flagged). All memory operations wrapped in try/catch with graceful degradation. |

### Graceful Degradation Strategy

```typescript
// All memory operations are optional and non-blocking:
try {
  const memories = await memoryService.retrieveRelevant(context);
  // Use memories...
} catch (err) {
  console.log(`⚠️ Memory retrieval failed: ${err.message}. Continuing without memories.`);
  // Agent continues normally, just without past learnings
}
```

---

## 10. Implementation Timeline

```
Month 1-2: Phase 1 (Foundation)
├── Week 1: DynamoDB table creation + MemoryService skeleton
├── Week 2: Migration script + import existing learnings
├── Week 3: Integration into CodeGenerationAgent + PlanningAgent
└── Week 4: Testing, feature-flag rollout, monitoring

Month 3-4: Phase 2 (Intelligence)
├── Week 1: Titan Embeddings integration
├── Week 2: Vector-based semantic retrieval
├── Week 3: Feedback loop (usefulness tracking)
└── Week 4: Post-session consolidation job

Month 5-8: Phase 3 (Specialisation)
├── Week 1-2: Agent profiles + domain metrics
├── Week 3-4: Role-aware retrieval
├── Week 5-6: Procedural memory extraction
└── Week 7-8: Cross-session state persistence
```

---

## Appendix: Quick Reference

### What to Implement First (Phase 1 MVP)

1. Create DynamoDB table `agent-memories`
2. Build `MemoryService.ts` with `storeEpisodic()` and `retrieveRelevant()`
3. Run migration script on existing 13 learning files
4. Modify `AgentOrchestrator.ts` to use MemoryService
5. Modify `CodeGenerationAgent.ts` to inject relevant memories
6. Feature-flag with `MEMORY_ENABLED` environment variable

### What Can Wait

- Vector embeddings (Phase 2) — tag-based retrieval is sufficient initially
- Agent profiles (Phase 3) — specialisation requires baseline data collection
- Procedural memory (Phase 3) — needs enough episodic data to extract patterns
- Cross-session state (Phase 3) — requires agent identity tracking

### Key Architectural Patterns Borrowed

| Pattern | Source | Application |
|---------|--------|-------------|
| Adaptive memory structure | FluxMem (arXiv:2602.14038) | Different retrieval weights per task type |
| Dual-granular storage | HyMem (arXiv:2602.13933) | Summary + detail for each memory |
| Six memory types | MIRIX (arXiv:2507.07957) | Our four-type taxonomy |
| Episodic → Procedural transformation | ProcMEM (arXiv:2602.01869) | Consolidation pipeline |
| Three-stage consolidation | TraceMem (arXiv:2602.09712) | Capture → Structure → Promote |
| Composite scoring | CrewAI Memory | similarity + recency + usefulness + domain |
| Role-aware routing | RCR-Router (arXiv:2508.04903) | Domain-weighted retrieval |
| Private + shared tiers | Collaborative Memory (arXiv:2505.18279) | Agent profiles + shared knowledge |

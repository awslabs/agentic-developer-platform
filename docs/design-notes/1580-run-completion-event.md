# Design Note: Structured Run-Completion Event for Agent Learnings

**Issue:** #1580
**Author:** @agent-architect
**Date:** 2026-06-17
**Status:** Implementation-ready design
**Scope:** Event schema + emission point + builder. Sink/storage is out of scope (separate issue).

---

## 1. Executive Summary

Today an agent's "learnings" are **wish-driven**: the agent writes `### Learnings` prose and `experience-save-hook.ts` scrapes it. The result is biased (agents cherry-pick), inconsistent (no structure), and often skipped entirely. Downstream memory (personal-context, gbrain, DDB) can't be trusted because its input is subjective.

This design introduces a **structured, always-on run-completion event** emitted at the end of every agent run. The event captures **objective signals** already in scope at the completion point (files changed via `git diff`, turn count, duration, skill usage, outcome derived from real status) alongside the optional agent narrative. It is the canonical input to any memory sink.

---

## 2. Problem Statement

| Aspect | Today (wish-driven) | Proposed (event-driven) |
|---|---|---|
| Trigger | Agent must write `### Learnings` | Always fires at run end |
| Content source | Agent self-report (prose) | Objective in-scope signals |
| Structure | Unstructured bullet points | Typed JSON schema |
| Coverage | ~60% of runs (agents skip it) | 100% of runs |
| Secret safety | Post-hoc regex scrub | Same scrub + field-level limits |
| Outcome reliability | Agent says "success" | Derived from `agentSucceeded` + beads + PR state |
| Downstream value | Noisy recall, low confidence | High-confidence structured recall |

---

## 3. Event Schema

```typescript
/**
 * RunCompletionEvent — emitted once per agent run at the completion block.
 *
 * Schema versioned for forward compatibility. Consumers must tolerate
 * unknown fields (open schema) and missing optional fields.
 */
interface RunCompletionEvent {
  /** Schema version for forward compatibility. */
  schema_version: '1';

  /** Unique run identifier. Matches the CloudWatch log stream naming. */
  run_id: string;

  /** Timestamp of event emission (ISO 8601). */
  timestamp: string;

  // ── Identity ──────────────────────────────────────────────────────────
  /** Agent persona that ran. */
  persona: 'developer' | 'operations' | 'architect' | 'reviewer' | 'product';

  /** Cognito subject (owner) — from trusted dispatch metadata. */
  owner_sub?: string;

  /** Tenant ID — from trusted dispatch metadata. */
  tenant_id?: string;

  // ── Task context ──────────────────────────────────────────────────────
  issue: {
    number: number;
    title: string;
    repo: string;        // "owner/repo"
    component?: string;  // detected component label
  };

  // ── Outcome (objective — derived, NOT self-reported) ──────────────────
  /** Derived from agentSucceeded + beads status. Never from agent prose. */
  outcome: 'success' | 'failure' | 'partial';

  /** Number of SDK turns (assistant messages). */
  turns: number;

  /** Wall-clock duration of the agent run in milliseconds. */
  duration_ms: number;

  // ── Artifact signals (objective) ──────────────────────────────────────
  /** Files touched during the run (from `git diff`, not prose). Capped at 50. */
  files_touched: string[];

  /** PR opened by this run (if any). */
  pr?: { number: number; state: string };

  /** Beads task tracking. */
  beads?: { task_id: string; status: string };

  /** Skills discovered/invoked during the run. */
  skills_used: string[];

  // ── Best-effort fields (may be empty) ─────────────────────────────────
  /** Structured error extraction (best-effort). */
  errors_encountered: Array<{ summary: string; resolved: boolean }>;

  /** Test run results (best-effort extraction from agent output). */
  tests?: { ran: boolean; passed?: number; failed?: number };

  /** The agent's own ### Learnings prose (optional, unstructured). */
  agent_narrative?: string;

  /**
   * Learning type classification.
   * Borrowed from gbrain gstack taxonomy for sink compatibility.
   */
  learning_type?: 'pattern' | 'pitfall' | 'operational' | 'investigation' | 'architecture' | 'tool' | 'preference';
}
```

### 3.1 Field Provenance — Objective vs Best-Effort

| Field | Source | Reliability |
|---|---|---|
| `run_id` | `LOG_STREAM` env var (set at pod start) | Objective |
| `timestamp` | `new Date().toISOString()` | Objective |
| `persona` | `AGENT_TYPE` env var | Objective |
| `owner_sub` / `tenant_id` | `ADP_OWNER_SUB` / `ADP_TENANT_ID` from dispatch | Objective |
| `issue.*` | `ISSUE_NUMBER` + GitHub API (already fetched) | Objective |
| `outcome` | `agentSucceeded` bool + beads completion status | Objective |
| `turns` | `turnCount` counter in `runAgent()` loop | Objective |
| `duration_ms` | `Date.now() - startedAt` from live comment | Objective |
| `files_touched` | `git diff --name-only` at completion | Objective |
| `pr` | GitHub API search (PR opened by this branch) | Objective |
| `beads` | Beads `completeWork` result | Objective |
| `skills_used` | `skillsDiscovered` Set (module-scoped) | Objective |
| `errors_encountered` | Regex extraction from `agentResult` | Best-effort |
| `tests` | Regex extraction from agent output | Best-effort |
| `agent_narrative` | `extractLearnings()` from agent prose | Best-effort |
| `learning_type` | Heuristic or cheap classifier | Best-effort |

---

## 4. Emission Point

**File:** `modules/agent-factory/agent/src/agent-worker.ts`
**Location:** The `finally` block at line ~1615, immediately after the existing memory-write and before the experience-save hook.

```
finally {
  // 1. Write agent memory context (existing)
  // 2. ★ Emit RunCompletionEvent (NEW — always fires)
  // 3. Experience-save hook (existing — can be refactored to consume the event)
  // 4. CloudWatch flush
  // 5. S3 git backup
  // 6. Process exit
}
```

### 4.1 Why This Point

All needed signals are in scope:
- `agentResult`, `agentSucceeded` — from try/catch
- `turnCount` — from `runAgent()` closure (needs to be returned or captured)
- `skillsDiscovered` — module-scoped Set
- `activeLiveComment.getStages()[0]?.startedAt` — run start time
- `beadsTaskId` — from outer scope
- `AGENT_TYPE`, `ISSUE_NUMBER`, `REPO_OWNER`, `REPO_NAME` — env/const
- `detectedComponent` — from issue labels

### 4.2 Turn Count Accessibility

Currently `turnCount` is local to `runAgent()`. The builder needs it. Two options:
1. **Return it alongside the response** (change `runAgent` return type to `{ response: string; turns: number }`)
2. **Capture it in a module-scope variable** (simpler; matches `skillsDiscovered` pattern)

**Decision:** Option 2 (module-scope `let lastRunTurnCount = 0`) — minimal diff, consistent with existing patterns.

---

## 5. Outcome Derivation

The `outcome` field is **never** taken from agent self-report. It is derived from objective signals:

```typescript
function deriveOutcome(params: {
  agentSucceeded: boolean;
  beadsStatus?: string;
  prState?: string;
}): 'success' | 'failure' | 'partial' {
  // Clear failure
  if (!params.agentSucceeded) return 'failure';

  // Agent succeeded but beads reported blocked/failed
  if (params.beadsStatus === 'blocked' || params.beadsStatus === 'failed') {
    return 'partial';
  }

  // Agent succeeded — full success
  return 'success';
}
```

---

## 6. Files-Touched Collection

Objective source: `git diff --name-only HEAD` at the completion point.

```typescript
async function getFilesTouched(cwd: string): Promise<string[]> {
  const { execSync } = await import('child_process');
  try {
    // Committed changes (on this branch vs origin/main) + uncommitted
    const committed = execSync(
      'git diff --name-only origin/main..HEAD 2>/dev/null || echo ""',
      { cwd, encoding: 'utf-8' }
    ).trim();
    const uncommitted = execSync(
      'git diff --name-only HEAD 2>/dev/null || echo ""',
      { cwd, encoding: 'utf-8' }
    ).trim();

    const all = [...committed.split('\n'), ...uncommitted.split('\n')]
      .filter(f => f.length > 0);
    const unique = [...new Set(all)];

    // Cap at 50 files to bound event size
    return unique.slice(0, 50);
  } catch {
    return [];
  }
}
```

---

## 7. Size Caps and Guardrails

| Field | Cap | Rationale |
|---|---|---|
| `files_touched` | 50 entries | Prevents megabyte events on large refactors |
| `errors_encountered` | 5 entries, 200 chars each | Best-effort; don't over-capture |
| `agent_narrative` | 2000 chars | Prose is supplementary, not primary |
| `run_id` | 128 chars | Sanity bound |
| Total event | ~10 KB max | Fits in a single SQS message or DDB item |

---

## 8. Feature Flag

```typescript
/** Whether run-completion event emission is enabled. Default: false (off). */
export function isRunCompletionEventEnabled(): boolean {
  return (process.env.RUN_COMPLETION_EVENT_ENABLED ?? 'false') === 'true';
}
```

Rollout plan:
1. Merge with flag **off** by default
2. Enable per-persona in dev (observe in CloudWatch logs)
3. Once validated, enable globally
4. Connect a sink (separate issue)

---

## 9. Non-Blocking Guarantee

The event emission MUST NOT fail the agent's actual task. This matches the pattern established by `experience-save-hook.ts`:

```typescript
try {
  if (isRunCompletionEventEnabled()) {
    const event = await buildRunCompletionEvent({ ... });
    await emitRunCompletionEvent(event, log);
  }
} catch (err) {
  log('WARN', `[run-completion-event] Emission failed (non-blocking): ${(err as Error).message}`);
}
```

---

## 10. Secret Scrubbing

Reuses `SECRET_PATTERNS` from `experience-save-hook.ts` (or a shared module). Applied to:
- `agent_narrative` (prose may contain secrets)
- `errors_encountered[].summary` (error messages may log secrets)
- `files_touched` entries are file paths — generally safe but scrubbed for paranoia

Fields like `persona`, `outcome`, `turns`, `duration_ms` are inherently safe (enum/numeric).

---

## 11. Emission Sink (Phase 1: Structured Log)

For the initial implementation, the event is **logged as structured JSON** to CloudWatch (via the existing `log()` function). This allows validation without standing up a dedicated sink:

```typescript
async function emitRunCompletionEvent(
  event: RunCompletionEvent,
  log: (level: string, msg: string, ctx?: Record<string, unknown>) => void,
): Promise<void> {
  // Phase 1: structured log (observable, queryable via CloudWatch Insights)
  log('INFO', '[run-completion-event] Event emitted', {
    run_completion_event: event,
  });

  // Phase 2 (future): POST to sink endpoint
  // Phase 3 (future): SQS publish for async processing
}
```

---

## 12. Reuse Table

| Component | Location | Reuse |
|---|---|---|
| Secret patterns | `experience-save-hook.ts` `SECRET_PATTERNS` | Import or extract to shared module |
| Identity headers | `personal-context-headers.ts` | Stamp `owner_sub`/`tenant_id` on event |
| Learning extraction | `experience-save-hook.ts` `extractLearnings()` | Populates `agent_narrative` field |
| Skill tracking | `agent-worker.ts` `skillsDiscovered` Set | Populates `skills_used` field |
| Non-blocking pattern | `experience-save-hook.ts` try/catch wrapper | Same pattern for event emission |
| Learning type taxonomy | gbrain `gstack-learnings` (conceptual) | Enum values borrowed for compatibility |

---

## 13. Migration Path

### Phase 1 (this issue): Event definition + builder + structured log
- New file: `run-completion-event.ts` (types, builder, emit, feature flag)
- New test: `run-completion-event.test.ts`
- Modify: `agent-worker.ts` completion block — call builder + emit (flag-gated)

### Phase 2 (separate issue): Wire to a sink
- Options: personal-context MCP (action=save with structured payload), gbrain `put_page`, DynamoDB direct
- The event schema is sink-agnostic by design

### Phase 3 (separate issue): Deprecate wish-driven path
- Once event emission is stable and a sink is connected, the `experience-save-hook.ts` prose-extraction path can be replaced by consuming the event's `agent_narrative` field
- The hook itself can be refactored to read from the event rather than re-parsing agent output

---

## 14. Test Strategy

| Layer | What it proves |
|---|---|
| Unit: `buildRunCompletionEvent()` | Produces valid schema from synthetic inputs; caps enforced; secrets scrubbed |
| Unit: `deriveOutcome()` | Correct mapping from signals to outcome enum |
| Unit: `getFilesTouched()` | Caps at 50; handles git failures gracefully |
| Unit: `scrubEventSecrets()` | Planted secrets in narrative/errors are removed |
| Unit: non-blocking | `emitRunCompletionEvent` swallows errors; never throws |
| Integration | Full builder with real-ish inputs produces valid JSON under 10KB |

---

## 15. Structured Reflection Questionnaire (Addendum — Issue #1580 follow-up)

**Added:** 2026-06-17 (follow-up to Comment #16)

### 15.1 Motivation

The merged Phase 1 event captures objective signals well, but reduces the *transferable lesson* to a single optional `agent_narrative` (free prose ≤2000 chars). A free-prose blob is the same wish-driven shape we aimed to fix, just smaller. This addendum adds a **required, structured `reflection` questionnaire** the agent fills at completion, **cross-checked against objective signals** to detect self-report bias.

### 15.2 New Schema: `RunReflection`

```typescript
interface RunReflection {
  /** What was actually being solved (one line; the recall key). */
  problem: string;
  /** What the agent tried, in order. */
  approach: string;
  /** Failures hit during the run. Highest-value field for future agents. */
  failures: Array<{ what: string; why: string; signal: string }>;
  /** How the agent recovered from each failure. */
  recovery: Array<{ from: string; fix: string }>;
  /** Concrete, imperative advice for the next agent on a similar task. */
  advice: string[];
  /** Agent's own confidence in this advice. */
  confidence: 'high' | 'medium' | 'low';
  /** Is this task-specific, or generally reusable across similar tasks? */
  reusable: boolean;
}
```

### 15.3 New Schema: `ReflectionConsistency` (derived, NOT agent-authored)

```typescript
interface ReflectionConsistency {
  /** Agent claimed success but objective outcome disagrees. */
  outcome_mismatch: boolean;
  /** Agent reported no failures but objective signals suggest struggle. */
  underreported_failures: boolean;
  /** Net consistency verdict for recall ranking. */
  verdict: 'consistent' | 'optimistic' | 'unreliable';
}
```

### 15.4 Cross-Check Rules

| Check | Condition | Result |
|---|---|---|
| `outcome_mismatch` | `failures.length === 0 && confidence === 'high' && outcome !== 'success'` | true |
| `underreported_failures` | `failures.length === 0 && (turns > 40 \|\| tests.failed > 0)` | true |
| `verdict = unreliable` | Both mismatch + underreported | `unreliable` |
| `verdict = optimistic` | Either one | `optimistic` |
| `verdict = consistent` | Neither | `consistent` |

**Key principle:** The consistency block is computed at capture time from objective signals. The agent NEVER fills it — it cannot influence its own credibility score.

### 15.5 Size Caps (Reflection)

| Field | Cap | Rationale |
|---|---|---|
| `problem` / `approach` | 300 chars | Recall key — must be concise |
| `failures` | 10 entries, 300 chars per sub-field | Covers most runs; prevents dumps |
| `recovery` | 10 entries, 300 chars per sub-field | Matches failures |
| `advice` | 10 entries, 300 chars each | Enough for real advice; bounded |
| `confidence` | enum | Fixed vocabulary |
| `reusable` | boolean | Fixed vocabulary |

### 15.6 Secret Scrubbing (Reflection)

- `problem` and `approach` are treated as critical — if EITHER contains a secret pattern, the entire reflection is dropped (returns undefined).
- `failures`, `recovery`, and `advice` entries are individually scrubbed — tainted entries are filtered out; clean ones survive.

### 15.7 How It Fits

```
RunCompletionEvent (always-on)
├── Objective spine: persona, issue, outcome, turns, duration, files, pr, beads, skills
├── Best-effort: errors_encountered, tests, agent_narrative, learning_type
├── reflection: RunReflection (structured self-report — required at completion)
└── consistency: ReflectionConsistency (computed at capture from objective signals)
```

`agent_narrative` remains as a raw prose fallback; `reflection` is the primary structured insight. Both are optional in the TypeScript interface (to support the migration window where reflection parsing isn't yet wired), but the completion contract should require valid reflection JSON once enabled.

### 15.8 Recall Implications (note, not scope)

When a future agent retrieves a reflection for recall:
- `verdict: 'consistent'` → full weight
- `verdict: 'optimistic'` → reduced weight, surface with caveat
- `verdict: 'unreliable'` → minimal weight, flag for human review

This ranking is implemented in the recall side (separate issue), not in the event itself.

---

## 16. Open Questions (for ratification)

1. **Should `files_touched` include the full path or just the module-relative path?** Recommendation: full repo-relative path (matches `git diff` output). Consumers can derive module from prefix.

2. **Should `learning_type` be assigned automatically or left for the sink to classify?** Recommendation: leave as optional/null in Phase 1; let the sink (gbrain/personal-context) classify on ingest. Avoids adding a classifier call to the hot path.

3. **Should the event replace `experience-save-hook.ts` or coexist?** Recommendation: coexist in Phase 1 (both fire); Phase 3 deprecates the hook once event→sink is proven.

4. **Should `reflection` be TypeScript-required or optional?** Decision: optional in the interface (supports migration window), but the completion prompt contract makes it required output from agents. The builder gracefully handles its absence.

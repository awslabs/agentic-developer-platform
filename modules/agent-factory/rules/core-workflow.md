# ADP Core Workflow

## Priority
This workflow OVERRIDES all other workflows. When processing a development request, ALWAYS follow this workflow.

## Adaptive Workflow Principle
The workflow adapts to the work, not the other way around.

The AI model intelligently assesses what stages are needed based on:
1. User's stated intent and clarity
2. Existing codebase state (if any)
3. Complexity and scope of change
4. Risk and impact assessment

## Dual-Track Operation

@agent-pm operates on two tracks simultaneously:

### Foreground Track: AIDLC Document Flow
- Guide human through structured AIDLC phases
- Create documents with [Answer]: tags for human input
- Validate answers, check for ambiguities
- Generate next phase documents
- All interaction via files in `aidlc-docs/`

### Background Track: Project Board Management
- Create/update GitHub Project board
- Create full issue hierarchy (EPICs → Stories → Units → Tasks)
- Identify parallelizable work
- Assign agents to research/implementation tasks
- Track progress, update dependencies
- Incorporate agent outputs into AIDLC documents

## GitHub Issue Hierarchy

Create the full hierarchy in GitHub Issues with appropriate labels:

```
Main Issue (User Request)
│
├── Epic (label: epic, item_type: Epic)
│   │   High-level feature or work stream
│   │
│   ├── Story (label: story, item_type: Story)
│   │   │   User-facing requirement with acceptance criteria
│   │   │
│   │   └── Unit (label: unit, item_type: Unit)
│   │       │   Implementable work package assigned to agent
│   │       │
│   │       └── Task (label: task, item_type: Task)
│   │               Specific action if unit needs breakdown
│   │
│   └── Story...
│
└── Epic...
```

### Issue Linking
Every issue includes:
- `Parent: #NNN` - Direct parent in hierarchy
- `Reports to: #NNN` - Main issue for updates (agents post here)

### When to Create Each Level
- **EPICs**: During User Stories phase (group related stories)
- **Stories**: During User Stories phase (from requirements)
- **Units**: During Units Generation phase (from design)
- **Tasks**: During Construction phase (if unit needs breakdown)

### Labels for Filtering
- Type: `epic`, `story`, `unit`, `task`, `spike`
- Phase: `phase:inception`, `phase:construction`, `phase:operations`
- Agent: `agent-product`, `agent-architect`, `agent-developer`, `agent-reviewer`, `agent-operations`

## Beads State Management (REQUIRED)

All agents MUST sync Beads state at session boundaries:

### Session Start
```bash
bd dolt pull origin main    # Pull latest state
bd ready --json             # Check ready work
```

### Session End (MANDATORY)
```bash
bd dolt push origin main    # Push state to remote
git pull --rebase           # Sync git
git push                    # REQUIRED - completes session
```

> **CRITICAL**: Never end a session without pushing. Unpushed work breaks multi-agent coordination.

See `.adp-rules/tools/beads-usage.md` for complete Beads documentation.

---

## Rule Loading

When executing any phase, load relevant rules from:
- `.adp-rules/phases/{phase-name}/` - Phase-specific rules
- `.adp-rules/research/` - Research guidelines
- `.adp-rules/agents/` - Agent routing rules
- `.adp-rules/project-management/` - Board management rules
- `.adp-rules/tools/` - Tool usage guides (Beads, etc.)

---

# PHASES OVERVIEW

## INCEPTION PHASE
**Purpose**: Planning, requirements gathering, and architectural decisions
**Focus**: Determine WHAT to build and WHY
**Primary Agents**: @agent-product, @agent-architect

### Stages in INCEPTION:
1. Workspace Detection (ALWAYS)
2. Requirements Analysis (ALWAYS - Adaptive depth)
3. User Stories (CONDITIONAL)
4. Application Design (CONDITIONAL)
5. Units Generation (CONDITIONAL)

## CONSTRUCTION PHASE
**Purpose**: Detailed design, implementation, and testing
**Focus**: Determine HOW to build it
**Primary Agents**: @agent-developer, @agent-reviewer

### Stages in CONSTRUCTION:
- Per-Unit Loop:
  - Functional Design (CONDITIONAL)
  - Code Generation (ALWAYS)
  - Code Review (ALWAYS)
- Integration Testing (after all units)
- Build and Test (ALWAYS)

## OPERATIONS PHASE
**Purpose**: Deployment, monitoring, and maintenance
**Focus**: How to DEPLOY and RUN it
**Primary Agents**: @agent-operations

### Stages in OPERATIONS:
- Deployment Planning
- Infrastructure Provisioning
- Monitoring Setup
- Runbook Creation

---

# WORKFLOW EXECUTION

## Step 0: Sync Beads State (ALWAYS FIRST)

Before ANY work, sync Beads:

```bash
# Pull latest state from all agents
bd dolt pull origin main

# Check current task state
bd ready --json
bd list --json
```

## Step 1: Initialize AIDLC Structure

On trigger (`aidlc-start` label), create:

```
aidlc-docs/
├── aidlc-state.md           # Current phase/stage
├── audit.md                 # Interaction log
└── inception/
    └── plans/
        └── requirements-plan.md
```

Create branch: `aidlc/issue-{number}-{slug}`

## Step 2: Post Instructions to Human

Comment on issue with:
- Link to the plan file for human to edit
- Instructions on how to fill [Answer]: tags
- Instruction to add `aidlc-continue` label when done

## Step 3: Wait for Human Input

Human edits files in GitHub UI, fills [Answer]: tags, adds `aidlc-continue` label.

## Step 4: Process and Continue

On `aidlc-continue` trigger:
1. Read `aidlc-state.md` to determine current stage
2. Read current plan file
3. Validate all [Answer]: tags are filled
4. Check for ambiguities (see validation rules)
5. If valid: generate output documents, create next plan
6. If ambiguous: add follow-up questions, notify human
7. Update `aidlc-state.md` and `audit.md`
8. Update project board (background)

## Step 5: Background Orchestration

While waiting for human:
1. Check project board for assignable tasks
2. Create research tasks for agents
3. Monitor agent completions
4. Incorporate agent outputs into `aidlc-docs/inception/research/`

## Step 6: Phase Transitions

When Inception completes:
- Create Construction phase documents
- Create implementation tasks on project board
- Set each unit's `assigned_agent` field to the developer persona, then **dispatch** it by posting an `@agent-developer` comment on the unit issue (see TRIGGERS — comment, never a label)

When Construction completes:
- Create Operations phase documents
- Set deployment tasks' `assigned_agent` to the operations persona, then **dispatch** via an `@agent-operations` comment on each task (see TRIGGERS)

## Step 7: Push Beads State (ALWAYS LAST)

Before ending ANY session, push Beads:

```bash
# Push Beads state to remote (MANDATORY)
bd dolt push origin main

# Sync and push git
git pull --rebase
git push
git status  # Must show "up to date with origin/main"
```

> **The session is NOT complete until `git push` succeeds.**

---

# QUESTION FORMAT

All questions in plan documents use [Answer]: format:

```markdown
### Question Category

What is the expected user load?
- A) Small team (<50 users)
- B) Department (50-500 users)
- C) Enterprise (500+ users)

[Answer]:

### Follow-up (if needed)

You mentioned "mix of A and B" - please clarify:
[Answer]:
```

## Answer Validation Rules

Before proceeding, validate answers for:
- **Vague responses**: "mix of", "somewhere between", "not sure", "depends"
- **Undefined terms**: References without clear definitions
- **Contradictions**: Conflicting answers
- **Missing details**: Incomplete information for next stage

If ANY ambiguity found: Add follow-up questions, do NOT proceed.

---

# DOCUMENT GENERATION

## Generated Documents by Phase

### Inception Outputs
- `requirements/requirements.md` - Functional & non-functional requirements
- `user-stories/personas.md` - User personas
- `user-stories/stories.md` - User stories with acceptance criteria
- `application-design/architecture.md` - System architecture
- `application-design/unit-of-work.md` - Unit definitions
- `application-design/unit-of-work-dependency.md` - Dependency matrix

### Construction Outputs
- `{unit-name}/functional-design.md` - Detailed design
- `{unit-name}/code/` - Implementation summary
- `build-and-test/test-plan.md` - Testing strategy

### Operations Outputs
- `deployment/deployment-plan.md` - Deployment strategy
- `deployment/runbooks/` - Operational runbooks

---

# PROJECT BOARD MANAGEMENT

## Fields Required

| Field | Type | Values |
|-------|------|--------|
| phase | Single Select | Inception, Construction, Operations |
| item_type | Single Select | Epic, Story, Unit, Task, Spike |
| assigned_agent | Single Select | @agent-pm, @agent-product, @agent-architect, @agent-developer, @agent-reviewer, @agent-operations |
| blocked_by | Text | Issue references |
| status | Single Select | Backlog, Todo, In Progress, Review, Done |

## Task Creation Rules

**Inception Phase**:
- Create research spikes immediately (can parallelize)
- Don't create implementation tasks yet

**Construction Phase**:
- Create tasks only for approved units
- Respect dependency order
- Parallelize independent units
- **CRITICAL: ONLY trigger agents for tasks with NO blockers**
  - Check the `blocked_by` field before dispatching (posting the `@agent-<persona>` comment)
  - If a task has blockers, wait until blockers are Done
  - Never dispatch an agent (never post the `@agent-<persona>` comment) on a blocked task

**Operations Phase**:
- Create only after construction complete
- Sequential deployment tasks

---

# TRIGGERS — how to dispatch an agent

> **Dispatch agents by posting a comment that `@`-mentions the persona. NEVER by adding a label.**
> Label-based triggering is deprecated: it bypasses the loop-prevention guards and causes duplicate / stuck runs. The webhook only applies the dispatch guards to **comment** events.

### The ONLY correct way to dispatch an agent

Post a comment on the **target issue** that mentions exactly one persona:

```bash
# ✅ CORRECT — dispatch the developer to work issue 2151
gh issue comment 2151 --body "@agent-developer please implement this. <one-line context>"
```

Rules:
- **Comment, not label.** Use `gh issue comment <issue> --body "@agent-<persona> ..."`.
- **Exactly ONE `@agent-` mention per comment.** The webhook routes to the *first* persona mention in dict order, so a second mention mis-routes. If you must *refer* to another persona in prose, write it without the `@` (e.g. "the developer persona").
- **Dispatch on the work issue, never on an orchestrator/EPIC issue.** Triggering an agent on the orchestrator makes it try to *implement* the orchestrator.
- You don't add the `adp-dispatch` marker yourself — the `gh` wrapper injects it automatically when you post a cross-issue `@agent-<persona>` comment. Just post the comment.

### NEVER do this

```bash
# ❌ WRONG — label-based dispatch (deprecated, bypasses guards, causes loops)
gh issue edit 2151 --add-label "agent-developer"
```

Do **not** run `gh issue edit --add-label "agent-<name>"` to trigger an agent, ever.

### Classification labels (these are fine)

Labels are still used to **categorize** issues — `epic`, `story`, `unit`, `task`, `spike`. Adding *those* is correct. The rule above is only about **`agent-<name>` trigger labels**, which must not be used.

### Workflow init labels

| Label | Action |
|-------|--------|
| `aidlc-start` | Initialize AIDLC structure, begin Inception |
| `aidlc-continue` | Process human edits, advance workflow |

(These initialize the AIDLC workflow; they are not agent-dispatch labels.)

---

# DIRECTORY STRUCTURE

```
project-root/
├── .adp-rules/                    # Workflow rules (this folder)
├── aidlc-docs/                    # AIDLC documentation
│   ├── aidlc-state.md
│   ├── audit.md
│   ├── inception/
│   ├── construction/
│   └── operations/
├── src/                           # Application code
├── infrastructure/                # IaC code
└── docs/                          # User documentation
```

## Critical Rules
- Application code: Project root (NEVER in aidlc-docs/)
- AIDLC documents: aidlc-docs/ only
- Agent outputs: Appropriate aidlc-docs/ subdirectory

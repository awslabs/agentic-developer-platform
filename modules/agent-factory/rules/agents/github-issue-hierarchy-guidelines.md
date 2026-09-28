# Agent Guidelines: GitHub Issue Hierarchy (Epics → Stories → Tasks)

## Purpose

These guidelines instruct AI agents on how to create and manage a three-level work hierarchy in GitHub using Issues, Labels, Tasklists, and Projects. The hierarchy maps to:

```
Epic (type: epic)
  └── Story (type: story)
        └── Task (type: unit | type: task)
              └── PR → merge
```

GitHub does not have native Epic/Story/Task types. We model them using **labels**, **Tasklists** (for parent-child linking), and **Projects** (for board views and custom fields).

---

## AIDLC Phase Integration

The issue hierarchy aligns with AIDLC phases:

```
AIDLC Phase              │  Issue Hierarchy Action
─────────────────────────┼─────────────────────────────────────────
INCEPTION                │
  Requirements Analysis  │  → Analyze request, identify Epic scope
  User Stories           │  → Create Epic(s) and Stories
  Application Design     │  → Define Units (dependencies, scope)
  Units Generation       │  → Create Unit issues under Stories
─────────────────────────┼─────────────────────────────────────────
CONSTRUCTION             │
  Functional Design      │  → Refine Unit details if needed
  Code Generation        │  → Agent works on Unit → creates PR
  Code Review            │  → PR reviewed → Unit closed on merge
  Build and Test         │  → Integration testing
─────────────────────────┼─────────────────────────────────────────
OPERATIONS               │
  Deployment             │  → Create deployment Units/Tasks
  Monitoring             │  → Create ops tasks
  Runbooks               │  → Documentation tasks
```

### When to Create Each Issue Type

| AIDLC Stage | Create | Labels |
|-------------|--------|--------|
| User Stories | Epics | `type: epic`, `phase: inception` |
| User Stories | Stories | `type: story`, `phase: inception` |
| Units Generation | Units | `type: unit`, `phase: construction` |
| Deployment | Ops Tasks | `type: task`, `phase: operations` |

---

## 1. Label Setup

Before creating any issues, ensure these labels exist. Use `gh label create` if they don't.

### Required Type Labels

| Label | Color | Purpose |
|-------|-------|---------|
| `type: epic` | `#3E4B9E` | Groups related stories into a feature or initiative |
| `type: story` | `#0052CC` | User story with acceptance criteria |
| `type: unit` | `#1D76DB` | Unit of work — implementable task assigned to agent |
| `type: task` | `#1D76DB` | Generic task (non-story work like chores, spikes) |

### Phase Labels

| Label | Purpose |
|-------|---------|
| `phase: inception` | Planning/design phase |
| `phase: construction` | Implementation phase |
| `phase: operations` | Deployment/ops phase |

### Status Labels

| Label | Purpose |
|-------|---------|
| `status: backlog` | Not ready to start |
| `status: ready` | Ready to be picked up |
| `status: blocked` | Waiting on dependency |
| `status: in-progress` | Currently being worked on |
| `status: review` | In review |
| `status: done` | Completed |

### Agent Labels (DEPRECATED — do not add)

`agent-<persona>` labels (`agent-pm`, `agent-developer`, `agent-operations`,
etc.) are deprecated. They do NOT dispatch agents and MUST NOT be added to
issues — as an agent, dispatch happens only via `adp-trigger --persona
<persona> --issue <N>` (see core-workflow.md TRIGGERS; bug #3626). A bot-authored
`@agent-<persona>` comment does not reliably dispatch and breaks lineage; the
mention path is for human operators only. Treat any existing `agent-*` label as
stale metadata.

---

## 2. Dependency Tracking (CRITICAL)

Dependencies MUST be tracked in THREE places for proper coordination:

### 2.1 Issue Body (Human-Readable)

```markdown
### Dependencies
- **Blocked by**: #123 (Unit 1-1: Auth middleware must complete first)
- **Blocks**: #125, #126 (These units depend on this one)
```

### 2.2 GitHub Project Board (PM Coordination)

| Field | Type | Usage |
|-------|------|-------|
| `blocked_by` | Text | Issue numbers: `#123, #124` |
| `status` | Select | Set to `Backlog` if blocked |

### 2.3 Beads State Management (Agent Coordination)

```bash
# When creating a unit that depends on another:
bd dep add <blocked-unit-id> <blocker-unit-id> --type blocks

# Example: Unit 1-2 is blocked by Unit 1-1
bd dep add bd-abc.2 bd-abc.1 --type blocks

# Check what's ready to work on (no blockers):
bd ready --json
```

### Dependency Flow

```
1. PM creates Unit A (no blockers)        → status: ready
2. PM creates Unit B (blocked by A)       → status: backlog, blocked_by: #A
3. Agent picks up Unit A                  → status: in-progress
4. Agent completes Unit A                 → status: done
5. pm-poll detects A done                 → clears B's blocked_by
6. B moves to ready                       → status: ready
7. Agent picks up Unit B                  → continues...
```

### Agent Rules for Dependencies

1. **ALWAYS identify dependencies** before creating units
2. **Set blocked_by field** on project board for blocked items
3. **Create Beads dependencies** with `bd dep add`
4. **Add status: blocked label** if item cannot start
5. **Document in issue body** with specific blocker details
6. **Never create circular dependencies** — if detected, flag to human

---

## 3. Creating Epics

An Epic is a GitHub Issue with the `type: epic` label that contains a Tasklist of child stories.

### Epic Template

```markdown
## Epic {N}: {Epic Title}

### Description
{1-3 sentence summary of the feature or initiative}

### Goals
- {Goal 1}
- {Goal 2}

### Acceptance Criteria
- [ ] {High-level criterion 1}
- [ ] {High-level criterion 2}

### Scope
**In scope:** {what's included}
**Out of scope:** {what's excluded}

```[tasklist]
### Stories
- [ ] #{story_issue_number} {Story title}
- [ ] #{story_issue_number} {Story title}
```

### Dependencies
- **Blocked by**: (none or list blockers)
- **Blocks**: (list what this epic enables)
```

### Agent Rules for Epics

1. Create the epic issue FIRST, before creating child stories.
2. Apply labels: `type: epic`, `phase: inception`, relevant `area:` labels.
3. After creating child stories, EDIT the epic body to add story issue numbers to the Tasklist.
4. Epic titles: `Epic {N}: {Title}` (e.g., `Epic 1: MCP Agent Mail Deployment`).
5. Create Beads epic: `bd create "Epic N: Title" -t epic -p 1 --json`

---

## 4. Creating Stories

A Story is a GitHub Issue with the `type: story` label. It describes a user-facing capability.

### Story Template

```markdown
## US-{Epic}.{Story}: {Story Title}

**As a** {persona},
**I want to** {action},
**So that** {benefit}.

**Epic**: #{epic_issue_number} | **Priority**: {priority} | **Size**: {size}

### Acceptance Criteria
- [ ] {Specific, testable criterion 1}
- [ ] {Specific, testable criterion 2}
- [ ] {Specific, testable criterion 3}

### Technical Notes
{Any implementation guidance, API contracts, or design references}

```[tasklist]
### Units
- [ ] #{unit_issue_number} {Unit title}
- [ ] #{unit_issue_number} {Unit title}
```

### Dependencies
- **Blocked by**: #{issue_number} (reason)
- **Blocks**: #{issue_number} (reason)
```

### Agent Rules for Stories

1. Every story MUST follow the "As a / I want to / So that" format.
2. Every story MUST have at least 2 testable acceptance criteria.
3. Apply labels: `type: story`, `phase: inception`, `priority:` label.
4. Story IDs: `US-{Epic}.{Story}` (e.g., `US-1.2`).
5. Reference the parent epic issue number in the body.
6. After creating child units, EDIT the story body to add unit issue numbers to the Tasklist.
7. Create Beads task: `bd create "US-X.Y: Title" -p 2 --json` then link to epic.

---

## 5. Creating Units (Tasks)

A Unit is a GitHub Issue with `type: unit` label. It represents an implementable piece of work.

### Unit Template

```markdown
## Unit {Epic}-{Seq}: {Unit Title}

**Story**: #{story_issue_number} | **Priority**: {priority}
**Assigned Agent**: @agent-{type}

### What to Build
{Clear description of the implementation scope}

### Files to Create/Modify
```
path/to/file1.py    # Description of changes
path/to/file2.ts    # Description of changes
```

### Implementation Details
{Specific technical guidance — API signatures, patterns to follow, constraints}

### Acceptance Criteria
- [ ] {Implementation-level criterion 1}
- [ ] {Implementation-level criterion 2}
- [ ] Tests pass
- [ ] PR approved

### Dependencies
- **Blocked by**: #{issue_number} — {reason, what must complete first}
- **Blocks**: #{issue_number} — {what depends on this unit}

### Rules/Constraints
- {Constraint 1 — e.g., "Do NOT modify shared/ directory"}
- {Constraint 2 — e.g., "Follow existing patterns in src/auth/"}
```

### Agent Rules for Units

1. Apply labels: `type: unit`, `phase: construction`, `agent-{type}`, `status: ready` or `status: blocked`.
2. Unit IDs: `Unit {Epic}-{Seq}` (e.g., `Unit 1-3`).
3. Reference the parent story issue number in the body.
4. Include specific file paths — units should be actionable without clarification.
5. **Set dependencies explicitly**:
   - In issue body: `Blocked by: #N`
   - On project board: `blocked_by` field
   - In Beads: `bd dep add <this-unit> <blocker> --type blocks`
6. Each unit maps to exactly one PR.
7. Create Beads task: `bd create "Unit X-Y: Title" -p 3 --json` then set dependencies.

---

## 6. GitHub Projects Configuration

### Required Custom Fields

| Field Name | Type | Values |
|------------|------|--------|
| `item_type` | Single select | `Epic`, `Story`, `Unit`, `Task` |
| `phase` | Single select | `Inception`, `Construction`, `Operations` |
| `status` | Single select | `Backlog`, `Todo`, `In Progress`, `Review`, `Done` |
| `assigned_agent` | Single select | `@agent-pm`, `@agent-developer`, etc. |
| `blocked_by` | Text | Issue references: `#123, #124` |
| `workflow_run` | Text | URL to workflow run |

### Status Transitions

```
Backlog → Todo (when unblocked)
Todo → In Progress (when agent starts)
In Progress → Review (when PR created)
Review → Done (when PR merged)
```

---

## 7. Complete Workflow: Feature Request → Done

### Step 1: Inception - Analyze and Create Epics/Stories

```bash
# 1. Create labels if missing
gh label create "type: epic" --color "3E4B9E" --repo "$REPO" || true
gh label create "type: story" --color "0052CC" --repo "$REPO" || true
gh label create "type: unit" --color "1D76DB" --repo "$REPO" || true

# 2. Create Epic
EPIC_NUM=$(gh issue create --repo "$REPO" \
  --title "Epic 1: MCP Agent Mail Deployment" \
  --label "type: epic,phase: inception" \
  --body "$EPIC_BODY" | grep -oE '[0-9]+$')

# 3. Create Beads epic
bd create "Epic 1: MCP Agent Mail Deployment" -t epic -p 1 --json
```

### Step 2: Inception - Create Stories

```bash
# Create Story under Epic
STORY_NUM=$(gh issue create --repo "$REPO" \
  --title "US-1.1: Deploy Agent Mail to K8s" \
  --label "type: story,phase: inception" \
  --body "$STORY_BODY" | grep -oE '[0-9]+$')

# Update Epic tasklist
gh issue edit "$EPIC_NUM" --repo "$REPO" --body "$EPIC_BODY_WITH_STORY"

# Create Beads task and link to epic
bd create "US-1.1: Deploy to K8s" -p 2 --json
bd dep add <story-id> <epic-id> --type parent-child
```

### Step 3: Construction - Create Units with Dependencies

```bash
# Unit 1 (no dependencies)
UNIT1_NUM=$(gh issue create --repo "$REPO" \
  --title "Unit 1-1: Create K8s manifests" \
  --label "type: unit,phase: construction,agent-developer,status: ready" \
  --body "$UNIT1_BODY" | grep -oE '[0-9]+$')

# Unit 2 (blocked by Unit 1)
UNIT2_NUM=$(gh issue create --repo "$REPO" \
  --title "Unit 1-2: Configure persistent storage" \
  --label "type: unit,phase: construction,agent-developer,status: blocked" \
  --body "$UNIT2_BODY_WITH_BLOCKED_BY" | grep -oE '[0-9]+$')

# Set project board blocked_by field
gh project item-edit --id "$UNIT2_ITEM_ID" --project-id "$PROJECT_ID" \
  --field-id "$BLOCKED_BY_FIELD_ID" --text "#$UNIT1_NUM"

# Create Beads dependencies
bd create "Unit 1-1: K8s manifests" -p 3 --json  # Returns bd-abc.1
bd create "Unit 1-2: Storage" -p 3 --json        # Returns bd-abc.2
bd dep add bd-abc.2 bd-abc.1 --type blocks       # 1-2 blocked by 1-1
```

### Step 4: Agent Execution

```bash
# Agent checks ready work
bd ready --json  # Returns Unit 1-1 (Unit 1-2 is blocked)

# Agent claims and works on Unit 1-1
bd update bd-abc.1 --claim --json

# Agent completes Unit 1-1
bd close bd-abc.1 --reason "PR #123 merged"

# pm-poll detects completion, unblocks Unit 1-2
# Unit 1-2 moves from Backlog → Todo
# Agent picks up Unit 1-2...
```

---

## 8. Naming Conventions

| Level | Pattern | Example |
|-------|---------|---------|
| Epic | `Epic {N}: {Title}` | `Epic 1: MCP Agent Mail Deployment` |
| Story | `US-{Epic}.{Story}: {Title}` | `US-1.2: Configure Authentication` |
| Unit | `Unit {Epic}-{Seq}: {Title}` | `Unit 1-3: Create Ingress Rules` |
| Task | `Task: {Title}` | `Task: Update documentation` |

---

## 9. Validation Checklist

After creating issue hierarchy, verify:

- [ ] Every Epic has `type: epic` label
- [ ] Every Story has `type: story` label and references parent Epic
- [ ] Every Unit has `type: unit` label and references parent Story
- [ ] Every blocked item has `status: blocked` label
- [ ] Every blocked item has `blocked_by` field set on project board
- [ ] Beads dependencies created with `bd dep add`
- [ ] Tasklists in Epics contain all child Story numbers
- [ ] Tasklists in Stories contain all child Unit numbers
- [ ] No orphan issues (every Story belongs to Epic, every Unit to Story)
- [ ] No circular dependencies

---

## 10. Error Handling

| Situation | Agent Action |
|-----------|-------------|
| Can't determine Epic boundaries | Ask human for clarification |
| Story too large (> 5 units) | Split into multiple stories |
| Circular dependency detected | Flag to human, suggest reordering |
| Blocker not clear | Add `needs-discussion` label, ask for clarification |
| Duplicate issue suspected | Search first, reference existing if found |

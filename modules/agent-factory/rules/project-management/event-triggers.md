# Event-Driven Agent Triggers

## Overview

GitHub Projects serves as the central state machine. Agents communicate through:
1. **Project board field updates** - State changes
2. **Issue labels** - Workflow triggers
3. **Issue comments** - Progress updates

---

# TRIGGER FLOW

## 1. PM Assigns Work to Agent

### ⚠️ CRITICAL: Blocker Check BEFORE Assignment

Before adding ANY agent label, PM MUST:

```bash
# 1. Get the item from project board
ITEM=$(gh project item-list $PROJECT --owner $OWNER --format json | \
  jq '.items[] | select(.content.number == '$ISSUE_NUM')')

# 2. Check blocked_by field
BLOCKED_BY=$(echo "$ITEM" | jq -r '.blocked_by // ""')

# 3. Only proceed if NOT blocked
if [ -z "$BLOCKED_BY" ]; then
  # Safe to add agent label
  gh issue edit $ISSUE_NUM --add-label "agent-$AGENT_NAME"
else
  echo "Task #$ISSUE_NUM is blocked by: $BLOCKED_BY - SKIPPING"
fi
```

**NEVER trigger agents for blocked tasks!** The `pm-notify` workflow will trigger them when blockers complete.

---

When @agent-pm wants to assign work:

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  @agent-pm  │────►│  GitHub Project  │────►│  Target Agent   │
│             │     │                  │     │                 │
│ 1. Set      │     │ 2. Field updated │     │ 3. Workflow     │
│    assigned │     │                  │     │    triggered    │
│    _agent   │     │                  │     │                 │
│             │     │                  │     │                 │
│ 4. Add      │     │                  │     │                 │
│    trigger  │─────┼──────────────────┼────►│                 │
│    label    │     │                  │     │                 │
└─────────────┘     └──────────────────┘     └─────────────────┘
```

### PM Actions:
```bash
# 1. Update project board field
gh project item-edit --id $ITEM_ID \
  --project-id $PROJECT_ID \
  --field-id $ASSIGNED_AGENT_FIELD \
  --single-select-option-id $AGENT_OPTION_ID

# 2. Update status to Todo
gh project item-edit --id $ITEM_ID \
  --project-id $PROJECT_ID \
  --field-id $STATUS_FIELD \
  --single-select-option-id $TODO_OPTION_ID

# 3. Add trigger label to issue
gh issue edit $ISSUE_NUMBER --add-label "agent-$AGENT_NAME"
```

### Trigger Labels:
| Label | Triggers |
|-------|----------|
| `agent-product` | @agent-product workflow |
| `agent-architect` | @agent-architect workflow |
| `agent-developer` | @agent-developer workflow |
| `agent-reviewer` | @agent-reviewer workflow |
| `agent-operations` | @agent-operations workflow |

---

## 2. Agent Picks Up Work

When agent workflow is triggered:

```yaml
# Agent workflow trigger
on:
  issues:
    types: [labeled]

jobs:
  work:
    if: github.event.label.name == 'agent-$NAME'
    steps:
      # Remove trigger label immediately
      - name: Remove trigger label
        run: gh issue edit $ISSUE --remove-label "agent-$NAME"

      # Update status to In Progress
      - name: Update board status
        run: |
          gh project item-edit --id $ITEM_ID \
            --project-id $PROJECT_ID \
            --field-id $STATUS_FIELD \
            --single-select-option-id $IN_PROGRESS_ID

      # Do the work...
```

### Agent Startup Checklist:
1. Remove trigger label (prevent re-trigger)
2. Update board status → "In Progress"
3. Post comment: "Starting work on this task"
4. Read task context from issue and project
5. Load relevant AIDLC rules
6. Execute work

---

## 3. Agent Completes Work

When agent finishes:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Target Agent   │────►│  GitHub Project  │────►│  @agent-pm  │
│                 │     │                  │     │             │
│ 1. Update       │     │ 2. Status shows  │     │ 3. Workflow │
│    board status │     │    Done/Review   │     │    triggered│
│    → Done       │     │                  │     │             │
│                 │     │                  │     │             │
│ 4. Add          │─────┼──────────────────┼────►│             │
│    pm-notify    │     │                  │     │             │
│    label        │     │                  │     │             │
└─────────────────┘     └──────────────────┘     └─────────────┘
```

### Agent Completion Actions:
```bash
# 1. Update board status to Done (or Review if needs review)
gh project item-edit --id $ITEM_ID \
  --project-id $PROJECT_ID \
  --field-id $STATUS_FIELD \
  --single-select-option-id $DONE_OPTION_ID

# 2. Post completion comment
gh issue comment $ISSUE_NUMBER --body "## Task Complete

**Agent**: @agent-$NAME
**Status**: Done
**Artifacts**: [list of created files/PRs]

@agent-pm - Ready for next steps."

# 3. Add PM notification label
gh issue edit $ISSUE_NUMBER --add-label "pm-notify"
```

### Completion Statuses:
| Status | Meaning | Next Action |
|--------|---------|-------------|
| `Review` | Needs code review | PM assigns @agent-reviewer |
| `Done` | Task complete | PM checks dependencies, assigns next |
| `Blocked` | Cannot proceed | PM investigates, resolves |

---

## 4. PM Receives Notification

PM workflow triggered by `pm-notify` label:

```yaml
# PM notification workflow
on:
  issues:
    types: [labeled]

jobs:
  check-progress:
    if: github.event.label.name == 'pm-notify'
    steps:
      - name: Remove notification label
        run: gh issue edit $ISSUE --remove-label "pm-notify"

      - name: Check project state
        run: |
          # Get all items and their statuses
          # Identify completed tasks
          # Check if any tasks are unblocked
          # Determine next actions

      - name: Assign next tasks
        run: |
          # For each unblocked task:
          # - Set assigned_agent
          # - Post a comment with a single @agent-<persona> mention
```

### PM Notification Actions:
1. Remove `pm-notify` label
2. Read project board state
3. Check which tasks are now unblocked
4. For unblocked tasks:
   - Assign to appropriate agent
   - Post a comment on the task with a single `@agent-<persona>` mention
     (NEVER an `agent-*` trigger label)
5. Check phase completion:
   - If phase complete, transition to next
   - Create next phase tasks
6. Post progress update comment

---

# LABEL REFERENCE

## Agent dispatch (comments, NEVER labels)

Agents are dispatched by posting a comment containing a single
`@agent-<persona>` mention (`@agent-developer`, `@agent-operations`, etc.).
**`agent-<persona>` labels do NOT dispatch agents.** Label-based triggering is
deprecated and forbidden — it causes duplicate/inconsistent runs and bypasses
wave sequencing (bug #3626: labels applied at issue creation implemented all
waves of EPIC #3557 at once). If you see an `agent-*` label on an issue, treat
it as stale metadata, not a trigger. One `@agent-` mention per comment
(dict-order routing — see core-workflow.md).

## Workflow Init Labels
| Label | Purpose |
|-------|---------|
| `aidlc-start` | Start AIDLC workflow |
| `aidlc-continue` | Human finished editing files |

## Notification Labels (Add to signal)
| Label | Purpose |
|-------|---------|
| `pm-notify` | Notify PM of task completion |
| `needs-review` | Signal PR needs review |
| `blocked` | Signal task is blocked |
| `needs-human` | Signal human decision needed |

## Status Labels (Informational)
| Label | Purpose |
|-------|---------|
| `phase:inception` | Currently in Inception |
| `phase:construction` | Currently in Construction |
| `phase:operations` | Currently in Operations |

---

# WORKFLOW DEFINITIONS

## Agent Workflow Template

```yaml
name: Agent - [Name]

on:
  issues:
    types: [labeled]

jobs:
  work:
    if: github.event.label.name == 'agent-[name]'
    runs-on: [runner]

    steps:
      - name: Remove trigger label
        run: gh issue edit ${{ github.event.issue.number }} --remove-label "agent-[name]"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Checkout
        uses: actions/checkout@v4

      - name: Update status to In Progress
        run: |
          # Get item ID from project
          ITEM_ID=$(gh project item-list $PROJECT_NUM --owner $ORG --format json | \
            jq -r ".items[] | select(.content.number == $ISSUE_NUM) | .id")

          # Update status
          gh project item-edit --id "$ITEM_ID" \
            --project-id "$PROJECT_ID" \
            --field-id "$STATUS_FIELD_ID" \
            --single-select-option-id "$IN_PROGRESS_OPTION_ID"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Run agent
        run: |
          # Load rules from .adp-rules/
          # Execute agent logic
          # Create artifacts
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Update status to Done
        run: |
          gh project item-edit --id "$ITEM_ID" \
            --project-id "$PROJECT_ID" \
            --field-id "$STATUS_FIELD_ID" \
            --single-select-option-id "$DONE_OPTION_ID"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Notify PM
        run: |
          gh issue comment ${{ github.event.issue.number }} --body "## Task Complete

          Agent: @agent-[name]
          Status: Done

          @agent-pm ready for next steps."

          gh issue edit ${{ github.event.issue.number }} --add-label "pm-notify"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## PM Notification Handler

```yaml
name: PM - Check Progress

on:
  issues:
    types: [labeled]

jobs:
  check:
    if: github.event.label.name == 'pm-notify'
    runs-on: [runner]

    steps:
      - name: Remove notification label
        run: gh issue edit ${{ github.event.issue.number }} --remove-label "pm-notify"

      - name: Checkout
        uses: actions/checkout@v4

      - name: Check and assign next tasks
        run: |
          # Load project state
          # Find completed task
          # Check what's now unblocked
          # Assign next tasks
          # Trigger agents
```

---

# SEQUENCE DIAGRAM

```
Human          PM             Project        Agent          Agent
  │             │               │              │              │
  │ Create Issue│               │              │              │
  │────────────►│               │              │              │
  │             │ Create Board  │              │              │
  │             │──────────────►│              │              │
  │             │               │              │              │
  │             │ Assign Task   │              │              │
  │             │──────────────►│              │              │
  │             │               │              │              │
  │             │ Add Label     │              │              │
  │             │───────────────┼─────────────►│              │
  │             │               │              │              │
  │             │               │ Update Status│              │
  │             │               │◄─────────────│              │
  │             │               │              │              │
  │             │               │    [Work]    │              │
  │             │               │◄─────────────│              │
  │             │               │              │              │
  │             │               │ Status: Done │              │
  │             │               │◄─────────────│              │
  │             │               │              │              │
  │             │ pm-notify     │              │              │
  │             │◄──────────────┼──────────────│              │
  │             │               │              │              │
  │             │ Check State   │              │              │
  │             │──────────────►│              │              │
  │             │               │              │              │
  │             │ Assign Next   │              │              │
  │             │──────────────►│              │              │
  │             │               │              │              │
  │             │ Add Label     │              │              │
  │             │───────────────┼──────────────┼─────────────►│
  │             │               │              │              │
  │             │               │              │    [Work]    │
  │             │               │              │              │
```

---

# ERROR HANDLING

## Agent Failure
If agent workflow fails:
1. Status remains "In Progress"
2. Workflow posts error comment
3. `agent-failed` label added
4. PM notified via `pm-notify`
5. PM can retry or reassign

## Stuck Tasks
PM periodically checks for:
- Tasks "In Progress" > 1 hour
- Tasks with no recent activity
- Blocked tasks with resolved blockers

## Manual Override
Human can always:
- Remove/add labels manually
- Update project board fields
- Comment to redirect agents
- Close/reopen issues

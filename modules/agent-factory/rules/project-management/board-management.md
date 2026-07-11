# Project Board Management

## Purpose
GitHub Projects serves as the state management system for AIDLC workflows. @agent-pm maintains the board to coordinate work across agents.

---

# PROJECT SETUP

## When to Create Project
- On `aidlc-start` trigger for any issue assessed as `deep` or `full_project`
- Not needed for `quick` or `standard` depth issues

## Project Creation Steps

```bash
# Create project
gh project create --owner [org] --title "[Issue Title] - #[number]"

# Get project ID
PROJECT_ID=$(gh project list --owner [org] --format json | jq -r '.projects[] | select(.title | contains("#[number]")) | .id')
```

## CRITICAL: Disable Default Automations

GitHub Projects V2 has default automations that **conflict with agent workflows**. After creating a project, you MUST disable these automations:

1. Go to: `https://github.com/orgs/[org]/projects/[number]/settings/workflows`
2. **Disable** the following:
   - ❌ "Pull request linked to issue" - This overwrites agent status updates!
   - ❌ "Item added to project" (optional) - Agents handle initial status

**Why?** When an agent creates a PR and then sets status to "Done", the "Pull request linked" automation immediately fires and resets status to "In Progress", overwriting the agent's update.

Keep these automations **enabled** (useful):
- ✅ "Item closed" - Sets Done when issue is closed
- ✅ "Pull request merged" - Sets Done when PR merges
- ✅ "Auto-add sub-issues" - Adds child issues automatically

---

## Required Fields

### Phase (Single Select)
```bash
gh project field-create [number] --owner [org] --name "phase" --data-type "SINGLE_SELECT" --single-select-options "Inception,Construction,Operations"
```

Options:
- `Inception` - Planning, requirements, design
- `Construction` - Building, coding, testing
- `Operations` - Deploying, monitoring

### Item Type (Single Select)
```bash
gh project field-create [number] --owner [org] --name "item_type" --data-type "SINGLE_SELECT" --single-select-options "Epic,Story,Unit,Task,Spike"
```

Options:
- `Epic` - Large feature/initiative
- `Story` - User-facing requirement
- `Unit` - Implementable work package
- `Task` - Specific action item
- `Spike` - Research/investigation

### Assigned Agent (Single Select)
```bash
gh project field-create [number] --owner [org] --name "assigned_agent" --data-type "SINGLE_SELECT" --single-select-options "@agent-pm,@agent-product,@agent-architect,@agent-developer,@agent-reviewer,@agent-operations"
```

### Blocked By (Text)
```bash
gh project field-create [number] --owner [org] --name "blocked_by" --data-type "TEXT"
```

Format: `#123, #124` (issue references)

### Workflow Run (Text)
```bash
gh project field-create [number] --owner [org] --name "workflow_run" --data-type "TEXT"
```

Stores the GitHub Actions workflow run URL for the current agent execution.
Automatically populated by agents when they start working on an issue.

### Status (Single Select)
Default GitHub status field, ensure options:
- `Backlog` - Not yet ready
- `Todo` - Ready to start
- `In Progress` - Being worked on
- `Review` - Awaiting review
- `Done` - Completed

---

# GITHUB LABELS

## Required Labels
Ensure these labels exist for filtering issues:

```bash
# Item type labels
gh label create "epic" --color "7057ff" --description "High-level feature or work stream" --force
gh label create "story" --color "0e8a16" --description "User-facing requirement" --force
gh label create "unit" --color "1d76db" --description "Implementable work package" --force
gh label create "task" --color "0075ca" --description "Specific action item" --force
gh label create "spike" --color "d93f0b" --description "Research or investigation" --force

# NOTE: agent-<persona> labels are DEPRECATED — do not create or apply them.
# They do not dispatch agents; dispatch is a comment with a single
# @agent-<persona> mention (see core-workflow.md "NEVER do this", bug #3626).

# Phase labels (informational)
gh label create "phase:inception" --color "bfd4f2" --description "Inception phase" --force
gh label create "phase:construction" --color "c2e0c6" --description "Construction phase" --force
gh label create "phase:operations" --color "fef2c0" --description "Operations phase" --force
```

---

# ISSUE HIERARCHY

## Full Hierarchy Structure

All issues follow a parent-child hierarchy:

```
Main Issue #100: Deploy DeepWiki on EKS (the original request)
│
├── Epic #101: Infrastructure Setup
│   │   Labels: epic, phase:inception
│   │   item_type: Epic
│   │
│   ├── Story #102: VPC and Networking
│   │   │   Labels: story, phase:inception
│   │   │   item_type: Story
│   │   │   Parent: #101
│   │   │
│   │   ├── Unit #103: Terraform VPC module
│   │   │   │   Labels: unit, phase:construction
│   │   │   │   item_type: Unit
│   │   │   │   Parent: #102, Reports to: #100
│   │   │   │
│   │   │   └── Task #104: Write VPC code
│   │   │           Labels: task
│   │   │           item_type: Task
│   │   │           Parent: #103, Reports to: #100
│   │   │
│   │   └── Unit #105: Security groups module
│   │
│   └── Story #106: EKS Cluster Setup
│
├── Epic #108: Application Deployment
│
└── Epic #111: Operations & Monitoring
```

## Issue Body Format

Every issue must include parent reference:

```markdown
Parent: #[PARENT_ISSUE_NUMBER]
Reports to: #[MAIN_ISSUE_NUMBER]

## Description
[What this issue covers]

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Criterion 2

## Dependencies
Blocked by: #[other_issue], #[another_issue]
```

## Filtering Issues

### By Type
```bash
gh issue list --label epic
gh issue list --label story
gh issue list --label unit
gh issue list --label task
```

### By Agent
Use the `assigned_agent` project field (agent-* labels are deprecated):
```bash
gh project item-list [number] --owner [org] --format json | jq '.items[] | select(.assigned_agent=="@agent-developer")'
```

### By Phase
```bash
gh issue list --label phase:construction
```

### Combined
```bash
gh issue list --label unit --label phase:construction
```

---

# ITERATIVE TASK CREATION

## Principle
**Create tasks just-in-time, not all at once.**

Development is iterative. Create only the tasks needed for the current phase, plus research tasks that can run in parallel.

## Task Creation Rules

### On Project Start
Create ONLY:
- Parent Epic (if applicable)
- Initial research spikes
- First phase plan tasks (Requirements)

```markdown
Example Initial Board:
| Issue | Type | Phase | Agent | Status |
|-------|------|-------|-------|--------|
| Deploy X on EKS | Epic | Inception | @agent-pm | In Progress |
| Research: X architecture | Spike | Inception | @agent-architect | Todo |
| Research: EKS patterns | Spike | Inception | @agent-architect | Todo |
| Requirements Analysis | Task | Inception | @agent-product | Todo |
```

### After Requirements Approved
Add:
- User Story tasks
- More specific research spikes

### After Stories Approved
Add:
- Application Design task
- Technical research spikes

### After Design Approved
Add:
- Units (but not all unit subtasks yet)
- Only Unit 1 detailed tasks initially

### After Unit N Completes
Add:
- Unit N+1 detailed tasks
- Update dependencies

---

# BOARD STATE MANAGEMENT

## Status Transitions

```
Backlog → Todo → In Progress → Review → Done
                      ↓
                   Blocked
```

### When to Move Status

| Transition | Trigger |
|------------|---------|
| Backlog → Todo | Dependencies cleared, ready to work |
| Todo → In Progress | Agent starts working |
| In Progress → Review | Work complete, needs review |
| Review → Done | Review approved |
| Any → Blocked | Dependency not met |
| Blocked → Todo | Blocker resolved |

## Dependency Management

### Setting Dependencies
```bash
gh project item-edit --id [item_id] --project-id [project_id] --field-id [blocked_by_field_id] --text "#123, #124"
```

### Checking Dependencies
Before moving task to Todo:
1. Parse `blocked_by` field
2. Check each referenced issue status
3. Only move to Todo if ALL blockers are Done

### Clearing Dependencies
When a task completes:
1. Find all tasks where `blocked_by` contains this issue
2. Remove this issue from their `blocked_by`
3. If `blocked_by` now empty, move to Todo

---

# AGENT ASSIGNMENT

## Assignment Rules

### Automatic Assignment
Based on item_type and phase:

```python
def assign_agent(item_type, phase):
    if item_type == "Spike":
        if "architecture" in title or "technical" in title:
            return "@agent-architect"
        elif "user" in title or "business" in title:
            return "@agent-product"
        else:
            return "@agent-architect"

    if phase == "Inception":
        if item_type in ["Epic", "Story"]:
            return "@agent-product"
        elif item_type == "Unit":
            return "@agent-architect"

    if phase == "Construction":
        if item_type == "Unit":
            return "@agent-developer"
        elif item_type == "Task" and "review" in title:
            return "@agent-reviewer"
        else:
            return "@agent-developer"

    if phase == "Operations":
        return "@agent-operations"
```

### Manual Override
Human can reassign by:
1. Editing the `assigned_agent` field in project
2. Adding specific agent label to issue

## Triggering Agents

When task is Todo and has assigned agent:
1. Add label `agent-[name]` to the issue
2. This triggers the agent's workflow
3. Agent picks up work, sets status to In Progress

---

# PROGRESS TRACKING

## Board Views

### By Phase View
Group by `phase` field:
- Shows work organized by AIDLC phase
- Easy to see phase completion status

### By Agent View
Group by `assigned_agent`:
- Shows each agent's workload
- Helps balance work distribution

### Kanban View
Group by `status`:
- Traditional task board view
- Shows flow through pipeline

## Progress Metrics

Track and report:
- Tasks per phase (total/complete)
- Tasks per agent (total/in-progress/complete)
- Blocked tasks count
- Average time in each status

## Progress Updates

@agent-pm posts progress summaries:
- When phase completes
- When significant milestones reached
- On request from human

```markdown
## Progress Update

### Inception Phase
- Requirements: Done
- User Stories: Done (5 stories)
- Application Design: In Progress
- Units: Pending

### Agent Workload
| Agent | Active | Completed |
|-------|--------|-----------|
| @agent-product | 0 | 3 |
| @agent-architect | 2 | 1 |
| @agent-developer | 0 | 0 |

### Blockers
- None currently

### Next Steps
- Complete Application Design
- Generate Units
- Begin Construction Phase
```

---

# AUTOMATIC REVIEW TASKS

## Overview
When an agent completes work and creates a PR, @agent-pm automatically creates a review task for @agent-reviewer.

## Trigger Conditions
PM creates a review task when:
1. A task is marked "Done"
2. The task has an open PR (branch: `agent/issue-[number]`)
3. No existing review task for that PR

## Review Task Structure

```markdown
Title: [Task] Review PR #[pr_number] - [unit_name]
Labels: task, agent-reviewer, phase:construction
Parent: #[original_issue]

## Instructions
1. Review the PR thoroughly
2. Document findings in `aidlc-docs/reviews/[unit]-review.md`
3. Fix any issues found
4. Merge the PR once approved
```

## Review Documentation

@agent-reviewer creates a review document at `aidlc-docs/reviews/[unit]-review.md`:

```markdown
# Code Review: U1

**PR:** #456
**Reviewed:** 2026-03-17T10:00:00Z
**Status:** APPROVED | CHANGES_REQUESTED | NEEDS_DISCUSSION

## Summary
[Brief summary of the implementation]

## Findings

### Critical Issues
- [Blocking issues that must be fixed]

### Recommendations
- [Suggested improvements]

### Positive Notes
- [What was done well]

## Test Results
[Summary of test execution]

## Decision
[Final recommendation and next steps]
```

## Workflow

```
Agent completes work → Creates PR → Sets status Done
        ↓
PM detects completion → Finds linked PR → Creates review task
        ↓
Review task added to board → Status: Todo → Assigned: @agent-reviewer
        ↓
@agent-reviewer triggered → Reviews → Documents → Fixes → Merges
        ↓
Review task Done → PM notified → Continues workflow
```

---

# BOARD CLEANUP

## When to Archive Items
- After project completes (all Done)
- After 30 days of inactivity on Done items

## Archiving Process
```bash
# Archive completed items
gh project item-archive --id [item_id] --project-id [project_id]
```

## Project Closure
When all work complete:
1. Ensure all items Done or archived
2. Post final summary to original issue
3. Close original issue
4. Optionally archive project

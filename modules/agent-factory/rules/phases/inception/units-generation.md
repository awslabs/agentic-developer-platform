# Units Generation - Detailed Steps

## Purpose
Decompose the system into manageable units of work that can be implemented independently or in parallel.

## Prerequisites
- Application Design complete
- architecture.md generated

## Primary Agent
@agent-architect (with @agent-product coordination)

## Definition
A **Unit of Work** is a logical grouping of stories that can be:
- Developed independently (minimal dependencies)
- Assigned to a single agent or team
- Delivered incrementally
- Tested in isolation (with mocks for dependencies)

---

# PART 1: PLANNING

## Step 1: Load Context
- Read architecture.md
- Read stories.md
- Identify component boundaries
- Map stories to components

## Step 2: Create Units Plan
Generate `aidlc-docs/inception/plans/units-generation-plan.md`:

```markdown
# Units of Work Planning

## Context
Based on the architecture and stories, here is the proposed decomposition into units.

## Proposed Units

Based on architecture analysis, these units are proposed:

### Unit 1: [Name based on component/feature]
**Scope**: [What this unit covers]
**Stories Included**: [Story IDs]
**Components**: [Architecture components involved]

Is this unit scoped correctly?
- A) Yes, proceed as defined
- B) Too large, split it
- C) Too small, combine with another
- D) Adjust scope (describe below)

[Answer]:

### Unit 2: [Name]
**Scope**: [Description]
**Stories Included**: [Story IDs]
**Components**: [Components]

Is this unit scoped correctly?
[Answer]:

[Repeat for each proposed unit]

---

## Dependency Questions

### Unit Dependencies
Review the proposed dependency order:

1. Unit [X] must complete before Unit [Y] because: [reason]
   - Correct? [Yes/No/Modify]:

2. Unit [A] and Unit [B] can run in parallel
   - Correct? [Yes/No/Modify]:

[Answer for each]:

### Critical Path
The critical path (longest dependency chain) is:
Unit [X] → Unit [Y] → Unit [Z]

Is this acceptable, or should we restructure to parallelize more?
[Answer]:

---

## Implementation Order Questions

### Priority
Which units should be implemented first?
- A) Foundation units (infrastructure, core services)
- B) User-facing units (highest value features)
- C) Risk-first (complex/uncertain units)
- D) Custom order (specify below)

[Answer]:

### Parallelism
How many units can your team work on in parallel?
- A) 1 (sequential)
- B) 2-3 (small team)
- C) 4-5 (medium team)
- D) 6+ (large team with multiple agents)

[Answer]:
```

---

# PART 2: VALIDATION

## Step 3: Wait for Human Input
Human reviews unit proposals, fills [Answer]: tags.

## Step 4: Validate Answers
Check for:
- Circular dependencies
- Units too large (>5 stories)
- Units too small (<2 stories)
- Missing story assignments
- Unclear boundaries

---

# PART 3: GENERATION

## Step 5: Generate Unit of Work Document
Create `aidlc-docs/inception/application-design/unit-of-work.md`:

```markdown
# Units of Work

## Overview
The system is decomposed into [N] units of work for incremental delivery.

## Unit Summary

| Unit | Stories | Dependencies | Agent | Phase |
|------|---------|--------------|-------|-------|
| [Unit 1] | 1.1, 1.2, 1.3 | None | @agent-developer | Construction |
| [Unit 2] | 2.1, 2.2 | Unit 1 | @agent-developer | Construction |
| [Unit 3] | 3.1, 3.2, 3.3 | Unit 1 | @agent-developer | Construction |

---

## Unit 1: [Name]

### Scope
[Detailed description]

### Stories
- Story 1.1: [Title] - [Brief description]
- Story 1.2: [Title] - [Brief description]

### Components
- [Component A] - [What's needed from it]
- [Component B] - [What's needed from it]

### Interfaces
- **Inputs**: [What this unit receives]
- **Outputs**: [What this unit produces]
- **APIs**: [Endpoints/interfaces exposed]

### Acceptance Criteria
- [ ] All stories pass acceptance tests
- [ ] Integration points documented
- [ ] Code reviewed and approved
- [ ] Documentation updated

### Estimated Effort
[T-shirt size or points if requested]

---

## Unit 2: [Name]
[Repeat structure]

---

## Implementation Notes

### Parallel Tracks
Units that can run in parallel:
- Track A: Unit 1, Unit 4
- Track B: Unit 2, Unit 3 (after Unit 1)

### Integration Points
- Unit 1 ↔ Unit 2: [Interface description]
- Unit 2 ↔ Unit 3: [Interface description]
```

## Step 6: Generate Dependency Matrix
Create `aidlc-docs/inception/application-design/unit-of-work-dependency.md`:

```markdown
# Unit Dependency Matrix

## Dependency Graph

```
[Unit 1: Infrastructure]
       |
       ├──────────────┐
       ▼              ▼
[Unit 2: Core]   [Unit 3: API]
       |              |
       └──────┬───────┘
              ▼
       [Unit 4: UI]
              |
              ▼
    [Unit 5: Integration]
```

## Dependency Table

| Unit | Depends On | Blocks | Can Parallel With |
|------|------------|--------|-------------------|
| Unit 1 | - | Unit 2, Unit 3 | - |
| Unit 2 | Unit 1 | Unit 4 | Unit 3 |
| Unit 3 | Unit 1 | Unit 4 | Unit 2 |
| Unit 4 | Unit 2, Unit 3 | Unit 5 | - |
| Unit 5 | Unit 4 | - | - |

## Critical Path
Unit 1 → Unit 2 → Unit 4 → Unit 5

**Estimated Duration**: [Sum of critical path estimates]

## Parallelization Opportunities
- Phase 1: Unit 1 (foundation)
- Phase 2: Unit 2 + Unit 3 (parallel)
- Phase 3: Unit 4 (integration)
- Phase 4: Unit 5 (final)
```

## Step 7: Generate Story Map
Create `aidlc-docs/inception/application-design/unit-of-work-story-map.md`:

```markdown
# Story to Unit Mapping

## By Story

| Story | Unit | Status |
|-------|------|--------|
| Story 1.1 | Unit 1 | Planned |
| Story 1.2 | Unit 1 | Planned |
| Story 2.1 | Unit 2 | Planned |
| Story 2.2 | Unit 3 | Planned |

## By Unit

### Unit 1: [Name]
- [ ] Story 1.1: [Title]
- [ ] Story 1.2: [Title]
- [ ] Story 1.3: [Title]

### Unit 2: [Name]
- [ ] Story 2.1: [Title]
- [ ] Story 2.2: [Title]

## Coverage Check
- Total Stories: [N]
- Assigned to Units: [N]
- Unassigned: [List any]
```

## Step 8: Update Project Board (CRITICAL)

For each unit, create items on the project board:

```markdown
For Unit 1:
- Create issue: "[Unit] Infrastructure Setup"
- Set phase: Construction
- Set item_type: Unit
- Set assigned_agent: @agent-developer
- Set blocked_by: (none)
- Set status: Todo

For Unit 2:
- Create issue: "[Unit] Core Services"
- Set phase: Construction
- Set item_type: Unit
- Set assigned_agent: @agent-developer
- Set blocked_by: #[Unit 1 issue number]
- Set status: Backlog

[Repeat for each unit]
```

## Step 9: Update State
Update aidlc-state.md:
```markdown
## Current Status
- Phase: Inception → Construction
- Stage: Functional Design (for Unit 1)
- Completed: Requirements, Stories, Design, Units
- Next: Begin Construction for Unit 1
```

## Step 10: Transition to Construction
- Create `construction/plans/unit-1-plan.md`
- Notify human of phase transition
- Trigger Unit 1 work if no blockers

---

# Background Tasks During This Phase

| Task | Agent | Purpose |
|------|-------|---------|
| Dependency analysis | @agent-architect | Validate dependencies |
| Effort estimation | @agent-developer | Size units |
| Risk assessment | @agent-architect | Identify complex units |

# User Stories - Detailed Steps

## Purpose
Convert requirements into user-centered stories with acceptance criteria and personas.

## Prerequisites
- Requirements Analysis complete
- requirements.md generated

## Primary Agent
@agent-product (with @agent-architect support)

## Research Requirements (MANDATORY)

### External Research
- User experience patterns for similar applications
- Industry-standard user journeys
- Accessibility requirements

### Internal Research
- Existing user personas in organization
- Similar user flows in codebase
- Feedback from previous implementations

---

# PART 1: PLANNING

## Step 1: Load Context
- Read `aidlc-docs/inception/requirements/requirements.md`
- Read any agent research outputs
- Identify user types from requirements

## Step 2: Create Stories Plan
Generate `aidlc-docs/inception/plans/user-stories-plan.md`:

```markdown
# User Stories Planning

## Context
Based on requirements analysis, we identified the following user types and features.

## Research Findings
[Incorporate agent research on user patterns]

---

## Persona Questions

### User Type 1: [Identified from requirements]
What are the key characteristics of this user?
- Technical expertise level?
  - A) Non-technical
  - B) Somewhat technical
  - C) Technical
  - D) Expert/Developer

[Answer]:

- Primary goals when using the system?
[Answer]:

- Pain points or challenges they face?
[Answer]:

### User Type 2: [If applicable]
[Repeat questions]

---

## Story Scope Questions

### Feature: [Feature 1 from requirements]
What user actions are needed for this feature?
[Answer]:

What is the expected outcome for the user?
[Answer]:

Are there any edge cases to consider?
[Answer]:

### Feature: [Feature 2]
[Repeat for each major feature]

---

## Story Format Preferences

How detailed should acceptance criteria be?
- A) High-level (Given/When/Then)
- B) Detailed (specific test cases)
- C) Very detailed (including edge cases)

[Answer]:

Should stories be estimated?
- A) No estimation needed
- B) T-shirt sizes (S/M/L/XL)
- C) Story points

[Answer]:
```

## Step 3: Assign Background Tasks
Create on project board:
- UX research spike → @agent-product
- Technical feasibility check → @agent-architect

---

# PART 2: VALIDATION

## Step 4: Wait for Human Input
Human fills [Answer]: tags, adds `aidlc-continue` label.

## Step 5: Validate Answers
Check for:
- Unclear user descriptions
- Missing acceptance criteria context
- Conflicting user needs
- Scope ambiguity

## Step 6: Handle Ambiguity
If issues found, add follow-up questions and wait.

---

# PART 3: GENERATION

## Step 7: Generate Personas
Create `aidlc-docs/inception/user-stories/personas.md`:

```markdown
# User Personas

## Persona 1: [Name]

### Demographics
- **Role**: [Job title/role]
- **Technical Level**: [From answers]
- **Experience**: [With similar systems]

### Goals
- Primary: [Main objective]
- Secondary: [Other objectives]

### Pain Points
- [Pain point 1]
- [Pain point 2]

### Scenarios
- **Typical Day**: [How they would use the system]
- **Key Tasks**: [Main activities]

---

## Persona 2: [Name]
[Repeat structure]
```

## Step 8: Generate Stories
Create `aidlc-docs/inception/user-stories/stories.md`:

```markdown
# User Stories

## Epic 1: [Epic Name from Requirements]

### Story 1.1: [Story Title]
**As a** [Persona name]
**I want** [Action/capability]
**So that** [Benefit/outcome]

**Acceptance Criteria**:
- [ ] Given [context], when [action], then [outcome]
- [ ] Given [context], when [action], then [outcome]
- [ ] Edge case: [description]

**Priority**: [Must Have / Should Have / Nice to Have]
**Estimate**: [If requested]
**Dependencies**: [Related stories]

---

### Story 1.2: [Story Title]
[Repeat structure]

---

## Epic 2: [Epic Name]
[Repeat for each epic/feature area]

---

## Story Map

| Persona | Epic 1 | Epic 2 | Epic 3 |
|---------|--------|--------|--------|
| [Persona 1] | Story 1.1, 1.2 | Story 2.1 | - |
| [Persona 2] | Story 1.3 | Story 2.2, 2.3 | Story 3.1 |
```

## Step 9: Update State
Update `aidlc-state.md`:
```markdown
## Current Status
- Phase: Inception
- Stage: Application Design (Planning)
- Completed: Requirements Analysis, User Stories
- Next: Create application-design-plan.md
```

## Step 10: Update Project Board
- Create Epic items for each epic identified
- Set phase = Inception
- Set item_type = Epic
- Assign @agent-architect for design

## Step 11: Create Next Plan
Generate `aidlc-docs/inception/plans/application-design-plan.md`

## Step 12: Notify Human
Comment with:
- Summary of personas and stories
- Links to generated documents
- Link to next plan file

---

# Background Tasks During This Phase

| Task | Agent | Purpose |
|------|-------|---------|
| UX pattern research | @agent-product | Story refinement |
| Technical feasibility | @agent-architect | Flag complex stories |
| Similar implementations | @agent-developer | Reuse opportunities |

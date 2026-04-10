# Functional Design - Detailed Steps

## Purpose
Create detailed design for a specific unit before code generation.

## Prerequisites
- Unit of Work defined
- Stories for this unit documented
- Architecture decisions made

## Primary Agent
@agent-architect (creates design) → @agent-developer (reviews feasibility)

## Scope
This phase executes **per unit**. Each unit gets its own functional design.

---

# PART 1: PLANNING

## Step 1: Load Unit Context
- Read unit definition from `unit-of-work.md`
- Read stories assigned to this unit
- Read architecture.md for component details
- Check dependencies - are blockers complete?

## Step 2: Create Design Plan
Generate `aidlc-docs/construction/plans/{unit-name}-design-plan.md`:

```markdown
# Functional Design Plan: [Unit Name]

## Unit Context
- **Stories**: [List]
- **Components**: [List]
- **Dependencies**: [Status of blockers]

## Research Conducted
[Agent research on implementation patterns]

---

## Data Model Questions

### Entities
Based on stories, these entities are needed:

1. **[Entity Name]**
   - Purpose: [Description]
   - Key fields: [List]
   - Relationships: [To other entities]

   Is this correct?
   [Answer]:

2. **[Entity Name]**
   [Repeat]

### Data Validation
What validation rules apply to the data?
[Answer]:

---

## API/Interface Questions

### Endpoints/Methods
These interfaces are proposed:

1. `[Method/Endpoint]` - [Purpose]
   - Input: [Parameters]
   - Output: [Response]

   Correct?
   [Answer]:

2. `[Method/Endpoint]`
   [Repeat]

### Error Handling
How should errors be handled?
- A) Return error codes with messages
- B) Throw exceptions with details
- C) Use Result/Either pattern
- D) Other (describe)

[Answer]:

---

## Business Logic Questions

### Key Algorithms
Are there complex algorithms or business rules?
[Answer]:

### State Management
How should state be managed?
[Answer]:

### Edge Cases
What edge cases should be handled?
[Answer]:

---

## Integration Questions

### Dependencies on Other Units
How will this unit interact with:
- [Dependency Unit 1]: [Proposed interface]
  - Correct? [Answer]:

### External Services
Any external service calls?
[Answer]:
```

---

# PART 2: VALIDATION & GENERATION

## Step 3: Wait for Human Input
Human reviews design questions, provides answers.

## Step 4: Generate Functional Design
Create `aidlc-docs/construction/{unit-name}/functional-design.md`:

```markdown
# Functional Design: [Unit Name]

## Overview
[Summary of unit purpose and scope]

## Data Model

### Entity: [Name]
```
[EntityName]
├── id: UUID (PK)
├── field1: string (required)
├── field2: integer (optional)
├── created_at: timestamp
├── updated_at: timestamp
└── [relationship]: FK → [OtherEntity]
```

**Validation Rules**:
- field1: max 255 characters, not empty
- field2: range 0-1000

### Entity: [Name 2]
[Repeat structure]

## API Design

### Endpoint: `POST /api/[resource]`
**Purpose**: [Description]

**Request**:
```json
{
  "field1": "string",
  "field2": 123
}
```

**Response** (201 Created):
```json
{
  "id": "uuid",
  "field1": "string",
  "field2": 123,
  "created_at": "ISO8601"
}
```

**Errors**:
- 400: Validation failed
- 409: Duplicate resource
- 500: Internal error

### Endpoint: `GET /api/[resource]/{id}`
[Repeat structure]

## Business Logic

### Process: [Name]
**Trigger**: [What initiates this]
**Steps**:
1. [Step 1]
2. [Step 2]
3. [Step 3]

**Edge Cases**:
- [Case 1]: [How handled]
- [Case 2]: [How handled]

## Integration Points

### With [Other Unit/Service]
- **Interface**: [API/Event/Direct call]
- **Contract**: [What's expected]
- **Error Handling**: [What if fails]

## Test Scenarios

### Happy Path
1. [Scenario]: Given [X], When [Y], Then [Z]

### Error Cases
1. [Scenario]: Given [X], When [Y], Then [Error]

### Edge Cases
1. [Scenario]: Given [Edge X], When [Y], Then [Z]
```

## Step 5: Update State
```markdown
## Current Status
- Phase: Construction
- Unit: [Unit Name]
- Stage: Code Generation
- Completed: Functional Design
```

## Step 6: Create Code Generation Plan
Generate `aidlc-docs/construction/plans/{unit-name}-code-plan.md`

---

# Background Tasks

| Task | Agent | Purpose |
|------|-------|---------|
| Pattern research | @agent-architect | Implementation patterns |
| Code scaffolding | @agent-developer | Prepare structure |
| Test strategy | @agent-reviewer | Define test approach |

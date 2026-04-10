# Requirements Analysis - Detailed Steps

## Purpose
Gather and document functional and non-functional requirements through structured questioning and research.

## Prerequisites
- Workspace detection complete
- Initial issue/request available

## Primary Agent
@agent-product (with @agent-pm coordination)

## Research Requirements (MANDATORY)

Before creating the requirements plan, conduct research:

### External Research
- Search for similar projects/solutions
- Review industry best practices
- Check for relevant standards/compliance requirements
- Research technology options

### Internal Research
- Search codebase for similar implementations
- Review existing documentation
- Check for team conventions/patterns
- Look for related ADRs (Architecture Decision Records)

Research outputs go to: `aidlc-docs/inception/research/`

---

# PART 1: PLANNING

## Step 1: Analyze Request
- Read the original issue/request
- Identify key objectives and constraints
- Note any ambiguities or missing information
- Determine complexity level (simple/standard/complex)

## Step 2: Conduct Research
- Execute external research (web search for similar projects)
- Execute internal research (codebase analysis)
- Document findings in `aidlc-docs/inception/research/initial-research.md`
- Identify patterns and recommendations from research

## Step 3: Create Requirements Plan
Generate `aidlc-docs/inception/plans/requirements-plan.md` with:
- Context from research
- Questions organized by category
- [Answer]: tags for human input
- Multiple choice where appropriate

## Step 4: Assign Background Tasks
Create tasks on project board:
- Technical spike: Research technology options → @agent-architect
- Domain research: Understand problem space → @agent-product
- These run in parallel while waiting for human

---

# PART 2: QUESTION CATEGORIES

Generate questions for applicable categories:

## Business Context
```markdown
### Business Goals
What is the primary business objective?
- A) Increase efficiency/automation
- B) Enable new capabilities
- C) Improve user experience
- D) Reduce costs
- E) Compliance/security requirement

[Answer]:

### Success Metrics
How will success be measured?
[Answer]:
```

## User Context
```markdown
### Target Users
Who are the primary users of this system?
- A) Internal team members
- B) External customers
- C) Administrators/operators
- D) API consumers/developers
- E) Multiple user types (describe below)

[Answer]:

### User Scale
What is the expected number of users?
- A) Small (<50 users)
- B) Medium (50-500 users)
- C) Large (500-5000 users)
- D) Enterprise (5000+ users)

[Answer]:
```

## Functional Requirements
```markdown
### Core Features
What are the must-have features? (List top 3-5)
[Answer]:

### Integration Needs
What systems must this integrate with?
[Answer]:

### Data Requirements
What data will be processed/stored?
[Answer]:
```

## Non-Functional Requirements
```markdown
### Performance
What are the performance expectations?
- A) Best effort (no strict requirements)
- B) Standard (sub-second response for most operations)
- C) High performance (millisecond response times)
- D) Real-time (streaming/live data)

[Answer]:

### Availability
What uptime is required?
- A) Business hours only
- B) 99% (about 3.5 days downtime/year)
- C) 99.9% (about 8.7 hours downtime/year)
- D) 99.99% (about 52 minutes downtime/year)

[Answer]:

### Security
What security requirements apply?
- A) Basic (authentication only)
- B) Standard (auth + encryption at rest/transit)
- C) High (compliance requirements, audit logging)
- D) Regulated (SOC2, HIPAA, PCI-DSS, etc.)

[Answer]:
```

## Technical Context
```markdown
### Existing Infrastructure
What infrastructure already exists?
[Answer]:

### Technology Preferences
Are there required or preferred technologies?
[Answer]:

### Constraints
What constraints must be respected? (budget, timeline, team skills)
[Answer]:
```

---

# PART 3: VALIDATION

## Step 5: Wait for Human Input
- Human edits requirements-plan.md in GitHub UI
- Human adds `aidlc-continue` label when done

## Step 6: Validate Answers
Check for ambiguity patterns:
- "mix of", "somewhere between" → Ask for specific criteria
- "not sure", "depends" → Ask what information would help decide
- "probably", "maybe" → Ask for definitive answer
- Conflicting answers → Ask for clarification

## Step 7: Handle Ambiguity
If ambiguities found:
1. Add follow-up questions to requirements-plan.md
2. Update aidlc-state.md to indicate waiting for clarification
3. Comment on issue notifying human
4. Do NOT proceed to generation

---

# PART 4: GENERATION

## Step 8: Generate Requirements Document
Create `aidlc-docs/inception/requirements/requirements.md`:

```markdown
# Requirements Document

## Project Overview
[Generated from answers]

## Business Requirements
### Goals
[From business context answers]

### Success Criteria
[From success metrics answers]

## Functional Requirements
### FR-1: [Feature Name]
**Description**: [Description]
**Priority**: [Must Have / Should Have / Nice to Have]
**Acceptance Criteria**:
- [ ] Criterion 1
- [ ] Criterion 2

[Repeat for each feature]

## Non-Functional Requirements
### NFR-1: Performance
[From performance answers]

### NFR-2: Availability
[From availability answers]

### NFR-3: Security
[From security answers]

## Technical Constraints
[From constraints answers]

## Research Findings
[Incorporate agent research outputs]

## Open Questions
[Any remaining uncertainties]
```

## Step 9: Update State
Update `aidlc-state.md`:
```markdown
## Current Status
- Phase: Inception
- Stage: User Stories (Planning)
- Completed: Requirements Analysis
- Next: Create user-stories-plan.md
```

## Step 10: Create Next Plan
Generate `aidlc-docs/inception/plans/user-stories-plan.md`
(See user-stories.md for format)

## Step 11: Notify Human
Comment on issue:
- Summary of requirements captured
- Link to generated requirements.md
- Link to next plan file (user-stories-plan.md)
- Instructions to continue

---

# Background Tasks During This Phase

While waiting for human input, assign:

| Task | Agent | Purpose |
|------|-------|---------|
| Research similar projects | @agent-architect | Architecture patterns |
| Research technology options | @agent-architect | Tech recommendations |
| Domain analysis | @agent-product | Business context |
| Existing code analysis | @agent-developer | Reuse opportunities |

Results incorporated into requirements document.

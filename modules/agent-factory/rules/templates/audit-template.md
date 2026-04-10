# AIDLC Audit Log

## Project Information
- **Issue**: #[NUMBER]
- **Title**: [TITLE]
- **Started**: [ISO DATE]

---

## Interaction Log

### [Stage Name]
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-[name]
**Action**: [Description of action taken]
**Artifacts Created**: [List of files created/modified]
**Next Step**: [What happens next]

---

### Workflow Started
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-pm
**Action**: Initialized AIDLC workflow
**User Request**:
```
[Complete raw user request from issue]
```
**Artifacts Created**:
- aidlc-state.md
- audit.md
- inception/plans/requirements-plan.md
**Next Step**: Wait for human to fill requirements-plan.md

---

### [Template for subsequent entries]

### Requirements Analysis - Planning
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-product
**Action**: Created requirements plan with questions
**Research Conducted**:
- External: [Topics searched]
- Internal: [Codebase areas analyzed]
**Artifacts Created**:
- inception/plans/requirements-plan.md
- inception/research/initial-research.md
**Next Step**: Wait for human input

---

### Requirements Analysis - Human Input
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**User Input**:
```
[Complete raw user responses - never summarize]
```
**Validation Result**: [Valid / Needs clarification]
**Issues Found**: [None / List of ambiguities]

---

### Requirements Analysis - Generation
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-product
**Action**: Generated requirements document
**Artifacts Created**:
- inception/requirements/requirements.md
**Decisions Made**:
- [Decision 1]
- [Decision 2]
**Next Step**: User Stories planning

---

### User Stories - Planning
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-product
**Action**: Created user stories plan
**Artifacts Created**:
- inception/plans/user-stories-plan.md
**Next Step**: Wait for human input

---

### User Stories - Human Input
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**User Input**:
```
[Complete raw user responses]
```
**Validation Result**: [Valid / Needs clarification]

---

### User Stories - Generation
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-product
**Action**: Generated personas and stories
**Artifacts Created**:
- inception/user-stories/personas.md
- inception/user-stories/stories.md
**Stories Created**: [Count]
**Next Step**: Application Design

---

### Application Design - Planning
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-architect
**Action**: Created design plan
**Research Conducted**:
- [Architecture patterns researched]
**Artifacts Created**:
- inception/plans/application-design-plan.md
**Next Step**: Wait for human input

---

### Application Design - Generation
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-architect
**Action**: Generated architecture document
**Artifacts Created**:
- inception/application-design/architecture.md
**Key Decisions**:
- [Architecture decision 1]
- [Technology choice 1]
**Next Step**: Units Generation

---

### Units Generation
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-architect
**Action**: Decomposed system into units
**Artifacts Created**:
- inception/application-design/unit-of-work.md
- inception/application-design/unit-of-work-dependency.md
**Units Created**: [Count]
**Next Step**: Construction Phase

---

### Phase Transition: Inception → Construction
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-pm
**Action**: Transitioned to Construction phase
**Inception Summary**:
- Requirements: Complete
- Stories: [Count] stories
- Architecture: Defined
- Units: [Count] units
**Construction Plan**:
- Unit order: [Unit 1] → [Unit 2] → ...
- Parallel tracks: [What can parallelize]

---

### Unit [N] - Design
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-architect
**Action**: Created functional design for Unit [N]
**Artifacts Created**:
- construction/[unit-name]/functional-design.md

---

### Unit [N] - Code Generation
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-developer
**Action**: Implemented Unit [N]
**Files Created**: [Count]
**Tests Created**: [Count]
**PR**: #[NUMBER]

---

### Unit [N] - Code Review
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-reviewer
**Action**: Reviewed Unit [N] implementation
**Result**: [Approved / Changes Requested]
**Comments**: [Summary of feedback]

---

### Build and Test
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-reviewer
**Action**: Integration testing
**Test Results**:
- Total: [N]
- Passed: [N]
- Failed: [N]
**Artifacts Created**:
- construction/build-and-test/test-results.md

---

### Phase Transition: Construction → Operations
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-pm
**Action**: Transitioned to Operations phase
**Construction Summary**:
- Units completed: [Count]
- Tests passing: [Count]
- PRs merged: [Count]

---

### Deployment
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-operations
**Action**: Deployed to [environment]
**Result**: [Success / Failed]
**Artifacts Created**:
- operations/deployment/deployment-status.md

---

### Project Complete
**Timestamp**: [YYYY-MM-DDTHH:MM:SSZ]
**Agent**: @agent-pm
**Action**: Closed AIDLC workflow
**Final Summary**:
- Duration: [Start] to [End]
- Phases completed: Inception, Construction, Operations
- Documents generated: [Count]
- Code PRs: [Count]
- Deployment status: [Status]

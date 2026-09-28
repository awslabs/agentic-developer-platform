# Code Generation - Detailed Steps

## Purpose
Generate implementation code based on functional design.

## Prerequisites
- Functional Design complete for this unit
- functional-design.md generated

## Primary Agent
@agent-developer (implements) → @agent-reviewer (reviews)

## Scope
Executes **per unit**. Code is generated in the project root, NOT in aidlc-docs/.

---

# PART 1: PLANNING

## Step 1: Load Design Context
- Read `aidlc-docs/construction/{unit-name}/functional-design.md`
- Read architecture.md for technology choices
- Check dependencies - are interface contracts defined?

## Step 2: Create Code Generation Plan
Generate `aidlc-docs/construction/plans/{unit-name}-code-plan.md`:

```markdown
# Code Generation Plan: [Unit Name]

## Context
Generating implementation for [Unit Name] based on functional design.

## Technology Stack
- Language: [From architecture]
- Framework: [From architecture]
- Database: [From architecture]
- Testing: [Framework]

---

## File Generation Checklist

### Data Layer
- [ ] Entity/Model: `src/models/[entity].ts`
- [ ] Repository: `src/repositories/[entity]-repository.ts`
- [ ] Migrations: `migrations/[timestamp]-create-[entity].ts`

### Business Logic
- [ ] Service: `src/services/[unit]-service.ts`
- [ ] Validators: `src/validators/[entity]-validator.ts`
- [ ] DTOs: `src/dtos/[entity]-dto.ts`

### API Layer
- [ ] Controller: `src/controllers/[unit]-controller.ts`
- [ ] Routes: `src/routes/[unit]-routes.ts`
- [ ] Middleware: `src/middleware/[unit]-middleware.ts` (if needed)

### Tests
- [ ] Unit tests: `tests/unit/[unit]-service.test.ts`
- [ ] Integration tests: `tests/integration/[unit]-api.test.ts`

### Configuration
- [ ] Config: `src/config/[unit]-config.ts` (if needed)
- [ ] Environment: Update `.env.example`

---

## Implementation Questions

### Code Organization
Review proposed file structure above. Any changes needed?
[Answer]:

### Existing Code Integration
How should this integrate with existing code?
- A) New module, minimal integration
- B) Extend existing services
- C) Replace existing implementation
- D) Other (describe)

[Answer]:

### Testing Approach
What test coverage is expected?
- A) Basic (happy path only)
- B) Standard (happy path + main errors)
- C) Comprehensive (all paths + edge cases)
- D) Full (including performance tests)

[Answer]:

### Documentation
What code documentation is needed?
- A) Minimal (complex parts only)
- B) Standard (public APIs documented)
- C) Comprehensive (all methods documented)

[Answer]:
```

---

# PART 2: GENERATION

## Step 3: Execute Code Generation

@agent-developer generates code following the plan:

### For Each Checklist Item:
1. Mark item as in-progress [~]
2. Generate the file
3. Ensure code follows project patterns
4. Mark item complete [x]
5. Update progress

### Code Quality Requirements
- Follow existing code style (detect from codebase)
- Include error handling per design
- Add appropriate logging
- Include inline documentation for complex logic
- No hardcoded secrets or configuration

## Step 4: Generate Tests

For each component:
```markdown
### Unit Tests
- Test happy path
- Test validation errors
- Test edge cases per design
- Mock external dependencies

### Integration Tests
- Test API endpoints
- Test database operations
- Test with real (test) database
```

## Step 5: Update Code Summary
Create `aidlc-docs/construction/{unit-name}/code/implementation-summary.md`:

```markdown
# Implementation Summary: [Unit Name]

## Files Generated

### Source Files
| File | Purpose | Lines |
|------|---------|-------|
| `src/models/[entity].ts` | Entity definition | ~50 |
| `src/services/[unit]-service.ts` | Business logic | ~150 |
| `src/controllers/[unit]-controller.ts` | API handlers | ~100 |

### Test Files
| File | Tests | Coverage |
|------|-------|----------|
| `tests/unit/[unit]-service.test.ts` | 12 | 85% |
| `tests/integration/[unit]-api.test.ts` | 8 | N/A |

## Implementation Notes

### Patterns Used
- [Pattern 1]: [Where and why]
- [Pattern 2]: [Where and why]

### Deviations from Design
- [If any]: [Reason]

### Technical Debt
- [If any]: [Description and ticket reference]

## How to Run

### Start Service
```bash
npm run dev
```

### Run Tests
```bash
npm test -- --grep "[unit-name]"
```

### Verify
```bash
curl -X GET http://localhost:3000/api/[endpoint]
```
```

## Step 6: Create Pull Request
@agent-developer creates PR:
- Title: `[Unit] Implement [Unit Name]`
- Body: Reference to design docs, summary of changes
- Labels: `unit-[name]`, `needs-review`

## Step 7: Update Project Board
- Update unit issue status → Review
- Assign @agent-reviewer for code review
- Update blocked_by if this unblocks other units

## Step 8: Trigger Review
- Add `agent-reviewer` label to PR
- @agent-reviewer conducts code review

---

# PART 3: CODE REVIEW

## Step 9: @agent-reviewer Reviews

### Review Checklist
- [ ] Code matches functional design
- [ ] All acceptance criteria addressed
- [ ] Tests adequate and passing
- [ ] Error handling appropriate
- [ ] Security considerations addressed
- [ ] Performance acceptable
- [ ] Documentation sufficient

### Review Outcomes
- **Approved**: Merge PR, mark unit Done
- **Changes Requested**: @agent-developer addresses feedback
- **Needs Discussion**: Flag for human review

## Step 10: Merge and Complete
- Merge PR to main branch
- Update unit issue status → Done
- Check if this unblocks other units
- Trigger next unit if dependencies met

---

# Background Tasks

| Task | Agent | Purpose |
|------|-------|---------|
| Parallel unit coding | @agent-developer | Other independent units |
| Review preparation | @agent-reviewer | Review as code completes |
| Integration planning | @agent-architect | Cross-unit integration |

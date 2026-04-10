# Build and Test - Detailed Steps

## Purpose
Verify all units work together and the system is ready for deployment.

## Prerequisites
- All units code-generated and reviewed
- Individual unit tests passing

## Primary Agent
@agent-reviewer (testing) → @agent-operations (deployment prep)

---

# PART 1: INTEGRATION TESTING

## Step 1: Verify All Units Complete
Check project board:
- All unit issues in Done status
- All PRs merged
- No blocking issues

## Step 2: Create Integration Test Plan
Generate `aidlc-docs/construction/build-and-test/integration-test-plan.md`:

```markdown
# Integration Test Plan

## Units Under Test
| Unit | Status | PR |
|------|--------|-----|
| Unit 1 | Merged | #XX |
| Unit 2 | Merged | #XX |
| Unit 3 | Merged | #XX |

## Integration Points

### Unit 1 ↔ Unit 2
- Interface: [API/Event/Direct]
- Test Scenarios:
  - [ ] Happy path data flow
  - [ ] Error propagation
  - [ ] Timeout handling

### Unit 2 ↔ Unit 3
[Repeat for each integration point]

## End-to-End Scenarios

### Scenario 1: [User Journey Name]
**Description**: [What this tests]
**Steps**:
1. [Action] → Expected: [Result]
2. [Action] → Expected: [Result]
3. [Action] → Expected: [Result]

**Test Status**: [ ] Pass / [ ] Fail

### Scenario 2: [User Journey Name]
[Repeat]

## Performance Baseline

### Expected Metrics
| Metric | Target | Acceptable |
|--------|--------|------------|
| Response time (p50) | <100ms | <200ms |
| Response time (p99) | <500ms | <1000ms |
| Throughput | >100 req/s | >50 req/s |
| Error rate | <0.1% | <1% |

## Test Environment
- [ ] Test database provisioned
- [ ] Test secrets configured
- [ ] External service mocks ready
- [ ] CI pipeline configured
```

## Step 3: Execute Integration Tests

@agent-reviewer runs tests:

```bash
# Run all integration tests
npm run test:integration

# Run specific scenario
npm run test:e2e -- --grep "Scenario 1"

# Performance baseline
npm run test:performance
```

## Step 4: Document Results
Create `aidlc-docs/construction/build-and-test/test-results.md`:

```markdown
# Test Results

## Summary
- **Total Tests**: [N]
- **Passed**: [N]
- **Failed**: [N]
- **Skipped**: [N]

## Integration Test Results

### Unit 1 ↔ Unit 2
| Test | Status | Duration |
|------|--------|----------|
| Happy path data flow | PASS | 150ms |
| Error propagation | PASS | 80ms |
| Timeout handling | PASS | 5100ms |

### Unit 2 ↔ Unit 3
[Results]

## E2E Scenario Results

### Scenario 1: [Name]
- **Status**: PASS
- **Duration**: 2.5s
- **Notes**: [Any observations]

### Scenario 2: [Name]
[Results]

## Performance Results

| Metric | Result | Target | Status |
|--------|--------|--------|--------|
| Response time (p50) | 85ms | <100ms | PASS |
| Response time (p99) | 420ms | <500ms | PASS |
| Throughput | 125 req/s | >100 req/s | PASS |
| Error rate | 0.02% | <0.1% | PASS |

## Issues Found
- [ ] Issue 1: [Description] - Severity: [High/Medium/Low]
- [ ] Issue 2: [Description] - Severity: [High/Medium/Low]

## Recommendations
- [Recommendation 1]
- [Recommendation 2]
```

---

# PART 2: BUILD VERIFICATION

## Step 5: Create Build Instructions
Create `aidlc-docs/construction/build-and-test/build-instructions.md`:

```markdown
# Build Instructions

## Prerequisites
- Node.js >= [version]
- Docker (for local development)
- AWS CLI (for deployment)

## Environment Setup

### Clone Repository
```bash
git clone [repo-url]
cd [project-name]
```

### Install Dependencies
```bash
npm install
```

### Configure Environment
```bash
cp .env.example .env
# Edit .env with your values
```

### Database Setup
```bash
# Run migrations
npm run db:migrate

# Seed test data (optional)
npm run db:seed
```

## Build Commands

### Development Build
```bash
npm run build:dev
```

### Production Build
```bash
npm run build:prod
```

### Docker Build
```bash
docker build -t [image-name] .
```

## Verification

### Run Locally
```bash
npm run dev
# Visit http://localhost:3000
```

### Health Check
```bash
curl http://localhost:3000/health
# Expected: {"status": "healthy"}
```

### Smoke Test
```bash
npm run test:smoke
```
```

## Step 6: Verify Build
@agent-reviewer verifies:
- [ ] Clean build from fresh clone
- [ ] All dependencies resolve
- [ ] Build completes without errors
- [ ] Docker image builds successfully
- [ ] Health check passes

---

# PART 3: DEPLOYMENT READINESS

## Step 7: Create Deployment Checklist
Create `aidlc-docs/construction/build-and-test/deployment-checklist.md`:

```markdown
# Deployment Readiness Checklist

## Code Quality
- [ ] All unit tests passing
- [ ] All integration tests passing
- [ ] Code review approved
- [ ] No critical security issues
- [ ] No high-severity bugs open

## Documentation
- [ ] README updated
- [ ] API documentation current
- [ ] Architecture diagrams updated
- [ ] Runbooks created

## Configuration
- [ ] Environment variables documented
- [ ] Secrets identified and secured
- [ ] Feature flags configured
- [ ] Logging configured

## Infrastructure
- [ ] Infrastructure code ready (Terraform/CDK)
- [ ] Database migrations ready
- [ ] Network configuration defined
- [ ] IAM roles defined

## Monitoring
- [ ] Health check endpoint
- [ ] Metrics endpoints
- [ ] Log aggregation configured
- [ ] Alerts defined

## Rollback Plan
- [ ] Rollback procedure documented
- [ ] Database rollback tested
- [ ] Previous version tagged

## Sign-off
- [ ] Development team: @agent-developer
- [ ] Review team: @agent-reviewer
- [ ] Operations team: @agent-operations
- [ ] Human approval: [Name]
```

## Step 8: Update State
```markdown
## Current Status
- Phase: Construction → Operations
- Stage: Deployment Planning
- Completed: All units, Integration, Build
- Next: Operations Phase
```

## Step 9: Transition to Operations
- Create `operations/plans/deployment-plan.md`
- Update project board - create Operations tasks
- Notify human of phase transition

---

# Background Tasks

| Task | Agent | Purpose |
|------|-------|---------|
| Infrastructure prep | @agent-operations | Prepare deployment |
| Documentation review | @agent-reviewer | Verify docs complete |
| Security scan | @agent-reviewer | Final security check |

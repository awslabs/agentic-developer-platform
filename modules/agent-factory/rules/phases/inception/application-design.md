# Application Design - Detailed Steps

## Purpose
Design the system architecture, define components, and establish technical patterns.

## Prerequisites
- Requirements Analysis complete
- User Stories complete (or skipped if simple)

## Primary Agent
@agent-architect (with @agent-developer support)

## Research Requirements (MANDATORY)

### External Research
- Architecture patterns for similar systems
- Cloud service options and best practices
- Security architecture patterns
- Performance optimization techniques

### Internal Research
- Existing architectural patterns in codebase
- Infrastructure patterns already in use
- Team's technology expertise
- Previous architectural decisions (ADRs)

---

# PART 1: PLANNING

## Step 1: Load Context
- Read requirements.md
- Read stories.md and personas.md
- Read agent research outputs
- Identify architectural drivers (NFRs, constraints)

## Step 2: Create Design Plan
Generate `aidlc-docs/inception/plans/application-design-plan.md`:

```markdown
# Application Design Planning

## Context
Based on requirements and stories, here are the key architectural considerations.

## Research Findings
[Agent research on architecture patterns]

---

## Architecture Style Questions

### System Type
What type of system is this primarily?
- A) Web application (UI + backend)
- B) API/Service (backend only)
- C) Data pipeline/ETL
- D) Infrastructure/Platform
- E) Hybrid/Multiple components

[Answer]:

### Architecture Pattern
What architecture pattern fits best?
- A) Monolith (single deployable)
- B) Modular monolith (single deploy, clear modules)
- C) Microservices (multiple independent services)
- D) Serverless (functions + managed services)
- E) Hybrid (combination)

[Answer]:

### Deployment Model
Where will this run?
- A) Kubernetes (EKS/GKE/AKS)
- B) Containers (ECS/Fargate)
- C) Serverless (Lambda/Cloud Functions)
- D) VMs (EC2/Compute Engine)
- E) Managed platform (App Runner, Elastic Beanstalk)

[Answer]:

---

## Data Architecture Questions

### Primary Data Store
What is the main data storage need?
- A) Relational (PostgreSQL, MySQL)
- B) Document (MongoDB, DynamoDB)
- C) Key-Value (Redis, Memcached)
- D) Search (Elasticsearch, OpenSearch)
- E) Multiple (describe below)

[Answer]:

### Data Patterns
What data patterns apply?
- A) CRUD (simple read/write)
- B) Event-driven (event sourcing)
- C) CQRS (separate read/write)
- D) Real-time streaming
- E) Batch processing

[Answer]:

---

## Integration Questions

### External Integrations
What external systems must be integrated?
[Answer]:

### Authentication
How will users authenticate?
- A) Custom (username/password)
- B) OAuth/OIDC provider
- C) SSO (corporate identity)
- D) API keys only
- E) Multiple methods

[Answer]:

---

## Component Questions

### Key Components
Based on stories, these components are needed. Please confirm or adjust:

1. [Component 1] - [Purpose]
   - Needed? [Yes/No/Modify]:

2. [Component 2] - [Purpose]
   - Needed? [Yes/No/Modify]:

[Answer for each]:

### Component Dependencies
Are there any dependencies between components not captured above?
[Answer]:
```

## Step 3: Assign Background Tasks
Create on project board:
- Deep architecture research → @agent-architect
- Technology evaluation → @agent-architect
- Proof of concept (if complex) → @agent-developer

---

# PART 2: VALIDATION

## Step 4: Wait for Human Input
Human fills [Answer]: tags, adds `aidlc-continue` label.

## Step 5: Validate Answers
Check for:
- Conflicting architecture choices
- Missing component definitions
- Unclear integration points
- Technology mismatches

---

# PART 3: GENERATION

## Step 6: Generate Architecture Document
Create `aidlc-docs/inception/application-design/architecture.md`:

```markdown
# System Architecture

## Overview
[High-level description based on answers]

## Architecture Style
- **Pattern**: [Selected pattern]
- **Rationale**: [Why this fits]

## System Context Diagram

```
[External Users] --> [System Boundary]
                         |
                    [Component 1]
                         |
                    [Component 2]
                         |
                [External Services]
```

## Component Architecture

### Component 1: [Name]
- **Purpose**: [Description]
- **Responsibilities**:
  - [Responsibility 1]
  - [Responsibility 2]
- **Technology**: [Chosen technology]
- **Interfaces**:
  - Input: [What it receives]
  - Output: [What it produces]

### Component 2: [Name]
[Repeat structure]

## Data Architecture
- **Primary Store**: [Selected database]
- **Caching**: [If applicable]
- **Data Flow**: [Description]

## Integration Architecture
- **Authentication**: [Selected method]
- **External APIs**: [List with purposes]
- **Event/Message Flow**: [If applicable]

## Infrastructure Architecture
- **Deployment**: [Selected model]
- **Networking**: [VPC, subnets, etc.]
- **Security**: [Security groups, IAM, etc.]

## Architecture Decision Records

### ADR-1: [Decision Title]
- **Context**: [Why decision was needed]
- **Decision**: [What was decided]
- **Consequences**: [Impact of decision]

[Repeat for key decisions]

## Diagrams
[Generate ASCII or Mermaid diagrams]
```

## Step 7: Update State and Create Next Plan
- Update aidlc-state.md
- Generate units-generation-plan.md

## Step 8: Update Project Board
- Create architecture review task → @agent-reviewer
- Prepare for units generation

---

# Background Tasks During This Phase

| Task | Agent | Purpose |
|------|-------|---------|
| Architecture patterns research | @agent-architect | Best practices |
| Technology deep-dive | @agent-architect | Tech selection |
| Security review | @agent-reviewer | Security patterns |
| Infrastructure patterns | @agent-operations | Deployment options |

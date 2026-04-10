# Agent Routing Rules

## Agent Overview

| Agent | Primary Role | Skills |
|-------|-------------|--------|
| @agent-pm | Orchestration, project management | GitHub Projects, workflow coordination |
| @agent-product | Requirements, stories, business analysis | User research, acceptance criteria |
| @agent-architect | Design, architecture, technical decisions | System design, technology selection |
| @agent-developer | Implementation, coding, testing | Code generation, unit tests |
| @agent-reviewer | Quality, code review, validation | Code review, integration testing |
| @agent-operations | Deployment, infrastructure, monitoring | IaC, Kubernetes, observability |

---

# ROUTING BY PHASE

## Inception Phase

### Early Inception (Product-focused)
| Stage | Primary | Support | Parallel Work |
|-------|---------|---------|---------------|
| Requirements Analysis | @agent-product | @agent-pm | @agent-architect (tech research) |
| Epic Creation | @agent-product | @agent-pm | - |
| User Stories | @agent-product | @agent-architect | @agent-developer (feasibility) |
| Personas | @agent-product | - | - |

### Late Inception (Architecture-focused)
| Stage | Primary | Support | Parallel Work |
|-------|---------|---------|---------------|
| Application Design | @agent-architect | @agent-product | @agent-operations (infra research) |
| Units Generation | @agent-architect | @agent-product | @agent-developer (estimation) |
| Dependency Mapping | @agent-architect | @agent-pm | - |

## Construction Phase

| Stage | Primary | Support | Parallel Work |
|-------|---------|---------|---------------|
| Functional Design | @agent-architect | @agent-developer | Other units |
| Code Generation | @agent-developer | @agent-architect | Other units |
| Unit Tests | @agent-developer | @agent-reviewer | Other units |
| Code Review | @agent-reviewer | @agent-architect | Other units |
| Integration Testing | @agent-reviewer | @agent-developer | - |
| Build & Test | @agent-reviewer | @agent-operations | - |

## Operations Phase

| Stage | Primary | Support | Parallel Work |
|-------|---------|---------|---------------|
| Infrastructure Setup | @agent-operations | @agent-architect | - |
| Deployment | @agent-operations | @agent-developer | - |
| Monitoring Setup | @agent-operations | @agent-developer | - |
| Runbooks | @agent-operations | @agent-developer | @agent-reviewer (review) |

---

# ROUTING BY TASK TYPE

## Task Type → Agent Mapping

| Task Type | Agent | Notes |
|-----------|-------|-------|
| Research (business) | @agent-product | Market, user, domain research |
| Research (technical) | @agent-architect | Architecture, technology research |
| Research (security) | @agent-reviewer | Security patterns, vulnerabilities |
| Requirements gathering | @agent-product | Always product-led |
| Story writing | @agent-product | Product owns stories |
| Architecture design | @agent-architect | Technical decisions |
| API design | @agent-architect | Interface definitions |
| Data modeling | @agent-architect | With @agent-developer input |
| Code implementation | @agent-developer | Primary coding agent |
| Test writing | @agent-developer | Unit tests |
| Code review | @agent-reviewer | Quality gate |
| Integration testing | @agent-reviewer | Cross-unit testing |
| Performance testing | @agent-reviewer | With @agent-operations |
| Infrastructure code | @agent-operations | Terraform, CDK, etc. |
| Kubernetes manifests | @agent-operations | K8s deployment |
| CI/CD pipelines | @agent-operations | With @agent-developer |
| Monitoring setup | @agent-operations | Dashboards, alerts |
| Documentation | Owner of component | Whoever built it documents it |

---

# HANDOFF RULES

## When to Handoff

### Product → Architect
- Requirements approved → Design can start
- Stories approved → Design can reference them
- Business questions answered → Technical questions begin

### Architect → Developer
- Design approved → Implementation can start
- Interfaces defined → Coding can begin
- Units defined → Work can be assigned

### Developer → Reviewer
- Code complete → Review can start
- Tests passing → Review can proceed
- PR created → Review assigned

### Reviewer → Operations
- All reviews approved → Deployment can prepare
- Integration tests passing → Deployment ready
- Documentation complete → Operations can proceed

## Handoff Artifacts

| From | To | Artifact |
|------|-----|----------|
| @agent-product | @agent-architect | requirements.md, stories.md |
| @agent-architect | @agent-developer | functional-design.md, architecture.md |
| @agent-developer | @agent-reviewer | PR, test results |
| @agent-reviewer | @agent-operations | deployment-checklist.md |

---

# PARALLEL WORK RULES

## What Can Run in Parallel

### Independent Research
- Multiple agents can research different topics simultaneously
- @agent-architect researches architecture while @agent-product gathers requirements

### Independent Units
- Different units can be designed/coded in parallel
- Unit 2 and Unit 3 can proceed if both only depend on Unit 1 (completed)

### Review Pipeline
- Multiple PRs can be reviewed in parallel
- Each @agent-reviewer instance handles one PR

## What Must Be Sequential

### Phase Dependencies
- Can't start Construction until Inception designs complete
- Can't deploy until build & test passes

### Within-Unit Stages
- Design → Code → Review → Merge (sequential for same unit)

### Integration Points
- Units that depend on each other's interfaces must coordinate

---

# AGENT CAPABILITIES

## @agent-pm
**Tools**: GitHub API, Project Management
**Can**:
- Create/update GitHub issues
- Manage project boards
- Assign agents to tasks
- Track progress
- Coordinate handoffs

**Cannot**:
- Write code
- Make architecture decisions
- Approve designs (human approval needed)

## @agent-product
**Tools**: Research, Documentation
**Can**:
- Gather requirements
- Write user stories
- Define acceptance criteria
- Create personas
- Research user needs

**Cannot**:
- Make technical decisions
- Write code
- Deploy systems

## @agent-architect
**Tools**: Research, Design, Documentation
**Can**:
- Design architecture
- Select technologies
- Define interfaces
- Create technical documentation
- Review designs

**Cannot**:
- Implement full features (only prototypes)
- Deploy to production
- Make business decisions

## @agent-developer
**Tools**: Code, Test, Build
**Can**:
- Write production code
- Write unit tests
- Create PRs
- Fix bugs
- Implement designs

**Cannot**:
- Change architecture (needs architect approval)
- Deploy to production (needs operations)
- Approve own code

## @agent-reviewer
**Tools**: Code Analysis, Testing, Git
**Can**:
- Review code
- Run tests
- Identify issues
- Approve/reject PRs
- Run integration tests
- Make minor fixes to PRs (typos, formatting, missing error handling, config issues)
- Merge approved PRs to main
- Checkout and push to PR branches

**Cannot**:
- Write new features from scratch
- Make major architecture changes
- Deploy systems
- Approve PRs without review

## @agent-operations
**Tools**: Infrastructure, Deployment, Monitoring
**Can**:
- Write infrastructure code
- Deploy applications
- Configure monitoring
- Create runbooks
- Manage environments

**Cannot**:
- Write application code
- Make business decisions
- Change architecture (needs architect)

---

# BEADS SYNC (ALL AGENTS)

Every agent MUST follow Beads sync protocol. See `.adp-rules/tools/beads-usage.md` for details.

## Session Boundaries

### Start of Session (REQUIRED)
```bash
bd dolt pull origin main    # Pull latest state from all agents
bd ready --json             # Check what work is available
```

### End of Session (MANDATORY)
```bash
bd dolt push origin main    # Push your state changes
git pull --rebase
git push
```

> **CRITICAL**: Never end a session without pushing Beads state. Unpushed work breaks multi-agent coordination.

## Task Operations

| Action | Command |
|--------|---------|
| Claim task | `bd update <id> --claim` |
| Start work | `bd update <id> --status in_progress` |
| Complete task | `bd update <id> --status done` |
| Found new work | `bd create "Title" -p 1 --json` |
| Add dependency | `bd dep add <child> <parent>` |

## Commit Message Convention

Include Beads task ID in commit messages:
```
Fix authentication bug (bd-a3f8)
```

This allows `bd doctor` to detect orphaned issues.

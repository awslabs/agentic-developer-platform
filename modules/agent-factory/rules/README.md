# ADP Rules - AIDLC Workflow for Agent Development Platform

This directory contains the workflow rules that guide @agent-pm and other agents through the AI-Driven Development Life Cycle (AIDLC).

## Overview

The ADP workflow is based on [AIDLC](https://github.com/awslabs/aidlc-workflows) with enhancements for:
- Multi-agent orchestration via GitHub Projects
- Research integration at every phase
- Parallel work coordination
- File-based human interaction

## Directory Structure

```
.adp-rules/
├── core-workflow.md              # Master workflow orchestration
├── phases/
│   ├── inception/                # Planning & Design phase
│   │   ├── requirements-analysis.md
│   │   ├── user-stories.md
│   │   ├── application-design.md
│   │   └── units-generation.md
│   ├── construction/             # Implementation phase
│   │   ├── functional-design.md
│   │   ├── code-generation.md
│   │   └── build-and-test.md
│   └── operations/               # Deployment phase
│       └── deployment.md
├── research/
│   └── research-guide.md         # How agents conduct research
├── agents/
│   └── agent-routing.md          # Which agent does what
├── project-management/
│   └── board-management.md       # GitHub Projects as state machine
└── templates/
    ├── aidlc-state-template.md
    ├── audit-template.md
    └── inception/
        └── requirements-plan-template.md
```

## How It Works

### Dual-Track Operation

@agent-pm operates on two tracks:

**Foreground (Human Interaction)**
- Creates plan documents with `[Answer]:` tags
- Human edits files in GitHub UI
- Agent validates and generates next documents

**Background (Project Management)**
- Creates/updates GitHub Project board
- Assigns tasks to other agents
- Tracks progress and dependencies

### Triggers

| Label | Action |
|-------|--------|
| `aidlc-start` | Initialize AIDLC workflow |
| `aidlc-continue` | Process human file edits |
| `agent-{name}` | Trigger specific agent |

### Phases

1. **Inception** - WHAT to build, WHY
   - @agent-product: Requirements, Stories
   - @agent-architect: Design, Units

2. **Construction** - HOW to build it
   - @agent-developer: Code, Tests
   - @agent-reviewer: Review, Integration

3. **Operations** - DEPLOY and RUN
   - @agent-operations: Infrastructure, Monitoring

## Generated Documents

All AIDLC documents go in `aidlc-docs/` at project root:

```
aidlc-docs/
├── aidlc-state.md           # Current workflow state
├── audit.md                 # Interaction log
├── inception/
│   ├── plans/               # Question documents
│   ├── research/            # Agent research outputs
│   ├── requirements/
│   ├── user-stories/
│   └── application-design/
├── construction/
│   ├── plans/
│   ├── {unit-name}/
│   └── build-and-test/
└── operations/
    └── deployment/
```

## Project Board Fields

| Field | Purpose |
|-------|---------|
| phase | Inception / Construction / Operations |
| item_type | Epic / Story / Unit / Task / Spike |
| assigned_agent | Which agent owns the task |
| blocked_by | Dependencies (issue references) |
| status | Backlog / Todo / In Progress / Review / Done |

## Research

Every phase includes research before decisions:
- **External**: Web search for patterns, best practices
- **Internal**: Codebase analysis for existing patterns

See `research/research-guide.md` for details.

## Customization

To customize the workflow:
1. Edit phase rules in `phases/`
2. Modify agent routing in `agents/`
3. Update templates in `templates/`

Rules are read at runtime - no code changes needed.

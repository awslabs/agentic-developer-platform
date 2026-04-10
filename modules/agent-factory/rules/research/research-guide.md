# Research Guide

## Purpose
Every AIDLC phase should include research to inform decisions. This guide defines how agents conduct research.

## Research Principle
**Research before you ask, research before you decide.**

Agents should gather context from external and internal sources before:
- Creating question plans for humans
- Making architectural decisions
- Recommending technologies
- Estimating effort

---

# RESEARCH TYPES

## External Research (Internet)

### When to Use
- Exploring new technologies
- Finding best practices
- Checking for security vulnerabilities
- Understanding industry standards
- Learning from similar projects

### Tools Available
- `WebSearch` - Search the internet
- `WebFetch` - Fetch and analyze specific URLs

### Search Strategies

#### Technology Research
```
Search queries:
- "[technology] best practices 2024"
- "[technology] vs [alternative] comparison"
- "[technology] production deployment guide"
- "[technology] security considerations"
```

#### Architecture Research
```
Search queries:
- "[system type] architecture patterns"
- "[use case] reference architecture AWS/GCP"
- "[scale] [system type] design"
- "how to build [system type]"
```

#### Problem Research
```
Search queries:
- "[error message] solution"
- "[technology] [problem] fix"
- "[technology] troubleshooting guide"
```

### Sources to Prioritize
1. Official documentation
2. Cloud provider guides (AWS, GCP, Azure)
3. Reputable tech blogs (Martin Fowler, Netflix Tech, etc.)
4. Stack Overflow (verified answers)
5. GitHub repositories (with stars/activity)

### Sources to Verify Carefully
- Personal blogs (check date, verify claims)
- Forum posts (may be outdated)
- AI-generated content (verify accuracy)

---

## Internal Research (Codebase/Docs)

### When to Use
- Understanding existing patterns
- Finding reusable code
- Checking for similar implementations
- Reviewing team conventions
- Finding previous decisions

### Tools Available
- `Glob` - Find files by pattern
- `Grep` - Search file contents
- `Read` - Read file contents

### Search Strategies

#### Find Similar Code
```bash
# Find files with similar names
Glob: "**/user*.ts"
Glob: "**/*service*.ts"

# Find similar patterns
Grep: "class.*Service"
Grep: "async function.*create"
```

#### Find Configuration
```bash
# Find config files
Glob: "**/*.config.*"
Glob: "**/config/**"

# Find environment examples
Glob: "**/.env*"
```

#### Find Documentation
```bash
# Find docs
Glob: "**/README.md"
Glob: "**/docs/**/*.md"
Glob: "**/ADR*.md"
```

#### Find Tests (for patterns)
```bash
# Find test examples
Glob: "**/*.test.ts"
Glob: "**/*.spec.ts"
Grep: "describe\\("
```

---

# RESEARCH OUTPUT

## Format
All research outputs go to: `aidlc-docs/inception/research/`

### Research Document Template
```markdown
# Research: [Topic]

**Conducted By**: @agent-[name]
**Date**: [ISO date]
**Phase**: [Inception/Construction/Operations]

## Research Questions
1. [Question being answered]
2. [Question being answered]

## External Findings

### Source 1: [Title]
- **URL**: [link]
- **Key Points**:
  - [Point 1]
  - [Point 2]
- **Relevance**: [How this applies]

### Source 2: [Title]
[Repeat]

## Internal Findings

### Existing Pattern: [Name]
- **Location**: [file path]
- **Description**: [What it does]
- **Reusability**: [Can we reuse? How?]

### Related Code: [Name]
[Repeat]

## Recommendations
Based on research:
1. [Recommendation 1]
2. [Recommendation 2]

## Open Questions
- [Questions that couldn't be answered]

## References
- [Link 1]
- [Link 2]
```

---

# RESEARCH BY PHASE

## Inception Phase Research

### Requirements Analysis
| Topic | External | Internal |
|-------|----------|----------|
| Similar projects | Search GitHub, case studies | Search codebase for similar |
| Technology options | Official docs, comparisons | What's already used |
| Compliance requirements | Industry standards | Existing compliance code |

### User Stories
| Topic | External | Internal |
|-------|----------|----------|
| UX patterns | Design systems, best practices | Existing UI patterns |
| User journeys | Industry examples | Similar features in codebase |

### Application Design
| Topic | External | Internal |
|-------|----------|----------|
| Architecture patterns | AWS/GCP reference architectures | Existing architecture |
| Technology decisions | Benchmarks, comparisons | Team experience |
| Security patterns | OWASP, cloud security guides | Existing security code |

## Construction Phase Research

### Functional Design
| Topic | External | Internal |
|-------|----------|----------|
| Implementation patterns | Language/framework best practices | Existing service patterns |
| API design | REST/GraphQL standards | Existing API patterns |
| Data modeling | Database best practices | Existing models |

### Code Generation
| Topic | External | Internal |
|-------|----------|----------|
| Library usage | Official docs, examples | How we use it elsewhere |
| Testing patterns | Testing best practices | Existing test patterns |
| Error handling | Framework guides | Existing error handlers |

## Operations Phase Research

### Deployment
| Topic | External | Internal |
|-------|----------|----------|
| Deployment patterns | Cloud provider guides | Existing deployments |
| Infrastructure | Terraform modules, Helm charts | Existing IaC |
| Monitoring | Observability best practices | Existing monitoring |

---

# RESEARCH QUALITY

## Good Research
- Multiple sources consulted
- Sources are current (within 2 years)
- Findings are specific and actionable
- Internal patterns identified for reuse
- Recommendations are justified

## Bad Research
- Single source only
- Outdated information
- Generic findings (not specific to context)
- No internal codebase analysis
- Recommendations without justification

## Verification
Before including in documents:
- [ ] Is the source reputable?
- [ ] Is the information current?
- [ ] Does it apply to our context?
- [ ] Have I verified key claims?
- [ ] Have I checked internal codebase too?

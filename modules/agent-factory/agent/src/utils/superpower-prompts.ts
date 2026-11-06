import { wrapUntrusted } from './trust-boundary';

interface SuperpowerIssue {
  number: number;
  title: string;
  body: string;
}

export function buildSuperpowerBrainstormingPrompt(
  issue: SuperpowerIssue, skills: string, contextSection: string,
): string {
  return `You are @agent-pt-superpower using the Superpowers workflow from obra/superpowers.

## Your Task
Process this GitHub issue using the brainstorming skill.

### Issue #${issue.number}: ${wrapUntrusted(issue.title)}

${wrapUntrusted(issue.body)}
${contextSection}
---

## Superpowers Skills Available

${skills}

---

## Instructions for Brainstorming Phase

**Follow the brainstorming skill workflow:**

1. **Don't jump into code** - Step back and understand what the user really wants
2. **Ask clarifying questions** - Identify ambiguities, assumptions, edge cases
3. **Explore alternatives** - Consider different approaches
4. **Present design in chunks** - Break it down for easy review



Return your understanding, open questions, and proposed design. Do not post the design yourself.
The host will post the exact plan with a fresh approval request and wait for an authorized writer.`;
}

export function buildSuperpowerImplementationPrompt(
  issue: SuperpowerIssue, skills: string, design: string, projectFolder: string, contextSection: string,
): string {
  return `You are @agent-pt-superpower using the Superpowers workflow from obra/superpowers.

## Context
The brainstorming phase is complete and the design has been approved.

### Issue #${issue.number}: ${wrapUntrusted(issue.title)}

${wrapUntrusted(issue.body)}

### Approved Design Summary
${wrapUntrusted(design.substring(0, 2000))}
${contextSection}
---

## Superpowers Skills Available

${skills}

---

## IMPORTANT: Project Folder Structure

**ALL generated files MUST be created inside this folder:**
\`\`\`
${projectFolder}/
\`\`\`

For example:
- Source code: \`${projectFolder}/src/\`
- Tests: \`${projectFolder}/tests/\` or \`${projectFolder}/src/__tests__/\`
- Config files: \`${projectFolder}/\`
- Documentation: \`${projectFolder}/docs/\` or \`${projectFolder}/README.md\`

**First, create the project folder if it doesn't exist:**
\`\`\`bash
mkdir -p ${projectFolder}
\`\`\`

---

## Instructions for Implementation Phase

**Follow these Superpowers skills in order:**

### 1. Writing Plans (writing-plans skill)
Create a detailed implementation plan:
- Break into 2-5 minute tasks
- Each task: exact file paths (inside ${projectFolder}/), code snippets, verification steps
- Post plan to issue for reference

### 2. Test-Driven Development (test-driven-development skill)
For each task:
- Write failing test FIRST (RED) in ${projectFolder}/tests/ or similar
- Implement minimal code to pass (GREEN)
- Refactor if needed
- Commit after each green test

### 3. Implementation
Execute the plan task by task:
- ALL files go in ${projectFolder}/
- Follow TDD strictly
- Commit after each task
- Self-review before moving on

### 4. Verification (verification-before-completion skill)
- Run all tests
- Verify the implementation actually works
- Fix any issues found

### 5. Report Results
Post a summary comment with:
- Tasks completed
- Tests passing
- Files changed (all should be in ${projectFolder}/)
- Any issues encountered

Now execute the implementation. Start by creating the plan, then implement with TDD.
Remember: ALL files must be created inside ${projectFolder}/`;
}

/**
 * Pure builder for the @agent-pt-superpower completion-summary prompt.
 *
 * Extracted from `agent-superpower.ts` (#4074, sub-EPIC #4068 · D, finding #7)
 * for two reasons:
 *
 *  1. The issue body reaches a query that runs with `allowedTools: ['Bash']`
 *     and `permissionMode: 'bypassPermissions'`, and whose tail instructs the
 *     model to run `gh issue comment`. It was interpolated raw, so anyone who
 *     can file (or a maintainer who labels) a drive-by issue could get
 *     attacker-authored instructions executed inside the run — where the run's
 *     GitHub App token and credential-plane access live. It is now wrapped with
 *     `wrapUntrusted()`, matching the sibling sites in `agent-superpower.ts`.
 *
 *  2. `agent-superpower.ts` has no exports and calls `main()` at module load,
 *     so the prompt could not be asserted on without executing the agent.
 *     Keeping the builder here makes the trust-boundary behaviour testable.
 */
import { wrapUntrusted } from './trust-boundary';

export interface SummaryPromptInput {
  /** Issue title (trusted enough to display; see #4074 follow-up for titles). */
  issueTitle: string;
  issueNumber: number | string;
  /** UNTRUSTED — attacker-controlled issue body. Wrapped before interpolation. */
  issueBody: string;
  /** Brainstorming/design phase output. */
  design: string;
  /** Implementation phase output. */
  result: string;
  /** Files created during implementation. */
  fileList: string[];
  projectFolder: string;
  /** Issue number used in the `gh issue comment` instruction. */
  commentIssueNumber: string;
}

/**
 * Build the completion-summary prompt.
 *
 * The untrusted issue body is wrapped with the trust-boundary preamble so the
 * model is told to treat it as DATA, not instructions, before it can reach the
 * Bash-enabled session.
 */
export function buildSummaryPrompt(input: SummaryPromptInput): string {
  const { issueTitle, issueNumber, issueBody, design, result, fileList, projectFolder, commentIssueNumber } = input;

  return `You just completed implementing a task. Generate a detailed, SPECIFIC completion summary for the GitHub issue.

## Original Issue
**Title**: ${issueTitle}
**Issue #**: ${issueNumber}

**Description**:
${wrapUntrusted(issueBody)}

## What Was Done

### Brainstorming/Design Phase Output:
${design.substring(0, 3000)}

### Implementation Output:
${result.substring(0, 3000)}

### Files Created (${fileList.length} total):
${fileList.slice(0, 50).join('\n')}
${fileList.length > 50 ? `\n... and ${fileList.length - 50} more files` : ''}

## Your Task

Write a GitHub comment that provides a **SPECIFIC** summary of what was accomplished.

**IMPORTANT**: Do NOT use generic phrases like "Created implementation plan" or "Analyzed requirements".
Instead, be SPECIFIC about:
- What specific design decisions were made and WHY
- What specific components/features were built
- What specific technologies/tools are used
- What specific tests were written
- Any important configuration or setup details
- Specific next steps the user should take

Format the comment as:

## 🦸 @agent-pt-superpower Complete

**Completed**: [timestamp]
**Project Folder**: \`${projectFolder}/\`

---

### 📋 Summary

[2-3 sentences summarizing the SPECIFIC outcome - what was built, not generic process description]

---

### 🎯 Key Decisions Made

[Bullet points of SPECIFIC decisions from brainstorming, e.g., "Chose Hub-and-Spoke architecture because...", "Selected ArgoCD over Flux because..."]

---

### 🔧 What Was Built

[SPECIFIC description of components created, e.g., "Terraform modules for EKS cluster with...", "ArgoCD ApplicationSets for..."]

---

### 📁 Project Structure

[Brief description of the folder structure and what each major directory contains]

---

### ✅ Tests & Validation

[What specific tests were written and what they validate]

---

### 🚀 Next Steps

[SPECIFIC, actionable next steps for THIS task, not generic instructions. E.g., "1. Set AWS credentials: export AWS_PROFILE=...", "2. Initialize Terraform: cd terraform/environments/hub && terraform init"]

---

### ⚠️ Important Notes

[Any caveats, assumptions, or things the user should be aware of specific to this implementation]

Post this summary using:
\`\`\`bash
gh issue comment ${commentIssueNumber} --body "<your summary>"
\`\`\``;
}

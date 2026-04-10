# PR Review Workflow

## Purpose
Review pull requests, ensure quality, make fixes if needed, and merge to main.

## Primary Agent
@agent-reviewer

---

## Step 1: Find the Associated PR

When triggered on an issue, first find the PR:

```bash
# Find PR linked to this issue
gh pr list --state open --json number,title,headRefName,body | \
  jq -r ".[] | select(.body | contains(\"#$ISSUE_NUMBER\") or .headRefName | contains(\"issue-$ISSUE_NUMBER\"))"

# Or by branch naming convention
gh pr list --head "agent/issue-$ISSUE_NUMBER" --json number,title,url
```

If no PR found, check if one was recently merged or closed.

---

## Step 2: Review the PR

### 2.1 Get PR Details
```bash
PR_NUMBER=<from step 1>

# View PR summary
gh pr view $PR_NUMBER

# View changed files
gh pr diff $PR_NUMBER --name-only

# View full diff
gh pr diff $PR_NUMBER
```

### 2.2 Review Checklist

**Code Quality:**
- [ ] Code follows project conventions and style
- [ ] No obvious bugs or logic errors
- [ ] Error handling is appropriate
- [ ] No hardcoded secrets or sensitive data
- [ ] No unnecessary complexity

**Completeness:**
- [ ] All acceptance criteria from issue are met
- [ ] Required files are created/modified
- [ ] No missing pieces from the task description

**Best Practices:**
- [ ] DRY - no unnecessary duplication
- [ ] SOLID principles followed (where applicable)
- [ ] Appropriate comments for complex logic
- [ ] No TODO or FIXME that should be addressed now

**Security (for infrastructure/ops PRs):**
- [ ] IAM policies follow least privilege
- [ ] No overly permissive security groups
- [ ] Secrets managed properly (not hardcoded)
- [ ] Resource naming follows conventions

---

## Step 2.5: Security Review (REQUIRED)

**IMPORTANT**: Run security checks on EVERY PR before approving.

### 2.5.1 Automated Security Scans

```bash
# Check for secrets in code (gitleaks)
if command -v gitleaks &>/dev/null; then
  gitleaks detect --source . --no-git --redact -v
fi

# Check for hardcoded secrets patterns manually
grep -rn --include="*.ts" --include="*.js" --include="*.py" --include="*.yaml" --include="*.yml" \
  -E "(password|secret|api_key|apikey|token|credential).*[=:].*['\"][^'\"]{8,}" . || true

# Check for AWS credentials
grep -rn --include="*.ts" --include="*.js" --include="*.py" --include="*.yaml" \
  -E "AKIA[0-9A-Z]{16}" . || true

# NPM audit (for Node.js projects)
if [ -f package.json ]; then
  npm audit --audit-level=high 2>/dev/null || echo "NPM audit found issues"
fi

# Check for overly permissive permissions
grep -rn --include="*.tf" --include="*.yaml" --include="*.yml" \
  -E '(\*:\*|"Action": "\*"|0\.0\.0\.0/0|::/0)' . || true
```

### 2.5.2 Manual Security Checklist

**Secrets & Credentials:**
- [ ] No hardcoded passwords, API keys, or tokens
- [ ] No AWS access keys or secret keys in code
- [ ] Secrets use environment variables or secret managers
- [ ] .gitignore includes sensitive file patterns

**Input Validation:**
- [ ] User inputs are validated and sanitized
- [ ] SQL queries use parameterized statements (no string concat)
- [ ] No eval() or exec() with user input
- [ ] File paths are validated (no path traversal)

**Authentication & Authorization:**
- [ ] Auth checks on all protected endpoints
- [ ] No auth bypass vulnerabilities
- [ ] Proper session management
- [ ] Least privilege access

**Infrastructure Security:**
- [ ] IAM roles follow least privilege
- [ ] Security groups are restrictive
- [ ] No public S3 buckets unless intended
- [ ] Encryption enabled for data at rest/transit

**Dependencies:**
- [ ] No known vulnerable dependencies (npm audit, pip-audit)
- [ ] Dependencies are from trusted sources
- [ ] Lock files are committed

### 2.5.3 OWASP Top 10 Quick Check

| Vulnerability | Check |
|--------------|-------|
| Injection | No unsanitized input in queries/commands |
| Broken Auth | Proper auth on all endpoints |
| Sensitive Data | No secrets in code, encryption used |
| XXE | XML parsing configured securely |
| Broken Access | Authorization checks present |
| Misconfig | Secure defaults, no debug in prod |
| XSS | Output encoding, CSP headers |
| Insecure Deserial | No untrusted deserialization |
| Vulnerable Deps | npm audit / pip-audit clean |
| Logging | No sensitive data in logs |

### 2.5.4 Security Issues You CAN Fix

**Secrets & Credentials:**
- Hardcoded API keys → Replace with `process.env.API_KEY`
- Hardcoded passwords → Replace with environment variable reference
- AWS credentials in code → Remove and add to .env.example

**Permissions & Access:**
- Overly permissive IAM (`*`) → Scope to specific resources/actions
- Open security groups (`0.0.0.0/0`) → Restrict to specific CIDRs
- Public S3 buckets → Add `block_public_access = true`

**Input Validation:**
- Missing input sanitization → Add validation function
- SQL string concatenation → Convert to parameterized query
- Path traversal risk → Add path validation

**Dependencies:**
- Vulnerable packages → Run `npm audit fix` or update versions
- Outdated dependencies → Update to patched versions

**Configuration:**
- Debug mode in prod → Set `DEBUG=false`
- Missing security headers → Add helmet/security middleware
- Insecure defaults → Set secure default values

### 2.5.5 Security Issues That BLOCK PR

Do NOT merge if these are found and can't be fixed:
- Authentication bypass vulnerabilities
- Authorization flaws (privilege escalation)
- Remote code execution risks
- Unfixable critical CVEs
- Business logic security decisions needed

### 2.5.6 Log Review Findings (REQUIRED)

**ALWAYS create a review log file** for tracking issues over time:

```bash
# Create review log directory if needed
mkdir -p data/code-review

# Create review log file
REVIEW_FILE="data/code-review/review-$(date +%Y%m%d)-pr-$PR_NUMBER.md"
```

**Review log format** (`data/code-review/review-YYYYMMDD-pr-NNN.md`):
```markdown
# Code Review Log

**PR**: #[PR_NUMBER]
**Issue**: #[ISSUE_NUMBER]
**Reviewer**: @agent-reviewer
**Date**: [YYYY-MM-DD]
**Branch**: [branch-name]

## Summary
- Files Reviewed: [N]
- Issues Found: [N]
- Issues Fixed: [N]
- Security Issues: [N]

## Findings

### Security Issues
| Severity | Category | File | Line | Description | Status |
|----------|----------|------|------|-------------|--------|
| HIGH | Hardcoded Secret | src/config.ts | 42 | API key in code | ✅ Fixed |
| MEDIUM | Vulnerable Dep | package.json | - | lodash < 4.17.21 | ✅ Fixed |

### Code Quality Issues
| Category | File | Line | Description | Status |
|----------|------|------|-------------|--------|
| Error Handling | src/api.ts | 55 | Missing try/catch | ✅ Fixed |
| Style | src/utils.ts | 12 | Inconsistent naming | ✅ Fixed |

## Fixes Applied
1. `src/config.ts:42` - Replaced hardcoded API key with `process.env.API_KEY`
2. `package.json` - Updated lodash to 4.17.21

## Verdict
- [x] Code Quality: Passed
- [x] Security: Passed
- [x] Ready to Merge
```

**Commit the review log to the PR branch:**
```bash
git add data/code-review/
git commit -m "docs: Add code review log for PR #$PR_NUMBER"
git push
```

This ensures review findings are tracked in the repo history.

### 2.5.7 Post Security Summary to PR

After logging, post a summary comment to the PR:

```bash
gh pr comment $PR_NUMBER --body "## 🔒 Security Review Complete

**Scans Run:**
- [x] Secret detection
- [x] Dependency audit
- [x] Permission check
- [x] OWASP quick check

**Findings:** [N] issues found, [N] fixed

| Severity | Count | Fixed |
|----------|-------|-------|
| Critical | 0 | - |
| High | 1 | 1 |
| Medium | 2 | 2 |
| Low | 0 | - |

**Review Log:** See \`data/code-review/review-YYYYMMDD-pr-$PR_NUMBER.md\`

**Verdict:** ✅ Approved for merge"
```

---

## Step 3: Provide Review Feedback

### 3.1 Post Review Comments
```bash
# Add line-specific comment
gh api repos/{owner}/{repo}/pulls/$PR_NUMBER/comments \
  -f body="Comment text" \
  -f path="file/path.ts" \
  -f line=42 \
  -f side="RIGHT"

# Add general PR comment
gh pr comment $PR_NUMBER --body "## Review Summary
...your review here..."
```

### 3.2 Review Outcomes

**If issues found that you CAN fix:**
- Checkout the PR branch
- Make the fixes
- Commit with clear message
- Push to the PR branch
- Document what you fixed

**If issues found that need original author:**
- Post detailed review comments
- Request changes
- Mark issue for re-review

**If PR looks good:**
- Proceed to merge

---

## Step 4: Make Fixes (if needed)

When fixing issues yourself:

```bash
# Checkout PR branch
gh pr checkout $PR_NUMBER

# Make your fixes
# ... edit files ...

# Commit fixes
git add -A
git commit -m "fix: Address review feedback

- Fixed [issue 1]
- Fixed [issue 2]

Reviewed-by: @agent-reviewer"

# Push to PR branch
git push
```

### What You CAN Fix:
- Typos and formatting issues
- Missing error handling
- Configuration mistakes
- Documentation gaps
- Minor logic errors
- Missing required fields
- Style/convention violations

### What Needs Human/Author:
- Major architectural changes
- Business logic questions
- Security policy decisions
- Breaking API changes

---

## Step 4.5: Check CI Status (REQUIRED)

Before approving, verify all CI checks pass:

```bash
# Check CI status on the PR
gh pr checks $PR_NUMBER --repo $TARGET_REPO

# If any checks failed, review the logs
gh run list --branch $(gh pr view $PR_NUMBER --json headRefName --jq '.headRefName') --limit 5

# View failed run logs
gh run view <RUN_ID> --log-failed
```

**If CI checks are failing:**
1. Review the CI failure logs carefully
2. Checkout the PR branch and fix the issues (lint errors, test failures, type errors, build failures)
3. Commit and push the fixes to the PR branch
4. Wait for CI to re-run and pass
5. Only then proceed to approve and merge

**Do NOT merge with failing CI checks.** Fix them first.

---

## Step 5: Approve and Merge

### 5.1 Approve PR
```bash
gh pr review $PR_NUMBER --approve --body "## Approved

Reviewed and verified:
- [x] Code quality meets standards
- [x] All acceptance criteria met
- [x] No security issues found
- [x] Ready for merge

[List any fixes you made]"
```

### 5.2 Merge PR
```bash
# Merge with squash (preferred for clean history)
gh pr merge $PR_NUMBER --squash --delete-branch

# Or merge commit (preserves all commits)
gh pr merge $PR_NUMBER --merge --delete-branch
```

### 5.3 Verify Merge
```bash
# Confirm merge succeeded
gh pr view $PR_NUMBER --json state,mergedAt

# Verify main branch has changes
git fetch origin main
git log origin/main --oneline -5
```

---

## Step 6: Document and Report

Post completion summary to the issue:

```markdown
## @agent-reviewer Complete

**PR**: #[PR_NUMBER]
**Status**: Merged to main
**Completed**: [timestamp]

### Review Summary
- Files reviewed: [N]
- Issues found: [N]
- Fixes applied: [list or "None needed"]

### Changes Merged
- [Brief description of what was merged]

### Quality Checks
- [x] Code quality verified
- [x] Acceptance criteria met
- [x] No security issues

@agent-pm - PR merged, ready for next steps.
```

---

## Error Handling

### PR Not Found
If no PR exists for the issue:
1. Check if task was supposed to create a PR
2. Post comment asking for clarification
3. Do not proceed without a PR to review

### Merge Conflicts
If PR has conflicts:
1. Post comment noting conflicts
2. Request original author to resolve
3. Do not force-merge

### CI Failures
If CI checks are failing:
1. Review CI logs
2. If fixable, make fixes
3. If not fixable, request author help

---

## Quick Reference

```bash
# Find PR for issue
gh pr list --head "agent/issue-$ISSUE_NUMBER"

# View PR diff
gh pr diff $PR_NUMBER

# Checkout PR
gh pr checkout $PR_NUMBER

# Approve PR
gh pr review $PR_NUMBER --approve

# Merge PR (squash)
gh pr merge $PR_NUMBER --squash --delete-branch
```

/**
 * GitHub Sub-Issues Module
 *
 * Provides utilities for managing GitHub's native sub-issues feature.
 * Sub-issues create a proper parent-child hierarchy visible in GitHub UI.
 *
 * GraphQL Mutations:
 * - addSubIssue: Adds an existing issue as a sub-issue of a parent
 * - removeSubIssue: Removes a sub-issue from a parent
 * - reprioritizeSubIssue: Reorders sub-issues within a parent
 *
 * Limits:
 * - Max 100 sub-issues per parent
 * - Max 8 levels of nesting
 */

import { execSync, execFileSync } from 'child_process';
import * as fs from 'fs';

// ============================================================================
// Types
// ============================================================================

export interface SubIssue {
  number: number;
  title: string;
  id: string;
}

export interface IssueWithSubIssues {
  number: number;
  title: string;
  id: string;
  subIssues: SubIssue[];
  parent?: {
    number: number;
    title: string;
  };
}

export interface AddSubIssueResult {
  success: boolean;
  parentNumber: number;
  subIssueNumber: number;
  error?: string;
}

// ============================================================================
// Input Validation (defense-in-depth against injection)
// ============================================================================

/** Maximum length for issue titles (GitHub's own limit) */
const MAX_TITLE_LENGTH = 256;
/** Maximum length for issue bodies (GitHub's own limit is ~65536) */
const MAX_BODY_LENGTH = 65536;
/** Allowed characters in GitHub labels */
const LABEL_CHARSET_RE = /^[a-zA-Z0-9: _\/.,-]+$/;

/**
 * Validate and sanitize an issue title for safe use in gh CLI argv.
 * Throws on invalid input rather than silently fixing — callers should
 * handle the error and report it rather than executing with bad data.
 */
export function validateIssueTitle(title: string): string {
  if (!title || title.trim().length === 0) {
    throw new Error('Issue title must not be empty');
  }
  if (title.length > MAX_TITLE_LENGTH) {
    throw new Error(`Issue title exceeds ${MAX_TITLE_LENGTH} characters (got ${title.length})`);
  }
  if (title.startsWith('-')) {
    throw new Error('Issue title must not start with a dash (prevents gh flag injection)');
  }
  if (title.includes('\0')) {
    throw new Error('Issue title must not contain null bytes');
  }
  return title;
}

/**
 * Validate an issue body for safe use with --body-file.
 */
export function validateIssueBody(body: string): string {
  if (body.length > MAX_BODY_LENGTH) {
    throw new Error(`Issue body exceeds ${MAX_BODY_LENGTH} characters (got ${body.length})`);
  }
  if (body.includes('\0')) {
    throw new Error('Issue body must not contain null bytes');
  }
  return body;
}

/**
 * Validate a label for safe use in gh CLI argv.
 */
export function validateLabel(label: string): string {
  if (!label || label.trim().length === 0) {
    throw new Error('Label must not be empty');
  }
  if (label.startsWith('-')) {
    throw new Error(`Label must not start with a dash: ${label}`);
  }
  if (!LABEL_CHARSET_RE.test(label)) {
    throw new Error(`Label contains invalid characters: ${label}`);
  }
  return label;
}

// ============================================================================
// Configuration
// ============================================================================

let execCommand: (cmd: string) => Promise<string> = async (cmd) => {
  return execSync(cmd, { encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024 }).trim();
};

/**
 * Safe command execution using execFileSync (no shell interpretation).
 * Used for commands where arguments may contain LLM-influenced content.
 */
let execFileCommand: (file: string, args: string[]) => Promise<string> = async (file, args) => {
  return execFileSync(file, args, { encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024 }).trim();
};

let logger: (level: string, msg: string, meta?: Record<string, unknown>) => void = (level, msg) => {
  console.log(`[${level}] ${msg}`);
};

export function configureSubIssues(config: {
  execCommand?: (cmd: string) => Promise<string>;
  execFileCommand?: (file: string, args: string[]) => Promise<string>;
  logger?: (level: string, msg: string, meta?: Record<string, unknown>) => void;
}): void {
  if (config.execCommand) execCommand = config.execCommand;
  if (config.execFileCommand) execFileCommand = config.execFileCommand;
  if (config.logger) logger = config.logger;
}

// ============================================================================
// Core Functions
// ============================================================================

/**
 * Get the node ID for an issue
 */
export async function getIssueNodeId(
  owner: string,
  repo: string,
  issueNumber: number
): Promise<string | null> {
  try {
    const query = `query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${issueNumber}) { id } } }`;
    const result = await execCommand(`gh api graphql -f query='${query}'`);
    const data = JSON.parse(result);
    return data?.data?.repository?.issue?.id || null;
  } catch (error) {
    logger('ERROR', `Failed to get issue node ID: ${(error as Error).message}`);
    return null;
  }
}

/**
 * Add an existing issue as a sub-issue of a parent issue
 */
export async function addSubIssue(
  owner: string,
  repo: string,
  parentIssueNumber: number,
  subIssueNumber: number,
  replaceParent: boolean = false
): Promise<AddSubIssueResult> {
  try {
    // Get node IDs for both issues
    const parentId = await getIssueNodeId(owner, repo, parentIssueNumber);
    const subIssueId = await getIssueNodeId(owner, repo, subIssueNumber);

    if (!parentId || !subIssueId) {
      return {
        success: false,
        parentNumber: parentIssueNumber,
        subIssueNumber: subIssueNumber,
        error: `Could not find issue node IDs (parent: ${parentId}, sub: ${subIssueId})`,
      };
    }

    const mutation = `mutation { addSubIssue(input: { issueId: "${parentId}", subIssueId: "${subIssueId}", replaceParent: ${replaceParent} }) { subIssue { number title } } }`;
    const result = await execCommand(`gh api graphql -f query='${mutation}'`);
    const data = JSON.parse(result);

    if (data?.data?.addSubIssue?.subIssue) {
      logger('INFO', `Added #${subIssueNumber} as sub-issue of #${parentIssueNumber}`);
      return {
        success: true,
        parentNumber: parentIssueNumber,
        subIssueNumber: subIssueNumber,
      };
    }

    return {
      success: false,
      parentNumber: parentIssueNumber,
      subIssueNumber: subIssueNumber,
      error: 'Unexpected response from GitHub API',
    };
  } catch (error) {
    const errorMsg = (error as Error).message;
    logger('ERROR', `Failed to add sub-issue: ${errorMsg}`);
    return {
      success: false,
      parentNumber: parentIssueNumber,
      subIssueNumber: subIssueNumber,
      error: errorMsg,
    };
  }
}

/**
 * Remove a sub-issue from its parent
 */
export async function removeSubIssue(
  owner: string,
  repo: string,
  parentIssueNumber: number,
  subIssueNumber: number
): Promise<boolean> {
  try {
    const parentId = await getIssueNodeId(owner, repo, parentIssueNumber);
    const subIssueId = await getIssueNodeId(owner, repo, subIssueNumber);

    if (!parentId || !subIssueId) {
      logger('ERROR', 'Could not find issue node IDs');
      return false;
    }

    const mutation = `mutation { removeSubIssue(input: { issueId: "${parentId}", subIssueId: "${subIssueId}" }) { issue { id } } }`;
    await execCommand(`gh api graphql -f query='${mutation}'`);
    logger('INFO', `Removed #${subIssueNumber} from parent #${parentIssueNumber}`);
    return true;
  } catch (error) {
    logger('ERROR', `Failed to remove sub-issue: ${(error as Error).message}`);
    return false;
  }
}

/**
 * Get all sub-issues for a given issue (recursive up to specified depth)
 */
export async function getSubIssues(
  owner: string,
  repo: string,
  issueNumber: number,
  depth: number = 2
): Promise<IssueWithSubIssues | null> {
  try {
    // Build nested query based on depth
    const buildSubIssueFragment = (d: number): string => {
      if (d <= 0) return '';
      return `subIssues(first: 100) { nodes { number title id ${buildSubIssueFragment(d - 1)} } }`;
    };

    const query = `query {
      repository(owner: "${owner}", name: "${repo}") {
        issue(number: ${issueNumber}) {
          id number title
          parent { number title }
          ${buildSubIssueFragment(depth)}
        }
      }
    }`;

    const result = await execCommand(`gh api graphql -f query='${query}'`);
    const data = JSON.parse(result);
    const issue = data?.data?.repository?.issue;

    if (!issue) return null;

    const parseSubIssues = (nodes: Array<{ number: number; title: string; id: string; subIssues?: { nodes: unknown[] } }>): SubIssue[] => {
      return (nodes || []).map((n) => ({
        number: n.number,
        title: n.title,
        id: n.id,
      }));
    };

    return {
      number: issue.number,
      title: issue.title,
      id: issue.id,
      parent: issue.parent,
      subIssues: parseSubIssues(issue.subIssues?.nodes || []),
    };
  } catch (error) {
    logger('ERROR', `Failed to get sub-issues: ${(error as Error).message}`);
    return null;
  }
}

/**
 * Create a new issue and immediately add it as a sub-issue.
 *
 * SECURITY: Uses execFileSync with argv array (no shell) to prevent command
 * injection from LLM-influenced title/body strings. Body is written to a temp
 * file and passed via --body-file to avoid any shell interpretation.
 * See: #1162, #1149, #615/H7.
 */
export async function createSubIssue(
  owner: string,
  repo: string,
  parentIssueNumber: number,
  title: string,
  body: string,
  labels: string[] = []
): Promise<{ issueNumber: number; success: boolean; error?: string }> {
  // Validate all inputs before execution (defense-in-depth)
  const validatedTitle = validateIssueTitle(title);
  const validatedBody = validateIssueBody(body);
  const validatedLabels = labels.map(validateLabel);

  // Write body to temp file to eliminate any possibility of shell interpretation.
  // Mirrors the safe pattern in utils/ghPost.ts.
  const tmpFile = `/tmp/sub-issue-body-${Date.now()}-${Math.random().toString(36).slice(2)}.md`;

  try {
    fs.writeFileSync(tmpFile, validatedBody);

    // Build argv array — no shell, no string interpolation, no injection.
    const args = [
      'issue', 'create',
      '--repo', `${owner}/${repo}`,
      '--title', validatedTitle,
      '--body-file', tmpFile,
    ];
    for (const label of validatedLabels) {
      args.push('--label', label);
    }

    // execFileCommand uses execFileSync (no shell) — title/body are passed as
    // literal argv entries, never interpreted by /bin/sh.
    const createResult = await execFileCommand('gh', args);

    // Extract issue number from URL
    const match = createResult.match(/\/issues\/(\d+)/);
    if (!match) {
      return { issueNumber: 0, success: false, error: 'Could not extract issue number from creation result' };
    }
    const newIssueNumber = parseInt(match[1], 10);

    // Add as sub-issue
    const addResult = await addSubIssue(owner, repo, parentIssueNumber, newIssueNumber);

    if (!addResult.success) {
      logger('WARN', `Created issue #${newIssueNumber} but failed to add as sub-issue: ${addResult.error}`);
    }

    return {
      issueNumber: newIssueNumber,
      success: addResult.success,
      error: addResult.error,
    };
  } catch (error) {
    logger('ERROR', `Failed to create sub-issue: ${(error as Error).message}`);
    return { issueNumber: 0, success: false, error: (error as Error).message };
  } finally {
    try { fs.unlinkSync(tmpFile); } catch { /* temp file cleanup is best-effort */ }
  }
}

/**
 * Get the parent issue of a given issue (if any)
 */
export async function getParentIssue(
  owner: string,
  repo: string,
  issueNumber: number
): Promise<{ number: number; title: string } | null> {
  try {
    const query = `query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${issueNumber}) { parent { number title } } } }`;
    const result = await execCommand(`gh api graphql -f query='${query}'`);
    const data = JSON.parse(result);
    return data?.data?.repository?.issue?.parent || null;
  } catch (error) {
    logger('ERROR', `Failed to get parent issue: ${(error as Error).message}`);
    return null;
  }
}

/**
 * Build a tree representation of issues for display
 */
export function formatIssueTree(issue: IssueWithSubIssues, indent: string = ''): string {
  let output = `${indent}#${issue.number}: ${issue.title}\n`;
  for (const sub of issue.subIssues) {
    output += `${indent}  └── #${sub.number}: ${sub.title}\n`;
  }
  return output;
}

// ============================================================================
// CLI Helper Commands (for use in agent prompts)
// ============================================================================

/**
 * Generate gh CLI commands for sub-issue operations
 * These can be included in agent prompts for direct execution
 */
export const CLI_COMMANDS = {
  addSubIssue: (owner: string, repo: string, parentNumber: number, subNumber: number) => `
# Add #${subNumber} as sub-issue of #${parentNumber}
PARENT_ID=$(gh api graphql -f query='query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${parentNumber}) { id } } }' --jq '.data.repository.issue.id')
SUB_ID=$(gh api graphql -f query='query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${subNumber}) { id } } }' --jq '.data.repository.issue.id')
gh api graphql -f query="mutation { addSubIssue(input: { issueId: \\"$PARENT_ID\\", subIssueId: \\"$SUB_ID\\" }) { subIssue { number } } }"
`,

  getSubIssues: (owner: string, repo: string, issueNumber: number) => `
# Get sub-issues of #${issueNumber}
gh api graphql -f query='query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${issueNumber}) { subIssues(first: 100) { nodes { number title state } } } } }' --jq '.data.repository.issue.subIssues.nodes'
`,

  getParent: (owner: string, repo: string, issueNumber: number) => `
# Get parent of #${issueNumber}
gh api graphql -f query='query { repository(owner: "${owner}", name: "${repo}") { issue(number: ${issueNumber}) { parent { number title } } } }' --jq '.data.repository.issue.parent'
`,
};

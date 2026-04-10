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

import { execSync } from 'child_process';

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
// Configuration
// ============================================================================

let execCommand: (cmd: string) => Promise<string> = async (cmd) => {
  return execSync(cmd, { encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024 }).trim();
};

let logger: (level: string, msg: string, meta?: Record<string, unknown>) => void = (level, msg) => {
  console.log(`[${level}] ${msg}`);
};

export function configureSubIssues(config: {
  execCommand?: (cmd: string) => Promise<string>;
  logger?: (level: string, msg: string, meta?: Record<string, unknown>) => void;
}): void {
  if (config.execCommand) execCommand = config.execCommand;
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
 * Create a new issue and immediately add it as a sub-issue
 */
export async function createSubIssue(
  owner: string,
  repo: string,
  parentIssueNumber: number,
  title: string,
  body: string,
  labels: string[] = []
): Promise<{ issueNumber: number; success: boolean; error?: string }> {
  try {
    // Create the issue first
    const labelArgs = labels.length > 0 ? `--label "${labels.join(',')}"` : '';
    const createCmd = `gh issue create --repo ${owner}/${repo} --title "${title.replace(/"/g, '\\"')}" --body "${body.replace(/"/g, '\\"')}" ${labelArgs}`;
    const createResult = await execCommand(createCmd);

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

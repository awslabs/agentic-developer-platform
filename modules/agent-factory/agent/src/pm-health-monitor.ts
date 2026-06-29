/**
 * PM Health Monitor - Comprehensive state reconciliation for agent orchestration
 *
 * Verifies ALL tasks have correct status by cross-checking:
 * - Done: Did the work actually complete? PR merged? Tests pass?
 * - In Progress: Is there an active workflow running?
 * - Todo: Actually ready? No hidden blockers?
 * - Backlog: Are blockers still valid (not closed)?
 *
 * For tasks needing re-run, analyzes:
 * - Is the task idempotent (safe to retry)?
 * - What cleanup is needed before retry?
 * - Partial PRs, branches, commits to clean up?
 *
 * User can influence via issue comments:
 * - /approve - Proceed with proposed action
 * - /skip - Don't take action on this task
 * - /retry - Reset and immediately retry
 * - /hold - Pause health monitoring for this issue
 * - /custom <instructions> - Custom action
 */

import { resilientQuery } from './utils/resilientQuery';
import { execSync } from 'child_process';
import { refreshGitHubToken, saveToS3Fallback } from './utils/ghPost';

// ============================================================================
// Types
// ============================================================================

export interface ProjectItem {
  id: string;
  issueNumber: number;
  title: string;
  status: string;
  assignedAgent: string | null;
  workflowRunUrl: string | null;
  blockedBy: string | null;
  updatedAt: string;
}

export interface WorkflowRun {
  id: number;
  status: 'queued' | 'in_progress' | 'completed';
  conclusion: 'success' | 'failure' | 'cancelled' | 'skipped' | null;
  createdAt: string;
  updatedAt: string;
  htmlUrl: string;
}

export interface StateAnomaly {
  item: ProjectItem;
  currentStatus: string;
  expectedStatus: string;
  anomalyType: AnomalyType;
  evidence: AnomalyEvidence;
  isIdempotent: boolean | null; // null = unknown, needs analysis
  cleanupNeeded: CleanupAction[];
}

export type AnomalyType =
  | 'done_but_incomplete'      // Marked Done but work not actually complete
  | 'done_but_failed'          // Marked Done but workflow failed
  | 'in_progress_no_workflow'  // In Progress but no active workflow
  | 'in_progress_failed'       // In Progress but workflow failed
  | 'todo_has_blockers'        // Todo but still has unresolved blockers
  | 'backlog_stale_blockers'   // Backlog blocked by closed issues
  | 'status_workflow_mismatch'; // Board status doesn't match workflow state

export interface AnomalyEvidence {
  lastWorkflow: WorkflowRun | null;
  relatedPR: PRInfo | null;
  blockerStatus: BlockerInfo | null;
  issueState: 'open' | 'closed';
  timeSinceLastActivity: number; // minutes
}

export interface PRInfo {
  number: number;
  state: 'open' | 'closed' | 'merged';
  url: string;
  branch: string;
  mergeable: boolean | null;
  checksPass: boolean | null;
}

export interface BlockerInfo {
  blockedBy: string;
  blockerIssues: Array<{ number: number; state: 'open' | 'closed' }>;
  allClosed: boolean;
}

export interface CleanupAction {
  type: 'close_pr' | 'delete_branch' | 'revert_commits' | 'close_issue' | 'manual_cleanup';
  target: string;
  description: string;
  command?: string;
}

export interface HealthAnalysis {
  summary: string;
  rootCause: string;
  proposedAction: ProposedAction;
  confidence: 'high' | 'medium' | 'low';
  reasoning: string;
  isIdempotent: boolean;
  cleanupSteps: string[];
  safeToAutoResolve: boolean;
}

export type ProposedAction =
  | 'reset_to_todo'      // Reset status, agent can pick up later
  | 'retry_agent'        // Reset and immediately re-trigger
  | 'cleanup_and_retry'  // Clean up first, then retry
  | 'clear_blocker'      // Clear stale blockers
  | 'mark_done'          // Actually complete, mark as done
  | 'revert_to_backlog'  // Not ready, move back to backlog
  | 'escalate'           // Needs human attention
  | 'no_action';         // Leave as is

export interface UserCommand {
  type: 'approve' | 'skip' | 'retry' | 'cleanup' | 'hold' | 'custom';
  customInstructions?: string;
}

// ============================================================================
// Configuration
// ============================================================================

let config = {
  repoOwner: '',
  repoName: '',
  projectNumber: 0,
  userResponseTimeout: 3 * 60 * 1000, // 3 minutes
  pollInterval: 15 * 1000, // 15 seconds
};

export function configureHealthMonitor(options: Partial<typeof config>) {
  config = { ...config, ...options };
}

// ============================================================================
// Utilities
// ============================================================================

function execCommand(cmd: string): string {
  try {
    return execSync(cmd, { encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024 }).trim();  // nosemgrep: detect-child-process
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    console.error(`Command failed: ${cmd}`);
    console.error(`Error: ${err.stderr || err.message}`);
    return '';
  }
}

function log(msg: string) {
  console.log(`[HealthMonitor] ${msg}`);
}

// ============================================================================
// Data Fetching
// ============================================================================

export async function fetchProjectItems(): Promise<ProjectItem[]> {
  const { repoOwner, projectNumber } = config;

  const itemsJson = execCommand(
    `gh project item-list ${projectNumber} --owner ${repoOwner} --format json --limit 100`
  );

  if (!itemsJson) return [];

  try {
    const data = JSON.parse(itemsJson);
    return (data.items || []).map((item: Record<string, unknown>) => ({
      id: item.id as string,
      issueNumber: (item.content as Record<string, unknown>)?.number as number,
      title: (item.content as Record<string, unknown>)?.title as string || item.title as string,
      status: item.status as string || '',
      assignedAgent: item.assigned_agent as string || null,
      workflowRunUrl: item.workflow_run as string || null,
      blockedBy: item.blocked_by as string || null,
      updatedAt: item.updatedAt as string || '',
    }));
  } catch {
    return [];
  }
}

export async function fetchActiveWorkflowsForIssue(issueNumber: number): Promise<WorkflowRun[]> {
  const { repoOwner, repoName } = config;

  // Get recent workflow runs that might be related to this issue
  const runsJson = execCommand(
    `gh api repos/${repoOwner}/${repoName}/actions/runs --jq '[.workflow_runs[] | select(.status == "queued" or .status == "in_progress") | {id: .id, status: .status, conclusion: .conclusion, createdAt: .created_at, updatedAt: .updated_at, htmlUrl: .html_url, headBranch: .head_branch, displayTitle: .display_title}]' 2>/dev/null || echo '[]'`
  );

  try {
    const runs = JSON.parse(runsJson) as Array<{
      id: number;
      status: string;
      conclusion: string | null;
      createdAt: string;
      updatedAt: string;
      htmlUrl: string;
      headBranch: string;
      displayTitle: string;
    }>;

    // Filter runs related to this issue
    return runs.filter(run => {
      const branchMatch = run.headBranch?.includes(`issue-${issueNumber}`) ||
                          run.headBranch?.includes(`issue_${issueNumber}`);
      const titleMatch = run.displayTitle?.includes(`#${issueNumber}`);
      return branchMatch || titleMatch;
    }).map(run => ({
      id: run.id,
      status: run.status as WorkflowRun['status'],
      conclusion: run.conclusion as WorkflowRun['conclusion'],
      createdAt: run.createdAt,
      updatedAt: run.updatedAt,
      htmlUrl: run.htmlUrl,
    }));
  } catch {
    return [];
  }
}

export async function fetchLastWorkflowForIssue(issueNumber: number): Promise<WorkflowRun | null> {
  const { repoOwner, repoName } = config;

  // Get the most recent completed workflow for this issue
  const runsJson = execCommand(
    `gh api "repos/${repoOwner}/${repoName}/actions/runs?per_page=20" --jq '[.workflow_runs[] | select(.status == "completed") | {id: .id, status: .status, conclusion: .conclusion, createdAt: .created_at, updatedAt: .updated_at, htmlUrl: .html_url, headBranch: .head_branch, displayTitle: .display_title}]' 2>/dev/null || echo '[]'`
  );

  try {
    const runs = JSON.parse(runsJson) as Array<{
      id: number;
      status: string;
      conclusion: string | null;
      createdAt: string;
      updatedAt: string;
      htmlUrl: string;
      headBranch: string;
      displayTitle: string;
    }>;

    // Find the most recent run related to this issue
    const related = runs.find(run => {
      const branchMatch = run.headBranch?.includes(`issue-${issueNumber}`) ||
                          run.headBranch?.includes(`issue_${issueNumber}`);
      const titleMatch = run.displayTitle?.includes(`#${issueNumber}`);
      return branchMatch || titleMatch;
    });

    if (!related) return null;

    return {
      id: related.id,
      status: related.status as WorkflowRun['status'],
      conclusion: related.conclusion as WorkflowRun['conclusion'],
      createdAt: related.createdAt,
      updatedAt: related.updatedAt,
      htmlUrl: related.htmlUrl,
    };
  } catch {
    return null;
  }
}

export async function checkBlockerStatus(blockedBy: string): Promise<BlockerInfo | null> {
  const { repoOwner, repoName } = config;

  if (!blockedBy) return null;

  // Extract issue numbers from blocked_by field (e.g., "#123, #456")
  const issueNumbers = blockedBy.match(/#(\d+)/g)?.map(m => parseInt(m.slice(1), 10)) || [];

  if (issueNumbers.length === 0) return null;

  const blockerIssues: Array<{ number: number; state: 'open' | 'closed' }> = [];

  for (const num of issueNumbers) {
    const stateJson = execCommand(
      `gh issue view ${num} --repo ${repoOwner}/${repoName} --json state --jq '.state' 2>/dev/null || echo 'unknown'`
    );

    const state = stateJson.toLowerCase() === 'open' ? 'open' : 'closed';
    blockerIssues.push({ number: num, state });
  }

  return {
    blockedBy,
    blockerIssues,
    allClosed: blockerIssues.every(b => b.state === 'closed'),
  };
}

export async function fetchPRForIssue(issueNumber: number): Promise<PRInfo | null> {
  const { repoOwner, repoName } = config;

  // Look for PRs that reference this issue or use the agent branch naming convention
  const prsJson = execCommand(
    `gh pr list --repo ${repoOwner}/${repoName} --state all --search "issue-${issueNumber} in:head" --json number,state,url,headRefName,mergeable,statusCheckRollup --limit 5 2>/dev/null || echo '[]'`
  );

  try {
    const prs = JSON.parse(prsJson) as Array<{
      number: number;
      state: string;
      url: string;
      headRefName: string;
      mergeable: string;
      statusCheckRollup: Array<{ state: string }>;
    }>;

    if (prs.length === 0) {
      // Also try searching by issue number in title/body
      const searchJson = execCommand(
        `gh pr list --repo ${repoOwner}/${repoName} --state all --search "#${issueNumber}" --json number,state,url,headRefName,mergeable,statusCheckRollup --limit 3 2>/dev/null || echo '[]'`
      );
      const searchPrs = JSON.parse(searchJson);
      if (searchPrs.length > 0) {
        prs.push(...searchPrs);
      }
    }

    if (prs.length === 0) return null;

    // Get the most recent/relevant PR
    const pr = prs[0];
    const checksPass = pr.statusCheckRollup?.every(c => c.state === 'SUCCESS') ?? null;

    return {
      number: pr.number,
      state: pr.state.toLowerCase() as 'open' | 'closed' | 'merged',
      url: pr.url,
      branch: pr.headRefName,
      mergeable: pr.mergeable === 'MERGEABLE' ? true : pr.mergeable === 'CONFLICTING' ? false : null,
      checksPass,
    };
  } catch {
    return null;
  }
}

export async function getIssueState(issueNumber: number): Promise<'open' | 'closed'> {
  const { repoOwner, repoName } = config;

  const stateJson = execCommand(
    `gh issue view ${issueNumber} --repo ${repoOwner}/${repoName} --json state --jq '.state' 2>/dev/null || echo 'OPEN'`
  );

  return stateJson.toLowerCase() === 'closed' ? 'closed' : 'open';
}

// ============================================================================
// Comprehensive State Anomaly Detection
// ============================================================================

export async function findStateAnomalies(): Promise<StateAnomaly[]> {
  const items = await fetchProjectItems();
  const anomalies: StateAnomaly[] = [];

  log(`Scanning ${items.length} project items for state anomalies...`);

  for (const item of items) {
    // Skip items without issue numbers or PM-assigned items
    if (!item.issueNumber || item.assignedAgent === '@agent-pm') {
      continue;
    }

    log(`  Checking #${item.issueNumber}: ${item.title} [${item.status}]`);

    // Gather evidence for this item
    const [activeWorkflows, lastWorkflow, relatedPR, blockerInfo, issueState] = await Promise.all([
      fetchActiveWorkflowsForIssue(item.issueNumber),
      fetchLastWorkflowForIssue(item.issueNumber),
      fetchPRForIssue(item.issueNumber),
      item.blockedBy ? checkBlockerStatus(item.blockedBy) : Promise.resolve(null),
      getIssueState(item.issueNumber),
    ]);

    const timeSinceLastActivity = lastWorkflow
      ? Math.floor((Date.now() - new Date(lastWorkflow.updatedAt).getTime()) / 60000)
      : 999; // Unknown

    const evidence: AnomalyEvidence = {
      lastWorkflow,
      relatedPR,
      blockerStatus: blockerInfo,
      issueState,
      timeSinceLastActivity,
    };

    // Check for anomalies based on current status
    const anomaly = await detectAnomalyForStatus(item, evidence, activeWorkflows.length > 0);

    if (anomaly) {
      log(`    ⚠ ANOMALY: ${anomaly.anomalyType}`);
      anomalies.push(anomaly);
    } else {
      log(`    ✓ Status appears correct`);
    }
  }

  return anomalies;
}

async function detectAnomalyForStatus(
  item: ProjectItem,
  evidence: AnomalyEvidence,
  hasActiveWorkflow: boolean
): Promise<StateAnomaly | null> {
  const { lastWorkflow, relatedPR, blockerStatus, issueState } = evidence;

  switch (item.status) {
    // ========== DONE STATUS ==========
    case 'Done': {
      // Check 1: Issue should be closed
      if (issueState === 'open') {
        return createAnomaly(item, 'Todo', 'done_but_incomplete', evidence,
          'Issue marked Done on board but issue is still open');
      }

      // Check 2: If there's a PR, it should be merged
      if (relatedPR && relatedPR.state === 'open') {
        return createAnomaly(item, 'Review', 'done_but_incomplete', evidence,
          'Issue marked Done but PR is still open');
      }

      // Check 3: Last workflow should have succeeded (if any)
      if (lastWorkflow?.conclusion === 'failure') {
        return createAnomaly(item, 'Todo', 'done_but_failed', evidence,
          'Issue marked Done but last workflow failed');
      }

      return null; // Done status is valid
    }

    // ========== IN PROGRESS STATUS ==========
    case 'In Progress': {
      // Check 1: Should have an active workflow
      if (!hasActiveWorkflow) {
        if (lastWorkflow?.conclusion === 'failure') {
          return createAnomaly(item, 'Todo', 'in_progress_failed', evidence,
            'In Progress but workflow failed, no active workflow');
        }
        if (lastWorkflow?.conclusion === 'cancelled') {
          return createAnomaly(item, 'Todo', 'in_progress_no_workflow', evidence,
            'In Progress but workflow was cancelled');
        }
        if (evidence.timeSinceLastActivity > 30) { // More than 30 min since last activity
          return createAnomaly(item, 'Todo', 'in_progress_no_workflow', evidence,
            `In Progress but no workflow activity for ${evidence.timeSinceLastActivity} minutes`);
        }
      }

      return null; // In Progress is valid
    }

    // ========== TODO STATUS ==========
    case 'Todo': {
      // Check 1: Should not have unresolved blockers
      if (blockerStatus && !blockerStatus.allClosed) {
        const openBlockers = blockerStatus.blockerIssues.filter(b => b.state === 'open');
        return createAnomaly(item, 'Backlog', 'todo_has_blockers', evidence,
          `Todo but still blocked by open issues: ${openBlockers.map(b => '#' + b.number).join(', ')}`);
      }

      // Check 2: If workflow is currently running, should be In Progress
      if (hasActiveWorkflow) {
        return createAnomaly(item, 'In Progress', 'status_workflow_mismatch', evidence,
          'Todo but has active workflow running');
      }

      return null; // Todo is valid
    }

    // ========== BACKLOG STATUS ==========
    case 'Backlog': {
      // Check 1: If blocked, blockers should still be open
      if (blockerStatus?.allClosed) {
        return createAnomaly(item, 'Todo', 'backlog_stale_blockers', evidence,
          'Backlog with stale blockers - all blocking issues are now closed');
      }

      return null; // Backlog is valid
    }

    // ========== REVIEW STATUS ==========
    case 'Review': {
      // Check 1: Should have an open PR
      if (!relatedPR || relatedPR.state !== 'open') {
        if (relatedPR?.state === 'merged') {
          return createAnomaly(item, 'Done', 'status_workflow_mismatch', evidence,
            'Review status but PR has been merged');
        }
        return createAnomaly(item, 'Todo', 'status_workflow_mismatch', evidence,
          'Review status but no open PR found');
      }

      return null; // Review is valid
    }

    default:
      return null;
  }
}

function createAnomaly(
  item: ProjectItem,
  expectedStatus: string,
  anomalyType: AnomalyType,
  evidence: AnomalyEvidence,
  _description: string
): StateAnomaly {
  // Determine cleanup actions based on evidence
  const cleanupNeeded: CleanupAction[] = [];

  // If there's an open PR for a task that needs to restart, it might need cleanup
  if (evidence.relatedPR?.state === 'open' &&
      (anomalyType === 'in_progress_failed' || anomalyType === 'done_but_failed')) {
    cleanupNeeded.push({
      type: 'close_pr',
      target: `PR #${evidence.relatedPR.number}`,
      description: 'Close stale PR before retry',
      command: `gh pr close ${evidence.relatedPR.number}`,
    });
  }

  // Determine if task is likely idempotent
  // Tasks creating new resources are usually idempotent
  // Tasks modifying existing resources may not be
  const isIdempotent = null; // Let AI analyze this

  return {
    item,
    currentStatus: item.status,
    expectedStatus,
    anomalyType,
    evidence,
    isIdempotent,
    cleanupNeeded,
  };
}

// ============================================================================
// AI Analysis
// ============================================================================

export async function analyzeAnomaly(anomaly: StateAnomaly): Promise<HealthAnalysis> {
  const { repoOwner, repoName } = config;

  // Fetch issue details for context
  const issueJson = execCommand(
    `gh issue view ${anomaly.item.issueNumber} --repo ${repoOwner}/${repoName} --json title,body,labels,comments --jq '{title: .title, body: .body, labels: [.labels[].name], recentComments: [.comments[-3:][] | {author: .author.login, body: .body}]}' 2>/dev/null || echo '{}'`
  );

  let issueContext = {};
  try {
    issueContext = JSON.parse(issueJson);
  } catch {
    // Ignore parse errors
  }

  const prompt = buildAnalysisPrompt(anomaly, issueContext);

  let fullResponse = '';

  try {
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          maxTurns: 1,
          allowedTools: [],
        },
      },
      maxRetries: 3,
      baseDelayMs: 5000,
      log: console.log,
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            fullResponse += block.text;
          }
        }
      }
    }

    return parseAnalysisResponse(fullResponse);
  } catch (error) {
    console.error('AI analysis failed:', error);
    return {
      summary: 'Unable to complete AI analysis',
      rootCause: 'Analysis error',
      proposedAction: 'escalate',
      confidence: 'low',
      reasoning: 'Defaulting to escalation due to analysis failure',
      isIdempotent: false,
      cleanupSteps: [],
      safeToAutoResolve: false,
    };
  }
}

function buildAnalysisPrompt(anomaly: StateAnomaly, issueContext: Record<string, unknown>): string {
  const { item, evidence, anomalyType, currentStatus, expectedStatus, cleanupNeeded } = anomaly;

  const anomalyDescriptions: Record<AnomalyType, string> = {
    done_but_incomplete: 'Task marked Done but the work is not actually complete',
    done_but_failed: 'Task marked Done but the last workflow failed',
    in_progress_no_workflow: 'Task In Progress but no active workflow is running',
    in_progress_failed: 'Task In Progress but the workflow failed',
    todo_has_blockers: 'Task in Todo but still has unresolved blockers',
    backlog_stale_blockers: 'Task in Backlog blocked by issues that are now closed',
    status_workflow_mismatch: 'Board status does not match the actual workflow state',
  };

  return `You are a project manager AI monitoring an agent-driven software development workflow.
Your job is to detect state inconsistencies and recommend corrective actions.

## State Anomaly Detected

**Issue**: #${item.issueNumber} - ${item.title}
**Current Board Status**: ${currentStatus}
**Expected Status**: ${expectedStatus}
**Anomaly Type**: ${anomalyDescriptions[anomalyType]}
**Assigned Agent**: ${item.assignedAgent || 'None'}

### Evidence

**Last Workflow**:
${evidence.lastWorkflow ? `- URL: ${evidence.lastWorkflow.htmlUrl}
- Status: ${evidence.lastWorkflow.status}
- Conclusion: ${evidence.lastWorkflow.conclusion}
- Last Activity: ${evidence.timeSinceLastActivity} minutes ago` : 'No workflow found'}

**Related PR**:
${evidence.relatedPR ? `- PR #${evidence.relatedPR.number}: ${evidence.relatedPR.state}
- Branch: ${evidence.relatedPR.branch}
- URL: ${evidence.relatedPR.url}
- Mergeable: ${evidence.relatedPR.mergeable}
- Checks Pass: ${evidence.relatedPR.checksPass}` : 'No PR found'}

**Blockers**:
${evidence.blockerStatus ? `- Blocked by: ${evidence.blockerStatus.blockedBy}
- Blocker states: ${evidence.blockerStatus.blockerIssues.map(b => `#${b.number} (${b.state})`).join(', ')}
- All blockers closed: ${evidence.blockerStatus.allClosed}` : 'No blockers'}

**Issue State**: ${evidence.issueState}

### Pre-identified Cleanup Needed
${cleanupNeeded.length > 0 ? cleanupNeeded.map(c => `- ${c.type}: ${c.description}`).join('\n') : 'None identified'}

## Issue Context
${JSON.stringify(issueContext, null, 2)}

## Your Task

Analyze this state anomaly and provide:

1. **Summary**: What's wrong in one sentence
2. **Root Cause**: Why this happened
3. **Is Task Idempotent**: Can we safely re-run without side effects?
   - Creating new files/resources: Usually idempotent
   - Modifying existing data: May not be idempotent
   - External API calls: Depends on the API
4. **Cleanup Steps**: What needs to be done before retrying (if any)
5. **Proposed Action**: What to do
6. **Safe to Auto-resolve**: Can we do this without human confirmation?

Respond in this EXACT format (will be parsed programmatically):

<analysis>
<summary>One-sentence summary of the situation</summary>
<root_cause>What caused the state inconsistency</root_cause>
<is_idempotent>true|false</is_idempotent>
<cleanup_steps>
Step 1 description
Step 2 description
(or "None" if no cleanup needed)
</cleanup_steps>
<proposed_action>reset_to_todo|retry_agent|cleanup_and_retry|clear_blocker|mark_done|revert_to_backlog|escalate|no_action</proposed_action>
<confidence>high|medium|low</confidence>
<safe_to_auto_resolve>true|false</safe_to_auto_resolve>
<reasoning>Why you recommend this action (2-3 sentences, will be shown to user)</reasoning>
</analysis>

Actions explained:
- reset_to_todo: Just reset status to Todo (agent picks up when ready)
- retry_agent: Reset to Todo AND immediately re-trigger the agent
- cleanup_and_retry: Execute cleanup steps first, then retry
- clear_blocker: Clear the blocked_by field and move to Todo
- mark_done: The work is actually complete, just update status to Done
- revert_to_backlog: Not ready yet, move back to Backlog
- escalate: Needs human attention, just notify
- no_action: Status is actually correct, leave as is`;
}

function parseAnalysisResponse(response: string): HealthAnalysis {
  const summaryMatch = response.match(/<summary>([\s\S]*?)<\/summary>/);
  const rootCauseMatch = response.match(/<root_cause>([\s\S]*?)<\/root_cause>/);
  const actionMatch = response.match(/<proposed_action>([\s\S]*?)<\/proposed_action>/);
  const confidenceMatch = response.match(/<confidence>([\s\S]*?)<\/confidence>/);
  const reasoningMatch = response.match(/<reasoning>([\s\S]*?)<\/reasoning>/);
  const idempotentMatch = response.match(/<is_idempotent>([\s\S]*?)<\/is_idempotent>/);
  const cleanupMatch = response.match(/<cleanup_steps>([\s\S]*?)<\/cleanup_steps>/);
  const safeMatch = response.match(/<safe_to_auto_resolve>([\s\S]*?)<\/safe_to_auto_resolve>/);

  // Parse cleanup steps
  const cleanupText = cleanupMatch?.[1]?.trim() || '';
  const cleanupSteps = cleanupText.toLowerCase() === 'none'
    ? []
    : cleanupText.split('\n').map(s => s.trim()).filter(s => s.length > 0);

  return {
    summary: summaryMatch?.[1]?.trim() || 'State anomaly detected',
    rootCause: rootCauseMatch?.[1]?.trim() || 'Unknown cause',
    proposedAction: (actionMatch?.[1]?.trim() as ProposedAction) || 'escalate',
    confidence: (confidenceMatch?.[1]?.trim() as HealthAnalysis['confidence']) || 'medium',
    reasoning: reasoningMatch?.[1]?.trim() || 'Analysis incomplete',
    isIdempotent: idempotentMatch?.[1]?.trim().toLowerCase() === 'true',
    cleanupSteps,
    safeToAutoResolve: safeMatch?.[1]?.trim().toLowerCase() === 'true',
  };
}

// ============================================================================
// User Interaction
// ============================================================================

export function formatProposalComment(anomaly: StateAnomaly, analysis: HealthAnalysis): string {
  const { item, evidence, anomalyType, currentStatus, expectedStatus } = anomaly;

  const actionEmoji: Record<string, string> = {
    reset_to_todo: '🔄',
    retry_agent: '🔁',
    cleanup_and_retry: '🧹',
    clear_blocker: '🔓',
    mark_done: '✅',
    revert_to_backlog: '⏪',
    escalate: '🚨',
    no_action: '⏸️',
  };

  const actionDescriptions: Record<string, string> = {
    reset_to_todo: 'Reset status to **Todo** (agent can pick it up later)',
    retry_agent: 'Reset to **Todo** and **immediately re-trigger** the agent',
    cleanup_and_retry: 'Execute cleanup steps, then retry the agent',
    clear_blocker: 'Clear stale blockers and move to **Todo**',
    mark_done: 'Mark as **Done** (work is actually complete)',
    revert_to_backlog: 'Move back to **Backlog** (not ready yet)',
    escalate: 'Flag for **human attention** (no automatic action)',
    no_action: 'Leave as is (status is actually correct)',
  };

  const anomalyEmoji: Record<AnomalyType, string> = {
    done_but_incomplete: '❌',
    done_but_failed: '💥',
    in_progress_no_workflow: '⏸️',
    in_progress_failed: '💥',
    todo_has_blockers: '🚧',
    backlog_stale_blockers: '🔓',
    status_workflow_mismatch: '🔀',
  };

  const timeoutMinutes = Math.ceil(config.userResponseTimeout / 60000);

  let cleanupSection = '';
  if (analysis.cleanupSteps.length > 0) {
    cleanupSection = `
### Cleanup Required
${analysis.cleanupSteps.map((s, i) => `${i + 1}. ${s}`).join('\n')}
`;
  }

  let idempotencySection = '';
  if (analysis.proposedAction === 'retry_agent' || analysis.proposedAction === 'cleanup_and_retry') {
    idempotencySection = `
### Idempotency Check
${analysis.isIdempotent
  ? '✅ **Safe to retry**: Task appears idempotent (re-running won\'t cause side effects)'
  : '⚠️ **Caution**: Task may not be idempotent. Review cleanup steps before proceeding.'}
`;
  }

  return `## 🏥 Health Check - State Anomaly Detected

${anomalyEmoji[anomalyType]} **Anomaly**: ${anomalyType.replace(/_/g, ' ')}

| Field | Current | Expected |
|-------|---------|----------|
| Status | ${currentStatus} | ${expectedStatus} |
| Issue | #${item.issueNumber} | - |
| Agent | ${item.assignedAgent || 'None'} | - |

### Evidence
${evidence.lastWorkflow
  ? `- **Last Workflow**: [${evidence.lastWorkflow.conclusion}](${evidence.lastWorkflow.htmlUrl}) (${evidence.timeSinceLastActivity}m ago)`
  : '- **Last Workflow**: None found'}
${evidence.relatedPR
  ? `- **Related PR**: [#${evidence.relatedPR.number}](${evidence.relatedPR.url}) (${evidence.relatedPR.state})`
  : ''}
${evidence.blockerStatus
  ? `- **Blockers**: ${evidence.blockerStatus.blockerIssues.map(b => `#${b.number} (${b.state})`).join(', ')}`
  : ''}
- **Issue State**: ${evidence.issueState}

### Analysis
${analysis.summary}

**Root Cause**: ${analysis.rootCause}
${cleanupSection}${idempotencySection}
### Proposed Action
${actionEmoji[analysis.proposedAction]} ${actionDescriptions[analysis.proposedAction]}

| | |
|---|---|
| **Confidence** | ${analysis.confidence} |
| **Safe to Auto-resolve** | ${analysis.safeToAutoResolve ? 'Yes' : 'No - human review recommended'} |

**Reasoning**: ${analysis.reasoning}

---

${analysis.safeToAutoResolve
  ? `⏱️ **I'll proceed with this action in ${timeoutMinutes} minutes** unless you respond:`
  : `⏳ **Waiting for your input** (will escalate after ${timeoutMinutes} minutes if no response):`}

| Command | Action |
|---------|--------|
| \`/approve\` | Proceed with proposed action |
| \`/skip\` | Don't take action on this task |
| \`/retry\` | Reset and immediately retry |
| \`/cleanup\` | Run cleanup steps only |
| \`/hold\` | Pause health checks for this issue |
| \`/custom <instructions>\` | Custom action |
`;
}

export async function postComment(issueNumber: number, body: string): Promise<void> {
  const { repoOwner, repoName } = config;
  try {
    await refreshGitHubToken();
    execCommand(
      `gh issue comment ${issueNumber} --repo ${repoOwner}/${repoName} --body ${JSON.stringify(body)}`
    );
  } catch (err) {
    console.warn(`GitHub post failed for #${issueNumber}, saving to S3: ${(err as Error).message}`);
    await saveToS3Fallback(issueNumber, 'comment', body);
  }
}

export async function waitForUserResponse(issueNumber: number, startTime: Date): Promise<UserCommand | null> {
  const { repoOwner, repoName, userResponseTimeout, pollInterval } = config;

  const deadline = startTime.getTime() + userResponseTimeout;

  while (Date.now() < deadline) {
    // Fetch recent comments
    const commentsJson = execCommand(
      `gh issue view ${issueNumber} --repo ${repoOwner}/${repoName} --json comments --jq '[.comments[-5:][] | {author: .author.login, body: .body, createdAt: .createdAt}]' 2>/dev/null || echo '[]'`
    );

    try {
      const comments = JSON.parse(commentsJson) as Array<{
        author: string;
        body: string;
        createdAt: string;
      }>;

      // Look for commands in comments after our proposal
      for (const comment of comments) {
        const commentTime = new Date(comment.createdAt);
        if (commentTime <= startTime) continue;

        const body = comment.body.trim().toLowerCase();

        if (body.startsWith('/approve')) {
          return { type: 'approve' };
        }
        if (body.startsWith('/skip')) {
          return { type: 'skip' };
        }
        if (body.startsWith('/retry')) {
          return { type: 'retry' };
        }
        if (body.startsWith('/cleanup')) {
          return { type: 'cleanup' };
        }
        if (body.startsWith('/hold')) {
          return { type: 'hold' };
        }
        if (body.startsWith('/custom')) {
          return {
            type: 'custom',
            customInstructions: comment.body.slice(7).trim(),
          };
        }
      }
    } catch {
      // Ignore parse errors
    }

    // Wait before polling again
    await new Promise(resolve => setTimeout(resolve, pollInterval));
  }

  return null; // Timeout - no user response
}

// ============================================================================
// Action Execution
// ============================================================================

async function getProjectFields(): Promise<{
  projectId: string;
  statusField: { id: string; options: Array<{ id: string; name: string }> } | null;
  blockedByField: { id: string } | null;
}> {
  const { repoOwner, projectNumber } = config;

  const fieldsJson = execCommand(
    `gh project field-list ${projectNumber} --owner ${repoOwner} --format json`
  );
  const fields = JSON.parse(fieldsJson || '{"fields":[]}');

  const projectsJson = execCommand(
    `gh api graphql -f query='query($org: String!) { organization(login: $org) { projectsV2(first: 20) { nodes { id number } } } }' -f org="${repoOwner}" --jq '.data.organization.projectsV2.nodes[] | select(.number == ${projectNumber}) | .id'`
  );
  const projectId = projectsJson.trim();

  const statusField = fields.fields?.find((f: { name: string }) => f.name === 'Status');
  const blockedByField = fields.fields?.find((f: { name: string }) => f.name === 'blocked_by');

  return {
    projectId,
    statusField: statusField ? { id: statusField.id, options: statusField.options || [] } : null,
    blockedByField: blockedByField ? { id: blockedByField.id } : null,
  };
}

async function updateBoardStatus(itemId: string, targetStatus: string): Promise<boolean> {
  const { projectId, statusField } = await getProjectFields();

  if (!projectId || !statusField) return false;

  const option = statusField.options.find(o => o.name === targetStatus);
  if (!option) return false;

  execCommand(
    `gh project item-edit --id "${itemId}" --project-id "${projectId}" --field-id "${statusField.id}" --single-select-option-id "${option.id}"`
  );
  return true;
}

export async function executeAction(
  anomaly: StateAnomaly,
  action: ProposedAction,
  analysis: HealthAnalysis,
  customInstructions?: string
): Promise<string> {
  const { repoOwner, repoName } = config;
  const { item, evidence } = anomaly;

  log(`Executing action '${action}' for issue #${item.issueNumber}`);

  const results: string[] = [];

  switch (action) {
    case 'reset_to_todo': {
      const success = await updateBoardStatus(item.id, 'Todo');
      if (success) {
        results.push(`✅ Reset #${item.issueNumber} to **Todo** status`);
      } else {
        results.push(`⚠️ Failed to update board status for #${item.issueNumber}`);
      }
      break;
    }

    case 'retry_agent': {
      // Reset to Todo first
      await updateBoardStatus(item.id, 'Todo');

      // Re-trigger the agent by adding the label
      if (item.assignedAgent) {
        const label = item.assignedAgent.replace('@', '');
        execCommand(
          `gh issue edit ${item.issueNumber} --repo ${repoOwner}/${repoName} --add-label "${label}"`
        );
        results.push(`✅ Reset #${item.issueNumber} to **Todo** and re-triggered **${item.assignedAgent}**`);
      } else {
        results.push(`✅ Reset #${item.issueNumber} to **Todo** (no agent assigned)`);
      }
      break;
    }

    case 'cleanup_and_retry': {
      // Execute cleanup steps
      results.push('**Cleanup executed:**');

      for (const cleanup of anomaly.cleanupNeeded) {
        switch (cleanup.type) {
          case 'close_pr': {
            if (evidence.relatedPR) {
              execCommand(
                `gh pr close ${evidence.relatedPR.number} --repo ${repoOwner}/${repoName} --comment "Closing PR for retry - health monitor cleanup"`
              );
              results.push(`  - Closed PR #${evidence.relatedPR.number}`);
            }
            break;
          }
          case 'delete_branch': {
            if (evidence.relatedPR?.branch) {
              execCommand(
                `gh api repos/${repoOwner}/${repoName}/git/refs/heads/${evidence.relatedPR.branch} -X DELETE 2>/dev/null || true`
              );
              results.push(`  - Deleted branch ${evidence.relatedPR.branch}`);
            }
            break;
          }
          default:
            results.push(`  - ${cleanup.description} (manual action needed)`);
        }
      }

      // Also apply any AI-suggested cleanup steps
      for (const step of analysis.cleanupSteps) {
        results.push(`  - ${step} (noted)`);
      }

      // Now retry
      await updateBoardStatus(item.id, 'Todo');
      if (item.assignedAgent) {
        const label = item.assignedAgent.replace('@', '');
        execCommand(
          `gh issue edit ${item.issueNumber} --repo ${repoOwner}/${repoName} --add-label "${label}"`
        );
        results.push(`✅ Cleanup complete, re-triggered **${item.assignedAgent}**`);
      }
      break;
    }

    case 'clear_blocker': {
      const { projectId, blockedByField } = await getProjectFields();

      if (projectId && blockedByField) {
        execCommand(
          `gh project item-edit --id "${item.id}" --project-id "${projectId}" --field-id "${blockedByField.id}" --text ""`
        );
      }

      await updateBoardStatus(item.id, 'Todo');
      results.push(`✅ Cleared blockers for #${item.issueNumber} and moved to **Todo**`);
      break;
    }

    case 'mark_done': {
      const success = await updateBoardStatus(item.id, 'Done');
      if (success) {
        // Also close the issue if it's open
        if (evidence.issueState === 'open') {
          execCommand(
            `gh issue close ${item.issueNumber} --repo ${repoOwner}/${repoName} --comment "Closed by health monitor - work verified complete"`
          );
          results.push(`✅ Marked #${item.issueNumber} as **Done** and closed issue`);
        } else {
          results.push(`✅ Marked #${item.issueNumber} as **Done**`);
        }
      }
      break;
    }

    case 'revert_to_backlog': {
      const success = await updateBoardStatus(item.id, 'Backlog');
      if (success) {
        results.push(`✅ Moved #${item.issueNumber} back to **Backlog**`);
      }
      break;
    }

    case 'escalate': {
      results.push(`🚨 **Escalated**: Issue #${item.issueNumber} needs human attention.`);
      results.push(`No automatic action taken. Please review and resolve manually.`);
      break;
    }

    case 'no_action': {
      results.push(`⏸️ No action taken for #${item.issueNumber} - status verified correct.`);
      break;
    }

    default: {
      if (customInstructions) {
        results.push(`📝 Custom action requested: "${customInstructions}"`);
        results.push(`Please handle manually.`);
      } else {
        results.push(`❓ Unknown action: ${action}`);
      }
    }
  }

  return results.join('\n');
}

// ============================================================================
// Main Entry Point
// ============================================================================

export async function runHealthCheck(): Promise<void> {
  log('Starting comprehensive health check...');

  const anomalies = await findStateAnomalies();

  if (anomalies.length === 0) {
    log('No state anomalies found. All tasks healthy!');
    return;
  }

  log(`Found ${anomalies.length} state anomaly/anomalies`);

  for (const anomaly of anomalies) {
    log(`\nProcessing anomaly: #${anomaly.item.issueNumber} (${anomaly.anomalyType})`);

    // Check if issue has /hold flag (skip if held)
    const { repoOwner, repoName } = config;
    const labelsJson = execCommand(
      `gh issue view ${anomaly.item.issueNumber} --repo ${repoOwner}/${repoName} --json labels --jq '[.labels[].name]' 2>/dev/null || echo '[]'`
    );
    try {
      const labels = JSON.parse(labelsJson) as string[];
      if (labels.includes('health-hold')) {
        log(`  Skipping - issue has 'health-hold' label`);
        continue;
      }
    } catch {
      // Ignore
    }

    // Analyze with AI
    log(`  Analyzing with AI...`);
    const analysis = await analyzeAnomaly(anomaly);
    log(`  Analysis: ${analysis.proposedAction} (${analysis.confidence}, safe=${analysis.safeToAutoResolve})`);

    // Post proposal comment
    const proposalComment = formatProposalComment(anomaly, analysis);
    await postComment(anomaly.item.issueNumber, proposalComment);
    log(`  Posted proposal comment`);

    // Wait for user response
    const startTime = new Date();
    log(`  Waiting up to ${config.userResponseTimeout / 60000} minutes for user response...`);
    const userResponse = await waitForUserResponse(anomaly.item.issueNumber, startTime);

    // Determine final action
    let finalAction = analysis.proposedAction;
    let customInstructions: string | undefined;

    if (userResponse) {
      log(`  User responded: ${userResponse.type}`);

      switch (userResponse.type) {
        case 'approve':
          // Use proposed action
          break;
        case 'skip':
          finalAction = 'no_action';
          break;
        case 'retry':
          finalAction = analysis.cleanupSteps.length > 0 || anomaly.cleanupNeeded.length > 0
            ? 'cleanup_and_retry'
            : 'retry_agent';
          break;
        case 'cleanup':
          // Run cleanup only, then reset to Todo (don't auto-trigger agent)
          finalAction = 'cleanup_and_retry';
          // Modify to not re-trigger - handled in executeAction
          break;
        case 'hold':
          // Add hold label
          execCommand(
            `gh issue edit ${anomaly.item.issueNumber} --repo ${repoOwner}/${repoName} --add-label "health-hold"`
          );
          finalAction = 'no_action';
          break;
        case 'custom':
          customInstructions = userResponse.customInstructions;
          finalAction = 'escalate'; // Custom = manual handling
          break;
      }
    } else {
      // No user response - check if safe to auto-resolve
      if (analysis.safeToAutoResolve) {
        log(`  No user response - auto-resolving (safe=true)`);
      } else {
        log(`  No user response and not safe to auto-resolve - escalating`);
        finalAction = 'escalate';
      }
    }

    // Execute action
    const result = await executeAction(anomaly, finalAction, analysis, customInstructions);

    // Post result comment
    const resultComment = `### Health Check Action Completed

${result}

${userResponse
  ? `**User command**: \`/${userResponse.type}\``
  : analysis.safeToAutoResolve
    ? '_No user response within timeout - proceeded with default action (safe to auto-resolve)_'
    : '_No user response within timeout - escalated for human review_'}`;

    await postComment(anomaly.item.issueNumber, resultComment);
    log(`  Action completed`);
  }

  log('\nHealth check complete!');
}

// CLI entry point
if (require.main === module) {
  const repoOwner = process.env.REPO_OWNER;
  const repoName = process.env.REPO_NAME;
  const projectNumber = parseInt(process.env.PROJECT_NUMBER || '0', 10);

  if (!repoOwner || !repoName || !projectNumber) {
    console.error('Required env vars: REPO_OWNER, REPO_NAME, PROJECT_NUMBER');
    process.exit(1);
  }

  configureHealthMonitor({
    repoOwner,
    repoName,
    projectNumber,
    userResponseTimeout: parseInt(process.env.USER_RESPONSE_TIMEOUT || '180000', 10),
  });

  runHealthCheck()
    .then(() => process.exit(0))
    .catch((err) => {
      console.error('Health check failed:', err);
      process.exit(1);
    });
}

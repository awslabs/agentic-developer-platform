/**
 * Reassessment Module for @agent-pm
 *
 * This module provides re-assessment capabilities when PM is triggered
 * on an issue that already has progress (comments, child issues, project board items).
 *
 * Designed to be modular and isolatable - can be disabled without affecting core PM flow.
 */

import * as fs from 'fs';

// ============================================================================
// Types
// ============================================================================

export interface IssueComment {
  author: string;
  body: string;
  createdAt: string;
  authorAssociation: string;
}

export interface ChildIssue {
  number: number;
  title: string;
  state: 'OPEN' | 'CLOSED';
  labels: string[];
  assignees: string[];
}

export interface ProjectBoardItem {
  issueNumber: number;
  title: string;
  status: string | null;
  assignedAgent: string | null;
  blockedBy: string | null;
  workflowRun: string | null;
}

export interface AIDLCArtifacts {
  epics: { number: number; title: string }[];
  stories: { number: number; title: string }[];
  units: { number: number; title: string }[];
  projectBoard: { number: number; url: string } | null;
  prsCreated: { number: number; title: string }[];
  prsMerged: { number: number; title: string }[];
}

export interface AIDLCStateInfo {
  waitingForUser: boolean;
  phase: string;
  stage: string;
  currentPlanFile: string | null;
  branch: string | null;
  pmRecommendations: string | null;  // PM's recommendations summary from plan file
  artifacts: AIDLCArtifacts | null;  // What has been created so far
}

export interface ReassessmentContext {
  hasExistingProgress: boolean;
  comments: IssueComment[];
  childIssues: ChildIssue[];
  projectBoardItems: ProjectBoardItem[];
  workflowRuns: WorkflowRun[];
  statusDiscrepancies: StatusDiscrepancy[];
  analysis: StateAnalysis;
  aidlcState: AIDLCStateInfo | null;
}

export interface StateAnalysis {
  phase: 'not_started' | 'inception' | 'construction' | 'operations' | 'unknown';
  completedTasks: number[];
  failedTasks: number[];
  blockedTasks: number[];
  inProgressTasks: number[];
  pendingTasks: number[];
  agentFailures: AgentFailure[];
  staleBlockers: Array<{ issueNumber: number; doneBlockers: number[] }>;
  recommendations: string[];
}

export interface AgentFailure {
  issueNumber: number;
  agent: string;
  reason: string;
}

export interface WorkflowRun {
  issueNumber: number;
  workflowName: string;
  conclusion: 'success' | 'failure' | 'cancelled' | 'skipped' | null;
  status: string;
  runId: number;
  runUrl: string;
}

export interface StatusDiscrepancy {
  issueNumber: number;
  boardStatus: string | null;
  actualStatus: 'success' | 'failure' | 'cancelled' | 'skipped' | 'no_runs';
  workflowConclusion: string | null;
  hasFailedLabel: boolean;
}

export interface ReassessmentConfig {
  enabled: boolean;
  maxCommentsToFetch: number;
  maxChildIssuesToFetch: number;
}

// ============================================================================
// Configuration
// ============================================================================

const DEFAULT_CONFIG: ReassessmentConfig = {
  enabled: true,
  maxCommentsToFetch: 50,
  maxChildIssuesToFetch: 100,
};

let config: ReassessmentConfig = { ...DEFAULT_CONFIG };

export function configureReassessment(newConfig: Partial<ReassessmentConfig>): void {
  config = { ...config, ...newConfig };
}

export function isReassessmentEnabled(): boolean {
  return config.enabled;
}

// ============================================================================
// Workflow Progress Builder (used in both main flow and reassessment)
// ============================================================================

/**
 * Builds a comprehensive workflow progress display showing:
 * 1. Current stage in AIDLC workflow
 * 2. Artifacts created (Epics, Stories, Units, Project Board, PRs)
 */
export function buildWorkflowProgress(
  aidlcState: AIDLCStateInfo,
  repoOwner: string,
  repoName: string
): string {
  const { phase, stage, artifacts } = aidlcState;

  // Stage definitions
  const stages = {
    inception: [
      { id: 'requirements', label: 'Requirements Analysis' },
      { id: 'stories', label: 'User Stories' },
      { id: 'design', label: 'Application Design' },
      { id: 'units', label: 'Units Generation' },
    ],
    construction: [
      { id: 'functional', label: 'Functional Design' },
      { id: 'code', label: 'Code Generation' },
      { id: 'review', label: 'Code Review' },
      { id: 'test', label: 'Build & Test' },
    ],
    operations: [
      { id: 'deploy', label: 'Deployment' },
      { id: 'monitor', label: 'Monitoring' },
    ],
  };

  const allPhases = ['inception', 'construction', 'operations'] as const;
  const currentPhaseIndex = allPhases.indexOf(phase.toLowerCase() as typeof allPhases[number]);

  // Build stage progress
  let stageProgress = '### 📍 Workflow Progress\n\n';

  for (let pi = 0; pi < allPhases.length; pi++) {
    const phaseName = allPhases[pi];
    const phaseStages = stages[phaseName];
    const isCurrentPhase = pi === currentPhaseIndex;
    const isPastPhase = pi < currentPhaseIndex;

    // Phase header with icon
    const phaseIcon = isPastPhase ? '✅' : isCurrentPhase ? '🔄' : '⏳';
    const phaseLabel = phaseName.charAt(0).toUpperCase() + phaseName.slice(1);
    stageProgress += `**${phaseIcon} ${phaseLabel}**\n`;

    // Stages within phase
    for (const s of phaseStages) {
      const isCurrentStage = isCurrentPhase && stage.toLowerCase().includes(s.id);
      const stageIndex = phaseStages.findIndex(x => stage.toLowerCase().includes(x.id));
      const thisIndex = phaseStages.indexOf(s);
      const isPastStage = isPastPhase || (isCurrentPhase && stageIndex > thisIndex);

      let icon = '○';  // pending
      if (isPastStage) icon = '●';  // done
      if (isCurrentStage) icon = '◉';  // current

      const pointer = isCurrentStage ? ' **← Current**' : '';
      stageProgress += `  ${icon} ${s.label}${pointer}\n`;
    }
    stageProgress += '\n';
  }

  // Build artifacts section
  let artifactsSection = '### 📦 Artifacts Created\n\n';

  if (!artifacts) {
    artifactsSection += '_No artifacts tracked yet_\n\n';
  } else {
    const formatIssueList = (items: { number: number; title: string }[], type: string) => {
      if (items.length === 0) return `- [ ] ${type}: _None yet_\n`;
      const links = items.map(i =>
        `[#${i.number}](https://github.com/${repoOwner}/${repoName}/issues/${i.number})`
      ).join(', ');
      return `- [x] **${type}** (${items.length}): ${links}\n`;
    };

    // Project board
    if (artifacts.projectBoard) {
      artifactsSection += `- [x] **Project Board**: [#${artifacts.projectBoard.number}](${artifacts.projectBoard.url})\n`;
    } else {
      artifactsSection += `- [ ] **Project Board**: _Not created_\n`;
    }

    // Issues by type
    artifactsSection += formatIssueList(artifacts.epics, 'Epics');
    artifactsSection += formatIssueList(artifacts.stories, 'Stories');
    artifactsSection += formatIssueList(artifacts.units, 'Units/Tasks');

    // PRs
    if (artifacts.prsMerged.length > 0) {
      const prLinks = artifacts.prsMerged.map(p =>
        `[#${p.number}](https://github.com/${repoOwner}/${repoName}/pull/${p.number})`
      ).join(', ');
      artifactsSection += `- [x] **PRs Merged** (${artifacts.prsMerged.length}): ${prLinks}\n`;
    }
    if (artifacts.prsCreated.length > 0) {
      const prLinks = artifacts.prsCreated.map(p =>
        `[#${p.number}](https://github.com/${repoOwner}/${repoName}/pull/${p.number})`
      ).join(', ');
      artifactsSection += `- [ ] **PRs In Progress** (${artifacts.prsCreated.length}): ${prLinks}\n`;
    }
    if (artifacts.prsCreated.length === 0 && artifacts.prsMerged.length === 0) {
      artifactsSection += `- [ ] **PRs**: _None yet_\n`;
    }
  }

  return stageProgress + artifactsSection;
}

// ============================================================================
// Data Fetching Functions
// ============================================================================

type ExecCommandFn = (command: string, useAppToken?: boolean) => Promise<string>;

export async function fetchIssueComments(
  issueNumber: string,
  execCommand: ExecCommandFn
): Promise<IssueComment[]> {
  try {
    const json = await execCommand(
      `gh issue view ${issueNumber} --json comments --jq '.comments'`
    );

    const comments = JSON.parse(json || '[]');
    return comments.slice(-config.maxCommentsToFetch).map((c: Record<string, unknown>) => ({
      author: (c.author as Record<string, string>)?.login || 'unknown',
      body: (c.body as string) || '',
      createdAt: (c.createdAt as string) || '',
      authorAssociation: (c.authorAssociation as string) || 'NONE',
    }));
  } catch (error) {
    console.warn('[reassessment] Failed to fetch comments:', (error as Error).message);
    return [];
  }
}

export async function findChildIssues(
  parentIssueNumber: string,
  repoOwner: string,
  repoName: string,
  execCommand: ExecCommandFn
): Promise<ChildIssue[]> {
  // Use GitHub's native sub-issues GraphQL field. This is the authoritative
  // parent-child relationship. An empty result means the parent has no
  // sub-issues — that is the correct answer, not a signal to text-search.
  //
  // The previous implementation fell through to a "body mentions #N" text
  // search on empty results, which returned false-positive "children" (any
  // issue that referenced the parent as a dependency, in documentation, or
  // in a comment). That produced misattributed task completion counts — see
  // the regression test in agent-pm-sub-issues.test.ts.
  const subIssuesQuery = `query { repository(owner: "${repoOwner}", name: "${repoName}") { issue(number: ${parentIssueNumber}) { subIssues(first: ${config.maxChildIssuesToFetch}) { nodes { number title state labels(first: 10) { nodes { name } } assignees(first: 5) { nodes { login } } } } } } }`;

  try {
    const subIssuesJson = await execCommand(
      `gh api graphql -f query='${subIssuesQuery}' --jq '.data.repository.issue.subIssues.nodes'`
    );
    const subIssues = JSON.parse(subIssuesJson || '[]');
    console.log(`[reassessment] Native sub-issues API returned ${subIssues.length} sub-issues for #${parentIssueNumber}`);
    return subIssues.map((i: Record<string, unknown>) => ({
      number: i.number as number,
      title: i.title as string,
      state: (i.state as string).toUpperCase() as 'OPEN' | 'CLOSED',
      labels: ((i.labels as { nodes: Array<{ name: string }> })?.nodes || []).map(l => l.name),
      assignees: ((i.assignees as { nodes: Array<{ login: string }> })?.nodes || []).map(a => a.login),
    }));
  } catch (error) {
    console.warn(`[reassessment] Failed to fetch sub-issues for #${parentIssueNumber}:`, (error as Error).message);
    return [];
  }
}

export async function getProjectBoardItems(
  projectNumber: number,
  repoOwner: string,
  execCommand: ExecCommandFn
): Promise<ProjectBoardItem[]> {
  try {
    // Get project items with their field values
    const json = await execCommand(
      `gh project item-list ${projectNumber} --owner ${repoOwner} --format json --limit 100`
    );

    const data = JSON.parse(json || '{"items":[]}');
    const items = data.items || [];

    return items
      .filter((item: Record<string, unknown>) => item.content && (item.content as Record<string, unknown>).number)
      .map((item: Record<string, unknown>) => {
        const content = item.content as Record<string, unknown>;
        return {
          issueNumber: content.number as number,
          title: content.title as string,
          status: (item.status as string) || null,
          assignedAgent: (item['assigned_agent'] as string) || (item['assignedAgent'] as string) || null,
          blockedBy: (item['blocked_by'] as string) || (item['blockedBy'] as string) || null,
          workflowRun: (item['workflow_run'] as string) || (item['workflowRun'] as string) || null,
        };
      });
  } catch (error) {
    console.warn('[reassessment] Failed to get project board items:', (error as Error).message);
    return [];
  }
}

export async function getWorkflowRunsForIssues(
  issueNumbers: number[],
  repoOwner: string,
  repoName: string,
  execCommand: ExecCommandFn
): Promise<WorkflowRun[]> {
  const runs: WorkflowRun[] = [];

  try {
    // Get recent workflow runs for agent workflows
    // Support both target repo pattern (call-agent-*.yml) and ADP pattern (agent-*.yml)
    const agentWorkflows = [
      { type: 'developer', files: ['call-agent-developer.yml', 'agent-developer.yml'] },
      { type: 'architect', files: ['call-agent-architect.yml', 'agent-architect.yml'] },
      { type: 'operations', files: ['call-agent-operations.yml', 'agent-operations.yml'] },
      { type: 'reviewer', files: ['call-agent-reviewer.yml', 'agent-reviewer.yml'] },
      { type: 'product', files: ['call-agent-product.yml', 'agent-product.yml'] },
    ];

    for (const { type, files } of agentWorkflows) {
      // Try each workflow file pattern
      for (const workflowFile of files) {
        try {
          const json = await execCommand(
            `gh run list --workflow ${workflowFile} --limit 50 --json databaseId,conclusion,status,displayTitle,url --repo ${repoOwner}/${repoName}`
          );

          const workflowRuns = JSON.parse(json || '[]');
          if (workflowRuns.length === 0) continue; // Try next pattern

          for (const run of workflowRuns) {
            // Try to extract issue number from title
            const titleMatch = (run.displayTitle as string)?.match(/#(\d+)|Issue\s+(\d+)/i);
            const issueNum = titleMatch ? parseInt(titleMatch[1] || titleMatch[2]) : null;

            if (issueNum && issueNumbers.includes(issueNum)) {
              runs.push({
                issueNumber: issueNum,
                workflowName: type,
                conclusion: run.conclusion as WorkflowRun['conclusion'],
                status: run.status as string,
                runId: run.databaseId as number,
                runUrl: run.url as string,
              });
            }
          }
          break; // Found runs with this pattern, don't try the next
        } catch {
          // Try next pattern
          continue;
        }
      }
    }
  } catch (error) {
    console.warn('[reassessment] Failed to get workflow runs:', (error as Error).message);
  }

  return runs;
}

export function detectStatusDiscrepancies(
  childIssues: ChildIssue[],
  projectBoardItems: ProjectBoardItem[],
  workflowRuns: WorkflowRun[]
): StatusDiscrepancy[] {
  const discrepancies: StatusDiscrepancy[] = [];

  for (const issue of childIssues) {
    const boardItem = projectBoardItems.find(b => b.issueNumber === issue.number);
    const boardStatus = boardItem?.status || null;
    const hasFailedLabel = issue.labels.includes('agent-failed');

    // Get the most recent workflow run for this issue
    const issueRuns = workflowRuns
      .filter(r => r.issueNumber === issue.number)
      .sort((a, b) => b.runId - a.runId); // Most recent first

    const latestRun = issueRuns[0];
    const actualConclusion = latestRun?.conclusion || null;

    // Determine actual status
    let actualStatus: StatusDiscrepancy['actualStatus'] = 'no_runs';
    if (latestRun) {
      actualStatus = latestRun.conclusion || 'no_runs';
    }

    // Detect discrepancies
    const isDiscrepancy =
      // Board says Done but workflow failed
      (boardStatus === 'Done' && (actualStatus === 'failure' || actualStatus === 'cancelled' || hasFailedLabel)) ||
      // Board says Done but has agent-failed label
      (boardStatus === 'Done' && hasFailedLabel) ||
      // Board says In Progress but workflow completed (success or failure)
      (boardStatus === 'In Progress' && actualStatus === 'success') ||
      (boardStatus === 'In Progress' && actualStatus === 'failure') ||
      // Has failed label but board doesn't reflect it
      (hasFailedLabel && boardStatus !== 'Todo' && boardStatus !== 'Backlog');

    if (isDiscrepancy) {
      discrepancies.push({
        issueNumber: issue.number,
        boardStatus,
        actualStatus,
        workflowConclusion: actualConclusion,
        hasFailedLabel,
      });
    }
  }

  return discrepancies;
}

// ============================================================================
// Analysis Functions
// ============================================================================

export function analyzeComments(comments: IssueComment[]): {
  agentActivities: Array<{ agent: string; action: string; timestamp: string }>;
  userApprovals: string[];
  failures: string[];
} {
  const agentActivities: Array<{ agent: string; action: string; timestamp: string }> = [];
  const userApprovals: string[] = [];
  const failures: string[] = [];

  for (const comment of comments) {
    const body = comment.body.toLowerCase();

    // Detect agent activities
    const agentMatch = comment.body.match(/@agent-(\w+)\s+(Started|Complete|Failed|Timeout)/i);
    if (agentMatch) {
      agentActivities.push({
        agent: `@agent-${agentMatch[1]}`,
        action: agentMatch[2],
        timestamp: comment.createdAt,
      });
    }

    // Detect failures
    if (body.includes('failed') || body.includes('error') || body.includes('timeout')) {
      failures.push(comment.body.substring(0, 200));
    }

    // Detect user approvals
    if (comment.authorAssociation === 'MEMBER' || comment.authorAssociation === 'OWNER') {
      if (body.includes('approve') || body.includes('lgtm') || body.includes('continue')) {
        userApprovals.push(comment.body.substring(0, 100));
      }
    }
  }

  return { agentActivities, userApprovals, failures };
}

export function analyzeChildIssues(childIssues: ChildIssue[]): {
  byType: Record<string, ChildIssue[]>;
  byStatus: Record<string, ChildIssue[]>;
  failed: ChildIssue[];
  withAgentLabels: ChildIssue[];
} {
  const byType: Record<string, ChildIssue[]> = {
    epic: [],
    story: [],
    unit: [],
    task: [],
    spike: [],
    other: [],
  };

  const byStatus: Record<string, ChildIssue[]> = {
    open: [],
    closed: [],
  };

  const failed: ChildIssue[] = [];
  const withAgentLabels: ChildIssue[] = [];

  for (const issue of childIssues) {
    // Categorize by type
    const typeLabel = issue.labels.find(l =>
      ['epic', 'story', 'unit', 'task', 'spike'].includes(l.toLowerCase())
    );
    if (typeLabel) {
      byType[typeLabel.toLowerCase()].push(issue);
    } else {
      byType.other.push(issue);
    }

    // Categorize by status
    byStatus[issue.state.toLowerCase()].push(issue);

    // Check for failures
    if (issue.labels.includes('agent-failed')) {
      failed.push(issue);
    }

    // Check for agent labels
    const agentLabel = issue.labels.find(l => l.startsWith('agent-'));
    if (agentLabel) {
      withAgentLabels.push(issue);
    }
  }

  return { byType, byStatus, failed, withAgentLabels };
}

export function analyzeProjectBoard(items: ProjectBoardItem[]): {
  byStatus: Record<string, ProjectBoardItem[]>;
  blocked: ProjectBoardItem[];
  stale: ProjectBoardItem[];
  staleBlockers: Array<{ item: ProjectBoardItem; doneBlockers: number[] }>;
} {
  const byStatus: Record<string, ProjectBoardItem[]> = {
    'Backlog': [],
    'Todo': [],
    'In Progress': [],
    'Review': [],
    'Done': [],
    'unknown': [],
  };

  const blocked: ProjectBoardItem[] = [];
  const stale: ProjectBoardItem[] = [];
  const staleBlockers: Array<{ item: ProjectBoardItem; doneBlockers: number[] }> = [];

  // Build a map of issue number -> status for quick lookup
  const issueStatusMap = new Map<number, string>();
  for (const item of items) {
    issueStatusMap.set(item.issueNumber, item.status || 'unknown');
  }

  for (const item of items) {
    // Categorize by status
    const status = item.status || 'unknown';
    if (byStatus[status]) {
      byStatus[status].push(item);
    } else {
      byStatus['unknown'].push(item);
    }

    // Check for blocked items - but validate blockers are still active
    if (item.blockedBy && item.blockedBy.trim() && item.blockedBy !== 'none') {
      // Parse blocker issue numbers (e.g., "#263, #264" or "#263")
      const blockerMatches = item.blockedBy.match(/#(\d+)/g) || [];
      const blockerNumbers = blockerMatches.map(m => parseInt(m.slice(1), 10));

      // Check which blockers are actually done
      const doneBlockers = blockerNumbers.filter(num => {
        const blockerStatus = issueStatusMap.get(num);
        return blockerStatus === 'Done';
      });

      const activeBlockers = blockerNumbers.filter(num => {
        const blockerStatus = issueStatusMap.get(num);
        return blockerStatus !== 'Done';
      });

      // Only consider truly blocked if there are active (non-done) blockers
      if (activeBlockers.length > 0) {
        blocked.push(item);
      }

      // Track stale blockers (item has blockers that are already done)
      if (doneBlockers.length > 0) {
        staleBlockers.push({ item, doneBlockers });
      }
    }

    // Check for stale items (in progress but no workflow run)
    if (item.status === 'In Progress' && !item.workflowRun) {
      stale.push(item);
    }
  }

  return { byStatus, blocked, stale, staleBlockers };
}

// ============================================================================
// Main Analysis Function
// ============================================================================

export function buildStateAnalysis(
  comments: IssueComment[],
  childIssues: ChildIssue[],
  projectBoardItems: ProjectBoardItem[]
): StateAnalysis {
  const commentAnalysis = analyzeComments(comments);
  const issueAnalysis = analyzeChildIssues(childIssues);

  // Scope project-board items to this EPIC's actual child issues.
  // `getProjectBoardItems` returns the full project, which can contain items
  // from many EPICs. Without this filter, reassessing EPIC #A would count
  // #B's completed items as #A's "completedTasks" — misattributing work.
  const childNumbers = new Set(childIssues.map(c => c.number));
  const scopedBoardItems = childNumbers.size === 0
    ? [] // no children → no board items belong to this EPIC
    : projectBoardItems.filter(item => childNumbers.has(item.issueNumber));
  const boardAnalysis = analyzeProjectBoard(scopedBoardItems);

  // Determine current phase
  let phase: StateAnalysis['phase'] = 'not_started';
  const phaseLabels = childIssues.flatMap(i => i.labels).filter(l => l.startsWith('phase:'));
  if (phaseLabels.includes('phase:operations')) {
    phase = 'operations';
  } else if (phaseLabels.includes('phase:construction')) {
    phase = 'construction';
  } else if (phaseLabels.includes('phase:inception')) {
    phase = 'inception';
  } else if (childIssues.length > 0 || scopedBoardItems.length > 0) {
    phase = 'unknown';
  }

  // Categorize tasks by status
  const completedTasks = boardAnalysis.byStatus['Done'].map(i => i.issueNumber);
  const inProgressTasks = boardAnalysis.byStatus['In Progress'].map(i => i.issueNumber);
  const pendingTasks = [
    ...boardAnalysis.byStatus['Backlog'],
    ...boardAnalysis.byStatus['Todo'],
  ].map(i => i.issueNumber);
  const blockedTasks = boardAnalysis.blocked.map(i => i.issueNumber);
  const failedTasks = issueAnalysis.failed.map(i => i.number);

  // Build agent failure details
  const agentFailures: AgentFailure[] = issueAnalysis.failed.map(issue => {
    const agentLabel = issue.labels.find(l => l.startsWith('agent-') && l !== 'agent-failed');
    return {
      issueNumber: issue.number,
      agent: agentLabel || 'unknown',
      reason: 'Check workflow logs for details',
    };
  });

  // Generate recommendations
  const recommendations: string[] = [];

  if (failedTasks.length > 0) {
    recommendations.push(`Re-trigger ${failedTasks.length} failed task(s): #${failedTasks.join(', #')}`);
  }

  if (blockedTasks.length > 0) {
    recommendations.push(`Review ${blockedTasks.length} blocked task(s) - dependencies may need updating`);
  }

  // Check for stale blockers (items blocked by already-done issues)
  if (boardAnalysis.staleBlockers.length > 0) {
    const staleDetails = boardAnalysis.staleBlockers.map(sb =>
      `#${sb.item.issueNumber} (blocked by done: ${sb.doneBlockers.map(n => `#${n}`).join(', ')})`
    ).join(', ');
    recommendations.push(`Clear stale blockers on ${boardAnalysis.staleBlockers.length} task(s): ${staleDetails}`);
  }

  if (boardAnalysis.stale.length > 0) {
    recommendations.push(`Check ${boardAnalysis.stale.length} stale task(s) marked "In Progress" without active workflow`);
  }

  // Check for issues with agent labels but not triggered
  const issuesWithAgentLabels = issueAnalysis.withAgentLabels.filter(i => i.state === 'OPEN');
  if (issuesWithAgentLabels.length > 0) {
    recommendations.push(`${issuesWithAgentLabels.length} issue(s) have agent labels - may need re-triggering`);
  }

  if (pendingTasks.length > 0 && inProgressTasks.length === 0 && failedTasks.length === 0) {
    recommendations.push(`${pendingTasks.length} task(s) pending - ready to assign agents`);
  }

  if (recommendations.length === 0) {
    if (completedTasks.length > 0 && pendingTasks.length === 0) {
      recommendations.push('All tasks appear complete - verify and close parent issue');
    } else {
      recommendations.push('No specific actions identified - review current state');
    }
  }

  // Extract stale blockers info
  const staleBlockers = boardAnalysis.staleBlockers.map(sb => ({
    issueNumber: sb.item.issueNumber,
    doneBlockers: sb.doneBlockers,
  }));

  return {
    phase,
    completedTasks,
    failedTasks,
    blockedTasks,
    inProgressTasks,
    pendingTasks,
    agentFailures,
    staleBlockers,
    recommendations,
  };
}

/**
 * Enhanced state analysis that cross-checks board status against actual workflow runs
 */
export function buildStateAnalysisWithDiscrepancies(
  comments: IssueComment[],
  childIssues: ChildIssue[],
  projectBoardItems: ProjectBoardItem[],
  workflowRuns: WorkflowRun[],
  statusDiscrepancies: StatusDiscrepancy[]
): StateAnalysis {
  // Start with basic analysis
  const basicAnalysis = buildStateAnalysis(comments, childIssues, projectBoardItems);

  // Override task categorization based on actual workflow results
  const trueFailedTasks = new Set<number>();
  const trueCompletedTasks = new Set<number>();
  const trueInProgressTasks = new Set<number>();

  // First, trust workflow run results over board status
  for (const issue of childIssues) {
    const issueRuns = workflowRuns
      .filter(r => r.issueNumber === issue.number)
      .sort((a, b) => b.runId - a.runId);

    const latestRun = issueRuns[0];
    const hasFailedLabel = issue.labels.includes('agent-failed');

    // If has agent-failed label, it's failed regardless of board status
    if (hasFailedLabel) {
      trueFailedTasks.add(issue.number);
      continue;
    }

    // If latest workflow run failed, it's failed
    if (latestRun?.conclusion === 'failure' || latestRun?.conclusion === 'cancelled') {
      trueFailedTasks.add(issue.number);
      continue;
    }

    // If latest workflow run succeeded, it's completed
    if (latestRun?.conclusion === 'success') {
      trueCompletedTasks.add(issue.number);
      continue;
    }

    // If workflow is still running
    if (latestRun?.status === 'in_progress' || latestRun?.status === 'queued') {
      trueInProgressTasks.add(issue.number);
    }
  }

  // Update the analysis with corrected data
  const correctedCompletedTasks = basicAnalysis.completedTasks.filter(n =>
    !trueFailedTasks.has(n) || trueCompletedTasks.has(n)
  );
  const correctedFailedTasks = Array.from(new Set([
    ...basicAnalysis.failedTasks,
    ...Array.from(trueFailedTasks)
  ]));
  const correctedInProgressTasks = Array.from(new Set([
    ...basicAnalysis.inProgressTasks.filter(n => !trueFailedTasks.has(n) && !trueCompletedTasks.has(n)),
    ...Array.from(trueInProgressTasks)
  ]));

  // Build enhanced agent failures with workflow details
  const enhancedAgentFailures: AgentFailure[] = [];
  for (const issueNum of correctedFailedTasks) {
    const issue = childIssues.find(i => i.number === issueNum);
    const latestRun = workflowRuns
      .filter(r => r.issueNumber === issueNum)
      .sort((a, b) => b.runId - a.runId)[0];

    const agentLabel = issue?.labels.find(l => l.startsWith('agent-') && l !== 'agent-failed') || 'unknown';

    enhancedAgentFailures.push({
      issueNumber: issueNum,
      agent: agentLabel,
      reason: latestRun
        ? `Workflow ${latestRun.conclusion} - ${latestRun.runUrl}`
        : issue?.labels.includes('agent-failed')
          ? 'Has agent-failed label'
          : 'Unknown failure reason',
    });
  }

  // Build enhanced recommendations
  const recommendations: string[] = [];

  // Add discrepancy warning first if any
  if (statusDiscrepancies.length > 0) {
    recommendations.push(
      `**STATUS DISCREPANCY**: ${statusDiscrepancies.length} task(s) have incorrect board status vs actual workflow results`
    );
  }

  if (correctedFailedTasks.length > 0) {
    recommendations.push(`Re-trigger ${correctedFailedTasks.length} failed task(s): #${correctedFailedTasks.join(', #')}`);
  }

  if (basicAnalysis.blockedTasks.length > 0) {
    recommendations.push(`Review ${basicAnalysis.blockedTasks.length} blocked task(s) - dependencies may need updating`);
  }

  // Check for issues with agent labels that might need re-triggering
  const issuesWithAgentLabels = childIssues.filter(i =>
    i.state === 'OPEN' &&
    i.labels.some(l => l.startsWith('agent-') && l !== 'agent-failed')
  );
  if (issuesWithAgentLabels.length > 0) {
    recommendations.push(`${issuesWithAgentLabels.length} issue(s) have agent labels - may need re-triggering`);
  }

  if (basicAnalysis.pendingTasks.length > 0 && correctedInProgressTasks.length === 0 && correctedFailedTasks.length === 0) {
    recommendations.push(`${basicAnalysis.pendingTasks.length} task(s) pending - ready to assign agents`);
  }

  if (recommendations.length === 0) {
    if (correctedCompletedTasks.length > 0 && basicAnalysis.pendingTasks.length === 0) {
      recommendations.push('All tasks appear complete - verify and close parent issue');
    } else {
      recommendations.push('No specific actions identified - review current state');
    }
  }

  return {
    phase: basicAnalysis.phase,
    completedTasks: correctedCompletedTasks,
    failedTasks: correctedFailedTasks,
    blockedTasks: basicAnalysis.blockedTasks,
    inProgressTasks: correctedInProgressTasks,
    pendingTasks: basicAnalysis.pendingTasks,
    agentFailures: enhancedAgentFailures,
    staleBlockers: basicAnalysis.staleBlockers,
    recommendations,
  };
}

// ============================================================================
// Main Entry Point
// ============================================================================

export async function gatherReassessmentContext(
  issueNumber: string,
  repoOwner: string,
  repoName: string,
  projectNumber: number | null,
  execCommand: ExecCommandFn,
  aidlcState?: AIDLCStateInfo | null
): Promise<ReassessmentContext> {
  console.log('[reassessment] Gathering context for re-assessment...');

  // Fetch basic data in parallel
  const [comments, childIssues, projectBoardItems] = await Promise.all([
    fetchIssueComments(issueNumber, execCommand),
    findChildIssues(issueNumber, repoOwner, repoName, execCommand),
    projectNumber
      ? getProjectBoardItems(projectNumber, repoOwner, execCommand)
      : Promise.resolve([]),
  ]);

  console.log(`[reassessment] Found: ${comments.length} comments, ${childIssues.length} child issues, ${projectBoardItems.length} board items`);

  // Fetch workflow runs for all child issues (thorough check)
  const childIssueNumbers = childIssues.map(i => i.number);
  const workflowRuns = await getWorkflowRunsForIssues(
    childIssueNumbers,
    repoOwner,
    repoName,
    execCommand
  );

  console.log(`[reassessment] Found ${workflowRuns.length} workflow runs for child issues`);

  // Detect discrepancies between board status and actual workflow results
  const statusDiscrepancies = detectStatusDiscrepancies(childIssues, projectBoardItems, workflowRuns);

  if (statusDiscrepancies.length > 0) {
    console.log(`[reassessment] WARNING: ${statusDiscrepancies.length} status discrepancies detected!`);
    for (const d of statusDiscrepancies) {
      console.log(`  - #${d.issueNumber}: Board="${d.boardStatus}" vs Actual="${d.actualStatus}" (hasFailedLabel=${d.hasFailedLabel})`);
    }
  }

  // Determine if there's existing progress.
  // Scope projectBoardItems to this EPIC's own child issues — otherwise any
  // item on the shared project board (from any other EPIC) would register
  // as "existing progress" on this EPIC.
  const childNumbersForProgress = new Set(childIssues.map(c => c.number));
  const scopedBoardItemsForProgress = childNumbersForProgress.size === 0
    ? []
    : projectBoardItems.filter(i => childNumbersForProgress.has(i.issueNumber));
  const hasExistingProgress =
    comments.length > 2 || // More than just initial comments
    childIssues.length > 0 ||
    scopedBoardItemsForProgress.length > 0;

  // Build analysis (now includes discrepancy info)
  const analysis = buildStateAnalysisWithDiscrepancies(
    comments,
    childIssues,
    projectBoardItems,
    workflowRuns,
    statusDiscrepancies
  );

  return {
    hasExistingProgress,
    comments,
    childIssues,
    projectBoardItems,
    workflowRuns,
    statusDiscrepancies,
    analysis,
    aidlcState: aidlcState || null,
  };
}

// ============================================================================
// Prompt Builder
// ============================================================================

export function buildReassessPrompt(
  issueNumber: string,
  issueTitle: string,
  issueBody: string,
  context: ReassessmentContext,
  rules: string
): string {
  const { analysis, comments, childIssues, projectBoardItems } = context;

  // Build comment summary
  const recentComments = comments.slice(-10).map(c =>
    `**${c.author}** (${c.createdAt}):\n${c.body.substring(0, 300)}${c.body.length > 300 ? '...' : ''}`
  ).join('\n\n---\n\n');

  // Build child issues summary
  const childIssuesSummary = childIssues.map(i =>
    `- #${i.number}: ${i.title} [${i.state}] Labels: ${i.labels.join(', ')}`
  ).join('\n');

  // Build board status summary — scoped to this EPIC's child issues only.
  // Without scoping, the prompt would include every item in the entire
  // project board, including items belonging to other EPICs.
  const childNumbers = new Set(childIssues.map(c => c.number));
  const scopedBoardItems = childNumbers.size === 0
    ? []
    : projectBoardItems.filter(item => childNumbers.has(item.issueNumber));
  const boardSummary = scopedBoardItems.map(i =>
    `- #${i.issueNumber}: ${i.title} | Status: ${i.status || 'N/A'} | Agent: ${i.assignedAgent || 'N/A'} | Blocked: ${i.blockedBy || 'None'}`
  ).join('\n');

  return `You are @agent-pm, the AIDLC Workflow Orchestrator.

## RE-ASSESSMENT MODE

This issue already has existing progress. You need to analyze what happened and determine the best path forward.

## Issue #${issueNumber}: ${issueTitle}

${issueBody}

---

## CURRENT STATE ANALYSIS

### Phase: ${analysis.phase.toUpperCase()}

### Task Summary
| Category | Count | Issues |
|----------|-------|--------|
| Completed | ${analysis.completedTasks.length} | ${analysis.completedTasks.length > 0 ? '#' + analysis.completedTasks.join(', #') : 'None'} |
| In Progress | ${analysis.inProgressTasks.length} | ${analysis.inProgressTasks.length > 0 ? '#' + analysis.inProgressTasks.join(', #') : 'None'} |
| Failed | ${analysis.failedTasks.length} | ${analysis.failedTasks.length > 0 ? '#' + analysis.failedTasks.join(', #') : 'None'} |
| Blocked | ${analysis.blockedTasks.length} | ${analysis.blockedTasks.length > 0 ? '#' + analysis.blockedTasks.join(', #') : 'None'} |
| Pending | ${analysis.pendingTasks.length} | ${analysis.pendingTasks.length > 0 ? '#' + analysis.pendingTasks.join(', #') : 'None'} |

### Agent Failures
${analysis.agentFailures.length > 0
  ? analysis.agentFailures.map(f => `- #${f.issueNumber}: ${f.agent} - ${f.reason}`).join('\n')
  : 'No agent failures detected'}

### System Recommendations
${analysis.recommendations.map(r => `- ${r}`).join('\n')}

---

## RECENT COMMENTS (Last 10)

${recentComments || 'No comments found'}

---

## CHILD ISSUES (${childIssues.length} found)

${childIssuesSummary || 'No child issues found'}

---

## PROJECT BOARD STATUS

${boardSummary || 'No project board items found'}

---

## YOUR TASK

Based on this analysis, determine the best course of action:

### Step 1: ANALYZE THE SITUATION
- What was the last successful step?
- What failed and why (check workflow logs if needed)?
- Are there blocking dependencies that need resolving?
- Are there stale tasks that need re-triggering?

### Step 2: DECIDE ON ACTION
Choose ONE of these approaches:

**A. RE-TRIGGER FAILED AGENTS**
If agents failed but the work is valid:
\`\`\`bash
# Remove and re-add agent label to re-trigger
gh issue edit <number> --remove-label "agent-failed"
gh issue edit <number> --remove-label "agent-<type>"
gh issue edit <number> --add-label "agent-<type>"
\`\`\`

**B. UPDATE BLOCKED DEPENDENCIES**
If tasks are blocked by completed work:
- Update the project board's blocked_by field
- Trigger the unblocked tasks

**C. REASSIGN WORK**
If the original approach failed:
- Update task descriptions with learnings
- Reassign to appropriate agent

**D. CONTINUE WHERE LEFT OFF**
If work is simply incomplete:
- Identify next pending task
- Trigger the appropriate agent

**E. MANUAL INTERVENTION NEEDED**
If the issue requires human decision:
- Clearly explain what went wrong
- Ask specific questions
- Wait for user response

### Step 3: EXECUTE YOUR DECISION
- Take the necessary actions using gh CLI
- Update the audit log
- Post a clear status comment explaining what you did and what happens next

### Step 4: POST STATUS UPDATE
Post a comment with:
1. What you found (brief summary)
2. What action you took
3. What happens next
4. Any questions for the user (if needed)

---

## RULES

${rules}

---

Execute the re-assessment now. Be decisive and take action.`;
}

// ============================================================================
// Utility: Check if reassessment is needed
// ============================================================================

export function shouldReassess(
  existingState: { waitingForUser?: boolean } | null,
  comments: IssueComment[]
): boolean {
  // If no existing state, this is a fresh start - might still need reassessment if there are comments
  if (!existingState) {
    // Check if there are agent activity comments
    const hasAgentActivity = comments.some(c =>
      c.body.includes('@agent-') &&
      (c.body.includes('Started') || c.body.includes('Complete') || c.body.includes('Failed'))
    );
    return hasAgentActivity;
  }

  // If waiting for user, this is a normal continue - no reassessment needed
  if (existingState.waitingForUser) {
    return false;
  }

  // Otherwise, reassess
  return true;
}

// ============================================================================
// Format Context for User Comment
// ============================================================================

export function formatReassessmentComment(
  context: ReassessmentContext,
  repoOwner: string,
  repoName: string
): string {
  const { analysis, childIssues, projectBoardItems, statusDiscrepancies, aidlcState } = context;

  // Scope board items to this EPIC's child issues for any user-facing count
  // that implies "this EPIC's progress" (rather than "what we fetched").
  const childNumbersForPrompt = new Set(childIssues.map(c => c.number));
  const scopedBoardItemsForPrompt = childNumbersForPrompt.size === 0
    ? []
    : projectBoardItems.filter(i => childNumbersForPrompt.has(i.issueNumber));

  // Build AIDLC pending input warning if user hasn't completed requirements review
  const editUrl = aidlcState?.currentPlanFile && aidlcState?.branch
    ? `https://github.com/${repoOwner}/${repoName}/edit/${aidlcState.branch}/${aidlcState.currentPlanFile}`
    : null;

  // Use the exported buildWorkflowProgress function for stage and artifacts display
  const workflowProgressSection = aidlcState
    ? buildWorkflowProgress(aidlcState, repoOwner, repoName)
    : '';

  const pmRecommendationsSection = aidlcState?.pmRecommendations
    ? `### PM Recommendations

${aidlcState.pmRecommendations}

> **Note**: These are my recommendations based on research. You can accept them or choose differently by editing the plan file.

`
    : '';

  const aidlcPendingWarning = aidlcState?.waitingForUser && aidlcState?.currentPlanFile
    ? `## ⚠️ Action Required: Complete Requirements Review

> **The workflow is waiting for your input!** You need to complete the requirements review before the workflow can proceed.

${workflowProgressSection}
${pmRecommendationsSection}### Quick Action

👉 **[Click here to edit the requirements plan](${editUrl})** 👈

Fill in all \`[Answer]:\` sections with your choices (you can accept my recommendations above or choose differently).

After editing, commit your changes with a message containing **"AIDLC continue"**.

<details>
<summary>Current workflow state</summary>

| Field | Value |
|-------|-------|
| Phase | ${aidlcState.phase} |
| Stage | ${aidlcState.stage} |
| Branch | \`${aidlcState.branch || 'main'}\` |
| Plan File | \`${aidlcState.currentPlanFile}\` |

</details>

---

`
    : '';

  // Build status discrepancy warning if any
  const discrepancyWarning = statusDiscrepancies && statusDiscrepancies.length > 0
    ? `### Status Discrepancies Detected

> **Important**: I found ${statusDiscrepancies.length} task(s) where the project board status doesn't match the actual workflow results. The analysis below uses the **actual workflow status**, not the board status.

| Issue | Board Says | Actually | Workflow Result |
|-------|------------|----------|-----------------|
${statusDiscrepancies.map(d =>
  `| #${d.issueNumber} | ${d.boardStatus || 'N/A'} | ${d.actualStatus} | ${d.workflowConclusion || 'N/A'}${d.hasFailedLabel ? ' (has agent-failed label)' : ''} |`
).join('\n')}

`
    : '';

  // Build child issues table
  const childIssuesTable = childIssues.length > 0
    ? `| Issue | Title | State | Labels |
|-------|-------|-------|--------|
${childIssues.slice(0, 20).map(i =>
  `| #${i.number} | ${i.title.substring(0, 40)}${i.title.length > 40 ? '...' : ''} | ${i.state} | ${i.labels.slice(0, 3).join(', ')} |`
).join('\n')}`
    : '_No child issues found_';

  // Build task status summary with links
  const formatTaskList = (tasks: number[], label: string) => {
    if (tasks.length === 0) return `**${label}**: None`;
    const links = tasks.slice(0, 10).map(n => `[#${n}](https://github.com/${repoOwner}/${repoName}/issues/${n})`);
    const suffix = tasks.length > 10 ? ` (+${tasks.length - 10} more)` : '';
    return `**${label}**: ${links.join(', ')}${suffix}`;
  };

  // Build agent failures section
  const failuresSection = analysis.agentFailures.length > 0
    ? `### Agent Failures Detected

| Issue | Agent | Details |
|-------|-------|---------|
${analysis.agentFailures.map(f =>
  `| [#${f.issueNumber}](https://github.com/${repoOwner}/${repoName}/issues/${f.issueNumber}) | \`${f.agent}\` | ${f.reason} |`
).join('\n')}

> These tasks have the \`agent-failed\` label and may need to be re-triggered.`
    : '';

  // Build blocked tasks section
  const blockedSection = analysis.blockedTasks.length > 0
    ? `### Blocked Tasks

The following tasks are waiting on dependencies:
${analysis.blockedTasks.map(n => {
  const boardItem = projectBoardItems.find(i => i.issueNumber === n);
  return `- [#${n}](https://github.com/${repoOwner}/${repoName}/issues/${n}) - Blocked by: ${boardItem?.blockedBy || 'unknown'}`;
}).join('\n')}`
    : '';

  // Build recommendations section
  const recommendationsSection = analysis.recommendations.length > 0
    ? `### Recommended Actions

${analysis.recommendations.map((r, i) => `${i + 1}. ${r}`).join('\n')}`
    : '';

  // Detect last agent activity
  const agentComments = context.comments.filter(c =>
    c.body.includes('@agent-') &&
    (c.body.includes('Started') || c.body.includes('Complete') || c.body.includes('Failed'))
  );
  const lastAgentActivity = agentComments.length > 0
    ? `**Last Agent Activity**: ${agentComments[agentComments.length - 1].createdAt}`
    : '';

  return `${aidlcPendingWarning}## Current State Analysis

I've analyzed the existing progress on this issue. Here's what I found:

${discrepancyWarning}
### Overview

| Metric | Value |
|--------|-------|
| **Phase** | ${analysis.phase.toUpperCase()} |
| **Total Child Issues** | ${childIssues.length} |
| **Project Board Items (this EPIC)** | ${scopedBoardItemsForPrompt.length} |
${lastAgentActivity ? `| ${lastAgentActivity} |` : ''}

### Task Status Summary

| Status | Count |
|--------|-------|
| Completed | ${analysis.completedTasks.length} |
| In Progress | ${analysis.inProgressTasks.length} |
| Failed | ${analysis.failedTasks.length} |
| Blocked | ${analysis.blockedTasks.length} |
| Pending | ${analysis.pendingTasks.length} |

<details>
<summary><b>Task Details</b> (click to expand)</summary>

${formatTaskList(analysis.completedTasks, 'Completed')}

${formatTaskList(analysis.inProgressTasks, 'In Progress')}

${formatTaskList(analysis.failedTasks, 'Failed')}

${formatTaskList(analysis.blockedTasks, 'Blocked')}

${formatTaskList(analysis.pendingTasks, 'Pending')}

</details>

${failuresSection}

${blockedSection}

### Child Issues

<details>
<summary><b>View ${childIssues.length} child issue(s)</b></summary>

${childIssuesTable}

</details>

${recommendationsSection}

---

### What would you like me to do?

Please reply with one of these commands:

| Command | Action |
|---------|--------|
| \`/approve\` | Execute all recommended actions automatically |
| \`/action 1\` | Execute only recommendation #1 |
| \`/action 1,2\` | Execute recommendations #1 and #2 |
| \`/retry #236\` | Re-trigger agent on specific issue |
| \`/skip\` | Skip reassessment and start fresh |
| \`/custom <instructions>\` | Provide custom instructions |

**I'll wait up to 30 minutes for your response.**`;
}

// ============================================================================
// Parse User Response for Reassessment
// ============================================================================

export interface UserReassessmentChoice {
  action: 'approve_all' | 'specific_actions' | 'retry_issues' | 'skip' | 'custom' | 'unknown';
  actionNumbers?: number[];
  issueNumbers?: number[];
  customInstructions?: string;
}

export function parseReassessmentResponse(comment: string): UserReassessmentChoice {
  const body = comment.trim().toLowerCase();

  // Check for /approve
  if (body.startsWith('/approve')) {
    return { action: 'approve_all' };
  }

  // Check for /skip
  if (body.startsWith('/skip')) {
    return { action: 'skip' };
  }

  // Check for /action N or /action N,M,O
  const actionMatch = body.match(/^\/action\s+([\d,\s]+)/);
  if (actionMatch) {
    const numbers = actionMatch[1].split(/[,\s]+/).map(n => parseInt(n.trim())).filter(n => !isNaN(n));
    return { action: 'specific_actions', actionNumbers: numbers };
  }

  // Check for /retry #N or /retry #N,#M
  const retryMatch = body.match(/^\/retry\s+([\d#,\s]+)/);
  if (retryMatch) {
    const numbers = retryMatch[1].replace(/#/g, '').split(/[,\s]+/).map(n => parseInt(n.trim())).filter(n => !isNaN(n));
    return { action: 'retry_issues', issueNumbers: numbers };
  }

  // Check for /custom
  const customMatch = comment.match(/^\/custom\s+(.+)/is);
  if (customMatch) {
    return { action: 'custom', customInstructions: customMatch[1].trim() };
  }

  return { action: 'unknown' };
}

// ============================================================================
// Build Execution Prompt Based on User Choice
// ============================================================================

export function buildReassessExecutionPrompt(
  issueNumber: string,
  issueTitle: string,
  issueBody: string,
  context: ReassessmentContext,
  userChoice: UserReassessmentChoice,
  rules: string
): string {
  const { analysis } = context;

  let actionInstructions = '';

  switch (userChoice.action) {
    case 'approve_all':
      actionInstructions = `
## USER APPROVED ALL RECOMMENDATIONS

Execute ALL of the following recommended actions:

${analysis.recommendations.map((r, i) => `${i + 1}. ${r}`).join('\n')}

For each action:
1. Execute it using the appropriate gh CLI commands
2. Log what you did
3. Move to the next action
`;
      break;

    case 'specific_actions':
      const selectedRecs = (userChoice.actionNumbers || [])
        .filter(n => n > 0 && n <= analysis.recommendations.length)
        .map(n => `${n}. ${analysis.recommendations[n - 1]}`);
      actionInstructions = `
## USER SELECTED SPECIFIC ACTIONS

Execute ONLY these actions (user's choice):

${selectedRecs.join('\n')}

Skip all other recommendations.
`;
      break;

    case 'retry_issues':
      actionInstructions = `
## USER REQUESTED RETRY ON SPECIFIC ISSUES

Re-trigger agents on these issues: ${(userChoice.issueNumbers || []).map(n => `#${n}`).join(', ')}

For each issue:
1. Remove the \`agent-failed\` label if present
2. Identify the appropriate agent from the issue labels or type
3. Remove and re-add the agent label to trigger the workflow
4. Post a comment noting the retry

\`\`\`bash
# Example for each issue:
gh issue edit <number> --remove-label "agent-failed" || true
gh issue edit <number> --remove-label "agent-developer"
gh issue edit <number> --add-label "agent-developer"
\`\`\`
`;
      break;

    case 'skip':
      actionInstructions = `
## USER CHOSE TO SKIP REASSESSMENT

The user wants to start fresh. Do NOT execute any reassessment actions.

Instead:
1. Post a comment acknowledging the skip
2. Clear any stale state if needed
3. Exit gracefully

The user may re-trigger with a fresh approach later.
`;
      break;

    case 'custom':
      actionInstructions = `
## USER PROVIDED CUSTOM INSTRUCTIONS

Follow these custom instructions from the user:

---
${userChoice.customInstructions}
---

Interpret and execute these instructions in the context of the current issue state.
If the instructions are unclear, post a comment asking for clarification.
`;
      break;

    default:
      actionInstructions = `
## COULD NOT PARSE USER RESPONSE

The user's response didn't match any expected command.

Post a comment asking them to use one of these commands:
- \`/approve\` - Execute all recommendations
- \`/action 1,2\` - Execute specific recommendations
- \`/retry #236\` - Retry specific issues
- \`/skip\` - Skip and start fresh
- \`/custom <instructions>\` - Custom instructions
`;
  }

  // Truncate issue body if too long
  const truncatedBody = issueBody.length > 2000
    ? issueBody.substring(0, 2000) + '\n\n... (truncated)'
    : issueBody;

  return `You are @agent-pm, the AIDLC Workflow Orchestrator.

## EXECUTING USER'S REASSESSMENT CHOICE

### Original Request
**Issue #${issueNumber}: ${issueTitle}**

${truncatedBody}

---

### Current State Summary
- Phase: ${analysis.phase}
- Failed: ${analysis.failedTasks.length} | Blocked: ${analysis.blockedTasks.length} | Pending: ${analysis.pendingTasks.length}

${actionInstructions}

## After Executing

1. Post a summary comment with what you did
2. If there are more pending tasks, trigger the next agents
3. Update any project board statuses as needed

## Rules
${rules}

Execute now.`;
}

// ============================================================================
// Format Command Acknowledgment
// ============================================================================

export function formatCommandAcknowledgment(
  choice: UserReassessmentChoice,
  originalComment: string
): string {
  const timestamp = new Date().toISOString();

  let acknowledgment = `## Command Received

**Time**: ${timestamp}
`;

  switch (choice.action) {
    case 'approve_all':
      acknowledgment += `
**Command**: \`/approve\`

**I understood**: Execute all recommended actions automatically.

**What I'm about to do**:
- Execute each recommendation in order
- Update project board statuses
- Re-trigger any failed agents
- Post a summary when complete

**Starting execution now...**`;
      break;

    case 'specific_actions':
      acknowledgment += `
**Command**: \`/action ${choice.actionNumbers?.join(', ')}\`

**I understood**: Execute only the specific recommendation(s) you selected.

**What I'm about to do**:
- Execute recommendation(s): ${choice.actionNumbers?.map(n => `#${n}`).join(', ')}
- Skip other recommendations
- Post a summary when complete

**Starting execution now...**`;
      break;

    case 'retry_issues':
      acknowledgment += `
**Command**: \`/retry ${choice.issueNumbers?.map(n => `#${n}`).join(', ')}\`

**I understood**: Re-trigger agents on the specified issue(s).

**What I'm about to do**:
- Remove \`agent-failed\` label (if present) from: ${choice.issueNumbers?.map(n => `#${n}`).join(', ')}
- Identify the appropriate agent for each issue
- Re-add agent labels to trigger workflows
- Post a summary when complete

**Starting execution now...**`;
      break;

    case 'skip':
      acknowledgment += `
**Command**: \`/skip\`

**I understood**: Skip the reassessment and don't take any corrective action.

**What I'm about to do**:
- Not execute any recommendations
- Exit gracefully
- You can restart with \`agent-pm\` label later

**Acknowledged.**`;
      break;

    case 'custom':
      // Truncate long custom instructions for display
      const displayInstructions = choice.customInstructions && choice.customInstructions.length > 300
        ? choice.customInstructions.substring(0, 300) + '...'
        : choice.customInstructions;

      acknowledgment += `
**Command**: \`/custom\`

**Your instructions**:
> ${displayInstructions}

**I understood**: Follow your custom instructions for this reassessment.

**What I'm about to do**:
- Analyze your instructions
- Take appropriate action based on your guidance
- Post a summary when complete

**Starting execution now...**`;
      break;

    default:
      acknowledgment += `
**Command**: Could not parse

**Your message**:
> ${originalComment.substring(0, 200)}${originalComment.length > 200 ? '...' : ''}

**I didn't recognize this command.** Please use one of:
- \`/approve\` - Execute all recommendations
- \`/action 1,2\` - Execute specific recommendations
- \`/retry #236\` - Retry specific issues
- \`/skip\` - Skip reassessment
- \`/custom <instructions>\` - Custom instructions`;
  }

  return acknowledgment;
}

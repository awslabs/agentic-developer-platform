/**
 * Monitoring Module for @agent-pm
 *
 * This module provides proactive monitoring capabilities for PM to:
 * - Track all triggered agents in real-time
 * - Monitor workflow runs for progress/completion/failure
 * - Report status updates to the user
 * - Accept user commands continuously
 * - Persist state for session resumption
 *
 * Designed to be modular and isolatable - can be disabled without affecting core PM flow.
 *
 * Architecture:
 * - Single async loop using Node.js event loop (not multi-threaded)
 * - Polls every 30 seconds: workflows, board, comments
 * - Only invokes Claude for decisions, not for polling
 * - State persisted to file for session resumption
 */

// ============================================================================
// Types
// ============================================================================

export interface MonitoringConfig {
  enabled: boolean;
  pollIntervalMs: number;          // How often to poll (default: 30s)
  sessionTimeoutMs: number;        // Max session duration (default: 2 hours)
  statusUpdateIntervalMs: number;  // How often to post status updates (default: 5 min)
  aiAnalysisIntervalMs: number;    // How often to run AI status analysis (default: 10 min)
  maxConsecutiveErrors: number;    // Errors before stopping (default: 5)
}

export interface TrackedAgent {
  issueNumber: number;
  issueTitle: string;
  agentType: string;               // developer, architect, operations, etc.
  status: AgentStatus;
  workflowRunId: number | null;
  workflowRunUrl: string | null;
  startedAt: string;
  lastCheckedAt: string;
  lastProgressAt: string;          // Last time we detected activity
  conclusion: string | null;       // success, failure, cancelled, etc.
  errorMessage: string | null;
}

export type AgentStatus =
  | 'pending'      // Label added, waiting for workflow to start
  | 'queued'       // Workflow queued
  | 'running'      // Workflow in progress
  | 'completed'    // Workflow completed successfully
  | 'failed'       // Workflow failed
  | 'cancelled'    // Workflow cancelled
  | 'stuck'        // No progress for too long
  | 'unknown';     // Cannot determine status

export interface MonitoringEvent {
  type: EventType;
  timestamp: string;
  issueNumber: number;
  agentType: string;
  details: Record<string, unknown>;
}

export type EventType =
  | 'agent_triggered'      // Agent label added
  | 'workflow_started'     // Workflow run started
  | 'workflow_completed'   // Workflow completed successfully
  | 'workflow_failed'      // Workflow failed
  | 'workflow_cancelled'   // Workflow cancelled
  | 'agent_stuck'          // No progress detected
  | 'user_command'         // User posted a command
  | 'status_update'        // Periodic status update posted
  | 'session_timeout'      // Session timeout reached
  | 'error';               // Error occurred

export interface UserCommand {
  command: string;
  args: string[];
  rawText: string;
  commentId: number;
  author: string;
  timestamp: string;
}

export interface MonitoringState {
  sessionId: string;
  parentIssueNumber: number;
  projectNumber: number | null;
  startedAt: string;
  lastPollAt: string;
  lastStatusUpdateAt: string;
  lastAIAnalysisAt: string;
  lastAIAnalysisReport: string | null;
  trackedAgents: TrackedAgent[];
  events: MonitoringEvent[];
  pendingCommands: UserCommand[];
  consecutiveErrors: number;
  isActive: boolean;
}

export interface MonitoringSnapshot {
  timestamp: string;
  pendingAgents: number;
  activeAgents: number;
  completedAgents: number;
  failedAgents: number;
  stuckAgents: number;
  pendingUserCommands: number;
  recentEvents: MonitoringEvent[];
  // Project board status (if available)
  projectBoard?: {
    total: number;
    todo: number;
    inProgress: number;
    blocked: number;
    done: number;
  };
}

// ============================================================================
// Configuration
// ============================================================================

const DEFAULT_CONFIG: MonitoringConfig = {
  enabled: true,
  pollIntervalMs: 5 * 60 * 1000,         // 5 minutes
  sessionTimeoutMs: 5 * 60 * 60 * 1000,  // 5 hours
  statusUpdateIntervalMs: 5 * 60 * 1000, // 5 minutes (status comment every poll)
  aiAnalysisIntervalMs: 5 * 60 * 1000,   // 5 minutes - AI reviews every poll
  maxConsecutiveErrors: 5,
};

let config: MonitoringConfig = { ...DEFAULT_CONFIG };

export function configureMonitoring(newConfig: Partial<MonitoringConfig>): void {
  config = { ...config, ...newConfig };
}

export function isMonitoringEnabled(): boolean {
  return config.enabled;
}

export function getMonitoringConfig(): MonitoringConfig {
  return { ...config };
}

// ============================================================================
// State Management
// ============================================================================

import * as fs from 'fs';
import * as path from 'path';
import { resilientQuery } from './utils/resilientQuery';
import {
  gatherReassessmentContext,
  ReassessmentContext,
} from './reassessment';

const STATE_FILE_NAME = 'pm-monitoring-state.json';

function getStateFilePath(workDir: string): string {
  return path.join(workDir, 'aidlc-docs', STATE_FILE_NAME);
}

export function loadMonitoringState(workDir: string): MonitoringState | null {
  const statePath = getStateFilePath(workDir);
  if (fs.existsSync(statePath)) {
    try {
      const data = fs.readFileSync(statePath, 'utf-8');
      return JSON.parse(data) as MonitoringState;
    } catch {
      return null;
    }
  }
  return null;
}

export function saveMonitoringState(workDir: string, state: MonitoringState): void {
  const aidlcDir = path.join(workDir, 'aidlc-docs');
  if (!fs.existsSync(aidlcDir)) {
    fs.mkdirSync(aidlcDir, { recursive: true });
  }
  fs.writeFileSync(getStateFilePath(workDir), JSON.stringify(state, null, 2));
}

export function createInitialState(
  parentIssueNumber: number,
  projectNumber: number | null
): MonitoringState {
  return {
    sessionId: `pm-${parentIssueNumber}-${Date.now()}`,
    parentIssueNumber,
    projectNumber,
    startedAt: new Date().toISOString(),
    lastPollAt: new Date().toISOString(),
    lastStatusUpdateAt: new Date().toISOString(),
    lastAIAnalysisAt: new Date().toISOString(),
    lastAIAnalysisReport: null,
    trackedAgents: [],
    events: [],
    pendingCommands: [],
    consecutiveErrors: 0,
    isActive: true,
  };
}

// ============================================================================
// GitHub Data Fetching
// ============================================================================

type ExecCommandFn = (command: string, useAppToken?: boolean) => Promise<string>;

export interface WorkflowRunInfo {
  id: number;
  name: string;
  status: string;        // queued, in_progress, completed
  conclusion: string | null; // success, failure, cancelled, skipped, etc.
  headSha: string;
  url: string;
  createdAt: string;
  updatedAt: string;
  event: string;
}

/**
 * Fetch recent workflow runs for agent workflows
 */
export async function fetchAgentWorkflowRuns(
  repoOwner: string,
  repoName: string,
  execCommand: ExecCommandFn,
  limit: number = 50
): Promise<WorkflowRunInfo[]> {
  try {
    const result = await execCommand(
      `gh run list --repo ${repoOwner}/${repoName} --limit ${limit} --json databaseId,name,status,conclusion,headSha,url,createdAt,updatedAt,event`
    );

    const runs = JSON.parse(result || '[]');
    return runs.map((run: Record<string, unknown>) => ({
      id: run.databaseId as number,
      name: run.name as string,
      status: run.status as string,
      conclusion: run.conclusion as string | null,
      headSha: run.headSha as string,
      url: run.url as string,
      createdAt: run.createdAt as string,
      updatedAt: run.updatedAt as string,
      event: run.event as string,
    }));
  } catch (error) {
    console.error('Failed to fetch workflow runs:', (error as Error).message);
    return [];
  }
}

/**
 * Fetch workflow run for a specific issue
 */
export async function fetchWorkflowRunForIssue(
  repoOwner: string,
  repoName: string,
  issueNumber: number,
  agentType: string,
  execCommand: ExecCommandFn
): Promise<WorkflowRunInfo | null> {
  // Try multiple workflow name patterns - target repos use call-agent-*, ADP uses agent-*
  const workflowPatterns = getWorkflowPatternsForAgent(agentType);

  for (const workflowName of workflowPatterns) {
    try {
      // Get recent runs for this workflow
      const result = await execCommand(
        `gh run list --repo ${repoOwner}/${repoName} --workflow "${workflowName}" --limit 20 --json databaseId,name,status,conclusion,headSha,url,createdAt,updatedAt,event`
      );

      const runs = JSON.parse(result || '[]') as Array<Record<string, unknown>>;
      if (runs.length === 0) continue; // Try next pattern

      // Find a run that was triggered for this issue
      // We need to check the run's triggering event for the issue number
      for (const run of runs) {
        // For label-triggered workflows, we can try to match by checking the run's jobs
        const runId = run.databaseId as number;

        try {
          const jobsResult = await execCommand(
            `gh run view ${runId} --repo ${repoOwner}/${repoName} --json jobs`
          );
          const jobsData = JSON.parse(jobsResult || '{}');

          // Check if any job step mentions our issue
          const jobsJson = JSON.stringify(jobsData);
          if (jobsJson.includes(`${issueNumber}`) || jobsJson.includes(`#${issueNumber}`)) {
            return {
              id: runId,
              name: run.name as string,
              status: run.status as string,
              conclusion: run.conclusion as string | null,
              headSha: run.headSha as string,
              url: run.url as string,
              createdAt: run.createdAt as string,
              updatedAt: run.updatedAt as string,
              event: run.event as string,
            };
          }
        } catch {
          // Continue to next run if we can't get job details
          continue;
        }
      }
    } catch {
      // Try next workflow pattern
      continue;
    }
  }

  return null;
}

function getWorkflowPatternsForAgent(agentType: string): string[] {
  // Return patterns to try in order: call-agent-* first (target repos), then agent-* (ADP)
  return [
    `call-agent-${agentType}.yml`,
    `agent-${agentType}.yml`,
  ];
}

/**
 * Project board summary for status reporting
 */
export interface ProjectBoardSummary {
  total: number;
  todo: number;
  inProgress: number;
  blocked: number;
  done: number;
  byStatus: Record<string, number>;
  items: Array<{
    issueNumber: number;
    title: string;
    status: string;
    assignedAgent: string | null;
  }>;
}

/**
 * Fetch project board items
 */
export async function fetchProjectBoardItems(
  repoOwner: string,
  projectNumber: number,
  execCommand: ExecCommandFn
): Promise<Array<{
  issueNumber: number;
  title: string;
  status: string | null;
  assignedAgent: string | null;
}>> {
  try {
    const result = await execCommand(
      `gh project item-list ${projectNumber} --owner ${repoOwner} --format json --limit 200`
    );

    const data = JSON.parse(result || '{"items":[]}');
    const items: Array<{
      issueNumber: number;
      title: string;
      status: string | null;
      assignedAgent: string | null;
    }> = [];

    for (const item of data.items || []) {
      const issueNumber = item.content?.number;
      if (issueNumber) {
        items.push({
          issueNumber,
          title: item.title || item.content?.title || '',
          status: item.status || null,
          assignedAgent: item.assigned_agent || null,
        });
      }
    }

    return items;
  } catch (error) {
    console.error('Failed to fetch project board items:', (error as Error).message);
    return [];
  }
}

/**
 * Fetch project board summary with counts by status
 */
export async function fetchProjectBoardSummary(
  repoOwner: string,
  projectNumber: number,
  parentIssueNumber: number,
  execCommand: ExecCommandFn
): Promise<ProjectBoardSummary> {
  const summary: ProjectBoardSummary = {
    total: 0,
    todo: 0,
    inProgress: 0,
    blocked: 0,
    done: 0,
    byStatus: {},
    items: [],
  };

  try {
    const items = await fetchProjectBoardItems(repoOwner, projectNumber, execCommand);

    for (const item of items) {
      // Skip the parent issue itself
      if (item.issueNumber === parentIssueNumber) continue;

      summary.total++;
      const status = (item.status || 'Unknown').toLowerCase();

      // Count by normalized status
      summary.byStatus[item.status || 'Unknown'] = (summary.byStatus[item.status || 'Unknown'] || 0) + 1;

      // Map to standard categories
      if (status.includes('done') || status.includes('complete')) {
        summary.done++;
      } else if (status.includes('progress') || status.includes('active') || status.includes('running')) {
        summary.inProgress++;
      } else if (status.includes('block')) {
        summary.blocked++;
      } else if (status.includes('todo') || status.includes('backlog') || status.includes('pending')) {
        summary.todo++;
      } else {
        summary.todo++; // Default to todo
      }

      summary.items.push({
        issueNumber: item.issueNumber,
        title: item.title,
        status: item.status || 'Unknown',
        assignedAgent: item.assignedAgent,
      });
    }
  } catch (error) {
    console.error('Failed to fetch project board summary:', (error as Error).message);
  }

  return summary;
}

/**
 * Fetch comments on an issue (looking for user commands)
 */
export async function fetchIssueComments(
  issueNumber: string | number,
  execCommand: ExecCommandFn,
  limit: number = 50
): Promise<Array<{
  id: number;
  author: string;
  body: string;
  createdAt: string;
}>> {
  try {
    const result = await execCommand(
      `gh issue view ${issueNumber} --json comments`
    );

    const data = JSON.parse(result || '{"comments":[]}');
    const comments = (data.comments || []).slice(-limit);

    return comments.map((c: Record<string, unknown>) => ({
      id: c.id as number || 0,
      author: (c.author as Record<string, unknown>)?.login as string || 'unknown',
      body: c.body as string || '',
      createdAt: c.createdAt as string || '',
    }));
  } catch (error) {
    console.error('Failed to fetch issue comments:', (error as Error).message);
    return [];
  }
}

/**
 * Fetch labels on child issues to detect triggered agents
 */
export async function fetchChildIssuesWithAgentLabels(
  repoOwner: string,
  repoName: string,
  parentIssueNumber: number,
  execCommand: ExecCommandFn
): Promise<Array<{
  issueNumber: number;
  title: string;
  labels: string[];
  state: string;
}>> {
  try {
    // Search for issues that mention the parent issue
    const searchResult = await execCommand(
      `gh issue list --repo ${repoOwner}/${repoName} --search "linked:#${parentIssueNumber} OR body:#${parentIssueNumber}" --json number,title,labels,state --limit 100`
    );

    const issues = JSON.parse(searchResult || '[]');
    return issues.map((issue: Record<string, unknown>) => ({
      issueNumber: issue.number as number,
      title: issue.title as string,
      labels: ((issue.labels as Array<Record<string, unknown>>) || []).map((l) => l.name as string),
      state: issue.state as string,
    }));
  } catch (error) {
    console.error('Failed to fetch child issues:', (error as Error).message);
    return [];
  }
}

/**
 * Fetch agents from project board based on assigned_agent field
 * This is more reliable than labels since labels are removed when workflows start
 */
export async function fetchAgentsFromProjectBoard(
  repoOwner: string,
  projectNumber: number,
  parentIssueNumber: number,
  execCommand: ExecCommandFn
): Promise<Array<{
  issueNumber: number;
  title: string;
  agentType: string;
  status: string;
}>> {
  try {
    const result = await execCommand(
      `gh project item-list ${projectNumber} --owner ${repoOwner} --format json --limit 100`
    );

    const data = JSON.parse(result || '{"items":[]}');
    const agents: Array<{ issueNumber: number; title: string; agentType: string; status: string }> = [];

    for (const item of data.items || []) {
      // Skip items without issue numbers or the parent issue itself
      const issueNumber = item.content?.number;
      if (!issueNumber || issueNumber === parentIssueNumber) continue;

      // Note: We don't filter by title anymore since the project board itself
      // is already scoped to a specific parent issue. All items on the board
      // are relevant child issues.
      const title = item.title || item.content?.title || '';

      // Check if assigned_agent is set
      const assignedAgent = item['assigned_agent'] || item.assigned_agent;
      if (!assignedAgent || assignedAgent === '@agent-pm') continue;

      // Extract agent type from "@agent-xxx" format
      const match = assignedAgent.match(/@agent-(\w+)/);
      if (!match) continue;

      const agentType = match[1];
      const status = item.status || 'Unknown';

      // Only track items that aren't Done
      if (status !== 'Done') {
        agents.push({
          issueNumber,
          title: item.title || `Issue #${issueNumber}`,
          agentType,
          status,
        });
      }
    }

    return agents;
  } catch (error) {
    console.error('Failed to fetch agents from project board:', (error as Error).message);
    return [];
  }
}

/**
 * Scan for running agent workflows on child issues.
 * This is a fallback when labels are removed and assigned_agent is not set.
 */
export async function fetchRunningAgentWorkflows(
  repoOwner: string,
  repoName: string,
  parentIssueNumber: number,
  execCommand: ExecCommandFn
): Promise<Array<{
  issueNumber: number;
  title: string;
  agentType: string;
  workflowRunId: number;
  workflowRunUrl: string;
}>> {
  // Support both target repo pattern (call-agent-*.yml) and ADP pattern (agent-*.yml)
  const agentWorkflows = [
    { type: 'operations', workflows: ['call-agent-operations.yml', 'agent-operations.yml'] },
    { type: 'developer', workflows: ['call-agent-developer.yml', 'agent-developer.yml'] },
    { type: 'architect', workflows: ['call-agent-architect.yml', 'agent-architect.yml'] },
    { type: 'reviewer', workflows: ['call-agent-reviewer.yml', 'agent-reviewer.yml'] },
    { type: 'product', workflows: ['call-agent-product.yml', 'agent-product.yml'] },
  ];

  const results: Array<{
    issueNumber: number;
    title: string;
    agentType: string;
    workflowRunId: number;
    workflowRunUrl: string;
  }> = [];

  for (const { type, workflows } of agentWorkflows) {
    // Try each workflow pattern (call-agent-* first, then agent-*)
    let runs: Array<Record<string, unknown>> = [];
    for (const workflow of workflows) {
      try {
        const runsResult = await execCommand(
          `gh run list --repo ${repoOwner}/${repoName} --workflow "${workflow}" --status in_progress --json databaseId,displayTitle,url --limit 10`
        );
        runs = JSON.parse(runsResult || '[]') as Array<Record<string, unknown>>;
        if (runs.length > 0) break; // Found runs with this pattern
      } catch {
        // Try next pattern
        continue;
      }
    }
    try {

      for (const run of runs) {
        const title = run.displayTitle as string || '';
        // Check if this run is for a child issue of our parent
        if (title.includes(`#${parentIssueNumber}`)) {
          // Extract issue number from title like "[Unit] U5: ... #247"
          const issueMatch = title.match(/\[Unit\].*#(\d+)/);
          // Also try to get from run name directly
          const runId = run.databaseId as number;

          // Get the issue number from the workflow run's triggering issue
          try {
            const runDetails = await execCommand(
              `gh run view ${runId} --repo ${repoOwner}/${repoName} --json jobs`
            );
            const runData = JSON.parse(runDetails || '{}');
            const jobsJson = JSON.stringify(runData);

            // Find issue numbers in the jobs that are child issues (contain #parentIssueNumber in title)
            const issueNumMatch = jobsJson.match(/"number":(\d+)/);
            if (issueNumMatch) {
              const issueNumber = parseInt(issueNumMatch[1], 10);
              if (issueNumber !== parentIssueNumber) {
                results.push({
                  issueNumber,
                  title,
                  agentType: type,
                  workflowRunId: runId,
                  workflowRunUrl: run.url as string,
                });
                continue;
              }
            }
          } catch {
            // Continue with fallback
          }

          // Fallback: use issue match from title
          if (issueMatch) {
            const issueNumber = parseInt(issueMatch[1], 10);
            results.push({
              issueNumber,
              title,
              agentType: type,
              workflowRunId: run.databaseId as number,
              workflowRunUrl: run.url as string,
            });
          }
        }
      }
    } catch (error) {
      console.error(`Failed to fetch running ${type} workflows:`, (error as Error).message);
    }
  }

  return results;
}

// ============================================================================
// Event Detection
// ============================================================================

const AGENT_LABELS = ['agent-developer', 'agent-architect', 'agent-operations', 'agent-reviewer', 'agent-product'];
const STUCK_THRESHOLD_MS = 15 * 60 * 1000; // 15 minutes without progress = stuck

/**
 * Detect agents that have been triggered (have agent labels)
 */
export function detectTriggeredAgents(
  childIssues: Array<{ issueNumber: number; title: string; labels: string[]; state: string }>,
  existingTracked: TrackedAgent[]
): TrackedAgent[] {
  const newAgents: TrackedAgent[] = [];
  const existingNumbers = new Set(existingTracked.map((a) => a.issueNumber));

  for (const issue of childIssues) {
    if (existingNumbers.has(issue.issueNumber)) continue;

    // Check if issue has an agent label
    const agentLabel = issue.labels.find((l) => AGENT_LABELS.includes(l));
    if (agentLabel) {
      const agentType = agentLabel.replace('agent-', '');
      newAgents.push({
        issueNumber: issue.issueNumber,
        issueTitle: issue.title,
        agentType,
        status: 'pending',
        workflowRunId: null,
        workflowRunUrl: null,
        startedAt: new Date().toISOString(),
        lastCheckedAt: new Date().toISOString(),
        lastProgressAt: new Date().toISOString(),
        conclusion: null,
        errorMessage: null,
      });
    }
  }

  return newAgents;
}

/**
 * Update tracked agent status based on workflow run
 */
export function updateAgentStatus(
  agent: TrackedAgent,
  workflowRun: WorkflowRunInfo | null
): { agent: TrackedAgent; event: MonitoringEvent | null } {
  const now = new Date().toISOString();
  const previousStatus = agent.status;

  agent.lastCheckedAt = now;

  if (!workflowRun) {
    // No workflow run found - still pending or may have failed to start
    if (agent.status === 'pending') {
      const pendingDuration = Date.now() - new Date(agent.startedAt).getTime();
      if (pendingDuration > STUCK_THRESHOLD_MS) {
        agent.status = 'stuck';
        agent.errorMessage = 'Workflow did not start within expected time';
        return {
          agent,
          event: createEvent('agent_stuck', agent.issueNumber, agent.agentType, {
            reason: 'workflow_not_started',
            pendingDurationMs: pendingDuration,
          }),
        };
      }
    }
    return { agent, event: null };
  }

  // Update workflow info
  agent.workflowRunId = workflowRun.id;
  agent.workflowRunUrl = workflowRun.url;

  // Map workflow status to agent status
  switch (workflowRun.status) {
    case 'queued':
      agent.status = 'queued';
      agent.lastProgressAt = now;
      break;

    case 'in_progress':
      agent.status = 'running';
      agent.lastProgressAt = now;
      break;

    case 'completed':
      agent.conclusion = workflowRun.conclusion;
      switch (workflowRun.conclusion) {
        case 'success':
          agent.status = 'completed';
          break;
        case 'failure':
          agent.status = 'failed';
          agent.errorMessage = 'Workflow failed';
          break;
        case 'cancelled':
          agent.status = 'cancelled';
          break;
        default:
          agent.status = 'failed';
          agent.errorMessage = `Workflow ended with: ${workflowRun.conclusion}`;
      }
      break;

    default:
      agent.status = 'unknown';
  }

  // Check for stuck agents (running but no progress)
  if (agent.status === 'running') {
    const timeSinceProgress = Date.now() - new Date(agent.lastProgressAt).getTime();
    if (timeSinceProgress > STUCK_THRESHOLD_MS) {
      agent.status = 'stuck';
      agent.errorMessage = 'No progress detected for extended period';
    }
  }

  // Generate event if status changed
  let event: MonitoringEvent | null = null;
  if (previousStatus !== agent.status) {
    switch (agent.status) {
      case 'running':
        event = createEvent('workflow_started', agent.issueNumber, agent.agentType, {
          workflowRunId: workflowRun.id,
          workflowUrl: workflowRun.url,
        });
        break;
      case 'completed':
        event = createEvent('workflow_completed', agent.issueNumber, agent.agentType, {
          workflowRunId: workflowRun.id,
          conclusion: workflowRun.conclusion,
        });
        break;
      case 'failed':
        event = createEvent('workflow_failed', agent.issueNumber, agent.agentType, {
          workflowRunId: workflowRun.id,
          conclusion: workflowRun.conclusion,
          errorMessage: agent.errorMessage,
        });
        break;
      case 'cancelled':
        event = createEvent('workflow_cancelled', agent.issueNumber, agent.agentType, {
          workflowRunId: workflowRun.id,
        });
        break;
      case 'stuck':
        event = createEvent('agent_stuck', agent.issueNumber, agent.agentType, {
          reason: agent.errorMessage,
        });
        break;
    }
  }

  return { agent, event };
}

function createEvent(
  type: EventType,
  issueNumber: number,
  agentType: string,
  details: Record<string, unknown>
): MonitoringEvent {
  return {
    type,
    timestamp: new Date().toISOString(),
    issueNumber,
    agentType,
    details,
  };
}

// ============================================================================
// User Command Parsing
// ============================================================================

const MONITORING_COMMANDS = [
  '/status',       // Get current status
  '/pause',        // Pause monitoring
  '/resume',       // Resume monitoring
  '/retry',        // Retry failed agent: /retry #123
  '/stop',         // Stop monitoring completely
  '/extend',       // Extend session timeout
  '/help',         // Show available commands
  '/instruct',     // Give custom instruction: /instruct unblock unit3 and trigger agent
  '/queryPM',      // Ask PM a question or change direction: /queryPM why is #276 stuck?
];

/**
 * Parse user commands from comments
 */
export function parseUserCommands(
  comments: Array<{ id: number; author: string; body: string; createdAt: string }>,
  lastProcessedCommentId: number
): UserCommand[] {
  const commands: UserCommand[] = [];

  for (const comment of comments) {
    // Skip already processed comments
    if (comment.id <= lastProcessedCommentId) continue;

    // Skip bot comments
    if (comment.author.includes('[bot]') || comment.author === 'github-actions') continue;

    // Look for commands
    const lines = comment.body.split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('/')) continue;

      const parts = trimmed.split(/\s+/);
      const cmd = parts[0].toLowerCase();

      if (MONITORING_COMMANDS.some((c) => cmd.startsWith(c))) {
        commands.push({
          command: cmd,
          args: parts.slice(1),
          rawText: trimmed,
          commentId: comment.id,
          author: comment.author,
          timestamp: comment.createdAt,
        });
      }
    }
  }

  return commands;
}

// ============================================================================
// Status Formatting
// ============================================================================

/**
 * Format monitoring status as a comment
 */
export function formatStatusUpdate(
  state: MonitoringState,
  snapshot: MonitoringSnapshot
): string {
  const sessionDuration = Math.round(
    (Date.now() - new Date(state.startedAt).getTime()) / 60000
  );
  const remainingTime = Math.round(
    (config.sessionTimeoutMs - (Date.now() - new Date(state.startedAt).getTime())) / 60000
  );

  // Build project board section if available
  let projectBoardSection = '';
  if (snapshot.projectBoard && snapshot.projectBoard.total > 0) {
    const pb = snapshot.projectBoard;
    const progressPct = pb.total > 0 ? Math.round((pb.done / pb.total) * 100) : 0;
    projectBoardSection = `
### Project Board Status

| Status | Count |
|--------|-------|
| Todo | ${pb.todo} |
| In Progress | ${pb.inProgress} |
| Blocked | ${pb.blocked} |
| Done | ${pb.done} |
| **Total** | **${pb.total}** |

**Progress**: ${progressPct}% complete (${pb.done}/${pb.total})

`;
  }

  let status = `## PM Monitoring Status Update

**Session**: ${state.sessionId}
**Duration**: ${sessionDuration} minutes (${remainingTime} min remaining)
**Last Poll**: ${state.lastPollAt}
${projectBoardSection}
### Active Workflows

| Status | Count |
|--------|-------|
| Pending (Triggered) | ${snapshot.pendingAgents} |
| Active (Running) | ${snapshot.activeAgents} |
| Completed | ${snapshot.completedAgents} |
| Failed | ${snapshot.failedAgents} |
| Stuck | ${snapshot.stuckAgents} |

### Tracked Agents

`;

  if (state.trackedAgents.length === 0) {
    status += '_No agents currently tracked_\n';
  } else {
    status += '| Issue | Agent | Status | Workflow |\n';
    status += '|-------|-------|--------|----------|\n';

    for (const agent of state.trackedAgents) {
      const statusEmoji = getStatusEmoji(agent.status);
      const workflowLink = agent.workflowRunUrl
        ? `[View](${agent.workflowRunUrl})`
        : '-';
      status += `| #${agent.issueNumber} | @agent-${agent.agentType} | ${statusEmoji} ${agent.status} | ${workflowLink} |\n`;
    }
  }

  if (snapshot.recentEvents.length > 0) {
    status += '\n### Recent Events\n\n';
    for (const event of snapshot.recentEvents.slice(-5)) {
      const time = new Date(event.timestamp).toLocaleTimeString();
      status += `- **${time}**: ${formatEventDescription(event)}\n`;
    }
  }

  status += `
---
**Commands**: \`/queryPM <question>\` \`/status\` \`/retry #N\` \`/instruct\` \`/help\`
`;

  return status;
}

function getStatusEmoji(status: AgentStatus): string {
  const emojis: Record<AgentStatus, string> = {
    pending: '⏳',
    queued: '📋',
    running: '🔄',
    completed: '✅',
    failed: '❌',
    cancelled: '🚫',
    stuck: '⚠️',
    unknown: '❓',
  };
  return emojis[status] || '❓';
}

function formatEventDescription(event: MonitoringEvent): string {
  switch (event.type) {
    case 'agent_triggered':
      return `Agent @agent-${event.agentType} triggered for #${event.issueNumber}`;
    case 'workflow_started':
      return `Workflow started for @agent-${event.agentType} (#${event.issueNumber})`;
    case 'workflow_completed':
      return `@agent-${event.agentType} completed #${event.issueNumber}`;
    case 'workflow_failed':
      return `@agent-${event.agentType} FAILED on #${event.issueNumber}`;
    case 'workflow_cancelled':
      return `@agent-${event.agentType} cancelled on #${event.issueNumber}`;
    case 'agent_stuck':
      return `@agent-${event.agentType} appears STUCK on #${event.issueNumber}`;
    case 'user_command':
      return `User command: ${event.details.command}`;
    default:
      return `${event.type} for #${event.issueNumber}`;
  }
}

/**
 * Format help message
 */
export function formatHelpMessage(): string {
  return `## PM Monitoring Commands

| Command | Description |
|---------|-------------|
| \`/status\` | Show current monitoring status |
| \`/retry #N\` | Retry failed agent on issue #N |
| \`/queryPM <question>\` | Ask PM a question or change direction (e.g., \`/queryPM why is #276 stuck?\`) |
| \`/instruct <message>\` | Give PM a custom instruction (e.g., \`/instruct unblock #237 and trigger agent\`) |
| \`/pause\` | Pause monitoring (agents continue, but PM stops watching) |
| \`/resume\` | Resume paused monitoring |
| \`/stop\` | Stop monitoring completely |
| \`/extend\` | Extend session by 1 hour |
| \`/help\` | Show this help message |

**Note**: PM monitors all child agents automatically. Use \`/queryPM\` to ask questions or change direction.
`;
}

/**
 * Create a monitoring snapshot from current state
 */
export function createSnapshot(
  state: MonitoringState,
  projectBoardSummary?: ProjectBoardSummary
): MonitoringSnapshot {
  const agents = state.trackedAgents;

  const snapshot: MonitoringSnapshot = {
    timestamp: new Date().toISOString(),
    pendingAgents: agents.filter((a) => a.status === 'pending').length,
    activeAgents: agents.filter((a) => a.status === 'running' || a.status === 'queued').length,
    completedAgents: agents.filter((a) => a.status === 'completed').length,
    failedAgents: agents.filter((a) => a.status === 'failed').length,
    stuckAgents: agents.filter((a) => a.status === 'stuck').length,
    pendingUserCommands: state.pendingCommands.length,
    recentEvents: state.events.slice(-10),
  };

  // Add project board data if available
  if (projectBoardSummary && projectBoardSummary.total > 0) {
    snapshot.projectBoard = {
      total: projectBoardSummary.total,
      todo: projectBoardSummary.todo,
      inProgress: projectBoardSummary.inProgress,
      blocked: projectBoardSummary.blocked,
      done: projectBoardSummary.done,
    };
  }

  return snapshot;
}

/**
 * Create a snapshot with project board data (async version)
 */
export async function createSnapshotWithProjectBoard(
  state: MonitoringState,
  repoOwner: string,
  execCommand: ExecCommandFn
): Promise<MonitoringSnapshot> {
  let projectBoardSummary: ProjectBoardSummary | undefined;

  if (state.projectNumber) {
    try {
      projectBoardSummary = await fetchProjectBoardSummary(
        repoOwner,
        state.projectNumber,
        state.parentIssueNumber,
        execCommand
      );
    } catch (error) {
      console.error('Failed to fetch project board for snapshot:', (error as Error).message);
    }
  }

  return createSnapshot(state, projectBoardSummary);
}

// ============================================================================
// Main Monitoring Loop
// ============================================================================

export interface MonitoringCallbacks {
  log: (level: string, message: string, context?: Record<string, unknown>) => void;
  postComment: (body: string) => Promise<void>;
  execCommand: ExecCommandFn;
  onEvent?: (event: MonitoringEvent) => void;
  onCommand?: (command: UserCommand) => Promise<boolean>; // Return true if handled
  /** Called when an agent completes - handles unblocking and triggering next agents */
  onAgentComplete?: (agent: TrackedAgent, state: MonitoringState) => Promise<{ goalAchieved: boolean }>;
  /** Called when user sends /instruct command */
  onInstruct?: (instruction: string, state: MonitoringState) => Promise<{ success: boolean }>;
  /** Called when user sends /queryPM command to ask questions or change direction */
  onQueryPM?: (query: string, state: MonitoringState) => Promise<{ response: string }>;
  /** Called periodically to analyze status and take action using AI */
  onAnalyze?: (state: MonitoringState, previousReport: string | null) => Promise<{ actionTaken: boolean; newReport: string }>;
}

export interface MonitoringLoopResult {
  reason: 'timeout' | 'stopped' | 'all_complete' | 'error' | 'paused';
  finalState: MonitoringState;
  summary: MonitoringSnapshot;
}

/**
 * Run the main monitoring loop
 *
 * This is an async generator that yields events as they occur,
 * allowing the caller to process events and optionally break out of the loop.
 */
export async function* runMonitoringLoop(
  state: MonitoringState,
  repoOwner: string,
  repoName: string,
  callbacks: MonitoringCallbacks,
  workDir: string
): AsyncGenerator<MonitoringEvent, MonitoringLoopResult, void> {
  const { log, postComment, execCommand, onEvent, onCommand, onAgentComplete, onInstruct, onQueryPM, onAnalyze } = callbacks;

  log('INFO', `Starting monitoring loop for session ${state.sessionId}`);

  let lastProcessedCommentId = 0;
  let isPaused = false;
  let pollCount = 0;

  // Initial status post (with project board data)
  const initialSnapshot = await createSnapshotWithProjectBoard(state, repoOwner, execCommand);
  await postComment(formatStatusUpdate(state, initialSnapshot));
  state.lastStatusUpdateAt = new Date().toISOString();

  while (state.isActive) {
    const now = Date.now();

    // Check session timeout
    const sessionDuration = now - new Date(state.startedAt).getTime();
    if (sessionDuration >= config.sessionTimeoutMs) {
      log('INFO', 'Session timeout reached');
      const event = createEvent('session_timeout', state.parentIssueNumber, 'pm', {
        sessionDuration,
      });
      state.events.push(event);
      yield event;

      return {
        reason: 'timeout',
        finalState: state,
        summary: createSnapshot(state),
      };
    }

    // Skip polling if paused
    if (isPaused) {
      await sleep(config.pollIntervalMs);
      continue;
    }

    try {
      // 1. Fetch agents from project board
      // Check every poll (5 minutes) to keep data fresh
      const existingNumbers = new Set(state.trackedAgents.map((a) => a.issueNumber));
      const shouldCheckBoard = true; // Always check - poll interval is already 5 minutes

      if (state.projectNumber && shouldCheckBoard) {
        log('DEBUG', `Checking project board for new agents (poll ${pollCount})`);
        const boardAgents = await fetchAgentsFromProjectBoard(
          repoOwner,
          state.projectNumber,
          state.parentIssueNumber,
          execCommand
        );

        for (const boardAgent of boardAgents) {
          if (existingNumbers.has(boardAgent.issueNumber)) continue;

          const now = new Date().toISOString();
          const newAgent: TrackedAgent = {
            issueNumber: boardAgent.issueNumber,
            issueTitle: boardAgent.title,
            agentType: boardAgent.agentType,
            status: 'pending',
            workflowRunId: null,
            workflowRunUrl: null,
            startedAt: now,
            lastCheckedAt: now,
            lastProgressAt: now,
            conclusion: null,
            errorMessage: null,
          };

          state.trackedAgents.push(newAgent);
          existingNumbers.add(boardAgent.issueNumber);

          const event = createEvent('agent_triggered', newAgent.issueNumber, newAgent.agentType, {
            issueTitle: newAgent.issueTitle,
          });
          state.events.push(event);
          onEvent?.(event);
          yield event;
        }
      }

      // 2. Also check issue labels (fallback, also throttled to every 10th poll)
      if (shouldCheckBoard) {
        const childIssues = await fetchChildIssuesWithAgentLabels(
          repoOwner,
          repoName,
          state.parentIssueNumber,
          execCommand
        );

        const newAgents = detectTriggeredAgents(childIssues, state.trackedAgents);
        for (const agent of newAgents) {
          state.trackedAgents.push(agent);
          const event = createEvent('agent_triggered', agent.issueNumber, agent.agentType, {
            issueTitle: agent.issueTitle,
          });
          state.events.push(event);
          onEvent?.(event);
          yield event;
        }
      }

      // 2b. Scan for running workflows (most reliable - catches agents even after labels removed)
      if (shouldCheckBoard) {
        const runningWorkflows = await fetchRunningAgentWorkflows(
          repoOwner,
          repoName,
          state.parentIssueNumber,
          execCommand
        );

        for (const wf of runningWorkflows) {
          if (existingNumbers.has(wf.issueNumber)) continue;

          const now = new Date().toISOString();
          const newAgent: TrackedAgent = {
            issueNumber: wf.issueNumber,
            issueTitle: wf.title,
            agentType: wf.agentType,
            status: 'running',
            workflowRunId: wf.workflowRunId,
            workflowRunUrl: wf.workflowRunUrl,
            startedAt: now,
            lastCheckedAt: now,
            lastProgressAt: now,
            conclusion: null,
            errorMessage: null,
          };

          state.trackedAgents.push(newAgent);
          existingNumbers.add(wf.issueNumber);
          log('INFO', `Detected running workflow for #${wf.issueNumber} @agent-${wf.agentType}`);

          const event = createEvent('workflow_started', newAgent.issueNumber, newAgent.agentType, {
            issueTitle: newAgent.issueTitle,
            workflowRunId: wf.workflowRunId,
            workflowRunUrl: wf.workflowRunUrl,
          });
          state.events.push(event);
          onEvent?.(event);
          yield event;
        }
      }

      // 3. Update status of tracked agents
      for (let i = 0; i < state.trackedAgents.length; i++) {
        const agent = state.trackedAgents[i];

        // Only check agents that aren't in terminal states
        if (['completed', 'failed', 'cancelled'].includes(agent.status)) continue;

        const workflowRun = await fetchWorkflowRunForIssue(
          repoOwner,
          repoName,
          agent.issueNumber,
          agent.agentType,
          execCommand
        );

        const { agent: updatedAgent, event } = updateAgentStatus(agent, workflowRun);
        state.trackedAgents[i] = updatedAgent;

        if (event) {
          state.events.push(event);
          onEvent?.(event);
          yield event;

          // Handle agent completion - unblock dependent items and trigger next agents
          if (event.type === 'workflow_completed' && onAgentComplete) {
            log('INFO', `Agent @agent-${updatedAgent.agentType} completed - invoking completion handler`);
            try {
              const result = await onAgentComplete(updatedAgent, state);
              if (result.goalAchieved) {
                log('INFO', 'Goal achieved! All work complete.');
                state.isActive = false;
                return {
                  reason: 'all_complete',
                  finalState: state,
                  summary: createSnapshot(state),
                };
              }
            } catch (err) {
              log('ERROR', `Completion handler error: ${(err as Error).message}`);
            }
          }
        }
      }

      // 3. Check for user commands
      const comments = await fetchIssueComments(
        state.parentIssueNumber,
        execCommand
      );

      const commands = parseUserCommands(comments, lastProcessedCommentId);
      if (commands.length > 0) {
        lastProcessedCommentId = Math.max(...commands.map((c) => c.commentId));
      }

      for (const cmd of commands) {
        log('INFO', `Processing command: ${cmd.command} from ${cmd.author}`);

        const event = createEvent('user_command', state.parentIssueNumber, 'pm', {
          command: cmd.command,
          args: cmd.args,
          author: cmd.author,
        });
        state.events.push(event);
        yield event;

        // Handle built-in commands
        let handled = false;

        switch (cmd.command) {
          case '/status': {
            const statusSnapshot = await createSnapshotWithProjectBoard(state, repoOwner, execCommand);
            await postComment(formatStatusUpdate(state, statusSnapshot));
            handled = true;
            break;
          }

          case '/help':
            await postComment(formatHelpMessage());
            handled = true;
            break;

          case '/pause':
            isPaused = true;
            await postComment('Monitoring paused. Use `/resume` to continue.');
            handled = true;
            break;

          case '/resume':
            isPaused = false;
            await postComment('Monitoring resumed.');
            handled = true;
            break;

          case '/stop':
            state.isActive = false;
            await postComment('Monitoring stopped.');
            return {
              reason: 'stopped',
              finalState: state,
              summary: createSnapshot(state),
            };

          case '/extend':
            config.sessionTimeoutMs += 60 * 60 * 1000; // Add 1 hour
            await postComment(`Session extended. New timeout: ${config.sessionTimeoutMs / 60000} minutes total.`);
            handled = true;
            break;

          default:
            // Handle /instruct command
            if (cmd.command.startsWith('/instruct') && onInstruct) {
              const instruction = cmd.rawText.replace(/^\/instruct\s*/i, '').trim();
              if (instruction) {
                log('INFO', `User instruction: ${instruction}`);
                await onInstruct(instruction, state);
                handled = true;
              } else {
                await postComment('Usage: `/instruct <your instruction here>`\n\nExample: `/instruct unblock #237 and trigger agent-developer`');
                handled = true;
              }
            }
            // Handle /queryPM command
            else if (cmd.command.startsWith('/queryPM') && onQueryPM) {
              const query = cmd.rawText.replace(/^\/queryPM\s*/i, '').trim();
              if (query) {
                log('INFO', `User query: ${query}`);
                await postComment(`Received your query. Analyzing...\n\n> ${query}`);
                const result = await onQueryPM(query, state);
                await postComment(`## PM Response\n\n${result.response}`);
                handled = true;
              } else {
                await postComment('Usage: `/queryPM <your question or direction>`\n\nExamples:\n- `/queryPM why is #276 stuck?`\n- `/queryPM skip #277 and move to documentation`\n- `/queryPM what is the current blocker?`');
                handled = true;
              }
            }
            // Let callback handle other custom commands
            else if (onCommand) {
              handled = await onCommand(cmd);
            }
        }

        if (!handled) {
          state.pendingCommands.push(cmd);
        }
      }

      // 4. Post periodic status update (if there are agents OR project board items)
      const timeSinceLastUpdate = now - new Date(state.lastStatusUpdateAt).getTime();
      if (timeSinceLastUpdate >= config.statusUpdateIntervalMs && (state.trackedAgents.length > 0 || state.projectNumber)) {
        const periodicSnapshot = await createSnapshotWithProjectBoard(state, repoOwner, execCommand);
        // Only post if there's something to report
        if (state.trackedAgents.length > 0 || (periodicSnapshot.projectBoard && periodicSnapshot.projectBoard.total > 0)) {
          await postComment(formatStatusUpdate(state, periodicSnapshot));
          state.lastStatusUpdateAt = new Date().toISOString();
        }
      }

      // 4.5. AI-driven status analysis and action (if callback provided)
      const timeSinceLastAnalysis = now - new Date(state.lastAIAnalysisAt).getTime();
      if (onAnalyze && state.projectNumber && timeSinceLastAnalysis >= config.aiAnalysisIntervalMs) {
        log('INFO', 'Running AI status analysis...');
        try {
          const analysisResult = await onAnalyze(state, state.lastAIAnalysisReport);
          state.lastAIAnalysisAt = new Date().toISOString();
          state.lastAIAnalysisReport = analysisResult.newReport;

          if (analysisResult.actionTaken) {
            log('INFO', 'AI took action during status analysis');
            // Save state after AI action
            saveMonitoringState(workDir, state);
          }
        } catch (error) {
          log('ERROR', `AI analysis failed: ${(error as Error).message}`);
        }
      }

      // 5. Check if all agents are complete or if there's nothing to monitor
      const activeAgents = state.trackedAgents.filter(
        (a) => !['completed', 'failed', 'cancelled'].includes(a.status)
      );

      // Exit if no agents to track after initial discovery period (2 poll cycles)
      if (state.trackedAgents.length === 0 && pollCount > 2) {
        log('INFO', 'No agents to track after discovery period - exiting monitoring');
        await postComment(`## Monitoring Complete\n\nNo agents found to monitor. Exiting monitoring mode.\n\nTo restart monitoring, add the \`agent-pm\` label to the issue.`);
        return {
          reason: 'all_complete',
          finalState: state,
          summary: createSnapshot(state),
        };
      }

      if (state.trackedAgents.length > 0 && activeAgents.length === 0) {
        log('INFO', 'All tracked agents have completed - checking for pending work');

        // Before exiting, check if there's more work to do on the project board
        if (state.projectNumber && onAgentComplete) {
          // Get the last completed agent to pass to completion handler
          const lastCompletedAgent = state.trackedAgents
            .filter((a) => a.status === 'completed')
            .sort((a, b) => new Date(b.lastCheckedAt).getTime() - new Date(a.lastCheckedAt).getTime())[0];

          if (lastCompletedAgent) {
            log('INFO', `Invoking completion handler for @agent-${lastCompletedAgent.agentType}`);
            try {
              const result = await onAgentComplete(lastCompletedAgent, state);
              if (result.goalAchieved) {
                log('INFO', 'Goal achieved! All work complete.');
                return {
                  reason: 'all_complete',
                  finalState: state,
                  summary: createSnapshot(state),
                };
              } else {
                // More work was triggered, continue monitoring
                log('INFO', 'More work triggered, continuing monitoring');
                continue;
              }
            } catch (err) {
              log('ERROR', `Completion handler error: ${(err as Error).message}`);
            }
          }
        }

        // No completion handler or no more work found
        return {
          reason: 'all_complete',
          finalState: state,
          summary: createSnapshot(state),
        };
      }

      // Reset error counter on successful poll
      state.consecutiveErrors = 0;

    } catch (error) {
      state.consecutiveErrors++;
      log('ERROR', `Monitoring poll error (${state.consecutiveErrors}/${config.maxConsecutiveErrors}): ${(error as Error).message}`);

      if (state.consecutiveErrors >= config.maxConsecutiveErrors) {
        const event = createEvent('error', state.parentIssueNumber, 'pm', {
          error: (error as Error).message,
          consecutiveErrors: state.consecutiveErrors,
        });
        state.events.push(event);
        yield event;

        return {
          reason: 'error',
          finalState: state,
          summary: createSnapshot(state),
        };
      }
    }

    // Update state
    state.lastPollAt = new Date().toISOString();
    saveMonitoringState(workDir, state);

    // Increment poll counter and wait for next poll
    pollCount++;
    await sleep(config.pollIntervalMs);
  }

  return {
    reason: 'stopped',
    finalState: state,
    summary: createSnapshot(state),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ============================================================================
// Agent Completion Handling
// ============================================================================

export interface CompletionContext {
  repoOwner: string;
  repoName: string;
  parentIssue: {
    number: number;
    title: string;
    body: string;
  };
  projectNumber: number;
  projectId: string;
}

interface ProjectItem {
  id: string;
  issueNumber: number;
  title: string;
  status: string;
  assignedAgent: string | null;
  blockedBy: string | null;
}

/**
 * Handle agent completion - unblock dependent items and trigger next agents.
 * This replaces the logic that was in pm-poll.yml and pm-notify.yml.
 */
export async function handleAgentCompletion(
  completedAgent: TrackedAgent,
  context: CompletionContext,
  callbacks: MonitoringCallbacks
): Promise<{ unblocked: number[]; triggered: number[] }> {
  const { log, execCommand } = callbacks;
  const { repoOwner, projectNumber, projectId } = context;
  const completedIssueNumber = completedAgent.issueNumber;

  log('INFO', `Handling completion of @agent-${completedAgent.agentType} on #${completedIssueNumber}`);

  const unblocked: number[] = [];
  const triggered: number[] = [];

  try {
    // 1. Fetch project items
    const itemsJson = await execCommand(
      `gh project item-list ${projectNumber} --owner ${repoOwner} --format json --limit 200`
    );
    const itemsData = JSON.parse(itemsJson || '{"items":[]}');

    // 2. Fetch field IDs
    const fieldsJson = await execCommand(
      `gh project field-list ${projectNumber} --owner ${repoOwner} --format json`
    );
    const fieldsData = JSON.parse(fieldsJson || '{"fields":[]}');

    const blockedByFieldId = fieldsData.fields?.find((f: { name: string }) => f.name === 'blocked_by')?.id;
    const statusFieldId = fieldsData.fields?.find((f: { name: string }) => f.name === 'Status')?.id;
    const inProgressOptionId = fieldsData.fields
      ?.find((f: { name: string }) => f.name === 'Status')
      ?.options?.find((o: { name: string }) => o.name === 'In Progress')?.id;

    if (!blockedByFieldId) {
      log('WARN', 'No blocked_by field found in project');
      return { unblocked, triggered };
    }

    // 3. Find items blocked by the completed issue
    const items: ProjectItem[] = (itemsData.items || []).map((item: Record<string, unknown>) => ({
      id: item.id as string,
      issueNumber: (item.content as Record<string, unknown>)?.number as number || 0,
      title: (item.title as string) || '',
      status: (item.status as string) || '',
      assignedAgent: (item.assigned_agent as string) || null,
      blockedBy: (item.blocked_by as string) || null,
    }));

    for (const item of items) {
      if (!item.blockedBy || !item.issueNumber) continue;
      if (item.status === 'Done' || item.status === 'In Progress') continue;

      // Check if blocked by the completed issue
      if (item.blockedBy.includes(`#${completedIssueNumber}`)) {
        // Remove the completed issue from blocked_by
        let newBlockedBy = item.blockedBy
          .replace(new RegExp(`#${completedIssueNumber}`, 'g'), '')
          .replace(/,\s*,/g, ',')
          .replace(/^,\s*/, '')
          .replace(/,\s*$/, '')
          .trim();

        // Use space to clear (empty string doesn't work with gh project item-edit)
        const fieldValue = newBlockedBy || ' ';

        log('INFO', `Unblocking #${item.issueNumber} (was blocked by #${completedIssueNumber})`);

        await execCommand(
          `gh project item-edit --id "${item.id}" --project-id "${projectId}" --field-id "${blockedByFieldId}" --text "${fieldValue}"`
        );

        unblocked.push(item.issueNumber);

        // If no more blockers and has assigned agent, trigger it
        if (!newBlockedBy && item.assignedAgent && item.assignedAgent !== '@agent-pm') {
          const agentLabel = item.assignedAgent.replace('@', '');

          // Idempotency check: Don't trigger if already has label or already in progress
          try {
            const issueJson = await execCommand(
              `gh issue view ${item.issueNumber} --repo ${repoOwner}/${context.repoName} --json labels,state`
            );
            const issueData = JSON.parse(issueJson || '{}');
            const labels = (issueData.labels || []).map((l: { name: string }) => l.name);

            // Skip if issue is closed, already has the agent label, or has agent-failed label
            if (issueData.state === 'CLOSED') {
              log('INFO', `Skipping #${item.issueNumber} - issue is closed`);
              continue;
            }
            if (labels.includes(agentLabel)) {
              log('INFO', `Skipping #${item.issueNumber} - already has ${agentLabel} label`);
              continue;
            }
            if (labels.includes('agent-failed')) {
              log('INFO', `Skipping #${item.issueNumber} - has agent-failed label (needs manual intervention)`);
              continue;
            }
          } catch (e) {
            log('WARN', `Could not check issue #${item.issueNumber} state: ${(e as Error).message}`);
          }

          log('INFO', `Triggering ${item.assignedAgent} for #${item.issueNumber}`);

          await execCommand(
            `gh issue edit ${item.issueNumber} --repo ${repoOwner}/${context.repoName} --add-label "${agentLabel}"`
          );

          // Update status to In Progress
          if (statusFieldId && inProgressOptionId) {
            await execCommand(
              `gh project item-edit --id "${item.id}" --project-id "${projectId}" --field-id "${statusFieldId}" --single-select-option-id "${inProgressOptionId}"`
            );
          }

          triggered.push(item.issueNumber);
        }
      }
    }

    log('INFO', `Completion handling done: ${unblocked.length} unblocked, ${triggered.length} triggered`);
    return { unblocked, triggered };

  } catch (error) {
    log('ERROR', `Failed to handle completion: ${(error as Error).message}`);
    return { unblocked, triggered };
  }
}

/**
 * AI-driven completion handling - uses Claude to analyze the situation and decide next steps.
 * This provides intelligent orchestration focused on achieving the end goal.
 */
export async function handleCompletionWithAI(
  completedAgent: TrackedAgent,
  state: MonitoringState,
  context: CompletionContext,
  callbacks: MonitoringCallbacks
): Promise<{ goalAchieved: boolean; summary: string }> {
  const { log, execCommand, postComment } = callbacks;

  log('INFO', `AI analyzing completion of @agent-${completedAgent.agentType} on #${completedAgent.issueNumber}`);

  try {
    // First, do the basic completion handling
    const { unblocked, triggered } = await handleAgentCompletion(completedAgent, context, callbacks);

    // Fetch current board state for AI analysis
    const itemsJson = await execCommand(
      `gh project item-list ${context.projectNumber} --owner ${context.repoOwner} --format json --limit 200`
    );
    const boardItems = JSON.parse(itemsJson || '{"items":[]}');

    // Count statuses
    const items = boardItems.items || [];
    const doneCount = items.filter((i: { status: string }) => i.status === 'Done').length;
    const inProgressCount = items.filter((i: { status: string }) => i.status === 'In Progress').length;
    const todoCount = items.filter((i: { status: string }) => i.status === 'Todo').length;
    const blockedCount = items.filter((i: { blocked_by: string }) => i.blocked_by && i.blocked_by.trim()).length;

    // Check if all done (goal achieved)
    const allDone = items.length > 0 && doneCount === items.length;
    const activeWork = inProgressCount > 0 || triggered.length > 0;

    // Build summary
    let summary = `**@agent-${completedAgent.agentType}** completed work on #${completedAgent.issueNumber}`;
    if (unblocked.length > 0) {
      summary += `\n- Unblocked: ${unblocked.map(n => `#${n}`).join(', ')}`;
    }
    if (triggered.length > 0) {
      summary += `\n- Triggered agents for: ${triggered.map(n => `#${n}`).join(', ')}`;
    }
    summary += `\n\n**Board Status:** ${doneCount} done, ${inProgressCount} in progress, ${todoCount} todo, ${blockedCount} blocked`;

    // Post progress update
    await postComment(`## Agent Completed

${summary}

${allDone ? '**All tasks complete! Goal achieved.**' : activeWork ? 'Work continues...' : 'Waiting for next steps.'}
`);

    // If goal achieved or complex decision needed, use AI
    if (allDone || (!activeWork && todoCount > 0)) {
      const aiDecision = await getAIDecision(
        completedAgent,
        context,
        boardItems,
        state,
        callbacks
      );

      // AI now executes actions directly via Bash tool during its query
      // We just need to check the goal status
      log('INFO', `AI decision: goalAchieved=${aiDecision.goalAchieved}`);

      return { goalAchieved: aiDecision.goalAchieved, summary };
    }

    return { goalAchieved: allDone, summary };

  } catch (error) {
    log('ERROR', `AI completion handling failed: ${(error as Error).message}`);
    return { goalAchieved: false, summary: `Error: ${(error as Error).message}` };
  }
}

/**
 * Get AI decision on next steps
 */
async function getAIDecision(
  completedAgent: TrackedAgent,
  context: CompletionContext,
  boardItems: { items: unknown[] },
  state: MonitoringState,
  callbacks: MonitoringCallbacks
): Promise<{ goalAchieved: boolean; actions: string[] }> {
  const { log } = callbacks;

  const prompt = `You are @agent-pm, an AI project manager orchestrating work toward a goal.

## Parent Issue (The Goal)
**#${context.parentIssue.number}**: ${context.parentIssue.title}

${context.parentIssue.body.substring(0, 2000)}

## Just Completed
@agent-${completedAgent.agentType} finished work on issue #${completedAgent.issueNumber}
Result: ${completedAgent.conclusion || 'success'}

## Initial Board State (verify with tools)
\`\`\`json
${JSON.stringify(boardItems.items, null, 2).substring(0, 3000)}
\`\`\`

## Tracked Agents
${state.trackedAgents.map(a => `- #${a.issueNumber} @agent-${a.agentType}: ${a.status}`).join('\n')}

## Your Task

1. **VERIFY current state** using tools:
   - \`gh issue view <num> --json state,labels,title\` - Check issue status
   - \`gh pr list --search "<issue-num>"\` - Check for PRs
   - \`gh run list --limit 5\` - Check recent workflow runs
   - \`gh project item-list ${context.projectNumber} --owner ${context.repoOwner} --format json\` - Get latest board state

2. **ANALYZE** what the completed agent accomplished:
   - Read the completion comment on issue #${completedAgent.issueNumber}
   - Check if there are any PRs or deliverables

3. **DECIDE** next steps:
   - Is the overall goal achieved? (all required work done)
   - Are there blocked items that can now be unblocked?
   - Which agent should be triggered next?

4. **EXECUTE** necessary actions directly using Bash:
   - Trigger agents: \`gh issue edit <num> --add-label agent-<type>\`
   - Clear blockers: \`gh project item-edit --id <item-id> --project-id ${context.projectId} --field-id <field-id> --text " "\`
   - Update status as needed

5. **REPORT** your findings in this JSON format:
\`\`\`json
{
  "goalAchieved": false,
  "reasoning": "What you verified and why this decision",
  "actionsExecuted": ["list of actions you already executed"]
}
\`\`\`

Rules:
- VERIFY before deciding - use tools to check actual state
- Execute actions directly using Bash tool - don't just list them
- Use " " (space) not "" to clear text fields
- Post a brief summary comment to issue #${context.parentIssue.number} about what you did`;

  try {
    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'us.anthropic.claude-sonnet-4-20250514-v1:0',
          cwd: process.cwd(),
          maxTurns: 15,
          allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 5000,
      log: (msg) => log('INFO', `[AI] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    // Parse JSON from response - AI now executes actions directly via Bash tool
    const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/);
    if (jsonMatch) {
      const decision = JSON.parse(jsonMatch[1]);
      const actionsExecuted = decision.actionsExecuted || decision.actions || [];
      if (actionsExecuted.length > 0) {
        log('INFO', `AI executed ${actionsExecuted.length} actions: ${actionsExecuted.join(', ')}`);
      }
      return {
        goalAchieved: decision.goalAchieved === true,
        actions: [], // Actions already executed by AI via Bash tool
      };
    }

    // Even if no JSON, AI may have executed actions via tools
    log('INFO', 'No JSON response from AI - actions may have been executed via tools');
    return { goalAchieved: false, actions: [] };

  } catch (error) {
    log('ERROR', `AI decision failed: ${(error as Error).message}`);
    return { goalAchieved: false, actions: [] };
  }
}

// ============================================================================
// AI-Driven Status Analysis
// ============================================================================

/**
 * Periodically analyze project status using AI and take action if needed.
 * Reuses the reassessment module to gather comprehensive context including:
 * - Child issues and their status
 * - Workflow runs and their outcomes
 * - Status discrepancies (board vs actual state)
 * - Progress indicators
 */
export async function analyzeStatusWithAI(
  state: MonitoringState,
  context: CompletionContext,
  previousReport: string | null,
  callbacks: MonitoringCallbacks
): Promise<{ actionTaken: boolean; summary: string; newReport: string }> {
  const { log, execCommand, postComment } = callbacks;

  log('INFO', 'Gathering comprehensive context using reassessment module...');

  try {
    // Use reassessment module to gather full context
    const reassessContext = await gatherReassessmentContext(
      String(context.parentIssue.number),
      context.repoOwner,
      context.repoName,
      context.projectNumber,
      execCommand
    );

    // Build current report for comparison
    const currentReport = JSON.stringify({
      childIssues: reassessContext.childIssues.map(i => ({
        number: i.number,
        title: i.title,
        state: i.state,
      })),
      boardItems: reassessContext.projectBoardItems.map(i => ({
        issueNumber: i.issueNumber,
        status: i.status,
        assignedAgent: i.assignedAgent,
      })),
      discrepancies: reassessContext.statusDiscrepancies,
      analysis: reassessContext.analysis,
      timestamp: new Date().toISOString(),
    });

    log('INFO', 'Running AI analysis with full context...');

    // Build prompt with comprehensive context
    const prompt = `You are @agent-pm monitoring issue #${context.parentIssue.number}: "${context.parentIssue.title}"

## Reassessment Analysis Summary
${reassessContext.analysis}

## Status Discrepancies Detected
${reassessContext.statusDiscrepancies.length > 0
  ? reassessContext.statusDiscrepancies.map(d =>
      `- **#${d.issueNumber}**: Board="${d.boardStatus}" but Actual="${d.actualStatus}" ${d.hasFailedLabel ? '(has agent-failed label)' : ''}`
    ).join('\n')
  : 'None - board status matches actual state'}

## Child Issues (${reassessContext.childIssues.length} total)
${reassessContext.childIssues.slice(0, 10).map(i =>
  `- #${i.number} [${i.state}]: ${i.title}`
).join('\n')}

## Project Board Items
${reassessContext.projectBoardItems.slice(0, 10).map(i =>
  `- #${i.issueNumber} | ${i.status} | ${i.assignedAgent || 'unassigned'} | ${i.title}`
).join('\n')}

## Recent Workflow Runs
${reassessContext.workflowRuns.slice(0, 10).map(w =>
  `- #${w.issueNumber} ${w.workflowName}: ${w.conclusion || w.status} (run ${w.runId})`
).join('\n')}

## Previous Analysis (5 minutes ago)
${previousReport ? (() => {
  try {
    const prev = JSON.parse(previousReport);
    return `**Expected by now**: ${prev.expectations || 'No expectations set'}
**Previous status**: ${prev.progressStatus || 'unknown'}`;
  } catch { return 'First analysis'; }
})() : 'First analysis'}

## Your Task
1. **CHECK EXPECTATIONS**: Did what was expected actually happen? If not, why?
2. **ASSESS CURRENT STATE**: Is progress happening or is something stuck/failed?
3. **IDENTIFY ISSUES**: Any discrepancies, failed workflows, or blocked tasks?
4. **ACT** if needed:
   - Fix status mismatch: \`gh project item-edit --id <ITEM_ID> --project-id ${context.projectId} --field-id <STATUS_FIELD_ID> --single-select-option-id <OPTION_ID>\`
   - Retry failed agent: \`gh issue edit #N --add-label "agent-<type>"\`
   - Clear blocker: Update blocked_by field
5. **SET EXPECTATIONS**: What should happen in the next 5 minutes?
6. **REPORT** in JSON:

\`\`\`json
{
  "progressStatus": "healthy|stuck|failed|blocked",
  "expectationsMet": true/false/null,
  "expectationsAnalysis": "Did previous expectations happen? Why or why not?",
  "actionTaken": true/false,
  "summary": "Current state and what you did",
  "expectations": "What SHOULD happen in the next 5 minutes (be specific: e.g., '#276 workflow should complete', '@agent-operations should start on #277')",
  "alertLevel": "normal|warning|critical"
}
\`\`\`

Rules:
- **ALWAYS set expectations** - be specific about what should happen next
- If expectations weren't met, investigate WHY and take action
- Fix discrepancies automatically (board status doesn't match reality)
- Retry failed agents that aren't actively running
- alertLevel: normal=progressing, warning=slower than expected, critical=stuck/failed
- Post comment to #${context.parentIssue.number} ONLY if alertLevel is warning/critical`;

    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'us.anthropic.claude-sonnet-4-20250514-v1:0',
          cwd: process.cwd(),
          maxTurns: 15,
          allowedTools: ['Bash', 'Read', 'Glob', 'Grep'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 5000,
      log: (msg) => log('INFO', `[AI-Monitor] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    // Parse response
    const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/);
    if (jsonMatch) {
      const result = JSON.parse(jsonMatch[1]);
      log('INFO', `AI analysis: progress=${result.progressStatus}, actionTaken=${result.actionTaken}, alert=${result.alertLevel}`);

      if (result.expectationsMet === false) {
        log('WARN', `Expectations NOT met: ${result.expectationsAnalysis}`);
      }

      // Store expectations in report for next analysis
      const reportWithExpectations = JSON.stringify({
        ...JSON.parse(currentReport),
        progressStatus: result.progressStatus,
        expectations: result.expectations,
        alertLevel: result.alertLevel,
        timestamp: new Date().toISOString(),
      });

      return {
        actionTaken: result.actionTaken === true,
        summary: result.summary || 'Analysis complete',
        newReport: reportWithExpectations,
      };
    }

    return { actionTaken: false, summary: 'Analysis complete', newReport: currentReport };

  } catch (error) {
    log('ERROR', `AI status analysis failed: ${(error as Error).message}`);
    return { actionTaken: false, summary: `Error: ${(error as Error).message}`, newReport: '' };
  }
}

// ============================================================================
// User Query Handling
// ============================================================================

/**
 * Handle user query via /queryPM command.
 * Allows humans to ask questions, get explanations, or change direction.
 */
export async function handleQueryPM(
  query: string,
  state: MonitoringState,
  context: CompletionContext,
  callbacks: MonitoringCallbacks
): Promise<{ response: string }> {
  const { log, execCommand } = callbacks;

  log('INFO', `Handling user query: ${query}`);

  try {
    // Gather current context using reassessment
    const reassessContext = await gatherReassessmentContext(
      String(context.parentIssue.number),
      context.repoOwner,
      context.repoName,
      context.projectNumber,
      execCommand
    );

    const prompt = `You are @agent-pm responding to a human's question about issue #${context.parentIssue.number}: "${context.parentIssue.title}"

## Human's Query
${query}

## Current Project State
${reassessContext.analysis}

## Board Items
${reassessContext.projectBoardItems.slice(0, 10).map(i =>
  `- #${i.issueNumber} | ${i.status} | ${i.assignedAgent || 'unassigned'} | ${i.title}`
).join('\n')}

## Recent Workflow Runs
${reassessContext.workflowRuns.slice(0, 5).map(w =>
  `- #${w.issueNumber} ${w.workflowName}: ${w.conclusion || w.status}`
).join('\n')}

## Status Discrepancies
${reassessContext.statusDiscrepancies.length > 0
  ? reassessContext.statusDiscrepancies.map(d => `- #${d.issueNumber}: Board="${d.boardStatus}" vs Actual="${d.actualStatus}"`).join('\n')
  : 'None'}

## Your Task
1. **ANSWER** the human's query clearly and concisely
2. If they're asking to change direction, explain what that would involve
3. If they're asking about a problem, diagnose it and suggest solutions
4. If they want to take action, explain what commands they should use

Respond directly - no JSON needed. Be helpful and specific.`;

    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'us.anthropic.claude-sonnet-4-20250514-v1:0',
          cwd: process.cwd(),
          maxTurns: 10,
          allowedTools: ['Bash', 'Read', 'Glob', 'Grep'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 5000,
      log: (msg) => log('INFO', `[AI-Query] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    return { response: response || 'Unable to generate response.' };

  } catch (error) {
    log('ERROR', `Query handling failed: ${(error as Error).message}`);
    return { response: `Error processing query: ${(error as Error).message}` };
  }
}

// ============================================================================
// User Instruction Execution
// ============================================================================

/**
 * Execute a user instruction using AI
 */
export async function executeUserInstruction(
  instruction: string,
  state: MonitoringState,
  context: CompletionContext,
  callbacks: MonitoringCallbacks
): Promise<{ success: boolean; summary: string }> {
  const { log, execCommand, postComment } = callbacks;

  log('INFO', `Executing user instruction: ${instruction}`);

  try {
    // Fetch current board state
    const itemsJson = await execCommand(
      `gh project item-list ${context.projectNumber} --owner ${context.repoOwner} --format json --limit 200`
    );
    const boardItems = JSON.parse(itemsJson || '{"items":[]}');

    // Fetch field IDs for the prompt
    const fieldsJson = await execCommand(
      `gh project field-list ${context.projectNumber} --owner ${context.repoOwner} --format json`
    );
    const fieldsData = JSON.parse(fieldsJson || '{"fields":[]}');

    const prompt = `You are @agent-pm, an AI project manager. The user has given you an instruction to execute.

## User Instruction
${instruction}

## Parent Issue (Goal)
**#${context.parentIssue.number}**: ${context.parentIssue.title}

## Initial Context (verify with tools as needed)

**Board State:**
\`\`\`json
${JSON.stringify(boardItems.items, null, 2).substring(0, 3000)}
\`\`\`

**Project Fields:**
\`\`\`json
${JSON.stringify(fieldsData.fields, null, 2).substring(0, 1500)}
\`\`\`

**Project Info:**
- Owner: ${context.repoOwner}
- Repo: ${context.repoName}
- Project Number: ${context.projectNumber}
- Project ID: ${context.projectId}

**Tracked Agents:**
${state.trackedAgents.map(a => `- #${a.issueNumber} @agent-${a.agentType}: ${a.status}`).join('\n') || 'None'}

## Your Task

1. **UNDERSTAND** the user's instruction - if unclear, check related issues/PRs for context

2. **VERIFY** current state using tools:
   - \`gh issue view <num> --json state,labels,body\` - Check issue details
   - \`gh pr list --search "<query>"\` - Find related PRs
   - \`gh run list --workflow <name>\` - Check workflow status
   - \`gh project item-list ${context.projectNumber} --owner ${context.repoOwner} --format json\` - Latest board state

3. **EXECUTE** the instruction directly using Bash tool:
   - Trigger agents: \`gh issue edit <num> --add-label agent-<type>\`
   - Update board: \`gh project item-edit --id <item-id> --project-id ${context.projectId} --field-id <field-id> --text " "\`
   - Close issues: \`gh issue close <num>\`
   - Any other gh CLI commands needed

4. **POST** a comment to issue #${context.parentIssue.number} summarizing what you did

5. **REPORT** your findings:
\`\`\`json
{
  "understood": "What the user asked for",
  "actionsExecuted": ["list of commands you ran"],
  "summary": "What was accomplished"
}
\`\`\`

Rules:
- Execute actions DIRECTLY using Bash tool - don't just list them
- Use " " (space) not "" to clear text fields
- Verify state before and after making changes
- If the instruction is ambiguous, read related issues/PRs for context`;

    let response = '';
    for await (const event of resilientQuery({
      queryParams: {
        prompt,
        options: {
          model: process.env.ANTHROPIC_MODEL || 'us.anthropic.claude-sonnet-4-20250514-v1:0',
          cwd: process.cwd(),
          maxTurns: 20,
          allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch'],
          permissionMode: 'bypassPermissions',
        },
      },
      maxRetries: 2,
      baseDelayMs: 5000,
      log: (msg) => log('INFO', `[AI Instruct] ${msg}`),
    })) {
      if (event.type === 'assistant' && event.message?.content) {
        for (const block of event.message.content) {
          if (block.type === 'text') {
            response += block.text;
          }
        }
      }
    }

    // Parse JSON from response - AI now executes actions directly via Bash tool
    // and posts its own comments to the issue
    const jsonMatch = response.match(/```json\s*([\s\S]*?)\s*```/);
    if (jsonMatch) {
      const result = JSON.parse(jsonMatch[1]);
      const actionsExecuted = result.actionsExecuted || result.actions || [];

      log('INFO', `AI understood: ${result.understood}`);
      log('INFO', `AI executed ${actionsExecuted.length} actions`);
      log('INFO', `AI summary: ${result.summary}`);

      return { success: true, summary: result.summary || 'Done' };
    }

    // Even without JSON response, AI may have executed the instruction via tools
    log('INFO', 'No JSON response - AI may have executed instruction via tools');
    return { success: true, summary: 'Instruction processed (no structured response)' };

  } catch (error) {
    const errMsg = (error as Error).message;
    log('ERROR', `Instruction execution failed: ${errMsg}`);
    await postComment(`Failed to execute instruction: ${errMsg}`);
    return { success: false, summary: errMsg };
  }
}

// ============================================================================
// Convenience Functions
// ============================================================================

/**
 * Start monitoring from scratch or resume existing session
 */
export async function startOrResumeMonitoring(
  parentIssueNumber: number,
  projectNumber: number | null,
  repoOwner: string,
  repoName: string,
  workDir: string,
  callbacks: MonitoringCallbacks
): Promise<MonitoringLoopResult> {
  // Try to load existing state
  let state = loadMonitoringState(workDir);

  if (state && state.isActive && state.parentIssueNumber === parentIssueNumber) {
    callbacks.log('INFO', `Resuming monitoring session ${state.sessionId}`);
    await callbacks.postComment(`Resuming monitoring session from previous run.`);
  } else {
    // Create new state
    state = createInitialState(parentIssueNumber, projectNumber);
    callbacks.log('INFO', `Starting new monitoring session ${state.sessionId}`);
  }

  // Run the monitoring loop
  const generator = runMonitoringLoop(state, repoOwner, repoName, callbacks, workDir);

  // Process all events until completion
  let result: IteratorResult<MonitoringEvent, MonitoringLoopResult>;
  do {
    result = await generator.next();
    if (!result.done && result.value) {
      callbacks.log('INFO', `Event: ${result.value.type} for #${result.value.issueNumber}`);
    }
  } while (!result.done);

  // Post final status
  await callbacks.postComment(`## Monitoring Session Complete

**Reason**: ${result.value.reason}
**Duration**: ${Math.round((Date.now() - new Date(state.startedAt).getTime()) / 60000)} minutes

### Final Summary
- Completed: ${result.value.summary.completedAgents}
- Failed: ${result.value.summary.failedAgents}
- Stuck: ${result.value.summary.stuckAgents}

To restart monitoring, add the \`agent-pm\` label again.
`);

  return result.value;
}

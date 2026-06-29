/**
 * Beads Integration Module for ADP Agents
 *
 * Provides a unified interface for all agents to interact with Beads (bd),
 * the distributed graph issue tracker powered by Dolt.
 *
 * Features:
 * - Shared state across all agents (PM, developer, architect, operations, reviewer)
 * - S3 remote storage for persistence across runner restarts
 * - Atomic task claiming to prevent race conditions
 * - Instant ready-work detection (no polling needed)
 * - Typed dependencies (blocks, parent-child, discovered-from)
 *
 * Architecture:
 *   S3 (s3://adp-agent-state/beads/) ←→ Local .beads/dolt/ ←→ All Agents
 */

import { execSync } from 'child_process';

// ============================================================================
// Configuration
// ============================================================================

export interface BeadsConfig {
  enabled: boolean;
  s3Bucket: string;
  s3Region: string;
  s3Path: string;
  syncOnStart: boolean;
  syncOnComplete: boolean;
  fallbackToGitHub: boolean;  // If beads fails, fall back to GitHub Projects
}

const DEFAULT_CONFIG: BeadsConfig = {
  enabled: true,
  s3Bucket: 'adp-agent-state',
  s3Region: 'us-west-2',
  s3Path: 'beads/adp',
  syncOnStart: true,
  syncOnComplete: true,
  fallbackToGitHub: true,
};

let config: BeadsConfig = { ...DEFAULT_CONFIG };

export function configureBeads(newConfig: Partial<BeadsConfig>): void {
  config = { ...config, ...newConfig };
}

export function isBeadsEnabled(): boolean {
  return config.enabled;
}

export function getBeadsConfig(): BeadsConfig {
  return { ...config };
}

// ============================================================================
// Types
// ============================================================================

export interface BeadsTask {
  id: string;                    // bd-a3f8 or bd-a3f8.1
  title: string;
  description: string;
  status: 'open' | 'in_progress' | 'closed';
  priority: number;              // 0 = highest
  type: 'task' | 'epic' | 'bug' | 'feature';
  assignee: string | null;
  labels: string[];
  blockedBy: string[];           // IDs of blocking tasks
  blocks: string[];              // IDs this task blocks
  parentId: string | null;       // For hierarchical tasks
  githubIssue: number | null;    // Link to GitHub issue
  createdAt: string;
  updatedAt: string;
  closedAt: string | null;
  closeReason: string | null;
}

export interface BeadsDependency {
  fromId: string;
  toId: string;
  type: 'blocks' | 'related' | 'parent-child' | 'discovered-from';
}

export interface BeadsEpic {
  id: string;
  title: string;
  tasks: BeadsTask[];
  completedCount: number;
  totalCount: number;
}

export interface BeadsReadyWork {
  tasks: BeadsTask[];
  count: number;
}

// ============================================================================
// Core Operations
// ============================================================================

type LogFn = (level: string, message: string, context?: Record<string, unknown>) => void;

let logFn: LogFn = (level, message) => console.log(`[${level}] ${message}`);

export function setLogger(fn: LogFn): void {
  logFn = fn;
}

function log(level: string, message: string, context?: Record<string, unknown>): void {
  logFn(level, message, context);
}

/**
 * Execute a bd command and return the result
 */
async function bd(
  args: string,
  options: { json?: boolean; cwd?: string } = {}
): Promise<string> {
  const { json = true, cwd = process.cwd() } = options;
  const jsonFlag = json ? ' --json' : '';
  const command = `bd ${args}${jsonFlag}`;

  try {
    const result = execSync(command, {  // nosemgrep: detect-child-process
      cwd,
      encoding: 'utf-8',
      env: {
        ...process.env,
        AWS_REGION: config.s3Region,
      },
      maxBuffer: 10 * 1024 * 1024,
    }).trim();

    return result;
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    log('ERROR', `bd command failed: ${command}`, { error: err.stderr || err.message });
    throw error;
  }
}

/**
 * Check if beads is installed and initialized
 */
export async function isBeadsAvailable(): Promise<boolean> {
  try {
    execSync('bd version', { encoding: 'utf-8', stdio: 'pipe' });
    return true;
  } catch {
    return false;
  }
}

/**
 * Check if beads is initialized in the current directory
 */
export async function isBeadsInitialized(cwd: string = process.cwd()): Promise<boolean> {
  try {
    const fs = await import('fs');

    // Check if .beads/dolt directory exists (the actual Dolt database)
    const beadsDoltPath = `${cwd}/.beads/dolt`;
    const beadsPath = `${cwd}/.beads`;

    if (fs.existsSync(beadsDoltPath)) {
      log('INFO', `isBeadsInitialized: Found .beads/dolt in ${cwd}`);

      // Verify bd can read the database by running a simple command
      try {
        execSync('bd ready --json', { cwd, encoding: 'utf-8', stdio: 'pipe' });
        log('INFO', 'isBeadsInitialized: bd ready succeeded, Beads is working');
        return true;
      } catch {
        log('WARN', 'isBeadsInitialized: .beads/dolt exists but bd ready failed');
        return false;
      }
    }

    // Fallback: check if .beads exists at all
    if (fs.existsSync(beadsPath)) {
      log('INFO', `isBeadsInitialized: Found .beads but no dolt subdirectory`);
      return false;
    }

    log('INFO', `isBeadsInitialized: .beads directory not found in ${cwd}`);
    return false;
  } catch (err) {
    log('WARN', `isBeadsInitialized failed: ${(err as Error).message}`);
    return false;
  }
}

/**
 * Initialize beads in the current directory
 */
export async function initializeBeads(cwd: string = process.cwd()): Promise<void> {
  log('INFO', 'Initializing beads...');

  // Initialize with stealth mode (don't commit .beads to git)
  await bd('init --stealth --quiet', { json: false, cwd });

  // Configure S3 remote if bucket is set
  if (config.s3Bucket) {
    const remoteUrl = `aws://${config.s3Bucket}/${config.s3Path}`;
    log('INFO', `Configuring S3 remote: ${remoteUrl}`);

    try {
      // Add S3 remote using bd dolt wrapper (not raw dolt)
      await bd(`dolt remote add origin ${remoteUrl}`, { json: false, cwd });
    } catch (error) {
      // Remote might already exist
      log('WARN', 'Remote may already exist, continuing...');
    }
  }

  log('INFO', 'Beads initialized successfully');
}

/**
 * Sync with remote (pull latest state)
 */
export async function syncPull(cwd: string = process.cwd()): Promise<void> {
  if (!config.s3Bucket) {
    log('WARN', 'No S3 bucket configured, skipping sync');
    return;
  }

  log('INFO', 'Pulling latest state from S3...');
  try {
    await bd('dolt pull', { json: false, cwd });
    log('INFO', 'Sync pull complete');
  } catch (error) {
    log('WARN', `Sync pull failed (may be first run): ${(error as Error).message}`);
  }
}

/**
 * Sync with remote (push current state)
 */
export async function syncPush(cwd: string = process.cwd()): Promise<void> {
  if (!config.s3Bucket) {
    log('WARN', 'No S3 bucket configured, skipping sync');
    return;
  }

  log('INFO', 'Pushing state to S3...');
  try {
    await bd('dolt push', { json: false, cwd });
    log('INFO', 'Sync push complete');
  } catch (error) {
    log('WARN', `Sync push failed: ${(error as Error).message}`);
  }
}

// ============================================================================
// Task Operations
// ============================================================================

/**
 * Get all ready (unblocked) tasks
 */
export async function getReadyTasks(cwd: string = process.cwd()): Promise<BeadsTask[]> {
  const result = await bd('ready', { cwd });
  const data = JSON.parse(result);
  return data.issues || data.tasks || [];
}

/**
 * Get a specific task by ID
 */
export async function getTask(taskId: string, cwd: string = process.cwd()): Promise<BeadsTask | null> {
  try {
    const result = await bd(`show ${taskId}`, { cwd });
    return JSON.parse(result);
  } catch {
    return null;
  }
}

/**
 * List all tasks (optionally filtered)
 */
export async function listTasks(
  filter?: { status?: string; assignee?: string; type?: string },
  cwd: string = process.cwd()
): Promise<BeadsTask[]> {
  let args = 'list';
  if (filter?.status) args += ` --status ${filter.status}`;
  if (filter?.assignee) args += ` --assignee ${filter.assignee}`;
  if (filter?.type) args += ` --type ${filter.type}`;

  const result = await bd(args, { cwd });
  const data = JSON.parse(result);
  return data.issues || data.tasks || [];
}

/**
 * Create a new task
 */
export async function createTask(
  title: string,
  options: {
    description?: string;
    priority?: number;
    type?: 'task' | 'epic' | 'bug' | 'feature';
    assignee?: string;
    labels?: string[];
    blockedBy?: string[];
    githubIssue?: number;
  } = {},
  cwd: string = process.cwd()
): Promise<BeadsTask> {
  let args = `create "${title}"`;

  if (options.priority !== undefined) args += ` -p ${options.priority}`;
  if (options.type) args += ` -t ${options.type}`;
  if (options.assignee) args += ` --assignee ${options.assignee}`;
  if (options.labels?.length) args += ` --labels ${options.labels.join(',')}`;
  if (options.description) {
    // Write description to temp file to avoid shell escaping issues
    const fs = require('fs');
    const tmpFile = `/tmp/bd-desc-${Date.now()}.txt`;
    fs.writeFileSync(tmpFile, options.description);
    args += ` --description-file ${tmpFile}`;
  }

  const result = await bd(args, { cwd });
  const task = JSON.parse(result);

  // Add dependencies if specified
  if (options.blockedBy?.length) {
    for (const blockerId of options.blockedBy) {
      await addDependency(task.id, blockerId, 'blocks', cwd);
    }
  }

  // Store GitHub issue reference in metadata
  if (options.githubIssue) {
    await bd(`update ${task.id} --metadata github_issue=${options.githubIssue}`, { cwd });
  }

  return task;
}

/**
 * Create an epic with subtasks
 */
export async function createEpic(
  title: string,
  subtasks: Array<{ title: string; priority?: number; blockedBy?: string[] }>,
  options: { priority?: number; githubIssue?: number } = {},
  cwd: string = process.cwd()
): Promise<BeadsEpic> {
  // Create the epic
  const epic = await createTask(title, {
    type: 'epic',
    priority: options.priority ?? 1,
    githubIssue: options.githubIssue,
  }, cwd);

  log('INFO', `Created epic: ${epic.id}`);

  // Create subtasks
  const tasks: BeadsTask[] = [epic];
  for (const subtask of subtasks) {
    const task = await createTask(subtask.title, {
      priority: subtask.priority ?? 1,
    }, cwd);
    tasks.push(task);

    // Add parent-child relationship
    await addDependency(task.id, epic.id, 'parent-child', cwd);

    // Add blockers
    if (subtask.blockedBy?.length) {
      for (const blockerId of subtask.blockedBy) {
        await addDependency(task.id, blockerId, 'blocks', cwd);
      }
    }

    log('INFO', `Created subtask: ${task.id} (${subtask.title})`);
  }

  return {
    id: epic.id,
    title,
    tasks,
    completedCount: 0,
    totalCount: tasks.length,
  };
}

/**
 * Atomically claim a task (sets assignee + status to in_progress)
 */
export async function claimTask(
  taskId: string,
  assignee?: string,
  cwd: string = process.cwd()
): Promise<BeadsTask> {
  let args = `update ${taskId} --claim`;
  if (assignee) args += ` --assignee ${assignee}`;

  const result = await bd(args, { cwd });
  return JSON.parse(result);
}

/**
 * Update a task's status
 */
export async function updateTaskStatus(
  taskId: string,
  status: 'open' | 'in_progress' | 'closed',
  cwd: string = process.cwd()
): Promise<BeadsTask> {
  const result = await bd(`update ${taskId} --status ${status}`, { cwd });
  return JSON.parse(result);
}

/**
 * Close a task with a reason
 */
export async function closeTask(
  taskId: string,
  reason: string,
  cwd: string = process.cwd()
): Promise<BeadsTask> {
  const result = await bd(`close ${taskId} --reason "${reason}"`, { cwd });
  return JSON.parse(result);
}

/**
 * Add a dependency between tasks
 */
export async function addDependency(
  fromId: string,
  toId: string,
  type: 'blocks' | 'related' | 'parent-child' | 'discovered-from' = 'blocks',
  cwd: string = process.cwd()
): Promise<void> {
  await bd(`dep add ${fromId} ${toId} --type ${type}`, { json: false, cwd });
}

/**
 * Remove a dependency between tasks
 */
export async function removeDependency(
  fromId: string,
  toId: string,
  cwd: string = process.cwd()
): Promise<void> {
  await bd(`dep remove ${fromId} ${toId}`, { json: false, cwd });
}

// ============================================================================
// Agent-Specific Operations
// ============================================================================

/**
 * Find a beads task linked to a GitHub issue
 */
export async function findTaskByGitHubIssue(
  issueNumber: number,
  cwd: string = process.cwd()
): Promise<BeadsTask | null> {
  try {
    const tasks = await listTasks({}, cwd);
    // Search for task with matching GitHub issue in metadata or title
    for (const task of tasks) {
      // Check metadata (cast through unknown to access dynamic properties)
      if ((task as unknown as Record<string, unknown>).github_issue === issueNumber) {
        return task;
      }
      // Check title for #N pattern
      if (task.title.includes(`#${issueNumber}`)) {
        return task;
      }
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Get context for Claude (bd prime equivalent)
 */
export async function getPrimeContext(cwd: string = process.cwd()): Promise<string> {
  try {
    const result = await bd('prime', { json: false, cwd });
    return result;
  } catch {
    // Fallback: construct context manually
    const ready = await getReadyTasks(cwd);
    const inProgress = await listTasks({ status: 'in_progress' }, cwd);

    let context = '## Current Work State (Beads)\n\n';
    context += `### Ready Tasks (${ready.length})\n`;
    for (const task of ready.slice(0, 5)) {
      context += `- ${task.id}: ${task.title} (P${task.priority})\n`;
    }
    context += `\n### In Progress (${inProgress.length})\n`;
    for (const task of inProgress.slice(0, 5)) {
      context += `- ${task.id}: ${task.title} (${task.assignee || 'unassigned'})\n`;
    }
    return context;
  }
}

// ============================================================================
// Workflow Helpers
// ============================================================================

/**
 * Start work on a task (sync, claim, return context)
 * Used by all agents at the start of their workflow
 */
export async function startWork(
  taskIdOrIssueNumber: string | number,
  agentName: string,
  cwd: string = process.cwd()
): Promise<{ task: BeadsTask; context: string } | null> {
  if (!config.enabled) {
    log('INFO', 'Beads disabled, skipping startWork');
    return null;
  }

  // Ensure beads is initialized
  if (!(await isBeadsInitialized(cwd))) {
    await initializeBeads(cwd);
  }

  // Sync if configured
  if (config.syncOnStart) {
    await syncPull(cwd);
  }

  // Find the task
  let task: BeadsTask | null = null;
  if (typeof taskIdOrIssueNumber === 'number') {
    task = await findTaskByGitHubIssue(taskIdOrIssueNumber, cwd);
  } else {
    task = await getTask(taskIdOrIssueNumber, cwd);
  }

  if (!task) {
    log('WARN', `Task not found: ${taskIdOrIssueNumber}`);
    return null;
  }

  // Claim the task
  const claimedTask = await claimTask(task.id, agentName, cwd);

  // Get context
  const context = await getPrimeContext(cwd);

  log('INFO', `Agent ${agentName} claimed task ${claimedTask.id}`);

  return { task: claimedTask, context };
}

/**
 * Complete work on a task (close, sync)
 * Used by all agents at the end of their workflow
 */
export async function completeWork(
  taskId: string,
  reason: string,
  cwd: string = process.cwd()
): Promise<BeadsTask | null> {
  if (!config.enabled) {
    log('INFO', 'Beads disabled, skipping completeWork');
    return null;
  }

  // Close the task
  const task = await closeTask(taskId, reason, cwd);

  // Sync if configured
  if (config.syncOnComplete) {
    await syncPush(cwd);
  }

  log('INFO', `Task ${taskId} completed: ${reason}`);

  return task;
}

/**
 * Report failure on a task
 */
export async function reportFailure(
  taskId: string,
  errorMessage: string,
  cwd: string = process.cwd()
): Promise<void> {
  if (!config.enabled) {
    return;
  }

  try {
    // Add failure label
    await bd(`update ${taskId} --labels +agent-failed`, { cwd });
    // Add comment with error
    await bd(`comment ${taskId} "Agent failed: ${errorMessage}"`, { json: false, cwd });
    // Sync
    if (config.syncOnComplete) {
      await syncPush(cwd);
    }
  } catch (error) {
    log('ERROR', `Failed to report failure: ${(error as Error).message}`);
  }
}

// ============================================================================
// PM-Specific Operations
// ============================================================================

/**
 * Create project structure from GitHub issue
 * Used by PM when starting a new project
 */
export async function createProjectFromIssue(
  issueNumber: number,
  issueTitle: string,
  tasks: Array<{
    title: string;
    agent: string;
    priority?: number;
    blockedBy?: number[];  // Indices of tasks that block this one
  }>,
  cwd: string = process.cwd()
): Promise<BeadsEpic> {
  log('INFO', `Creating beads project for issue #${issueNumber}`);

  // Create epic
  const epic = await createTask(`#${issueNumber} - ${issueTitle}`, {
    type: 'epic',
    priority: 1,
    githubIssue: issueNumber,
  }, cwd);

  const createdTasks: BeadsTask[] = [epic];

  // Create tasks
  for (let i = 0; i < tasks.length; i++) {
    const taskDef = tasks[i];
    const task = await createTask(taskDef.title, {
      priority: taskDef.priority ?? 1,
      assignee: taskDef.agent,
      labels: [`agent-${taskDef.agent.replace('@agent-', '')}`],
    }, cwd);

    createdTasks.push(task);

    // Add parent-child relationship to epic
    await addDependency(task.id, epic.id, 'parent-child', cwd);

    // Add blockers (convert indices to task IDs)
    if (taskDef.blockedBy?.length) {
      for (const blockerIndex of taskDef.blockedBy) {
        const blockerId = createdTasks[blockerIndex + 1]?.id; // +1 because epic is at index 0
        if (blockerId) {
          await addDependency(task.id, blockerId, 'blocks', cwd);
        }
      }
    }

    log('INFO', `Created task ${task.id}: ${taskDef.title} (${taskDef.agent})`);
  }

  // Sync to S3
  await syncPush(cwd);

  return {
    id: epic.id,
    title: issueTitle,
    tasks: createdTasks,
    completedCount: 0,
    totalCount: createdTasks.length,
  };
}

/**
 * Get project status for PM monitoring
 */
export async function getProjectStatus(
  epicId: string,
  cwd: string = process.cwd()
): Promise<{
  epic: BeadsTask;
  tasks: BeadsTask[];
  ready: BeadsTask[];
  inProgress: BeadsTask[];
  completed: BeadsTask[];
  blocked: BeadsTask[];
}> {
  // Get epic
  const epic = await getTask(epicId, cwd);
  if (!epic) {
    throw new Error(`Epic not found: ${epicId}`);
  }

  // Get all tasks (children of epic)
  const allTasks = await listTasks({}, cwd);
  const tasks = allTasks.filter(t =>
    t.id.startsWith(epicId.split('.')[0]) && t.id !== epicId
  );

  // Categorize
  const ready = await getReadyTasks(cwd);
  const readyIds = new Set(ready.map(t => t.id));

  const inProgress = tasks.filter(t => t.status === 'in_progress');
  const completed = tasks.filter(t => t.status === 'closed');
  const blocked = tasks.filter(t =>
    t.status === 'open' && !readyIds.has(t.id)
  );
  const readyTasks = tasks.filter(t => readyIds.has(t.id));

  return {
    epic,
    tasks,
    ready: readyTasks,
    inProgress,
    completed,
    blocked,
  };
}

// ============================================================================
// Installation & Setup
// ============================================================================

/**
 * Install beads CLI (for use in workflow setup)
 */
export async function installBeadsCLI(): Promise<boolean> {
  try {
    // Check if already installed
    if (await isBeadsAvailable()) {
      log('INFO', 'Beads CLI already installed');
      return true;
    }

    log('INFO', 'Installing beads CLI...');
    execSync(
      'curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash',
      { encoding: 'utf-8', stdio: 'inherit' }
    );

    return await isBeadsAvailable();
  } catch (error) {
    log('ERROR', `Failed to install beads: ${(error as Error).message}`);
    return false;
  }
}

/**
 * Setup beads for ADP (initialize + configure S3)
 */
export async function setupBeadsForADP(
  cwd: string = process.cwd(),
  s3Config?: { bucket: string; region: string; path: string }
): Promise<boolean> {
  try {
    // Install if needed
    if (!(await isBeadsAvailable())) {
      const installed = await installBeadsCLI();
      if (!installed) {
        return false;
      }
    }

    // Configure S3 if provided
    if (s3Config) {
      configureBeads({
        s3Bucket: s3Config.bucket,
        s3Region: s3Config.region,
        s3Path: s3Config.path,
      });
    }

    // Initialize
    if (!(await isBeadsInitialized(cwd))) {
      await initializeBeads(cwd);
    }

    // Try to pull existing state
    await syncPull(cwd);

    log('INFO', 'Beads setup complete for ADP');
    return true;
  } catch (error) {
    log('ERROR', `Beads setup failed: ${(error as Error).message}`);
    return false;
  }
}

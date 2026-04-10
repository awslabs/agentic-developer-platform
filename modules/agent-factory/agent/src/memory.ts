/**
 * Agent Memory System
 *
 * Persistent context storage for agents using a dedicated 'adp' orphan branch.
 * This branch lives in each target repo and stores agent context files that
 * persist across runs — no PRs needed, no branch protection conflicts.
 *
 * Architecture:
 *   target-repo/adp branch (orphan)
 *     └── agent_context/
 *         ├── components/<name>/issue-<N>_<timestamp>.md
 *         └── agents/<persona>/run_issue-<N>_<timestamp>.md
 */

import { execSync } from 'child_process';
import * as path from 'path';

// ============================================================================
// Types
// ============================================================================

export interface MemoryConfig {
  cwd: string;
  agentType: string;
  issueNumber: string;
  /** Maximum number of context files to load per folder (most recent first) */
  maxFilesPerFolder?: number;
  /** Logger function */
  log?: (level: string, message: string, context?: Record<string, unknown>) => void;
}

interface ContextFile {
  path: string;
  content: string;
}

// ============================================================================
// Defaults
// ============================================================================

const ADP_BRANCH = 'adp';
const CONTEXT_ROOT = 'agent_context';
const DEFAULT_MAX_FILES = 5;

// ============================================================================
// Internal helpers
// ============================================================================

let _config: MemoryConfig | null = null;

function cfg(): MemoryConfig {
  if (!_config) throw new Error('Memory module not configured. Call configureMemory() first.');
  return _config;
}

function log(level: string, message: string, context?: Record<string, unknown>): void {
  if (_config?.log) {
    _config.log(level, `[memory] ${message}`, context);
  }
}

/**
 * Run a shell command and return stdout. Returns null on failure (non-throwing).
 */
function run(command: string, cwd?: string): string | null {
  try {
    return execSync(command, {
      cwd: cwd || cfg().cwd,
      encoding: 'utf-8',
      timeout: 30_000,
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return null;
  }
}

/**
 * Run a shell command, throwing on failure.
 */
function runOrThrow(command: string, cwd?: string): string {
  return execSync(command, {
    cwd: cwd || cfg().cwd,
    encoding: 'utf-8',
    timeout: 30_000,
    stdio: ['pipe', 'pipe', 'pipe'],
  }).trim();
}

/**
 * Generate an ISO-ish timestamp suitable for filenames: YYYY-MM-DDTHH-MM
 */
function fileTimestamp(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}-${pad(now.getMinutes())}`;
}

/**
 * Get the current branch name so we can switch back after writing.
 */
function getCurrentBranch(): string | null {
  return run('git rev-parse --abbrev-ref HEAD');
}

// ============================================================================
// Public API
// ============================================================================

/**
 * Configure the memory module. Must be called before any other function.
 */
export function configureMemory(config: MemoryConfig): void {
  _config = { maxFilesPerFolder: DEFAULT_MAX_FILES, ...config };
  log('INFO', 'Memory module configured', {
    agentType: config.agentType,
    issueNumber: config.issueNumber,
    cwd: config.cwd,
  });
}

/**
 * Ensure the 'adp' orphan branch exists in origin.
 * Creates it (with an initial commit) if it doesn't exist yet.
 * This is safe to call multiple times — it's a no-op if the branch exists.
 */
export async function ensureAdpBranch(): Promise<void> {
  const { cwd } = cfg();

  // Try fetching the branch from origin
  const fetched = run(`git fetch origin ${ADP_BRANCH}`, cwd);
  if (fetched !== null) {
    // Check if the remote branch ref exists after fetch
    const refCheck = run(`git rev-parse --verify origin/${ADP_BRANCH}`, cwd);
    if (refCheck !== null) {
      log('INFO', 'adp branch exists on origin');
      return;
    }
  }

  log('INFO', 'adp branch not found — creating orphan branch');

  const originalBranch = getCurrentBranch();

  try {
    // Create orphan branch
    runOrThrow(`git checkout --orphan ${ADP_BRANCH}`, cwd);
    // Remove all tracked files from the index (we want a clean branch)
    run('git rm -rf .', cwd);
    // Create the agent_context directory structure
    runOrThrow(`mkdir -p ${CONTEXT_ROOT}/components ${CONTEXT_ROOT}/agents`, cwd);
    // Write a README so the branch isn't completely empty
    const readme = [
      '# Agent Context (adp branch)',
      '',
      'This branch stores persistent context for ADP agents.',
      'It is an orphan branch — it never merges to main.',
      '',
      'Structure:',
      '  agent_context/components/<name>/  — component-level records',
      '  agent_context/agents/<persona>/   — agent run summaries',
      '',
      'See .adp-rules/memory.md for templates and usage.',
    ].join('\n');

    execSync(`cat > "${path.join(CONTEXT_ROOT, 'README.md')}" << 'ADPEOF'\n${readme}\nADPEOF`, {
      cwd,
      encoding: 'utf-8',
    });

    runOrThrow(`git add ${CONTEXT_ROOT}/`, cwd);
    runOrThrow('git commit -m "Initialize agent_context on adp branch"', cwd);
    runOrThrow(`git push origin ${ADP_BRANCH}`, cwd);
    log('INFO', 'Created and pushed adp orphan branch');
  } finally {
    // Switch back to the original branch
    if (originalBranch && originalBranch !== ADP_BRANCH) {
      run(`git checkout ${originalBranch}`, cwd);
    }
  }
}

/**
 * Detect the component name from issue labels or body.
 * Looks for labels like `component:<name>`, falls back to `general`.
 */
export function detectComponent(labels: string[], issueBody?: string): string {
  // Priority 1: label-based detection
  for (const label of labels) {
    const match = label.match(/^component[:\-_](.+)$/i);
    if (match) return sanitizeComponentName(match[1]);
  }

  // Priority 2: body-based detection (look for "Component: <name>")
  if (issueBody) {
    const bodyMatch = issueBody.match(/(?:^|\n)\s*component\s*:\s*(.+)/im);
    if (bodyMatch) return sanitizeComponentName(bodyMatch[1]);
  }

  return 'general';
}

/**
 * Sanitize a component/agent name to prevent command injection.
 * Only allows alphanumeric, hyphens, and underscores.
 */
function sanitizeComponentName(raw: string): string {
  const sanitized = raw.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
  return sanitized || 'general';
}

/**
 * Read all context files from `agent_context/components/<component>/`.
 * Returns the content of the most recent N files (sorted by filename desc).
 */
export async function readComponentContext(component: string): Promise<string[]> {
  return readContextFolder(`${CONTEXT_ROOT}/components/${sanitizeComponentName(component)}`);
}

/**
 * Read all context files from `agent_context/agents/<agentType>/`.
 * Returns the content of the most recent N files (sorted by filename desc).
 */
export async function readAgentContext(agentType: string): Promise<string[]> {
  return readContextFolder(`${CONTEXT_ROOT}/agents/${sanitizeComponentName(agentType)}`);
}

/**
 * Write a component record to the adp branch.
 */
export async function writeComponentRecord(component: string, content: string): Promise<void> {
  const safeComponent = sanitizeComponentName(component);
  const fileName = `issue-${cfg().issueNumber}_${fileTimestamp()}.md`;
  const filePath = `${CONTEXT_ROOT}/components/${safeComponent}/${fileName}`;
  await writeToAdpBranch(filePath, content);
}

/**
 * Write an agent run summary to the adp branch.
 */
export async function writeAgentRecord(agentType: string, content: string): Promise<void> {
  const safeAgent = sanitizeComponentName(agentType);
  const fileName = `run_issue-${cfg().issueNumber}_${fileTimestamp()}.md`;
  const filePath = `${CONTEXT_ROOT}/agents/${safeAgent}/${fileName}`;
  await writeToAdpBranch(filePath, content);
}

// ============================================================================
// Internal: read/write helpers
// ============================================================================

/**
 * Read context files from a folder on the adp branch (via git show).
 * Never checks out the adp branch — reads objects directly.
 */
async function readContextFolder(folderPath: string): Promise<string[]> {
  const { cwd, maxFilesPerFolder } = cfg();
  const maxFiles = maxFilesPerFolder ?? DEFAULT_MAX_FILES;

  // Ensure we have the latest
  run(`git fetch origin ${ADP_BRANCH}`, cwd);

  // List files in the folder on origin/adp
  const listing = run(`git ls-tree --name-only origin/${ADP_BRANCH}:${folderPath}`, cwd);
  if (!listing) {
    log('INFO', `No context files found at ${folderPath}`);
    return [];
  }

  // Sort descending (newest first by filename convention) and limit
  const files = listing
    .split('\n')
    .filter((f) => f.endsWith('.md'))
    .sort()
    .reverse()
    .slice(0, maxFiles);

  const results: ContextFile[] = [];
  for (const file of files) {
    const content = run(`git show "origin/${ADP_BRANCH}:${folderPath}/${file}"`, cwd);
    if (content) {
      results.push({ path: `${folderPath}/${file}`, content });
    }
  }

  log('INFO', `Read ${results.length} context files from ${folderPath}`);
  return results.map((r) => r.content);
}

/**
 * Write a file to the adp branch, commit, and push.
 * Handles concurrent writes with pull --rebase + retry.
 */
async function writeToAdpBranch(filePath: string, content: string): Promise<void> {
  const { cwd } = cfg();
  const originalBranch = getCurrentBranch();

  try {
    // Fetch latest
    run(`git fetch origin ${ADP_BRANCH}`, cwd);

    // Checkout adp branch
    runOrThrow(`git checkout origin/${ADP_BRANCH}`, cwd);
    runOrThrow(`git checkout -B ${ADP_BRANCH}`, cwd);

    // Ensure parent directory exists
    const dir = path.dirname(path.join(cwd, filePath));
    execSync(`mkdir -p "${dir}"`, { cwd, encoding: 'utf-8' });

    // Write file
    const fullPath = path.join(cwd, filePath);
    const { writeFileSync } = await import('fs');
    writeFileSync(fullPath, content, 'utf-8');

    // Stage, commit, push
    runOrThrow(`git add "${filePath}"`, cwd);
    runOrThrow(`git commit -m "agent-context: ${path.basename(filePath)}"`, cwd);

    // Push with retry (handles concurrent writes)
    const pushed = pushWithRetry(cwd);
    if (pushed) {
      log('INFO', `Wrote context: ${filePath}`);
    } else {
      log('WARN', `Failed to push context file: ${filePath} (will not block agent)`);
    }
  } catch (err) {
    log('WARN', `Failed to write context: ${filePath}`, { error: (err as Error).message });
  } finally {
    // Always switch back to the work branch
    if (originalBranch && originalBranch !== ADP_BRANCH) {
      run(`git checkout ${originalBranch}`, cwd);
    }
  }
}

/**
 * Push to origin/adp with pull --rebase retry for concurrent writes.
 * Returns true on success, false on failure.
 */
function pushWithRetry(cwd: string, retries: number = 2): boolean {
  for (let attempt = 0; attempt <= retries; attempt++) {
    const result = run(`git push origin ${ADP_BRANCH}`, cwd);
    if (result !== null) return true;

    if (attempt < retries) {
      log('INFO', `Push failed, pulling --rebase and retrying (attempt ${attempt + 1}/${retries})`);
      const rebase = run(`git pull --rebase origin ${ADP_BRANCH}`, cwd);
      if (rebase === null) {
        log('WARN', 'Pull --rebase failed, aborting push retry');
        run('git rebase --abort', cwd);
        return false;
      }
    }
  }
  return false;
}

// ============================================================================
// Context builders — produce markdown content for records
// ============================================================================

export interface ComponentRecordInput {
  issueNumber: string;
  issueTitle: string;
  component: string;
  agentType: string;
  status: 'success' | 'partial' | 'failed';
  summary: string;
  learnings?: string[];
  errors?: string[];
  prNumber?: string;
}

/**
 * Build a structured component record (detailed).
 */
export function buildComponentRecord(input: ComponentRecordInput): string {
  const lines: string[] = [
    `# Component Record: ${input.component}`,
    '',
    '## Metadata',
    `- **Issue**: #${input.issueNumber} — ${input.issueTitle}`,
    `- **Agent**: @agent-${input.agentType}`,
    `- **Date**: ${new Date().toISOString()}`,
    `- **Status**: ${input.status}`,
    input.prNumber ? `- **PR**: #${input.prNumber}` : '',
    '',
    '## Summary',
    input.summary,
    '',
  ];

  if (input.learnings && input.learnings.length > 0) {
    lines.push('## Learnings');
    for (const learning of input.learnings) {
      lines.push(`- ${learning}`);
    }
    lines.push('');
  }

  if (input.errors && input.errors.length > 0) {
    lines.push('## Errors Encountered');
    for (const error of input.errors) {
      lines.push(`- ${error}`);
    }
    lines.push('');
  }

  return lines.filter((l) => l !== undefined).join('\n');
}

export interface AgentRecordInput {
  issueNumber: string;
  issueTitle: string;
  agentType: string;
  component: string;
  status: 'success' | 'partial' | 'failed';
  oneLiner: string;
}

/**
 * Build a lightweight agent run summary (brief cross-reference).
 */
export function buildAgentRecord(input: AgentRecordInput): string {
  return [
    `# Agent Run: @agent-${input.agentType}`,
    '',
    `- **Issue**: #${input.issueNumber} — ${input.issueTitle}`,
    `- **Date**: ${new Date().toISOString()}`,
    `- **Component**: ${input.component}`,
    `- **Status**: ${input.status}`,
    '',
    '## Summary',
    input.oneLiner,
    '',
    `See \`agent_context/components/${input.component}/issue-${input.issueNumber}_*.md\` for details.`,
    '',
  ].join('\n');
}

/**
 * Format loaded context into a string suitable for injection into a prompt.
 */
export function formatContextForPrompt(
  componentContext: string[],
  agentContext: string[],
  component: string,
  agentType: string
): string {
  if (componentContext.length === 0 && agentContext.length === 0) {
    return '';
  }

  const sections: string[] = ['## Agent Memory (from adp branch)', ''];

  if (componentContext.length > 0) {
    sections.push(`### Component History: ${component} (${componentContext.length} records)`);
    sections.push('');
    for (const ctx of componentContext) {
      sections.push(ctx);
      sections.push('\n---\n');
    }
  }

  if (agentContext.length > 0) {
    sections.push(`### Agent History: @agent-${agentType} (${agentContext.length} records)`);
    sections.push('');
    for (const ctx of agentContext) {
      sections.push(ctx);
      sections.push('\n---\n');
    }
  }

  return sections.join('\n');
}

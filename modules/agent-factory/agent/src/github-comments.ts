/**
 * Live Status Comment — edit-in-place progress updates on GitHub issues.
 *
 * Posts an initial "live status" comment when the agent starts, then PATCHes
 * it in-place as stages transition. Rate-limited to max 1 update per 5s to
 * avoid GitHub secondary rate limits.
 *
 * Design reference: docs/hosted-platform-design.md §Live progress UX (Phase 1)
 */

// ─── Types ───────────────────────────────────────────────────────────────────

/** Status of a single stage in the pipeline. */
export type StageStatus = 'pending' | 'in_progress' | 'complete' | 'skipped';

export interface StageDefinition {
  /** Human-readable label, e.g. "Research" */
  label: string;
  /** Current status */
  status: StageStatus;
  /** Epoch ms when this stage started (set on transition to in_progress) */
  startedAt?: number;
  /** Epoch ms when this stage completed */
  completedAt?: number;
}

export interface LiveStatusCommentOptions {
  /** GitHub API base URL (default: https://api.github.com) */
  apiBaseUrl?: string;
  /** Repository owner */
  owner: string;
  /** Repository name */
  repo: string;
  /** Issue number to post on */
  issueNumber: number;
  /** GitHub token (installation token or PAT) */
  token: string;
  /** Minimum interval between PATCH calls in ms (default: 5000) */
  minUpdateIntervalMs?: number;
  /** Optional logger function */
  log?: (level: string, message: string) => void;
}

export interface SuccessSummary {
  prUrl?: string;
  artifacts?: string[];
  durationMs: number;
  /** Optional additional markdown to append */
  details?: string;
}

export interface FailureSummary {
  error: string;
  stackExcerpt?: string;
  suggestedNextSteps?: string[];
  durationMs: number;
}

// ─── Rendering helpers ───────────────────────────────────────────────────────

function stageCheckbox(status: StageStatus): string {
  switch (status) {
    case 'complete': return '[x]';
    case 'in_progress': return '[~]';
    case 'skipped': return '[x]'; // show as done with note
    case 'pending':
    default: return '[ ]';
  }
}

function formatElapsed(ms: number): string {
  if (ms < 1000) return '<1s';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSec = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainSec}s`;
  const hours = Math.floor(minutes / 60);
  const remainMin = minutes % 60;
  return `${hours}h ${remainMin}m`;
}

function relativeTime(epochMs: number, now: number): string {
  const diff = now - epochMs;
  if (diff < 1000) return 'just now';
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ago`;
}

function formatDuration(ms: number): string {
  return formatElapsed(ms);
}

// ─── LiveStatusComment class ─────────────────────────────────────────────────

export class LiveStatusComment {
  private readonly options: Required<Pick<LiveStatusCommentOptions, 'apiBaseUrl' | 'minUpdateIntervalMs'>> & LiveStatusCommentOptions;
  private stages: StageDefinition[];
  private commentId: number | null = null;
  private lastUpdateTime = 0;
  private pendingUpdate: ReturnType<typeof setTimeout> | null = null;
  private runStartTime: number;
  private latestMessage = '';

  constructor(stages: StageDefinition[], options: LiveStatusCommentOptions) {
    this.stages = stages.map(s => ({ ...s }));
    this.options = {
      apiBaseUrl: 'https://api.github.com',
      minUpdateIntervalMs: 5000,
      ...options,
    };
    this.runStartTime = Date.now();
  }

  /** Returns the comment ID once posted (null before post()). */
  getCommentId(): number | null {
    return this.commentId;
  }

  /** Returns a shallow copy of current stages (for testing/inspection). */
  getStages(): StageDefinition[] {
    return this.stages.map(s => ({ ...s }));
  }

  /**
   * Post the initial live status comment. Must be called once before
   * any stage transitions.
   */
  async post(): Promise<number> {
    const body = this.renderBody();
    const resp = await this.apiRequest('POST', this.commentsUrl(), { body });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Failed to post status comment: ${resp.status} ${text}`);
    }
    const data = await resp.json() as { id: number };
    this.commentId = data.id;
    this.lastUpdateTime = Date.now();
    this.log('INFO', `Live status comment posted: id=${this.commentId}`);
    return this.commentId;
  }

  /**
   * Transition a stage to a new status. Triggers a rate-limited comment update.
   */
  transition(stageIndex: number, status: StageStatus, message?: string): void {
    if (stageIndex < 0 || stageIndex >= this.stages.length) return;

    const stage = this.stages[stageIndex];
    stage.status = status;
    const now = Date.now();

    if (status === 'in_progress' && !stage.startedAt) {
      stage.startedAt = now;
    }
    if (status === 'complete' || status === 'skipped') {
      stage.completedAt = now;
    }

    if (message) {
      this.latestMessage = message;
    }

    this.scheduleUpdate();
  }

  /**
   * Mark the run as successful and replace the comment with a final summary.
   */
  async finalizeSuccess(summary: SuccessSummary): Promise<void> {
    this.cancelPendingUpdate();
    const lines: string[] = [
      '## Agent Complete',
      '',
      `**Duration**: ${formatDuration(summary.durationMs)}`,
    ];
    if (summary.prUrl) {
      lines.push(`**PR**: ${summary.prUrl}`);
    }
    if (summary.artifacts && summary.artifacts.length > 0) {
      lines.push('', '**Artifacts**:');
      for (const a of summary.artifacts) {
        lines.push(`- ${a}`);
      }
    }
    // Include stage summary
    lines.push('', '### Stages');
    for (const stage of this.stages) {
      const elapsed = stage.startedAt && stage.completedAt
        ? ` (${formatElapsed(stage.completedAt - stage.startedAt)})`
        : '';
      lines.push(`- [x] ${stage.label}${elapsed}`);
    }
    if (summary.details) {
      lines.push('', summary.details);
    }
    await this.updateComment(lines.join('\n'));
  }

  /**
   * Mark the run as failed and replace the comment with a failure summary.
   */
  async finalizeFailure(summary: FailureSummary): Promise<void> {
    this.cancelPendingUpdate();
    const lines: string[] = [
      '## Agent Failed',
      '',
      `**Duration**: ${formatDuration(summary.durationMs)}`,
      '',
      '**Error**:',
      '```',
      summary.error.substring(0, 1000),
      '```',
    ];
    if (summary.stackExcerpt) {
      lines.push('', '<details><summary>Stack trace</summary>', '', '```', summary.stackExcerpt.substring(0, 2000), '```', '', '</details>');
    }
    if (summary.suggestedNextSteps && summary.suggestedNextSteps.length > 0) {
      lines.push('', '**Suggested next steps**:');
      for (const step of summary.suggestedNextSteps) {
        lines.push(`- ${step}`);
      }
    }
    // Include stage summary showing where it failed
    lines.push('', '### Stages');
    for (const stage of this.stages) {
      const checkbox = stageCheckbox(stage.status);
      const elapsed = stage.startedAt
        ? ` (${formatElapsed((stage.completedAt || Date.now()) - stage.startedAt)})`
        : '';
      const suffix = stage.status === 'in_progress' ? ' **FAILED HERE**' : '';
      lines.push(`- ${checkbox} ${stage.label}${elapsed}${suffix}`);
    }
    await this.updateComment(lines.join('\n'));
  }

  /**
   * Force an immediate update (bypasses rate limit). Use sparingly.
   */
  async flush(): Promise<void> {
    this.cancelPendingUpdate();
    if (this.commentId) {
      await this.updateComment(this.renderBody());
    }
  }

  // ─── Private ─────────────────────────────────────────────────────────────

  private renderBody(): string {
    const now = Date.now();
    const lines: string[] = [
      `## Agent running — last update ${relativeTime(now, now + 1)}`,
      '',
      '### Progress',
    ];

    for (const stage of this.stages) {
      const checkbox = stageCheckbox(stage.status);
      let detail = '';
      if (stage.status === 'in_progress' && stage.startedAt) {
        detail = ` (running, ${formatElapsed(now - stage.startedAt)} elapsed)`;
      } else if (stage.status === 'complete' && stage.startedAt && stage.completedAt) {
        detail = ` (${formatElapsed(stage.completedAt - stage.startedAt)})`;
      } else if (stage.status === 'skipped') {
        detail = ' (skipped)';
      }
      lines.push(`- ${checkbox} ${stage.label}${detail}`);
    }

    if (this.latestMessage) {
      lines.push('', `Latest: ${this.latestMessage}`);
    }

    return lines.join('\n');
  }

  private scheduleUpdate(): void {
    if (!this.commentId) return;

    const now = Date.now();
    const elapsed = now - this.lastUpdateTime;
    const minInterval = this.options.minUpdateIntervalMs;

    if (elapsed >= minInterval) {
      // Can update immediately
      this.doUpdate();
    } else if (!this.pendingUpdate) {
      // Schedule for later
      const delay = minInterval - elapsed;
      this.pendingUpdate = setTimeout(() => {
        this.pendingUpdate = null;
        this.doUpdate();
      }, delay);
    }
    // If there's already a pending update, it will pick up the latest state
  }

  private doUpdate(): void {
    this.lastUpdateTime = Date.now();
    const body = this.renderBody();
    // Fire and forget — don't block stage transitions on network
    this.updateComment(body).catch(err => {
      this.log('WARN', `Failed to update status comment: ${(err as Error).message}`);
    });
  }

  private cancelPendingUpdate(): void {
    if (this.pendingUpdate) {
      clearTimeout(this.pendingUpdate);
      this.pendingUpdate = null;
    }
  }

  private async updateComment(body: string): Promise<void> {
    if (!this.commentId) return;
    const url = `${this.options.apiBaseUrl}/repos/${this.options.owner}/${this.options.repo}/issues/comments/${this.commentId}`;
    const resp = await this.apiRequest('PATCH', url, { body });
    if (!resp.ok) {
      const text = await resp.text();
      this.log('WARN', `Comment update failed: ${resp.status} ${text.substring(0, 200)}`);
    }
  }

  private commentsUrl(): string {
    return `${this.options.apiBaseUrl}/repos/${this.options.owner}/${this.options.repo}/issues/${this.options.issueNumber}/comments`;
  }

  private async apiRequest(method: string, url: string, body?: Record<string, unknown>): Promise<Response> {
    return fetch(url, {
      method,
      headers: {
        'Authorization': `token ${this.options.token}`,
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.github+json',
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  private log(level: string, message: string): void {
    if (this.options.log) {
      this.options.log(level, message);
    }
  }
}

// ─── Factory helper ──────────────────────────────────────────────────────────

/**
 * Create a standard set of stages for agent-worker runs.
 */
export function createWorkerStages(): StageDefinition[] {
  return [
    { label: 'Setup', status: 'pending' },
    { label: 'Analyze', status: 'pending' },
    { label: 'Plan', status: 'pending' },
    { label: 'Implement', status: 'pending' },
    { label: 'Verify', status: 'pending' },
    { label: 'PR', status: 'pending' },
  ];
}

/**
 * Create a standard set of stages for skill-agent runs.
 */
export function createSkillAgentStages(): StageDefinition[] {
  return [
    { label: 'Planning', status: 'pending' },
    { label: 'Approval', status: 'pending' },
    { label: 'Execution', status: 'pending' },
    { label: 'Finalize', status: 'pending' },
  ];
}

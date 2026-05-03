/**
 * CheckRunStreamer — per-turn live streaming to GitHub Check Run output.
 *
 * Subscribes to the agent message loop in agent-worker.ts and buffers events
 * into a Markdown document that is PATCH-ed to the Check Run every ~2s so
 * users watching the check page see turn-by-turn activity in near real time.
 *
 * Design constraints:
 *  - Min 2s between PATCH calls, max 30 PATCHes per run
 *  - output.text must stay ≤ 65,535 chars (GitHub hard limit); we target 60 KB
 *  - If text would exceed 60 KB, keep first plan turn + last N turns + a hidden-count marker
 *  - Writes final Markdown to /tmp/adp-check-run-final.md so entrypoint.py can
 *    include it in the final update_check_run call (preserves transcript across process boundary)
 *  - All PATCH calls are best-effort; errors are logged but never throw
 */

import * as fs from 'fs';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CheckRunStreamerConfig {
  /** Integer check run ID returned by the GitHub Checks API. */
  checkRunId: number;
  /** Full repo name, e.g. "acme-corp/adp". */
  repo: string;
  /** GitHub installation access token with checks:write scope. */
  token: string;
  /** Agent persona name, e.g. "developer". */
  persona: string;
  /** Issue number being worked on. */
  issueNumber: number;
  /** Model identifier string. */
  model: string;
  /** Optional logger function (defaults to console.warn). */
  log?: (msg: string) => void;
}

interface ToolSummary {
  name: string;
  /** Human-readable one-liner describing what the tool was called with. */
  inputPreview: string;
}

interface TurnRecord {
  turn: number;
  tools: ToolSummary[];
  /** First meaningful text from this turn (capped at 500 chars). */
  textPreview: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_PATCHES = 30;
const MIN_PATCH_INTERVAL_MS = 2_000;
/** Target ceiling on output.text; GitHub hard limit is 65,535. */
const MAX_OUTPUT_BYTES = 60 * 1024; // 60 KB
/** Path where the final rendered Markdown is written for entrypoint.py. */
const FINAL_OUTPUT_PATH = '/tmp/adp-check-run-final.md';

// ---------------------------------------------------------------------------
// CheckRunStreamer
// ---------------------------------------------------------------------------

export class CheckRunStreamer {
  private readonly cfg: CheckRunStreamerConfig;
  private readonly startTimeMs: number;
  private readonly warn: (msg: string) => void;

  private turns: TurnRecord[] = [];
  private planText: string | null = null;
  private totalCostUsd: number = 0;

  private patchCount: number = 0;
  private lastPatchMs: number = 0;
  private pendingTimer: ReturnType<typeof setTimeout> | null = null;
  private midTurnTimer: ReturnType<typeof setInterval> | null = null;
  private destroyed: boolean = false;

  constructor(cfg: CheckRunStreamerConfig) {
    this.cfg = cfg;
    this.startTimeMs = Date.now();
    this.warn = cfg.log ?? ((msg) => console.warn(`[CheckRunStreamer] ${msg}`));
  }

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  /**
   * Call once per 'assistant' message from the SDK loop.
   * content is the raw message.content array from the Claude SDK response.
   */
  onTurn(data: {
    turn: number;
    content: Array<{ name?: string; input?: Record<string, unknown>; text?: string }>;
    costUsd?: number;
  }): void {
    if (this.destroyed) return;

    // Stop mid-turn polling timer (turn completed)
    this._clearMidTurnTimer();

    const tools: ToolSummary[] = [];
    let textPreview = '';

    for (const block of data.content) {
      if (block.name) {
        tools.push({ name: block.name, inputPreview: this._previewInput(block.name, block.input ?? {}) });
      }
      if (block.text && !textPreview) {
        textPreview = block.text.trim().slice(0, 500);
      }
    }

    // First substantive text becomes the "plan"
    if (!this.planText && textPreview) {
      this.planText = textPreview;
    }

    if (data.costUsd !== undefined) {
      this.totalCostUsd = data.costUsd;
    }

    this.turns.push({ turn: data.turn, tools, textPreview });
    this._schedulePatch();
  }

  /**
   * Call on each 'tool_progress' message to keep the display live during
   * long-running tool calls (e.g. a Bash command that takes > 2s).
   */
  onToolProgress(_toolName: string): void {
    if (this.destroyed) return;
    this._ensureMidTurnPolling();
  }

  /**
   * Call when the 'result' message is received (run complete).
   */
  onResult(data: { costUsd?: number; turns?: number; durationMs?: number }): void {
    if (this.destroyed) return;
    this._clearMidTurnTimer();
    if (data.costUsd !== undefined) {
      this.totalCostUsd = data.costUsd;
    }
    // Fire a final PATCH immediately to capture the complete transcript
    this._firePatch(true /* immediate */);
  }

  /** Clean up timers. Call when the message loop exits. */
  destroy(): void {
    this.destroyed = true;
    this._clearMidTurnTimer();
    if (this.pendingTimer) {
      clearTimeout(this.pendingTimer);
      this.pendingTimer = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Markdown rendering
  // ---------------------------------------------------------------------------

  buildMarkdown(status: 'running' | 'completed'): string {
    const elapsedSec = Math.round((Date.now() - this.startTimeMs) / 1000);
    const turnLabel = status === 'running'
      ? `${this.turns.length} / running`
      : `${this.turns.length} / done`;
    const costLabel = `$${this.totalCostUsd.toFixed(4)}`;

    const header = [
      `## Agent: ${this.cfg.persona} · issue #${this.cfg.issueNumber}`,
      ``,
      `**Model:** ${this.cfg.model}`,
      `**Cost:** ${costLabel} · **Turn:** ${turnLabel}`,
      `**Elapsed:** ${elapsedSec}s`,
    ].join('\n');

    const planSection = this.planText
      ? `\n\n### Plan\n> ${this.planText.split('\n').join('\n> ')}`
      : '';

    const activitySection = this.turns.length > 0
      ? `\n\n### Activity\n${this._renderActivity()}`
      : '';

    const full = header + planSection + activitySection;
    if (Buffer.byteLength(full, 'utf8') <= MAX_OUTPUT_BYTES) {
      return full;
    }
    return this._truncated(header, planSection);
  }

  // ---------------------------------------------------------------------------
  // Private helpers
  // ---------------------------------------------------------------------------

  private _previewInput(toolName: string, input: Record<string, unknown>): string {
    switch (toolName) {
      case 'Bash': {
        const cmd = (input.command as string) ?? '';
        return cmd.slice(0, 120) + (cmd.length > 120 ? '…' : '');
      }
      case 'Read':
      case 'Write':
      case 'Edit':
        return (input.file_path as string) ?? '';
      case 'Grep':
        return `${input.pattern ?? ''}${input.glob ? ` (${input.glob})` : ''}`;
      case 'Glob':
        return (input.pattern as string) ?? '';
      case 'WebSearch':
        return (input.query as string) ?? '';
      case 'WebFetch':
        return (input.url as string) ?? '';
      default:
        return '';
    }
  }

  private _renderTurnDetails(rec: TurnRecord, isOpen: boolean): string {
    const openAttr = isOpen ? ' open' : '';

    // Build a one-liner summary
    let summaryLabel: string;
    if (rec.tools.length > 0) {
      const t = rec.tools[0];
      summaryLabel = t.inputPreview
        ? `🔧 Turn ${rec.turn} — ${t.name}: ${t.inputPreview}`
        : `🔧 Turn ${rec.turn} — ${t.name}`;
    } else if (rec.textPreview) {
      summaryLabel = `💭 Turn ${rec.turn}`;
    } else {
      summaryLabel = `Turn ${rec.turn}`;
    }

    // Build detail body: first tool block + any text
    const bodyParts: string[] = [];

    if (rec.tools.length > 0) {
      const t = rec.tools[0];
      if (t.inputPreview) {
        const lang = t.name === 'Bash' ? 'bash' : '';
        bodyParts.push(`\`\`\`${lang}\n${t.inputPreview}\n\`\`\``);
      }
      if (rec.tools.length > 1) {
        const extra = rec.tools.slice(1).map(x => `${x.name}${x.inputPreview ? `: ${x.inputPreview}` : ''}`).join(', ');
        bodyParts.push(`_Also: ${extra}_`);
      }
    }

    if (rec.textPreview && rec.tools.length === 0) {
      bodyParts.push(rec.textPreview);
    }

    const body = bodyParts.length > 0 ? `\n\n${bodyParts.join('\n\n')}\n` : '…';
    return `<details${openAttr}><summary>${summaryLabel}</summary>${body}</details>`;
  }

  private _renderActivity(): string {
    // Show turns in reverse (newest first), newest is open
    const reversed = [...this.turns].reverse();
    return reversed
      .map((rec, idx) => this._renderTurnDetails(rec, idx === 0))
      .join('\n');
  }

  /**
   * Build a truncated version: keep header + plan + last N turns that fit.
   */
  private _truncated(header: string, planSection: string): string {
    const base = header + planSection;
    const baseBytes = Buffer.byteLength(base, 'utf8');
    const budget = MAX_OUTPUT_BYTES - baseBytes - 200; // reserve for hidden-count marker

    // Walk turns from newest backwards, accumulate until budget exhausted
    const reversed = [...this.turns].reverse();
    const kept: TurnRecord[] = [];
    let used = 0;

    for (const rec of reversed) {
      const details = this._renderTurnDetails(rec, kept.length === 0);
      const detailsBytes = Buffer.byteLength(details, 'utf8');
      if (used + detailsBytes > budget) break;
      kept.push(rec);
      used += detailsBytes;
    }

    const hiddenCount = this.turns.length - kept.length;
    const hiddenMarker = hiddenCount > 0
      ? `\n\n_**(${hiddenCount} turns hidden — output truncated to fit GitHub's 60 KB limit)**_`
      : '';

    const keptSection = kept.length > 0
      ? `\n\n### Activity\n${hiddenMarker}\n${kept.map((rec, idx) => this._renderTurnDetails(rec, idx === 0)).join('\n')}`
      : hiddenMarker
        ? `\n\n### Activity${hiddenMarker}`
        : '';

    return base + keptSection;
  }

  // ---------------------------------------------------------------------------
  // Patch scheduling and debounce
  // ---------------------------------------------------------------------------

  private _schedulePatch(): void {
    if (this.destroyed || this.patchCount >= MAX_PATCHES) return;
    if (this.pendingTimer) return; // already scheduled

    const msSinceLast = Date.now() - this.lastPatchMs;
    const delay = Math.max(0, MIN_PATCH_INTERVAL_MS - msSinceLast);

    this.pendingTimer = setTimeout(() => {
      this.pendingTimer = null;
      this._firePatch(false);
    }, delay);
  }

  private _firePatch(immediate: boolean): void {
    if (this.destroyed || this.patchCount >= MAX_PATCHES) return;

    const msSinceLast = Date.now() - this.lastPatchMs;
    if (!immediate && msSinceLast < MIN_PATCH_INTERVAL_MS) {
      this._schedulePatch();
      return;
    }

    this.patchCount++;
    this.lastPatchMs = Date.now();

    const status = this.destroyed ? 'completed' : 'running';
    const md = this.buildMarkdown(status);

    // Write final output file for entrypoint.py to pick up
    if (immediate) {
      try {
        fs.writeFileSync(FINAL_OUTPUT_PATH, md, 'utf8');
      } catch {
        // best-effort
      }
    }

    const turnLabel = status === 'running'
      ? `Agent ${this.cfg.persona} · Turn ${this.turns.length} / running`
      : `Agent ${this.cfg.persona} · ${this.turns.length} turns completed`;
    const elapsedSec = Math.round((Date.now() - this.startTimeMs) / 1000);
    const summaryLine = `Cost: $${this.totalCostUsd.toFixed(4)} · Elapsed: ${elapsedSec}s`;

    this._doPatch(turnLabel, summaryLine, md).catch((err: unknown) => {
      this.warn(`PATCH failed (${this.patchCount}/${MAX_PATCHES}): ${(err as Error).message}`);
    });
  }

  private async _doPatch(title: string, summary: string, text: string): Promise<void> {
    const url = `https://api.github.com/repos/${this.cfg.repo}/check-runs/${this.cfg.checkRunId}`;
    // Clamp to GitHub's hard limit just in case
    const safeText = text.length > 65535 ? text.slice(0, 65535) : text;

    const payload: Record<string, unknown> = {
      output: { title, summary, text: safeText },
    };

    const resp = await fetch(url, {
      method: 'PATCH',
      headers: {
        Authorization: `Bearer ${this.cfg.token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      throw new Error(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
    }
  }

  /** Start a setInterval that fires a PATCH every MIN_PATCH_INTERVAL_MS during a long tool call. */
  private _ensureMidTurnPolling(): void {
    if (this.midTurnTimer) return;
    this.midTurnTimer = setInterval(() => {
      if (this.patchCount < MAX_PATCHES && !this.destroyed) {
        this._firePatch(false);
      }
    }, MIN_PATCH_INTERVAL_MS);
  }

  private _clearMidTurnTimer(): void {
    if (this.midTurnTimer) {
      clearInterval(this.midTurnTimer);
      this.midTurnTimer = null;
    }
  }
}

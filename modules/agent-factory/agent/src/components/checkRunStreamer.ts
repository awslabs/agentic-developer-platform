/**
 * CheckRunStreamer — per-turn live streaming to GitHub Check Run output.
 *
 * Subscribes to the agent message loop in agent-worker.ts and buffers events
 * into a Markdown document that is PATCH-ed to the Check Run every ~2s so
 * users watching the check page see turn-by-turn activity in near real time.
 *
 * Design constraints:
 *  - Minimum interval between PATCH calls decays with elapsed run time
 *    (see _currentMinIntervalMs) so a long run keeps updating for its whole
 *    lifetime instead of freezing after a fixed patch budget. MAX_PATCHES is a
 *    generous circuit-breaker only, never a normal-operation cap.
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

/**
 * Circuit-breaker only. A logic bug that tries to PATCH in a tight loop is
 * capped here so it can't hammer the GitHub API into a secondary rate limit.
 * This is NOT a normal-operation budget: the decaying interval (see
 * _currentMinIntervalMs) keeps a healthy 60-min run well under this number.
 */
const MAX_PATCHES = 150;

/**
 * Elapsed-time breakpoints for the minimum interval between PATCHes. Early in a
 * run we update often; as the run gets long we back off so a multi-hour run
 * stays under the circuit-breaker while still always eventually updating.
 * Worst-case count on a 60-min run ≈ 24 + 52 + 60 ≈ 136 (< MAX_PATCHES).
 */
const INTERVAL_SCHEDULE: Array<{ afterMs: number; intervalMs: number }> = [
  { afterMs: 15 * 60_000, intervalMs: 45_000 }, // > 15 min elapsed → 45s
  { afterMs: 2 * 60_000, intervalMs: 15_000 }, //  2–15 min elapsed → 15s
  { afterMs: 0, intervalMs: 5_000 }, //           0–2 min elapsed → 5s
];
/** Throttle marker is shown in the header once the interval reaches this. */
const THROTTLE_MARKER_MIN_INTERVAL_MS = 15_000;
/** Target ceiling on output.text; GitHub hard limit is 65,535. */
const MAX_OUTPUT_BYTES = 60 * 1024; // 60 KB
/**
 * Rolling cap on retained Codex sub-step lines (issue #2884). Keeps the Codex
 * section from dominating the 60 KB budget on a long delegation; the full
 * history stays in the archival JSONL (#2753).
 */
const MAX_CODEX_LINES = 200;
/** Path where the final rendered Markdown is written for entrypoint.py. */
const FINAL_OUTPUT_PATH = '/tmp/adp-check-run-final.md';

/**
 * GPT-5.5 pricing per 1K tokens — ESTIMATE for display only.
 * Canonical source: modules/gateway/src/budget/pricing.py:163
 * usage_logs (gateway-side) stays the single source of truth for billing.
 */
export const CODEX_INPUT_PER_1K = 0.0055;
export const CODEX_OUTPUT_PER_1K = 0.033;

/**
 * Compute an estimated Codex cost from token counts. Display-only; the
 * gateway's usage_logs remains the authoritative billing source.
 */
export function computeCodexCostUsd(inputTokens: number, outputTokens: number): number {
  return (inputTokens / 1000) * CODEX_INPUT_PER_1K + (outputTokens / 1000) * CODEX_OUTPUT_PER_1K;
}

// ---------------------------------------------------------------------------
// CheckRunStreamer
// ---------------------------------------------------------------------------

export class CheckRunStreamer {
  private readonly cfg: CheckRunStreamerConfig;
  private readonly startTimeMs: number;
  private readonly warn: (msg: string) => void;

  private turns: TurnRecord[] = [];
  private planText: string | null = null;
  private reasoningThoughts: string[] = [];
  private totalCostUsd: number = 0;
  /** Estimated Codex delegation cost (display only; issue #2970). */
  private codexCostUsd: number = 0;
  /** Compact Codex sub-step lines (issue #2884), rolling, bounded. */
  private codexLines: string[] = [];

  private patchCount: number = 0;
  private lastPatchMs: number = 0;
  private pendingTimer: ReturnType<typeof setTimeout> | null = null;
  private midTurnTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed: boolean = false;
  private breakerWarned: boolean = false;

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
    codexCostUsd?: number;
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

    // Accumulate every thought for the Reasoning summary section
    if (textPreview) {
      this.reasoningThoughts.push(textPreview);
    }

    if (data.costUsd !== undefined) {
      this.totalCostUsd = data.costUsd;
    }
    if (data.codexCostUsd !== undefined) {
      this.codexCostUsd = data.codexCostUsd;
    }

    this.turns.push({ turn: data.turn, tools, textPreview });
    this._schedulePatch();
  }

  /**
   * Stream compact Codex sub-step lines to the live page (issue #2884).
   *
   * Called by CodexEventWatcher while a codex-bridge delegation is in flight —
   * the delegation is ONE Bash tool call from the SDK's point of view, so
   * without this the page goes dark for its whole duration. The lines are
   * accumulated and rendered under a bounded "### Codex" section, then flushed
   * via the SAME decaying-throttle/PATCH-budget path as onTurn (`_schedulePatch`)
   * — this deliberately adds NO second PATCH path, so the #2801 circuit-breaker
   * and cadence still govern the whole page.
   */
  onCodexActivity(lines: string[]): void {
    if (this.destroyed) return;
    if (!lines || lines.length === 0) return;
    for (const line of lines) {
      if (typeof line === 'string' && line.length > 0) {
        this.codexLines.push(line);
      }
    }
    // Keep the section bounded so a very long delegation can't dominate the
    // 60 KB budget; the archival JSONL (#2753) retains the full history.
    if (this.codexLines.length > MAX_CODEX_LINES) {
      this.codexLines = this.codexLines.slice(-MAX_CODEX_LINES);
    }
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
  onResult(data: { costUsd?: number; codexCostUsd?: number; turns?: number; durationMs?: number }): void {
    if (this.destroyed) return;
    this._clearMidTurnTimer();
    if (data.costUsd !== undefined) {
      this.totalCostUsd = data.costUsd;
    }
    if (data.codexCostUsd !== undefined) {
      this.codexCostUsd = data.codexCostUsd;
    }
    // Fire a final PATCH immediately to capture the complete transcript.
    // Pass an explicit 'completed' status: this.destroyed is still false here
    // (destroy() runs later), so deriving the status from it would render the
    // final page as "running".
    this._firePatch(true /* immediate */, 'completed');
  }

  /** Clean up timers. Call when the message loop exits. */
  destroy(): void {
    this.destroyed = true;
    this._clearMidTurnTimer();
    if (this.pendingTimer) {
      clearTimeout(this.pendingTimer);
      this.pendingTimer = null;
    }

    // Flush the final rendered Markdown to disk so entrypoint.py's
    // finalize-check-run step can read it and pass it to the completion
    // PATCH. Without this, GitHub's output.text ends up empty on the
    // completed check run — the page looks blank after the run finishes.
    // Best-effort: if the write fails, entrypoint falls back to a
    // summary-only completion.
    try {
      fs.writeFileSync(FINAL_OUTPUT_PATH, this.buildMarkdown('completed'), 'utf8');
    } catch {
      // best-effort; don't throw on shutdown
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
    const costLabel = this.codexCostUsd > 0
      ? `$${this.totalCostUsd.toFixed(4)} (Claude) + ~$${this.codexCostUsd.toFixed(4)} (Codex est.)`
      : `$${this.totalCostUsd.toFixed(4)}`;

    const headerLines = [
      `## Agent: ${this.cfg.persona} · issue #${this.cfg.issueNumber}`,
      ``,
      `**Model:** ${this.cfg.model}`,
      `**Cost:** ${costLabel} · **Turn:** ${turnLabel}`,
      `**Elapsed:** ${elapsedSec}s`,
    ];

    // While the run is live and updates have decayed to a slow cadence, tell the
    // reader the page is intentionally throttled (not stuck) and where to look
    // for finer detail.
    if (status === 'running') {
      const intervalMs = this._currentMinIntervalMs();
      if (intervalMs >= THROTTLE_MARKER_MIN_INTERVAL_MS) {
        headerLines.push(
          `_Live updates throttled to every ${Math.round(intervalMs / 1000)}s — full detail in the issue's progress comment._`,
        );
      }
    }

    const header = headerLines.join('\n');

    const planSection = this.planText
      ? `\n\n### Plan\n> ${this.planText.split('\n').join('\n> ')}`
      : '';

    const reasoningSection = this._renderReasoningSection(this.reasoningThoughts);

    const codexSection = this._renderCodexSection();

    const activitySection = this.turns.length > 0
      ? `\n\n### Activity\n${this._renderActivity()}`
      : '';

    const full = header + planSection + reasoningSection + codexSection + activitySection;
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

    // Build detail body: thought (if any) above the tool block
    const bodyParts: string[] = [];

    if (rec.textPreview) {
      bodyParts.push(`_${rec.textPreview}_`);
    }

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
  private _renderReasoningSection(thoughts: string[]): string {
    if (thoughts.length === 0) return '';
    const bullets = thoughts.map(t => `- ${t}`).join('\n');
    return `\n\n### Reasoning\n${bullets}`;
  }

  /**
   * Render the live Codex delegation sub-steps (issue #2884) as a fenced block
   * so the nested "codex ▸ …" one-liners read as a distinct, monospaced stream
   * interleaved into the run page. Empty (and thus invisible) when no Codex
   * delegation has streamed anything — the inert default.
   */
  private _renderCodexSection(): string {
    if (this.codexLines.length === 0) return '';
    return `\n\n### Codex\n\`\`\`\n${this.codexLines.join('\n')}\n\`\`\``;
  }

  private _truncated(header: string, planSection: string): string {
    const reasoningSection = this._renderReasoningSection(this.reasoningThoughts.slice(-20));
    // Keep a bounded tail of Codex lines in the truncated view so a live
    // delegation stays visible even when the transcript overflows 60 KB.
    const codexSection = this.codexLines.length > 0
      ? `\n\n### Codex\n\`\`\`\n${this.codexLines.slice(-40).join('\n')}\n\`\`\``
      : '';
    const base = header + planSection + reasoningSection + codexSection;
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

  /**
   * Minimum interval (ms) allowed between PATCHes right now, decaying with
   * elapsed run time. There is no lifetime cap on the number of PATCHes — a
   * PATCH is always eventually allowed — so the live page keeps updating for
   * the whole run instead of freezing after a fixed budget.
   */
  private _currentMinIntervalMs(): number {
    const elapsedMs = Date.now() - this.startTimeMs;
    for (const step of INTERVAL_SCHEDULE) {
      if (elapsedMs >= step.afterMs) return step.intervalMs;
    }
    // INTERVAL_SCHEDULE always ends with afterMs: 0, so this is unreachable;
    // fall back to the slowest cadence defensively.
    return INTERVAL_SCHEDULE[0].intervalMs;
  }

  /** True once the circuit-breaker is tripped; logs a WARN on the first trip. */
  private _breakerTripped(): boolean {
    if (this.patchCount < MAX_PATCHES) return false;
    if (!this.breakerWarned) {
      this.breakerWarned = true;
      this.warn(`circuit-breaker tripped: reached MAX_PATCHES=${MAX_PATCHES}; suppressing further live PATCHes`);
    }
    return true;
  }

  private _schedulePatch(): void {
    if (this.destroyed || this._breakerTripped()) return;
    if (this.pendingTimer) return; // already scheduled

    const msSinceLast = Date.now() - this.lastPatchMs;
    const delay = Math.max(0, this._currentMinIntervalMs() - msSinceLast);

    this.pendingTimer = setTimeout(() => {
      this.pendingTimer = null;
      this._firePatch(false);
    }, delay);
  }

  private _firePatch(immediate: boolean, statusOverride?: 'running' | 'completed'): void {
    if (this.destroyed || this._breakerTripped()) return;

    const msSinceLast = Date.now() - this.lastPatchMs;
    if (!immediate && msSinceLast < this._currentMinIntervalMs()) {
      this._schedulePatch();
      return;
    }

    this.patchCount++;
    this.lastPatchMs = Date.now();

    const status = statusOverride ?? 'running';
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
    const summaryLine = this.codexCostUsd > 0
      ? `Cost: $${this.totalCostUsd.toFixed(4)} (Claude) + ~$${this.codexCostUsd.toFixed(4)} (Codex est.) · Elapsed: ${elapsedSec}s`
      : `Cost: $${this.totalCostUsd.toFixed(4)} · Elapsed: ${elapsedSec}s`;

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

    // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf — base host is hardcoded https://api.github.com; only repo/checkRunId are interpolated (validated at config time)
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

  /**
   * Keep the display live during a long-running tool call by firing a PATCH at
   * the current decayed interval. Self-reschedules (rather than a fixed
   * setInterval) so the cadence tracks _currentMinIntervalMs as the run ages.
   */
  private _ensureMidTurnPolling(): void {
    if (this.midTurnTimer) return;
    const tick = (): void => {
      if (this.destroyed || this._breakerTripped()) {
        this._clearMidTurnTimer();
        return;
      }
      this._firePatch(false);
      this.midTurnTimer = setTimeout(tick, this._currentMinIntervalMs());
    };
    this.midTurnTimer = setTimeout(tick, this._currentMinIntervalMs());
  }

  private _clearMidTurnTimer(): void {
    if (this.midTurnTimer) {
      clearTimeout(this.midTurnTimer);
      this.midTurnTimer = null;
    }
  }
}

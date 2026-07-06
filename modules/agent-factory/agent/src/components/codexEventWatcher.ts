/**
 * CodexEventWatcher — stream Codex delegation steps to the live run page in
 * real time (issue #2884, EPIC #2702, Phase 2 of Codex observability).
 *
 * When a supervising agent delegates to Codex via codex-bridge, the whole
 * delegation is ONE opaque Bash tool call, so the live check-run page goes dark
 * for its entire duration. Phase 1 (#2753) made Codex's steps visible *after*
 * the fact (run-codex.sh tees `codex exec --json` events and renders them into
 * the Bash result). This watcher makes them visible *while* Codex is running:
 * run-codex.sh additionally appends the same JSONL stream to a stable file
 * (CODEX_EVENTS_FILE); this class tails that file and forwards compact per-step
 * summaries to the live sinks, interleaved with the Claude turns.
 *
 * Hard design constraints (see issue #2884 impact table):
 *  - NEVER-THROW: a watcher bug must never break non-Codex runs. Every poll is
 *    wrapped; errors are swallowed with a single WARN (never re-thrown).
 *  - INERT BY DEFAULT: no Codex delegation → no events file / no new writes →
 *    zero sink calls, zero log noise.
 *  - PARTIAL-LINE SAFE: only complete (newline-terminated) lines are parsed;
 *    a trailing partial line is buffered until the rest arrives (lesson #2828).
 *  - SUMMARIES ONLY: forward event TYPE + command/file NAME, never full command
 *    output — Codex output can carry secret material onto a public page.
 *  - REUSE THE PATCH BUDGET: forwards to CheckRunStreamer via onCodexActivity(),
 *    which shares the existing decaying-throttle/PATCH budget (#2801). This
 *    class adds NO second PATCH path of its own.
 *  - CLEAN STOP: the worker lifecycle owns the watcher (start at run start,
 *    dispose at finalize). The events-file path is stable, so no per-delegation
 *    start/stop coupling is needed.
 */

import * as fs from 'fs';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** CheckRunStreamer sink — shares the existing PATCH budget (see #2801). */
export interface CodexActivitySink {
  onCodexActivity(lines: string[]): void;
}

/** LiveStatusComment sink — one line per activity entry. */
export interface CodexActivityAppender {
  appendActivity(line: string): void;
}

export interface CodexEventWatcherConfig {
  /** Stable JSONL path written by run-codex.sh. Defaults to the wrapper's default. */
  eventsFile?: string;
  /** Check-run sink; forwards a coalesced block per poll (shares PATCH budget). */
  checkRunStreamer?: CodexActivitySink | null;
  /** Live-comment sink; forwards one line per coalesced entry. */
  liveComment?: CodexActivityAppender | null;
  /** Poll cadence in ms (default 1000). */
  pollIntervalMs?: number;
  /** Optional logger (defaults to console.warn). */
  log?: (msg: string) => void;
}

/** A mapped one-liner plus its coalescing key. */
interface CodexStep {
  /** Coalescing key — consecutive same-key steps collapse to "…× N". */
  kind: string;
  /** The rendered, indented one-liner (used when NOT collapsed). */
  line: string;
}

/** Accumulated token usage from `turn.completed` events. */
export interface CodexUsageTotals {
  inputTokens: number;
  outputTokens: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_EVENTS_FILE = '/tmp/codex-events/current.jsonl';
const DEFAULT_POLL_INTERVAL_MS = 1_000;
/** Nested sub-step prefix so lines read as children of the Codex Bash call. */
const PREFIX = '  codex ▸ ';
/** Keep each summary line compact and single-line. */
const MAX_LINE = 120;

// ---------------------------------------------------------------------------
// CodexEventWatcher
// ---------------------------------------------------------------------------

export class CodexEventWatcher {
  private readonly eventsFile: string;
  private readonly checkRunStreamer: CodexActivitySink | null;
  private readonly liveComment: CodexActivityAppender | null;
  private readonly pollIntervalMs: number;
  private readonly warn: (msg: string) => void;

  private timer: ReturnType<typeof setInterval> | null = null;
  private disposed = false;
  private warnedOnce = false;

  /** Byte offset consumed so far from the events file. */
  private offset = 0;
  /** Buffered trailing partial line (no newline yet). */
  private lineBuffer = '';
  /** Codex session id (from thread.started), emitted once for correlation. */
  private sessionId: string | null = null;
  private sessionEmitted = false;

  /**
   * Last-seen cumulative usage per Codex session. Codex reports CUMULATIVE
   * token counts within a session on each `turn.completed` — we track the LAST
   * value per session (not a running sum across turns) to avoid double-counting.
   * Sum across sessions gives total Codex spend for the run.
   */
  private usagePerSession: Map<string, { inputTokens: number; outputTokens: number }> = new Map();

  constructor(cfg: CodexEventWatcherConfig) {
    this.eventsFile = cfg.eventsFile ?? DEFAULT_EVENTS_FILE;
    this.checkRunStreamer = cfg.checkRunStreamer ?? null;
    this.liveComment = cfg.liveComment ?? null;
    this.pollIntervalMs = cfg.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    this.warn = cfg.log ?? ((msg) => console.warn(`[CodexEventWatcher] ${msg}`));
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /** Begin polling the events file. Idempotent; safe to call once at run start. */
  start(): void {
    if (this.timer || this.disposed) return;
    this.timer = setInterval(() => this._poll(), this.pollIntervalMs);
    // Don't keep the event loop alive solely for this poller.
    if (typeof this.timer.unref === 'function') this.timer.unref();
  }

  /** Stop polling and release resources. Idempotent. */
  dispose(): void {
    this.disposed = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /**
   * Aggregate token usage across all Codex sessions/delegations in this run.
   * Returns {inputTokens, outputTokens} — both 0 when no usage has been seen.
   * Safe to call at any time (never throws).
   */
  getTotalUsage(): CodexUsageTotals {
    // Drain any events written since the last 1s poll tick so a turn.completed
    // that landed just before the caller's read (e.g. the final result flush)
    // is counted. _poll() is inert when disposed or when there are no new bytes.
    if (!this.disposed) this._poll();
    let inputTokens = 0;
    let outputTokens = 0;
    for (const usage of this.usagePerSession.values()) {
      inputTokens += usage.inputTokens;
      outputTokens += usage.outputTokens;
    }
    return { inputTokens, outputTokens };
  }

  // -------------------------------------------------------------------------
  // Poll loop (never-throw)
  // -------------------------------------------------------------------------

  /**
   * Read any newly-appended complete lines, map to summaries, and forward one
   * coalesced block to the sinks. Fully inert when the file is absent or has no
   * new bytes. Any error is swallowed with a single WARN — this must never
   * throw into the worker loop.
   */
  private _poll(): void {
    if (this.disposed) return;
    try {
      let stat: fs.Stats;
      try {
        stat = fs.statSync(this.eventsFile);
      } catch {
        // No events file yet → no Codex delegation active. Inert, no noise.
        return;
      }

      // Truncation (new delegation reset the stable file) → restart our view.
      if (stat.size < this.offset) {
        this.offset = 0;
        this.lineBuffer = '';
        this.sessionId = null;
        this.sessionEmitted = false;
      }
      if (stat.size === this.offset) return; // nothing new

      const length = stat.size - this.offset;
      const buf = Buffer.alloc(length);
      const fd = fs.openSync(this.eventsFile, 'r');
      try {
        fs.readSync(fd, buf, 0, length, this.offset);
      } finally {
        fs.closeSync(fd);
      }
      this.offset = stat.size;

      // Partial-line safety: only the portion up to the last newline is
      // complete; hold any trailing fragment for the next poll.
      this.lineBuffer += buf.toString('utf8');
      const newlineIdx = this.lineBuffer.lastIndexOf('\n');
      if (newlineIdx === -1) return; // still no complete line
      const complete = this.lineBuffer.slice(0, newlineIdx);
      this.lineBuffer = this.lineBuffer.slice(newlineIdx + 1);

      const steps: CodexStep[] = [];
      for (const raw of complete.split('\n')) {
        const step = this._mapLine(raw);
        if (step) steps.push(step);
      }
      if (steps.length === 0) return;

      this._forward(this._coalesce(steps));
    } catch (err) {
      // Never-throw: a single WARN, then stay quiet to avoid log floods.
      if (!this.warnedOnce) {
        this.warnedOnce = true;
        this.warn(`poll error (further errors suppressed): ${(err as Error).message}`);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Event → summary mapping (mirrors render-codex-events.py line format)
  // -------------------------------------------------------------------------

  /**
   * Map one raw JSONL line to a compact summary step, or null if it carries no
   * renderable step (envelopes, blank lines, unparseable lines are dropped
   * silently). Captures the session id from thread.started. Never throws.
   */
  private _mapLine(raw: string): CodexStep | null {
    const line = raw.trim();
    if (!line) return null;

    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      return null; // partial/garbage line → drop silently
    }
    if (!obj || typeof obj !== 'object') return null;
    const evt = obj as Record<string, unknown>;

    const etype = evt.type;
    if (etype === 'thread.started') {
      if (typeof evt.thread_id === 'string') this.sessionId = evt.thread_id;
      return null;
    }
    if (etype === 'turn.completed') {
      this._trackUsage(evt);
      return null;
    }
    if (etype !== 'item.completed') return null; // turn.started etc.

    const item = evt.item;
    if (!item || typeof item !== 'object') return null;
    const it = item as Record<string, unknown>;
    const itype = typeof it.type === 'string' ? it.type : 'unknown';

    switch (itype) {
      case 'reasoning': {
        let text = typeof it.text === 'string' ? it.text : '';
        if (!text && Array.isArray(it.summary)) {
          text = (it.summary as unknown[]).map((s) => String(s)).join(' ');
        }
        return { kind: 'thinking', line: `${PREFIX}thinking${text ? `: ${clip(text)}` : ''}` };
      }
      case 'command_execution': {
        // The command STRING is a summary (a name), not command OUTPUT — safe
        // to forward. Full stdout/stderr is never included.
        const cmd = clip(typeof it.command === 'string' ? it.command : '');
        return { kind: 'exec', line: `${PREFIX}exec: ${cmd}` };
      }
      case 'file_change': {
        const changes = it.changes;
        if (Array.isArray(changes) && changes.length > 0) {
          const first = changes[0];
          if (first && typeof first === 'object') {
            const ch = first as Record<string, unknown>;
            const kind = typeof ch.kind === 'string' ? ch.kind : 'edit';
            const path = typeof ch.path === 'string' ? ch.path : '?';
            const extra = changes.length > 1 ? ` (+${changes.length - 1} more)` : '';
            return { kind: 'edit', line: `${PREFIX}edit: ${kind} ${clip(path)}${extra}` };
          }
        }
        return { kind: 'edit', line: `${PREFIX}edit: (file change)` };
      }
      case 'agent_message': {
        const text = typeof it.text === 'string' ? it.text : '';
        return { kind: 'note', line: `${PREFIX}note: ${clip(text)}` };
      }
      default:
        // Unknown item type → one generic line naming only the type (no payload
        // dump, to avoid forwarding potentially sensitive content).
        return { kind: itype, line: `${PREFIX}${itype}` };
    }
  }

  // -------------------------------------------------------------------------
  // Usage tracking (turn.completed → last-per-session, sum across sessions)
  // -------------------------------------------------------------------------

  /**
   * Extract cumulative usage from a `turn.completed` event and store it as
   * the LAST-SEEN value for the current session. Codex reports cumulative
   * counts per session — storing last-seen (not summing turns) prevents
   * double-counting. Never throws.
   */
  private _trackUsage(evt: Record<string, unknown>): void {
    try {
      const usage = evt.usage;
      if (!usage || typeof usage !== 'object') return;
      const u = usage as Record<string, unknown>;

      const inputTokens = (typeof u.input_tokens === 'number' ? u.input_tokens : 0);
      // reasoning_output_tokens is a breakdown of output_tokens (Codex maps it
      // from the Responses API's output_tokens_details.reasoning_tokens), so it
      // must NOT be added on top — the gateway bills output_tokens alone.
      const outputTokens = (typeof u.output_tokens === 'number' ? u.output_tokens : 0);

      // Key by current session id; fall back to a synthetic key for the
      // (unlikely) case where turn.completed arrives before thread.started.
      const key = this.sessionId ?? '__unknown__';
      this.usagePerSession.set(key, { inputTokens, outputTokens });
    } catch {
      // Never-throw: malformed usage → silently ignored.
    }
  }

  // -------------------------------------------------------------------------
  // Coalescing + forwarding
  // -------------------------------------------------------------------------

  /**
   * Collapse consecutive same-kind steps into one "…× N" line; prepend the
   * session id once for the check-run ↔ usage_logs ↔ Codex-session join.
   */
  private _coalesce(steps: CodexStep[]): string[] {
    const out: string[] = [];

    if (!this.sessionEmitted && this.sessionId) {
      this.sessionEmitted = true;
      out.push(`${PREFIX}session ${this.sessionId}`);
    }

    let i = 0;
    while (i < steps.length) {
      let j = i + 1;
      while (j < steps.length && steps[j].kind === steps[i].kind) j++;
      const runLen = j - i;
      if (runLen === 1) {
        out.push(steps[i].line);
      } else {
        // e.g. "  codex ▸ exec × 4"
        out.push(`${PREFIX}${steps[i].kind} × ${runLen}`);
      }
      i = j;
    }
    return out;
  }

  /** Forward one coalesced block to BOTH sinks. Sink throws are swallowed. */
  private _forward(lines: string[]): void {
    if (lines.length === 0) return;

    // Check-run sink: one block, reuses the existing PATCH budget (#2801).
    if (this.checkRunStreamer) {
      try {
        this.checkRunStreamer.onCodexActivity(lines);
      } catch (err) {
        this._warnOnce(`checkRunStreamer sink error: ${(err as Error).message}`);
      }
    }

    // Live-comment sink: one entry per line (appendActivity is itself throttled).
    if (this.liveComment) {
      for (const line of lines) {
        try {
          this.liveComment.appendActivity(line.trim());
        } catch (err) {
          this._warnOnce(`liveComment sink error: ${(err as Error).message}`);
        }
      }
    }
  }

  private _warnOnce(msg: string): void {
    if (this.warnedOnce) return;
    this.warnedOnce = true;
    this.warn(`${msg} (further errors suppressed)`);
  }
}

/** Collapse a possibly multi-line string to a single truncated line. */
function clip(text: string, limit = MAX_LINE): string {
  const flat = String(text).split(/\s+/).join(' ').trim();
  if (flat.length > limit) return flat.slice(0, limit - 1) + '…';
  return flat;
}

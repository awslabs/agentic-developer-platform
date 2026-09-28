/**
 * GitHub auth watchdog (issues #4369, #4430).
 *
 * The writes that actually matter in a run — `git push`, `gh pr create` — happen
 * inside the Claude SDK subprocess, not in the worker's own `execWithFreshToken`
 * calls. So when the installation token went stale, the worker's retry-on-401
 * logic never saw a thing: the subprocess just kept getting `401 Bad credentials`,
 * the model kept retrying, and the run burned turns until it hit the cap and
 * exited having produced nothing. Meanwhile the heartbeat comment kept updating,
 * so the run looked healthy right up to the point it failed.
 *
 * This module watches the SDK message stream (and, since #4430, the check-run
 * streamer's PATCH failures) for that signature and escalates: first force a
 * token refresh (which rewrites the token file the subprocess reads), and if
 * 401s continue even after that, give up fast so the task can be retried in a
 * fresh pod instead of dying silently at the turn cap.
 *
 * History (#4430): the first shipped version counted CONSECUTIVE failures and
 * reset the streak on any non-failure output. In the real stream, failures
 * arrive interleaved with assistant prose ("Let me retry that..."), so the
 * streak never reached its threshold — 27 and 36 real 401s produced zero
 * escalations. The current design uses a sliding time window instead, and only
 * treats a *successful GitHub write* as evidence of recovery. Non-auth prose is
 * not proof of health — that conflation was the bug.
 *
 * Deliberately pure — no I/O, no imports, decisions returned rather than acted
 * on, clock injected. `agent-worker.ts` runs `main()` at import time and so
 * cannot be imported by a test; escalation logic living there would be
 * permanently untestable, which is how the original bug survived so long.
 */

/** What the caller should do in response to the sighting just recorded. */
export type WatchdogAction =
  /** Nothing unusual — carry on. */
  | 'none'
  /**
   * Enough auth failures inside the window to rule out a blip: force a token
   * refresh (rewriting the token file) and let the run continue.
   */
  | 'force_refresh'
  /**
   * Still failing well after a forced refresh, so the credential itself is dead
   * (installation suspended/revoked, or a token holder we cannot reach). Abort
   * the run for retry — looping to the turn cap produces nothing and blocks the
   * queue slot for the rest of the run.
   */
  | 'abort';

export interface AuthWatchdogOptions {
  /**
   * Auth failures inside the window before escalating. Default 3 — a single
   * 401 is routinely transient (a race against a refresh, a GitHub blip), and
   * aborting a healthy run on one is worse than the bug being fixed.
   */
  threshold?: number;
  /**
   * Width of the sliding window. Default 10 minutes: 3 failures inside 10 min
   * is a cluster, not a blip; 3 failures spread over an hour with successful
   * writes between them is not.
   */
  windowMs?: number;
  /**
   * How long after a forced refresh failures may keep arriving before we give
   * up. Default 5 minutes — several PATCH cycles and several agent turns, long
   * enough that a genuine re-mint would have taken effect, short enough to
   * beat a turn-cap burn. Worst case first-401 → abort ≈ windowMs + this.
   */
  abortAfterRefreshMs?: number;
  /**
   * Clock, injected so tests can drive a fake one. Defaults to Date.now. The
   * class never calls Date.now() directly anywhere else.
   */
  now?: () => number;
}

/**
 * Does this text carry the signature of a GitHub auth failure?
 *
 * Matched case-insensitively and on substrings because the text is whatever
 * git/gh/the API happened to print into a tool result — there is no structured
 * status code to read at this layer.
 */
export function looksLikeAuthFailure(text: string): boolean {
  if (!text) return false;
  const haystack = text.toLowerCase();
  return (
    haystack.includes('bad credentials') ||
    haystack.includes('401 unauthorized') ||
    haystack.includes('http 401') ||
    haystack.includes('status: 401') ||
    // `remote: Invalid username or password` / `Authentication failed` is what a
    // stale token looks like to git-over-HTTPS, which never says "401".
    haystack.includes('authentication failed') ||
    haystack.includes('invalid username or password')
  );
}

/**
 * Does this text evidence a SUCCESSFUL GitHub write?
 *
 * Only a write that went through proves the credential is healthy — assistant
 * prose between failures does not (#4430). Matched on the concrete success
 * output of the writes that matter in a run: `git push` and `gh pr create`.
 */
export function looksLikeSuccessfulWrite(text: string): boolean {
  if (!text) return false;
  const haystack = text.toLowerCase();
  return (
    // gh/worker-side summaries: "pushed 1 commit", "Branch pushed"
    haystack.includes('pushed') ||
    // git push ref-update output: "To https://github.com/o/r.git\n  abc..def  main -> main"
    (haystack.includes('github.com') && haystack.includes(' -> ')) ||
    haystack.includes('everything up-to-date') ||
    // gh pr create prints the new PR URL on success
    /https:\/\/github\.com\/\S+\/pull\/\d+/i.test(text)
  );
}

/**
 * Tracks GitHub auth failures in a sliding time window across SDK messages and
 * check-run PATCH errors, and decides when to refresh and when to give up.
 */
export class AuthWatchdog {
  private readonly threshold: number;
  private readonly windowMs: number;
  private readonly abortAfterRefreshMs: number;
  private readonly now: () => number;

  /** Timestamps of auth failures still inside the window, oldest first. */
  private failureTimestamps: number[] = [];
  /** When a forced refresh was requested, or null if none is outstanding. */
  private refreshedAtMs: number | null = null;

  constructor(options: AuthWatchdogOptions = {}) {
    this.threshold = options.threshold ?? 3;
    this.windowMs = options.windowMs ?? 10 * 60_000;
    this.abortAfterRefreshMs = options.abortAfterRefreshMs ?? 5 * 60_000;
    this.now = options.now ?? (() => Date.now());
  }

  /**
   * Record one observed chunk of output.
   *
   * @param text Text from an SDK message (assistant text, tool_result content)
   *             or a check-run PATCH error message.
   * @returns The action the caller should take.
   */
  observe(text: string): WatchdogAction {
    const t = this.now();

    if (!looksLikeAuthFailure(text)) {
      if (looksLikeSuccessfulWrite(text)) {
        // A write went through, so the credential works: any earlier cluster is
        // over, and a later cluster is a new incident rather than a continuation.
        this.reset();
      }
      // Anything else is non-auth prose, which proves nothing either way
      // (#4430) — the model narrating its retries must not erase the evidence
      // of a dying token.
      return 'none';
    }

    this.failureTimestamps.push(t);
    // Drop entries older than the window, and cap the list at threshold — on a
    // long run nothing beyond the newest `threshold` sightings can change the
    // verdict, so the list stays bounded.
    this.failureTimestamps = this.failureTimestamps
      .filter((ts) => t - ts <= this.windowMs)
      .slice(-this.threshold);

    if (this.refreshedAtMs !== null) {
      // A refresh is outstanding. Give it time to take effect; if failures are
      // still arriving past the deadline, the credential itself is dead.
      return t - this.refreshedAtMs >= this.abortAfterRefreshMs ? 'abort' : 'none';
    }

    if (this.failureTimestamps.length >= this.threshold) {
      // First escalation: assume a stale token and re-mint. Latch so continued
      // failures are judged against the post-refresh deadline, not re-refreshed.
      this.refreshedAtMs = t;
      return 'force_refresh';
    }

    return 'none';
  }

  /** Clear all failure history (called only on evidence of a successful write). */
  reset(): void {
    this.failureTimestamps = [];
    this.refreshedAtMs = null;
  }

  /** Auth failures currently inside the window — for logging/tests. */
  get failureCount(): number {
    return this.failureTimestamps.length;
  }

  /** Whether a forced refresh is outstanding (not yet evidenced recovered). */
  get hasForcedRefresh(): boolean {
    return this.refreshedAtMs !== null;
  }
}

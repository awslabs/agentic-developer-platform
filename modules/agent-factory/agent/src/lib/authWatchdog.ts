/**
 * GitHub auth watchdog (issue #4369).
 *
 * The writes that actually matter in a run — `git push`, `gh pr create` — happen
 * inside the Claude SDK subprocess, not in the worker's own `execWithFreshToken`
 * calls. So when the installation token went stale, the worker's retry-on-401
 * logic never saw a thing: the subprocess just kept getting `401 Bad credentials`,
 * the model kept retrying, and the run burned turns until it hit the cap and
 * exited having produced nothing. Meanwhile the heartbeat comment kept updating,
 * so the run looked healthy right up to the point it failed.
 *
 * This module watches the SDK message stream for that signature and escalates:
 * first force a token refresh (which rewrites the token file the subprocess
 * reads), and if 401s continue even after that, give up fast so the task can be
 * retried in a fresh pod instead of dying silently at the turn cap.
 *
 * Deliberately pure — no I/O, no imports, decisions returned rather than acted
 * on. `agent-worker.ts` runs `main()` at import time and so cannot be imported by
 * a test; escalation logic living there would be permanently untestable, which is
 * how the original bug survived so long.
 */

/** What the caller should do in response to the sighting just recorded. */
export type WatchdogAction =
  /** Nothing unusual — carry on. */
  | 'none'
  /**
   * Enough consecutive auth failures to rule out a blip: force a token refresh
   * (rewriting the token file) and let the run continue.
   */
  | 'force_refresh'
  /**
   * Still failing after a forced refresh, so the credential itself is dead
   * (installation suspended/revoked, or the subprocess is holding a token we
   * cannot reach). Abort the run for retry — looping to the turn cap produces
   * nothing and blocks the queue slot for the rest of the run.
   */
  | 'abort';

export interface AuthWatchdogOptions {
  /**
   * Consecutive auth failures before escalating. Default 3 — a single 401 is
   * routinely transient (a race against a refresh, a GitHub blip), and aborting a
   * healthy run on one is worse than the bug being fixed.
   */
  threshold?: number;
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
 * Tracks consecutive GitHub auth failures across SDK messages and decides when to
 * refresh and when to give up.
 */
export class AuthWatchdog {
  private readonly threshold: number;
  private consecutiveFailures = 0;
  private refreshed = false;

  constructor(options: AuthWatchdogOptions = {}) {
    this.threshold = options.threshold ?? 3;
  }

  /**
   * Record one observed chunk of output.
   *
   * @param text Text from an SDK message (assistant text, or tool_result content).
   * @returns The action the caller should take.
   */
  observe(text: string): WatchdogAction {
    if (!looksLikeAuthFailure(text)) {
      // Any clean output means auth is working, so a later cluster of 401s is a
      // new incident rather than a continuation of this one. Without this reset a
      // run that saw scattered single 401s over an hour would eventually abort
      // despite never actually being broken.
      this.reset();
      return 'none';
    }

    this.consecutiveFailures++;

    if (this.consecutiveFailures < this.threshold) {
      return 'none';
    }

    if (!this.refreshed) {
      // First escalation: assume a stale token and re-mint. Latch so that the
      // NEXT cluster is understood as "refreshing did not help".
      this.refreshed = true;
      this.consecutiveFailures = 0;
      return 'force_refresh';
    }

    return 'abort';
  }

  /** Clear the failure streak (called on any non-auth-failure output). */
  reset(): void {
    this.consecutiveFailures = 0;
  }

  /** Consecutive auth failures currently recorded — for logging/tests. */
  get failureCount(): number {
    return this.consecutiveFailures;
  }

  /** Whether a forced refresh has already been requested in this run. */
  get hasForcedRefresh(): boolean {
    return this.refreshed;
  }
}

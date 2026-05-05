/**
 * LCM Scrubber — redacts registered sensitive values from text before persistence.
 *
 * Task-scoped: each task run gets a fresh scrubber instance. Registered patterns
 * live for the duration of the task and are destroyed on completion. No cross-task
 * leakage is possible.
 *
 * Issue #137: Vault Phase 4
 */

/**
 * Per-task registry of (value, replacement) pairs applied to any string written
 * to persistence (DDB chat history, memory writes, S3 text artifacts).
 */
export class Scrubber {
  private readonly patterns: Map<string, string> = new Map();

  /**
   * Register a sensitive value for scrubbing. Values shorter than 8 characters
   * are ignored to avoid over-scrubbing common words or short strings.
   */
  registerSensitiveValue(value: string, replacement: string): void {
    if (value && value.length >= 8) {
      this.patterns.set(value, replacement);
    }
  }

  /**
   * Replace all registered sensitive values in the given text.
   * Uses split/join for literal replacement (avoids regex escaping issues
   * for credential values containing $, ., +, etc.).
   */
  scrub(text: string): string {
    if (this.patterns.size === 0) return text;
    let result = text;
    for (const [value, replacement] of this.patterns) {
      result = result.split(value).join(replacement);
    }
    return result;
  }

  /** Returns true if at least one sensitive value has been registered. */
  get hasPatterns(): boolean {
    return this.patterns.size > 0;
  }
}

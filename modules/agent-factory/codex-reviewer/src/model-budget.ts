/** One model execution allowance across retained turns; provider waits cost no model time. */
export class ModelExecutionBudget {
  private readonly initial: number;
  constructor(private remaining: number, private now = () => performance.now()) {
    if (!Number.isFinite(remaining) || remaining <= 0) throw new Error("Invalid reviewer model deadline");
    this.initial = remaining;
  }
  /** Milliseconds of model execution still available. */
  get remainingMs(): number { return Math.max(0, this.remaining); }
  get initialMs(): number { return this.initial; }
  /** Fraction of the allowance already consumed, 0..1. */
  get usedShare(): number { return Math.min(1, Math.max(0, 1 - this.remaining / this.initial)); }
  async run<T>(work: (signal: AbortSignal) => Promise<T>): Promise<T> {
    if (this.remaining <= 0) throw new Error("Reviewer model execution deadline exhausted");
    const started = this.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("Reviewer model execution deadline exhausted")), this.remaining);
    timer.unref();
    try { return await work(controller.signal); }
    finally {
      clearTimeout(timer);
      this.remaining -= this.now() - started;
    }
  }
}

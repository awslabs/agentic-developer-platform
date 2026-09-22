/** One model execution allowance across retained turns; provider waits cost no model time. */
export class ModelExecutionBudget {
  constructor(private remaining: number, private now = () => performance.now()) {
    if (!Number.isFinite(remaining) || remaining <= 0) throw new Error("Invalid reviewer model deadline");
  }
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

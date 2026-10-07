import { setTimeout as delay } from "node:timers/promises";

/** Only an observed HTTP rejection may be retried; thrown/unknown outcomes escape. */
export class ModelHttpError extends Error {
  constructor(readonly status: number) {
    super(`Model request failed: HTTP ${status}`);
    if (!Number.isInteger(status) || status < 400 || status > 599) throw new Error("Invalid model HTTP status");
  }
}

export type HttpRejection = { httpStatus: number };
export async function retryModelHttp<T>(
  attempt: () => Promise<T | HttpRejection>, signal: AbortSignal,
  wait: (ms: number, signal: AbortSignal) => Promise<void> = async (ms, active) => { await delay(ms, undefined, { signal: active }); },
): Promise<T> {
  for (let index = 0; ; index++) {
    signal.throwIfAborted();
    const result = await attempt();
    if (typeof result !== "object" || result === null || !("httpStatus" in result)) return result as T;
    const error = new ModelHttpError(result.httpStatus as number);
    if (index >= 2 || ![429, 500, 502, 503, 504].includes(error.status)) throw error;
    await wait(1000 * 2 ** index, signal);
  }
}

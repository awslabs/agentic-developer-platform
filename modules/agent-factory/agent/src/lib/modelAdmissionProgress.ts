/** Serialized diagnostics for all model requests in one worker; no admission authority. */
export type AdmissionWait = 'budget_reservations_pending' | 'budget_unavailable';
export type AdmissionStatus = AdmissionWait | 'not_waiting';

export function modelAdmissionProgress(
  report: (status: AdmissionStatus, remaining: number) => Promise<void>,
  now: () => number = Date.now,
) {
  const waits = new Map<symbol, { status: AdmissionWait; deadline: number }>();
  let nextReportAt = 0;
  let dirty = false;
  let running = false;
  let current = Promise.resolve();
  const publish = () => {
    dirty = true;
    if (running) return;
    running = true;
    current = (async () => {
      while (dirty) {
        dirty = false;
        const entries = [...waits.values()];
        const status = entries.length
          ? entries.some(entry => entry.status === 'budget_unavailable') ? 'budget_unavailable' : 'budget_reservations_pending'
          : 'not_waiting';
        const remaining = entries.length ? Math.max(0, Math.max(...entries.map(entry => entry.deadline)) - now()) : 0;
        // Await acknowledgement or timeout before publishing the latest state.
        // Gateway's sequence fence also rejects late writes after a timeout.
        try { await report(status, remaining); } catch { /* Diagnostics cannot block model work. */ }
      }
      running = false;
    })();
  };
  return {
    wait(key: symbol, status: AdmissionWait, deadline: number) {
      const previous = waits.get(key);
      waits.set(key, { status, deadline });
      if (!previous || previous.status !== status || now() >= nextReportAt) {
        nextReportAt = now() + 60_000;
        publish();
      }
    },
    clear(key: symbol) {
      if (waits.delete(key)) publish();
    },
    settled: () => current,
  };
}

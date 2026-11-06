import { modelAdmissionProgress } from './modelAdmissionProgress';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

test('a delayed wait acknowledgement cannot overtake a fast admission clear', async () => {
  const acknowledgement = deferred();
  const persisted: string[] = [];
  const report = jest.fn(async (status: string) => {
    if (status !== 'not_waiting') await acknowledgement.promise;
    persisted.push(status);
  });
  const reporter = modelAdmissionProgress(report, () => 1000);
  const request = Symbol();
  reporter.wait(request, 'budget_reservations_pending', 4_201_000);
  reporter.clear(request);
  expect(report).toHaveBeenCalledTimes(1);
  expect(persisted).toEqual([]);
  acknowledgement.resolve();
  await reporter.settled();
  expect(persisted).toEqual(['budget_reservations_pending', 'not_waiting']);
});

test('one admitted request cannot clear a different request that still waits', async () => {
  const persisted: string[] = [];
  const reporter = modelAdmissionProgress(async status => { persisted.push(status); }, () => 1000);
  const first = Symbol(), second = Symbol();
  reporter.wait(first, 'budget_reservations_pending', 10000);
  reporter.wait(second, 'budget_unavailable', 10000);
  await reporter.settled();
  reporter.clear(first);
  await reporter.settled();
  expect(persisted.at(-1)).toBe('budget_unavailable');
  expect(persisted).not.toContain('not_waiting');
  reporter.clear(second);
  await reporter.settled();
  expect(persisted.at(-1)).toBe('not_waiting');
});

test('rapid denials are coalesced, heartbeat refreshes, and failed telemetry cannot retain a wait', async () => {
  let now = 1000;
  const report = jest.fn(async () => { throw new Error('diagnostics unavailable'); });
  const reporter = modelAdmissionProgress(report, () => now);
  const request = Symbol();
  reporter.wait(request, 'budget_reservations_pending', 4_201_000);
  await reporter.settled();
  for (let i = 0; i < 10; i++) reporter.wait(request, 'budget_reservations_pending', 4_201_000);
  expect(report).toHaveBeenCalledTimes(1);
  now += 60_000;
  reporter.wait(request, 'budget_reservations_pending', 4_201_000);
  await reporter.settled();
  reporter.clear(request);
  await reporter.settled();
  expect(report).toHaveBeenLastCalledWith('not_waiting', 0);
  expect(report).toHaveBeenCalledTimes(3);
});

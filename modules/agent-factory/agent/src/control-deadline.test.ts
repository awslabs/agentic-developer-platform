import { controlDeadlineAt } from './control-deadline';
import { PauseGate } from './pause-gate';

test('bootstrap delay and token renewal cannot extend the original workload deadline', () => {
  const started = Date.parse('2026-09-19T10:00:00Z');
  const registered = started + 10 * 60_000;
  const env = { ADP_POD_DEADLINE_AT: new Date(started + 3600_000).toISOString(),
    ADP_CONTROL_TOKEN_EXPIRES_AT: new Date(registered + 3600_000).toISOString() };
  const gate = new PauseGate({ deadlineAt: () => controlDeadlineAt(env), now: () => started + 55 * 60_000 });
  expect(gate.safeBudget()).toBe(4 * 60_000);
  env.ADP_CONTROL_TOKEN_EXPIRES_AT = new Date(started + 10 * 3600_000).toISOString();
  expect(gate.safeBudget()).toBe(4 * 60_000);
});

test.each([undefined, '', 'invalid'])('missing or invalid workload deadline %s refuses pause even with a fresh token', value => {
  const gate = new PauseGate({ deadlineAt: () => controlDeadlineAt({ ADP_POD_DEADLINE_AT: value,
    ADP_CONTROL_TOKEN_EXPIRES_AT: new Date(Date.now() + 3600_000).toISOString() }) });
  expect(gate.safeBudget()).toBeNull();
});

test('credential expiry remains an additional tighter bound', () => {
  expect(controlDeadlineAt({ ADP_POD_DEADLINE_AT: '2026-09-19T12:00:00Z',
    ADP_CONTROL_TOKEN_EXPIRES_AT: '2026-09-19T11:00:00Z' })).toBe(Date.parse('2026-09-19T11:00:00Z'));
});

test('an absent token expiry cannot extend a known absolute workload deadline', () => {
  expect(controlDeadlineAt({ ADP_POD_DEADLINE_AT: '2026-09-19T12:00:00Z' })).toBe(Date.parse('2026-09-19T12:00:00Z'));
});

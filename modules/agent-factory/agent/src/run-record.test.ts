import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { RunRecordCapture, parseTaskChecklist } from './run-record';

let directory: string;
beforeEach(() => { directory = mkdtempSync(join(tmpdir(), 'adp-record-test-')); process.env.ADP_MESSAGE_ID = 'run-42'; });
afterEach(() => { rmSync(directory, { recursive: true, force: true }); delete process.env.ADP_MESSAGE_ID; });
function capture() { return new RunRecordCapture({ persona: 'reviewer', model: 'test', repo: 'acme/repo', issueNumber: 42 }, join(directory, 'record.json')); }
test('retains the first plan, task transitions, removal and reopening across incremental saves', () => {
  const c = capture();
  c.checklist('- ☑ Previously done\n- ☐ Fix parser (in progress)\n- ☐ Old task');
  c.checklist('- ☑ Previously done\n- ☑ Fix parser\n- ☐ Renamed task');
  c.checklist('- ☐ Previously done\n- ☑ Fix parser\n- ☐ Renamed task');
  const saved = JSON.parse(readFileSync(join(directory, 'record.json'), 'utf8'));
  expect(saved.invocation_id).toBe('run-42');
  expect(saved.first_checklist.tasks.map((t: any) => t.status)).toEqual(['completed', 'in_progress', 'pending']);
  expect(saved.latest_checklist.tasks.map((t: any) => t.text)).toEqual(['Previously done', 'Fix parser', 'Renamed task']);
  expect(saved.task_transitions.map((t: any) => `${t.from}>${t.to}`)).toEqual(['in_progress>completed', 'not_listed>pending', 'pending>not_listed', 'completed>pending']);
  expect(saved.capture_closed_at).toBeUndefined();
  c.session('repair-session'); c.session('inspection-session'); c.session('repair-session');
  c.close(); c.checklist('- ☑ Everything');
  expect(c.record.session_ids).toEqual(['repair-session', 'inspection-session']);
  expect(c.record.latest_checklist?.tasks).toHaveLength(3);
  expect(c.record).not.toHaveProperty('outcome');
  expect(c.markdown()).toMatch(/^<!-- adp-run-record:v1 /);
});
test('rejects duplicate/secret/oversized tasks and redacts reported evidence', () => {
  process.env.TEST_API_KEY = 'a-private-api-key';
  try {
    expect(parseTaskChecklist('- ☐ a-private-api-key')).toBeUndefined();
    expect(parseTaskChecklist('- ☐ Duplicate\n- ☑ Duplicate')).toBeUndefined();
    expect(parseTaskChecklist('- ☐ ' + 'x'.repeat(1025))).toBeUndefined();
    expect(parseTaskChecklist('I did all of the work')).toBeUndefined();
    const c = capture(); c.evidence('Used a-private-api-key');
    expect(c.markdown()).not.toContain('a-private-api-key');
    expect(c.record.evidence[0].text).toContain('[redacted]');
  } finally { delete process.env.TEST_API_KEY; }
});
test('bounds long runs while preserving the original baseline', () => {
  const c = capture();
  // Avoid one git process per checklist in this bound test; evidence is also bounded.
  for (let i = 0; i < 40; i++) { c.evidence(`Observation ${i}`); c.session(`session-${i}`); }
  expect(c.record.evidence).toHaveLength(32);
  expect(c.record.session_ids).toHaveLength(32);
  expect(c.record.evidence[0].text).toBe('Observation 8');
});

test('archives the final checklist and sanitized closure ahead of transcript history', () => {
  const c = capture();
  c.checklist('- ☑ Workspace screen\n- ☐ Browser validation\n- ⛔ Live demo — deferred to evaluator');
  c.checklist('- ☑ Workspace screen\n- ☑ Browser validation\n- ⛔ Live demo — deferred to evaluator');
  process.env.TEST_API_KEY = 'a-private-api-key';
  try {
    c.closure({ summary: 'The workspace screen is ready for integration.', completed: ['Browser validation passed.'],
      remaining: ['Run the live demo with a-private-api-key.'], delivery: 'Pull request merged.', reporting_notes: [] });
    c.close();
    const record = JSON.parse(readFileSync(join(directory, 'record.json'), 'utf8'));
    expect(record.latest_checklist.tasks.map((t: any) => t.status)).toEqual(['completed', 'completed', 'pending']);
    expect(record.closure_report.delivery).toBe('Pull request merged.');
    expect(record.closure_report.remaining[0]).toContain('[redacted]');
    expect(c.markdown().indexOf('## Closure report')).toBeLessThan(c.markdown().indexOf('## Run record'));
    expect(c.markdown()).not.toContain('a-private-api-key');
  } finally { delete process.env.TEST_API_KEY; }
});

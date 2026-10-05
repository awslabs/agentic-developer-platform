import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { writeFailureReport } from './failure-report';

test('original failure is bounded and known credentials are redacted before persistence', () => {
  const directory = mkdtempSync(join(tmpdir(), 'failure-test-'));
  const previous = process.env.ADP_FAILURE_REPORT_FILE;
  const token = process.env.GITHUB_TOKEN;
  try {
    process.env.ADP_FAILURE_REPORT_FILE = join(directory, 'failure.json');
    process.env.GITHUB_TOKEN = 'test-installation-token';
    writeFailureReport(new Error('Bad credentials test-installation-token Bearer abcdef ' + 'x'.repeat(9000)));
    const report = JSON.parse(readFileSync(process.env.ADP_FAILURE_REPORT_FILE, 'utf8'));
    expect(report.status).toBe('agent_failed');
    expect(report.error.message).toContain('Bad credentials');
    expect(report.error.message).not.toContain('test-installation-token');
    expect(report.error.message).not.toContain('abcdef');
    expect(report.error.message.length).toBe(8192);
  } finally {
    if (previous === undefined) delete process.env.ADP_FAILURE_REPORT_FILE; else process.env.ADP_FAILURE_REPORT_FILE = previous;
    if (token === undefined) delete process.env.GITHUB_TOKEN; else process.env.GITHUB_TOKEN = token;
    rmSync(directory, { recursive: true, force: true });
  }
});

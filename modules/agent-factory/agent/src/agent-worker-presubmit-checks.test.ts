import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-worker pre-submit checks contract', () => {
  it('requires completed work before opening a PR, allowing branch checkpoints', () => {
    expect(source).toContain('## Pre-submit checks (MANDATORY before requesting review)');
    expect(source).toContain('Do not create draft PRs');
    expect(source).toContain('before opening a ready PR or requesting review');
    expect(source).toContain('Incomplete branch checkpoints may be pushed with check status disclosed');
    expect(source).not.toContain('fix the underlying issue before pushing');
  });

  it('contains the ruff check command string', () => {
    expect(source).toContain('ruff check');
  });

  it('contains the ruff format --check command string', () => {
    expect(source).toContain('ruff format --check');
  });

  it('contains the npx tsc --noEmit command string', () => {
    expect(source).toContain('npx tsc --noEmit');
  });

  it('contains the terraform fmt -check command string', () => {
    expect(source).toContain('terraform fmt -check');
  });

  it('contains the "don\'t clean up unrelated debt in the same PR" instruction', () => {
    expect(source).toContain("Don't clean up unrelated debt in the same PR");
  });

  it('gives developers a direct PR handoff while retaining review validation guidance', () => {
    const start = source.indexOf("${AGENT_TYPE === 'developer' ? `## Developer delivery");
    const end = source.indexOf('## Pre-submit checks (MANDATORY before requesting review)', start);
    const developerGuidance = source.slice(start, end);
    expect(start).toBeGreaterThan(-1);
    expect(developerGuidance).toContain('Once that PR is open, stop developer work');
    expect(developerGuidance).toContain('Codex owns review, additional validation, repairs and merge');
    expect(developerGuidance).not.toContain('adp-validate verify');
  });
});

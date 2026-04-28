import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-worker pre-submit checks contract', () => {
  it('contains the "Pre-submit checks (MANDATORY before creating a PR)" heading', () => {
    expect(source).toContain('## Pre-submit checks (MANDATORY before creating a PR)');
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

  it('appears in the shared scaffolding, not inside per-agent-type conditionals', () => {
    const presubmitIndex = source.indexOf('## Pre-submit checks (MANDATORY before creating a PR)');
    expect(presubmitIndex).toBeGreaterThan(-1);

    // Must appear after the shared Step 3 section
    const step3Execute = source.indexOf('### Step 3: Execute Your Plan');
    expect(step3Execute).toBeGreaterThan(-1);
    expect(presubmitIndex).toBeGreaterThan(step3Execute);

    // Must appear BEFORE the first per-agent AGENT_TYPE conditional
    const instructionsStart = source.indexOf('## Instructions');
    expect(instructionsStart).toBeGreaterThan(-1);
    const firstConditional = source.indexOf("AGENT_TYPE === '", instructionsStart);
    expect(firstConditional).toBeGreaterThan(-1);
    expect(presubmitIndex).toBeLessThan(firstConditional);
  });
});
